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
provider is installed, and section 4.3 covers the review-time validation of a
candidate change to that content.

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

### 4.3 Reviewing a candidate content change (`manage.py validate_v1_content`)

`GET /api/v1/tips/` is served from the reviewed canonical content the image ships
(section 4.1). Preparing a change to that content is a manual, reviewed change to
`odds/alltips_scraper/content/v1/`, and the read-only, offline validator is what a
reviewer runs before committing it:

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py validate_v1_content --root <candidate-directory>
```

* `--root DIRECTORY` names the candidate content directory, and it is required:
  there is no default, because the directory this package ships is a deployment's
  content decision rather than a fallback for a review. A run without it is refused
  rather than defaulted, so a mistyped invocation can never be mistaken for a
  validation of artifacts an operator did not name.
* The command is offline and read-only. It reads no configuration value, touches no
  database and no cache, reaches no network and writes no file: it does not publish,
  rewrite or create a content artifact, and it never touches `content/v1/` unless
  that directory is the one it was handed.
* It judges the candidate against the reader's own rules (`jsoncontent_v1`) and in
  the reader's own vocabulary, plus the one candidate-only check below. Registry
  membership is deliberately not checked: whether the versioned registry publishes
  a type key, whether the payload describes tips and whether the instant is recent
  are the serializer's and the reviewer's questions, so the validator never refuses
  a candidate the deployment would serve.
* A missing manifest in an explicit candidate root is refused as
  `manifest_missing`. That state is deliberately not the reader's empty deployment:
  "nothing is published" is a valid deployment and never a valid candidate to
  review.
* The report is deterministic - the manifest line first, then one line per type key
  in sorted order - and every type key is sanitised by the same rule the writer
  uses (`reporting_v1.safe_token`, re-exported by `refresh_v1`), so a key read out
  of a candidate file cannot forge a second line or a second field:

  ```text
  manifest status=ok entries=<n>
  type=<token> status=ok
  type=<token> status=refused reason=<token>
  manifest status=refused reason=<token>
  ```

* The exit status is `0` when the candidate is usable - which includes a valid
  manifest that names no entry at all - `1` when the manifest or one or more of the
  artifacts it names was refused, and `2` when the invocation itself cannot be
  served: `--root` was not given (`root_not_given`) or does not name a directory
  (`root_not_a_directory`), in which case no artifact is checked and no report line
  is printed at all. That `2` is the invocation's own status; it means something
  different from the writer's `2` in section 4.2, which reports an unusable
  `--type` name.
* A refusal names only fixed tokens: no line carries a path, a file name, a digest,
  a payload value, an instant or an exception, and the report is printed in full
  before the exit status is decided, so a reviewer sees the whole list of what has
  to be fixed.

### 4.4 Preparing a candidate content change (`manage.py prepare_v1_candidate`)

Section 4.3 validates a candidate that already exists. The preparation command writes
one, so the reviewer no longer spells the mechanical parts - the canonical digest, the
record file and the manifest entry - by hand:

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py prepare_v1_candidate `
  --payload <payload-file> --type <type-key> `
  --fetched-at <instant> --out <candidate-directory>
```

* **Preparation is not publication.** A run writes two files into `--out` and nothing
  else: `<type-key>.json`, holding the payload in the canonical byte form its own
  digest is taken over plus one newline, and `manifest.json`, holding every entry the
  directory already named plus the new one. It does not commit, deploy, refresh or
  serve anything. The packaged content under `odds/alltips_scraper/content/v1/`,
  including its tracked empty manifest, is unchanged unless a reviewer commits the
  prepared files deliberately.
* **All four options are required and none has a default**: `--payload FILE`,
  `--type TYPE_KEY`, `--fetched-at INSTANT` and `--out DIRECTORY`. A run missing one
  of them is refused rather than defaulted, so a mistyped invocation can never be
  mistaken for a preparation of records an operator did not name.
* **The type key must be a published registry key.** A key the versioned registry
  does not publish is refused as `unknown_type` before a file name is built from it,
  and the name it does build - the key plus `.json` - is then checked against the
  reader's own rule for a record file, so an arbitrary string can never become a
  path.
