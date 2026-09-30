# Data contract — `alltips_scraper` scraper output

Date: 2026-09-28 · Sprint 1B (parser fixture contract)

| Item | Value |
| --- | --- |
| Django app | `alltips_scraper` |
| Module under contract | `odds/alltips_scraper/utils.py` |
| Fixtures | `odds/alltips_scraper/fixtures/` (static, synthetic) |
| Parser contract tests | `odds/alltips_scraper/tests_parser_contract.py` |
| Network-isolation proof | `odds/alltips_scraper/tests_offline_guard.py` |
| Verified with | CPython 3.12.10, `beautifulsoup4` 4.14.3 |

## 1. Why this document exists

Before Sprint 1B nothing in the repository pinned what the real parser functions
return. The only tests (`tests.py`) asserted cache and health behaviour against
hand-written payload mirrors, so a future model, ingestion job, or client could be
built on field names and value shapes the parsers never actually emit.

Sprint 1B closes that gap with static HTML fixtures plus tests that call the real
parsers, and this document records the resulting contract in one place.

**Scope:** the six existing source keys and the three parser functions behind
them. **Out of scope:** the six HTTP endpoints (see `docs/RUNBOOK.md`), caching
(`alltips_scraper/decorators.py`), Django models (there are none yet), and any
database schema — this sprint adds no model, no schema, and no migration.

## 2. Source keys and parser functions

| Source key | Parser | Payload shape |
| --- | --- | --- |
| `bet_of_the_day` | `parse_bet_of_the_day_page(html, current_date=None)` | flat `matches` list |
| `daily_accumulator` | `parse_accumulator_page(html, current_date=None)` | `accumulators` list, no `tip_type` |
| `over_25_goals` | `parse_generic_tips_page(html, 'over_2.5_goals', current_date=None)` | `accumulators` list + `tip_type` |
| `both_teams_to_score` | `parse_generic_tips_page(html, 'btts', ...)` | same as above |
| `btts_and_win` | `parse_generic_tips_page(html, 'btts_and_win', ...)` | same as above |
| `anytime_goalscorer` | `parse_generic_tips_page(html, 'anytime_goalscorer', ...)` | same as above |

The table is `utils.SCRAPER_CONFIGS`; each entry is `{'path', 'parser',
'is_accumulator'}`. `path` is **relative** — the host comes from the `SCRAPE_URL`
environment variable, which must never be written into documentation, fixtures,
logs, or tests. The configured parsers for the four generic pages are lambdas
that accept only `html`, so they cannot forward a date of their own; that
single-argument signature is itself pinned by
`LegacyConfigCallPathTests.test_config_lambdas_deliberately_keep_a_single_argument_signature`.

`current_date` is the only production change Sprint 1B made. It is optional and
defaults to `None`; `utils.resolve_source_date(None)` reproduces the legacy
`datetime.now().strftime('%Y-%m-%d')` exactly. Passing a value pins the stamp so
parser output is identical on any machine at any wall-clock time. `None` means
"use the clock"; any other value (including `''`) is used verbatim.

## 3. The three response envelopes

All keys below are always present in a success envelope.

### 3.1 `bet_of_the_day`

```
{date, total_tips, total_cards, matches, count, source}
```

* `date` — the injected/current date stamp, `YYYY-MM-DD`.
* `matches` — a flat list of every leg of every card, in document order.
* `count` == `total_tips` == `len(matches)`.
* `total_cards` — the number of `div.Card` elements **found**, including cards
  that produced no legs, so it can exceed the number of contributing cards.
* `source` — the constant string `'freesupertips'`.
* There is **no** `tip_type` key.
* Error envelope:
  `{'error': 'No tip cards found', 'matches': [], 'count': 0}` — no `date`, no
  `source`, no `total_tips`, no `total_cards`.

### 3.2 `daily_accumulator`

```
{date, total_accumulators, accumulators, count, source}
```

* `total_accumulators` — the number of cards that produced at least one leg.
* `count` — the sum of every `matches_count`.
* Error envelope:
  `{'error': 'No accumulator cards found', 'accumulators': [], 'count': 0}`.

### 3.3 Generic tips pages

```
{date, tip_type, total_accumulators, accumulators, count, source}
```

* `tip_type` — one of `over_2.5_goals`, `btts`, `btts_and_win`,
  `anytime_goalscorer`.
* Error envelope: `{'error': 'No tip cards found', 'accumulators': [], 'count': 0}`
  — the same message text as 3.1 but with `accumulators` and no `tip_type`, so do
  not branch on the message string alone.

### 3.4 The fetch layer adds two keys the parsers never set

