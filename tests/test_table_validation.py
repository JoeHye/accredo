"""Table answers (table_rows): validation engine, completion counting, save payloads, and the 25 MB guard.

Run from the repo root:  python -m pytest tests/test_table_validation.py
SQLite by default; set TEST_DATABASE_URL=postgresql+psycopg2://... to run the same tests on Postgres.
"""
import io
import json
import os
from datetime import date
from types import SimpleNamespace

import pytest

from app import create_app, db
from app.models import (Application, ApplicationMember, Criterion, Portfolio, Response, StandardPack,
                        TableRowData, Users)
from app.wizard_routes import is_table_response_complete, normalize_table_columns, table_row_has_data

PW = "password123"
HISTORY_COLS = [{"key": "year", "label": "Year"}, {"key": "event", "label": "Event"},
                {"key": "description", "label": "Description"}]
REQUIRED_COLS = [{"key": "year", "label": "Year", "required": True},
                 {"key": "event", "label": "Event", "required": True},
                 {"key": "notes", "label": "Notes"}]
STRING_COLS = ["Approval Date (Month/Year)", "Substantive Change"]


def crit(columns):
    return SimpleNamespace(table_columns=columns)


# ---------- normalize_table_columns ----------

def test_normalize_accepts_strings_objects_and_json_text():
    assert normalize_table_columns(STRING_COLS) == [
        {"key": c, "label": c, "required": False} for c in STRING_COLS]
    assert normalize_table_columns(HISTORY_COLS)[0] == {"key": "year", "label": "Year", "required": False}
    assert normalize_table_columns([{"key": "k"}]) == [{"key": "k", "label": "k", "required": False}]
    assert normalize_table_columns([{"label": "Only label"}])[0]["key"] == "Only label"
    assert normalize_table_columns(json.dumps(HISTORY_COLS)) == normalize_table_columns(HISTORY_COLS)
    assert normalize_table_columns(REQUIRED_COLS)[0]["required"] is True


@pytest.mark.parametrize("raw", [None, [], "not json", "{}", {"key": "x"}, 5, [5], [""], ["  "],
                                 [{"label": ""}], ["a", "a"], [{"key": "a"}, {"key": "a"}],
                                 [{"key": "a", "required": "yes"}, None]])
def test_normalize_rejects_malformed(raw):
    assert normalize_table_columns(raw) == []


# ---------- is_table_response_complete ----------

@pytest.mark.parametrize("table_data", [None, "rows", {}, [], [{}], [None, 3, "x"],
                                        [{"year": "", "event": "   ", "description": "\t\n"}],
                                        [{"unknown": "data outside the defined columns"}],
                                        [{"year": True}]])
def test_incomplete_tables(table_data):
    assert is_table_response_complete(crit(HISTORY_COLS), table_data) is False


@pytest.mark.parametrize("table_data", [
    [{"year": "2019", "event": "", "description": ""}],
    [{}, {"event": "  Opened campus  "}],
    [{"year": 2019}],                                      # numbers count as data
])
def test_complete_tables(table_data):
    assert is_table_response_complete(crit(HISTORY_COLS), table_data) is True


def test_string_columns_still_work():
    assert is_table_response_complete(crit(STRING_COLS), [{STRING_COLS[0]: "03/2025"}]) is True
    assert is_table_response_complete(crit(STRING_COLS), [{STRING_COLS[0]: " "}]) is False


def test_required_columns_must_all_be_filled_in_one_row():
    c = crit(REQUIRED_COLS)
    assert is_table_response_complete(c, [{"notes": "only the optional column"}]) is False
    assert is_table_response_complete(c, [{"year": "2019", "notes": "x"}]) is False          # event missing
    assert is_table_response_complete(c, [{"year": "2019"}, {"event": "Opened"}]) is False   # split across rows
    assert is_table_response_complete(c, [{"year": "2019", "event": "Opened"}]) is True


def test_criterion_without_usable_columns_is_never_complete():
    for cols in (None, [], "garbage", [{"label": ""}]):
        assert is_table_response_complete(crit(cols), [{"year": "2019"}]) is False