* **The instant must carry an offset.** It is stored exactly as supplied and read as
  UTC; a naive or unparseable text is refused as `fetched_at` rather than assumed to
  be UTC.
* **An existing entry is never replaced.** A byte-identical rerun is a no-op: both
  files keep their bytes and their modification times, and the validator's report is
  still printed. The same type key with a different entry - even one that differs
  only in the instant - is refused as `entry_conflict`, and a different payload under
  an existing record name as `file_conflict`. A replacement is a reviewed edit to the
  manifest, never a side effect of a preparation run.
* **The output directory is judged before anything is written.** An absent directory
  is created when its parent exists; a directory holding files but no manifest is
  refused as `out_not_empty`; a manifest that does not validate has its report
  printed before the refusal. The packaged content root, anything inside it and any
  ancestor of it are refused as `shipped_root` or `out_contains_shipped`.
* **Staging, then finalisation.** A staging directory (`.prepare-v1-candidate-…`) is
  created inside `--out`, the record and the merged manifest are written there, the
  records the manifest already names are copied in beside them, and the whole staged
  directory is validated twice: with `validate_content_root` and with the reader's own
  `JsonSnapshotProvider`. Only then is the record moved into place, and the manifest
  after it, with one `os.replace` each. If the manifest cannot be replaced, the record
  that run added is removed again, and a directory the run created is removed too, so
  a refusal leaves the directory as it was found - *when that recovery completes*. A
  record that cannot be taken back, a staging directory that cannot be removed, or a
  directory the run created and cannot remove is answered with `status=inspect` and
  exit `3` instead, because the directory may then hold a file its manifest does not
  name, or a leftover the run knows of. A staging directory an earlier run left behind
  is never deleted by a later one: this command removes only what it created itself, so
  the leftover stays where it is. With no manifest beside it, its contents make the
  directory unusable and the next run refuses it as `out_not_empty`; beside a valid
  manifest the validator ignores it, so it can stay there - ignored, and undiagnosed -
  until a reviewer removes it. Only a staging directory this run created and could not
  remove is the reported case, and that is exit `3`.
* **The report is the validator's**, printed verbatim for the directory as it stands
  after the run - the manifest line, then one `type=<token> status=ok` line per entry
  in sorted order. A refused run prints one line instead,
  `candidate status=refused reason=<token>` or
  `type=<token> status=refused reason=<token>`. A run that cannot vouch for the
  directory ends with `candidate status=inspect reason=<token>` instead of a refusal
  line - after the validator's report when that report is what settled the run, or as
  that one line alone when the run stopped during finalisation and could not undo its
  own installation. Every token is fixed and every type key is sanitised by
  `reporting_v1.safe_token`, so no line carries a path, a file name, a digest, a
  payload value, an instant or an exception.
* **The exit status** is `0` when the candidate is in place and validates; `1` when the
  run was refused and this run left final output unchanged after any recovery it needed
  completed successfully; `2` when the invocation cannot be served - an option is
  missing, a path is not the kind of path it has to be, or `--out` is the packaged
  content directory, one of its descendants or one of its ancestors; and `3` when the
  run cannot vouch for `--out`: an installation, a reclamation or a cleanup step did
  not finish, or the directory does not validate after installation, so the output may
  have changed or a recovery could not be confirmed. `3` therefore requires the
  reviewer to inspect what is there before reusing it, and it promises no rollback:
  the command reports what it cannot vouch for rather than restoring it. Only `1`
  claims an unchanged directory and only `0` a valid one. Help exits `0`, as every
  Django command's help does.
* **After exit `3`, inspect before anything else.** Print the state with
  `manage.py validate_v1_content --root <the same directory>` (section 4.3), which
  reads the directory and changes nothing, then compare the record files present with
  the file names the manifest's entries give: a record the manifest does not name is
  ignored by the validator and left alone by every later run, so it is the reviewer who
  removes it or commits the entry that names it. A `.prepare-v1-candidate-…`
  directory left inside `--out` is removed by the reviewer too, never by a run. Rerun
  only once the directory is a state you can explain - a rerun is not a way to clean
  one up.
