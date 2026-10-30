# Sprint 0.5 — `odds/` service audit and baseline

Date: 2026-09-27

| Item | Value |
| --- | --- |
| Baseline commit | `93250589e9f0feffb6387eb78f2e71a115f7e7e2` ("Initial commit") |
| Recovery tag | `sprint-0-audit-baseline` → `93250589e9f0feffb6387eb78f2e71a115f7e7e2` |
| Branch | `main` |
| Interpreter | CPython 3.12.13 in `odds/.venv` (provisioned with `uv 0.11.19`) |
| Scope | make the existing scraper service runnable, repo-hygienic, and contract-stable |

This document records what was true *before* the Sprint 0.5 work, what changed,
and what was deliberately left alone. It is the reference for follow-up sprints
(Flutter client, accounts, payments, deployment). Product brand, terminology,
access-tier, pricing, conversion, and match-lifecycle decisions are recorded
separately in `docs/PRODUCT_DECISIONS.md`.

## 1. Repository state at baseline

* Single `main` branch at `9325058…`; no tags, no stashes, no other refs.
* **7,335 tracked files**, of which:
  * 1,761 were committed `*.pyc` bytecode files (1,748 of them inside `myvenv/`),
  * the whole `myvenv/` virtual environment was committed (7,29x files),
  * `odds/db.sqlite3` (local dev database) was committed,
  * the only project source was `odds/` (8 Python modules) plus `sure_tips/`.
* `sure_tips/` was a **byte-identical duplicate** of the repository root
  (27 files on each side, identical hashes), including its own `.git` directory
  with 19 commits, all refs pointing at the same `9325058…` commit and no unique
  commits or uncommitted work (`git status --porcelain` was empty).
* No root `.gitignore`. `odds/.gitignore` contained a single line: `.env`.
* `odds/.env`, `odds/.env.example` and `odds/.venv` did **not** exist, so the
  project could not boot at all: `odds/odds/settings.py` requires `SECRET_KEY`
  and `DEFAULT_FROM_EMAIL` with no defaults, and `alltips_scraper/utils.py`
  requires `SCRAPE_URL` at import time.
* `.vs/` (Visual Studio cache) existed only as untracked scratch.

## 2. What was broken (pre-change)

### 2.1 The service could not start

* Before Sprint 0.5, `corsheaders.middleware.CorsMiddleware` was referenced in
  `MIDDLEWARE`, but `django-cors-headers` was **not installed or pinned in
  `odds/requirements.txt`** → `ModuleNotFoundError` on boot. Sprint 0.5 adds the
  dependency and adds `corsheaders` to `INSTALLED_APPS`.
* Required-but-absent environment variables (`SECRET_KEY`,
  `DEFAULT_FROM_EMAIL`, `SCRAPE_URL`) → `UndefinedValueError` on boot.
* `DEBUG = True` and `ALLOWED_HOSTS = ["*"]` were hard-coded, and
  `CORS_ALLOW_ALL_ORIGINS = True` was hard-coded: any deployment would have
  served a wildcard-CORS, debug-mode API.

### 2.2 Leaked / ad-hoc output

* `handlers.py:13` printed the *value* of an environment variable
  (`FREESUPERTIPS_ENABLE_VERIFICATION`) to stdout on every import.
* `handlers.py:44`, `handlers.py:47`, `utils.py:100` and `utils.py:165` used
  `print()` for diagnostics instead of the logging framework.

### 2.3 Legacy response-envelope defect

`alltips_scraper/decorators.py` cached responses under a key made from the view
name only, then re-wrapped cache hits:

* On a **cache miss** the raw view payload was returned and the *whole dict* was
  stored (`cache.set(cache_key, response_data, …)` when the payload contained
  `matches`).
* On a **cache hit** the branch that mattered for five of the six endpoints
  (payloads shaped `{date, total_accumulators, accumulators, count, source}`)
  produced a *different* envelope:
  `{"matches": <entire payload dict>, "message": "Found N cached matches",
  "available": true, "cached": true}`.
* `len(cached_data)` was therefore `len(dict)` (the number of top-level keys)
  misreported as a match count.
* The key was not date-scoped: a response cached at any time was served with
  `cached: true` for the next 4 hours (LocMemCache, TTL 14400 s), including
  across the UTC midnight rollover into a new day's tips.
* Error payloads were cached too, so a transient upstream failure was pinned for
  four hours.

### 2.4 Other pre-existing (unchanged) defects kept on the backlog

* `alltips_scraper/utils.py` is a single ~400-line module holding all six
  scrapers plus the HTML parsers; there is no separation between fetching,
  parsing, and shaping.
* `parse_match_from_leg` / `parse_card` swallow every exception and return
  `None` / an empty payload, so upstream HTML changes degrade into empty tips
  rather than a visible error.
* `handlers.get_all_tips()` exists but is not routed; `/api/health/` did not
  exist, so there was no way to check liveness without scraping.
* `db.sqlite3` is used for a service whose API is entirely read-through-cache;
  the database is not needed for the tips endpoints.
* No tests at all (`tests.py` was the Django stub).

## 3. Changes made in Sprint 0.5

### 3.1 Dependency + environment (Step 2/3)

* `odds/requirements.txt` now pins **`django-cors-headers==4.9.0`** (latest
  release; supports Django 6.0 and Python 3.12+), so the settings module can
  import.
* Created `odds/.env.example` — the documented list of every environment
  variable, its default, and which values are required.
* Created a git-ignored `odds/.env` for local development (`DEBUG=True`,
  localhost hosts, no wildcard CORS, verification disabled).
* `handlers.py`'s `FREESUPERTIPS_ENABLE_VERIFICATION` now comes from `decouple`
  like every other setting (`default=False`, `cast=bool`) instead of raw
  `os.getenv`, so it is read from `odds/.env` rather than only from the process
  environment.

### 3.2 Public URL surface (Step 7)

Unchanged and frozen for the legacy client:

| Path | View name | Handler |
| --- | --- | --- |
| `/api/bet-of-the-day/` | `bet_of_the_day` | `get_bet_of_the_day` |
| `/api/daily-accumulator/` | `daily_accumulator` | `get_daily_accumulator` |
| `/api/btts-win-accumulator/` | `btts_win_accumulator` | `get_btts_win_accumulator` |
| `/api/over-25-goals-accumulator/` | `over_25_goals_accumulator` | `get_over_25_goals_accumulator` |
| `/api/BTTS/` | `both_teams_to_score` | `get_both_teams_to_score` |
| `/api/goalscorer/` | `anytime_goalscorer` | `get_anytime_goalscorer` |

Added: `GET /api/health/` → `{"status": "ok", "service": "sure-tips-api",
"api_version": "legacy"}`. It performs no scraping, touches neither the
database nor the cache, requires no credentials, and repeats no configuration.
Note the mixed-case `/api/BTTS/` path is preserved deliberately: it is part of
the legacy contract.

### 3.3 Cache contract (Step 5, `decorators.py`)

`cache_matches()` was rewritten so that caching cannot change an envelope:

* the payload is cached exactly as the view produced it — no re-wrapping and no
  re-keying under `matches`;
* a hit returns the stored payload with `cached: true`, a miss returns the view
  payload with `cached: false`, so the key set of the two responses is identical;
* payloads containing a top-level `error` key are never cached;
* cache keys are namespaced and date-scoped:
  `legacy_api:v1:<view_name>:<YYYY-MM-DD>` (+ a short query-string fingerprint
  when query parameters are present), so yesterday's tips can never be served
  today, and future parameters cannot collide;
* the timeout default is unchanged (14,400 s).

### 3.4 Settings (Step 4, `odds/odds/settings.py`)

| Setting | Before | After |
| --- | --- | --- |
| `DEBUG` | hard-coded `True` | `config('DEBUG', default=False, cast=bool)` |
| `ALLOWED_HOSTS` | hard-coded `["*"]` | `config('ALLOWED_HOSTS', default='localhost,127.0.0.1,[::1]', cast=Csv())` |
| `CORS_ALLOW_ALL_ORIGINS` | hard-coded `True` | `config(..., default=False, cast=bool)` (opt-in, dev only) |
| `CORS_ALLOWED_ORIGINS` | hard-coded single origin | from `CORS_ALLOWED_ORIGINS` (CSV); forced to `[]` while the wildcard is on |
| `corsheaders` app | missing from requirements | installed and still listed before `django.contrib.admin`; `CorsMiddleware` still first |
| `LOGGING` | absent (defaults) | console handler + formatter; `alltips_scraper` logger; levels from `DJANGO_LOG_LEVEL` / `ALLTIPS_LOG_LEVEL` |

A missing or mistyped environment variable can no longer switch on debug mode or
open CORS to every origin.

### 3.5 Logging instead of `print()` (Step 6)

All five print sites are gone, replaced by `logging.getLogger(__name__)` calls
(`logger.debug` for verification flow, `logger.warning` for parse failures). The
`handlers.py` line that echoed the value of an environment variable on import
was deleted outright rather than reformatted — the setting's value is never
logged. No `SECRET_KEY`, SMTP, or `SCRAPE_URL` value is logged anywhere.

### 3.6 Tests (Step 8, `odds/alltips_scraper/tests.py`)

Eight tests, no network access (all six view handlers are monkeypatched):

1. `/api/health/` works even when every scraper raises, and its body contains
   exactly three keys with no configuration, path, or `.env` leakage.
2. For **all six** legacy endpoints: cold and warm responses have identical key
   sets and identical values apart from the `cached` flag, `count` is preserved,
   and the scraper is called exactly once across the two requests.
