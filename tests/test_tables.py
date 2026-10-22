"""Table answers (table_rows): column data, save validation, and completion status.

Run from the repo root:  python -m pytest tests
"""
import json
import os
from datetime import date

import pytest

from app import create_app, db
from app.models import Application, ApplicationMember, Criterion, Portfolio, StandardPack, Users

PW = "password123"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = ["Approval Date (Month/Year)", "Substantive Change"]
INITIAL_TABLES = {"0-A-2", "I-A-6", "I-E-2", "III-A-10", "VI-A-5"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    for var in ("SECRET_KEY", "GCS_BUCKET_NAME", "K_SERVICE"):
        monkeypatch.delenv(var, raising=False)
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        users = {}
        for name in ("owner", "member", "outsider"):
            u = Users(email=f"{name}@example.edu")
            u.set_password(PW)
            db.session.add(u)
            users[name] = u
        db.session.add(StandardPack(id="pk", accrediting_body="ACCSC", application_type="initial",
                                    version_effective_date=date(2026, 1, 1)))
        db.session.flush()
        portfolio = Portfolio(owner_user_id=users["owner"].id, name="P")
        db.session.add(portfolio)
        db.session.flush()
        application = Application(portfolio_id=portfolio.id, standard_pack_id="pk")
        db.session.add(application)
        db.session.flush()
        db.session.add(Criterion(id="hist", standard_pack_id="pk", section_code="0", section_name="S",
                                 subsection_code="A", subsection_name="Sub A", item_number="2",
                                 question_text="List substantive changes.", answer_type="table_rows",
                                 table_columns=COLS))
        db.session.add(Criterion(id="nocols", standard_pack_id="pk", section_code="0", section_name="S",
                                 subsection_code="B", subsection_name="Sub B", item_number="1",
                                 question_text="Broken table.", answer_type="table_rows", table_columns=None))
        db.session.add(ApplicationMember(application_id=application.id, user_id=users["member"].id))
        db.session.commit()
        app_id = application.id
    return app, app_id


def login(app, who):
    c = app.test_client()
    assert c.post("/api/auth/login", json={"email": f"{who}@example.edu", "password": PW}).status_code == 200
    return c


def put_rows(c, app_id, rows, crit="hist"):
    return c.put(f"/api/applications/{app_id}/responses/{crit}/table-rows", json={"rows": rows})


def hist_complete(c, app_id):
    comp = c.get(f"/api/applications/{app_id}/completeness").get_json()
    in_overall = "hist" not in [i["criterion_id"] for i in comp["incomplete_criteria"]]
    subs = c.get(f"/api/applications/{app_id}/subsections").get_json()
    sub = next(s for s in subs if s["subsection_code"] == "A")
    in_subsection = sub["answered_count"] == sub["criterion_count"]
    assert in_overall == in_subsection, "both completion aggregators must agree"
    return in_overall


# ---------- the column data that caused the lock ----------

@pytest.mark.parametrize("pack", ["standard_packs/accsc_initial.json", "accsc_standard_pack.json"])
def test_initial_pack_tables_all_have_columns(pack):
    def walk(o):
        if isinstance(o, dict):
            if o.get("answer_type") == "table_rows":
                yield o
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
    tables = {c["criterion_id"]: c.get("table_columns") for c in walk(json.load(open(os.path.join(REPO, pack))))}
    assert set(tables) == INITIAL_TABLES
    assert all(isinstance(cols, list) and cols and all(isinstance(x, str) and x for x in cols)
               for cols in tables.values()), tables
    assert tables["0-A-2"] == COLS


def test_loading_initial_pack_creates_tables_with_columns(env):
    from app.loader import load_standard_pack
    app, _ = env
    with app.app_context():
        pack = load_standard_pack(os.path.join(REPO, "standard_packs", "accsc_initial.json"))
        rows = Criterion.query.filter_by(standard_pack_id=pack.id, answer_type="table_rows").all()
        assert {c.id for c in rows} == INITIAL_TABLES
        assert all(c.table_columns for c in rows)


def test_get_returns_columns(env):
    app, app_id = env
    data = login(app, "member").get(f"/api/applications/{app_id}/responses/hist/table-rows").get_json()
    assert data == {"table_columns": COLS, "rows": []}


# ---------- saving ----------

def test_member_saves_rows_and_blank_rows_are_dropped(env):
    app, app_id = env
    c = login(app, "member")
    resp = put_rows(c, app_id, [
        {COLS[0]: "03/2025", COLS[1]: "Change of ownership"},
        {COLS[0]: "  ", COLS[1]: ""},
        {COLS[0]: "06/2025", COLS[1]: 7},
    ])
    body = resp.get_json()
    assert resp.status_code == 200 and body["complete"] is True
    assert [r["row_data"] for r in body["rows"]] == [
        {COLS[0]: "03/2025", COLS[1]: "Change of ownership"}, {COLS[0]: "06/2025", COLS[1]: "7"}]
    got = c.get(f"/api/applications/{app_id}/responses/hist/table-rows").get_json()["rows"]
    assert [r["row_index"] for r in got] == [0, 1]


def test_outsider_cannot_save(env):
    app, app_id = env
    assert put_rows(login(app, "outsider"), app_id, [{COLS[0]: "x"}]).status_code == 403


@pytest.mark.parametrize("payload,status,code", [
    ({"rows": "nope"}, 400, "invalid_payload"),
    ({}, 400, "invalid_payload"),
    ({"rows": ["not a dict"]}, 400, "invalid_row"),
    ({"rows": [{"Made Up Column": "x"}]}, 400, "unknown_column"),
    ({"rows": [{COLS[0]: ["list"]}]}, 400, "invalid_cell"),
    ({"rows": [{COLS[0]: "x" * 2001}]}, 400, "cell_too_long"),
    ({"rows": [{COLS[0]: "x"}] * 501}, 400, "too_many_rows"),
])
def test_validation(env, payload, status, code):
    app, app_id = env
    resp = login(app, "member").put(f"/api/applications/{app_id}/responses/hist/table-rows", json=payload)
    assert resp.status_code == status and resp.get_json()["code"] == code, resp.get_json()
    assert "details" not in resp.get_json()


def test_non_json_body_is_a_clean_400(env):
    app, app_id = env
    resp = login(app, "member").put(f"/api/applications/{app_id}/responses/hist/table-rows",
                                    data="not json", content_type="text/plain")
    assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_payload"


def test_table_without_columns_refuses_to_save(env):
    app, app_id = env
    resp = put_rows(login(app, "member"), app_id, [{}], crit="nocols")
    assert resp.status_code == 422 and resp.get_json()["code"] == "table_not_configured"


def test_db_error_on_save_is_generic(env, monkeypatch):
    from sqlalchemy.exc import OperationalError
    app, app_id = env
    c = login(app, "member")

    def boom():
        raise OperationalError("INSERT", {}, Exception("SECRET-DETAIL"))
    monkeypatch.setattr(db.session, "commit", boom)
    resp = put_rows(c, app_id, [{COLS[0]: "x"}])
    assert resp.status_code == 500 and resp.get_json()["code"] == "database_error"
    assert "SECRET-DETAIL" not in resp.get_data(as_text=True)


# ---------- completion status ----------

def test_completion_tracks_real_data(env):
    app, app_id = env
    c = login(app, "member")
    assert hist_complete(c, app_id) is False                        # nothing yet
    assert put_rows(c, app_id, [{COLS[0]: "", COLS[1]: "   "}]).get_json()["complete"] is False
    assert hist_complete(c, app_id) is False                        # blank row is not an answer
    put_rows(c, app_id, [{COLS[0]: "03/2025", COLS[1]: "Change of ownership"}])
    assert hist_complete(c, app_id) is True                         # real data -> complete
    put_rows(c, app_id, [])
    assert hist_complete(c, app_id) is False                        # cleared -> incomplete again


def test_legacy_blank_rows_already_in_db_do_not_count(env):
    from app.models import Response, TableRowData
    app, app_id = env
    with app.app_context():
        r = Response(application_id=app_id, criterion_id="hist", narrative_text="", not_applicable=False)
        db.session.add(r)
        db.session.flush()
        db.session.add(TableRowData(response_id=r.id, row_index=0, row_data={}))
        db.session.add(TableRowData(response_id=r.id, row_index=1, row_data={COLS[0]: ""}))
        db.session.commit()
    assert hist_complete(login(app, "member"), app_id) is False