`utils._fetch_one()` — the only place in the module that performs a request —
adds `scraped_at` and `source_url` **after** parsing. Parser output therefore
never contains `source_url` or `scraped_at`, and a test asserts that for the error
envelopes. After that injection they are the only volatile keys in a full API
response; nothing else in the payload depends on when it was read except `date`.

`scraped_at` is currently `datetime.now().isoformat()` — a **naive local-time
string with no UTC offset** — so it is *not* yet an authoritative ISO-8601 UTC
value. See 13.7 before treating it as one.

## 4. Field dictionary

### 4.1 Tip-card match (`bet_of_the_day`)

Exact key set: `date`, `time`, `match_title`, `teams`, `prediction`,
`opponent_text`, `tip_reason`, `match_url`, `tip_category`, `stake`, `returns`,
`odds`.

| Field | Type | Where it comes from | Notes |
| --- | --- | --- | --- |
| `date` | str | injected date or the clock | source **display** date text, `YYYY-MM-DD`; the day the page was read, **not** the fixture date. Future field name `source_date_text` (13.4) |
| `time` | str | text of `<time>` | source **display** kick-off text, e.g. `19:45`; `''` when absent. No date part, no timezone, and **not UTC**. Future field name `source_time_text` (13.4) |
| `teams` | list[str] | team-image `style` URLs, plus the `Leg__lose` text | 0, 1, or 2 names; `%20` decoded, `.png`/`.jpg` stripped. Order is the page's order, unverified |
| `match_title` | str | derived | `" vs ".join(teams)` when `len(teams) >= 2`, otherwise it falls back to `prediction` (and can be `''`) |
| `prediction` | str | `<div class="Leg__win">` text | free text exactly as published |
| `opponent_text` | str | `<div class="Leg__lose">` text | raw display text, e.g. `vs Manchester United` or `at Chelsea` |
| `tip_reason` | str | first `<p>` inside `div.Exp-collapse > div.TipReason__body` | `''` when absent |
| `match_url` | str | `href` of `a.TipReason__link` inside `div.TipReason__header` | a relative source path; `''` when the leg has no reason header |
| `tip_category` | str | `<h2>` inside `header.TipHeader` | verbatim source label; `''` when the card has no `<h2>` |
| `stake` | float or None | `value` of the `option.active` in the card's stake `select` | the **card** stake, repeated onto every leg of that card |
| `returns` | float or None | text of `div.BetGrid__returns` | the **card** returns, repeated onto every leg |
| `odds` | float or None | derived | `round(returns / stake, 2)` when both exist and `stake > 0`, else `None` |

`stake`, `returns`, and `odds` are **card-level values copied onto each leg**.
They are not per-leg prices, and the source does not publish per-leg prices.

### 4.2 Accumulator object

`daily_accumulator` emits `category`, `stake`, `returns`, `total_odds`,
`matches`, `matches_count`. The four generic pages add `tip_type`.

| Field | Type | Where it comes from | Notes |
| --- | --- | --- | --- |
| `category` | str | the card's `tip_category`, i.e. the `<h2>` text verbatim | `''` when the card has no `<h2>` |
| `tip_type` | str | the constant `SCRAPER_CONFIGS` passes in | generic pages only |
| `stake` | float or None | as 4.1 | `None` when the card has no `BetGrid` (or no active option) |
| `returns` | float or None | as 4.1 | `None` when the card has no returns box |
| `total_odds` | float or None | as 4.1 | `None` unless both of the above are usable |
| `matches` | list[dict] | the card's legs | key set is 4.3 |
| `matches_count` | int | derived | always `len(matches)` |

The `'Unknown Accumulator'` / `tip_type` fallback strings passed to `dict.get()`
in the source are currently **unreachable**, because `parse_card` always sets the
`tip_category` key.

### 4.3 Accumulator leg

Exact key set: `date`, `time`, `match_title`, `teams`, `prediction`,
`opponent_text`, `tip_reason`, `match_url` — identical semantics to 4.1. There is
**no** `tip_category` and **no** `stake`/`returns`/`odds`, so a consumer must not
assume one leg shape for both payload kinds.

## 5. Field availability matrix

Legend used by every **Class** column in this document:

```text
A  = Available from current scraper
A* = Available from current scraper but source-derived,
     unreliable, incomplete, or non-authoritative
P  = Available only after future persisted-data work
U  = Unavailable; requires future provider/source
X  = Prototype example only
```

