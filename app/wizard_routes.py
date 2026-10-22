# app/wizard_routes.py
import os
import glob
import logging
import difflib
from datetime import datetime
from flask import Blueprint, jsonify, request, render_template, send_file
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
from app import db
from app.models import Application, StandardPack, Criterion, Response, EvidenceDocument, Portfolio, TableRowData, ApplicationMember, Users, SubsectionAssignment
from app.report_generator import generate_report

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

wizard_bp = Blueprint("wizard", __name__, template_folder="templates", static_folder="static")

GEMINI_MODEL_NAME = "gemini-2.5-flash"
DRAFT_MAX_OUTPUT_TOKENS = 8000
DRAFT_REQUEST_TIMEOUT_MS = 120000

SECTION_TONE_GUIDANCE = {
    "I": "Emphasize policy, process, and organizational structure.",
    "II": "Emphasize curriculum design, learning objectives, and educational rationale.",
    "III": "Emphasize faculty qualifications, credentials, and oversight processes.",
    "IV": "Emphasize transparency, accuracy, and ethical practice in recruitment/advertising.",
    "V": "Emphasize fairness, consistency, and documentation in admissions decisions.",
    "VI": "Emphasize student support, accessibility, and responsiveness of services.",
    "VII": "Emphasize measurable outcomes, tracking, and evidence-based assessment.",
}

def get_report_output_dir():
    report_dir = os.environ.get("REPORT_OUTPUT_DIR")
    if not report_dir:
        base_dir = os.path.abspath(os.path.dirname(__file__))
        report_dir = os.path.join(base_dir, "..", "instance", "reports")
    else:
        report_dir = os.path.abspath(report_dir)
    return report_dir

def verify_application_ownership(application_id):
    try:
        app_record = Application.query.get(application_id)
        if not app_record:
            return None, jsonify({"error": "Application not found"}), 404
        is_owner = app_record.portfolio and app_record.portfolio.owner_user_id == current_user.id
        is_member = ApplicationMember.query.filter_by(
            application_id=application_id, user_id=current_user.id
        ).first() is not None
        if not is_owner and not is_member:
            return None, jsonify({"error": "Forbidden", "message": "You do not have access to this application"}), 403
        return app_record, None, None
    except Exception as e:
        logger.error(f"Error verifying application ownership for app {application_id}: {str(e)}", exc_info=True)
        return None, jsonify({"error": "Internal server error"}), 500

def check_edit_permission(application_id, section_code, subsection_code):
    # Call AFTER verify_application_ownership already succeeded.
    # Returns (True, None, None) if editing is allowed, or 
    # (False, error_response, status_code) if blocked.
    app_record = Application.query.get(application_id)
    if not app_record.enforce_assignments:
        return True, None, None
    if app_record.portfolio.owner_user_id == current_user.id:
        return True, None, None  # owner can always edit regardless of assignment
    assignment = SubsectionAssignment.query.filter_by(
        application_id=application_id, section_code=section_code, subsection_code=subsection_code
    ).first()
    if not assignment:
        return True, None, None  # unassigned subsections stay open to anyone with access
    if assignment.assigned_user_id != current_user.id:
        return False, jsonify({"error": "Forbidden", "message": "This section is assigned to another team member"}), 403
    return True, None, None

TABLE_MAX_ROWS = 500
TABLE_MAX_CELL_CHARS = 2000


def table_row_has_data(row_data):
    """A table row counts only if at least one cell has non-blank text."""
    return isinstance(row_data, dict) and any(
        isinstance(v, str) and v.strip() for v in row_data.values())


def table_has_data(response_id):
    """Shared completeness rule for table_rows answers, used by every completion aggregator."""
    rows = TableRowData.query.filter_by(response_id=response_id).all()
    return any(table_row_has_data(r.row_data) for r in rows)


def calculate_completeness(application_id):
    app_record = Application.query.get(application_id)
    if not app_record:
        raise ValueError("Application not found")

    pack_id = app_record.standard_pack_id
    all_criteria = Criterion.query.filter_by(standard_pack_id=pack_id).all()
    responses = Response.query.filter_by(application_id=application_id).all()
    response_map = {r.criterion_id: r for r in responses}

    subsections_map = {}
    for c in all_criteria:
        key = (c.section_code, c.subsection_code)
        if key not in subsections_map:
            subsections_map[key] = []
        subsections_map[key].append(c)

    subsection_na_map = {}
    for key, crits in subsections_map.items():
        all_na = True if crits else False
        for c in crits:
            resp = response_map.get(c.id)
            if not resp or not resp.not_applicable:
                all_na = False
                break
        subsection_na_map[key] = all_na

    total_required = len(all_criteria)
    completed = 0
    incomplete_criteria = []

    for c in all_criteria:
        sub_key = (c.section_code, c.subsection_code)
        is_sub_na = subsection_na_map.get(sub_key, False)
        resp = response_map.get(c.id)

        has_narrative = resp and resp.narrative_text and resp.narrative_text.strip() != ""
        criterion_na = resp and resp.not_applicable

        has_exhibit = False
        if c.answer_type == "exhibit" and resp:
            exhibit_count = EvidenceDocument.query.filter_by(response_id=resp.id).count()
            has_exhibit = exhibit_count > 0

        has_table_rows = False
        if c.answer_type == "table_rows" and resp:
            has_table_rows = table_has_data(resp.id)

        is_complete = has_narrative or is_sub_na or criterion_na or has_exhibit or has_table_rows

        if is_complete:
            completed += 1
        else:
            incomplete_criteria.append({
                "criterion_id": c.id,
                "section_name": c.section_name,
                "subsection_name": c.subsection_name,
                "question_text": c.question_text
            })

    percentage = round((completed / total_required * 100), 1) if total_required > 0 else 100.0

    return {
        "total_required": total_required,
        "completed": completed,
        "percentage": percentage,
        "incomplete_criteria": incomplete_criteria
    }