* **Limits.** Atomicity is per file, not a transaction over the directory: a process
  killed between the two replaces leaves a record file beside the old manifest, which
  the reader ignores and a rerun of the same command reuses byte for byte before
  replacing the manifest. Two runs are not coordinated with each other either - the
  command is a review-time tool and the review is the serialisation - and a run writes
  only inside the directory it was handed.
* **Nothing in a deployment runs it.** The release command, gunicorn, the health
  check, the image build and both workflows never invoke it: it is a
  developer/reviewer tool, offline, that reads no configuration and touches no
  database, cache or network. A prepared candidate is committed and deployed by the
  reviewed path in section 11, not by this command.

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
.\.venv\Scripts\python.exe .\odds\manage.py test alltips_scraper -v 2 --noinput # expect: 662 tests, 1 skipped, all passing
.\.venv\Scripts\python.exe .\odds\manage.py test alltips_scraper.tests_deployment_v1 --noinput  # the 27 deployment tests

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
database, `tests_refresh_v1.py` fakes the fetch layer and disables the socket
layer as well, and `tests_contentcheck_v1.py` runs the review-time validator
(section 4.3) over temporary candidate directories with the socket layer disabled
as well.

Nothing touches `freesupertips.com` or any other host, and no test refreshes or
regenerates a fixture. The contract these tests pin is documented in
`docs/DATA_CONTRACT.md`; fixture provenance and the refresh procedure live in
`odds/alltips_scraper/fixtures/README.md`.

One of them,
`LegacyCacheContractTests.test_cache_keys_are_namespaced_per_view`, is
time-of-day sensitive: see section 4.3 of `docs/AUDIT.md`.

### 7.1 CI gate: "Image build and local smoke check"

`.github/workflows/image-build-smoke.yml` — **"Image build and local smoke
check"** — builds the production image from the commit under test on a
GitHub-hosted runner and runs the narrow local smoke gate described in
`docs/DEPLOYMENT.md` §8.1. It runs on pushes to `main`, on pull requests targeting
`main`, and on manual workflow dispatch.

**Require a green "Image build and local smoke check" result on a `main` commit
before treating that commit as eligible for the next deployment step (section 11).**
Green means the image built and its narrow local smoke gate passed — it does not
mean the workflow deployed anything.

The workflow deploys nothing: it does not deploy to Fly, does not publish an image
or log into an image registry, and reads no repository, organization, environment
or cloud secret. It also does not run the Django test suite — it is the image build
and the smoke gate only — so run the commands above for the tests. It is not a
substitute for the deployment verification in section 11 and `docs/DEPLOYMENT.md`
§6, and one green run does not by itself make a commit production-ready.

### 7.2 CI gate: "Django regression tests"

`.github/workflows/django-tests.yml` — **"Django regression tests"** — runs the
verification commands above on a GitHub-hosted runner, against the pinned
dependencies in `odds/requirements.txt`. It runs on pushes to `main`, on pull
requests targeting `main`, and on manual workflow dispatch.

**Require a green "Django regression tests" result on a `main` commit, together
with a green "Image build and local smoke check" result (section 7.1), before
treating that commit as eligible for the next deployment step (section 11).**

A green result means:

* the job selected Python 3.12;
* the pinned dependencies were installed into a disposable virtualenv created in
  the runner's temporary directory: no developer `.venv`, and nothing that
  outlives the job;
* `python manage.py check` passed;
* `python manage.py test alltips_scraper -v 2 --noinput` passed.

It is a **test-only Django regression gate**: it says that suite passed on that
commit and nothing more. The workflow deploys nothing — it does not deploy to Fly,
does not build or publish a container image, does not log into an image registry,
reads no repository, organization, environment or cloud secret, and uses no write
permission. It is **not** a replacement for the image build and local smoke gate in
section 7.1, and it is **not** a replacement for the deployment verification in
section 11 and `docs/DEPLOYMENT.md` §6.