| Field | Payload location | Class | Notes |
| --- | --- | --- | --- |
| source display date | `date` on every envelope and every leg | **A\*** | source-derived display text only; not a fixture date, never UTC, never converted — see 13.3 |
| source display kick-off text | `time` | **A\*** | display text only; no date part, no timezone, never verified — see 13.3 |
| match title | `match_title` | **A\*** | derived; falls back to `prediction` — see 9.2 |
| team names | `teams` | **A\*** | 0–2 entries; fragile extraction, source-derived — see 9.1 |
| opponent display text | `opponent_text` | **A\*** | raw source text; the only place `vs`/`at` survives — see 6.4 |
| tip reason | `tip_reason` | **A\*** | may be `''` |
| source-relative match link | `match_url` | **A\*** | may be `''`; not a fixture ID |
| card stake | `stake` | **A\*** | `None` without a `BetGrid` |
| card returns | `returns` | **A\*** | `None` without a `BetGrid` |
| derived card odds | `odds`, `total_odds` (future name `derived_total_odds`) | **A\*** | `returns / stake`; parser-derived, not an independent price — see 9.7 |
| fetched-at stamp / source URL | `scraped_at`, `source_url` | **A\*** | added by `_fetch_one`, never by a parser; naive local time, diagnostic only — see 13.7 |
| result counts | `count`, `total_tips`, `total_cards`, `total_accumulators`, `matches_count` | **A** | counted from parsed elements |
| prediction text | `prediction` | **A** | free text, verbatim as published |
| source category label | `tip_category`, `category` | **A** | verbatim source `<h2>` |
| tip type | `tip_type` | **A** | constant, generic pages only |
| source label | `source` on success envelopes | **A** | constant `'freesupertips'` |
| per-match odds | — | **U** | see 6.1 |
| raw (fractional or American) odds | — | **U** | see 6.1 |
| bookmaker, market, line/handicap | — | **U** | see 6.1 |
| competition, league, country, season | — | **U** | see 6.2 |
| official fixture ID / third-party match ID | — | **U** | see 6.3 |
| verified home/away side | — | **U** | `teams` order is the page's order and is unverified; `opponent_text` keeps `vs`/`at` as text only — see 6.4 |
| authoritative kick-off timestamp | `kickoff_at` (future) | **U** | must stay null until a legitimate fixture provider supplies one; see 13.3 |
| kick-off verified flag | `kickoff_time_verified` (future) | **U** | always `false` today; `true` only with a real `kickoff_at`; see 13.3 |
| final score, result | — | **U** | see 6.5 |
| settled status (WON/LOST/VOID) | — | **U** | see 6.5 |
| winnings | — | **U** | see 6.5 |
| win rate and "Verified Accuracy" (measured) | — | **U** | see 6.6 |
| 99.6% accuracy | — | **X** | prototype example only, never measured — see 6.6 |
| 99.4% win rate | — | **X** | prototype example only, never measured — see 6.6 |
| prototype-only pricing | — | **X** | prototype/screenshot values only; production pricing is server-side and verified — see 6.6 and `docs/PRODUCT_DECISIONS.md` |
| team crests, kit colours, flags | — | **U** | see 6.7 |
| venue, attendance, referee, line-ups, injuries | — | **U** | see 6.7 |

The **A\*** class is not a weaker **A**. It marks a value the current scraper really
does emit whose meaning is source-derived: it may be displayed, but it must never
be treated as authoritative, verified, official, or machine-parseable as a
timestamp or identifier.

No row is **P** today. **P** means "available only after future persisted-data
work": nothing in the current payload depends on stored data, and every future
metric that would (for example a measured win rate over stored settled results)
also needs a results provider that does not exist yet, so it stays **U** until
such a provider is contracted.

## 6. Fields that are not available — do not invent them

None of the fields below may be added to a payload, mirrored in a DTO, or shown
in a client until a genuine source exists. Each rule is a hard constraint, not a
preference.

### 6.1 Per-leg odds, raw odds, bookmaker, market, line

The only prices on the page are the card-level stake selector and the card-level
returns box. `stake`, `returns`, and `total_odds` are card-level and are copied
onto every leg (see 9.6). Never present them as per-leg odds, never divide them
across legs, and never synthesise a fractional or American equivalent. No
bookmaker, market, or line/handicap value is parsed.

### 6.2 Competition, league, country, season

The markup the parsers read contains none of these. No payload field names a
competition. A client must not label a tip with a league it inferred from a team
name.

### 6.3 Official fixture IDs

The only identifier present is `match_url`, a relative source path. It is not a
fixture ID, is not guaranteed stable, and must never be used to join against a
results provider or to deduplicate tips across days.