@wizard_bp.route("/wizard", methods=["GET"])
@login_required
def wizard_page():
    return render_template("wizard.html")

@wizard_bp.route("/api/applications", methods=["GET"])
@login_required
def get_user_applications():
    try:
        portfolios = Portfolio.query.filter_by(owner_user_id=current_user.id).all()
        portfolio_ids = [p.id for p in portfolios]
        
        owned_apps = Application.query.filter(Application.portfolio_id.in_(portfolio_ids)).all() if portfolio_ids else []
        
        member_rows = ApplicationMember.query.filter_by(user_id=current_user.id).all()
        member_app_ids = [m.application_id for m in member_rows]
        member_apps = Application.query.filter(Application.id.in_(member_app_ids)).all() if member_app_ids else []

        seen_ids = set()
        combined_apps = []
        for app in owned_apps + member_apps:
            if app.id not in seen_ids:
                seen_ids.add(app.id)
                combined_apps.append(app)

        result = [{
            "id": app.id,
            "portfolio_id": app.portfolio_id,
            "standard_pack_id": app.standard_pack_id,
            "status": app.status,
            "due_date": app.due_date.isoformat() if app.due_date else None,
            "institution_name": app.portfolio.institution_name if app.portfolio else None,
            "founding_year": app.portfolio.founding_year if app.portfolio else None,
            "mission_statement": app.portfolio.mission_statement if app.portfolio else None,
            "created_at": app.created_at.isoformat(),
            "updated_at": app.updated_at.isoformat() if app.updated_at else None
        } for app in combined_apps]
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Error fetching user applications for user {current_user.id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


def _mark_subsection_not_applicable(application_id, section_code, subsection_code, not_applicable_val=True):
    app_record = Application.query.get(application_id)
    if not app_record:
        raise ValueError("Application not found")
    pack_id = app_record.standard_pack_id

    all_crit_ids = []
    def gather_ids(c):
        all_crit_ids.append(c.id)
        for sc in c.sub_criteria:
            gather_ids(sc)

    top_criteria = Criterion.query.filter_by(
        standard_pack_id=pack_id,
        section_code=section_code,
        subsection_code=subsection_code,
        parent_id=None
    ).all()

    if not top_criteria:
        raise ValueError("No criteria found for this subsection")

    for c in top_criteria:
        gather_ids(c)

    now = datetime.utcnow()
    responses = Response.query.filter_by(application_id=application_id).all()
    resp_map = {r.criterion_id: r for r in responses}

    for cid in all_crit_ids:
        resp = resp_map.get(cid)
        if resp:
            resp.not_applicable = not_applicable_val
            resp.updated_at = now
        else:
            new_resp = Response(
                application_id=application_id,
                criterion_id=cid,
                narrative_text="",
                not_applicable=not_applicable_val,
                created_at=now,
                updated_at=now
            )
            db.session.add(new_resp)


@wizard_bp.route("/api/applications", methods=["POST"])
@login_required
def create_application():
    data = request.get_json(silent=True) or {}
    requested_pack_id = data.get("standard_pack_id")
    portfolio_name = data.get("name")
    onboarding_data = data.get("onboarding")

    try:
        if requested_pack_id:
            pack = StandardPack.query.get(requested_pack_id)
            if not pack:
                return jsonify({
                    "error": "StandardPack not found",
                    "details": f"No StandardPack exists with id '{requested_pack_id}'."
                }), 400
        else:
            all_packs = StandardPack.query.all()
            if len(all_packs) == 0:
                return jsonify({
                    "error": "No StandardPack available",
                    "details": "No accreditation standard has been loaded into the system yet."
                }), 400
            elif len(all_packs) > 1:
                return jsonify({
                    "error": "standard_pack_id is required",
                    "details": "Multiple StandardPacks exist; specify which one to use.",
                    "available_standard_packs": [
                        {"id": p.id, "accrediting_body": p.accrediting_body, "application_type": p.application_type}
                        for p in all_packs
                    ]
                }), 400
            pack = all_packs[0]

        portfolio = Portfolio.query.filter_by(owner_user_id=current_user.id).first()
        if not portfolio:
            portfolio = Portfolio(
                owner_user_id=current_user.id,
                name=portfolio_name or f"{current_user.email}'s Portfolio"
            )
            db.session.add(portfolio)
            db.session.flush()
            logger.info(f"Created new Portfolio {portfolio.id} for user {current_user.id}")

        if onboarding_data:
            if onboarding_data.get("institution_name"):
                portfolio.institution_name = onboarding_data.get("institution_name")
            if onboarding_data.get("founding_year") is not None:
                portfolio.founding_year = onboarding_data.get("founding_year")
            if onboarding_data.get("mission_statement"):
                portfolio.mission_statement = onboarding_data.get("mission_statement")

        new_application = Application(
            portfolio_id=portfolio.id,
            standard_pack_id=pack.id,
            status="draft"
        )
        db.session.add(new_application)
        db.session.commit()
        logger.info(f"Created new Application {new_application.id} for user {current_user.id} against StandardPack {pack.id}")

        if onboarding_data:
            distance_ed = onboarding_data.get("offers_distance_education")
            if distance_ed is False:
                for sec, sub in [("IX", "A"), ("IX", "B"), ("IX", "C"), ("IX", "D"), ("IX", "E"), ("IX", "F"), ("IX", "G"), ("IX", "H")]:
                    try:
                        _mark_subsection_not_applicable(new_application.id, sec, sub, True)
                        db.session.commit()
                    except Exception as sub_err:
                        db.session.rollback()
                        logger.error(f"Failed to mark subsection {sec}/{sub} N/A during onboarding: {str(sub_err)}", exc_info=True)

            offers_esl = onboarding_data.get("offers_esl")
            if offers_esl is False:
                try:
                    _mark_subsection_not_applicable(new_application.id, "V", "E", True)
                    db.session.commit()
                except Exception as sub_err:
                    db.session.rollback()
                    logger.error(f"Failed to mark subsection V/E N/A during onboarding: {str(sub_err)}", exc_info=True)

            offers_grad = onboarding_data.get("offers_graduate_degrees")
            if offers_grad is False:
                try:
                    _mark_subsection_not_applicable(new_application.id, "V", "D", True)
                    db.session.commit()
                except Exception as sub_err:
                    db.session.rollback()
                    logger.error(f"Failed to mark subsection V/D N/A during onboarding: {str(sub_err)}", exc_info=True)

        return jsonify({
            "id": new_application.id,
            "portfolio_id": new_application.portfolio_id,
            "standard_pack_id": new_application.standard_pack_id,
            "status": new_application.status,
            "due_date": new_application.due_date.isoformat() if new_application.due_date else None,
            "institution_name": new_application.portfolio.institution_name if new_application.portfolio else None,
            "founding_year": new_application.portfolio.founding_year if new_application.portfolio else None,
            "mission_statement": new_application.portfolio.mission_statement if new_application.portfolio else None,
            "created_at": new_application.created_at.isoformat(),
            "updated_at": new_application.updated_at.isoformat() if new_application.updated_at else None
        }), 201

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error creating application for user {current_user.id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Database error creating application", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error creating application for user {current_user.id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

@wizard_bp.route("/api/applications/<int:application_id>/due-date", methods=["PUT"])
@login_required
def update_application_due_date(application_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        data = request.get_json(silent=True) or {}
        date_val = data.get("due_date")

        parsed_date = None
        if date_val:
            try:
                parsed_date = datetime.strptime(date_val, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({
                    "error": "Bad Request",
                    "message": "Invalid date format, expected YYYY-MM-DD"
                }), 400

        app_record.due_date = parsed_date
        app_record.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info(f"Updated due_date for application {application_id} to {parsed_date}")

        return jsonify({
            "application_id": application_id,
            "due_date": app_record.due_date.isoformat() if app_record.due_date else None
        }), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error updating due date for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Database error updating due date", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error updating due date for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

@wizard_bp.route("/api/applications/<int:application_id>/subsections", methods=["GET"])
@login_required
def get_subsections(application_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        pack = StandardPack.query.get(app_record.standard_pack_id)
        if not pack:
            return jsonify({"error": "StandardPack not found for application"}), 404

        criteria = Criterion.query.filter_by(standard_pack_id=pack.id).all()

        subsections_map = {}
        for c in criteria:
            key = (c.section_code, c.section_name, c.subsection_code, c.subsection_name)
            if key not in subsections_map:
                subsections_map[key] = []
            subsections_map[key].append(c)

        responses = Response.query.filter_by(application_id=application_id).all()
        response_map = {r.criterion_id: r for r in responses}

        result = []
        for (sec_code, sec_name, sub_code, sub_name), crits in subsections_map.items():
            answered_count = 0
            all_not_applicable = True if len(crits) > 0 else False

            for c in crits:
                resp = response_map.get(c.id)
                is_na = resp and resp.not_applicable if resp else False
                if not is_na:
                    all_not_applicable = False

                has_narrative = resp and resp.narrative_text and resp.narrative_text.strip() != ""
                has_exhibit = False
                if c.answer_type == "exhibit" and resp:
                    has_exhibit = EvidenceDocument.query.filter_by(response_id=resp.id).count() > 0

                has_table_rows = False
                if c.answer_type == "table_rows" and resp:
                    has_table_rows = table_has_data(resp.id)

                if has_narrative or is_na or has_exhibit or has_table_rows:
                    answered_count += 1

            result.append({
                "section_code": sec_code,
                "section_name": sec_name,
                "subsection_code": sub_code,
                "subsection_name": sub_name,
                "criterion_count": len(crits),
                "answered_count": answered_count,
                "is_not_applicable": all_not_applicable if crits else False
            })

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error fetching subsections for application {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/subsections/<section_code>/<subsection_code>", methods=["GET"])
@login_required
def get_subsection_criteria(application_id, section_code, subsection_code):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        pack_id = app_record.standard_pack_id

        criteria = Criterion.query.filter_by(
            standard_pack_id=pack_id,
            section_code=section_code,
            subsection_code=subsection_code,
            parent_id=None
        ).order_by(Criterion.sort_order).all()

        responses = Response.query.filter_by(application_id=application_id).all()
        response_map = {r.criterion_id: r for r in responses}

        def serialize_criterion(c):
            resp = response_map.get(c.id)
            sub_crits = Criterion.query.filter_by(parent_id=c.id).order_by(Criterion.sort_order).all()
            return {
                "id": c.id,
                "section_code": c.section_code,
                "section_name": c.section_name,
                "subsection_code": c.subsection_code,
                "subsection_name": c.subsection_name,
                "item_number": c.item_number,
                "question_text": c.question_text,
                "answer_type": c.answer_type,
                "required": c.required,
                "table_columns": c.table_columns,
                "sort_order": c.sort_order,
                "response": {
                    "narrative_text": resp.narrative_text if resp else "",
                    "not_applicable": resp.not_applicable if resp else False,
                    "updated_at": resp.updated_at.isoformat() if resp and resp.updated_at else None
                },
                "sub_criteria": [serialize_criterion(sc) for sc in sub_crits]
            }

        serialized = [serialize_criterion(c) for c in criteria]

        subsection_not_applicable = False
        if criteria:
            all_na = True
            for c in criteria:
                resp = response_map.get(c.id)
                if not resp or not resp.not_applicable:
                    all_na = False
                    break
            subsection_not_applicable = all_na

        return jsonify({
            "section_code": section_code,
            "subsection_code": subsection_code,
            "is_not_applicable": subsection_not_applicable,
            "criteria": serialized
        }), 200

    except Exception as e:
        logger.error(f"Error fetching subsection criteria for app {application_id}, sec {section_code}, sub {subsection_code}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>", methods=["PUT"])
@login_required
def upsert_response(application_id, criterion_id):
    data = request.get_json() or {}
    narrative_text = data.get("narrative_text", "")

    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        criterion = Criterion.query.get(criterion_id)
        if not criterion:
            return jsonify({"error": "Criterion not found"}), 404

        edit_ok, edit_resp, edit_code = check_edit_permission(application_id, criterion.section_code, criterion.subsection_code)
        if not edit_ok:
            return edit_resp, edit_code

        existing_resp = Response.query.filter_by(
            application_id=application_id,
            criterion_id=criterion_id
        ).first()

        now = datetime.utcnow()

        if existing_resp:
            logger.info(f"Updating existing Response for app {application_id}, criterion {criterion_id}")
            existing_resp.narrative_text = narrative_text
            existing_resp.updated_at = now
            db.session.commit()
            action = "updated"
        else:
            logger.info(f"Creating new Response for app {application_id}, criterion {criterion_id}")
            new_resp = Response(
                application_id=application_id,
                criterion_id=criterion_id,
                narrative_text=narrative_text,
                not_applicable=False,
                created_at=now,
                updated_at=now
            )
            db.session.add(new_resp)
            db.session.commit()
            action = "created"

        return jsonify({
            "criterion_id": criterion_id,
            "action": action,
            "saved_at": now.isoformat()
        }), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error during response upsert (app {application_id}, criterion {criterion_id}): {str(e)}", exc_info=True)
        return jsonify({"error": "Database error during save", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during response upsert (app {application_id}, criterion {criterion_id}): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/table-rows", methods=["GET"])
@login_required
def get_table_rows(application_id, criterion_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        criterion = Criterion.query.get(criterion_id)
        if not criterion:
            return jsonify({"error": "Criterion not found"}), 404

        if criterion.answer_type != "table_rows":
            return jsonify({"error": "Criterion does not accept table rows"}), 400

        resp = Response.query.filter_by(
            application_id=application_id,
            criterion_id=criterion_id
        ).first()

        rows = []
        if resp:
            table_rows = TableRowData.query.filter_by(response_id=resp.id).order_by(TableRowData.row_index).all()
            rows = [{"id": r.id, "row_index": r.row_index, "row_data": r.row_data} for r in table_rows]

        return jsonify({
            "table_columns": criterion.table_columns or [],
            "rows": rows
        }), 200

    except Exception as e:
        logger.error(f"Error fetching table rows for app {application_id}, criterion {criterion_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "The table could not be loaded. Please refresh and try again.", "code": "internal_error"}), 500


@wizard_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/table-rows", methods=["PUT"])
@login_required
def save_table_rows(application_id, criterion_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return jsonify({"error": "Request body must be JSON with a 'rows' list.", "code": "invalid_payload"}), 400
    rows_data = data["rows"]
    if len(rows_data) > TABLE_MAX_ROWS:
        return jsonify({"error": f"A table can have at most {TABLE_MAX_ROWS} rows.", "code": "too_many_rows"}), 400

    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        criterion = Criterion.query.get(criterion_id)
        if not criterion:
            return jsonify({"error": "Criterion not found"}), 404

        edit_ok, edit_resp, edit_code = check_edit_permission(application_id, criterion.section_code, criterion.subsection_code)
        if not edit_ok:
            return edit_resp, edit_code

        if criterion.answer_type != "table_rows":
            return jsonify({"error": "This question does not accept table rows.", "code": "not_a_table_criterion"}), 400

        columns = criterion.table_columns or []
        if not columns:
            logger.error(f"Table criterion {criterion_id} has no table_columns configured; cannot save rows")
            return jsonify({"error": "This table has no columns configured. Please contact support.",
                            "code": "table_not_configured"}), 422

        clean_rows = []
        for i, row in enumerate(rows_data, start=1):
            if not isinstance(row, dict):
                return jsonify({"error": f"Row {i} is not valid.", "code": "invalid_row"}), 400
            unknown = set(row) - set(columns)
            if unknown:
                return jsonify({"error": f"Row {i} has unknown column(s): {', '.join(sorted(unknown))}.",
                                "code": "unknown_column"}), 400
            cleaned = {}
            for col in columns:
                value = row.get(col, "")
                if value is None:
                    value = ""
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = str(value)
                if not isinstance(value, str):
                    return jsonify({"error": f"Row {i}, column '{col}' must be text.", "code": "invalid_cell"}), 400
                if len(value) > TABLE_MAX_CELL_CHARS:
                    return jsonify({"error": f"Row {i}, column '{col}' is longer than {TABLE_MAX_CELL_CHARS} characters.",
                                    "code": "cell_too_long"}), 400
                cleaned[col] = value
            if table_row_has_data(cleaned):  # fully blank rows are not stored
                clean_rows.append(cleaned)

        resp = Response.query.filter_by(
            application_id=application_id,
            criterion_id=criterion_id
        ).first()

        now = datetime.utcnow()
        if not resp:
            resp = Response(
                application_id=application_id,
                criterion_id=criterion_id,
                narrative_text="",
                not_applicable=False,
                created_at=now,
                updated_at=now
            )
            db.session.add(resp)
            db.session.flush()

        TableRowData.query.filter_by(response_id=resp.id).delete()

        saved_rows = []
        for idx, row_dict in enumerate(clean_rows):
            new_row = TableRowData(
                response_id=resp.id,
                row_index=idx,
                row_data=row_dict
            )
            db.session.add(new_row)
            saved_rows.append({
                "row_index": idx,
                "row_data": row_dict
            })

        resp.updated_at = now
        db.session.commit()
        logger.info(f"Successfully updated table rows for app {application_id}, criterion {criterion_id}")

        return jsonify({
            "criterion_id": criterion_id,
            "rows": saved_rows,
            "complete": len(saved_rows) > 0,
            "saved_at": now.isoformat()
        }), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error during table rows update (app {application_id}, criterion {criterion_id}): {str(e)}", exc_info=True)
        return jsonify({"error": "The table could not be saved. Please try again.", "code": "database_error"}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during table rows update (app {application_id}, criterion {criterion_id}): {str(e)}", exc_info=True)
        return jsonify({"error": "The table could not be saved. Please try again.", "code": "internal_error"}), 500


@wizard_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/draft", methods=["POST"])
@login_required
def draft_narrative_response(application_id, criterion_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        criterion = Criterion.query.get(criterion_id)
        if not criterion:
            return jsonify({"error": "Criterion not found"}), 404

        edit_ok, edit_resp, edit_code = check_edit_permission(application_id, criterion.section_code, criterion.subsection_code)
        if not edit_ok:
            return edit_resp, edit_code

        if criterion.answer_type != "narrative":
            return jsonify({
                "error": "Invalid answer type",
                "details": f"AI drafting is only supported for narrative criteria. Criterion {criterion_id} has answer_type '{criterion.answer_type}'."
            }), 400

        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION")

        if not project_id or not location:
            logger.warning(f"AI drafting requested for app {application_id}, criterion {criterion_id}, but GOOGLE_CLOUD_PROJECT or GOOGLE_CLOUD_LOCATION is not configured.")
            return jsonify({
                "error": "AI drafting is not configured",
                "details": "Missing required environment variables GOOGLE_CLOUD_PROJECT or GOOGLE_CLOUD_LOCATION."
            }), 503

        # 1. Ground in onboarding data from portfolio
        portfolio = Portfolio.query.get(app_record.portfolio_id)
        known_facts_lines = []
        if portfolio:
            if portfolio.institution_name:
                known_facts_lines.append(f"Name: {portfolio.institution_name}")
            if portfolio.founding_year:
                known_facts_lines.append(f"Founded: {portfolio.founding_year}")
            if portfolio.mission_statement:
                known_facts_lines.append(f"Mission: {portfolio.mission_statement}")

        onboarding_context = ""
        if known_facts_lines:
            onboarding_context = "Known facts about this institution: " + ". ".join(known_facts_lines) + "."

        # 2. Ground in same-application answers (same section_code and subsection_code, non-empty narrative)
        same_sub_criteria = Criterion.query.filter_by(
            standard_pack_id=app_record.standard_pack_id,
            section_code=criterion.section_code,
            subsection_code=criterion.subsection_code
        ).all()

        other_crit_ids = [c.id for c in same_sub_criteria if c.id != criterion.id]
        other_responses = Response.query.filter(
            Response.application_id == application_id,
            Response.criterion_id.in_(other_crit_ids),
            Response.narrative_text != None,
            Response.narrative_text != ""
        ).limit(5).all()

        other_resp_map = {r.criterion_id: r.narrative_text for r in other_responses}
        context_excerpts = []
        for c in same_sub_criteria:
            if c.id in other_resp_map and len(context_excerpts) < 5:
                ans = other_resp_map[c.id].strip()
                if len(ans) > 150:
                    ans = ans[:147] + "..."
                context_excerpts.append(f"- [{c.question_text}]: {ans}")

        same_app_context = ""
        if context_excerpts:
            same_app_context = "For context, this school has already answered related questions in this section as follows:\n" + "\n".join(context_excerpts)

        # 3. Section-aware tone
        tone_instruction = SECTION_TONE_GUIDANCE.get(criterion.section_code, "Use clear, professional, policy-appropriate language.")

        # 4. Unknowable-fact detection & trigger phrases
        trigger_phrases = [
            "state specific", "state law", "state requirement", "licensure requirement",
            "legal requirement", "government requirement", "regulation", "statute",
            "code section", "citation"
        ]
        q_text_lower = criterion.question_text.lower()
        is_guidance_mode = any(phrase in q_text_lower for phrase in trigger_phrases)

        sub_criteria_text = ""
        sub_crits = Criterion.query.filter_by(parent_id=criterion.id).order_by(Criterion.sort_order).all()
        if sub_crits:
            sub_items_list = [f"- Item {sc.item_number}: {sc.question_text}" for sc in sub_crits]
            sub_criteria_text = "\nSub-criteria to address:\n" + "\n".join(sub_items_list)

        anti_fabrication = (
            "Never invent or guess at: dollar amounts, staff or faculty names, specific dates beyond what is explicitly "
            "provided above, percentages or statistics, specific policy or regulation citations, exact page/document references. "
            "If specific information is needed that isn't provided above, write generic placeholder language like '[specific amount]' "
            "rather than inventing a plausible-sounding number."
        )

        if is_guidance_mode:
            prompt = (
                f"You are an expert compliance consultant assisting an educational institution in drafting their ACCSC accreditation self-evaluation report.\n"
                f"This question asks for external regulatory, statutory, or state-specific legal facts that Accredo's internal data cannot supply.\n\n"
                f"Criterion Question: {criterion.question_text}\n"
                f"{sub_criteria_text}\n\n"
                f"{onboarding_context}\n"
                f"{same_app_context}\n\n"
                f"Tone guidance: {tone_instruction}\n\n"
                f"Instructions:\n"
                f"- Do not attempt to draft an answer. Instead, generate a short bulleted 'what to research and include' guidance list (3-5 bullets) specific to this question.\n"
                f"- Explicitly do NOT invent or guess at any actual regulation, citation, or legal requirement — only describe the SHAPE of information needed.\n"
                f"- {anti_fabrication}"
            )
        else:
            prompt = (
                f"You are an expert compliance consultant assisting an educational institution in drafting their ACCSC accreditation self-evaluation report.\n"
                f"Write a professional, thorough, and plausible starting narrative answer for the following accreditation criterion.\n\n"
                f"Criterion Question: {criterion.question_text}\n"
                f"{sub_criteria_text}\n\n"
                f"{onboarding_context}\n"
                f"{same_app_context}\n\n"
                f"Tone guidance: {tone_instruction}\n\n"
                f"Instructions:\n"
                f"- Write generic, editable placeholder-style professional language.\n"
                f"- {anti_fabrication}\n"
                f"- Provide structured paragraphs that the compliance team can review, refine, and populate with their actual institutional data."
            )

        try:
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=location,
                http_options=genai_types.HttpOptions(timeout=DRAFT_REQUEST_TIMEOUT_MS),
            )
            # Thinking is disabled so hidden reasoning tokens cannot consume the output budget and truncate drafts.
            response = client.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=DRAFT_MAX_OUTPUT_TOKENS,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )

            if not response or not response.candidates:
                block_reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
                logger.warning(f"Gemini returned no candidates (block_reason: {block_reason}) for app {application_id}, criterion {criterion_id}")
                return jsonify({
                    "error": "AI generation blocked",
                    "details": f"The model returned no draft (block reason: {block_reason}).",
                    "block_reason": str(block_reason)
                }), 422

            candidate = response.candidates[0]
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason != genai_types.FinishReason.STOP:
                safety_ratings = getattr(candidate, "safety_ratings", None)
                logger.warning(f"Gemini generation non-STOP finish_reason: {finish_reason}, safety_ratings: {safety_ratings} for app {application_id}, criterion {criterion_id}")
                return jsonify({
                    "error": "AI generation interrupted",
                    "details": f"Model generation stopped with finish reason: {finish_reason}",
                    "finish_reason": str(finish_reason)
                }), 422

            raw_text = (response.text or "").strip()
            if not raw_text:
                logger.error(f"Gemini returned STOP with empty text for app {application_id}, criterion {criterion_id}")
                return jsonify({
                    "error": "AI generation returned no text",
                    "details": "The model finished without producing a draft. Please try again."
                }), 502
            
            if is_guidance_mode:
                draft_text = "GUIDANCE (this requires information Accredo cannot supply — please research and replace this with your actual answer):\n\n" + raw_text
            else:
                draft_text = raw_text

            generated_at = datetime.utcnow().isoformat()

            logger.info(f"Successfully generated AI draft for app {application_id}, criterion {criterion_id} (Guidance mode: {is_guidance_mode})")
            return jsonify({
                "criterion_id": criterion_id,
                "draft_text": draft_text,
                "generated_at": generated_at
            }), 200

        except genai_errors.APIError as g_err:
            logger.error(f"Gemini API error (HTTP {g_err.code}) during drafting for app {application_id}, criterion {criterion_id}: {str(g_err)}", exc_info=True)
            return jsonify({
                "error": "Upstream AI service error",
                "details": str(g_err),
                "upstream_status": g_err.code
            }), 502
        except (TimeoutError, httpx.TimeoutException) as t_err:
            logger.error(f"Gemini request timed out for app {application_id}, criterion {criterion_id}: {str(t_err)}", exc_info=True)
            return jsonify({
                "error": "AI drafting request timed out",
                "details": str(t_err)
            }), 504
        except Exception as ai_err:
            logger.error(f"Unexpected error calling Gemini for app {application_id}, criterion {criterion_id}: {str(ai_err)}", exc_info=True)
            return jsonify({
                "error": "Failed to generate AI draft",
                "details": str(ai_err)
            }), 502

    except Exception as e:
        logger.error(f"Unexpected error in draft route for app {application_id}, criterion {criterion_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error during AI drafting", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:target_application_id>/clone-from/<int:source_application_id>", methods=["POST"])
@login_required
def clone_from_application(target_application_id, source_application_id):
    try:
        target_app, err_resp, err_code = verify_application_ownership(target_application_id)
        if err_resp:
            return err_resp, err_code

        source_app, err_resp, err_code = verify_application_ownership(source_application_id)
        if err_resp:
            return err_resp, err_code

        target_responses = Response.query.filter_by(application_id=target_application_id).all()
        if any(r.narrative_text and r.narrative_text.strip() for r in target_responses):
            return jsonify({
                "error": "Conflict",
                "message": "Target application already contains non-empty responses. Cloning is only permitted into an empty application."
            }), 409

        source_criteria = Criterion.query.filter_by(standard_pack_id=source_app.standard_pack_id).all()
        source_responses = Response.query.filter_by(application_id=source_application_id).all()
        source_resp_map = {r.criterion_id: r for r in source_responses if r.narrative_text and r.narrative_text.strip()}

        source_crits_by_sub = {}
        for sc in source_criteria:
            if sc.answer_type == "narrative" and sc.id in source_resp_map:
                key = (sc.section_code, sc.subsection_code)
                if key not in source_crits_by_sub:
                    source_crits_by_sub[key] = []
                source_crits_by_sub[key].append(sc)

        target_criteria = Criterion.query.filter_by(standard_pack_id=target_app.standard_pack_id).all()
        cloned_criteria = []
        now = datetime.utcnow()

        target_resp_map = {r.criterion_id: r for r in target_responses}

        for tc in target_criteria:
            if tc.answer_type != "narrative":
                continue

            key = (tc.section_code, tc.subsection_code)
            candidates = source_crits_by_sub.get(key, [])
            if not candidates:
                continue

            best_match = None
            best_ratio = 0.0

            for sc in candidates:
                ratio = difflib.SequenceMatcher(None, tc.question_text, sc.question_text).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = sc

            if best_match and best_ratio > 0.75:
                source_resp = source_resp_map[best_match.id]
                existing_target_resp = target_resp_map.get(tc.id)
                if existing_target_resp:
                    existing_target_resp.narrative_text = source_resp.narrative_text
                    existing_target_resp.updated_at = now
                else:
                    new_target_resp = Response(
                        application_id=target_application_id,
                        criterion_id=tc.id,
                        narrative_text=source_resp.narrative_text,
                        not_applicable=False,
                        created_at=now,
                        updated_at=now
                    )
                    db.session.add(new_target_resp)
                    target_resp_map[tc.id] = new_target_resp

                cloned_criteria.append({
                    "target_criterion_id": tc.id,
                    "source_criterion_id": best_match.id,
                    "similarity": round(best_ratio, 3)
                })

        db.session.commit()
        logger.info(f"Successfully cloned {len(cloned_criteria)} criteria from app {source_application_id} to app {target_application_id}")

        return jsonify({
            "cloned_criteria": cloned_criteria,
            "total_cloned": len(cloned_criteria),
            "total_target_criteria": len(target_criteria)
        }), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error during clone from app {source_application_id} to {target_application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Database error during clone", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during clone from app {source_application_id} to {target_application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error during clone", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/share", methods=["POST"])
@login_required
def share_application(application_id):
    try:
        app_record = Application.query.get(application_id)
        if not app_record:
            return jsonify({"error": "Application not found"}), 404
        if not app_record.portfolio or app_record.portfolio.owner_user_id != current_user.id:
            return jsonify({"error": "Forbidden", "message": "Only the application owner may share this application"}), 403

        data = request.get_json() or {}
        email = data.get("email")
        if not email:
            return jsonify({"error": "Email is required"}), 400

        user_to_share = Users.query.filter_by(email=email).first()
        if not user_to_share:
            return jsonify({"error": "User not found", "message": f"No registered user exists with email '{email}'."}), 404

        if user_to_share.id == current_user.id or (app_record.portfolio and app_record.portfolio.owner_user_id == user_to_share.id):
            return jsonify({"error": "Conflict", "message": "Cannot share an application with its owner."}), 409

        existing_member = ApplicationMember.query.filter_by(
            application_id=application_id, user_id=user_to_share.id
        ).first()
        if existing_member:
            return jsonify({"error": "Conflict", "message": "User is already a member of this application."}), 409

        new_member = ApplicationMember(
            application_id=application_id,
            user_id=user_to_share.id,
            role="editor"
        )
        db.session.add(new_member)
        db.session.commit()
        logger.info(f"User {user_to_share.id} ({email}) added as member to application {application_id}")

        return jsonify({
            "user_id": user_to_share.id,
            "email": user_to_share.email,
            "role": new_member.role
        }), 201

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error sharing application {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Database error sharing application", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error sharing application {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/members", methods=["GET"])
@login_required
def get_application_members(application_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        owner = app_record.portfolio.owner if app_record.portfolio else None
        owner_info = {
            "user_id": owner.id if owner else None,
            "email": owner.email if owner else None,
            "role": "owner"
        }

        members = ApplicationMember.query.filter_by(application_id=application_id).all()
        members_info = [{
            "user_id": m.user_id,
            "email": m.user.email if m.user else None,
            "role": m.role
        } for m in members]

        return jsonify([owner_info] + members_info), 200

    except Exception as e:
        logger.error(f"Error retrieving members for application {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/members/<int:user_id>", methods=["DELETE"])
@login_required
def remove_application_member(application_id, user_id):
    try:
        app_record = Application.query.get(application_id)
        if not app_record:
            return jsonify({"error": "Application not found"}), 404
        if not app_record.portfolio or app_record.portfolio.owner_user_id != current_user.id:
            return jsonify({"error": "Forbidden", "message": "Only the application owner may modify membership"}), 403

        if app_record.portfolio.owner_user_id == user_id:
            return jsonify({"error": "Bad Request", "message": "Cannot remove the application owner."}), 400

        member = ApplicationMember.query.filter_by(
            application_id=application_id, user_id=user_id
        ).first()
        if not member:
            return jsonify({"error": "Membership not found"}), 404

        db.session.delete(member)
        db.session.commit()
        logger.info(f"Removed user {user_id} from application {application_id}")

        return jsonify({"message": "Member removed successfully", "user_id": user_id}), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error removing member {user_id} from application {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Database error removing member", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error removing member {user_id} from application {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/assignments", methods=["PUT"])
@login_required
def upsert_assignment(application_id):
    try:
        app_record = Application.query.get(application_id)
        if not app_record:
            return jsonify({"error": "Application not found"}), 404
        if not app_record.portfolio or app_record.portfolio.owner_user_id != current_user.id:
            return jsonify({"error": "Forbidden", "message": "Only the application owner may manage assignments"}), 403

        data = request.get_json(silent=True) or {}
        section_code = data.get("section_code")
        subsection_code = data.get("subsection_code")
        assigned_user_id = data.get("assigned_user_id")

        if not section_code or assigned_user_id is None:
            return jsonify({"error": "Bad Request", "message": "section_code and assigned_user_id are required"}), 400

        owner_id = app_record.portfolio.owner_user_id
        is_owner = (assigned_user_id == owner_id)
        is_member = ApplicationMember.query.filter_by(
            application_id=application_id, user_id=assigned_user_id
        ).first() is not None

        if not is_owner and not is_member:
            return jsonify({"error": "Bad Request", "message": "Assigned user must be the application owner or an application member"}), 400

        assignment = SubsectionAssignment.query.filter_by(
            application_id=application_id,
            section_code=section_code,
            subsection_code=subsection_code
        ).first()

        now = datetime.utcnow()
        if assignment:
            assignment.assigned_user_id = assigned_user_id
            action = "updated"
        else:
            assignment = SubsectionAssignment(
                application_id=application_id,
                section_code=section_code,
                subsection_code=subsection_code,
                assigned_user_id=assigned_user_id
            )
            db.session.add(assignment)
            action = "created"

        db.session.commit()
        logger.info(f"Successfully {action} assignment for app {application_id}, sec {section_code}, sub {subsection_code} to user {assigned_user_id}")

        return jsonify({
            "id": assignment.id,
            "application_id": assignment.application_id,
            "section_code": assignment.section_code,
            "subsection_code": assignment.subsection_code,
            "assigned_user_id": assignment.assigned_user_id,
            "created_at": assignment.created_at.isoformat()
        }), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error during assignment upsert for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Database error saving assignment", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during assignment upsert for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/assignments", methods=["GET"])
@login_required
def get_assignments(application_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        assignments = db.session.query(SubsectionAssignment, Users).join(
            Users, SubsectionAssignment.assigned_user_id == Users.id
        ).filter(SubsectionAssignment.application_id == application_id).all()

        result = [{
            "id": asm.id,
            "section_code": asm.section_code,
            "subsection_code": asm.subsection_code,
            "assigned_user_id": asm.assigned_user_id,
            "email": usr.email,
            "created_at": asm.created_at.isoformat()
        } for asm, usr in assignments]

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error fetching assignments for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/assignments/<int:assignment_id>", methods=["DELETE"])
@login_required
def delete_assignment(application_id, assignment_id):
    try:
        app_record = Application.query.get(application_id)
        if not app_record:
            return jsonify({"error": "Application not found"}), 404
        if not app_record.portfolio or app_record.portfolio.owner_user_id != current_user.id:
            return jsonify({"error": "Forbidden", "message": "Only the application owner may delete assignments"}), 403

        assignment = SubsectionAssignment.query.filter_by(
            id=assignment_id, application_id=application_id
        ).first()
        if not assignment:
            return jsonify({"error": "Assignment not found"}), 404

        db.session.delete(assignment)
        db.session.commit()
        logger.info(f"Successfully deleted assignment ID {assignment_id} for app {application_id}")

        return jsonify({"message": "Assignment deleted successfully", "assignment_id": assignment_id}), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error deleting assignment {assignment_id} for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Database error deleting assignment", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error deleting assignment {assignment_id} for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/assignment-mode", methods=["PUT"])
@login_required
def update_assignment_mode(application_id):
    try:
        app_record = Application.query.get(application_id)
        if not app_record:
            return jsonify({"error": "Application not found"}), 404
        if not app_record.portfolio or app_record.portfolio.owner_user_id != current_user.id:
            return jsonify({"error": "Forbidden", "message": "Only the application owner may update assignment mode"}), 403

        data = request.get_json(silent=True) or {}
        if "enforce_assignments" not in data:
            return jsonify({"error": "Bad Request", "message": "enforce_assignments field is required"}), 400

        enforce_val = bool(data.get("enforce_assignments"))
        app_record.enforce_assignments = enforce_val
        app_record.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info(f"Updated enforce_assignments for application {application_id} to {enforce_val}")

        return jsonify({
            "application_id": application_id,
            "enforce_assignments": app_record.enforce_assignments
        }), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error updating assignment mode for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Database error updating assignment mode", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error updating assignment mode for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/subsections/<section_code>/<subsection_code>/not-applicable", methods=["PUT"])
@login_required
def set_subsection_not_applicable(application_id, section_code, subsection_code):
    data = request.get_json() or {}
    not_applicable_val = data.get("not_applicable", True)

    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        edit_ok, edit_resp, edit_code = check_edit_permission(application_id, section_code, subsection_code)
        if not edit_ok:
            return edit_resp, edit_code

        pack_id = app_record.standard_pack_id

        all_crit_ids = []
        def gather_ids(c):
            all_crit_ids.append(c.id)
            for sc in c.sub_criteria:
                gather_ids(sc)

        top_criteria = Criterion.query.filter_by(
            standard_pack_id=pack_id,
            section_code=section_code,
            subsection_code=subsection_code,
            parent_id=None
        ).all()

        if not top_criteria:
            return jsonify({"error": "No criteria found for this subsection"}), 404

        for c in top_criteria:
            gather_ids(c)

        now = datetime.utcnow()

        responses = Response.query.filter_by(application_id=application_id).all()
        resp_map = {r.criterion_id: r for r in responses}

        for cid in all_crit_ids:
            resp = resp_map.get(cid)
            if resp:
                resp.not_applicable = not_applicable_val
                resp.updated_at = now
            else:
                new_resp = Response(
                    application_id=application_id,
                    criterion_id=cid,
                    narrative_text="",
                    not_applicable=not_applicable_val,
                    created_at=now,
                    updated_at=now
                )
                db.session.add(new_resp)

        db.session.commit()
        logger.info(f"Successfully set not_applicable={not_applicable_val} for subsection {section_code}/{subsection_code}, app {application_id}")

        return jsonify({
            "section_code": section_code,
            "subsection_code": subsection_code,
            "not_applicable": not_applicable_val,
            "saved_at": now.isoformat()
        }), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error setting subsection not-applicable (app {application_id}, sec {section_code}, sub {subsection_code}): {str(e)}", exc_info=True)
        return jsonify({"error": "Database error", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error setting subsection not-applicable: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/completeness", methods=["GET"])
@login_required
def get_completeness(application_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        comp_data = calculate_completeness(application_id)
        return jsonify(comp_data), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error calculating completeness for app {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/report/generate", methods=["POST"])
@login_required
def generate_application_report(application_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        pdf_path = generate_report(application_id)
        comp_data = calculate_completeness(application_id)
        generated_at = datetime.utcnow().isoformat()

        return jsonify({
            "application_id": application_id,
            "report_path": pdf_path,
            "generated_at": generated_at,
            "completeness_percentage": comp_data["percentage"]
        }), 200

    except Exception as e:
        logger.error(f"Failed to generate report for application {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Report generation failed", "details": str(e)}), 500


@wizard_bp.route("/api/applications/<int:application_id>/report/download", methods=["GET"])
@login_required
def download_application_report(application_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        report_dir = get_report_output_dir()
        if not os.path.exists(report_dir):
            return jsonify({"error": "No reports generated yet. Please generate a report first."}), 404

        pattern = os.path.join(report_dir, f"application_{application_id}_report_*.pdf")
        report_files = glob.glob(pattern)

        if not report_files:
            return jsonify({"error": "No reports generated yet for this application. Please generate one first."}), 404

        latest_report = max(report_files, key=os.path.getmtime)
        absolute_path = os.path.abspath(latest_report)

        logger.info(f"Serving latest report for application {application_id} from {absolute_path}")
        return send_file(absolute_path, as_attachment=True, download_name=os.path.basename(absolute_path))

    except Exception as e:
        logger.error(f"Error downloading report for application {application_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error during report download", "details": str(e)}), 500
