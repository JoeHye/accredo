"""Backfill script against real PostgreSQL (the JSON-null failure only happens there).

Requires pgserver (pip install pgserver==0.1.4), which ships a real PostgreSQL binary;
the whole file is skipped if it is not installed.
"""
import importlib.util
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("backfill", os.path.join(REPO, "scripts", "backfill_table_columns.py"))
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


pgserver = pytest.importorskip("pgserver", reason="pgserver not installed; Postgres backfill tests skipped")
INITIAL = os.path.join(REPO, "standard_packs", "accsc_initial.json")
FIVE = ["0-A-2", "I-A-6", "I-E-2", "III-A-10", "VI-A-5"]


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")), cleanup_mode="stop")
    yield srv.get_uri()


@pytest.fixture
def pg(pg_uri, monkeypatch):
    """Fresh schema with the initial pack loaded through the real app, then table columns emptied
    in each form production can contain."""
    import psycopg2
    from app import create_app, db
    from app.loader import load_standard_pack
    from app.models import Criterion

    monkeypatch.setenv("DATABASE_URL", pg_uri.replace("postgresql://", "postgresql+psycopg2://", 1))
    monkeypatch.setenv("SECRET_KEY", "test-only")
    for var in ("GCS_BUCKET_NAME", "K_SERVICE"):
        monkeypatch.delenv(var, raising=False)
    app = create_app()
    with app.app_context():
        db.drop_all()
        db.create_all()
        load_standard_pack(INITIAL)
        # Production state: the pack was loaded before the JSON fix, so the ORM wrote None -> JSON null.
        for cid in FIVE:
            db.session.get(Criterion, cid).table_columns = None
        db.session.commit()
        db.engine.dispose()
    conn = psycopg2.connect(pg_uri)
    cur = conn.cursor()
    cur.execute("UPDATE criteria SET table_columns = NULL WHERE id = 'I-A-6'")          # SQL NULL
    cur.execute("UPDATE criteria SET table_columns = '[]'::jsonb WHERE id = 'I-E-2'")   # empty list
    cur.execute("""UPDATE criteria SET table_columns = '""'::jsonb WHERE id = 'III-A-10'""")  # empty string
    conn.commit()
    yield conn
    conn.close()


def stored(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, table_columns, COALESCE(jsonb_typeof(table_columns), 'SQL NULL') FROM criteria "
                "WHERE id = ANY(%s) ORDER BY id", (FIVE,))
    return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def test_orm_none_is_stored_as_json_null_and_old_guard_missed_it(pg):
    """Reproduces the production error: the previous UPDATE guard matched 0 rows for JSON null."""
    assert stored(pg)["0-A-2"] == (None, "null")
    cur = pg.cursor()
    cur.execute("UPDATE criteria SET table_columns = '[\"x\"]'::jsonb "
                "WHERE id = '0-A-2' AND (table_columns IS NULL OR table_columns = '[]'::jsonb)")
    assert cur.rowcount == 0
    pg.rollback()


def test_dry_run_reports_all_five_and_changes_nothing(pg):
    before = stored(pg)
    lines = []
    assert backfill.run_backfill(pg, backfill.pack_table_columns(json.load(open(INITIAL))), apply=False, out=lines.append) == 5
    assert stored(pg) == before
    text = "\n".join(lines)
    for cid in FIVE:
        assert f"would update {cid} " in text
    assert "currently stored as null" in text and "currently stored as SQL NULL" in text


def test_apply_fills_every_empty_form_then_is_idempotent(pg):
    pack_cols = backfill.pack_table_columns(json.load(open(INITIAL)))
    assert backfill.run_backfill(pg, pack_cols, apply=True, out=lambda _: None) == 5
    after = stored(pg)
    assert all(after[cid] == (pack_cols[cid], "array") for cid in FIVE), after
    lines = []
    assert backfill.run_backfill(pg, pack_cols, apply=True, out=lines.append) == 0
    assert sum("already correct" in l for l in lines) == 5


def test_apply_never_overwrites_real_columns(pg):
    cur = pg.cursor()
    cur.execute("""UPDATE criteria SET table_columns = '["Hand set"]'::jsonb WHERE id = 'VI-A-5'""")
    pg.commit()
    pack_cols = backfill.pack_table_columns(json.load(open(INITIAL)))
    assert backfill.run_backfill(pg, pack_cols, apply=True, out=lambda _: None) == 4
    assert stored(pg)["VI-A-5"] == (["Hand set"], "array")


def test_row_changed_between_select_and_update_aborts_whole_apply(pg, monkeypatch):
    """If the guard stops matching (value changed after the SELECT), nothing is committed."""
    pack_cols = backfill.pack_table_columns(json.load(open(INITIAL)))
    monkeypatch.setattr(backfill, "UPDATE_SQL", backfill.UPDATE_SQL + " AND false")
    with pytest.raises(RuntimeError, match="expected to update 1 row for 0-A-2"):
        backfill.run_backfill(pg, pack_cols, apply=True, out=lambda _: None)
    pg.rollback()
    assert stored(pg)["0-A-2"] == (None, "null")