### 6.4 Home/away designation and a UTC kick-off timestamp

`teams` order follows the page (usually home then away) but the parser does not
verify that, and `opponent_text` is the only place the `vs`/`at` distinction
survives. `date`/`time` are display text only: no offset, no timezone, no
authoritative fixture timestamp. Any real timestamp requires a different source —
see §13 for how the API and the client must handle that gap.

### 6.5 Final scores, results, settled status, winnings

The page does not publish results at read time. `handlers.ENABLE_VERIFICATION`
(default `False`) guards legacy helper code that adds only
`verification_enabled: True`, `verified: False`,
`verification_note: 'Soccerbase integration pending'`, and per-match
`verification_status: 'pending'` — it never produces a score, a
`WON`/`LOST`/`VOID` state, or winnings. `verified_scrapers.py` is unused
placeholder code that `views.py` and `urls.py` do not import.

Per `docs/PRODUCT_DECISIONS.md` §5: use `PENDING` for upcoming or unfinished
matches, `UNVERIFIED` / `RESULT AWAITING CONFIRMATION` for anything that cannot be
settled from a genuine source, and never fabricate scores, `WON`/`LOST` states, or
winnings.

### 6.6 Win-rate and "Verified Accuracy" — including the 99.6% and 99.4% figures

No parser, handler, or endpoint computes or stores a win-rate or accuracy value.
The 99.6% and 99.4% figures belong to the prototype visuals only: they are not
produced by any code path, are not stored anywhere in this repository, and must
never be presented as measured performance. An accuracy number cannot be derived
from scraped tips alone — it needs settled results (6.5) first.

The §5 matrix splits these into three separate classes, and they must not be
merged:

* measured win rate and "Verified Accuracy" — **U**: no provider and no settled
  results exist, so no truthful value can be produced.
* the `99.6%` accuracy and `99.4%` win-rate strings — **X**: prototype examples
  only; they are hard-coded visuals, not data.
* prototype-only pricing — **X**: prototype/screenshot values only; production
  pricing must come from verified server-side configuration
  (`docs/PRODUCT_DECISIONS.md`).

### 6.7 Team crests, kit colours, venue, line-ups, injuries

None are parsed. `teams` names are derived from logo *file names*, so they are a
display convenience and not a canonical team registry; `extract_team_from_style`
additionally cannot match a name containing `&` (see 9.1).

## 7. Source-safe terminology

`tip_category`, `category`, and `tip_type` are **source labels kept verbatim on
purpose**. They are not a controlled vocabulary, and a raw-text comparison against
them must not become a client-side switch or a database enum. Use `tip_type` (a
constant the code owns) for branching, and treat `category` as display text.

Never describe a tip as "verified", "settled", "won", or "accurate" using only
these fields. Never label `date`/`time` as UTC and never present them as a
verified kick-off time — see §13. Never present `date` as the fixture date: it is
the day the page was read. `source` is the constant `'freesupertips'` and is
internal technical identification, not user-facing copy; the user-facing brand is
**OddMate** / **ODDMATE TIPS** (see `docs/PRODUCT_DECISIONS.md` §6–§7).

## 8. Proposed normalization mapping (proposal only — not implemented)

Recorded so a later sprint does not have to reverse-engineer the strings. **None
of this is implemented**, no controlled value appears in any payload, and no
database column exists.

| Source `tip_category` (verbatim) | Source key | Proposed controlled value |
| --- | --- | --- |
| `Bet of the Day` | `bet_of_the_day` | `BET_OF_THE_DAY` |
| `Daily Accumulator` | `daily_accumulator` | `DAILY_ACCUMULATOR` |
| `Both Teams to Score Tips`, `BTTS Accumulator` | `both_teams_to_score` | `BTTS` |
| `BTTS and Win Tips`, `BTTS and Win Accumulator` | `btts_and_win` | `BTTS_AND_WIN` |
| `Over 2.5 Goals Tips`, `Over 2.5 Goals Accumulator` | `over_25_goals` | `OVER_25_GOALS` |
| `Anytime Goalscorer Tips`, `Anytime Goalscorer Accumulator` | `anytime_goalscorer` | `ANYTIME_GOALSCORER` |

Rules for whoever implements it:

* Map on the **source key** (or on the page that produced the card), never on the
  `<h2>` text. The labels change whenever the site's copy changes, several labels
  map to one key, and one label can be reused on another page.
