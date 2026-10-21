"""Admin-only: fill in missing table_columns for table_rows criteria in Cloud SQL.

The loader skips a StandardPack that already exists, so fixes to a pack JSON never reach a
database that loaded the old version. This copies non-empty table_columns from the pack JSON
into criteria whose table_columns are currently NULL or empty. It never overwrites existing
columns. Dry run by default.

Run from Cloud Shell (uses your gcloud credentials via the Cloud SQL connector):

    export ACCREDO_DBURL="$(gcloud secrets versions access latest --secret=accredo-database-url)"
    ~/rotvenv/bin/python ~/accredo-fix/scripts/backfill_table_columns.py            # dry run
    ~/rotvenv/bin/python ~/accredo-fix/scripts/backfill_table_columns.py --apply    # write

Requires: pip install "cloud-sql-python-connector[pg8000]"
"""
import argparse
import json
import os
import sys
from urllib.parse import unquote, urlsplit

INSTANCE = os.environ.get("CLOUD_SQL_INSTANCE", "accredo-test:us-central1:accredo-test")
DEFAULT_PACK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "standard_packs", "accsc_initial.json")


def pack_table_columns(pack):
    """{criterion_id: columns} for every table_rows criterion in the pack that has non-empty columns."""
    found = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get("answer_type") == "table_rows" and o.get("criterion_id"):
                cols = o.get("table_columns")
                if isinstance(cols, list) and cols and all(isinstance(c, str) and c for c in cols):
                    found[o["criterion_id"]] = cols
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(pack)
    return found


def plan_updates(pack_cols, db_rows):
    """db_rows: {criterion_id: current_columns_or_None}. Returns (to_update, skipped) lists of dicts."""
    to_update, skipped = [], []
    for cid, cols in sorted(pack_cols.items()):
        if cid not in db_rows:
            skipped.append({"id": cid, "reason": "not in database"})
            continue
        current = db_rows[cid]
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except json.JSONDecodeError:
                current = None
        if not current:
            to_update.append({"id": cid, "columns": cols})
        elif current != cols:
            skipped.append({"id": cid, "reason": "already has different columns; left unchanged", "current": current})
        else:
            skipped.append({"id": cid, "reason": "already correct"})
    return to_update, skipped


def main(argv=None):
    p = argparse.ArgumentParser(description="Backfill missing table_columns from a StandardPack JSON.")
    p.add_argument("--pack", default=DEFAULT_PACK)
    p.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    args = p.parse_args(argv)

    url = os.environ.get("ACCREDO_DBURL", "")
    if not url:
        sys.exit("ERROR: ACCREDO_DBURL is not set. Nothing changed.")
    u = urlsplit(url)
    user, pw, dbname = unquote(u.username or ""), unquote(u.password or ""), u.path.lstrip("/")
    if not (user and pw and dbname):
        sys.exit("ERROR: could not read user/password/database from ACCREDO_DBURL (values not shown). Nothing changed.")

    try:
        with open(args.pack, encoding="utf-8") as f:
            pack_cols = pack_table_columns(json.load(f))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"ERROR: could not read pack {args.pack}: {e}. Nothing changed.")
    if not pack_cols:
        sys.exit(f"ERROR: no table_rows criteria with columns found in {args.pack}. Nothing changed.")

    from google.cloud.sql.connector import Connector

    connector = Connector()
    try:
        conn = connector.connect(INSTANCE, "pg8000", user=user, password=pw, db=dbname)
    except Exception as e:
        connector.close()
        sys.exit(f"ERROR: could not connect to Cloud SQL ({type(e).__name__}): {e}. Nothing changed.")

    try:
        cur = conn.cursor()
        ids = sorted(pack_cols)
        placeholders = ", ".join(["%s"] * len(ids))
        cur.execute(f"SELECT id, table_columns FROM criteria WHERE answer_type = 'table_rows' AND id IN ({placeholders})", ids)
        db_rows = {row[0]: row[1] for row in cur.fetchall()}
        to_update, skipped = plan_updates(pack_cols, db_rows)

        for s in skipped:
            print(f"skip   {s['id']}: {s['reason']}")
        for t in to_update:
            print(f"{'UPDATE' if args.apply else 'would update'} {t['id']}: {len(t['columns'])} columns -> {t['columns']}")

        if not args.apply:
            conn.rollback()
            print(f"DRY RUN: {len(to_update)} criteria would be updated. Re-run with --apply to write.")
            return
        for t in to_update:
            cur.execute("UPDATE criteria SET table_columns = CAST(%s AS JSONB) "
                        "WHERE id = %s AND (table_columns IS NULL OR table_columns = '[]'::jsonb)",
                        (json.dumps(t["columns"]), t["id"]))
            if cur.rowcount != 1:
                raise RuntimeError(f"expected to update 1 row for {t['id']}, updated {cur.rowcount}")
        conn.commit()
        print(f"OK: updated {len(to_update)} criteria.")
    except Exception as e:
        conn.rollback()
        sys.exit(f"ERROR: backfill failed and was rolled back ({type(e).__name__}): {e}")
    finally:
        conn.close()
        connector.close()


if __name__ == "__main__":
    main()
