# app/models.py
from datetime import datetime, date
from app import db
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.dialects.sqlite import JSON as SQLiteJSON
from sqlalchemy.dialects.postgresql import JSONB

JSONType = JSONB().with_variant(SQLiteJSON(), "sqlite")

class Users(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    portfolios = db.relationship("Portfolio", backref="owner", lazy=True)
    application_memberships = db.relationship("ApplicationMember", backref="user", cascade="all, delete-orphan", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

class StandardPack(db.Model):
    __tablename__ = "standard_packs"

    id = db.Column(db.String(100), primary_key=True)
    accrediting_body = db.Column(db.String(100), nullable=False)
    application_type = db.Column(db.String(50), nullable=False)
    version_effective_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    criteria = db.relationship("Criterion", backref="standard_pack", cascade="all, delete-orphan", lazy=True)
    applications = db.relationship("Application", backref="standard_pack", lazy=True)

class Criterion(db.Model):
    __tablename__ = "criteria"

    id = db.Column(db.String(100), primary_key=True)
    standard_pack_id = db.Column(db.String(100), db.ForeignKey("standard_packs.id"), nullable=False)
    parent_id = db.Column(db.String(100), db.ForeignKey("criteria.id"), nullable=True)
    section_code = db.Column(db.String(50), nullable=False)
    section_name = db.Column(db.String(255), nullable=False)
    subsection_code = db.Column(db.String(50), nullable=True)
    subsection_name = db.Column(db.String(255), nullable=True)
    item_number = db.Column(db.String(50), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    answer_type = db.Column(db.String(50), nullable=False)
    required = db.Column(db.Boolean, default=True, nullable=False)
    table_columns = db.Column(JSONType, nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    sub_criteria = db.relationship("Criterion", backref=db.backref("parent", remote_side=[id]), cascade="all, delete-orphan")
    responses = db.relationship("Response", backref="criterion", cascade="all, delete-orphan", lazy=True)

class Portfolio(db.Model):
    __tablename__ = "portfolios"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    applications = db.relationship("Application", backref="portfolio", cascade="all, delete-orphan", lazy=True)

class Application(db.Model):
    __tablename__ = "applications"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    portfolio_id = db.Column(db.Integer, db.ForeignKey("portfolios.id"), nullable=False)
    standard_pack_id = db.Column(db.String(100), db.ForeignKey("standard_packs.id"), nullable=False)
    status = db.Column(db.String(50), nullable=False, default="draft")
    due_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    responses = db.relationship("Response", backref="application", cascade="all, delete-orphan", lazy=True)
    status_history = db.relationship("StatusHistory", backref="application", cascade="all, delete-orphan", lazy=True)
    members = db.relationship("ApplicationMember", backref="application", cascade="all, delete-orphan", lazy=True)

class ApplicationMember(db.Model):
    __tablename__ = "application_members"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    application_id = db.Column(db.Integer, db.ForeignKey("applications.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    role = db.Column(db.String(50), nullable=False, default="editor")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("application_id", "user_id", name="uq_application_member"),
    )

class Response(db.Model):
    __tablename__ = "responses"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    application_id = db.Column(db.Integer, db.ForeignKey("applications.id"), nullable=False)
    criterion_id = db.Column(db.String(100), db.ForeignKey("criteria.id"), nullable=False)
    narrative_text = db.Column(db.Text, nullable=True)
    not_applicable = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("application_id", "criterion_id", name="uq_application_criterion"),
    )

    evidence_documents = db.relationship("EvidenceDocument", backref="response", cascade="all, delete-orphan", lazy=True)
    table_rows = db.relationship("TableRowData", backref="response", cascade="all, delete-orphan", lazy=True)

class TableRowData(db.Model):
    __tablename__ = "table_row_data"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    response_id = db.Column(db.Integer, db.ForeignKey("responses.id"), nullable=False)
    row_index = db.Column(db.Integer, nullable=False, default=0)
    row_data = db.Column(JSONType, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class EvidenceDocument(db.Model):
    __tablename__ = "evidence_documents"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    response_id = db.Column(db.Integer, db.ForeignKey("responses.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    gcs_path = db.Column(db.String(1024), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    file_size_bytes = db.Column(db.Integer, nullable=False)

class StatusHistory(db.Model):
    __tablename__ = "status_histories"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    application_id = db.Column(db.Integer, db.ForeignKey("applications.id"), nullable=False)
    previous_status = db.Column(db.String(50), nullable=True)
    new_status = db.Column(db.String(50), nullable=False)
    changed_at = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.Text, nullable=True)