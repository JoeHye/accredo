import csv
import importlib.util
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from ser_app import SerLead, create_app, db
from ser_app.scoring import DEFAULT_CONTENT_PATH, load_content

_spec = importlib.util.spec_from_file_location(
    "export_leads", os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "export_leads.py"))
export_leads = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_leads)

QIDS = [q["id"] for q in load_content(DEFAULT_CONTENT_PATH)["questions"]]


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "ser_export.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.delenv("K_SERVICE", raising=False)
    app = create_app()
    c = app.test_client()

    def submit(name, email, school, answer="yes", pilot=False, **overrides):
        data = {"name": name, "email": email, "school": school, "consent": "yes", "website": ""}
        data.update({q: answer for q in QIDS})
        data.update(overrides)
        resp = c.post("/submit", data=data)
        assert resp.status_code == 303
        lead_id = resp.headers["Location"].rsplit("/", 1)[-1]
        if pilot:
            assert c.post(f"/result/{lead_id}/pilot").status_code == 303
        return lead_id

    submit("Real Person", "real@school.edu", "Real Tech", pilot=True)
    submit("Other Person", "other@school.edu", "Other Academy", answer="partly")
    submit("Joe", "joehye+sertest@gmail.com", "TEST - delete", pilot=True)
    submit("Mallory", "m@school.edu", "=HYPERLINK(\"http://evil\")", answer="no")
    old = submit("Old Lead", "old@school.edu", "Old School")
    with app.app_context():
        lead = db.session.get(SerLead, old)
        lead.created_at = datetime.utcnow() - timedelta(days=30)
        db.session.commit()
    return path


def run_export(db_path, **kwargs):
    sql, params = export_leads.build_query(placeholder="?", **kwargs)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(sql, params)
        columns = [d[0] for d in cur.description]
        out = io.StringIO()
        count = export_leads.write_csv(cur.fetchall(), columns, QIDS, out)
    finally:
        conn.close()
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert len(rows) == count
    return rows


def test_default_excludes_test_leads_and_orders_pilot_first(db_path):
    rows = run_export(db_path)
    emails = [r["email"] for r in rows]
    assert "joehye+sertest@gmail.com" not in emails
    assert len(rows) == 4
    assert rows[0]["email"] == "real@school.edu" and rows[0]["pilot_requested"] in ("1", "True")


def test_include_test(db_path):
    assert "joehye+sertest@gmail.com" in [r["email"] for r in run_export(db_path, include_test=True)]


def test_pilot_only(db_path):
    assert [r["email"] for r in run_export(db_path, pilot_only=True)] == ["real@school.edu"]


def test_since_filter(db_path):
    emails = [r["email"] for r in run_export(db_path, since=datetime.utcnow() - timedelta(days=1))]
    assert "old@school.edu" not in emails and "real@school.edu" in emails


def test_answer_columns_and_band_present(db_path):
    rows = {r["email"]: r for r in run_export(db_path)}
    assert list(rows["other@school.edu"].keys())[-len(QIDS):] == QIDS
    assert all(rows["other@school.edu"][q] == "partly" for q in QIDS)
    assert rows["other@school.edu"]["band"] == "yellow" and rows["other@school.edu"]["percent"] == "50.0"


def test_formula_injection_neutralized(db_path):
    school = [r["school"] for r in run_export(db_path) if r["email"] == "m@school.edu"][0]
    assert school.startswith("'=")


def test_main_without_dburl_exits_cleanly(monkeypatch):
    monkeypatch.delenv("SER_DBURL", raising=False)
    with pytest.raises(SystemExit) as e:
        export_leads.main([])
    assert "SER_DBURL is not set" in str(e.value)


def test_main_with_malformed_dburl_exits_without_echoing_it(monkeypatch):
    monkeypatch.setenv("SER_DBURL", "postgresql+psycopg2://nopassword@/")
    with pytest.raises(SystemExit) as e:
        export_leads.main([])
    assert "values not shown" in str(e.value) and "nopassword" not in str(e.value)


def test_query_is_select_only():
    sql, _ = export_leads.build_query(pilot_only=True, since=datetime(2026, 1, 1))
    assert sql.lstrip().upper().startswith("SELECT")
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
        assert word not in sql.upper()


def test_json_export_nests_answers_and_keeps_raw_values(db_path):
    sql, params = export_leads.build_query(placeholder="?")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(sql, params)
        columns = [d[0] for d in cur.description]
        out = io.StringIO()
        count = export_leads.write_json(cur.fetchall(), columns, QIDS, out)
    finally:
        conn.close()
    leads = json.loads(out.getvalue())
    assert count == len(leads) == 4
    by_email = {lead["email"]: lead for lead in leads}
    assert by_email["other@school.edu"]["answers"] == {q: "partly" for q in QIDS}
    assert by_email["other@school.edu"]["band"] == "yellow"
    assert by_email["m@school.edu"]["school"].startswith("=")  # raw in JSON, escaped only in CSV
    assert "joehye+sertest@gmail.com" not in by_email


def test_format_flag_parses():
    assert export_leads.parse_args(["--format", "json"]).format == "json"
    assert export_leads.parse_args([]).format == "csv"
    with pytest.raises(SystemExit):
        export_leads.parse_args(["--format", "xml"])


class _FakeCursor:
    """pg8000-style cursor over sqlite: %s placeholders, ignores SET TRANSACTION."""
    def __init__(self, conn, log):
        self._cur, self._log = conn.cursor(), log
    def execute(self, sql, params=()):
        self._log.append(sql)
        if sql.startswith("SET TRANSACTION"):
            return
        self._cur.execute(sql.replace("%s", "?").replace("= TRUE", "= 1"), params)
    @property
    def description(self):
        return self._cur.description
    def fetchall(self):
        return self._cur.fetchall()


class _FakeConn:
    def __init__(self, path, log):
        self._conn, self._log = sqlite3.connect(path), log
    def cursor(self):
        return _FakeCursor(self._conn, self._log)
    def rollback(self):
        self._log.append("ROLLBACK")
    def close(self):
        self._conn.close()


def test_main_end_to_end_writes_owner_only_file(db_path, tmp_path, monkeypatch):
    import sys, types
    log = []
    fake_mod = types.ModuleType("google.cloud.sql.connector")
    class Connector:
        def connect(self, instance, driver, user, password, db):
            assert (driver, user, password, db) == ("pg8000", "ser_app", "pw", "ser")
            return _FakeConn(db_path, log)
        def close(self):
            log.append("CONNECTOR_CLOSED")
    fake_mod.Connector = Connector
    monkeypatch.setitem(sys.modules, "google.cloud.sql.connector", fake_mod)
    monkeypatch.setenv("SER_DBURL", "postgresql+psycopg2://ser_app:pw@/ser?host=/cloudsql/x")

    out_csv, out_json = tmp_path / "leads.csv", tmp_path / "leads.json"
    export_leads.main(["--out", str(out_csv), "--pilot-only"])
    export_leads.main(["--out", str(out_json), "--format", "json", "--include-test"])

    assert oct(out_csv.stat().st_mode & 0o777) == "0o600"
    rows = list(csv.DictReader(out_csv.open()))
    assert [r["email"] for r in rows] == ["real@school.edu"]
    assert len(json.loads(out_json.read_text())) == 5
    assert log.count("SET TRANSACTION READ ONLY") == 2 and log.count("ROLLBACK") == 2
    assert log.count("CONNECTOR_CLOSED") == 2
