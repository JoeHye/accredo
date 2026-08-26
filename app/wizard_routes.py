import os
import glob
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, render_template, send_file
from flask_login import login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
from app import db
from app.models import Application, StandardPack, Criterion, Response, EvidenceDocument, Portfolio
from app.report_generator import generate_report

logger = logging.getLogger(__name__)

wizard_bp = Blueprint("wizard", __name__, template_folder="templates", static_folder="static")

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
        if not app_record.portfolio or app_record.portfolio.owner_user_id != current_user.id:
            return None, jsonify({"error": "Forbidden", "message": "You do not have access to this application"}), 403
        return app_record, None, None
    except Exception as e:
        logger.error(f"Error verifying application ownership for app {application_id}: {str(e)}", exc_info=True)
        return None, jsonify({"error": "Internal server error"}), 500

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

        is_complete = has_narrative or is_sub_na or criterion_na or has_exhibit

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
        if not portfolio_ids:
            return jsonify([]), 200

        apps = Application.query.filter(Application.portfolio_id.in_(portfolio_ids)).all()
        result = [{
            "id": app.id,
            "portfolio_id": app.portfolio_id,
            "standard_pack_id": app.standard_pack_id,
            "status": app.status,
            "created_at": app.created_at.isoformat(),
            "updated_at": app.updated_at.isoformat() if app.updated_at else None
        } for app in apps]
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Error fetching user applications for user {current_user.id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@wizard_bp.route("/api/applications", methods=["POST"])
@login_required
def create_application():
    data = request.get_json(silent=True) or {}
    requested_pack_id = data.get("standard_pack_id")
    portfolio_name = data.get("name")

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

        new_application = Application(
            portfolio_id=portfolio.id,
            standard_pack_id=pack.id,
            status="draft"
        )
        db.session.add(new_application)
        db.session.commit()
        logger.info(f"Created new Application {new_application.id} for user {current_user.id} against StandardPack {pack.id}")

        return jsonify({
            "id": new_application.id,
            "portfolio_id": new_application.portfolio_id,
            "standard_pack_id": new_application.standard_pack_id,
            "status": new_application.status,
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

                if has_narrative or is_na or has_exhibit:
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


@wizard_bp.route("/api/applications/<int:application_id>/subsections/<section_code>/<subsection_code>/not-applicable", methods=["PUT"])
@login_required
def set_subsection_not_applicable(application_id, section_code, subsection_code):
    data = request.get_json() or {}
    not_applicable_val = data.get("not_applicable", True)

    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

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
