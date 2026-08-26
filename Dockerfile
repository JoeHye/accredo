FROM python:3.11-slim

RUN groupadd -r accredo && useradd -r -g accredo accredo

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/instance /app/instance/reports /app/instance/exhibits \
    && chown -R accredo:accredo /app

USER accredo

ENV PORT=8080
EXPOSE 8080

CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 0 "app:create_app()"
