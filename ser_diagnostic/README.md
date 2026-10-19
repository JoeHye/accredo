# SER Readiness Diagnostic

A public 10-question lead-magnet app for ACCSC initial applicants. It lives in the
Accredo repo but is a separate deployable: its own Cloud Run service
(`ser-diagnostic`), its own runtime service account (`ser-diagnostic-runner`), and its
own database and database user on the existing Cloud SQL instance.

## Editing the questionnaire
All wording, answer points, band thresholds, the gate rule, and the CTA live in
`content.json`. Changing them needs a rebuild and redeploy but **no code changes**.
The app validates `content.json` at startup and refuses to start if it is malformed.

- Score = sum of answer points / max points. Bands: Green >= 80%, Yellow >= 50%, Red < 50%.
- Questions with `"gate": true` force the "Not yet eligible" (Red) result when answered "No".
- Each saved lead records `content_version`. Bump `version` when you change questions.

## Data
Table `ser_leads`: name, email, school, consent, answers (JSON), points, max_points,
percent, band, gated, content_version, pilot_requested, pilot_requested_at, created_at.
No emails are sent. Leads are read directly from Cloud SQL.

## Local development
```bash
cd ser_diagnostic
pip install -r requirements-dev.txt
python -m pytest tests          # uses SQLite in a temp dir
flask --app wsgi run            # http://127.0.0.1:5000, local SQLite in instance/
```

## Runtime configuration
| Env var | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes on Cloud Run | From Secret Manager `ser-database-url`. The app refuses to start on Cloud Run without it. |
| `SER_CONTENT_PATH` | No | Defaults to the bundled `content.json`. |

## Build (from the repo root)
```bash
gcloud builds submit ser_diagnostic --tag gcr.io/accredo-test/ser-diagnostic:<commit>
```
