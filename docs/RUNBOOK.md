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

## 5. Caching semantics

* Decorator: `alltips_scraper.decorators.cache_matches`.
* Key: `legacy_api:v1:<view_name>:<YYYY-MM-DD>[:<query fingerprint>]`.
* TTL: 14,400 s (4 h), unchanged from the original implementation.
* Backend: Django's default `LocMemCache` (per process — restarting the server or
  running extra workers gives each process its own cache).
* The key includes the local date, so yesterday's tips can never be served today
  and no manual flush is needed at midnight.
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

Generate a secret key with:

```powershell
.\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

## 7. Verification commands

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py check                              # expect: no issues
.\.venv\Scripts\python.exe .\odds\manage.py migrate                            # currently: no migrations to apply
.\.venv\Scripts\python.exe .\odds\manage.py test alltips_scraper -v 2 --noinput # expect: 8 tests
```

The app label is required. `odds/` is not a Python package, so a bare
`manage.py test` runs Django's discovery against the repository root, finds
**0 tests** and prints `NO TESTS RAN`.

The tests mock every scraper, so they are safe to run offline and never touch
`freesupertips.com`. One of them,
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
| `uv venv` exits non-zero: "already exists" | the repository-root `.venv` is present; use `uv venv --clear --python 3.12 .venv` to rebuild. |
| PowerShell refuses to run `Activate.ps1` | not needed — call the interpreter directly (`.\.venv\Scripts\python.exe`); never activate a virtual environment or change the execution policy. |

## 10. Repository hygiene rules

* Never commit the repository-root `.venv/`, the legacy `odds/.venv` or
  `myvenv/`, `__pycache__/`, `*.pyc`, `odds/db.sqlite3` or any `.env` file —
  all are covered by the root `.gitignore` and `odds/.gitignore`.
  `.env.example` **is** tracked (it is documentation).
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

