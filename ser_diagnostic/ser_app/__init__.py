import logging
import os
import re
import uuid
from datetime import datetime

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError

from ser_app.scoring import DEFAULT_CONTENT_PATH, load_content, score

db = SQLAlchemy()
logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FIELD_LIMITS = {"name": 200, "email": 254, "school": 255}


class SerLead(db.Model):
    __tablename__ = "ser_leads"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(254), nullable=False, index=True)
    school = db.Column(db.String(255), nullable=False)
    consent = db.Column(db.Boolean, nullable=False)
    answers = db.Column(db.JSON, nullable=False)
    points = db.Column(db.Integer, nullable=False)
    max_points = db.Column(db.Integer, nullable=False)
    percent = db.Column(db.Float, nullable=False)
    band = db.Column(db.String(20), nullable=False)
    gated = db.Column(db.Boolean, nullable=False)
    content_version = db.Column(db.String(20), nullable=True)
    pilot_requested = db.Column(db.Boolean, nullable=False, default=False)
    pilot_requested_at = db.Column(db.DateTime, nullable=True)


def create_app(content_path=None):
    app = Flask(__name__)
    logging.basicConfig(level=logging.INFO)

    content = load_content(content_path or os.environ.get("SER_CONTENT_PATH") or DEFAULT_CONTENT_PATH)
    app.config["SER_CONTENT"] = content

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        if os.environ.get("K_SERVICE"):
            # On Cloud Run a missing DATABASE_URL would silently write leads to an ephemeral file. Refuse instead.
            logger.error("DATABASE_URL is not set on Cloud Run. Refusing to start.")
            raise RuntimeError("DATABASE_URL must be set when running on Cloud Run.")
        db_url = "sqlite:///" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "instance", "ser_local.db")
        os.makedirs(os.path.dirname(db_url[len("sqlite:///"):]), exist_ok=True)
        logger.warning("DATABASE_URL not set; using local SQLite for development.")
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    if not db_url.startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}

    db.init_app(app)
    with app.app_context():
        try:
            db.create_all()
        except Exception as e:
            logger.error(f"Failed to initialize SER database tables: {e}", exc_info=True)
            raise

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.route("/", methods=["GET"])
    def form():
        return render_template("form.html", c=content, values={}, errors={})

    @app.route("/submit", methods=["POST"])
    def submit():
        values = request.form.to_dict()
        if values.get("website"):
            # Honeypot field, hidden from people; only bots fill it in.
            logger.warning("SER submission rejected by honeypot")
            return render_template("error.html", c=content, message="Your submission could not be processed."), 400

        errors = {}
        for field, limit in FIELD_LIMITS.items():
            v = values.get(field, "").strip()
            if not v:
                errors[field] = "Required."
            elif len(v) > limit:
                errors[field] = f"Must be {limit} characters or fewer."
        email = values.get("email", "").strip().lower()
        if "email" not in errors and not EMAIL_RE.match(email):
            errors["email"] = "Enter a valid email address."
        if values.get("consent") != "yes":
            errors["consent"] = "Please confirm so we can follow up on your results."

        answers = {q["id"]: values.get(q["id"]) for q in content["questions"]}
        valid_answers = {a["key"] for a in content["answers"]}
        for qid, a in answers.items():
            if a not in valid_answers:
                errors[qid] = "Please choose an answer."

        if errors:
            return render_template("form.html", c=content, values=values, errors=errors), 400

        result = score(content, answers)
        lead = SerLead(
            name=values["name"].strip(), email=email, school=values["school"].strip(), consent=True,
            answers=answers, points=result["points"], max_points=result["max"], percent=result["percent"],
            band=result["band"]["key"], gated=result["gated"], content_version=content.get("version"),
        )
        try:
            db.session.add(lead)
            db.session.commit()
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Failed to save SER lead: {e}", exc_info=True)
            return render_template("error.html", c=content,
                                   message="We couldn't save your results just now. Please try again in a few minutes."), 500
        logger.info(f"SER lead saved: id={lead.id} band={lead.band} percent={lead.percent}")
        return redirect(url_for("result", lead_id=lead.id), code=303)

    def _get_lead_or_404(lead_id):
        try:
            uuid.UUID(lead_id)
        except ValueError:
            abort(404)
        try:
            lead = db.session.get(SerLead, lead_id)
        except SQLAlchemyError as e:
            logger.error(f"Failed to load SER lead {lead_id}: {e}", exc_info=True)
            abort(503)
        if not lead:
            abort(404)
        return lead

    @app.route("/result/<lead_id>", methods=["GET"])
    def result(lead_id):
        lead = _get_lead_or_404(lead_id)
        try:
            outcome = score(content, lead.answers)
        except ValueError:
            # Questions changed since this lead was saved: show the stored score without per-question gaps.
            logger.warning(f"SER lead {lead.id} answers no longer match content; showing stored result only")
            bands = {b["key"]: b for b in content["bands"]}
            outcome = {"points": lead.points, "max": lead.max_points, "percent": lead.percent,
                       "band": bands.get(lead.band, content["bands"][-1]), "gated": lead.gated, "gaps": []}
        return render_template("result.html", c=content, lead=lead, r=outcome,
                               answer_labels={a["key"]: a["label"] for a in content["answers"]})

    @app.route("/result/<lead_id>/pilot", methods=["POST"])
    def request_pilot(lead_id):
        lead = _get_lead_or_404(lead_id)
        if not lead.pilot_requested:
            try:
                lead.pilot_requested = True
                lead.pilot_requested_at = datetime.utcnow()
                db.session.commit()
            except SQLAlchemyError as e:
                db.session.rollback()
                logger.error(f"Failed to record pilot request for SER lead {lead.id}: {e}", exc_info=True)
                return render_template("error.html", c=content,
                                       message="We couldn't record your request just now. Please try again in a few minutes."), 500
            logger.info(f"SER pilot requested: id={lead.id}")
        return redirect(url_for("result", lead_id=lead.id), code=303)

    @app.route("/health", methods=["GET"])
    def health():
        try:
            db.session.execute(db.text("SELECT 1"))
            return jsonify({"status": "healthy"}), 200
        except Exception as e:
            logger.error(f"SER health check failed: {e}", exc_info=True)
            return jsonify({"status": "unhealthy"}), 503

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", c=content, message="That page doesn't exist."), 404

    @app.errorhandler(503)
    def unavailable(_e):
        return render_template("error.html", c=content, message="The diagnostic is temporarily unavailable. Please try again shortly."), 503

    return app