First recorded evidence: workflow run #1 completed successfully on `main` at commit
`dff5339` ("Add Django regression test workflow") on 2026-10-11, with an observed
duration of 22 seconds. A single green run is evidence that the run succeeded, not
proof of indefinite reliability: it does not show that the workflow is reliable for
every future change, that every dependency is secure, that the service is
production-ready, or that a Fly deployment succeeded.

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
| `400 DisallowedHost` in the machine's log while `[[http_service.checks]]` is red | the check is reaching Django with the machine's private address as `Host` (it probes over the private network, not through the fly.dev name). Restore the check's `headers = { Host = … }` in `fly.toml` and redeploy — never add the machine's address to `ALLOWED_HOSTS`: Fly documents that those addresses change. |
| health check red and every request to `/api/health/` in the log answers `301`, and the check's probe is a `301`/timeout rather than a served `200` | the access log shows `301` and there is no `GET /api/health/ 200`: the probe arrived as plain HTTP and `SECURE_SSL_REDIRECT` answered it — Fly does not follow redirects, so the check never passes. Restore `"X-Forwarded-Proto" = "https"` in the check's `headers`; do not exempt `/api/health/` from the redirect. |
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
environment (no Docker CLI), and no `fly` command was run. The configuration now
names the app the first deployment will create — `sure-tips-api-mate` in region
`iad`, answering as `sure-tips-api-mate.fly.dev` (sprint 1O-1P, `fly.toml`) — but
**no Fly app has been created** from it and no `fly` command has been run against
it. The full procedure, including the image-safety checks, is
`docs/DEPLOYMENT.md`. The short version:

```powershell
fly apps create sure-tips-api-mate                    # the app fly.toml declares
fly secrets set SECRET_KEY=<generated>                # see section 6
fly deploy                                            # release_command: migrate only
curl.exe https://sure-tips-api-mate.fly.dev/api/health/   # the committed host
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
* `fly.toml` carries a selected identity, not a created app: `app`
  `sure-tips-api-mate`, `primary_region` `iad` and the `sure-tips-api-mate.fly.dev`
  host in `[env] ALLOWED_HOSTS` are committed (sprint 1O-1P), while no Fly app has
  been created from them. The machine size stays a creation-time choice — no
  `[[vm]]` block or size is committed — so the first deploy still starts with
  `fly apps create sure-tips-api-mate` and `fly scale`. Because the identity is
  committed rather than filled in at deploy time, `fly deploy` no longer fails on
  an unfilled template: it now names one specific app, and whether that app exists
  and whether this account owns the name are Fly-side facts this repository cannot
  verify.
* `odds/alltips_scraper/tests_deployment_v1.py` pins all of the above, so a
  change to the Dockerfile, `.dockerignore`, `fly.toml` or the pinned
  dependencies has to be deliberate.
* A `main` commit is eligible for the next deployment step only after **both**
  the **"Django regression tests"** workflow
  (`.github/workflows/django-tests.yml`, section 7.2) and the
  **"Image build and local smoke check"** workflow
  (`.github/workflows/image-build-smoke.yml`, section 7.1) are green on it — the
  two gates recorded in sections 7.1 and 7.2. Green gates deploy nothing.

### 11.1 Initial static-v1 deployment scope (2026-10-10, sprint 1K)

The first deployment is a **read-only static-v1 service**: `GET /api/health/` plus
the versioned read path `GET /api/v1/tips/` (§4.1), answered from the reviewed
canonical JSON committed under `odds/alltips_scraper/content/v1/` and shipped in the
image. That reader is local, offline and database-free at runtime — no database, no
cache, no network, no scheduler — which is the scope `docs/DEPLOYMENT.md` section 1.1
records.

Content reaches the image only through the reviewed loop: validate the candidate
offline (§4.3), review it, commit the payload and its manifest entry, build a new
image and redeploy. There is no runtime publication: `manage.py refresh_tips` (§4.2)
is not an in-container publisher, and against the read-only provider this image
installs it refuses to run (exit status `3`). The tracked manifest is intentionally
empty, so a supported type key that no publication names answers `503`
`source_unavailable` by design — that is the expected answer for an unpublished key,
not a fault to chase.

Deferred, with no architecture selected: authentication and sessions, the admin,
payments and entitlements, any cache or shared store (Fly volume, managed database,
Redis/Upstash), a scheduler or worker, and any runtime content writer. No persistence
architecture is chosen, and this sprint changed no configuration or code: it recorded
the scope. No image was built, no container was run, and no deployment occurred.

The six legacy routes in §4 stay frozen and routable. They are outside the static-v1
guarantee and can perform synchronous upstream work inside a request, so this scope is
**not** a claim that the whole public service is offline; their production exposure is
a separate, deferred decision.

### 11.2 Manual release procedure (2026-10-11, sprint 1N)

CI eligibility, the deploy attempt and a verified release are three separate
things, and only a human moves from one to the next:

* *Eligible* — both gates in sections 7.1 and 7.2 are green on the same `main`
  commit (section 11). Green gates deploy nothing, and neither workflow may ever be
  given a Fly credential, a registry login, a deploy step or a write permission.
* *Attempted* — a named operator ran the command sequence in `docs/DEPLOYMENT.md`
  section 4 against a reviewed, eligible commit. An accepted deploy is not yet a
  release.
* *Verified* — every approved read-only check below passed and the result was
  recorded in the release record below. Sections 11.1 and 4.1 are the only public
  surface this sprint claims.

There is no staging environment, no scheduled release and no auto-deploy: green on
`main` means *eligible*, not *released*.

**Preconditions.** All of these hold before any `fly` command is run, and each failed
condition is an abort (see the abort table).

1. The working directory is the repository root — the directory holding `odds/`,
   `Dockerfile` and `fly.toml` (`docs/DEPLOYMENT.md` section 2).
2. The worktree is clean and `HEAD` is `origin/main`. The remote-tracking ref must be
   current, because a stale `origin/main` is one way the wrong commit gets released:

   ```powershell
   git status --short --untracked-files=all   # expect no output
   git rev-parse HEAD
   git rev-parse origin/main                  # expect the same SHA as HEAD
   git --no-pager log -1 --format='%h %s'     # candidate short SHA and subject
   ```

3. The candidate is identified as that short SHA and both workflow results are green
   **on that commit**: "Django regression tests" (`.github/workflows/django-tests.yml`,
   section 7.2) and "Image build and local smoke check"
   (`.github/workflows/image-build-smoke.yml`, section 7.1). Read them from the
   candidate's own checks at release time — the run numbers recorded in this document
   are the first runs only (section 7.2, `docs/DEPLOYMENT.md` section 10) — so a run
   identifier or URL is recorded only if it is visibly attached to the candidate.
   Never invent one, and never re-dispatch a red commit to make it green.
4. A named maintainer authorized the attempt, and the authorizing person and the
   operating person are both recorded below. No release-approval authority is
   documented in this repository yet (see the unresolved prerequisites).
5. A human is authenticated to Fly **locally**. No Fly credential is ever stored in
   the repository and CI is never used for this. `fly version` proves the CLI is
   present (`docs/DEPLOYMENT.md` section 2); this repository documents no command that
   establishes or proves the local session, so that part is on the operator.
6. Runtime configuration is confirmed present **without reading any value**:
   `SECRET_KEY` exists as a Fly secret (`fly secrets list` shows names and digests
   only — `docs/DEPLOYMENT.md` section 3.1), the `[env]` values in `fly.toml` are the
   committed ones (section 6), and neither `DEBUG` nor `CORS_ALLOW_ALL_ORIGINS` is set
   (`docs/DEPLOYMENT.md` section 3).
7. `fly.toml` names the identity this repository declares: `app`
   `sure-tips-api-mate`, `primary_region` `iad` and the `sure-tips-api-mate.fly.dev`
   host in `[env] ALLOWED_HOSTS` (`docs/DEPLOYMENT.md` section 4; committed in
   sprint 1O-1P). Confirm the committed values are the ones being released rather
   than editing them: changing the identity is a **committed** change, so land it in
   its own commit and then repeat preconditions 2 and 3 against that commit. Never
   hand-edit a live `[env]` to work around this, and never edit `fly.toml` on a
   dirty tree.
8. The host used below is `sure-tips-api-mate.fly.dev`, the one `fly.toml` commits
   and the one the created app has to own. No Fly app has been created for it yet,
   so until the app exists the whole procedure is blocked (see the unresolved
   prerequisites).

**The deploy attempt** — manual, in this order (`docs/DEPLOYMENT.md` sections 4
and 5):

```powershell
fly apps create sure-tips-api-mate      # only if the app does not exist yet
fly secrets set SECRET_KEY=$secret    # $secret generated by docs/DEPLOYMENT.md section 3.1
fly secrets list                      # names and digests only; values are never shown
fly deploy                            # build, release_command migrate, roll out
fly status                            # machines, release, health
```

`fly deploy` deploys the working tree, so the clean-tree precondition above is what
ties the deployed bytes to the gated commit. The only release action is
`python manage.py migrate --noinput`; if it exits non-zero the release is abandoned
and the previous version keeps serving (`docs/DEPLOYMENT.md` section 5).

**Outcome ladder.** Three states, in order — do not skip to the last one:

1. *Accepted*: `fly deploy` exited 0 and `fly status` shows a new release version.
   This is not verification.
2. *Healthy*: the `[[http_service.checks]]` probe of `/api/health/` in `fly.toml`
   (interval 15s, timeout 5s, grace 10s) passes and the health request below returns
   the expected response. The check sends this app's own `Host` and
   `X-Forwarded-Proto: https` because it probes the machine over the app's private
   network, not through the public name: without them the probe is answered `400`
   and then `301`, which is a failed check. `docs/DEPLOYMENT.md` section 5 says why
   both headers are there, and section 9 below lists the two failure shapes.
3. *Verified*: state 2 **and** every other approved check below. A release is never
   called verified, and never recorded as verified, until all of them pass.

**Approved checks** (read-only; `docs/DEPLOYMENT.md` section 6.1 is the same matrix
with the source that pins each expectation):

| Read-only check | Expected result |
| --- | --- |
| `fly status` | machine running, new release version, checks green |
| `fly logs` | console logging only; nothing sensitive echoed |
| `GET https://sure-tips-api-mate.fly.dev/api/health/` | `200` and the pinned health body |
| `GET https://sure-tips-api-mate.fly.dev/api/v1/tips/?type=bet_of_the_day` | `503` `source_unavailable` — expected, the tracked manifest is the empty map |