* Keep the verbatim label in its own field for display and audit.
* Treat an unmapped label as "unknown" — never mint a new category at runtime.
* The `tip_type` constants the code already owns are the closest thing to a
  controlled vocabulary today: `over_2.5_goals`, `btts`, `btts_and_win`,
  `anytime_goalscorer`. Note the legacy `over_2.5_goals` spelling and the fact
  that the `both_teams_to_score` source key uses `btts` — do not "fix" either
  without treating it as a contract change.

## 9. Known gaps, risks, and pinned quirks

Every item below is asserted by a test, so a future change to `utils.py` that
alters this behaviour fails the suite instead of silently changing the contract
for downstream consumers.

### 9.1 Team extraction depends on a literal `&quot;` surviving in the parsed attribute

`extract_team_from_style` runs
`re.search(r'background-image:url\(&quot;([^&]+)&quot;\)', style)` against the
`style` attribute **after** BeautifulSoup has parsed the page. `html.parser`
decodes character references inside attribute values, so a source tag written as

```html
style="background-image:url(&quot;/image/team/Liverpool.png&quot;)"
```

reaches the regex as `background-image:url("/image/team/Liverpool.png")` — the
literal `&quot;` is gone, the regex does not match, and **no team name is
extracted**. Extraction only works when the parsed attribute still literally
contains the six characters `&quot;`, which requires the source markup to be
double escaped (`&amp;quot;`).

Pinned by tests:

* with single escaping, `teams` collapses to the opponent text only (`['Everton']`
  for a `vs Everton` leg) and `match_title` falls back to `prediction`
  (`fixtures/single_escaped_team_style.html`);
* with double escaping both team names come through
  (`fixtures/bet_of_the_day.html` → `['Liverpool', 'Manchester United']`);
* `teams` therefore holds 0, 1, or 2 entries and must never be assumed to contain
  two names in a fixed order;
* other limits: only the `.png` and `.jpg` suffixes are stripped
  (`Getafe.svg` stays `Getafe.svg`), a filename containing `&` never matches
  (the capture group is `[^&]+`), an empty or unrelated `style` returns `''`, and
  a duplicate `style` attribute does not raise — the last attribute wins.

**This is the largest single risk to the contract**: if the live page single-escapes
`&quot;`, `teams` is always empty in production and only `match_title` (from the
prediction text) carries any team information. It can only be resolved against a
genuine page capture (§10).

### 9.2 `match_title` falls back to the prediction

`match_title` is `" vs ".join(teams)` only when two or more team names were found;
otherwise it is the prediction text, and `''` when there is no prediction. Combined
with 9.1, a card can legitimately have `match_title == prediction`, which is not a
match title at all. **Never use `match_title` as a match identifier or as an
input to matching against a results provider.**

### 9.3 A leg with no prediction is still counted

A completely empty `<div class="Leg">` produces an all-default leg (`date`
filled, every other field `''` or `[]`) and **is** counted in `count` and
`matches_count`. A leg with a time and an opponent but no prediction keeps
`teams: ['Everton']`, `prediction: ''`, and `match_title: ''`. A consumer must
filter on empty `match_title`/`prediction` before displaying or persisting
anything.

### 9.4 One unreadable card is dropped whole

`parse_card` wraps its body in a `try`. A non-numeric active stake value makes
`float()` raise, so it logs a warning and returns
`{'tip_category': '', 'matches': [], 'count': 0}` — the **entire card**, including
its otherwise-perfect legs, disappears from the payload. Sibling cards are
unaffected. Consequences: `total_cards` can exceed `total_accumulators`, and
`total_tips`/`count` can be lower than the number of legs visibly on the page.

### 9.5 A leg that raises is skipped

`parse_leg` is also wrapped in a `try`; a raising leg logs a warning and returns
`None`, and only that leg is dropped, so the card and its odds survive. A
duplicate `style` attribute does **not** raise in BeautifulSoup (the last value
wins and stays a `str`), so this path is exercised in the test suite by injecting
a failing `extract_team_from_style`.

### 9.6 Card-level odds are repeated onto every leg

`stake`, `returns`, and `odds` on a tip-card match are the **card's** values. A
four-leg card shows the same three numbers on all four legs. Deduplicate on the
card, never aggregate them per leg, and never sum them across legs.

### 9.7 Odds are derived, not read

`odds` / `total_odds` is `round(returns / stake, 2)`, computed from two independent
page elements. It is a display derivation only: never present it as a bookmaker's
price, and expect `None` whenever either element is missing, the stake is not a
positive number, or the active option's value cannot be parsed as a float.

### 9.8 Category fallbacks are unreachable

`parse_card` always sets `tip_category`, so the `'Unknown Accumulator'` and
`tip_type` defaults passed to `dict.get()` in the page parsers never trigger. A
card without an `<h2>` therefore yields `category: ''` — which a consumer must
handle as an empty label, not as an error.

