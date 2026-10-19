FROM python:3.11-slim

RUN groupadd -r ser && useradd -r -g ser ser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY content.json wsgi.py ./
COPY ser_app ./ser_app

USER ser

ENV PORT=8080
EXPOSE 8080

CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 60 "wsgi:app"
