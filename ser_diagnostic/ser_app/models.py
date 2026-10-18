# ser_app/models.py
# Lead storage. Agreed fields only: name, email, school name, answers, score, timestamp.
# content_version and result_key are not personal data; they record which wording and
# scoring produced the stored score, so results stay explainable after content edits.
from datetime import datetime
from ser_app import db
from sqlalchemy.dialects.sqlite import JSON as SQLiteJSON
from sqlalchemy.dialects.postgresql import JSONB

JSONType = JSONB().with_variant(SQLiteJSON(), "sqlite")


class Lead(db.Model):
    __tablename__ = "ser_leads"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(255), nullable=False, index=True)
    school_name = db.Column(db.String(255), nullable=False)
    answers = db.Column(JSONType, nullable=False)
    score = db.Column(db.Integer, nullable=False)
    result_key = db.Column(db.String(50), nullable=False)
    content_version = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
