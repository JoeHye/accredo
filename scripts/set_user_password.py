"""Admin-only: set an Accredo user's password directly in Cloud SQL.

Replaces the removed unauthenticated /api/auth/reset-password endpoint.
Run from Cloud Shell (uses your gcloud credentials via the Cloud SQL connector):

    export ACCREDO_DBURL="$(gcloud secrets versions access latest --secret=accredo-database-url)"
    ~/rotvenv/bin/python ~/accredo-fix/scripts/set_user_password.py

Requires: pip install "cloud-sql-python-connector[pg8000]" werkzeug==3.0.3
"""
import getpass
import os
import sys
from urllib.parse import urlsplit, unquote

from google.cloud.sql.connector import Connector
from werkzeug.security import generate_password_hash, check_password_hash

INSTANCE = os.environ.get("CLOUD_SQL_INSTANCE", "accredo-test:us-central1:accredo-test")


def main():
    url = os.environ.get("ACCREDO_DBURL", "")
    if not url:
        sys.exit("ERROR: ACCREDO_DBURL is not set. Nothing changed.")
    u = urlsplit(url)
    user, pw, dbname = unquote(u.username or ""), unquote(u.password or ""), u.path.lstrip("/")
    if not (user and pw and dbname):
        sys.exit("ERROR: could not read user/password/database from ACCREDO_DBURL (values not shown). Nothing changed.")

    email = input("User email: ").strip().lower()
    if not email:
        sys.exit("ERROR: no email given. Nothing changed.")
    p1 = getpass.getpass(f"New password for {email} (min 16 chars): ")
    p2 = getpass.getpass("Repeat it: ")
    if p1 != p2:
        sys.exit("ERROR: passwords do not match. Nothing changed.")
    if len(p1) < 16:
        sys.exit("ERROR: use at least 16 characters. Nothing changed.")
    new_hash = generate_password_hash(p1)
    if not check_password_hash(new_hash, p1):
        sys.exit("ERROR: hash self-check failed. Nothing changed.")

    connector = Connector()
    try:
        conn = connector.connect(INSTANCE, "pg8000", user=user, password=pw, db=dbname)
    except Exception as e:
        connector.close()
        sys.exit(f"ERROR: could not connect to Cloud SQL ({type(e).__name__}): {e}. Nothing changed.")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET password_hash = %s WHERE email = %s", (new_hash, email))
        if cur.rowcount != 1:
            conn.rollback()
            sys.exit(f"ERROR: expected exactly 1 user with that email, found {cur.rowcount}. Rolled back, nothing changed.")
        conn.commit()
        cur.execute("SELECT password_hash FROM users WHERE email = %s", (email,))
        stored = cur.fetchone()[0]
        if not check_password_hash(stored, p1):
            sys.exit("ERROR: update committed but re-read check FAILED. Stop and report.")
        print(f"OK: password for {email} updated and verified against the database.")
    except SystemExit:
        raise
    except Exception as e:
        conn.rollback()
        sys.exit(f"ERROR: database update failed and was rolled back ({type(e).__name__}): {e}")
    finally:
        conn.close()
        connector.close()


if __name__ == "__main__":
    main()