```powershell
fly status
fly logs
curl.exe https://sure-tips-api-mate.fly.dev/api/health/
curl.exe "https://sure-tips-api-mate.fly.dev/api/v1/tips/?type=bet_of_the_day"
```

**Never request these in production:** the six legacy routes in section 4
(`/api/bet-of-the-day/`, `/api/daily-accumulator/`, `/api/btts-win-accumulator/`,
`/api/over-25-goals-accumulator/`, `/api/BTTS/`, `/api/goalscorer/`) — frozen and
routable, but outside the static-v1 guarantee and able to perform synchronous
upstream work inside a request (sections 4 and 11.1); `/admin/` (no persistence
architecture is chosen, `docs/DEPLOYMENT.md` section 10); any `POST`, `PUT`, `PATCH`
or `DELETE`; `manage.py refresh_tips`; `manage.py migrate`; `manage.py
validate_v1_content`; any mail-sending, scraping or publication trigger; and any
environment or secret dump. Paste only status codes and pinned error codes into the
record.

**Abort conditions.** When one of these is true, stop and take the listed action
instead of continuing.

| Condition | Action |
| --- | --- |
| Dirty worktree, `HEAD` not `origin/main`, or an unknown or stale `origin/main` | Stop. Fix the local git state, or re-derive the candidate, and begin again. |
| Missing or red CI evidence for the candidate, or green gates on a different commit | Stop. Wait for both gates on that commit (sections 7.1 and 7.2); never re-dispatch to force green. |
| No recorded human authorization | Stop. Obtain it before running any `fly` command. |
| Local Fly authentication absent or unverifiable | Stop. Authenticate locally, outside this repository; never through CI or a repository secret. |
| Required configuration unconfirmed, `DEBUG` or wildcard CORS set, or the committed `fly.toml` identity not the one being released | Stop. Fix it as a committed change, then repeat preconditions 2 and 3. |
| `fly deploy` fails, or the release command exits non-zero | Investigate, do not roll back: by design the previous version keeps serving (`docs/DEPLOYMENT.md` section 5). |
| A post-deploy check fails | Do not call the release verified. Use only the documented rollback path (`docs/DEPLOYMENT.md` section 7), then record the outcome. |
| Any urge to repair production by hand — secret rotation, `refresh_tips`, `migrate`, content edits | Stop. All of that is outside this procedure (sections 4.2 and 11.1, `docs/DEPLOYMENT.md` section 9). |

