# OddMate — ODDMATE TIPS

Backend repository for the **OddMate** sports-tips product (app label
**ODDMATE TIPS**).

> **Sprint 0.5 status:** this repository currently contains the legacy tips
> scraper service only. Flutter client, pricing, subscriptions, payments, and
> result settlement are **not** implemented and are intentionally out of scope
> for Sprint 0.5. See [`docs/PRODUCT_DECISIONS.md`](docs/PRODUCT_DECISIONS.md)
> for the product direction that future sprints must follow.

> **Deployment readiness (sprint 1E-A1):** the service is now containerised
> (`Dockerfile`, `.dockerignore`, `fly.toml`) and runs on its local, git-ignored
> SQLite file in every environment — there is no `DATABASE_URL` and no PostgreSQL
> dependency. Transport security is
> environment-driven: a deployment states the six transport values in `fly.toml`
> `[env]`, and unset local defaults keep plain HTTP working. `fly.toml` is a
> template — `app`, `primary_region` and the `<app>.fly.dev` host are placeholders
> until the app is created. **Nothing has been deployed:** the procedure, the
> secrets, and the open gaps are in
> [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md), and the sprint record is in
> [`docs/AUDIT.md`](docs/AUDIT.md) section 7.

> **Published content (batch B, 2026-10-04):** the versioned endpoint is served from
> reviewed canonical JSON that ships inside the image
> (`odds/alltips_scraper/content/v1/`), so the deployed reader is read-only. The
> out-of-band writer `manage.py refresh_tips` writes the durable snapshot table
> instead and refuses to run (exit status `3`) unless
> `storage_v1.DatabaseSnapshotProvider` is the installed provider. See
> [`docs/API_V1_CONTRACT.md`](docs/API_V1_CONTRACT.md) and
> [`docs/RUNBOOK.md`](docs/RUNBOOK.md) section 4.2.

> **Initial deployment scope (sprint 1K, 2026-10-10):** the first deployment is a
> **read-only static-v1 service** — `GET /api/health/` plus `GET /api/v1/tips/`,
> answered from the reviewed canonical JSON committed under
> `odds/alltips_scraper/content/v1/` and shipped inside the image. That reader is
> local, offline and database-free at runtime; content is published only by
> validating a candidate offline, reviewing it, committing it and rebuilding the
> image; and the empty manifest means a supported type key no publication names is
> answered `503` `source_unavailable` by design. No Fly volume, managed database,
> Redis/Upstash, scheduler, worker, runtime cache, authentication, payments or
> entitlements are part of it, and no persistence architecture is selected. The six
> legacy routes stay frozen and routable but are **outside** this guarantee — they
> can still call upstream synchronously. See
> [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) §1.1 and
> [`docs/RUNBOOK.md`](docs/RUNBOOK.md) §11.1. Nothing was built or deployed.

## Repository layout

- `odds/` — Django backend service (the tips scraper API).
  - `odds/odds/` — project settings and URL routing.
  - `odds/alltips_scraper/` — legacy tips scraping endpoints, plus the versioned
    `urls_v1.py`, `views_v1.py`, `readmodel_v1.py`, `serializers_v1.py`,
    `storage_v1.py` and `refresh_v1.py` modules behind `GET /api/v1/tips/`. The
    endpoint is served from the read-only published content the image ships
    (`jsoncontent_v1.py` reading `odds/alltips_scraper/content/v1/`, installed by
    `apps.py`); `management/commands/refresh_tips.py` is the only writer of the
    durable snapshot table and runs only while
    `storage_v1.DatabaseSnapshotProvider` is the installed provider. The package
    also holds the offline parser-contract tests and their static HTML fixtures in
    `odds/alltips_scraper/fixtures/`.
  - `odds/customers/` — placeholder app for future account/entitlement work.
- `docs/` — product decisions, data contract, audit trail, runbook, and the
  deployment procedure.
- `Dockerfile`, `.dockerignore`, `fly.toml` — container image and Fly.io
  configuration for the service (sprint 1E-A1). `fly.toml` is a template: its app
  name, region and host are placeholders that are filled in when the Fly app is
  created. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
- `.venv/` — (git-ignored) local virtual environment at the repository root.

## Documentation

| Document | Purpose |
| --- | --- |
| [`docs/PRODUCT_DECISIONS.md`](docs/PRODUCT_DECISIONS.md) | Brand, terminology, access tiers, pricing-card spec, Go Premium, match lifecycle/results, and settlement rules. |
| [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) | Frozen output contract of the `alltips_scraper` parsers: envelopes, field dictionary, availability matrix, fields that are **not** available, fixture provenance, and the timezone architecture (UTC storage, device-local display). |
| [`docs/AUDIT.md`](docs/AUDIT.md) | Sprint 0.5 audit of the `odds/` service. |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Setup, run, verify, and operate the service. |
| [`docs/API_V1_CONTRACT.md`](docs/API_V1_CONTRACT.md) | Implemented contract of `GET /api/v1/tips/`: route, query parameters, date/timezone semantics, response envelope, error codes, and deferrals. |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Container and Fly.io deployment: image contents, secrets versus `[env]`, release command, health check, rollback, and known gaps. |

## Quick start

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md). Run every command from the repository
root (the directory containing `odds/` and `.venv/`) with the root virtual
environment:

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py check
.\.venv\Scripts\python.exe .\odds\manage.py migrate
.\.venv\Scripts\python.exe .\odds\manage.py test alltips_scraper
.\.venv\Scripts\python.exe .\odds\manage.py runserver 127.0.0.1:8000
curl.exe http://127.0.0.1:8000/api/health/
```

Deploying is a separate step: the image, its build context and the Fly
configuration are described in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). The
deployment configuration check uses the same interpreter — set `DEBUG=False` and
the six transport values from `fly.toml` `[env]`, then run
`manage.py check --deploy`, which reports no issues (`docs/RUNBOOK.md` §7).

## Brand and terminology

- Product brand: **OddMate**; app label: **ODDMATE TIPS**.
- Customer-facing access terms: **Free** and **Premium** (never "VIP").
- Prototype names, prices, performance metrics, and settled outcomes are visual
  examples only, never production facts.
- Internal identifiers `sure_tips` (repo) and `sure-tips-api` (health payload)
  are technical, not user-facing brands.
