FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# postgresql-client: pg_isready (startup wait) and pg_dump / pg_restore (backups)
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /app/staticfiles /data/media \
 && DEBUG=False SECRET_KEY=build-time-only-collectstatic-0123456789abcdef python manage.py collectstatic --noinput \
 && chown -R app:app /app /data
USER app

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