### 9.9 Empty-leg defaults differ between the two payload kinds

A tip-card match always carries `tip_category`, `stake`, `returns`, and `odds` —
even for an empty leg, where they may be `None` or the card's values. An
accumulator leg carries only the eight leg keys. A single DTO must not assume one
key set covers both, and a strict decoder will reject whichever shape it was not
written for.

## 10. Fixture provenance and refresh procedure

Fixtures live in `odds/alltips_scraper/fixtures/`. They are **static,
hand-written, synthetic** parser inputs — not captured pages and not Django
`loaddata` fixtures (`FIXTURE_DIRS` does not point there). Tests read them with
`Path.read_bytes()` and never fetch anything; `tests_offline_guard.py` disables the
socket layer and proves the fetch layer fails closed.

`fixtures/README.md` holds the selector table, the committing rules, and the
per-file index.

Because the fixtures are synthetic, they prove the contract of the code **as
written**, not that the code matches the live page. Real-capture validation is a
manual, later task. When it happens:

1. Obtain the page deliberately, out of band — never from a test run.
2. Sanitize it: strip cookies, response headers, tracking IDs, account/payment
   fragments, and any hostname the team treats as private.
3. Re-check it against the selector table in `fixtures/README.md`.
4. Update `tests_parser_contract.py` expectations in the same change.
5. Record the capture date here and list every difference from the synthetic
   fixtures — in particular whether the real markup uses `&quot;` (single) or
   `&amp;quot;` (double), because that decides whether `teams` ever populates
   (9.1).

Fixture hygiene is enforced by tests: every HTML fixture must be ASCII-only, open
with a `SYNTHETIC PARSER FIXTURE` provenance comment, contain no `http://`,
`https://`, or `www.` string, and contain none of a list of credential-shaped
substrings. No test module may import a live HTTP client.

If a fixture is ever replaced by a genuine capture, the capture is committed as a
*new* reviewed file rather than silently overwriting a synthetic one, so the
synthetic baseline remains available for comparison.

## 11. How to verify

From the repository root:

```powershell
.\\.venv\\Scripts\\python.exe .\\odds\\manage.py check
.\\.venv\\Scripts\\python.exe .\\odds\\manage.py test alltips_scraper -v 2 --noinput
git diff --check
```

Expected: no system-check issues, every test passes, and a clean whitespace check.
The app label is required (`odds/` is not a Python package). The suite runs fully
offline: the socket layer is disabled for every parser-contract test and the fetch
layer is exercised only through a mock client that always fails.

`tests_parser_contract.py` pins the envelopes, key sets, per-field values, error
envelopes, malformed-card and malformed-leg behaviour, the date injection, and
fixture hygiene. `tests_offline_guard.py` proves the isolation. `tests.py` keeps
the legacy cache/health contract green.

## 12. Change rules

* The three envelopes and their key sets are frozen. Adding, renaming, or removing
  a key is a contract change: update `tests_parser_contract.py` and this document
  in the same change.
* Do not change the six legacy source keys, the `tip_type` constants, the
  `category` strings, the error message strings, or `'freesupertips'`.
* `current_date` is optional. Any new parameter must also be optional, and the
  legacy no-argument call path (`SCRAPER_CONFIGS[key]['parser'](html)`) must keep
  working — the configured lambdas intentionally keep a one-argument signature.
* Never invent a field from §6. If a client needs one, obtain a genuine source
  first, document it here, and only then add it to a payload.
* Never write `SCRAPE_URL`, a host, or any credential into this document, the
  fixtures, a log line, a test, or a fixture comment.
* Timestamps follow §13. Source display text stays display text, authoritative
  timestamps are timezone-aware UTC, and no user-facing date filter may use the
  server's timezone.
* Keep the parsers free of network access. A parser takes `bytes` in and returns a
  dict; fetching belongs to `_fetch_one`.
* Do not promote a source `<h2>` label to a permanent category. §8 is a proposal
  only.
* The versioned `GET /api/v1/tips/` surface is additive to these rules: it is the
  versioned contract for the same six source keys and is documented in
  `docs/API_V1_CONTRACT.md`. Changing it updates that document and
  `odds/alltips_scraper/tests_api_v1.py` in the same change. Its out-of-band writer,
  `refresh_v1.py` run by `manage.py refresh_tips`, stores the source envelope as
  fetched and changes nothing in this contract; it is documented in
  `docs/RUNBOOK.md` section 4.2.

## 13. Timezone architecture (authoritative UTC storage, device-local display)

