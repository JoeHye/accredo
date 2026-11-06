"""Pure-logic tests for scripts/backfill_table_columns.py (the Cloud SQL write path runs only against Postgres)."""
import importlib.util
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("backfill", os.path.join(REPO, "scripts", "backfill_table_columns.py"))
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

COLS = ["Approval Date (Month/Year)", "Substantive Change"]


def test_reads_all_five_tables_from_the_initial_pack():
    with open(os.path.join(REPO, "standard_packs", "accsc_initial.json"), encoding="utf-8") as f:
        cols = backfill.pack_table_columns(json.load(f))
    assert set(cols) == {"0-A-2", "I-A-6", "I-E-2", "III-A-10", "VI-A-5"}
    assert cols["0-A-2"] == COLS


def test_plan_fills_only_empty_and_never_overwrites():
    pack = {"a": COLS, "b": COLS, "c": COLS, "d": COLS, "e": COLS}
    db_rows = {"a": None, "b": [], "c": "[]", "d": ["Other"], "e": COLS}
    to_update, skipped = backfill.plan_updates(pack, db_rows)
    assert [t["id"] for t in to_update] == ["a", "b", "c"]
    reasons = {s["id"]: s["reason"] for s in skipped}
    assert reasons["d"].startswith("already has different columns")
    assert reasons["e"] == "already correct"


def test_ids_missing_from_db_are_reported_not_created():
    to_update, skipped = backfill.plan_updates({"zz": COLS}, {})
    assert to_update == [] and skipped == [{"id": "zz", "reason": "not in database"}]


def test_missing_dburl_exits_without_touching_anything(monkeypatch):
    monkeypatch.delenv("ACCREDO_DBURL", raising=False)
    with pytest.raises(SystemExit, match="ACCREDO_DBURL is not set"):
        backfill.main([])


def test_malformed_dburl_never_echoed(monkeypatch):
    monkeypatch.setenv("ACCREDO_DBURL", "postgresql+psycopg2://onlyuser@/")
    with pytest.raises(SystemExit) as e:
        backfill.main([])
    assert "values not shown" in str(e.value) and "onlyuser" not in str(e.value)


def test_empty_string_and_json_text_forms_count_as_empty():
    to_update, _ = backfill.plan_updates({"a": COLS, "b": COLS}, {"a": "", "b": "null"})
    assert [t["id"] for t in to_update] == ["a", "b"]


# ---------- apply path with a fake DB-API connection (no Postgres needed) ----------

class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rowcount = conn, -1

    def execute(self, sql, params):
        self.conn.sql.append(sql)
        if sql.startswith("SELECT"):
            self._rows = [(cid, cols, "null" if cols is None else "array")
                          for cid, cols in self.conn.rows.items() if cid in params]
        else:
            cols, cid = params
            self.rowcount = 0 if cid in self.conn.missing_on_update else int(cid in self.conn.rows)
            if self.rowcount:
                self.conn.rows[cid] = json.loads(cols)

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, rows, missing_on_update=()):
        self.rows, self.missing_on_update = dict(rows), set(missing_on_update)
        self.sql, self.commits, self.rollbacks = [], 0, 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_update_targets_primary_key_only_and_select_locks_rows():
    assert backfill.UPDATE_SQL == "UPDATE criteria SET table_columns = CAST(%s AS JSONB) WHERE id = %s"
    assert backfill.SELECT_SQL.rstrip().endswith("FOR UPDATE")


def test_apply_updates_every_planned_row_by_id_and_commits():
    conn = FakeConn({"0-A-2": None, "x": ["Other"]})
    lines = []
    assert backfill.run_backfill(conn, {"0-A-2": COLS, "x": COLS}, apply=True, out=lines.append) == 1
    assert conn.rows == {"0-A-2": COLS, "x": ["Other"]} and conn.commits == 1
    assert lines[-1] == "OK: updated 1 criteria: 0-A-2."
    assert [s for s in conn.sql if s.startswith("UPDATE")] == [backfill.UPDATE_SQL]


def test_apply_raises_without_commit_when_a_planned_row_is_not_updated():
    conn = FakeConn({"0-A-2": None, "I-A-6": None}, missing_on_update={"I-A-6"})
    with pytest.raises(RuntimeError, match="expected to update 1 row for I-A-6"):
        backfill.run_backfill(conn, {"0-A-2": COLS, "I-A-6": COLS}, apply=True, out=lambda _: None)
    assert conn.commits == 0


def test_dry_run_rolls_back_and_issues_no_update():
    conn = FakeConn({"0-A-2": None})
    assert backfill.run_backfill(conn, {"0-A-2": COLS}, apply=False, out=lambda _: None) == 1
    assert conn.rollbacks == 1 and conn.commits == 0
    assert not any(s.startswith("UPDATE") for s in conn.sql) and conn.rows["0-A-2"] is None
