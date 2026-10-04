# Runbook — `odds/` (sure_tips backend)

How to set up, run, verify, and operate the legacy scraper API.
All commands below are run from the repository root — the directory that
contains `odds/` and the virtual environment `.venv/` (for example
`D:\Dev\projects\sure_tips` on this checkout). The Django project lives in
`odds/`.

> **Product note:** this service is the backend for **OddMate**
> (**ODDMATE TIPS**). The repo name `sure_tips` and the `/api/health/` service
> identifier `sure-tips-api` are internal technical identifiers, not
> user-facing brands. See `docs/PRODUCT_DECISIONS.md`.

## 1. Prerequisites

* Windows with PowerShell.
* [`uv`](https://docs.astral.sh/uv/) (used only to provision Python and install
  dependencies). Verified with `uv 0.11.19`.
* **Python 3.12** — the project's committed bytecode was built with CPython 3.12
  and that is the interpreter `.venv` is created with.

## 2. First-time setup

```powershell
# 1. Provision CPython 3.12 (idempotent: reports "already installed" if present)
uv python install 3.12

# 2. Create the virtual environment at the repository root
uv venv --python 3.12 .venv

# 3. Install pinned dependencies into it
uv pip install --python .\.venv\Scripts\python.exe -r odds\requirements.txt

# 4. Create your local environment file
Copy-Item odds\.env.example odds\.env
#    then edit odds\.env and set SECRET_KEY (+ SMTP values if you need email)
```

The repository-root `.venv/` and `odds/.env` are both git-ignored; never commit
either.

The same file also pins the container-only dependency (`gunicorn`). `gunicorn`
cannot even be imported on Windows — it needs the POSIX-only `fcntl` module —
which is expected: local development runs the Django development server
(section 3) while the container runs gunicorn. There is no database driver in
the list: the service runs on the SQLite support that ships with Python.

To confirm the environment (from the repository root):

```powershell
.\.venv\Scripts\python.exe -c "import django, corsheaders; print(django.get_version())"
```

## 3. Running the service

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py runserver 127.0.0.1:8000
```

Liveness check (no scraping, no database, no credentials):

```powershell
curl.exe http://127.0.0.1:8000/api/health/
# {"status": "ok", "service": "sure-tips-api", "api_version": "legacy"}
```

## 4. API endpoints (legacy contract — do not change the shapes)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health/` | liveness probe (new in Sprint 0.5) |
| GET | `/api/bet-of-the-day/` | Bet of the Day |
| GET | `/api/daily-accumulator/` | Daily Accumulator |
| GET | `/api/btts-win-accumulator/` | BTTS & Win Accumulator |
| GET | `/api/over-25-goals-accumulator/` | Over 2.5 Goals Accumulator |
| GET | `/api/BTTS/` | Both Teams to Score (note the mixed-case path — intentional) |
| GET | `/api/goalscorer/` | Anytime Goalscorer |

All are public, unauthenticated `GET`s. Each response contains a `cached`
boolean: `false` on a fresh scrape (upstream call happened), `true` when served
from the on-process cache. **Apart from that flag, a cached response is identical
to a fresh one.** Error responses are never cached and always contain an `error`
key.

### 4.1 Versioned endpoint (`/api/v1/tips/`)

`GET /api/v1/tips/` is **not** in the table above and is not part of the legacy
contract: it has no `cached` flag, it never scrapes, and it answers from the
snapshot this process installed at startup, which is the read-only published
content under `odds/alltips_scraper/content/v1/` that the image ships. The request
path only ever reads, so a server whose content names no usable record returns
`503` by design instead of scraping. Its contract is `docs/API_V1_CONTRACT.md`;
section 4.2 covers the out-of-band writer, which is refused unless the durable
provider is installed.

### 4.2 Populating the durable snapshot (`manage.py refresh_tips`)

The one supported writer is the out-of-band management command `refresh_tips`.
Nothing in the request path imports it, so no request can start a fetch, a scrape,
a refresh or a cache fill.

