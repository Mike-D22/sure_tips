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
* **Fly configuration.** `fly.toml`: a placeholder app name and region,
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
* No filled-in Fly identity: `fly.toml` keeps placeholders for `app` and
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
