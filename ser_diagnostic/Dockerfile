# Build from this folder: gcloud builds submit ser_diagnostic/ --tag gcr.io/accredo-test/ser-diagnostic
FROM python:3.11-slim

RUN groupadd -r serdiag && useradd -r -g serdiag serdiag

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ser_app/ ./ser_app/
COPY content.json .

# Code stays root-owned and read-only; only instance/ (local SQLite fallback) is writable.
RUN chown -R root:root /app && chmod -R a+rX /app \
    && mkdir -p /app/instance && chown serdiag:serdiag /app/instance

USER serdiag

ENV PORT=8080
EXPOSE 8080

CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 60 "ser_app:create_app()"