It writes the durable store, so it runs only while the installed provider is
`storage_v1.DatabaseSnapshotProvider`. Against any other one - the published-content
reader a deployment starts with, the seam's own in-memory default, or an object that
merely has the seam's three methods - it refuses the whole run before it resolves a
type: one line, `refresh refused: the installed snapshot provider is not writable
durable storage`, on stderr, and exit status `3`. Such a run fetches nothing, reads
nothing, writes nothing and logs nothing.

The snapshot table has to exist first:

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py migrate       # creates the snapshot table
.\.venv\Scripts\python.exe .\odds\manage.py refresh_tips  # fetches and stores the types
```

Without an option, every type the versioned registry publishes is refreshed, in
the registry's own order. `--type TIP_TYPE` refreshes only that type and may be
repeated to refresh several; the selection is de-duplicated and refreshed in the
registry's own order rather than the order the options were given, and a value the
registry does not publish is refused before anything is fetched, with exit status
`2` and without printing a report line.

One line is printed per type, in the order the types were refreshed:

```text
type=<key> outcome=<ok|empty> state=<new|unchanged|changed> count=<n> [legs=<n>] fetched_at=<UTC instant>
type=<key> outcome=failed reason=<token>
```

* `outcome=<ok|empty>`: `ok` is a payload that describes tips, and `empty` is the
  pinned "no cards" answer its own source key produces, which is published as a
  result: the endpoint answers that type with `200` and no tips. Any other payload
  that describes no tips is refused instead.
* `count` is the number of entries the payload lists (`matches` for a `match` unit,
  `accumulators` for a `card` unit), and `legs` is the total the cards state for
  their own legs. `legs=` is printed for the `card` unit only, because a `match`
  unit states no legs at all.
* `state` compares the fetched payload with the one already stored: `new` when
  nothing was stored for the type, `unchanged` when the two digests match, and
  `changed` when they differ. An unchanged payload is still stored again with the
  fresh instant, so a run always publishes the fetch that just happened.
* Each type is stamped with its own instant, taken when its own fetch was accepted,
  so a row records when that answer was read rather than when the run started. A
  refusal carries no instant at all.
* `--dry-run` runs every selected type through the whole pipeline short of the
  write: each is fetched, serialised, digested and compared, and the state it would
  have written is printed with `dry-run:` in front of it. No row is written or
  replaced, so a dry run is the safe way to ask what a refresh would do.
* A refused type prints its key, `outcome=failed` and one fixed reason token, and
  nothing else: no state, no counts, no instant. The same refusal is logged once
  through `alltips_scraper.refresh_v1`.
* Types are refreshed one at a time, so a refusal affects only its own type: every
  type that succeeded stays stored.
* The exit status is `0` when every selected type was accepted (`ok`, or `empty` for
  a pinned empty payload; a dry run accepts without writing), `1` when any type was
  refused, `2` when `--type` named a type the registry does not publish, and `3` when
  the installed provider is not the durable one. The `3` is decided before `--type`
  is resolved, so a run against a provider that cannot store is refused as one
  whatever the selection says, and it is how a scheduler or a cron job learns the
  outcome.

## 5. Caching semantics

This section covers the legacy routes in section 4 only: the versioned route
(section 4.1) reads its stored snapshot and uses no cache backend, no cache key and
no TTL.

* Decorator: `alltips_scraper.decorators.cache_matches`.
* Key: `legacy_api:v1:<view_name>:<YYYY-MM-DD>[:<query fingerprint>]`.
* TTL: 14,400 s (4 h), unchanged from the original implementation.
* Backend: Django's default `LocMemCache` (per process — restarting the server or
  running extra workers gives each process its own cache).
* The key includes the date resolved in `TIME_ZONE` (`UTC`), so yesterday's tips
  can never be served today and no manual flush is needed at midnight. This is an
  internal cache key, **not** a user-facing date boundary: client-facing date
  filtering must pass an explicit user timezone
  (`docs/DATA_CONTRACT.md` §13.7–13.8).
* To clear the cache during development:

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py shell -c "from django.core.cache import cache; cache.clear()"
```

