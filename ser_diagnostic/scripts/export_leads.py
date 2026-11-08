"""Admin-only, read-only: export SER Readiness Diagnostic leads to CSV or JSON.

Run from Cloud Shell (uses your gcloud credentials via the Cloud SQL connector):

    export SER_DBURL="$(gcloud secrets versions access 1 --secret=ser-database-url)"
    ~/rotvenv/bin/python ~/accredo-fix/ser_diagnostic/scripts/export_leads.py --out ~/ser_leads.csv

Options:
    --pilot-only         only leads that clicked the pilot CTA
    --since YYYY-MM-DD   only leads created on or after this date (UTC)
    --include-test       include test leads (excluded by default: school starts with
                         "TEST" or email contains "+sertest")
    --format csv|json    output format (default csv). JSON nests answers per lead.
    --out PATH           write here (file is created with owner-only permissions);
                         omit to print to stdout

Requires: pip install "cloud-sql-python-connector[pg8000]"

The output contains personal data (names, emails). Do not commit it or share it
outside the team.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime
from urllib.parse import unquote, urlsplit

INSTANCE = os.environ.get("CLOUD_SQL_INSTANCE", "accredo-test:us-central1:accredo-test")
CONTENT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "content.json")

BASE_COLUMNS = ["created_at", "name", "email", "school", "percent", "band", "gated",
                "pilot_requested", "pilot_requested_at", "points", "max_points", "content_version", "id"]
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def build_query(pilot_only=False, since=None, include_test=False, placeholder="%s"):
    """Return (sql, params). Read-only SELECT; filters are parameterized."""
    where, params = [], []
    if pilot_only:
        where.append("pilot_requested = " + ("TRUE" if placeholder == "%s" else "1"))
    if since is not None:
        where.append(f"created_at >= {placeholder}")
        params.append(since)
    if not include_test:
        where.append(f"NOT (UPPER(school) LIKE {placeholder} OR LOWER(email) LIKE {placeholder})")
        params.extend(["TEST%", "%+sertest%"])
    sql = ("SELECT id, created_at, name, email, school, consent, answers, points, max_points, percent, "
           "band, gated, content_version, pilot_requested, pilot_requested_at FROM ser_leads")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pilot_requested DESC, created_at DESC"
    return sql, params


def safe_cell(value):
    """Neutralize spreadsheet formula injection from user-entered text."""
    if value is None:
        return ""
    s = str(value)
    return "'" + s if s.startswith(FORMULA_PREFIXES) else s


def write_csv(rows, columns, question_ids, out):
    """rows: iterable of tuples in `columns` order (as returned by build_query's SELECT)."""
    writer = csv.writer(out)
    writer.writerow(BASE_COLUMNS + question_ids)
    count = 0
    for row in rows:
        r = dict(zip(columns, row))
        answers = r.get("answers") or {}
        if isinstance(answers, str):
            try:
                answers = json.loads(answers)
            except json.JSONDecodeError:
                answers = {}
        writer.writerow([safe_cell(r.get(c)) for c in BASE_COLUMNS] +
                        [safe_cell(answers.get(q, "")) for q in question_ids])
        count += 1
    return count


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def write_json(rows, columns, question_ids, out):
    """Write a JSON array; each lead has the base fields plus an 'answers' object.

    Values are kept raw (no spreadsheet escaping) since JSON is not opened as a sheet.
    """
    leads = []
    for row in rows:
        r = dict(zip(columns, row))
        answers = r.get("answers") or {}
        if isinstance(answers, str):
            try:
                answers = json.loads(answers)
            except json.JSONDecodeError:
                answers = {}
        lead = {c: _json_value(r.get(c)) for c in BASE_COLUMNS}
        lead["answers"] = {q: answers.get(q) for q in question_ids}
        leads.append(lead)
    json.dump(leads, out, indent=2, default=str)
    out.write("\n")
    return len(leads)


def load_question_ids(path=CONTENT_PATH):
    with open(path, encoding="utf-8") as f:
        return [q["id"] for q in json.load(f)["questions"]]


def parse_args(argv):
    p = argparse.ArgumentParser(description="Export SER diagnostic leads to CSV (read-only).")
    p.add_argument("--pilot-only", action="store_true")
    p.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d"))
    p.add_argument("--include-test", action="store_true")
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--out")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    url = os.environ.get("SER_DBURL", "")
    if not url:
        sys.exit("ERROR: SER_DBURL is not set. Nothing exported.")
    u = urlsplit(url)
    user, pw, dbname = unquote(u.username or ""), unquote(u.password or ""), u.path.lstrip("/")
    if not (user and pw and dbname):
        sys.exit("ERROR: could not read user/password/database from SER_DBURL (values not shown). Nothing exported.")

    try:
        question_ids = load_question_ids()
    except (OSError, KeyError, json.JSONDecodeError) as e:
        sys.exit(f"ERROR: could not read question ids from {CONTENT_PATH}: {e}")

    from google.cloud.sql.connector import Connector  # imported here so the pure functions are testable without it

    sql, params = build_query(args.pilot_only, args.since, args.include_test)
    connector = Connector()
    try:
        conn = connector.connect(INSTANCE, "pg8000", user=user, password=pw, db=dbname)
    except Exception as e:
        connector.close()
        sys.exit(f"ERROR: could not connect to Cloud SQL ({type(e).__name__}): {e}")

    out = None
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if args.out:
            path = os.path.expanduser(args.out)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            out = os.fdopen(fd, "w", newline="", encoding="utf-8")
        else:
            out = sys.stdout
        writer = write_json if args.format == "json" else write_csv
        count = writer(rows, columns, question_ids, out)
        conn.rollback()  # read-only transaction; nothing to commit
    except Exception as e:
        sys.exit(f"ERROR: export failed ({type(e).__name__}): {e}")
    finally:
        if out is not None and out is not sys.stdout:
            out.close()
        conn.close()
        connector.close()

    where = f" to {args.out}" if args.out else ""
    print(f"OK: exported {count} lead(s) as {args.format.upper()}{where}.", file=sys.stderr)


if __name__ == "__main__":
    main()
