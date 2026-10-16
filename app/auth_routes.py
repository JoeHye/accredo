import logging
from flask import Blueprint, jsonify, request, render_template, redirect, url_for
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import SQLAlchemyError
from app import db
from app.models import Users
logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__, template_folder="templates")
@auth_bp.route("/login", methods=["GET"])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("wizard.wizard_page"))
    return render_template("login.html")
@auth_bp.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters long"}), 400
    try:
        existing_user = Users.query.filter_by(email=email).first()
        if existing_user:
            return jsonify({"error": "Email is already registered"}), 409
        new_user = Users(email=email)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        logger.info(f"Successfully registered new user: {email}")
        return jsonify({"message": "Registration successful. Please log in."}), 201
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error during user registration for {email}: {str(e)}", exc_info=True)
        return jsonify({"error": "Registration failed due to a server error. Please try again later."}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during user registration for {email}: {str(e)}", exc_info=True)
        return jsonify({"error": "Registration failed due to a server error. Please try again later."}), 500
@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "Invalid email or password"}), 401
    try:
        user = Users.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            logger.warning(f"Failed login attempt for email: {email}")
            return jsonify({"error": "Invalid email or password"}), 401
        login_user(user)
        logger.info(f"User logged in successfully: {email}")
        return jsonify({"message": "Logged in successfully", "user": {"id": user.id, "email": user.email}}), 200
    except Exception as e:
        logger.error(f"Error during login for {email}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error during login"}), 500
@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    if not current_user.is_authenticated:
        return jsonify({"error": "Bad Request", "message": "Not logged in"}), 400
    try:
        email = current_user.email
        logout_user()
        logger.info(f"User logged out successfully: {email}")
        return jsonify({"message": "Logged out successfully"}), 200
    except Exception as e:
        logger.error(f"Error during logout: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error during logout"}), 500
@auth_bp.route("/api/auth/me", methods=["GET"])
@login_required
def me():
    return jsonify({
        "id": current_user.id,
        "email": current_user.email
    }), 200