## 6. Environment variables

Everything is read from `odds/.env` via `python-decouple`; see
`odds/.env.example` for the annotated list. The essentials:

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `SECRET_KEY` | yes | — | no default; generate a fresh one |
| `DEBUG` | no | `False` | `True` only in a local `.env` |
| `ALLOWED_HOSTS` | no | `localhost,127.0.0.1,[::1]` | CSV; no wildcard default |
| `CORS_ALLOW_ALL_ORIGINS` | no | `False` | dev-only opt-in |
| `CORS_ALLOWED_ORIGINS` | no | `http://localhost:8000,http://127.0.0.1:8000` | CSV; ignored while the wildcard is on |
| `DEFAULT_FROM_EMAIL` | yes | — | no default |
| `SCRAPE_URL` | yes | — | upstream base URL, read at import time |
| `FREESUPERTIPS_ENABLE_VERIFICATION` | no | `False` | settlement is not implemented; keep off |
| `DJANGO_LOG_LEVEL` / `ALLTIPS_LOG_LEVEL` | no | `INFO` | console logging levels |
| `SECURE_SSL_REDIRECT` | no | `False` | transport: redirect plain HTTP to HTTPS |
| `SESSION_COOKIE_SECURE` | no | `False` | transport: secure-only session cookie |
| `CSRF_COOKIE_SECURE` | no | `False` | transport: secure-only CSRF cookie |
| `SECURE_HSTS_SECONDS` | no | `0` | transport: HSTS lifetime in seconds (`0` sends no header) |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | no | `False` | transport: HSTS `includeSubDomains` |
| `SECURE_HSTS_PRELOAD` | no | `False` | transport: HSTS `preload` header value |

There is no `DATABASE_URL` and no database variable of any kind: `settings.py`
reads no connection string, and every environment uses the git-ignored SQLite
file `odds/db.sqlite3`.

The six transport values are explicit and independent: `settings.py` reads each
one on its own and derives none of them from `DEBUG`, so the defaults above are
exactly what an unset environment gets and plain HTTP keeps working locally. A
deployment sets all six in `fly.toml` `[env]` — they describe the transport
policy and carry no credential, so they are **not** secrets.
`SECURE_PROXY_SSL_HEADER` is the one transport setting that is not an environment
value: it is unconditional, and it is only sound because the container is never
exposed except behind Fly's TLS proxy. `docs/DEPLOYMENT.md` §3 lists the whole
set.

Generate a secret key with:

```powershell
.\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

## 7. Verification commands

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py check                              # expect: no issues
.\.venv\Scripts\python.exe .\odds\manage.py makemigrations --check --dry-run   # expect: No changes detected
.\.venv\Scripts\python.exe .\odds\manage.py test alltips_scraper -v 2 --noinput # expect: 515 tests, all passing
.\.venv\Scripts\python.exe .\odds\manage.py test alltips_scraper.tests_deployment_v1 --noinput  # the 15 deployment tests

# The deployment configuration check. A deployed process never sets DEBUG, and it
# does set the six transport values, so the check runs with them too.
$env:DEBUG='False'
$env:SECURE_SSL_REDIRECT='True'; $env:SESSION_COOKIE_SECURE='True'; $env:CSRF_COOKIE_SECURE='True'
$env:SECURE_HSTS_SECONDS='31536000'; $env:SECURE_HSTS_INCLUDE_SUBDOMAINS='True'; $env:SECURE_HSTS_PRELOAD='True'
.\.venv\Scripts\python.exe .\odds\manage.py check --deploy  # expect: no issues
```

The last four lines are the deployment configuration check. They run with
`DEBUG=False` and the six transport values set, which is exactly what a
deployment sets (`fly.toml` `[env]`). Left at their local defaults the same check
reports four warnings — `security.W004` (HSTS), `W008` (`SECURE_SSL_REDIRECT`),
`W012` (session cookie) and `W016` (CSRF cookie) — because those are values a
deployment is expected to state, not values `settings.py` should assume on its
behalf. Section 11 and `docs/DEPLOYMENT.md` describe what they protect.

