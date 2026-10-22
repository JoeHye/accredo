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
