import os
import logging
from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError
db = SQLAlchemy()
# Public, unauthenticated form: keep request bodies small.
MAX_REQUEST_BYTES = 16 * 1024
def create_app(content_path=None):
    app = Flask(__name__)

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    from ser_app.content import load_content, ContentError
    try:
        content = load_content(content_path)
    except ContentError as e:
        logger.error(f"SER diagnostic content failed to load: {e}")
        raise RuntimeError(f"SER diagnostic content is invalid: {e}") from e
    app.config["SER_CONTENT"] = content
    app.config["SER_ALLOW_DRAFT"] = os.environ.get("SER_ALLOW_DRAFT") == "1"
    if content["status"] == "DRAFT":
        if app.config["SER_ALLOW_DRAFT"]:
            logger.warning("Serving DRAFT content (SER_ALLOW_DRAFT=1). Do not expose this deployment publicly.")
        else:
            logger.warning("Content is DRAFT and SER_ALLOW_DRAFT is not set: the diagnostic will answer 503 until content is APPROVED.")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        # On Cloud Run (K_SERVICE is set) a SQLite fallback would store leads on the
        # instance's ephemeral filesystem and lose them. Refuse instead.
        if os.environ.get("K_SERVICE"):
            logger.error("DATABASE_URL is not set on Cloud Run. Refusing to start rather than store leads on ephemeral disk.")
            raise RuntimeError("DATABASE_URL must be set when running on Cloud Run.")
        base_dir = os.path.abspath(os.path.dirname(__file__))
        db_path = os.path.join(base_dir, "..", "instance", "ser_local.db")
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        except OSError as e:
            logger.error(f"Cannot create local SQLite directory {os.path.dirname(db_path)}: {e}. Set DATABASE_URL.")
            raise RuntimeError(f"No DATABASE_URL and local SQLite directory is not writable: {e}") from e
        db_url = f"sqlite:///{db_path}"
        logger.info(f"No DATABASE_URL found. Falling back to local SQLite at absolute path: {db_path}")
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    if not db_url.startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
    db.init_app(app)
    with app.app_context():
        from ser_app.models import Lead
        try:
            db.create_all()
            logger.info("Database tables initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize database tables: {str(e)}", exc_info=True)
            raise
    from ser_app.routes import ser_bp
    app.register_blueprint(ser_bp)
    @app.errorhandler(413)
    def too_large(e):
        return jsonify({"error": "Request too large"}), 413
    @app.route("/health", methods=["GET"])
    def health_check():
        try:
            db.session.execute(db.text("SELECT 1"))
            return jsonify({"status": "healthy", "database": "connected",
                            "content_status": content["status"], "content_version": content["version"]}), 200
        except SQLAlchemyError as e:
            # Full error goes to server logs only; the response must not expose connection details.
            logger.error(f"Health check failed - Database unreachable: {str(e)}", exc_info=True)
            return jsonify({"status": "unhealthy", "error": "Database connection failed"}), 503
        except Exception as e:
            logger.error(f"Health check failed with unexpected error: {str(e)}", exc_info=True)
            return jsonify({"status": "unhealthy", "error": "An unexpected error occurred during health check"}), 503
    return app
