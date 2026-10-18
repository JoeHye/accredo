import logging
import re
from flask import Blueprint, jsonify, request, render_template, current_app
from sqlalchemy.exc import SQLAlchemyError
from ser_app import db
from ser_app.content import score_answers, AnswerError
from ser_app.models import Lead
logger = logging.getLogger(__name__)
ser_bp = Blueprint("ser", __name__, template_folder="templates")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
FIELD_LIMITS = {"name": 200, "email": 255, "school_name": 255}
# Hidden form field; real users leave it empty, simple bots fill it in.
HONEYPOT_FIELD = "website"
def _content():
    return current_app.config["SER_CONTENT"]
def _draft_blocked():
    return _content()["status"] == "DRAFT" and not current_app.config["SER_ALLOW_DRAFT"]
def _clean(value, limit):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())  # collapses newlines/tabs, strips control whitespace
    if not value or len(value) > limit or any(ord(c) < 32 for c in value):
        return None
    return value
@ser_bp.route("/", methods=["GET"])
def diagnostic_page():
    if _draft_blocked():
        return render_template("unavailable.html"), 503
    content = _content()
    by_id = {q["id"]: q for q in content["questions"]}
    sections = [{"title": s["title"], "questions": [by_id[qid] for qid in s["question_ids"]]} for s in content["sections"]]
    return render_template("index.html", content=content, sections=sections, is_draft=content["status"] == "DRAFT")
@ser_bp.route("/api/submit", methods=["POST"])
def submit():
    if _draft_blocked():
        return jsonify({"error": "The diagnostic is not published yet."}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    content = _content()
    errors = {}
    fields = {}
    for field, limit in FIELD_LIMITS.items():
        cleaned = _clean(data.get(field), limit)
        if cleaned is None:
            errors[field] = f"Required, at most {limit} characters"
        else:
            fields[field] = cleaned
    if "email" in fields:
        fields["email"] = fields["email"].lower()
        if not EMAIL_RE.match(fields["email"]):
            errors["email"] = "Enter a valid email address"
    try:
        result = score_answers(content, data.get("answers"))
    except AnswerError as e:
        errors["answers"] = str(e)
        result = None
    if errors:
        return jsonify({"error": "Please correct the highlighted fields", "fields": errors}), 400
    if data.get(HONEYPOT_FIELD):
        # Looks like a bot: show a normal result but store nothing.
        logger.warning(f"Honeypot field filled; lead discarded (email domain: {fields['email'].split('@')[-1]})")
        return jsonify(result), 200
    try:
        lead = Lead(
            name=fields["name"], email=fields["email"], school_name=fields["school_name"],
            answers=data["answers"], score=result["score"], result_key=result["result_key"],
            content_version=result["content_version"],
        )
        db.session.add(lead)
        db.session.commit()
        logger.info(f"Stored SER diagnostic lead id={lead.id} result={result['result_key']} score={result['score']}")
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error storing SER lead: {str(e)}", exc_info=True)
        return jsonify({"error": "We couldn't save your results due to a server error. Please try again in a moment."}), 503
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error storing SER lead: {str(e)}", exc_info=True)
        return jsonify({"error": "We couldn't save your results due to a server error. Please try again in a moment."}), 500
    return jsonify(result), 200
