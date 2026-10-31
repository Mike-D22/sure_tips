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
| `fly.toml` | repository root | the selected identity (app `sure-tips-api-mate`, region `iad`), non-secret `[env]` (including the six transport values), release command, HTTPS, health check, no committed VM size |
| Database | `odds/odds/settings.py` | the local, git-ignored SQLite file `odds/db.sqlite3`, the same in development and in the container: no `DATABASE_URL`, no PostgreSQL, no connection options |
| Pinned dependencies | `odds/requirements.txt` | `gunicorn` (the container's WSGI server), Django, and the scraping stack |
| Published content | `odds/alltips_scraper/content/v1/` | the reviewed canonical JSON the versioned endpoint is served from: it ships inside the image and is never written at runtime |
| Deployment contract tests | `odds/alltips_scraper/tests_deployment_v1.py` | pins the decisions above |

Deliberately **not** part of this image or this sprint:

* no `/healthz` alias — the existing `/api/health/` route is the liveness probe;
* no WhiteNoise and no `collectstatic` — the image serves no static files (the
  Django admin would render unstyled; see §10);
* no scheduler, no Celery and no automatic `refresh_tips`: the versioned endpoint
  is served from published content, the snapshot stays out of band and manual, and
  the writer refuses to run (exit status `3`) against the read-only reader this image
  installs;
* no change to the six legacy routes, the versioned route, or their envelopes;
* no publication automation and no content writer at runtime:
  `manage.py validate_v1_content` is a developer/reviewer tool
  (`docs/RUNBOOK.md` section 4.3) that neither the image nor the release invokes,
  and nothing in a deployment writes, refreshes or republishes the shipped content;
* no cache, Redis or Upstash: the versioned endpoint reads the shipped content
  through the seam and needs no shared cache (the legacy `LocMemCache` is unchanged
  and per process).

### 1.1 Initial static-v1 deployment scope (2026-10-10, sprint 1K)

The first deployment is a **read-only static-v1 service**. Its public surface is
`GET /api/health/` plus the versioned read path `GET /api/v1/tips/`, answered from
the reviewed canonical JSON committed under
`odds/alltips_scraper/content/v1/` and shipped inside the image. At runtime that
reader is local, offline and database-free: it opens files in the image and touches
no database, no cache, no socket and no scheduler.

Content reaches the image only through the reviewed loop, and there is no runtime
publication: validate a candidate offline (`manage.py validate_v1_content --root
<candidate>`, `docs/RUNBOOK.md` section 4.3), review it, commit the payload with its
`manifest.json` entry, then build a new image and redeploy. `manage.py refresh_tips`
is not an in-container publisher — it is refused (exit status `3`) against the
read-only provider this image installs — and nothing in this image writes,
refreshes or republishes content.

The tracked manifest is intentionally empty, so a supported type key that no
reviewed publication has named is answered `503` `source_unavailable` **by design**:
that is the published-content answer for an unpublished key, not a runtime fault.

**Deferred, with no architecture selected:** authentication and sessions, the admin,
payments and entitlements, any cache or shared store (Fly volume, managed database,
Redis/Upstash), a scheduler or worker, and any runtime content writer. No persistence
architecture is chosen, `odds/db.sqlite3` stays a local git-ignored file, and the gap
in section 10 stays open.

**The six legacy routes are frozen but outside this guarantee.** They stay routable
and unchanged, they are not part of the static-v1 surface, and they can perform
synchronous upstream work inside a request, so this scope is not a claim that the
whole public service is offline. Their production exposure is a separately deferred
contract and security decision.

**Nothing was built or deployed to record this scope.** It is a decision recorded by
a documentation sprint: no image was built, no container was run, no `fly` command was
executed, and no deployment occurred.

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
| `ALLOWED_HOSTS` | `[env]` in `fly.toml` | committed | Must contain `sure-tips-api-mate.fly.dev`, or every request is `400 DisallowedHost`. |
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
# 1. Create the Fly app this repository already declares. The committed fly.toml
#    carries the whole identity - [app] sure-tips-api-mate, [primary_region] iad
#    and the sure-tips-api-mate.fly.dev entry in ALLOWED_HOSTS - and no Fly app
#    has been created for it yet.
fly apps create sure-tips-api-mate

# 2. The application secret (see §3.1).
fly secrets set SECRET_KEY=$secret

# 3. Deploy. There is no database step: the service runs on its local SQLite
#    file, so no cluster is created and no DATABASE_URL secret is set.
fly deploy
```

The name is not invented at the console: `app` (`sure-tips-api-mate`),
`primary_region` (`iad`) and the `[env] ALLOWED_HOSTS` host
(`sure-tips-api-mate.fly.dev`) are committed together in `fly.toml`, and no Fly app
has been created for that identity yet. If this account cannot create the name,
change all three as one committed edit before the first deploy: an app that does not
exist yet cannot be repaired by editing a local file, and a `fly.toml` naming some
other app deploys this service under that identity instead of this one.

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
4. Waits for the `[[http_service.checks]]` probe of `/api/health/` to answer
   `200`. The probe is not a public request: Fly runs a service check against the
   service on the machine **over the app's private network**, so the check itself
   states the identity and the scheme —
   `headers = { Host = "sure-tips-api-mate.fly.dev", "X-Forwarded-Proto" = "https" }`.
   Without the first header Django sees the machine's private address as `Host`
   and answers `400 DisallowedHost`; without the second the probe arrives as plain
   HTTP, `SECURE_SSL_REDIRECT` answers `301`, and Fly counts anything but `2xx` as
   a failed check (it does not follow redirects). Neither header widens
   `ALLOWED_HOSTS` or exempts a path from the redirect — a plain-HTTP request from
   anywhere else is still redirected and still cannot claim another host. Leave
   both in place; `odds/alltips_scraper/tests_deployment_v1.py`
   (`HealthCheckProbeTests`) fails if either is removed.

Migrations are forward-only: a deploy that migrated and then needed to be undone
requires a new migration.

## 6. Verifying a deployment

```powershell
fly status                 # machines, release, health
fly logs                   # console logging (DJANGO_LOG_LEVEL / ALLTIPS_LOG_LEVEL)
curl.exe https://sure-tips-api-mate.fly.dev/api/health/
# {"status": "ok", "service": "sure-tips-api", "api_version": "legacy"}
```

The versioned route answers from the published content the image ships, not from a
written snapshot, so no write gates it: a deploy serves whatever the reviewed
manifest names, and a type that manifest does not name answers `503`
`source_unavailable` — by design, not a deployment failure. The tracked manifest is
still the empty map (`{"schema_version": 1, "snapshots": {}}`):

```powershell
curl.exe "https://sure-tips-api-mate.fly.dev/api/v1/tips/?type=bet_of_the_day"
```

### 6.1 Manual release verification matrix (2026-10-11, sprint 1N)

These are the only approved post-deploy checks, and they are read-only. The
checklist, the preconditions, the abort conditions and the release record live in
`docs/RUNBOOK.md` section 11.2.

| Read-only check | Expected result | Pinned by |
| --- | --- | --- |
| `fly status` | machine running, new release version, `[[http_service.checks]]` green | this section, `fly.toml` |
| `fly logs` | console logging only; nothing sensitive echoed | this section, `docs/RUNBOOK.md` section 8 |
| `GET https://sure-tips-api-mate.fly.dev/api/health/` | `200`, JSON body exactly `{"status": "ok", "service": "sure-tips-api", "api_version": "legacy"}` | section 6, `odds/alltips_scraper/views.py`, `docs/RUNBOOK.md` section 3, `.github/workflows/image-build-smoke.yml` |
| `GET https://sure-tips-api-mate.fly.dev/api/v1/tips/?type=bet_of_the_day` | `503`, `{"api_version": "v1", "error": {"code": "source_unavailable", "message": "tip data is temporarily unavailable", "field": null}}` — expected, because the shipped manifest is the empty map | section 6, `docs/API_V1_CONTRACT.md` sections 5 and 6, `.github/workflows/image-build-smoke.yml` |
| optional, same route: `?type=not_a_type` | `400` `unknown_tip_type`, `field` `"type"` | `docs/API_V1_CONTRACT.md` section 5, `.github/workflows/image-build-smoke.yml` |
| optional, same route: no `type` parameter | `400` `missing_tip_type`, `field` `"type"` | `docs/API_V1_CONTRACT.md` section 5, `.github/workflows/image-build-smoke.yml` |

`GET /api/health/` plus the versioned read path are the whole static-v1 public
surface (`docs/RUNBOOK.md` section 11.1). Do not call the six legacy routes in
section 4 — frozen and routable, outside the static-v1 guarantee, and able to do
synchronous upstream work inside a request; do not touch `/admin/` while no
persistence architecture is chosen (section 10); and do not issue a write method,
`manage.py refresh_tips`, `manage.py migrate`, or any environment or secret dump.

A release moves through three states and is verified only at the last one:
*accepted* (`fly deploy` exited 0 and `fly status` shows a new release version),
*healthy* (the `/api/health/` checks pass and the health request answers as pinned
above), and *verified* (healthy, plus every approved check above). An accepted
deploy, and a single green CI run, are each not a verified release.

## 7. Rollback

```powershell
fly releases                                        # find the previous good version
fly deploy --image registry.fly.io/sure-tips-api-mate:<previous-version>
```

A rollback does not undo migrations. Check `fly releases` and the logs before
assuming the previous version still matches the database schema. The abort
conditions, the stop rules and the release record for a manual release attempt are
in `docs/RUNBOOK.md` section 11.2.

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

### 8.1 Automated image build and smoke check (2026-10-10, sprint 1L)

The repository now has a GitHub Actions workflow at
`.github/workflows/image-build-smoke.yml`, named
**"Image build and local smoke check"**. It is a test-only build-and-smoke job: it
builds the image from the commit under test on a GitHub-hosted runner and exercises
the approved local HTTP surface. It never deploys.

It runs on pushes to `main`, on pull requests targeting `main`, and on manual
workflow dispatch.

What it does, and what it deliberately does not do:

* builds the Docker image **locally on a GitHub-hosted runner**;
* runs a **single container bound to loopback only**, under a run-unique tag and
  container name, and removes those run-scoped Docker resources afterwards;
* checks `GET /api/health/`;
* checks only the approved static-v1 error-path contracts of `GET /api/v1/tips/`
  for the empty shipped manifest (§1.1) — a supported type key answers `503`
  `source_unavailable` — and requests no legacy scrape route;
* verifies the shipped manifest's pinned integrity values inside the image;
* does **not** deploy to Fly;
* does **not** publish an image or log into an image registry;
* uses **no repository, organization, environment or cloud secrets**;
* does **not** run the Django test suite: it is the image build plus its narrow
  local smoke gate, nothing more;
* is **not** a replacement for production deployment verification (§6).

First recorded evidence: workflow run #1 completed successfully on `main` at
commit `7f71870` ("Add Docker image smoke workflow") on 2026-10-10, with an
observed duration of 28 seconds. A single green run is a first data point: it does
not show that the workflow is reliable for every future change, that every
dependency is secure, that the service is production-ready, or that a Fly
deployment succeeded.

### 8.2 Django regression tests in CI (2026-10-11, sprint 1M)

The Django suite is covered by the separate `.github/workflows/django-tests.yml`
workflow, named **"Django regression tests"**: it runs `manage.py check` and the
`alltips_scraper` suite on a GitHub-hosted runner against the pinned
dependencies, and `docs/RUNBOOK.md` section 7.2 records it. Like the workflow in
§8.1 it is a test-only gate that deploys nothing.

Both gates must be green on a `main` commit — the Django regression tests and
the image build / local smoke check — before treating that commit as eligible for
the next deployment step. A green gate is a successful CI test or image result,
not deployment verification (§6): it does not show that a real production or Fly
deployment succeeded. Neither gate deploys anything.

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

* **The image build is run in CI, not from this checkout.** `docker` was not
  installed in the sprint environment, so `docker build`, `docker run` and the
  local liveness curl in §8 were not executed here. Since sprint 1L the Dockerfile
  is built and smoke-checked by the workflow in §8.1, whose first recorded build
  and smoke run is workflow run #1; `fly.toml`, the Dockerfile and the pins stay
  covered by `manage.py check`, `check --deploy` and `tests_deployment_v1.py`.
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
* **No publication automation and no validator in the image.** The review-time
  validator (`manage.py validate_v1_content`, `docs/RUNBOOK.md` section 4.3) is a
  developer/reviewer tool: the release command, gunicorn and the health check never
  invoke it, and neither it nor anything else writes, refreshes or republishes the
  shipped content. No cache, Redis, Upstash or publication secret was added, and
  preparing a candidate, computing its digest and committing it remain manual and
  reviewed. The mechanical half of that preparation - the record file, the canonical
  digest and the manifest entry - is written by `manage.py prepare_v1_candidate`
  (`docs/RUNBOOK.md` section 4.4), which is a reviewer tool for the same reasons: it
  is not installed as a service, nothing in a build, a release or a request invokes
  it, it reaches no network, and it publishes nothing until a reviewer commits the
  prepared files and redeploys.
* **Forward-only migrations.** The release command migrates; there is no
  rollback beyond a new forward migration.
* **A committed identity, an uncreated app.** `fly.toml` names `app`
  `sure-tips-api-mate`, `primary_region` `iad` and the `sure-tips-api-mate.fly.dev`
  host in `[env] ALLOWED_HOSTS`, but **no Fly app has been created** from it: no
  `fly apps create`, no secret and no deploy has run, so the identity is a decision
  rather than an observation. No `[[vm]]` block is committed either — the machine
  size stays a deploy-time choice — so the first deploy still starts with
  `fly apps create`.
* **No managed database.** The service runs on the git-ignored SQLite file
  `odds/db.sqlite3`, which lives in the machine's own filesystem. Nothing is
  configured to persist it across a deploy, so a row written by
  `manage.py refresh_tips` does not survive a redeploy. The deployed reader is the
  read-only published-content reader, so the endpoint is not served from that table
  in any case, and a refresh is refused there (exit status `3`). A volume or a
  managed database is future work; this sprint only removed the old PostgreSQL plan.
* **A release-machine migration does not supply a serving-machine database.**
  The image excludes `odds/db.sqlite3`, and no volume or managed database shares
  state between machines. The configured `release_command` runs `manage.py
  migrate` on the one-off release machine's own ephemeral filesystem; when that
  machine is discarded, any SQLite schema and data created there are not available
  to a separate serving machine. The published-content reader does not need a
  database — `/api/health/` and `GET /api/v1/tips/` read no database state — but
  `/admin/` and any session- or auth-dependent path are not deployment-ready
  without an explicit persistence decision. This conclusion is configuration
  review, not a completed container or Fly verification. **Deferred without a
  decision:** use a persistent volume, use a managed database, or remove the
  release-command/admin dependency in the initial deployment scope. **Sprint 1K keeps
  this deferred** (see §1.1): the initial static-v1 scope makes no persistence
  decision, and `/admin/` and session- or auth-dependent paths stay outside it.
  The image gives the runtime account ownership of `/app/odds`, so the release
  command can create its ephemeral `db.sqlite3` there.
* **No `CSRF_TRUSTED_ORIGINS`.** The API is read-only public `GET`s, so no
  browser POST surface needs it yet. A future form or dashboard on the deployed
  origin will.
* **No connection reuse and no connection options.** Every request opens its own
  SQLite connection, which is Django's default here: `CONN_MAX_AGE`,
  `DISABLE_SERVER_SIDE_CURSORS` and `OPTIONS['prepare_threshold']` are not set,
  because they were the old pooled-PostgreSQL path and are gone with it
  (`odds/odds/settings.py`, "Database"). Pooler-aware tuning is backlog, not this
  sprint.
