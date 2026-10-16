import os
import logging
from flask import Flask, jsonify, redirect, url_for, request
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from sqlalchemy.exc import SQLAlchemyError
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
def create_app():
    app = Flask(__name__)

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        base_dir = os.path.abspath(os.path.dirname(__file__))
        db_path = os.path.join(base_dir, "..", "instance", "accredo_local.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        db_url = f"sqlite:///{db_path}"
        logger.info(f"No DATABASE_URL found. Falling back to local SQLite at absolute path: {db_path}")
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    if not db_url.startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if db_url.startswith("sqlite"):
            secret_key = "dev-secret-key-local-only"
            logger.warning("SECRET_KEY not set; using local-only dev key (SQLite mode).")
        else:
            logger.error("SECRET_KEY is not set but DATABASE_URL points at a non-SQLite database. Refusing to start.")
            raise RuntimeError("SECRET_KEY must be set when DATABASE_URL points at a non-SQLite database.")
    app.config["SECRET_KEY"] = secret_key
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login_page"
    @login_manager.user_loader
    def load_user(user_id):
        from app.models import Users
        try:
            return Users.query.get(int(user_id))
        except Exception as e:
            logger.error(f"Error loading user {user_id}: {str(e)}", exc_info=True)
            return None
    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized", "message": "Authentication required"}), 401
        return redirect(url_for("auth.login_page"))
    with app.app_context():
        from app.models import (
            Users, StandardPack, Criterion, Portfolio,
            Application, Response, EvidenceDocument, StatusHistory
        )
        try:
            db.create_all()
            logger.info("Database tables initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize database tables: {str(e)}", exc_info=True)
            raise
    from app.wizard_routes import wizard_bp
    from app.exhibit_routes import exhibit_bp
    from app.auth_routes import auth_bp

    app.register_blueprint(wizard_bp)
    app.register_blueprint(exhibit_bp)
    app.register_blueprint(auth_bp)
    @app.route("/health", methods=["GET"])
    def health_check():
        try:
            db.session.execute(db.text("SELECT 1"))
            return jsonify({"status": "healthy", "database": "connected"}), 200
        except SQLAlchemyError as e:
            # Full error goes to server logs only; the response must not expose connection details.
            logger.error(f"Health check failed - Database unreachable: {str(e)}", exc_info=True)
            return jsonify({
                "status": "unhealthy",
                "error": "Database connection failed"
            }), 503
        except Exception as e:
            logger.error(f"Health check failed with unexpected error: {str(e)}", exc_info=True)
            return jsonify({
                "status": "unhealthy",
                "error": "An unexpected error occurred during health check"
            }), 503
    return app