def test_row_has_data_limits_to_defined_columns():
    cols = normalize_table_columns(HISTORY_COLS)
    assert table_row_has_data({"stale": "x"}) is True                 # no columns: any key
    assert table_row_has_data({"stale": "x"}, cols) is False
    assert table_row_has_data({"year": "2019"}, cols) is True


# ---------- integration: save route + both completion aggregators ----------

@pytest.fixture
def env(tmp_path, monkeypatch):
    pg_url = os.environ.get("TEST_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", pg_url or f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("EXHIBIT_UPLOAD_DIR", str(tmp_path / "exhibits"))
    for var in ("SECRET_KEY", "GCS_BUCKET_NAME", "K_SERVICE"):
        monkeypatch.delenv(var, raising=False)
    if pg_url:
        monkeypatch.setenv("SECRET_KEY", "test-only-secret-key")
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        if pg_url:
            db.drop_all()
            db.create_all()
        u = Users(email="member@example.edu")
        u.set_password(PW)
        owner = Users(email="owner@example.edu")
        owner.set_password(PW)
        db.session.add_all([u, owner])
        db.session.add(StandardPack(id="pk", accrediting_body="ACCSC", application_type="initial",
                                    version_effective_date=date(2026, 1, 1)))
        db.session.flush()
        portfolio = Portfolio(owner_user_id=owner.id, name="P")
        db.session.add(portfolio)
        db.session.flush()
        application = Application(portfolio_id=portfolio.id, standard_pack_id="pk")
        db.session.add(application)
        db.session.flush()
        common = dict(standard_pack_id="pk", section_code="0", section_name="Section 0", subsection_code="A",
                      subsection_name="History", answer_type="table_rows", required=True)
        db.session.add(Criterion(id="hist", item_number="2", question_text="List your history.",
                                 table_columns=HISTORY_COLS, **common))
        db.session.add(Criterion(id="req", item_number="3", question_text="Required columns.",
                                 table_columns=REQUIRED_COLS, **common))
        db.session.add(Criterion(id="bad", item_number="4", question_text="Malformed columns.",
                                 table_columns="not json", **common))
        db.session.add(Criterion(id="exh", standard_pack_id="pk", section_code="0", section_name="Section 0",
                                 subsection_code="B", subsection_name="Docs", item_number="1",
                                 question_text="Upload.", answer_type="exhibit"))
        db.session.add(ApplicationMember(application_id=application.id, user_id=u.id))
        db.session.commit()
        app_id = application.id
    return app, app_id


def login(app):
    c = app.test_client()
    assert c.post("/api/auth/login", json={"email": "member@example.edu", "password": PW}).status_code == 200
    return c


def put(c, app_id, rows, criterion="hist"):
    return c.put(f"/api/applications/{app_id}/responses/{criterion}/table-rows", json={"rows": rows})


def answered(c, app_id, criterion="hist"):
    """Answered state from both aggregators; they must agree."""
    comp = c.get(f"/api/applications/{app_id}/completeness").get_json()
    in_overall = criterion not in [i["criterion_id"] for i in comp["incomplete_criteria"]]
    subs = c.get(f"/api/applications/{app_id}/subsections").get_json()
    sub_a = next(s for s in subs if s["subsection_code"] == "A")
    return in_overall, sub_a["answered_count"]


def test_history_table_lifecycle_counts_only_real_data(env):
    app, app_id = env
    c = login(app)
    assert answered(c, app_id) == (False, 0)                                   # no Response at all

    body = put(c, app_id, [{"year": "", "event": "  ", "description": ""}]).get_json()
    assert body["status"] == "saved" and body["is_complete"] is False and body["rows"] == []
    assert answered(c, app_id) == (False, 0)                                   # blank row: not answered

    body = put(c, app_id, [{"year": "2019", "event": "Founded", "description": ""}]).get_json()
    assert body["status"] == "saved" and body["is_complete"] is True and body["complete"] is True
    assert answered(c, app_id) == (True, 1)

    assert put(c, app_id, []).get_json()["is_complete"] is False                # all rows deleted
    assert answered(c, app_id) == (False, 0)


def test_subsection_is_not_2_of_2_with_one_blank_table(env):
    """The reported bug: 2/2 answered while a table is blank."""
    app, app_id = env
    c = login(app)
    put(c, app_id, [{"year": "2019", "event": "Founded"}], criterion="hist")
    put(c, app_id, [{"notes": "optional only"}], criterion="req")
    _, count = answered(c, app_id)
    assert count == 1


def test_required_columns_partial_row_is_saved_as_draft_but_not_complete(env):
    app, app_id = env
    c = login(app)
    body = put(c, app_id, [{"year": "2019", "notes": "draft"}], criterion="req").get_json()
    assert body["status"] == "saved" and body["is_complete"] is False
    assert body["rows"][0]["row_data"] == {"year": "2019", "event": "", "notes": "draft"}
    assert answered(c, app_id, "req")[0] is False
    assert put(c, app_id, [{"year": "2019", "event": "Founded"}], criterion="req").get_json()["is_complete"] is True
    assert answered(c, app_id, "req")[0] is True


def test_legacy_rows_with_stale_keys_do_not_count(env):
    """Rows stored before a column rename (keys no longer match table_columns) are not an answer."""
    app, app_id = env
    with app.app_context():
        r = Response(application_id=app_id, criterion_id="hist", narrative_text="", not_applicable=False)
        db.session.add(r)
        db.session.flush()
        db.session.add(TableRowData(response_id=r.id, row_index=0, row_data={"Old Column": "2019"}))
        db.session.commit()
    assert answered(login(app), app_id) == (False, 0)


@pytest.mark.parametrize("kwargs,code", [
    (dict(data="{not json", content_type="application/json"), "invalid_payload"),
    (dict(data="plain text", content_type="text/plain"), "invalid_payload"),
    (dict(json=[1, 2]), "invalid_payload"),
    (dict(json={"rows": {"year": "x"}}), "invalid_payload"),
    (dict(json={"rows": [1, "x"]}), "invalid_row"),
    (dict(json={"rows": [{"year": {"nested": 1}}]}), "invalid_cell"),
    (dict(json={"rows": [{"Year": "label used as key"}]}), "unknown_column"),
])
def test_malformed_payloads_are_400_never_500(env, kwargs, code):
    app, app_id = env
    resp = login(app).put(f"/api/applications/{app_id}/responses/hist/table-rows", **kwargs)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["code"] == code


def test_malformed_columns_in_db_are_422_not_500(env):
    app, app_id = env
    c = login(app)
    resp = put(c, app_id, [{"anything": "x"}], criterion="bad")
    assert resp.status_code == 422 and resp.get_json()["code"] == "table_not_configured"
    got = c.get(f"/api/applications/{app_id}/responses/bad/table-rows")
    assert got.status_code == 200 and got.get_json()["table_columns"] == []


def test_get_returns_object_columns_as_stored(env):
    app, app_id = env
    got = login(app).get(f"/api/applications/{app_id}/responses/hist/table-rows").get_json()
    assert got == {"table_columns": HISTORY_COLS, "rows": []}


def test_unknown_column_message_does_not_crash_on_odd_keys(env):
    app, app_id = env
    resp = put(login(app), app_id, [{"year": "2019", "": "blank key"}])
    assert resp.status_code == 400 and resp.get_json()["code"] == "unknown_column"


# ---------- 25 MB request guard ----------

def test_create_app_sets_25mb_max_content_length(env):
    app, _ = env
    assert app.config["MAX_CONTENT_LENGTH"] == 25 * 1024 * 1024


def test_oversized_api_request_gets_json_413(env):
    app, app_id = env
    app.config["MAX_CONTENT_LENGTH"] = 1024           # same handler, without sending 25 MB in a test
    c = login(app)
    resp = c.put(f"/api/applications/{app_id}/responses/hist/table-rows",
                 json={"rows": [{"description": "x" * 2000}]})
    assert resp.status_code == 413
    assert resp.get_json() == {"status": "error", "error": "The upload is too large. Files must be smaller than 25 MB.",
                               "code": "file_too_large"}
    up = c.post(f"/api/applications/{app_id}/responses/exh/exhibit",
                data={"file": (io.BytesIO(b"%PDF-1.4" + b"x" * 4096), "big.pdf")}, content_type="multipart/form-data")
    assert up.status_code == 413 and up.get_json()["code"] == "file_too_large"