**Status: architecture only.** Sprint 1B implements none of this and adds no
user-timezone filtering. It is recorded here so that the first storage model, the
first serializer, and the Flutter client are all built to one rule instead of
three.

### 13.1 The rules

1. Django stores authoritative timestamps in **UTC**, in timezone-aware
   datetime fields (`models.DateTimeField`, never a naive string column).
2. Django APIs return authoritative timestamps as **ISO-8601 UTC** values
   (`2026-09-28T18:00:00Z`), never as a server-local formatted string.
3. Flutter converts an authoritative UTC timestamp to the **device/user local
   timezone** for display, and only for display.
4. Flutter must **not** use the backend server's local timezone. The server runs
   in UTC; its timezone is not a display preference.
5. Flutter must **not** convert an unknown source display time into local time.
   Text that arrived without a known timezone stays exactly the text that
   arrived.

Rules 3 and 5 are two halves of one discipline: a value may only be converted
when its timezone is known to be UTC, and only a stored authoritative timestamp
guarantees that.

### 13.2 What the code does today

* `odds/odds/settings.py` already sets `TIME_ZONE = 'UTC'` and `USE_TZ = True`,
  so Django's own date/time helpers are UTC-based and aware. `alltips_scraper`
  has **no models**, so rule 1 has nothing to apply to yet.
* The parser produces no reliable, timezone-aware fixture kick-off timestamp. It
  has source-derived display text only (`date`, `time` — see 4.1, 6.4).
* The current `scraped_at` is a naive local string (13.7), so it is not an
  authoritative UTC value either.

### 13.3 Sprint 1 rule: source text stays source text

Because the scraper cannot supply an authoritative timestamp, Sprint 1 handles
these values as follows. Each line is a do-not-do, not a preference:

* `kickoff_at` stays **null / unavailable**. Nothing populates it, nothing
  derives it from `date` + `time`.
* **No** timezone is inferred for any value, and no offset is fabricated.
* The source display time is **never** labelled "UTC" in a payload, a document,
  a log line, or UI copy.
* Flutter does **not** convert the source display time as though it were
  verified.
* `kickoff_time_verified` stays `false`.
* `kickoff_at` becomes non-null only when a **legitimate fixture provider**
  supplies an authoritative timestamp — not a scraper heuristic, not a
  hard-coded offset, not a guess from the source's locale.

#### 13.3.1 The policy, restated without hedging

Current frozen legacy parser keys remain `date` and `time`.

* They are source-derived display fields only.
* They are not verified UTC timestamps.
* They must not be converted to user local time.
* They must not be used to derive `kickoff_at`.
* They must not be labelled as official fixture times.
* In §5 they are classified **A\***: emitted by the current scraper, but
  source-derived and non-authoritative, so they may be shown as source text and
  nothing more.

Future persisted models and future `/api/v1/` output will use:

* `source_date_text`
* `source_time_text`

Future verified timestamps from a legitimate fixture provider will use:

* `kickoff_at`
* `kickoff_time_verified`

Until such a provider exists, `kickoff_at` stays `null` and
`kickoff_time_verified` stays `false`. Neither may be back-filled from `date`,
`time`, `source_date_text`, or `source_time_text`.

Sprint 1C's additive `/api/v1/tips/` serializer surface now publishes
`source_date_text` and `source_time_text`, while the legacy parser envelopes
and their pinned key sets remain unchanged. Nothing is persisted yet.
`kickoff_at` and `kickoff_time_verified` appear in that response only as the
serializer's unavailable markers (`null` / `false`); they are not values the read
model publishes. See `docs/API_V1_CONTRACT.md`.

### 13.4 Key mapping (target naming — not yet applied to the live payload)

> **Sprint 1C status:** the versioned `GET /api/v1/tips/` serializer surface
> already publishes the target names `source_date_text` and `source_time_text`,
> and it emits `kickoff_at: null` / `kickoff_time_verified: false` as unavailable
> markers on every selection. It stores nothing and it does **not** rename the
> live legacy payload, so this heading and the mapping below still hold unchanged.
> `docs/API_V1_CONTRACT.md` records that surface.

The names the architecture requires for source text are `source_date_text` and
`source_time_text`. They map one-to-one onto today's payload keys:

| today's key | target name | meaning |
| --- | --- | --- |
| `date` | `source_date_text` | source display date the page was read, `YYYY-MM-DD` |
| `time` | `source_time_text` | source display kick-off text, e.g. `19:45`, may be `''` |