The app label is required. `odds/` is not a Python package, so a bare
`manage.py test` runs Django's discovery against the repository root, finds
**0 tests** and prints `NO TESTS RAN`.

The suite runs **entirely offline**: `tests.py` mocks every scraper handler;
`tests_parser_contract.py` calls the real parser functions against the static,
synthetic HTML in `odds/alltips_scraper/fixtures/` (read with `Path.read_bytes`);
and `tests_offline_guard.py` disables the socket layer for every test and proves
the fetch path fails closed with an error envelope instead of raising.

The versioned surface is offline too: `tests_api_v1.py` reads through a provider
double it installs itself, `tests_jsoncontent_v1.py` reads temporary content
directories, `tests_publishedcontent_reader_v1.py` serves the endpoint from them,
`tests_storage_v1.py` and `tests_startup_v1.py` exercise the real table in the test
database, and `tests_refresh_v1.py` fakes the fetch layer and disables the socket
layer as well.

Nothing touches `freesupertips.com` or any other host, and no test refreshes or
regenerates a fixture. The contract these tests pin is documented in
`docs/DATA_CONTRACT.md`; fixture provenance and the refresh procedure live in
`odds/alltips_scraper/fixtures/README.md`.

One of them,
`LegacyCacheContractTests.test_cache_keys_are_namespaced_per_view`, is
time-of-day sensitive: see section 4.3 of `docs/AUDIT.md`.

## 8. Logging

Logging is console-only (`LOGGING` in `odds/odds/settings.py`). Modules log with
`logging.getLogger(__name__)`. To get more detail without editing code:

```powershell
$env:ALLTIPS_LOG_LEVEL = 'DEBUG'
.\.venv\Scripts\python.exe .\odds\manage.py runserver
```

Never log `SECRET_KEY`, SMTP credentials, or `SCRAPE_URL` values.

## 9. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `ModuleNotFoundError: corsheaders` | dependencies not installed into the repository-root `.venv`; re-run setup step 3. |
| `UndefinedValueError: SECRET_KEY not found` | `odds/.env` missing; copy it from `.env.example`. |
| `UndefinedValueError: SCRAPE_URL not found` | same — `utils.py` reads it at import time. |
| `DisallowedHost` | add the host to `ALLOWED_HOSTS` in `odds/.env`. |
| `400 DisallowedHost` from a deployed host | add the host to `fly.toml` `[env] ALLOWED_HOSTS` and redeploy; a deployment never reads `odds/.env`. |
| Local requests redirect to an `https://` URL | one of the six transport values is set in `odds/.env` (usually `SECURE_SSL_REDIRECT`); unset it locally — those six describe a deployment's transport policy. |
| `uv venv` exits non-zero: "already exists" | the repository-root `.venv` is present; use `uv venv --clear --python 3.12 .venv` to rebuild. |
| PowerShell refuses to run `Activate.ps1` | not needed — call the interpreter directly (`.\.venv\Scripts\python.exe`); never activate a virtual environment or change the execution policy. |

## 10. Repository hygiene rules

* Never commit the repository-root `.venv/`, the legacy `odds/.venv` or
  `myvenv/`, `__pycache__/`, `*.pyc`, `odds/db.sqlite3` or any `.env` file —
  all are covered by the root `.gitignore` and `odds/.gitignore`.
  `.env.example` **is** tracked (it is documentation).
* Parser fixtures in `odds/alltips_scraper/fixtures/` are **static, synthetic,
  test-only** HTML. They are not Django `loaddata` fixtures and must not be
  regenerated, fetched, or refreshed by a test. Never write `SCRAPE_URL`, a host,
  or any credential into a fixture. See `fixtures/README.md` and
  `docs/DATA_CONTRACT.md`.
* Note the spelling difference: `odds/alltips_scraper/tests.py` holds the legacy
  cache/health tests, while `tests_parser_contract.py` and
  `tests_offline_guard.py` hold the Sprint 1B parser tests. Do not add a `tests`
  package next to `tests.py`.
