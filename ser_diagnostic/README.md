# SER Readiness Diagnostic

A public, 10-question ACCSC Self-Evaluation Report readiness check that stores leads.
It is a **separate service** from the main Accredo app: it has its own Flask app, image,
Cloud Run service, service account, and database.

> **Status: scaffold. Content is DRAFT v0.1, awaiting owner review.**
> While `content.json` says `"status": "DRAFT"`, the app answers **503** unless
> `SER_ALLOW_DRAFT=1` is set, so a draft can't go public by accident. To publish, set
> `"status": "APPROVED"`. That status also requires `branding` and
> `call_to_action.decided` to be filled in, or the app refuses to start.

## Architecture (agreed)

| Piece | Value |
|---|---|
| Code | this folder, `ser_diagnostic/` in the Accredo repo. Kept out of the main Accredo image by the root `.dockerignore`. |
| Cloud Run service | `ser-diagnostic`, project `accredo-test`, region `us-central1`, public (unauthenticated) |
| Service account | `ser-diagnostic-runner`: **only** `roles/cloudsql.client` plus access to **one** secret |
| Database | its own database `ser` and user `ser_app` on the existing Cloud SQL instance `accredo-test` |
| Secret | `ser-database-url`, mounted as `DATABASE_URL` |
| Image | `gcr.io/accredo-test/ser-diagnostic` |
| Email | **none**. The result is shown on screen and the lead is stored. |

**Stored per lead:** name, email, school name, answers, score, and a timestamp (the agreed
fields). Two extra fields, `result_key` and `content_version`, record which wording and
scoring produced the score. They contain no personal data.

## Layout
```
ser_diagnostic/
  content.json          questions, scoring, result bands, CTA (edit wording here, not in code)
  ser_app/__init__.py   create_app(): content load + validation, DB config, /health
  ser_app/content.py    content validation and scoring (pure functions)
  ser_app/routes.py     GET /  and  POST /api/submit
  ser_app/models.py     Lead -> table ser_leads
  ser_app/templates/    index.html (form + result), unavailable.html (draft not published)
  tests/                pytest suite
  Dockerfile            python:3.11-slim, non-root, gunicorn on $PORT
```

