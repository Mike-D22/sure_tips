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
* `check --deploy` hardening (HSTS / SSL redirect / secure cookies) — deployment
  sprint.
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
