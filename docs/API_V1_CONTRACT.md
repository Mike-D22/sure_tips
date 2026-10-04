# API v1 contract — `GET /api/v1/tips/`

Date: 2026-09-28 · Sprint 1C (versioned tips endpoint)
Snapshot storage revised: 2026-09-29 (durable row store and out-of-band writer; the
request path is unchanged)
Snapshot source revised: 2026-10-04 (the installed reader is the read-only
published-content reader; the writer runs only against the durable store)

| Item | Value |
| --- | --- |
| Django app | `alltips_scraper` |
| Route | `GET /api/v1/tips/` |
| Namespaced route name | `tips_v1:tips` |
| Versioned modules | `urls_v1.py`, `views_v1.py`, `readmodel_v1.py`, `serializers_v1.py` |
| Snapshot source (`readmodel_v1` provider) | `jsoncontent_v1.py` (`JsonSnapshotProvider`), installed by `apps.AlltipsScraperConfig.ready()`: the read-only canonical content under `odds/alltips_scraper/content/v1/` |
| Durable store (writer-only) | `storage_v1.py` (`DatabaseSnapshotProvider`), one row per type key in the `SnapshotV1` table |
| Out-of-band writer (not in the request path) | `refresh_v1.py`, driven by `manage.py refresh_tips`, which runs only while the durable store is the installed provider |
| Contract tests | `odds/alltips_scraper/tests_api_v1.py`, plus `tests_jsoncontent_v1.py`, `tests_publishedcontent_reader_v1.py`, `tests_storage_v1.py`, `tests_startup_v1.py` and `tests_refresh_v1.py` for the content reader, its install, the durable store and the writer |

This document records the implemented contract of the versioned endpoint only. The
legacy surface it sits beside is documented in `docs/RUNBOOK.md` and frozen in
`docs/DATA_CONTRACT.md`, and a change to anything stated here is a contract change
under §12 of `docs/DATA_CONTRACT.md`.

## 1. Status and scope

* `GET /api/v1/tips/` is implemented and reachable, and it is strictly additive:
  the six legacy paths, their view names, their response shapes and their cache
  behaviour are untouched.
* The route is published through the `tips_v1` namespace as `tips_v1:tips`, so a
  caller can reverse it without knowing the path.
* The endpoint is offline by design. Its only data source is a snapshot read through
  `readmodel_v1.load_snapshot()`, which in a deployment is the published content the
  image ships: the versioned layer imports no scraper module, no parser, no handler,
  no cache helper and no HTTP client, so a request can never start a fetch, a scrape,
  a refresh or a cache fill.
* In scope: the route, the three query parameters, the success envelope and the
  error bodies. Out of scope: the legacy endpoints, the legacy parser contract, and
  the deferred work listed in section 6.

## 2. Route and query parameters

The whole public query surface is exactly three names:

| Parameter | Required | Accepted value |
| --- | --- | --- |
| `type` | yes | one of the six type values below |
| `date` | no | a strict ISO calendar date, `YYYY-MM-DD` |
| `timezone` | no | a loadable IANA timezone name |

* A parameter that is not named here is **ignored**, not rejected.
* A duplicated parameter resolves the way `QueryDict` does: the **final** value is
  the one used.
* `type` is the only required parameter. The six accepted values, with the `unit`
  each one publishes, are `bet_of_the_day` (`match`) and `daily_accumulator`,
  `over_25_goals`, `both_teams_to_score`, `btts_and_win` and `anytime_goalscorer`
  (all `card`).

* `match` means one entry in `tips` is a single selection. `card` means one entry is
  a source card (an accumulator) whose selections live in its `legs`. The registry
  belongs to the versioned layer and is not read from the legacy configuration.

## 3. Date and timezone semantics

* `date` is accepted only in the exact spelling `YYYY-MM-DD`: the text must match
  `[0-9]{4}-[0-9]{2}-[0-9]{2}` and must then be accepted by
  `datetime.date.fromisoformat()`. A well-spelled but impossible day such as
  `2026-02-30` is rejected, and so is every other spelling, including `20260927`.
* A supplied `date` requires a supplied `timezone` (`missing_timezone`), and a
  supplied `timezone` without a `date` is rejected (`timezone_requires_date`).
  Neither parameter is ever defaulted or inferred.
* `timezone` is validated for loadability only: `zoneinfo.ZoneInfo(name)` must
  succeed. The loaded zone is then **discarded** and never used; the name is echoed
  in `filter.timezone` exactly as it arrived.
* No timezone arithmetic of any kind is performed:
  * no UTC day window is computed from `date` together with `timezone`;
  * no DST rule, offset or conversion is applied, and no zone is activated; the
    server's own `TIME_ZONE` setting is not read;
  * `date` is compared for **equality** against the snapshot's own date text, and
    nothing else is derived from it.
* `date` is therefore a snapshot-availability filter, not a time window: a request
  naming a date the snapshot does not carry is an empty `200`, not an error.

## 4. Response envelope

A `200` body is a JSON object with these keys. Key order in this document is
illustrative, not normative: a client reads keys by name.

| Key | Meaning |
| --- | --- |
| `api_version` | the version string of this surface |
| `type` | the requested `type` value, echoed |
| `unit` | `match` or `card` |
| `count` | the number of entries in `tips` |
| `legs_count` | the total number of nested legs across `tips` |
| `tips` | the mapped tips list; `[]` when the result is empty |
| `source` | provenance of the snapshot the body was built from |
| `filter` | what the request asked for and what happened |

`source` carries `label` and `date_text`, the payload's own label and date text,
quoted verbatim and `null` when the payload states none: `date_text` is display text,
never parsed, normalised or converted to UTC, and not an authoritative timestamp.
`fetched_at` is the snapshot's fetch instant as ISO-8601 UTC, omitted when absent.

