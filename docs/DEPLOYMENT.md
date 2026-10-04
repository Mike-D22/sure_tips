# Deployment — Fly.io (`odds/`, sprint 1E-A1)

Date: 2026-10-02

How to build `odds/` (the OddMate tips API) as a container and how to put it on
[Fly.io](https://fly.io). This document is the *procedure*; the configuration it
describes is `Dockerfile`, `.dockerignore` and `fly.toml` at the repository root,
and `odds/alltips_scraper/tests_deployment_v1.py` is the test module that keeps
those files honest.

> **Status: not deployed.** Sprint 1E-A1 is a configuration-only sprint. No `fly`
> command was run against a real app, no image was published, and the
> `Dockerfile` was not built during the sprint (no `docker` CLI in that
> environment). Everything was verified with `manage.py check`,
> `manage.py check --deploy`, the Django test suite and a static review of the
> shipped files. Treat the first `fly deploy` as the moment the image itself is
> first exercised.
>
> **Database scope (2026-10-03):** the Fly Managed PostgreSQL design originally
> planned here — `fly postgres create` / `attach` and a `DATABASE_URL` secret —
> was removed. `odds/odds/settings.py` reads no `DATABASE_URL` and the service
> runs on its local SQLite file in every environment, so there is no database
> step in §4 and no database secret in §3. See `docs/AUDIT.md` §8.

## 1. What ships

| Piece | Location | Purpose |
| --- | --- | --- |
| `Dockerfile` | repository root | `python:3.12-slim`, gunicorn, unprivileged `appuser` (uid 10001) |
| `.dockerignore` | repository root | keeps `odds/.env`, `odds/db.sqlite3`, venvs and bytecode out of the build context |
| `fly.toml` | repository root | placeholder app/region, non-secret `[env]` (including the six transport values), release command, HTTPS, health check, no committed VM size |
| Database | `odds/odds/settings.py` | the local, git-ignored SQLite file `odds/db.sqlite3`, the same in development and in the container: no `DATABASE_URL`, no PostgreSQL, no connection options |
| Pinned dependencies | `odds/requirements.txt` | `gunicorn` (the container's WSGI server), Django, and the scraping stack |
| Published content | `odds/alltips_scraper/content/v1/` | the reviewed canonical JSON the versioned endpoint is served from: it ships inside the image and is never written at runtime |
| Deployment contract tests | `odds/alltips_scraper/tests_deployment_v1.py` | pins the decisions above |

Deliberately **not** part of this image or this sprint:

* no `/healthz` alias — the existing `/api/health/` route is the liveness probe;
* no WhiteNoise and no `collectstatic` — the image serves no static files (the
  Django admin would render unstyled; see §10);
* no scheduler, no Celery, no automatic `refresh_tips` — the snapshot is
  refreshed out of band only;
* no scheduler, no Celery and no automatic `refresh_tips`: the versioned endpoint
  is served from published content, and the writer refuses to run (exit status `3`)
  against the read-only reader this image installs;
* no change to the six legacy routes, the versioned route, or their envelopes.

## 2. Prerequisites

* `flyctl` — <https://fly.io/docs/flyctl/install/> (`fly version`).
* Docker, only to build the image locally; Fly builds remotely by default.
* A checkout of this repository on `main`, with the **repository root** as the
  working directory (the directory that holds `odds/`, `Dockerfile`, `fly.toml`).
* The repository-root `.venv` with `odds/requirements.txt` installed, to run the
  local checks in `docs/RUNBOOK.md` §2.

## 3. Secrets and non-secrets

| Name | Kind | Set with | Notes |
| --- | --- | --- | --- |
| `SECRET_KEY` | **secret** | `fly secrets set SECRET_KEY=…` | Generate a fresh one; never reuse a development key. |
| `DEFAULT_FROM_EMAIL` | `[env]` in `fly.toml` | committed | Not a secret. |
| `SCRAPE_URL` | `[env]` in `fly.toml` | committed | Upstream base URL, not a credential. |
| `ALLOWED_HOSTS` | `[env]` in `fly.toml` | committed | Must contain `<app>.fly.dev`, or every request is `400 DisallowedHost`. |
| `PORT` | `[env]` in `fly.toml` | committed | Must match `[http_service].internal_port`. |
| `SECURE_SSL_REDIRECT` | `[env]` in `fly.toml` | committed | Sends plain-HTTP requests to HTTPS; `force_https` makes the edge do it too. |
| `SESSION_COOKIE_SECURE` | `[env]` in `fly.toml` | committed | The session cookie travels over HTTPS only. |
| `CSRF_COOKIE_SECURE` | `[env]` in `fly.toml` | committed | The CSRF cookie travels over HTTPS only. |
| `SECURE_HSTS_SECONDS` | `[env]` in `fly.toml` | committed | `31536000` — one year of HSTS, the highest value still worth stating. |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `[env]` in `fly.toml` | committed | Extends HSTS to subdomains. |
| `SECURE_HSTS_PRELOAD` | `[env]` in `fly.toml` | committed | Adds `preload` to the HSTS header; harmless without a browser submission. |
| `DEBUG` | never set | — | Defaults to `False`; setting it would serve a debug-mode API. |
| `CORS_ALLOW_ALL_ORIGINS` | never set | — | Defaults to `False`; the wildcard is a local-development opt-in only. |

`odds/.env` is for local development. It is git-ignored **and** excluded by
`.dockerignore`: never commit it, never copy it into an image, and never paste its
values into a tracked file or a log.

The six transport values are `[env]` rather than secrets: they state a transport
policy, they carry no credential, and being readable is the point. They are also
independent — `odds/odds/settings.py` reads each one on its own and derives none
of them from `DEBUG`, so leaving them unset (which is what `odds/.env.example`
does) keeps plain HTTP working, while a deployment states all six. The only
transport setting that is not an environment value is `SECURE_PROXY_SSL_HEADER`:
it is unconditional, and it is correct only because the container is never
reachable except through Fly's TLS terminator.

### 3.1 Setting the secret key

```powershell
$secret = & .\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
fly secrets set SECRET_KEY=$secret
fly secrets list            # names and digests only; values are never shown
```

## 4. First-time setup

Run every command from the repository root.

```powershell
# 1. Create the Fly app. Pick the real name now: the committed fly.toml is a
#    template, so this name goes into [app], [primary_region] and the
#    <app>.fly.dev entry in ALLOWED_HOSTS before the first deploy.
fly apps create <app-name>

# 2. The application secret (see §3.1).
fly secrets set SECRET_KEY=$secret

# 3. Deploy. There is no database step: the service runs on its local SQLite
#    file, so no cluster is created and no DATABASE_URL secret is set.
fly deploy
```

Replace `<app-name>` with the real name, and replace the
placeholders inside `fly.toml` (`app`, `primary_region`, `[env] ALLOWED_HOSTS`)
before the first deploy. `fly deploy` against an unfilled template fails fast,
which is intended: this repository does not get to guess the identity of an app it
does not own.

## 5. What `fly deploy` does

1. Builds the image from `Dockerfile` in the repository root, filtered by
   `.dockerignore`.
2. Runs `release_command` — `python manage.py migrate --noinput`, from
   `fly.toml` — in a one-off machine built from that image. It is the only
   release action there is, and a non-zero exit aborts the release, leaving the
   previous version serving traffic.
3. Rolls out machines that listen on `internal_port` 8080 as `appuser`, behind
   Fly's TLS terminator (`force_https = true`). `auto_stop_machines = "off"` keeps
   a machine running: a cold start would otherwise add its boot time to the first
   scrape, which already runs synchronously past gunicorn's default timeout.
4. Waits for the `[[http_service.checks]]` probe of `/api/health/`.

Migrations are forward-only: a deploy that migrated and then needed to be undone
requires a new migration.

## 6. Verifying a deployment

```powershell
fly status                 # machines, release, health
fly logs                   # console logging (DJANGO_LOG_LEVEL / ALLTIPS_LOG_LEVEL)
curl.exe https://<app>.fly.dev/api/health/
# {"status": "ok", "service": "sure-tips-api", "api_version": "legacy"}
```

The versioned route answers from the stored snapshot and returns `503` until a
snapshot has been written — that is by design, not a deployment failure:

```powershell
curl.exe "https://<app>.fly.dev/api/v1/tips/?type=bet_of_the_day"
```

## 7. Rollback

```powershell
fly releases                                        # find the previous good version
fly deploy --image registry.fly.io/<app>:<previous-version>
```

A rollback does not undo migrations. Check `fly releases` and the logs before
assuming the previous version still matches the database schema.

## 8. Building and running the image locally

Docker is not needed for local development — `manage.py runserver` remains the
development server (`docs/RUNBOOK.md` §3). These commands reproduce what Fly
builds:

```powershell
docker build -t sure-tips-api:local .
docker run --rm -p 8080:8080 --env-file odds/.env -e DEBUG=False -e PORT=8080 sure-tips-api:local
curl.exe http://127.0.0.1:8080/api/health/
```

Image-safety checks, one claim each:

```powershell
# the image holds no local environment file, database or virtual environment
docker run --rm --entrypoint sh sure-tips-api:local -c "ls -a /app/odds"
# the container does not run as root
docker run --rm --entrypoint sh sure-tips-api:local -c "id -u"          # 10001
# the WSGI target imports exactly as the CMD names it
docker run --rm --entrypoint sh sure-tips-api:local -c "cd /app/odds && gunicorn odds.wsgi:application --check-config"
```

`--env-file odds/.env` gives the container the same local configuration as the
development server. Nothing else has to be passed: the container uses the same
SQLite file, so there is no database endpoint to point it at.

## 9. Operating the service

```powershell
fly logs                                                # follow the console log
fly ssh console -C "python manage.py check --deploy"     # deployed configuration check
fly ssh console -C "python manage.py migrate --noinput"  # only if a release was skipped
```

Refreshing the durable snapshot is out of band and manual, and it needs the durable
provider to be installed: `manage.py refresh_tips` refuses the whole run (exit
status `3`) against any other one, which is the reader this image installs.
Publishing a type on a deployment is therefore a reviewed change to
`odds/alltips_scraper/content/v1/` rather than a refresh:

```powershell
fly ssh console -C "python manage.py refresh_tips"   # refused: the deployed reader is read-only
```

Nothing schedules it: the versioned endpoint answers `503` for a type the shipped
manifest does not name.

## 10. Known gaps and deferrals

* **The image has never been built here.** `docker` was not installed in the
  sprint environment, so `docker build`, `docker run` and the local liveness curl
  were not executed; `fly.toml`, the Dockerfile and the pins are covered by
  `manage.py check`, `check --deploy` and `tests_deployment_v1.py` instead. Add a
  build-and-smoke-test job before the first production deploy.
* **No static files.** No WhiteNoise, no `collectstatic`: `/admin/` would load
  unstyled in this image. Serving the admin's assets is a separate, deliberate
  change.
* **No `/healthz`.** `/api/health/` is the liveness route — Fly check, container
  check and runbook all use it — and a second alias was explicitly not added.
* **`LocMemCache` is per process.** With two gunicorn workers each process warms
  its own legacy cache with its own date-scoped keys. Correct but not shared; a
  Redis backend stays backlog.
* **No refresh scheduling.** `refresh_tips` remains out of band and manual: no
  `release_command`, process group or cron in this sprint starts it.
* **Forward-only migrations.** The release command migrates; there is no
  rollback beyond a new forward migration.
* **A template `fly.toml`.** `app` (committed as
  `<staging-or-production-app-name>`), `primary_region` (`<fly-region>`) and the
  `<app>.fly.dev` host in `[env] ALLOWED_HOSTS` are placeholders, and no `[[vm]]`
  block is committed: the app's identity and machine size are decided when the app
  is created, so the first deploy starts with `fly apps create` and a short edit.
* **No managed database.** The service runs on the git-ignored SQLite file
  `odds/db.sqlite3`, which lives in the machine's own filesystem. Nothing is
  configured to persist it across a deploy, so a row written by
  `manage.py refresh_tips` does not survive a redeploy. The deployed reader is the
  read-only published-content reader, so the endpoint is not served from that table
  in any case, and a refresh is refused there (exit status `3`). A volume or a
  managed database is future work; this sprint only removed the old PostgreSQL plan.
* **No `CSRF_TRUSTED_ORIGINS`.** The API is read-only public `GET`s, so no
  browser POST surface needs it yet. A future form or dashboard on the deployed
  origin will.
* **No connection reuse.** `CONN_MAX_AGE = 0` means every request opens its own
  database connection, and disabling server-side cursors and prepared statements
  costs a little setup each time. That is the price of being safe against a
  pooler endpoint; pooler-aware tuning is backlog, not this sprint.