* Always call Django through the repository-root interpreter
  (`.\.venv\Scripts\python.exe .\odds\manage.py <command>`); the project virtual
  environment lives at the repository root, not inside `odds/`.
* Do not change the six legacy paths, view names, or response shapes; add new
  behaviour behind new paths (as `/api/health/` does).
* Baseline recovery point: tag `sprint-0-audit-baseline`
  (`93250589e9f0feffb6387eb78f2e71a115f7e7e2` — the commit it points at). It is
  an annotated tag, so `git rev-parse sprint-0-audit-baseline` prints the tag
  object `49272696090d266dfcb3d1c401225652a5250879`; add `^{commit}` to get the
  commit. No history was rewritten and nothing was pushed during Sprint 0.5.
* See `docs/AUDIT.md` for the pre-change inventory, the envelope defect analysis,
  and the deliberate backlog.
* Deployment files are tracked at the repository root — `Dockerfile`,
  `.dockerignore` and `fly.toml` — and no secret may ever be written into them:
  `SECRET_KEY` is a Fly secret, while `DEFAULT_FROM_EMAIL`,
  `SCRAPE_URL`, `ALLOWED_HOSTS`, `PORT` and the six transport values
  (`SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
  `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`,
  `SECURE_HSTS_PRELOAD`) are deliberately readable `[env]` values. `.dockerignore` is a security control, not an optimisation: it is what
  keeps `odds/.env`, `odds/db.sqlite3` and the virtual environments out of an
  image layer. See `docs/DEPLOYMENT.md`.

## 11. Deployment (Fly.io)

The service is containerised and its Fly configuration is committed, but **no
deployment was performed in sprint 1E-A1**: the image was never built in that
environment (no Docker CLI), and no `fly` command was run. The full procedure,
including the image-safety checks, is `docs/DEPLOYMENT.md`. The short version:

```powershell
fly apps create <app-name>                           # then put the same name in fly.toml [app]
fly secrets set SECRET_KEY=<generated>                # see section 6
fly deploy                                            # release_command: migrate only
curl.exe https://<app-name>.fly.dev/api/health/       # the app name chosen above
```

What a deployment relies on, and what it deliberately does not do:

* The container runs `gunicorn odds.wsgi:application` as the unprivileged
  `appuser` (uid 10001) on port 8080. It never runs `manage.py runserver`, never
  runs `collectstatic`, and never serves static files.
* The database is the git-ignored SQLite file `odds/db.sqlite3`, the same one
  local development uses: there is no `DATABASE_URL`, no PostgreSQL and no
  database secret. Migrations are the only release action — no snapshot refresh,
  no fixture load.
* Liveness is `/api/health/`; there is no `/healthz`. `GET /api/v1/tips/` answers
  from the published content the image ships and returns `503` for a type the
  shipped manifest does not name; nothing schedules a refresh, and
  `manage.py refresh_tips` refuses to run (exit status `3`) against the read-only
  reader this image installs.
* Transport security is explicit, never inferred from `DEBUG`: `fly.toml` `[env]`
  sets `SECURE_SSL_REDIRECT`, secure session/CSRF cookies and one-year HSTS
  (with subdomains and preload), while a local `odds/.env` leaves the same six
  values unset and keeps serving plain HTTP. `SECURE_PROXY_SSL_HEADER` is
  unconditional, so Django recognises the `https` scheme Fly's edge forwards in
  `X-Forwarded-Proto`.
* `fly.toml` is a template, not an identity: `app`, `primary_region` and the
  `<app>.fly.dev` host in `[env] ALLOWED_HOSTS` are placeholders, and no `[[vm]]`
  block or machine size is committed. All four are chosen when the app is created
  (`fly apps create <name>`, `fly scale`), and `fly deploy` against an unfilled
  template fails fast rather than shipping under a guessed name.
* `odds/alltips_scraper/tests_deployment_v1.py` pins all of the above, so a
  change to the Dockerfile, `.dockerignore`, `fly.toml` or the pinned
  dependencies has to be deliberate.