* Every selection — each entry in `tips` for a `match` unit, and each entry in a
  card's `legs` — carries the temporal markers `kickoff_at: null` and
  `kickoff_time_verified: false`. They state that no verified kickoff exists, and
  they are never back-filled from source display text.
* A selection also carries the renamed source display text `source_date_text` /
  `source_time_text`: neither a UTC value nor a device-local value.

`filter` carries exactly five keys: `date` (the validated request value, or `null`),
`timezone` (the echoed name, or `null`), `applied` (whether a date filter was applied
at all), `matched` (whether the snapshot's date text equalled `date`, or `null` when
no date filter was applied) and `available_date` (the snapshot's own usable date
text, or `null`).

* An empty result is still a result: `count: 0`, `legs_count: 0`, `tips: []`, with
  `source` and `filter` present and the request echoed back.
* An error body never carries `tips`, `source` or `filter` (section 5).

## 5. Status codes and errors

| Status | When |
| --- | --- |
| `200` | the query is valid and a usable snapshot was read, including an empty result |
| `400` | the query is not valid: one of the six query errors below |
| `405` | the method is not `GET`; refused before the query is read, with an empty body |
| `503` | the query is valid but no usable snapshot is available |

Every error body has the same shape: `api_version` plus an `error` object whose
`code`, `message` and `field` are the client-safe constants fixed here.

| `code` | `field` | `message` |
| --- | --- | --- |
| `missing_tip_type` | `type` | `the type query parameter is required` |
| `unknown_tip_type` | `type` | `the type query parameter is not a supported tip type` |
| `invalid_date` | `date` | `date must be an ISO calendar date in YYYY-MM-DD form` |
| `timezone_requires_date` | `timezone` | `timezone is only accepted together with date` |
| `missing_timezone` | `timezone` | `timezone is required when date is supplied` |
| `invalid_timezone` | `timezone` | `timezone must be a valid IANA timezone name` |
| `source_unavailable` | `null` | `tip data is temporarily unavailable` |

* The messages are constants: a client may display them, and no request value, host
  or exception detail is ever interpolated into one.
* Validation runs in the documented order and completes before the snapshot is read,
  so a query that can be answered with a `400` can never report source availability.
* A `405` is the framework's method refusal and carries an empty body.
* Server-side diagnostics are logged without exposing upstream URLs, credentials,
  configuration, payloads, or exception details to clients.

## 6. Operational status and deferrals

* The snapshot is read through the `readmodel_v1` seam, whose installed provider is
  chosen once per process at startup (`apps.AlltipsScraperConfig.ready()`): the
  read-only `jsoncontent_v1.JsonSnapshotProvider`, which reads reviewed canonical
  JSON from the content directory the image ships
  (`odds/alltips_scraper/content/v1/`: one `manifest.json` plus one payload file per
  type key the manifest names). A published record is either usable or absent — a
  record that fails its own record version, digest, payload shape or timestamp is
  answered as "no snapshot", never published and never a `500`. An empty manifest
  and a type key no manifest names are the two states an unpublished deployment is
  allowed to be in, and both are answered as "no snapshot" without being reported
  as failures.
* **The request path is read-only and holds no writer**: no request stores, clears or
  refreshes a snapshot, and none performs a network call. A server whose published
  content names no usable record answers `503` `source_unavailable`, which is the
  designed behaviour, not a defect.
* The deployed reader and the writer are two different stores. The reader publishes
  reviewed content; `manage.py refresh_tips` driving `refresh_v1.py` writes the
  durable `SnapshotV1` table instead, and refuses the whole run (exit status `3`,
  before it resolves a type) unless `storage_v1.DatabaseSnapshotProvider` is the
  installed provider. That writer is deliberately outside this request-path contract
  and no route can reach it; it is documented in `docs/RUNBOOK.md` section 4.2.
* `filter.timezone` is echoed but never applied, so a `200` is not evidence that a
  timezone-aware day window was honoured.
* Deferred, and deliberately not implemented in this sprint:
  * any write a request can trigger, and any in-request refresh or ingestion path;
  * any shared or external cache between processes;
  * history, versioning or retention of past snapshots — a refresh replaces the
    type's own row rather than keeping the one it replaced;
  * the actual timezone-aware UTC day-window filtering of §13.8 in
    `docs/DATA_CONTRACT.md`.

## 7. Verification and compatibility

The contract is covered by offline tests in
`odds/alltips_scraper/tests_api_v1.py`, with the published-content reader covered by
`tests_jsoncontent_v1.py` and `tests_publishedcontent_reader_v1.py`, and the durable
store, its startup install and the out-of-band writer by `tests_storage_v1.py`,
`tests_startup_v1.py` and `tests_refresh_v1.py`.

```powershell
.\.venv\Scripts\python.exe .\odds\manage.py check
.\.venv\Scripts\python.exe .\odds\manage.py test alltips_scraper -v 2 --noinput
```

Manual check, local development only. `127.0.0.1:8000` is the loopback address of
the local development server; no external host, upstream URL or configuration value
is recorded in this document.

```powershell
# Local-development verification only. A server answers 503
# source_unavailable by design until manage.py refresh_tips has stored a row
# for that type (docs/RUNBOOK.md section 4.2).
curl.exe -i "http://127.0.0.1:8000/api/v1/tips/?type=bet_of_the_day"
```

* Compatibility: this surface is additive, so the six legacy endpoints and the
  legacy parser envelopes stay exactly as documented in `docs/DATA_CONTRACT.md` and
  `docs/RUNBOOK.md`.
* Adding a query parameter, changing a key set, or changing the status for an
  existing input is a contract change, and one change must update this document, the
  tests named above and `docs/DATA_CONTRACT.md` §12 together.