**Sprint 1B does not rename them.** The envelopes and key sets in §3 are frozen
(§12), `date`/`time` are consumed by existing tests, and the payload is an
internal scraper structure, not the public API. The rename belongs to the change
that introduces the persistence layer and the public serializer — the point at
which "store as `source_date_text`/`source_time_text`" becomes an actual
write — and must land with its test updates in one change (§12).

Until then the rule is about **meaning**, not spelling: whatever the key is
called, the value is display text, it is not UTC, it is not verified, and it must
not be converted.

Both names belong to the future persisted models, not to the scraper payload. In
§5 the values behind them stay **A\*** — source-derived, non-authoritative display
text — whatever the key is eventually called.

### 13.5 API shapes

With an authoritative timestamp (only once a real provider supplies one):

```json
{
  "kickoff_at": "2026-09-28T18:00:00Z",
  "kickoff_time_verified": true
}
```

With scraped display text only — **this is the Sprint 1 shape**:

```json
{
  "source_date_text": "2026-09-28",
  "source_time_text": "18:00",
  "kickoff_at": null,
  "kickoff_time_verified": false
}
```

`kickoff_at: null` is a deliberate, honest value: it means "no authoritative
timestamp exists". It is not a placeholder for a guessed one, and a client must
render it as unknown (§6.4) rather than back-filling it from
`source_date_text` + `source_time_text`.

### 13.6 Flutter rules

* Render an authoritative timestamp with `DateTime.parse(value).toLocal()`, where
  `value` is the ISO-8601 UTC string from the API. `toLocal()` uses the device
  timezone, which is exactly what rule 3 requires.
* Do not accept a pre-formatted date/time string from the backend for
  authoritative values: the backend sends ISO-8601 UTC so the device decides the
  display zone.
* Do **not** call `.toLocal()` — or any equivalent — on `source_date_text` /
  `source_time_text`, and do not feed them to a parser that assumes UTC. Show
  them as text and treat them as unverified.
* When `kickoff_at` is null, show "time unavailable" or the source text as-is.
  Never back-fill it from the device clock or from a server-computed local time.
* Any label that depends on "now" (for example a relative date) is computed in
  the device timezone, not from a server-supplied local date.

### 13.7 Known deviations in the current code (fix before relying on them)

Recorded rather than hidden. None of these is fixed in Sprint 1B:

* `utils._fetch_one()` sets `scraped_at = datetime.now().isoformat()`: naive
  **local** time with no offset, so it is not rule-2 compliant and a client must
  not parse it as UTC. It is a volatile diagnostic stamp, not an authoritative
  event time.
* `handlers.get_all_tips()` (`handlers.py:147`, stamp at line 156) does the same.
  That helper is unrouted legacy code.
* `decorators.build_cache_key()` uses `timezone.localdate()`, which resolves in
  `TIME_ZONE = 'UTC'`. That is correct for an internal cache key and is **not** a
  user-facing "today"; a user-facing Today/Tomorrow filter must not reuse it
  (13.8).

The eventual fix is `django.utils.timezone.now()` (aware, UTC) serialised with an
explicit offset, landed as one contract change with its tests — not a scattered
edit, and not part of Sprint 1B.

### 13.8 Future date filtering (not implemented in Sprint 1B)

When Flutter later offers "Today", "Tomorrow", or a selected-date filter:

* Flutter sends the user's timezone **explicitly**: a validated IANA name (for
  example `Europe/London`) as a query parameter, or a documented request header.
  The backend validates it and rejects an unknown zone; it never falls back to the
  server's timezone.
* The backend applies the day boundaries in that supplied timezone (for example
  with `zoneinfo.ZoneInfo` and `timezone.activate`) and converts them to UTC for
  the query, while storage stays UTC (rule 1).
* The backend machine timezone and the developer's timezone are never inputs to a
  user-facing date boundary.
* The response may echo the timezone actually applied so the client can label the
  result, but its authoritative timestamps stay ISO-8601 UTC (rule 2).
* A request without a timezone is not silently defaulted to a server-local "today";
  either the endpoint rejects it, or the absent value is defined as "no date
  filter".

Feasibility note: this environment already resolves IANA zones (`tzdata` is
installed alongside `zoneinfo`, and `ZoneInfo('Europe/London')` loads), so 13.8
needs no new dependency when it is scheduled.

Sprint 1C's `/api/v1/tips/` accepts `date` and `timezone`, validates the IANA
timezone name and echoes it in `filter`, but does not implement the §13.8
timezone-aware UTC day-window query.

**Sprint 1B implements none of 13.8.** The six legacy endpoints accept no date or
timezone parameter, and adding one is a contract change under §12.