## Behaviour and failure paths
| Situation | Response |
|---|---|
| Content DRAFT, no `SER_ALLOW_DRAFT=1` | `GET /` and `POST /api/submit` return 503 "not published" |
| `content.json` missing, malformed, or inconsistent | the app refuses to start and logs the exact problem |
| On Cloud Run (`K_SERVICE` set) without `DATABASE_URL` | the app refuses to start rather than store leads on ephemeral disk |
| Invalid fields or answers | 400 with per-field messages; nothing stored |
| Body over 16 KB | 413 |
| Hidden honeypot field filled (simple bots) | normal result shown, nothing stored, a warning logged |
| Database error on save | 503 "couldn't save your results"; full error in logs only, never in the response |
| `/health` database error | 503 with a generic message (same rule as Accredo PR #3) |

**Scoring rules, as drafted:** Yes = 2, Partly = 1, No = 0, maximum 20. A **No** on Q1 or
Q2 always gives "Not yet eligible". **Partly** on a gate question does *not* trigger the
gate; that's an open review question. Bands: 16+ is Ready, 9–15 is Close, 0–8 is
Foundation. Gaps are listed worst answer first, then in question order.

## Local run and tests
```bash
cd ser_diagnostic
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests/
SER_ALLOW_DRAFT=1 .venv/bin/flask --app "ser_app:create_app()" run   # local SQLite in ./instance
```

## Deploy runbook (Cloud Shell) — NOT YET EXECUTED

Run these in order. Each step says how to check it worked.

**0. Confirm the instance connection name.** The commands below assume `accredo-test:us-central1:accredo-test`.
```bash
gcloud sql instances describe accredo-test --format='value(connectionName,databaseVersion)'
```

**1. Service account, with Cloud SQL Client only**
```bash
gcloud iam service-accounts create ser-diagnostic-runner --display-name="SER diagnostic runtime"
gcloud projects add-iam-policy-binding accredo-test \
  --member="serviceAccount:ser-diagnostic-runner@accredo-test.iam.gserviceaccount.com" \
  --role=roles/cloudsql.client --condition=None --format=none
```
- *Failure:* "already exists" on create is fine. A permission error means your account
  can't manage IAM.

**2. Database and user, created with SQL rather than `gcloud sql users create`**
```bash
gcloud sql connect accredo-test --user=postgres   # then run the SQL below at the psql prompt
```
```sql
CREATE DATABASE ser;
CREATE ROLE ser_app LOGIN PASSWORD '<generate a strong password>';
GRANT CONNECT ON DATABASE ser TO ser_app;
\c ser
GRANT USAGE, CREATE ON SCHEMA public TO ser_app;
-- Isolation: stop ser_app (and any other PUBLIC member) from connecting to Accredo's DB.
-- Find Accredo's database name in the accredo-database-url secret first. Check that Accredo's
-- own user has an explicit CONNECT grant before revoking PUBLIC, or Accredo loses access.
-- GRANT CONNECT ON DATABASE <accredo_db> TO <accredo_user>;
-- REVOKE CONNECT ON DATABASE <accredo_db> FROM PUBLIC;
```
- *Why SQL:* I believe, but have **not verified**, that users made with
  `gcloud sql users create` are added to `cloudsqlsuperuser`. That would weaken the
  separation from Accredo's data.
- *Check:* `psql "host=... dbname=<accredo_db> user=ser_app"` should be refused.

**3. Secret with the database URL**
```bash
printf '%s' 'postgresql+psycopg2://ser_app:<URL-ENCODED-PASSWORD>@/ser?host=/cloudsql/accredo-test:us-central1:accredo-test' \
  | gcloud secrets create ser-database-url --data-file=-
gcloud secrets add-iam-policy-binding ser-database-url \
  --member="serviceAccount:ser-diagnostic-runner@accredo-test.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --format=none
```
- *Failure:* characters like `@`, `/` or `:` in the password must be URL-encoded, or the
  connection fails at startup. `printf '%s'` avoids a trailing newline in the secret.

**4. Build and deploy**
```bash
gcloud builds submit ser_diagnostic/ --tag gcr.io/accredo-test/ser-diagnostic
gcloud run deploy ser-diagnostic --image gcr.io/accredo-test/ser-diagnostic \
  --region us-central1 \
  --service-account ser-diagnostic-runner@accredo-test.iam.gserviceaccount.com \
  --add-cloudsql-instances accredo-test:us-central1:accredo-test \
  --set-secrets DATABASE_URL=ser-database-url:latest \
  --allow-unauthenticated --max-instances 3
```
- *Check:* `curl https://<service-url>/health` should return 200 with
  `"content_status":"DRAFT"`. `GET /` returns 503 until the content is approved. That's
  expected.
- *Failure:* if a revision won't start, the logs show the reason. "DATABASE_URL must be
  set" means the secret isn't mounted. A psycopg2 `OperationalError` means a wrong URL,
  password, or connection name.
- *Reviewing the draft privately:* deploy with `--no-allow-unauthenticated` and
  `--set-env-vars SER_ALLOW_DRAFT=1`, then open it with
  `gcloud run services proxy ser-diagnostic --region us-central1`.

## Open items before going public
1. Owner review of `content.json`: the (A) items checked against the current ACCSC
   Standards, the scoring and bands, the gate treatment of "Partly", the call to action,
   and branding.
2. **Abuse protection:** the honeypot only stops simple bots. Before broad outreach,
   consider reCAPTCHA or Cloud Armor rate limiting. In-app rate limiting doesn't work
   across Cloud Run instances without a shared store.
3. **Consent and privacy notice:** the form collects personal data for outreach. Decide
   the wording, and whether a consent checkbox is needed.
4. **Schema:** uses `db.create_all()`, the same as Accredo. Migrations are already a known gap for both.
