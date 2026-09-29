# OddMate — ODDMATE TIPS

Backend repository for the **OddMate** sports-tips product (app label
**ODDMATE TIPS**).

> **Sprint 0.5 status:** this repository currently contains the legacy tips
> scraper service only. Flutter client, pricing, subscriptions, payments, and
> result settlement are **not** implemented and are intentionally out of scope
> for Sprint 0.5. See [`docs/PRODUCT_DECISIONS.md`](docs/PRODUCT_DECISIONS.md)
> for the product direction that future sprints must follow.

## Repository layout

- `odds/` — Django backend service (the tips scraper API).
  - `odds/odds/` — project settings and URL routing.
  - `odds/alltips_scraper/` — legacy tips scraping endpoints, plus the versioned
    `urls_v1.py`, `views_v1.py`, `readmodel_v1.py` and `serializers_v1.py` modules
    behind `GET /api/v1/tips/`, the offline parser-contract tests, and their static
    HTML fixtures in `odds/alltips_scraper/fixtures/`.
  - `odds/customers/` — placeholder app for future account/entitlement work.
- `docs/` — product decisions, data contract, audit trail, and runbook.
- `.venv/` — (git-ignored) local virtual environment at the repository root.

## Documentation

| Document | Purpose |
| --- | --- |
| [`docs/PRODUCT_DECISIONS.md`](docs/PRODUCT_DECISIONS.md) | Brand, terminology, access tiers, pricing-card spec, Go Premium, match lifecycle/results, and settlement rules. |
| [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) | Frozen output contract of the `alltips_scraper` parsers: envelopes, field dictionary, availability matrix, fields that are **not** available, fixture provenance, and the timezone architecture (UTC storage, device-local display). |
| [`docs/AUDIT.md`](docs/AUDIT.md) | Sprint 0.5 audit of the `odds/` service. |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Setup, run, verify, and operate the service. |
| [`docs/API_V1_CONTRACT.md`](docs/API_V1_CONTRACT.md) | Implemented contract of `GET /api/v1/tips/`: route, query parameters, date/timezone semantics, response envelope, error codes, and deferrals. |

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

## Brand and terminology

- Product brand: **OddMate**; app label: **ODDMATE TIPS**.
- Customer-facing access terms: **Free** and **Premium** (never "VIP").
- Prototype names, prices, performance metrics, and settled outcomes are visual
  examples only, never production facts.
- Internal identifiers `sure_tips` (repo) and `sure-tips-api` (health payload)
  are technical, not user-facing brands.
