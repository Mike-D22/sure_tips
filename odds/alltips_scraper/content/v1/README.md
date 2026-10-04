# Canonical v1 content (shipped with the application, read-only)

This directory is the **canonical-content root** for the versioned tips API
(`/api/v1/tips/`). It holds the reviewed JSON artifacts that the read-only
provider `alltips_scraper/jsoncontent_v1.py` (`JsonSnapshotProvider`) reads when
the endpoint is served from shipped content instead of from a database row a
fetch wrote. Nothing here is fetched, generated, or refreshed by a test or by the
running application.

## What is tracked

| File | Role |
| --- | --- |
| `manifest.json` | the index: one record version and one entry per tip type key |
| `<name>.json` | one published record payload, named by an entry's `file` |

`manifest.json` is tracked here **empty**:

```json
{"schema_version": 1, "snapshots": {}}
```

An empty manifest and a type key that is not named are the two states that are
answered as "no snapshot" without being reported as a failure. That is deliberate:
until a reviewed publication names a key, the endpoint keeps its existing
client-safe answer (`503` with `source_unavailable`) and no reader can mistake an
unpublished key for published content.

## Manifest shape

```json
{
  "schema_version": 1,
  "snapshots": {
    "<type_key>": {
      "schema_version": 1,
      "file": "<name>.json",
      "sha256": "<canonical digest of the payload that file holds>",
      "fetched_at": "<ISO-8601 instant with an offset>"
    }
  }
}
```

The manifest carries **no** value the provider may quote into a log line: every
refusal is reported through `alltips_scraper.jsoncontent_v1` as a fixed template
line holding the type key and a reason token and nothing else.

## How to publish a record (manual, reviewed change)

1. Write the payload document as `<name>.json` **beside** this manifest. `file`
   is a plain file name: it may not contain a separator or a drive prefix, and the
   path it resolves to has to stay inside this directory.
2. Compute the digest of the **payload**, not of the file bytes, with the one rule
   in `alltips_scraper/canonical_json_v1.py`:

   ```python
   from alltips_scraper.canonical_json_v1 import canonical_payload_sha256
   digest = canonical_payload_sha256(json.loads(Path("<name>.json").read_text("utf-8")))
   ```

   Because the digest is taken over the canonical form — keys sorted, compact
   separators, non-ASCII escaped — reformatting a file does not change its digest,
   and rewriting what it holds does.
3. Add the entry under its type key in `manifest.json` with that digest and the
   **authoritative instant** as an ISO-8601 instant that carries an offset
   (`2026-09-28T17:12:03+00:00`). A naive or missing instant is refused rather
   than read as UTC — see `docs/DATA_CONTRACT.md` §13.
4. Update the tests that pin the published artifact in the same change.

## Committing rules

Content here is safe to commit only if it contains:

- no secrets, tokens, cookies, API keys, or environment values
- no personal data, user information, or account/payment data
- no private hostnames, internal URLs, or identifiers the team treats as private

There is no automatic refresh, and nothing is published by placing a capture here
by hand. A future controlled publication workflow may produce candidate
artifacts, but a candidate becomes content here only when it has validated the v1
payload contract, had prohibited data removed, had its canonical digest computed,
had its authoritative instant pinned, received code review, and been committed and
deployed through the normal reviewed release path. Runtime scraper output must not
be copied directly into this directory.
