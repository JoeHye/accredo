"""SER PDF export, JSON column parity, table completion, and the AI-drafting button flag.

Run from the repo root:  python -m pytest tests/test_report_export.py
SQLite by default; set TEST_DATABASE_URL=postgresql+psycopg2://... to run the same tests on Postgres.
"""
import os
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from reportlab.platypus import KeepTogether, Paragraph, Table

from app import create_app, db
from app import report_generator
from app.models import (Application, Criterion, EvidenceDocument, Portfolio, Response, StandardPack, TableRowData,
                        Users)
from app.wizard_routes import is_table_response_complete

PW = "password123"
COLS = [{"key": "year", "label": "Year"}, {"key": "event", "label": "Event"}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    pg_url = os.environ.get("TEST_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", pg_url or f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("REPORT_OUTPUT_DIR", str(tmp_path / "reports"))
    for var in ("SECRET_KEY", "GCS_BUCKET_NAME", "K_SERVICE", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(var, raising=False)
    if pg_url:
        monkeypatch.setenv("SECRET_KEY", "test-only-secret-key")
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        if pg_url:
            db.drop_all()
            db.create_all()
        u = Users(email="owner@example.edu")
        u.set_password(PW)
        db.session.add(u)
        db.session.add(StandardPack(id="pk", accrediting_body="ACCSC", application_type="initial",
                                    version_effective_date=date(2026, 1, 1)))
        db.session.flush()
        p = Portfolio(owner_user_id=u.id, name="Proof School")
        db.session.add(p)
        db.session.flush()
        a = Application(portfolio_id=p.id, standard_pack_id="pk")
        db.session.add(a)
        common = dict(standard_pack_id="pk", section_code="0", section_name="S", subsection_code="A",
                      subsection_name="History")
        db.session.add(Criterion(id="nar", item_number="1", question_text="Describe.", answer_type="narrative",
                                 sort_order=1, **common))
        db.session.add(Criterion(id="exh", item_number="2", question_text="Upload catalog.", answer_type="exhibit",
                                 sort_order=2, **common))
        db.session.add(Criterion(id="tbl", item_number="3", question_text="History table.", answer_type="table_rows",
                                 table_columns=COLS, sort_order=3, **common))
        db.session.commit()
        app_id = a.id
    return app, app_id


def answer(app, app_id, narrative=True, exhibit=True, table_rows=({"year": "2019", "event": "Founded"},)):
    with app.app_context():
        for cid in ("nar", "exh", "tbl"):
            db.session.add(Response(application_id=app_id, criterion_id=cid,
                                    narrative_text="Our mission." if (cid == "nar" and narrative) else "",
                                    not_applicable=False))
        db.session.flush()
        resp = {r.criterion_id: r for r in Response.query.filter_by(application_id=app_id)}
        if exhibit:
            db.session.add(EvidenceDocument(response_id=resp["exh"].id, filename="catalog_2026.pdf",
                                            gcs_path="/nonexistent/catalog_2026.pdf", file_size_bytes=204800,
                                            uploaded_at=datetime(2026, 11, 1, 9, 30)))
        for i, row in enumerate(table_rows):
            db.session.add(TableRowData(response_id=resp["tbl"].id, row_index=i, row_data=row))
        db.session.commit()


def story_text(app, app_id, monkeypatch):
    """Build the report but capture the flowables instead of writing a PDF; return all text in order."""
    captured = []
    monkeypatch.setattr(report_generator.SimpleDocTemplate, "build", lambda self, story: captured.extend(story))
    with app.app_context():
        report_generator.generate_report(app_id)

    out = []

    def walk(f):
        if isinstance(f, KeepTogether):
            for x in f._content:
                walk(x)
        elif isinstance(f, Paragraph):
            out.append(f.getPlainText())
        elif isinstance(f, Table):
            for row in f._cellvalues:
                for cell in row:
                    walk(cell)
    for f in captured:
        walk(f)
    return out, captured


# ---------- report ----------

def test_incomplete_application_gets_draft_banner_and_no_legacy_placeholder(env, monkeypatch):
    app, app_id = env
    answer(app, app_id, narrative=False, exhibit=False, table_rows=({"year": "  ", "event": ""},))
    text, _ = story_text(app, app_id, monkeypatch)
    joined = "\n".join(text)
    assert report_generator.DRAFT_LABEL in text[:3]
    assert "0 of 3 criteria complete" in joined and "1 of them exhibits" in joined
    assert "NOT YET IMPLEMENTED" not in joined
    assert "[EXHIBIT REQUIRED — NO FILE UPLOADED]" in joined
    assert "[TABLE DATA — NOT YET ANSWERED]" in joined          # blank row is not an answer
    assert "[INCOMPLETE — NOT YET ANSWERED]" in joined


def test_complete_application_gets_final_banner_exhibit_index_and_table(env, monkeypatch):
    app, app_id = env
    answer(app, app_id)
    text, _ = story_text(app, app_id, monkeypatch)
    assert report_generator.READY_LABEL in text[:3]
    assert "All 3 criteria pass validation." in text
    i = text.index("Exhibit")
    assert text[i:i + 8] == ["Exhibit", "File", "Uploaded (UTC)", "Size",
                             f"E-{1}", "catalog_2026.pdf", "2026-11-01 09:30", "200 KB"]
    j = text.index("Year")
    assert text[j:j + 4] == ["Year", "Event", "2019", "Founded"]   # column labels, values by key


def test_criteria_are_kept_together_and_tables_repeat_their_header(env, monkeypatch):
    app, app_id = env
    answer(app, app_id)
    _, story = story_text(app, app_id, monkeypatch)
    blocks = [f for f in story if isinstance(f, KeepTogether)]
    assert len(blocks) == 3                                          # one per criterion
    tables = [x for b in blocks for x in b._content if isinstance(x, Table)]
    assert tables and all(t.repeatRows == 1 for t in tables)


def test_real_pdf_is_written(env):
    app, app_id = env
    answer(app, app_id)
    with app.app_context():
        path = report_generator.generate_report(app_id)
    assert os.path.isabs(path) and os.path.getsize(path) > 1000
    with open(path, "rb") as f:
        assert f.read(5) == b"%PDF-"


# ---------- JSON column parity (SQLite and Postgres) ----------

@pytest.mark.parametrize("value", [None, [], ["Year", "Event"], COLS])
def test_table_columns_round_trip(env, value):
    app, _ = env
    with app.app_context():
        c = db.session.get(Criterion, "tbl")
        c.table_columns = value
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(Criterion, "tbl").table_columns == value
        is_sql_null = db.session.execute(db.text("SELECT table_columns IS NULL FROM criteria WHERE id = 'tbl'")).scalar()
        assert bool(is_sql_null) is (value is None)                     # None is SQL NULL, not JSON 'null'


@pytest.mark.parametrize("row", [{}, {"year": ""}, {"year": "2019", "event": "Founded"}, {"year": "O'Brien \"x\" ü"}])
def test_row_data_round_trip(env, row):
    app, app_id = env
    answer(app, app_id, table_rows=())
    with app.app_context():
        resp = Response.query.filter_by(application_id=app_id, criterion_id="tbl").one()
        db.session.add(TableRowData(response_id=resp.id, row_index=0, row_data=row))
        db.session.commit()
        db.session.expire_all()
        assert TableRowData.query.filter_by(response_id=resp.id).one().row_data == row


def test_row_data_none_is_rejected_not_stored_as_json_null(env):
    from sqlalchemy.exc import IntegrityError
    app, app_id = env
    answer(app, app_id, table_rows=())
    with app.app_context():
        resp = Response.query.filter_by(application_id=app_id, criterion_id="tbl").one()
        db.session.add(TableRowData(response_id=resp.id, row_index=0, row_data=None))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


# ---------- table completion ----------

@pytest.mark.parametrize("rows,expected", [
    (None, False),
    ([], False),                                                  # no rows at all
    ([{}], False),                                                # structural row only
    ([{"Action": "Delete"}], False),                              # only the editor's Action column
    ([{"year": "   ", "event": "\t"}], False),                    # blank cells
    ([{"year": "2019"}], True),
    ([{}, {"event": "Founded"}], True),
])
def test_is_table_response_complete(rows, expected):
    assert is_table_response_complete(SimpleNamespace(table_columns=COLS), rows) is expected


# ---------- AI drafting button flag ----------

@pytest.mark.parametrize("project,location,flag", [(None, None, "false"), ("proj", None, "false"),
                                                   ("proj", "us-central1", "true")])
def test_wizard_page_sets_ai_drafting_flag(env, monkeypatch, project, location, flag):
    app, _ = env
    for var, val in (("GOOGLE_CLOUD_PROJECT", project), ("GOOGLE_CLOUD_LOCATION", location)):
        if val:
            monkeypatch.setenv(var, val)
        else:
            monkeypatch.delenv(var, raising=False)
    c = app.test_client()
    assert c.post("/api/auth/login", json={"email": "owner@example.edu", "password": PW}).status_code == 200
    page = c.get("/wizard").get_data(as_text=True)
    assert f"window.AI_DRAFTING_AVAILABLE = {flag};" in page
