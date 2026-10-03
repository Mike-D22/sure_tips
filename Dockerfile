# ---------------------------------------------------------------------------
# sure_tips / odds - production image for the OddMate tips API.
#
# Build context is the repository root (the directory that holds odds/ and this
# file). Inside the image the Django project lives at /app/odds, which is also
# the working directory, because the WSGI module is "odds.wsgi" and the app
# package is "alltips_scraper" - both are importable only from there.
#
#   docker build -t sure-tips-api:local .
#   docker run --rm -p 8080:8080 --env-file odds/.env -e DEBUG=False sure-tips-api:local
#
# The container runs gunicorn, never `manage.py runserver`, and it never runs
# migrations: the database is migrated by Fly's release_command before a release
# takes traffic. See docs/DEPLOYMENT.md.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# No .pyc files in the image, and unbuffered output so `fly logs` is not
# delayed. PORT is Fly's contract with [http_service].internal_port in fly.toml.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# Dependencies first: this layer is cached until odds/requirements.txt changes.
# psycopg[binary] ships manylinux wheels, so no compiler is needed in the image.
COPY odds/requirements.txt ./odds/requirements.txt
RUN pip install --no-cache-dir --requirement ./odds/requirements.txt

# Unprivileged runtime account (uid 10001). It owns the copied tree so the
# git-ignored SQLite fallback stays writable if DATABASE_URL is ever unset.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

# Only the Django service directory is copied. odds/.env, odds/db.sqlite3 and
# every virtual environment are excluded by .dockerignore; secrets are injected
# at run time by the platform, never baked into a layer.
COPY --chown=appuser:appuser odds/ ./odds/

WORKDIR /app/odds

USER appuser

EXPOSE 8080

# Gunicorn only. The bind port is Fly's $PORT; --timeout is raised well above
# gunicorn's 30 s default because the legacy /api/* routes fetch the upstream
# source synchronously inside the request.
CMD ["sh", "-c", "exec gunicorn odds.wsgi:application --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile -"]