3. For the five accumulator-shaped endpoints: `accumulators`,
   `total_accumulators`, `count` and `tip_type` survive a cache hit unchanged,
   and `matches` / `message` / `available` never appear (the old bug's
   signature).
4. Cache keys are namespaced per view and contain the current date.
5. Two error payloads (bet-of-the-day shaped and accumulator shaped) are
   returned unchanged, are *not* cached, and reach the scraper on every request.
6. Date rollover: a payload cached for `2026-09-26` is never served for
   `2026-09-27`; a same-day repeat is served from cache.

### 3.7 Repository hygiene (Step 9)

* `myvenv/` (7,29x files), the 13 committed `__pycache__`/`*.pyc` bytecode files
  and `odds/db.sqlite3` were removed from the index with `git rm --cached`
  (files kept on disk, now ignored).
* Root `.gitignore` created; `odds/.gitignore` expanded from one line to cover
  env files (with `!.env.example`), virtualenvs, bytecode, Django runtime
  artifacts, editor folders, and archives.
* The byte-identical duplicate tree `sure_tips/` (87 MB, 27 files, 19 commits all
  at `9325058…`, clean working tree) and the untracked `.vs/` cache were deleted
  **after** the recovery tag was created.
* No history was rewritten, nothing was force-pushed, and nothing was pushed at
  all in this sprint.

## 4. Verification (verbatim, Step 11)

Environment: CPython 3.12.13 in `odds/.venv`, `DEBUG=True` from the local
`odds/.env`. All commands were run from `odds/` with
`.\.venv\Scripts\python.exe`.

```
> python manage.py check
System check identified no issues (0 silenced).                       [exit 0]

> python manage.py check --deploy                                     [exit 0]
WARNINGS: security.W004, security.W008, security.W012, security.W016,
          security.W018
System check identified 5 issues (0 silenced).

> python manage.py migrate --noinput                                  [exit 0]
Operations to perform:
  Apply all migrations: admin, auth, contenttypes, sessions
Running migrations:
  No migrations to apply.

> python manage.py test -v 2 --noinput                                [exit 0]
Found 8 test(s).
...
Ran 8 tests in 0.157s

OK
```

The eight tests, all `ok`:

```
alltips_scraper.tests.CacheKeyDateTests.test_cache_key_is_scoped_to_the_current_date
alltips_scraper.tests.ErrorResponseCacheTests.test_accumulator_error_is_not_cached
alltips_scraper.tests.ErrorResponseCacheTests.test_bet_of_the_day_error_is_not_cached
alltips_scraper.tests.HealthEndpointTests.test_health_exposes_no_configuration_or_paths
alltips_scraper.tests.HealthEndpointTests.test_health_is_scraper_independent
alltips_scraper.tests.LegacyCacheContractTests.test_accumulator_envelopes_are_never_rewritten
alltips_scraper.tests.LegacyCacheContractTests.test_cache_keys_are_namespaced_per_view
alltips_scraper.tests.LegacyCacheContractTests.test_envelope_is_identical_cold_and_warm
```

### 4.1 The five `check --deploy` warnings

`W004` (no HSTS), `W008` (no SSL redirect), `W012`/`W016` (secure-only session
and CSRF cookies) and `W018` (`DEBUG=True`) are the standard production
hardening checklist. `W018` is expected for a local `.env`; the other four
belong to the deployment sprint (they require knowing the real host/TLS setup)
and are **not** Sprint 0.5 scope. They are recorded here so the deployment
sprint starts from a known list rather than a surprise.

### 4.2 Live smoke test (real `runserver`, not the test client)

`manage.py runserver 127.0.0.1:8123 --noreload` was started as a real process,
queried over HTTP, and stopped again:

```
== start runserver ==
server_pid=23020 has_exited=False
== GET /api/health/ ==
status=200
content_type=application/json
body={"status": "ok", "service": "sure-tips-api", "api_version": "legacy"}
== GET /api/health/ from a browser-like origin (CORS headers) ==
status=200
allow_origin=http://localhost:50083
== GET an unknown path (should be 404) ==
unknown_path_status=404
server_stopped=True
== runserver stderr ==
[27/Sep/2026 18:54:04] "GET /api/health/ HTTP/1.1" 200 69
Not Found: /api/does-not-exist/
2026-09-27 18:54:04,460 WARNING django.request Not Found: /api/does-not-exist/
[27/Sep/2026 18:54:04] "GET /api/does-not-exist/ HTTP/1.1" 404 3544
```

Three things this proves beyond the unit tests:

1. the process boots against `odds/.env` with `corsheaders` installed;
2. the `CORS_ALLOWED_ORIGIN_REGEXES` rule still answers the Flutter-Web-style
   `http://localhost:<port>` origin (the previously hard-coded, commented-out
   `http://localhost:50083` entry is no longer needed);
3. the new `LOGGING` configuration is active — the third-party
   `django.request` warning is rendered with the configured
   `{asctime} {levelname} {name} {message}` format.

### 4.3 Re-verification from the repository root (2026-09-27, close-out)

The Step 11 commands above were run from `odds/`. Re-running the same
verification from the documented repository root surfaced two things that the
`cd odds` transcript cannot show.

**1. `manage.py test` needs an app label from the repository root.** `odds/` is
not a Python package, so Django's default discovery walks the repository root
without importing `odds/` and reports:

```
Found 0 test(s).
Ran 0 tests in 0.000s

NO TESTS RAN
```

`manage.py test alltips_scraper -v 2 --noinput` finds all 8 tests.
`odds/customers/tests.py` is the untouched Django stub, so every test lives in
`alltips_scraper`.

**2. `LegacyCacheContractTests.test_cache_keys_are_namespaced_per_view` is
time-of-day sensitive.** `tests.py:159` asserts that the OS-local
`date.today()` appears in a cache key built from Django's
`timezone.localdate()` (`TIME_ZONE = 'UTC'`, `USE_TZ = True`). While the host's
local date and the UTC date differ — on this UTC-04:00 host, between 20:00 and
24:00 local time — the two disagree and the test fails:

```
FAIL: test_cache_keys_are_namespaced_per_view
  (alltips_scraper.tests.LegacyCacheContractTests.test_cache_keys_are_namespaced_per_view)
  File "...\odds\alltips_scraper\tests.py", line 159, in test_cache_keys_are_namespaced_per_view
    self.assertIn(date.today().isoformat(), bet_of_the_day)
AssertionError: '2026-09-27' not found in 'legacy_api:v1:bet_of_the_day:2026-09-28'

Ran 8 tests in 0.078s

FAILED (failures=1)
```

The production behaviour is correct — keys are date-scoped in `TIME_ZONE` — so
the assertion, not the decorator, is the defect. It was **not** changed during
Sprint 0.5 close-out, because this sprint is documentation- and
verification-only and product code stayed frozen. Recommended one-line fix:
assert against `timezone.localdate()` (or an injected `today=`) instead of
`date.today()`.


## 5. Repository hygiene: before / after

| Metric | Before | After |
| --- | --- | --- |
| Tracked files | 7,335 | 27 + files added in this sprint |
| Tracked files under `myvenv/` | 7,29x | 0 |
| Tracked `*.pyc` | 1,761 | 0 |
| Tracked `*.sqlite3` | 1 (`odds/db.sqlite3`) | 0 |
| Duplicate trees | `sure_tips/` (untracked, 87 MB) | removed |
| Root `.gitignore` | absent | present |
| `odds/.gitignore` | 1 line (`.env`) | 7 sections |

Ignore rules verified with `git check-ignore -v`:

```
odds/.gitignore:4:.env              odds/.env          <- ignored
odds/.gitignore:6:!.env.example     odds/.env.example  <- matched by negation, committed
odds/.gitignore:11:.venv/           odds/.venv         <- ignored
```

## 6. Deliberately out of scope (backlog for later sprints)

* Database/model work, accounts, subscriptions, payments — untouched.
* Flutter/mobile code — not present in this repository; root `.gitignore` already
  covers Dart/Flutter build output as a placeholder.
* Auth, rate limiting and per-tier gating on the tips endpoints — the legacy six
  remain unauthenticated public GETs, as they are today.
* `check --deploy` hardening (HSTS / SSL redirect / secure cookies) — **done in
  sprint 1E-A1** (section 7): the settings follow `DEBUG`, so
  `manage.py check --deploy` is clean with `DEBUG=False`.
* Shared cache backend (Redis/Memcached): the default `LocMemCache` is
  per-process, so with multiple workers each process warms its own cache and the
  date-scoped keys are per-process. Correct, but not shared.
* `scraped_at` in `handlers.get_all_tips()` is still a naive local timestamp, and
  that helper is still unrouted.
* `utils.py` module split, and turning silent parse failures into visible errors.
* Product brand and terminology are now decided (see `docs/PRODUCT_DECISIONS.md`):
  product brand **OddMate**, app label **ODDMATE TIPS**, customer-facing access
  terms **Free** / **Premium** (never VIP). The *implementation* of pricing,
  subscriptions, payments, entitlements, and result settlement remains future
  work and does not block this sprint.


## 7. Sprint 1E-A1 — deployment readiness (2026-10-02)

> **Superseded in part — see §8 (2026-10-03).** The `DATABASE_URL` /
> Fly Managed PostgreSQL part of this sprint was removed afterwards. Everything
> below stays as the historical record of sprint 1E-A1: read its database, pin
> and `.env.example` claims as the state *at that date*, not as the current
> architecture.