**Rollback.** `docs/DEPLOYMENT.md` section 7 is the only documented rollback:
`fly releases` to find the previous good version, then
`fly deploy --image registry.fly.io/sure-tips-api-mate:<previous-version>`. Two documented facts
govern it: a rollback does not undo migrations (migrations are forward-only), and a
release command that exits non-zero already leaves the previous version serving.
**Not documented, therefore not assumed:** whether an earlier image is still
retained for this app, what the real tag is, what to do when the failed release is
the first one, and whether any Fly-side machine rollback exists. If the documented
path does not apply, stop and escalate instead of improvising. A rolled-back
release is recorded as not verified.

**Release record.** Safe metadata only — never a token, a secret value, secret-list
content beyond names, `odds/.env` content, a database URL, an SMTP or transport
credential, an environment dump, or any shipped or customer content.

```
Release record
  Date/time (operator local, with UTC offset):
  Candidate commit (short SHA):
  origin/main short SHA at attempt time / match?:
  "Django regression tests" on that commit (run #/URL if visible):
  "Image build and local smoke check" on that commit (run #/URL if visible):
  Authorized by (name/initials, when):
  Deploy operator (name/initials):
  Deploy outcome (`fly deploy` exit status; `fly status` release version):
  Health check result (status code; pinned body matched yes/no):
  v1 content check result (status code; `source_unavailable` observed yes/no):
  Outcome: verified / not verified + reason:
  Rollback: none / performed (version from `fly releases`) / escalated:
  Unresolved prerequisites or follow-ups discovered:
```

**Unresolved prerequisites.** These are not established anywhere in this
repository, so a human must confirm each one before the first real release:

* The Fly app identity — `app` `sure-tips-api-mate`, `primary_region` `iad` and the
  host `sure-tips-api-mate.fly.dev` are committed in `fly.toml` (section 11,
  `docs/DEPLOYMENT.md` section 4), but **no Fly app has been created** for them, and
  whether this account can own that name is a Fly-side fact no file here records.
* Whether the app exists at all: `fly apps create sure-tips-api-mate` is documented
  but has never been run (section 11, `docs/DEPLOYMENT.md` section 10).
* The concrete public base URL the checks use: only the committed
  `sure-tips-api-mate.fly.dev` host is known, and it has never been called.
* The local Fly authentication and which person holds it (precondition 5).
* The machine size — no `[[vm]]` block or machine size is committed (section 11).
* Whether a previous release image is retained for a rollback (see Rollback).
* Which person or role authorizes a release (precondition 4).
* The CI run identifiers for the candidate commit: only the first runs of each
  workflow are recorded (sections 7.1 and 7.2, `docs/DEPLOYMENT.md` section 10).

**Why the separation exists.** The gates and the release are kept apart because each
one protects against a different failure. A green run says that the suite and the
local smoke gate passed on a commit — not that anything was deployed. The clean-tree
and SHA preconditions tie the deployed bytes to that gated commit, which a stale
`origin/main`, a dirty worktree or a hand-edited `fly.toml` would break. The closed
list of approved checks keeps production requests on the read-only static-v1 surface
instead of the frozen legacy routes that can reach upstream. The credential-free,
deploy-free workflows mean a compromised runner cannot release anything. The record
template keeps evidence to status codes and pinned error codes, so logs, secrets and
configuration stay out of documentation. One green run is not evidence of indefinite
reliability, and an accepted deploy is not a verified release.