Sprint 1E-A1 makes the service *deployable*: it adds the container and the Fly
configuration, PostgreSQL through `DATABASE_URL`, and the pins the image needs.
**Nothing was deployed** — no `fly` command ran against a real app, no image was
published, and the Dockerfile was not built (the sprint environment had no Docker
CLI). This section is the sprint record; `docs/DEPLOYMENT.md` is the procedure.

| Item | Value |
| --- | --- |
| Branch | `main` at `40d5b24` (the sprint 1D merge) |
| Scope | container, build context, Fly configuration, `DATABASE_URL`, pins, tests, docs — the `DATABASE_URL` part was removed later, see §8 |
| Deployment performed | **no** — configuration only |
| Suite | 500 → 515 tests, all passing |
| `check --deploy` | 4 warnings → no issues (with `DEBUG=False` and the six transport values) |

### 7.1 What changed

* **Container.** `Dockerfile` at the repository root: `python:3.12-slim`,
  dependencies installed from `odds/requirements.txt`, the service directory
  copied to `/app/odds`, an unprivileged `appuser` (uid 10001), `EXPOSE 8080`, and
  a `gunicorn odds.wsgi:application` `CMD` that binds Fly's `$PORT` with a 120 s
  timeout (the legacy routes fetch upstream synchronously, well past gunicorn's
  30 s default). No `manage.py runserver`, no `collectstatic`/WhiteNoise, and no
  migrations at container start.
* **Build context.** `.dockerignore` excludes `.env`, `*.sqlite3`, virtual
  environments, bytecode, VCS/editor state, `docs/` and the deployment
  descriptors. This is a security control rather than an optimisation: the
  context root holds `odds/.env` and `odds/db.sqlite3`, and the root is what
  `.gitignore` already protects from the repository side.
* **Fly configuration.** *(Superseded in part — §13.)* `fly.toml`: a placeholder app name and region,
  `[build] dockerfile`, a `[env]` block holding non-secrets only
  (`ALLOWED_HOSTS`, `DEFAULT_FROM_EMAIL`, `SCRAPE_URL`, `PORT`, and the six
  transport values), `[deploy] release_command = "python manage.py migrate
  --noinput"` as the single release action, an `[http_service]` on port 8080 with
  `force_https`, `auto_stop_machines = "off"` (a stopped machine would stall the
  first scrape of the day) and one `GET /api/health/` check, and no `[[vm]]` block
  at all — the machine size is a deploy-time choice, not a committed one.
  `SECRET_KEY` and `DATABASE_URL` are Fly secrets and appear nowhere in the
  repository; `DEBUG` and `CORS_ALLOW_ALL_ORIGINS` are deliberately absent, so the
  safe defaults apply. (`DATABASE_URL` is no longer a secret or a setting at all —
  §8.)
* **Settings.** *(Superseded — §8.)* `DATABASES['default']` is still the git-ignored SQLite file by
  default; a non-empty `DATABASE_URL` replaces it through `dj_database_url.parse`,
  and the PostgreSQL path is configured for Fly's pooler: `CONN_MAX_AGE = 0`,
  `DISABLE_SERVER_SIDE_CURSORS = True` and
  `OPTIONS['prepare_threshold'] = None`, so no pooled connection is left holding
  a cursor, a transaction or a prepared statement that the pooler could hand to
  another client. Transport security is six explicit environment values
  (`SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
  `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`,
  `SECURE_HSTS_PRELOAD`), each read on its own and none derived from `DEBUG`, so
  the local defaults keep plain HTTP working; `SECURE_PROXY_SSL_HEADER` stays
  unconditional because the container is only ever reached through Fly's TLS
  proxy.
* **Pins.** *(Superseded in part — §8.)* `gunicorn==26.2.0`, `psycopg[binary]==3.3.6` and
  `dj-database-url==3.1.2`, appended to the flat pinned list.
* **Configuration.** *(Superseded in part — §8.)* `odds/.env.example` documents `DATABASE_URL` (empty by
  default, a secret the moment it holds a value) and the six transport values
  (left unset, because a development machine wants the plain-HTTP defaults), with
  the PostgreSQL pooling note beside them.
* **Tests.** *(Superseded in part — §8.)* `odds/alltips_scraper/tests_deployment_v1.py` (15 tests) pins the
  two ends that matter: `DATABASE_URL` precedence, the pooling-safe PostgreSQL
  options and the six transport values — each observed by importing the shipped
  settings module in a subprocess, so the assertions describe the file on disk
  rather than a re-implementation of it; and the shipped `Dockerfile`,
  `.dockerignore`, `fly.toml`, requirements and `.env.example`, read from disk
  (`fly.toml` through `tomllib`). `fly.toml` and `.env.example` are also scanned
  for forbidden tokens, so a stray credential fails the suite instead of shipping.
* **Docs.** `docs/DEPLOYMENT.md` (new), plus `README.md`, `docs/RUNBOOK.md`
  (§6, §7, §9, §10, §11) and this section. The corrective pass that followed the
  first review aligned every count, setting name, test-file reference and Fly
  placeholder in those four documents with the shipped files, so no document
  describes a number, a `DEBUG`-derived rule or an app identity the code no longer
  has.

### 7.2 Verification performed

| Command | Result |
| --- | --- |
| `manage.py check` | no issues |
| `manage.py makemigrations --check --dry-run` | no changes detected (no model changes in this sprint) |
| `manage.py test alltips_scraper` | 515 tests, OK |
| `manage.py test alltips_scraper.tests_deployment_v1` | 15 tests, OK |
| `DEBUG=False manage.py check --deploy` | 4 warnings with the transport values unset; no issues with them set |
| `docker build` / `docker run` / image-safety checks | **not run** — no Docker CLI in the sprint environment |

### 7.3 Deliberately out of scope (backlog)

* The image has never been built, so the Dockerfile is unproven end to end;
  a build-and-smoke-test CI job is the next step (`docs/DEPLOYMENT.md` §10).
* No `/healthz`: `/api/health/` is the liveness route, and a second alias was
  deliberately not added.
* No WhiteNoise/`collectstatic`: the image serves no static files, so `/admin/`
  would be unstyled in a deployment.
* No scheduler, process group or automatic `refresh_tips`: snapshot refresh stays
  out of band and manual, exactly as sprint 1D left it.
* No shared cache: `LocMemCache` is per process, so two gunicorn workers hold two
  caches with their own date-scoped keys.
* No `CSRF_TRUSTED_ORIGINS`: the API is read-only public `GET`s, so no browser
  POST surface needs it yet.
* No filled-in Fly identity. *(Superseded — §13.)* `fly.toml` keeps placeholders for `app` and
  `primary_region`, so the first deploy has to be preceded by `fly apps create`
  and a one-line edit. That is deliberate — an app name belongs to the account
  that owns it, not to this repository.


## 8. Database scope correction — SQLite only (2026-10-03)

Sprint 1E-A1 planned PostgreSQL through a Fly-managed cluster and a
`DATABASE_URL` secret. That design was removed here, completing a removal that
was already sitting unstaged in the working tree at `7fb71bd` — in
`odds/odds/settings.py`, the `dj_database_url` import and the whole
`if DATABASE_URL:` branch, including `CONN_MAX_AGE = 0`,
`DISABLE_SERVER_SIDE_CURSORS = True` and `OPTIONS['prepare_threshold'] = None`.
The partial edit was **kept and completed coherently**, not restored: the intent
of the change had already been decided, and reverting it only to re-apply the
same removal would have discarded reviewed work.

| Item | Value |
| --- | --- |
| Branch | `main` at `7fb71bd`, with this change uncommitted in the working tree |
| Scope | settings, requirements, `.env.example`, `fly.toml` / `Dockerfile` comments, deployment tests, docs |
| Deployment performed | **no** — and nothing was built, installed, migrated or refreshed |
| External actions | none: no `pip install`, no Docker, no `fly`, no migration run, no `refresh_tips`, no network call, no commit |
| Suite | 515 tests, all passing |
| `tests_deployment_v1.py` | 15 tests, all passing |
| `manage.py check` | no issues |
| `manage.py makemigrations --check --dry-run` | no changes detected |
| `git diff --check` | clean |

### 8.1 What changed

* **Settings.** `odds/odds/settings.py` no longer imports `dj_database_url` and no
  longer reads `DATABASE_URL`: the stranded `DATABASE_URL = config(...)` line and
  the commented-out import went with the branch they belonged to.
  `DATABASES['default']` is the git-ignored SQLite file for every environment, so
  an unset, empty or hostile `DATABASE_URL` now changes nothing at all, and the
  three pooler-safety options are gone with the pooled deployment that needed
  them.
* **Deliberately unchanged.** The SQLite `DATABASES` entry, Django's own installed
  apps (`admin`, `auth`, `sessions`, `contenttypes`), the existing migrations, the
  `SnapshotV1` model, `DatabaseSnapshotProvider`, `refresh_v1` and `refresh_tips`
  are untouched: the versioned snapshot table still lives in that same file, which
  is why `fly.toml` keeps `release_command = "python manage.py migrate --noinput"`.
* **Pins.** `dj-database-url==3.1.2` and `psycopg[binary]==3.3.6` were removed
  from `odds/requirements.txt`. `gunicorn==26.2.0` stays: the container still
  serves the app with gunicorn.
* **Configuration.** `odds/.env.example` no longer documents `DATABASE_URL`, and
  the `postgres://USER:PASSWORD@HOST:5432/DBNAME` shape went with it. `fly.toml`
  and `Dockerfile` lost the `fly postgres attach` and `psycopg[binary]` claims
  from their comments; no instruction, port, health check or release action
  changed.
* **Tests.** The two obsolete assertions were replaced in the same diff, keeping
  the module at 15 tests:
  `test_database_url_selects_postgres_with_its_own_credentials` became
  `test_database_url_selects_nothing_and_no_credential_reaches_the_mapping` (a
  credential-shaped `DATABASE_URL` is set, the mapping must stay SQLite, and no
  `USER` / `HOST` / `PORT` key may appear), and
  `test_postgres_connection_options_are_pooling_safe` became
  `test_postgres_pooling_options_are_gone` (no connection option may be
  configured for any input). `EnvExampleTests` now asserts the *absence* of a
  `DATABASE_URL` key and of any `postgres://` line instead of asserting that one
  is present.
* **Docs.** `README.md`, `docs/DEPLOYMENT.md` (§1, §3, §4, §8, §10) and
  `docs/RUNBOOK.md` (§2, §6, §9, §10, §11) were corrected: the secret tables, the
  first-time setup steps, the troubleshooting rows and the environment-variable
  table carry no database entry and no `fly postgres` command any more. Section 7
  above is kept as history, with a superseded banner.

### 8.2 Open gap this leaves

The database is now a file on the machine's own filesystem, so a snapshot written
by `manage.py refresh_tips` does not survive a redeploy, and nothing schedules
that command. Recorded as a gap in `docs/DEPLOYMENT.md` §10; a volume, a shared
cache and a scheduler all remain future work, out of scope for this correction.


## 9. Published-content reader and the writer precondition (2026-10-04)

Batch B makes reviewed published content the reader a deployment answers from, and
makes the out-of-band writer refuse to run against anything that cannot store. It
is a wiring and safety change: no request-path behaviour, envelope, query
parameter or error code changed.

| Item | Value |
| --- | --- |
| Branch | `main`, with batch A (content reader, canonical digest rule, deployment docs) uncommitted in the working tree |
| Scope | `apps.py`, `management/commands/refresh_tips.py`, `tests_startup_v1.py`, `tests_refresh_v1.py`, `tests_deployment_v1.py`, the new `tests_publishedcontent_reader_v1.py`, and the docs |
| Deployment performed | **no** - nothing was built, installed, migrated, served or deployed |
| External actions | none: no network call, no `pip install`, no Docker, no `fly`, no migration run, no `refresh_tips` against a source, no commit |
| Suite | 605 tests, all passing (was 575: +16 new module, +7 writer precondition, +4 image content, +3 startup) |
| `manage.py check` | no issues |
| `manage.py makemigrations --check --dry-run` | no changes detected |
| `git diff --check` | clean |

### 9.1 What changed

* **The install point installs the read-only reader.**
  `apps.AlltipsScraperConfig.ready()` now installs
  `jsoncontent_v1.JsonSnapshotProvider()` instead of
  `storage_v1.DatabaseSnapshotProvider()`. The body keeps its shape: one seam
  accessor import, one provider import, one unconditional call, no branch and no
  configuration read. The provider defaults its content root to the directory this
  package ships, so the artifacts inside the image are what a deployment publishes.
* **`refresh_tips` refuses to run unless the durable provider is installed.** The
  command gained one precondition, decided before `--type` is resolved: when the
  installed provider is not `storage_v1.DatabaseSnapshotProvider`, the run reports
  `refresh refused: the installed snapshot provider is not writable durable
  storage` with return code `3`. Django prints that as one `CommandError:` line on
  stderr and exits `3`; `--traceback` re-raises it instead. Such a run fetches
  nothing, reads no row, writes no row, prints no report line and logs nothing.
* **Exit statuses are now four.** `0` when every selected type was published, `1`
  when a type was refused, `2` when `--type` named a type the registry does not
  publish, and `3` when the installed provider cannot store - and the `3` is
  decided first, so it wins over a `2`.
* **Tests.** `tests_startup_v1.py` pins the new install (reader identity, content
  root, read-only seam, a durable row left untouched); `tests_refresh_v1.py` adds
  the precondition coverage (the deployed reader, the seam's in-memory default, an
  arbitrary three-method object, an unusable `--type`, `--dry-run`, stderr with
  exit `3`, and `--traceback`) and installs the durable provider for the command
  tests that must still succeed; the new `tests_publishedcontent_reader_v1.py`
  serves `/api/v1/tips/` from a reader whose content root the test writes (envelope,
  zero database queries, both `503` states, a published record winning over a
  stored row, and a refused write); and `tests_deployment_v1.py` adds the
  image/content contract (the manifest ships, the copied service directory holds
  it, and the build context excludes nothing under it).
* **Docs.** `README.md`, `docs/API_V1_CONTRACT.md`, `docs/RUNBOOK.md` (sections
  4.1, 4.2, 7, 11), `docs/DEPLOYMENT.md` (sections 1, 9, 10) and
  `docs/DATA_CONTRACT.md` section 12 now name the published content as the deployed
  reader, the durable store as writer-only, and the `3` refusal.

### 9.2 What deliberately did not change

* The request path: `views_v1.py`, `urls_v1.py`, `serializers_v1.py` and
  `readmodel_v1.py` are untouched, so the route, the query parameters, the envelope
  and every error code are exactly as sprint 1C published them.
* The durable store: `storage_v1.py`, the `SnapshotV1` model and its migration are
  unchanged, and the provider is still installed by everything that wants it - only
  the deployment's own install point chose the reader.
* The writer itself: `refresh_v1` still decides, stores and reports exactly as
  before; only the command's precondition is new.
* The shipped content: `content/v1/manifest.json` is still the tracked empty
  manifest (`{"schema_version": 1, "snapshots": {}}`), so this batch publishes no
  tip type.

### 9.3 Open gap this leaves

The deployed reader publishes what the image ships, and the writer's rows are not
read by it, so refreshing a deployment's snapshots would need a durable provider
installed there - which is a storage decision (a volume or a managed database), not
a configuration flag. Until then, a type appears on a deployment only through a
reviewed change to the content directory and a redeploy. Recorded as a gap in
`docs/DEPLOYMENT.md` section 10.

## 10. Review-time candidate-content validator (2026-10-04)

Sprint 1F adds the review-time reading of a candidate content directory and shares
the report-token rule between the writer and the reviewer. It is a review-tooling
change: no request-path behaviour, envelope, query parameter, error code, seam,
provider, content manifest, setting, dependency, migration, Docker/Fly
configuration or endpoint/API contract changed.

| Item | Value |
| --- | --- |
| Branch | `main`, with the published-content reader (section 9) and this review tooling uncommitted in the working tree |
| Scope | the new `alltips_scraper/contentcheck_v1.py`, the new `alltips_scraper/management/commands/validate_v1_content.py`, the new `alltips_scraper/reporting_v1.py` (re-exported by `refresh_v1.py`), the new `tests_contentcheck_v1.py`, and the docs |
| Deployment performed | **no** - nothing was built, installed, migrated, served or deployed |
| External actions | none: no network call, no `docker`, no `fly`, no migration run, no `refresh_tips` against a source, no publish, no commit |
| Suite | 654 tests, 1 skipped (`manage.py test alltips_scraper -v 2 --noinput`); the review-tooling module contributes 49 of them |
| `manage.py check` | no issues |
| `manage.py validate_v1_content --root odds/alltips_scraper/content/v1` | exit `0` - the tracked empty manifest is a valid candidate |
| `manage.py makemigrations --check --dry-run` | no changes detected |
| `git diff --check` | clean |

### 10.1 What changed

* **A second reading of the same artifacts.** `contentcheck_v1` checks a candidate
  directory against the reader's own rules (`jsoncontent_v1`) and in the reader's
  own vocabulary, and answers one question: can the current JSON reader safely load
  this candidate artifact? It enumerates everything the candidate claims, refuses
  the whole directory when anything it claims would not load, and reports one line
  per artifact - the manifest line first, then one line per type key in sorted
  order, so two runs over one candidate print the same report:

  ```text
  manifest status=ok entries=<n>
  type=<token> status=ok
  type=<token> status=refused reason=<token>
  manifest status=refused reason=<token>
  ```

* **One candidate-only check.** Two of the states the reader answers `None` for are
  silent, and a review has to catch them. A type key no manifest names stays "not
  published yet"; a candidate root with no manifest at all is refused as
  `manifest_missing`, the one reason token this module adds to the reader's
  vocabulary, so the set a report can print is the reader's tokens plus that one.
  The two states are deliberately different, because "nothing is published" is a
  valid deployment and never a valid candidate to review.

* **Not a second rule book.** Registry membership is deliberately not checked, and
  neither is whether the payload describes tips or whether the instant is recent:
  those are the serializer's and the reviewer's questions, and a validator that
  guessed at them would refuse candidates the deployment would happily serve.
* **The command that runs it prints the reading and carries the verdict.**
  `manage.py validate_v1_content --root DIRECTORY` is this app's command, `--root`
  is explicit with no default, and the report is printed in full before the exit
  status is decided: `0` for a usable candidate (including a valid manifest that
  names no entry at all), `1` when the manifest or one or more named artifacts were
  refused, and `2` when the invocation itself cannot be served (`--root` missing or
  not a directory), in which case no artifact is checked and no report line is
  printed. It writes no file, creates no directory, reads no configuration, reaches
  no network, touches no database and no cache, and never touches `content/v1/`
  unless that is the directory it was handed.
* **The token rule is stated once.** `safe_token` and its constants moved into the
  new `reporting_v1`, which imports nothing at all; `refresh_v1` re-exports the four
  names (the same function object - `refresh_v1.safe_token is
  reporting_v1.safe_token`) and `contentcheck_v1` imports the function directly, so
  the validator no longer reaches the writer at all. A key that came from outside
  therefore cannot forge a second report line, a second field or an invented `=` in
  either the writer's report or the reviewer's.
* **Tests.** The new `tests_contentcheck_v1.py` pins the review contract end to end:
  the checks and the reason vocabulary, the deterministic report, the sanitised key,
  the command's report and all three exit statuses, a refused invocation leaving no
  line and no change on disk, the offline guard, the shared token object, and that
  `reporting_v1` imports nothing.

### 10.2 What deliberately did not change

* The request path, the reader, the seam and the provider: `views_v1.py`,
  `urls_v1.py`, `serializers_v1.py`, `readmodel_v1.py`, `jsoncontent_v1.py`,
  `storage_v1.py` and `apps.py` are untouched, so the route, the query parameters,
  the envelope, the error codes and the install point are exactly as sections 8 and
  9 recorded them.
* The writer: `refresh_v1` still decides, stores and reports exactly as before.
  Only `safe_token` and its three constants moved to `reporting_v1` and are
  re-exported, so no name, token or output line changed.
* The shipped content: `content/v1/manifest.json` is still the tracked empty
  manifest (`{"schema_version": 1, "snapshots": {}}`), so this batch publishes no
  tip type, and the validator's passing run over it proves the candidate is usable
  rather than that any type is published.
* The deployment: the validator is not invoked at build, release or runtime, and
  nothing in a deployment writes, refreshes or republishes content. No cache,
  Redis, Upstash, secret or publication automation was added, and no setting,
  dependency, migration, content manifest or Docker/Fly file changed.

### 10.3 Open gap this leaves

Publication is still a reviewed change to the content directory followed by a
redeploy. The validator tells a reviewer whether a candidate would load, but
nothing generates a candidate, computes its canonical digest or pins its
authoritative instant, and nothing schedules any of it: the review-time checks
`tests_contentcheck_v1.py` pins are not a replacement for the reader's runtime
answer. A controlled publication workflow remains future work, recorded beside the
storage and publication gaps in `docs/DEPLOYMENT.md` section 10.

## 11. Deployment-readiness documentation audit (2026-10-09)

Sprint 1I re-read the deployment documents against the code, the container
configuration and the contracts they describe, and corrected the drifts it could
verify. It is a documentation sprint: no application code, setting, dependency,
migration, content manifest, Dockerfile, `fly.toml`, test or endpoint changed, and
nothing was built, migrated, served or deployed.

| Item | Value |
| --- | --- |
| Branch | `main` at `77a6ffcb21668545da2f0c4aefd00a109a4d5a81`, which is also `origin/main`, with a clean working tree at the start |
| Scope | `docs/DEPLOYMENT.md` sections 1, 6 and 10, this record, and `docs/RUNBOOK.md` section 7; `docs/API_V1_CONTRACT.md` was re-read and left unchanged |
| Deployment performed | **no** - nothing was built, installed, migrated, served or deployed |
| External actions | none: no network call, no `docker`, no `fly`, no migration, no `refresh_tips`, no publish, no commit |
| Suite | 654 tests, 1 skipped (`manage.py test alltips_scraper --noinput`); unchanged, so `docs/RUNBOOK.md` section 7 still states it |
| `manage.py check` | no issues |
| `manage.py makemigrations --check --dry-run` | no changes detected |
| `git diff --check` | clean |

### 11.1 The drifts the audit corrected

* **The versioned route was described as write-gated** (`docs/DEPLOYMENT.md`
  section 6): it answered "from the stored snapshot" and returned `503` "until a
  snapshot has been written". The deployed reader is not a table — `jsoncontent_v1`
  serves the published content the image ships. The legacy database-backed writer is
  not the installed v1 publication mechanism, and `manage.py refresh_tips` is
  refused against this reader (exit status `3`). `503` `source_unavailable` is the
  answer for a type key the shipped manifest does not name, and the tracked manifest
  is still the empty map, so the correction keeps the status code and fixes the
  mechanism.
* **A merged duplicate** (`docs/DEPLOYMENT.md` section 1): two consecutive bullets
  both said "no scheduler, no Celery" and each carried half of the same fact. They
  are one bullet now, keeping both halves — the endpoint is served from published
  content, and the writer is refused here.
* **Connection options that no longer exist** (`docs/DEPLOYMENT.md` section 10): the
  bullet stated `CONN_MAX_AGE = 0` and "disabling server-side cursors and prepared
  statements" as this image's settings. There is no such setting: `CONN_MAX_AGE`,
  `DISABLE_SERVER_SIDE_CURSORS` and `OPTIONS['prepare_threshold']` were the pooled
  PostgreSQL path and went with it, as `odds/odds/settings.py` records under
  "Database". The corrected bullet says what applies — one SQLite connection per
  request, Django's own default — and keeps the backlog note.
* **One gap the audit added** (`docs/DEPLOYMENT.md` section 10): the image excludes
  `odds/db.sqlite3`, and no volume or managed database shares state between
  machines. The configured release-command migration runs on a one-off machine's
  ephemeral filesystem, so any SQLite schema or data it creates is not available to
  a separate serving machine. The health and published-content read paths do not
  need a database, but `/admin/` and session- or auth-dependent paths are not
  deployment-ready without an explicit persistence decision. This is configuration
  review, not a completed container or Fly verification.

### 11.2 What was checked and deliberately left alone

* **The counts still hold.** *(Superseded in part — §13: the count changed.)* The suite was re-run from the repository-root `.venv`
  and reported the same 654 tests and 1 skipped that `docs/RUNBOOK.md` section 7 and
  section 10 of this document record, so no count was edited anywhere.
* **One stale count was observed and corrected.** *(Superseded in part — §13: §7 now reads "the 27 deployment tests".)* `docs/RUNBOOK.md` section 7
  labelled the deployment-contract module with 15 tests; the module holds 19 —
  `manage.py test alltips_scraper.tests_deployment_v1` reported `Ran 19 tests`,
  `OK`. That label now reads "the 19 deployment tests": it was corrected in the same
  commit as this record, and the earlier sections of this document are left as they
  were.
* **This record is the only addition to this document**, appended after section 10
  rather than folded into it: sections 8, 9 and 10 record what those sprints did and
  are left as they were.
* **Nothing else was audited away.** The deferred items in `docs/DEPLOYMENT.md`
  section 10 — no volume, no managed database, no `CSRF_TRUSTED_ORIGINS`,
  forward-only migrations, the template `fly.toml` *(superseded in part — §13)*, the
  unmade image — are
  unchanged.

## 12. Initial static-v1 deployment scope declared (2026-10-10)

Sprint 1K recorded what the approved first deployment is, so that the static v1
architecture is stated in the documents rather than inferred from them. It is a
documentation sprint: `docs/DEPLOYMENT.md` (a new §1.1 and one sentence in §10),
`docs/RUNBOOK.md` (a new §11.1), `docs/API_V1_CONTRACT.md` (a scope line in the
header and one bullet in §1) and `README.md` (one callout) changed, beside this
record. No application code, URLconf, setting, installed app, middleware, template,
management command, `Dockerfile`, `.dockerignore`, `fly.toml`, dependency,
migration, content artifact, manifest, test or endpoint behaviour changed.

| Item | Value |
| --- | --- |
| Branch | `main` at `1d2ced6d5e2e470ae2563ea63ff7324b41941f77`, one commit ahead of `origin/main` (`77a6ffcb21668545da2f0c4aefd00a109a4d5a81`), clean working tree and empty index |
| Scope | `docs/DEPLOYMENT.md` §1.1 (new) and §10; `docs/RUNBOOK.md` §11.1 (new) and the §7 label; `docs/API_V1_CONTRACT.md` header and §1; `README.md`; this record and the corrected §11.2 bullet |
| Deployment performed | **no** — no image was built, no container was run, no `fly` command was executed, nothing was served, and no deployment occurred |
| External actions | none: no network call, no `docker`, no `fly`, no migration, no `refresh_tips`, no scrape, no publication, no staging and no commit |
| Suite | `Found 654 test(s)` — `Ran 654 tests in 5.478s`, `OK (skipped=1)`, so the count `docs/RUNBOOK.md` §7 states still holds |
| Deployment-contract module | `Found 19 test(s)` — `Ran 19 tests in 2.083s`, `OK` |
| `manage.py check` | `System check identified no issues (0 silenced).` |
| `manage.py makemigrations --check --dry-run` | `No changes detected` |
| `manage.py check --deploy` | `System check identified no issues (0 silenced).`, run with `DEBUG=False` and the six transport values set; the process-scoped overrides were removed again after the run |
| Shipped manifest | `odds/alltips_scraper/content/v1/manifest.json` unchanged: 38 bytes, `{"schema_version": 1, "snapshots": {}}`, sha256 `ec7e91f60d5ee0ef73754ea87adf2686bf251781e39308788bc5f361773e3b63` |
| `git diff --check` | clean |

### 12.1 What the declared scope is

* **The first deployment's public surface is `/api/health/` plus
  `GET /api/v1/tips/`.** The versioned path answers from the reviewed canonical
  JSON committed under `odds/alltips_scraper/content/v1/` and shipped inside the
  image, and at runtime that reader is local, offline and database-free: no
  database, no cache, no network, no scheduler.
* **Publication is a review-time loop, never a runtime path:** validate a candidate
  offline (`manage.py validate_v1_content`, `docs/RUNBOOK.md` §4.3), review it,
  commit the payload with its manifest entry, then build a new image and redeploy.
  `manage.py refresh_tips` is not an in-container publisher; against the read-only
  provider this image installs it is refused with exit status `3`.
* **The empty manifest is intentional.** A supported type key that no reviewed
  publication has named is answered `503` `source_unavailable` by design.
* **Deferred, with no architecture selected:** authentication and sessions, the
  admin, payments and entitlements, any cache or shared store (Fly volume, managed
  database, Redis/Upstash), a scheduler or worker, and any runtime content writer.
  No persistence architecture was chosen; `odds/db.sqlite3` stays a local,
  git-ignored file and the gap in `docs/DEPLOYMENT.md` §10 stays open.
* **The six legacy routes are frozen, routable and outside the static-v1
  guarantee.** They can perform synchronous upstream work inside a request, so this
  scope is not a claim that the whole public service is offline; their production
  exposure stays a separately deferred contract and security decision.

### 12.2 What deliberately did not change

* **No route, app or middleware was removed or added.** `/admin/` is still routed,
  and `django.contrib.admin`, `auth`, `sessions`, `messages` and `contenttypes` are
  still installed.
* **`fly.toml`, the `Dockerfile` and `.dockerignore` are untouched**, including the
  `release_command` that file already carried: this sprint neither changed it nor
  exercised it in a container, and no image was built.
* **No persistence direction was selected.** The deferral is recorded, not decided:
  no volume, no managed database, no Redis, no session backend was added.
* **No image or Fly verification is claimed.** The gaps in `docs/DEPLOYMENT.md`
  §10 remain configuration review.
* **The stale deployment-test label is gone.** *(Superseded in part — §13.)* `docs/RUNBOOK.md` §7 reads "the 19
  deployment tests", and the §11.2 bullet above is corrected from "left alone" to
  "corrected", so no document presents the old label as current.

## 13. Fly identity selected — committed, not deployed (2026-10-11)

Sprint 1O-1P replaces the placeholder Fly identity with one committed identity, so
the first deployment starts from a decision this repository has already made instead
of a name an operator invents at the console: `app` `sure-tips-api-mate`,
`primary_region` `iad`, host `sure-tips-api-mate.fly.dev`. **No Fly app has been
created** from it: no `fly apps create`, no `fly secrets set`, no image build, no
deploy and no production request happened, so every identity value here is a
decision, never an observation of Fly.

It is one atomic change set — `fly.toml`, the deployment tests that pin the files,
and the documents that state the identity — because landing any one of them alone
would leave `main` describing an identity the other files still call unresolved.

| Item | Value |
| --- | --- |
| Branch | `main`; the change set is **uncommitted**, so `HEAD` is still `8c49f44` and `origin/main` is unchanged |
| Scope | `fly.toml` (comments only; the three value lines are byte-identical), `odds/alltips_scraper/tests_deployment_v1.py`, `README.md`, `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, this record |
| Deployment performed | **no** — no image was built, no container was run, no `fly` command was executed, nothing was served, no Fly resource was created, and the committed host was never requested |
| External actions | none: no network call, no `docker`, no `fly`, no secret, no migration, no scrape, no publication, no commit and no push |
| Identity | `app` `sure-tips-api-mate`, `primary_region` `iad`, `[env] ALLOWED_HOSTS` = `sure-tips-api-mate.fly.dev` followed by the three local hosts |
| Retired spellings | the four angle-bracket identity placeholders this sprint removed are gone from all five files; `<previous-version>` stays, because it is a genuine metavariable of the documented rollback command |
| Suite | `Found 662 test(s)` — `Ran 662 tests in 3.991s`, `OK (skipped=1)`; 654 before this sprint, and `docs/RUNBOOK.md` §7 states the new count |
| Deployment-contract module | `Found 27 test(s)` — `Ran 27 tests in 1.486s`, `OK`; 19 before this sprint |
| `manage.py check` | `System check identified no issues (0 silenced).` |
| `manage.py makemigrations --check --dry-run` | `No changes detected` |
| `manage.py check --deploy` | `System check identified no issues (0 silenced).` — run with `DEBUG=False`, the six transport values, and a throwaway secret key with `.invalid` email and scrape inputs; no process-scoped value was left set after the run |
| `git diff --check` | clean |

### 13.1 What the identity is, and what it is not

* **One app, one region, one host, committed together.** `fly.toml` declares
  `app = "sure-tips-api-mate"`, `primary_region = "iad"` and
  `ALLOWED_HOSTS = "sure-tips-api-mate.fly.dev,localhost,127.0.0.1,[::1]"`. The host
  is pinned as derived from the app name, so a name and a Fly host that could not
  belong to the same app fail the suite instead of shipping.
* **No wildcard host.** `ALLOWED_HOSTS` keeps its local entries so the same image can
  be run and curled on a developer machine, and it carries no `*`.
* **The size is the remaining decision.** No `[[vm]]` block and no size, memory or
  CPU key is committed: the machine size is still chosen at creation and revisited
  once real load is known.
* **Nothing else in the file moved.** Only the comments around the identity were
  rewritten; `app`, `primary_region`, `ALLOWED_HOSTS`, the `[env]` non-secrets, the
  six transport values, `release_command`, `[http_service]` and the health check are
  unchanged, so the deployment contract itself did not change.
* **No Fly-side fact is claimed.** Whether the account can create the name, whether
  the app already exists and whether the host resolves are all unverifiable from this
  repository; `docs/RUNBOOK.md` §11.2 keeps them as unresolved prerequisites.

### 13.2 What deliberately did not change

* **No resource, no secret, no deploy.** No app was created, no secret was set, no
  image was built, no machine was started and no request reached Fly.
* **The deferrals stay deferred.** `docs/DEPLOYMENT.md` §10 and §1.1 still record no
  volume, no managed database, no Redis/Upstash, no scheduler or worker, no
  authentication, no payments, no `CSRF_TRUSTED_ORIGINS` and forward-only
  migrations. Section 12 of this document is unchanged.
* **The service is untouched.** No route, view, setting, middleware, template,
  management command, migration, dependency, content artifact or manifest changed:
  this sprint changed a name, the tests that pin it, and the prose that repeats it.
* **Neither workflow was touched.** `.github/workflows/django-tests.yml` and
  `.github/workflows/image-build-smoke.yml` are byte-identical and stay test-only —
  no Fly token, no registry login, no deploy step and no write permission — which
  `WorkflowCapabilityTests` now pins directly rather than by review.
* **Green still deploys nothing.** A gated commit is eligible, not released, and the
  manual procedure in `docs/RUNBOOK.md` §11.2 is unchanged in shape: precondition 7
  now confirms a committed identity instead of filling in a template.
* **The placeholder-era records are marked, not rewritten.** The Fly bullets in §7.1
  and §7.3, and the §11 note that listed "the template `fly.toml`" as unchanged, now
  carry an inline superseded marker pointing here, following the §8 precedent; the
  sprint records themselves are left as they were.

## 14. Candidate preparation tool — prepared offline, never published (2026-10-30)

A resume of the candidate-preparation work: an offline, review-time command that
writes one validated canonical v1 record, so a reviewer no longer spells the digest,
the record file and the manifest entry by hand. The artifact rules are pure
(`alltips_scraper/candidate_v1.py`), the filesystem work belongs to the command
(`alltips_scraper/management/commands/prepare_v1_candidate.py`), and both are pinned
by `alltips_scraper/tests_prepare_v1_candidate.py`. **No deployment or external
action was performed during this sprint.**

| Item | Value |
| --- | --- |
| Branch | `main` at `2608440d30cd3239531549fbadc8341b363bdf60` ("Repair the Fly health check Host and X-Forwarded-Proto headers"), two commits ahead of `origin/main` (`23cdd80`, "Select Fly deployment identity"); the recovery found the three sprint files untracked and no tracked file modified |
| Scope | `alltips_scraper/candidate_v1.py`, `alltips_scraper/management/commands/prepare_v1_candidate.py`, `alltips_scraper/tests_prepare_v1_candidate.py`, `docs/RUNBOOK.md` §4.4, `docs/DEPLOYMENT.md` §10, `docs/API_V1_CONTRACT.md` §6 and §7, `alltips_scraper/content/v1/README.md`, and this section |
| Interpreter | CPython 3.12.13 in `odds/.venv` (`odds\.venv\Scripts\python.exe -V` → `Python 3.12.13`) |
| Deployment performed | **no** — nothing was built, installed, migrated, served or deployed |
| External actions | none: no network call, no scraping, no `fly` command, no login, no secret read or change, no migration run, no publication, no commit or push |
| Suite | `Found 702 test(s)` — `Ran 702 tests in 8.245s`, `OK (skipped=1)`; this includes the 31 tests of the new module. A first verbose attempt was interrupted by the terminal harness before it reported and produced no result, so the recorded figure is the completion of the run that followed it |
| Targeted evidence of the repair | recorded before this resume, in `tmp/prepare_tests.txt` and `tmp/prepare_tests2.txt`: `Ran 31 tests in 0.766s` and `Ran 31 tests in 0.978s`, both `FAILED (failures=6)`. After this resume's corrections: `Ran 31 tests in 1.308s`, `FAILED (failures=1)`; then `Ran 31 tests in 1.143s`, `FAILED (failures=1)` at a further assertion. That last assertion was corrected and is covered by the passing full-suite run below; no third targeted run was made |
| `manage.py check` | `System check identified no issues (0 silenced).` |
| `manage.py makemigrations --check --dry-run` | `No changes detected` |
| Synthetic round-trip | `prepare_v1_candidate --type bet_of_the_day --fetched-at 2026-10-30T12:00:00+00:00 --out tmp/resume_rt/c` → `manifest status=ok entries=1`, `type=bet_of_the_day status=ok`, exit `0`; the produced `manifest.json` (274 bytes) then validated independently with `validate_v1_content --root tmp/resume_rt/c` → the same two lines, exit `0` |
| `git diff --check` | clean |
| Shipped manifest | `odds/alltips_scraper/content/v1/manifest.json` unmodified: 38 bytes, `{"schema_version": 1, "snapshots": {}}`, sha256 `ec7e91f60d5ee0ef73754ea87adf2686bf251781e39308788bc5f361773e3b63` |

### 14.1 What the tool is

* **Preparation is not publication.** One run writes two files into `--out` and
  nothing else: `<type_key>.json` holding the payload in the canonical byte form its
  own digest is taken over plus one newline, and `manifest.json` holding every entry
  the directory already named plus the new one. It commits nothing, deploys nothing,
  refreshes nothing and serves nothing.
* **Four options, all required, no defaults**: `--payload`, `--type`, `--fetched-at`
  and `--out`. A missing option, a path of the wrong kind, or an `--out` that is the
  packaged content root, a descendant or an ancestor of it is refused with exit
  status `2`, before anything is written.
* **Exit status** `0` when the candidate is in place and validates, `1` when the run
  was refused and this run left final output unchanged after any recovery it needed
  completed successfully, `2` when the invocation cannot be served, `3` when the run
  cannot vouch for the directory - output may have changed or a recovery could not be
  confirmed, so inspection is required and no rollback is promised - and `0` for help.
  Only `1` claims an unchanged directory and only `0` a valid one (see §15).
* **The report is the validator's**, printed verbatim for the directory as it stands
  after the run: the manifest line, then one `type=<token> status=ok` line per entry
  in sorted order. A refusal prints one fixed line - `candidate status=refused
  reason=<token>` or `type=<token> status=refused reason=<token>` - and every type key
  reaching a line passes through `reporting_v1.safe_token`. A run that cannot vouch for
  the directory ends with `candidate status=inspect reason=<token>` instead (§15).
* **An existing entry is never replaced.** A byte-identical rerun is a no-op: both
  files keep their bytes and their modification times. A different entry for the same
  key is `entry_conflict`, a different payload under an existing record name is
  `file_conflict`, and a replacement stays a reviewed edit to the manifest.
* **Stage, prove, then finalise.** A staging directory inside `--out` receives the
  record and the merged manifest, the records the manifest already names are copied in
  beside them, and the staged directory is validated twice - with
  `validate_content_root` and with the reader's own `JsonSnapshotProvider` - before
  the record and then the manifest are moved into place with one `os.replace` each. A
  manifest that cannot be replaced takes the record that run added back out, and a
  directory the run created is removed, so a refusal leaves the directory as it was
  found - when that recovery completes. A record that cannot be taken back, staging
  that cannot be removed, or a created directory that cannot be removed is reported as
  `status=inspect` with exit `3` instead (§15). A staging directory an earlier run left
  behind is never deleted by a later one - a run removes only what it created - so it
  stays where it is: with no manifest beside it, its contents make the directory
  unusable (`out_not_empty`), and beside a valid manifest the validator ignores it, so
  it may remain there ignored and undiagnosed until a reviewer removes it. Only staging
  this run created and could not remove is the reported case.
* **Limits.** Atomicity is per file rather than transactional over the directory, two
  runs are not coordinated with each other (the review is the serialisation), and the
  command writes only inside the directory it is handed.

### 14.2 What this sprint repaired

* **One implementation defect.** Finalisation replaced the manifest unconditionally,
  so a rerun that planned identical bytes still rewrote `manifest.json` and moved its
  modification time, contradicting the no-op rerun the module documents. Finalisation
  now compares the planned manifest bytes with the file's and skips the replace when
  they already match (`_manifest_replace_needed`), which is what the rerun test and
  the round-trip observe.
* **Six defects in the module's own tests**, none of which changed a product rule: an
  assertion that required a plan's manifest to equal the empty manifest and to contain
  its entry at the same time; three key-order assertions made against the whole
  document, where the top-level `schema_version` precedes the entry's; an AST helper
  that recorded nothing for `from ... import candidate_v1`; a mock that identified the
  manifest by an unresolved path string and so never refused it; two expectations that
  compared `sorted(...)` with an unsorted literal; and a report expectation that
  omitted the entry a second run carries over.
* **One test rewritten to stay meaningful.** The failed-replacement test now adds a
  second type key, so the manifest genuinely has to be replaced before it can fail: a
  rerun that plans identical bytes no longer attempts a replace at all, and the test's
  subject - the record a run added being taken back - is exercised as written.

### 14.3 What deliberately did not change

* **No wire shape, route, view, setting, dependency or migration.** `fly.toml`, the
  Dockerfile, both workflows, the six legacy routes and the versioned endpoint are
  untouched: the API contract gained documentation, not behaviour.
* **The packaged content is byte-identical**, including the tracked empty
  `manifest.json`; no payload was added, and the preparation command refuses the
  packaged root, anything inside it and any ancestor of it.
* **Nothing runs it.** The release command, gunicorn, the health check, the image build
  and both workflows never invoke the preparation command or the validator.
* **No scheduler, no automation, no commit.** Publication is still a reviewed commit
  plus a redeploy; this sprint added the tool that prepares a record and the documents
  that describe it.

### 14.4 Open gap this leaves

Publication remains manual: preparing a candidate writes files into a directory the
reviewer names, and nothing schedules, queues, promotes or verifies a prepared record
afterwards. Two preparations are not coordinated with each other, the directory's
atomicity is per file, and whether the deployed reader serves a type key is still
decided by the manifest a reviewer commits - not by this command, which only refuses
what the reader could not load.

## 15. Candidate preparation: a run that cannot vouch for `--out` says so (2026-10-30)

A follow-up repair to §14. The command answered every directory-side refusal with
`candidate status=refused` and exit `1`, a pair the documents read as "`--out` was left
as it was found". That claim was not always true. Finalisation installs the record and
then the manifest, and takes the record back out when the manifest cannot be replaced;
the reclamation, the removal of a staging directory and the removal of a directory the
run created are each a filesystem operation that can fail in turn. When one did, the run
still printed the refusal line and exited `1`, though `--out` might now hold a record
its manifest does not name, or a leftover directory this run knows about. A directory
that stopped validating after installation had the same problem from the other side:
the files were installed, so calling the run refused denied the change it had made.

Both outcomes now answer `candidate status=inspect reason=<token>` with exit `3`, and
that status word - not the token - carries the meaning: the directory has to be looked
at before it is reused. **No deployment or external action was performed during this
repair.**

| Item | Value |
| --- | --- |
| Branch | `main` at `2608440d30cd3239531549fbadc8341b363bdf60` ("Repair the Fly health check Host and X-Forwarded-Proto headers"); the repair is uncommitted working-tree state on top of the untracked §14 files - no commit, no push |
| Scope | `alltips_scraper/management/commands/prepare_v1_candidate.py`, `alltips_scraper/tests_prepare_v1_candidate.py`, `docs/RUNBOOK.md` §4.4, `docs/API_V1_CONTRACT.md` §7 (comment), §14.1 above and this section |
| Interpreter | CPython 3.12.13 in `odds/.venv` (`odds\.venv\Scripts\python.exe`) |
| Deployment performed | **no** — nothing was built, installed, migrated, served or deployed |
| External actions | none: no network call, no scraping, no `fly` command, no login, no secret read or change, no migration run, no publication, no commit or push |
| Suite | `Found 712 test(s)` — `Ran 712 tests in 8.440s`, `OK (skipped=3)`; the three skips are the three link tests - the two new prepare-command tests and the existing `tests_contentcheck_v1.py` link test - all skipped because this account cannot create a link. As the later run printed them: `symbolic links need a privilege this account lacks` twice and `this platform does not let a test create a link` once. Neither is the operating system's error, and this log recorded none, so the `WinError 1314` attribution once made here is withdrawn. The runner's exit code was not read for this run, and no `0` is assigned to it (§15.3) |
| Targeted | `Ran 41 tests in 1.654s`, `OK (skipped=2)` — the module's ten new tests included, both skips the same symlink privilege. Re-run after the documentation edits: `Ran 41 tests in 1.404s`, `OK (skipped=2)`. Both are runner summaries; no process exit code was read for either |
| `manage.py check` | `System check identified no issues (0 silenced).` |
| `git diff --check` | clean |
| Shipped manifest | `odds/alltips_scraper/content/v1/manifest.json` unmodified: 38 bytes, `{"schema_version": 1, "snapshots": {}}`, sha256 `ec7e91f60d5ee0ef73754ea87adf2686bf251781e39308788bc5f361773e3b63` |

### 15.1 What changed

* **A third answer, and only for a directory.** `OPERATIONAL_RETURNCODE = 3` and
  `INSPECT_LINE = 'candidate status=inspect reason={reason}'`, printed with
  `INSPECT_MESSAGE`. The refusal vocabularies are unchanged: the tokens still say what
  stopped the run, and no new token was invented for "something else went wrong".
* **Cleanup decides the outcome, after it has run.** `_finalise` now returns
  `(refusal, inspection_required)`: a record that could not be taken back out - because
  it could not be unlinked, or because this run installed it and the unlink failed -
  asks for inspection, while a record this run never installed does not.
  `_stage_and_finalise` returns the same pair and sets it when the staging directory it
  created is still there afterwards, or when the output directory it created could not
  be removed. A run whose write phase failed before any rename stays a plain refusal.
* **Finalisation is settled from the directory, not from the plan.**
  `_report_and_finish` prints the validator's report for `--out` and answers a directory
  that no longer validates with `candidate_unusable` on the inspect line, exit `3`; an
  otherwise good report still answers `write_failed` on the inspect line when this run's
  cleanup did not finish. `_refusal` chooses the inspect answer before either refusal
  form, so one incomplete recovery cannot be reported as a state of one artifact.
* **Ten tests, covering each new guarantee and each way it can fail.** Unrelated files are
  preserved; a record name colliding with a file and with a directory is refused; a
  record name resolving outside `--out` is refused; the shipped root is refused through
  a `..` alias and through a symlink; a staged candidate that does not validate leaves
  no trace; an unreclaimable record, an unremovable staging directory and an unremovable
  created directory each answer `status=inspect` with exit `3`; a directory that stops
  validating after installation reports the validator's findings and then the inspect
  line; and the vocabularies stay complete and disjoint. Eight of the ten ran on this
  account: the two link tests skipped before asserting anything, so neither the
  record-name link refusal nor the shipped-root-through-a-link refusal is verified
  here.

### 15.2 What deliberately did not change

* **Exit `1` still means an unchanged directory, and is still the ordinary answer.** The
  existing refusal tests are untouched and still pass: a conflict, a bad rerun, an
  unusable `--out` and a failed write that was fully reclaimed all answer `status=refused`
  with exit `1`.
* **Idempotence is unchanged.** `_manifest_replace_needed` still decides whether the
  manifest is written at all, so a byte-identical rerun writes nothing and moves no
  modification time - and a run that replaced nothing has nothing to reclaim.
* **The HTTP API is untouched, and the management command gained one answer and one
  line.** No view, URL, serializer, response shape or status code changed, and the
  shipped content tree is byte-identical. The command's four options, and the rule that
  none has a default, are unchanged, as are the reader, validator, writer and packaging
  modules. What changed is the management-command interface: `0`, `1` and `2` keep the
  meanings §14.1 gave them, exit `3` is new, and a run that cannot vouch for `--out` now
  ends with `candidate status=inspect reason=<token>`. No new reason token was invented
  for it: the inspect line carries one of the two existing directory tokens -
  `candidate_unusable` when the directory no longer validates after installation, and
  `write_failed` when the installation stands but this run's cleanup did not finish - so
  `INVOCATION_REFUSALS` and `DIRECTORY_REFUSALS` are exactly as they were. Only private
  helper signatures inside the command changed, and `_report_and_finish` gained a
  defaulted argument so its call sites read as before.
* **Nothing runs it and nothing publishes.** The release command, gunicorn, the health
  check, the image build and both workflows still never invoke the preparation command.

### 15.3 The final full-suite run, and the exit code it returned (2026-10-30)

The suite figures in §15 are runner summaries: the process exit code behind them was
never read, and this section assigns none of them a retroactive `0`. The run below was
started to supply exactly that measurement. It is recorded as it came out, and it is
**not green**.

| Item | Value |
| --- | --- |
| Interpreter | `odds\.venv\Scripts\python.exe`, with the working directory `d:\Dev\projects\sure_tips` — lower-case drive letter, which is what the failure below turns on |
| Command | `odds\manage.py test alltips_scraper -v 2 --noinput` — no option added; the only database created and destroyed is the runner's own in-memory test database |
| Start / end (UTC) | `2026-10-30T19:56:11.238Z` / `2026-10-30T19:56:27.651Z`; 16.414 s wall |
| Native exit code | **`1`** — read from the child process object after it exited and both redirected streams were closed, not inferred from the terminal or the harness |
| Runner summary | `Found 712 test(s)` — `Ran 712 tests in 9.461s`, `FAILED (failures=1, skipped=3)`; `System check identified no issues (0 silenced).` |
| Failure | `tests_startup_v1.AppConfigSelectionTests.test_the_config_loads_from_the_bare_app_entry` (`tests_startup_v1.py:232`): `'d:\Dev\projects\sure_tips\odds\alltips_scraper' != 'D:\Dev\projects\sure_tips\odds\alltips_scraper'` — one directory, two spellings |
| Skips | the same three link tests, with the words this run printed: `symbolic links need a privilege this account lacks` twice, `this platform does not let a test create a link` once |
| Sprint sources | unchanged across the run — `candidate_v1.py` `db53c945…9c92f9`, `prepare_v1_candidate.py` `db089e05…b3eca1`, `tests_prepare_v1_candidate.py` `0d2cd772…a7d960`, before and after |
| Packaged content | unchanged — `manifest.json` 38 bytes `ec7e91f6…773e3b63`, `README.md` 7131 bytes `43de1c5f…bfdd75` |
| `git diff --check` | clean before this section, and re-run clean after it |
| Evidence | `tmp/sprint_a_native_exitcode_20261030_run2/`: `python_stdout.txt`, `python_stderr.txt`, `process_metadata.json` (written only after the process exited), `source_hashes_pre/post.txt`, `content_inventory_pre/post.txt`, and the runner script. The sibling `tmp/sprint_a_native_exitcode_20261030/` records a first attempt that stopped in its own launcher before Python started; it produced no suite result and is kept as it failed |

**What the one failure is, and what it is not.** The assertion is
`self.assertEqual(config.path, str(APPS_PATH.parent))`, and both sides name the same
directory. `APPS_PATH` is resolved with `Path.resolve()`, which on Windows reports the
drive letter in upper case; `AppConfig.path` carries the `sys.path` entry the interpreter
built from the process's working directory, and that string keeps whatever case the
caller passed. This run was started with a lower-cased `d:` in its working-directory
argument, so the two spellings diverged and the assertion failed. The earlier passing
runs in this document were started from a shell whose prompt reads
`D:\Dev\projects\sure_tips`.

That makes the failure an artefact of how this measurement was taken rather than a
change in behaviour: it touches no sprint file, `tests_startup_v1.py` is untouched by the
sprint, and the whole suite is otherwise `ok`. It is also a real fragility in an older
test — the assertion compares two spellings of one path instead of the two paths, so it
depends on the case of the caller's working directory. Neither repair is made here.

**What this leaves open.** No second run was authorised after this result, so this
document does not claim a green suite behind the hardening. The verified record remains
the earlier `Ran 712 tests in 8.440s`, `OK (skipped=3)`, whose exit code was not captured;
the `1` above belongs to a run whose single failure is environmental. Closing the gap
takes one re-run from a `D:`-spelled working directory, which this record does not
perform and which nothing changed in this sprint is evidence for.

**The controlled re-run, and what the spelling explains (2026-10-30, same day).**
One further run was authorised to test the explanation above. It ran the same single
command with nothing else changed, except that every path handed to the process carried an
upper-case drive letter: the interpreter
`D:\Dev\projects\sure_tips\odds\.venv\Scripts\python.exe`, the script argument
`D:\Dev\projects\sure_tips\odds\manage.py`, and the working directory
`D:\Dev\projects\sure_tips`. Both carriers that the failing run had spelled with a
lower-case `d:` were therefore upper case here.

| Item | Value |
| --- | --- |
| Native exit code | **`0`** — read from the child process object after it exited with both redirected streams drained, the same way as above |
| Runner summary | `Found 712 test(s)` — `Ran 712 tests in 11.075s`, `OK (skipped=3)`; `System check identified no issues (0 silenced).` |
| Previously failing test | `tests_startup_v1.AppConfigSelectionTests.test_the_config_loads_from_the_bare_app_entry` — `ok` |
| Start / end (UTC) | `2026-10-30T20:16:42.707Z` / `2026-10-30T20:17:02.542Z`; 19.836 s wall |
| Skips | the same three link-capability skips, in the same words: `symbolic links need a privilege this account lacks` twice, `this platform does not let a test create a link` once |
| Sources and content | the three hashes above, unchanged before and after; packaged inventory unchanged — `manifest.json` 38 bytes `ec7e91f6…773e3b63`, `README.md` 7131 bytes `43de1c5f…bfdd75` |
| Evidence | `tmp/sprint_a_uppercase_d_rerun_20261030/`: `runner_started.txt`, `launcher_note.txt`, `python_started.json`, `python_stdout.txt`, `python_stderr.txt`, `process_metadata.json`, `source_hashes_pre/post.txt`, `content_inventory_pre/post.txt`, and that run's runner script |

The gap described above is closed by measurement rather than argument: with the drive
letter upper case on the paths the process is given, the assertion passes and the suite
is green. That supports the explanation - the failure tracked the spelling supplied to
the process, not any behaviour under test - with one limit worth stating: the working
directory and the script argument were both upper case here and both lower case there, so
this run does not say which of the two carried the spelling into `config.path`. The older
test is spelling-sensitive and stays that way; it was not modified, and this section does
not call it fixed. The three skips are link-capability skips and say nothing about either
symlink behaviour: neither the record-name link refusal nor the refusal of the shipped
root reached through a link is verified on this account.
