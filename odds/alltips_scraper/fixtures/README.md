# Scraper parser fixtures (test-only, static HTML)

These files are **static, offline test inputs** for the `alltips_scraper` HTML
parsers. They are read from disk by the parser contract tests in
`alltips_scraper/tests_parser_contract.py`. They are never fetched, refreshed,
or regenerated during a test run, and no test performs a network request.

## What these files are not

- They are **not** Django `loaddata` fixtures. No management command or test
  reads them through `loaddata`, and `FIXTURE_DIRS` does not point here. They are
  HTML documents consumed by `BeautifulSoup`.
- They are **not** captured production pages. See *Provenance* below.

## Provenance

Every file in this directory is a **synthetic parser fixture**, written by hand
to contain exactly the selectors, attribute names, and value shapes that the
existing parser functions in `alltips_scraper/utils.py` read today:

| Selector | Read by | Field produced |
| --- | --- | --- |
| `div.Card` | `parse_*_page`, `parse_card` | card boundary |
| `header.TipHeader > h2` | `parse_card` | `tip_category` / `category` |
| `div.Leg` | `parse_card` | one match/leg |
| `time` | `parse_leg` | `time` |
| `div.Teams > div.Img.Team.Team--xs[style]` | `parse_leg` | `teams` |
| `div.Leg__title > div.Leg__win` | `parse_leg` | `prediction` |
| `div.Leg__title > div.Leg__lose` | `parse_leg` | `opponent_text` |
| `div.Exp-collapse > div.TipReason__body > p` | `parse_leg` | `tip_reason` |
| `div.TipReason__header > a.TipReason__link[href]` | `parse_leg` | `match_url` |
| `div.BetGrid > select[aria-label="select your stake"] > option.active[value]` | `parse_card` | `stake` |
| `div.BetGrid > div.BetGrid__returns` | `parse_card` | `returns`, `total_odds` |

No live request was made to produce any of them and no "real capture" claim is
made anywhere. **Validating these fixtures against a genuine source page remains
a manual, later task** — see `docs/DATA_CONTRACT.md` →
*Fixture provenance and refresh procedure*.

## The `&amp;quot;` requirement (important)

`parse_leg` extracts team names with the regex

```python
r'background-image:url\(&quot;([^&]+)&quot;\)'
```

run against the `style` attribute **after** BeautifulSoup has parsed the page.
BeautifulSoup (`html.parser`) decodes character references inside attribute
values, so a source page containing

```html
style="background-image:url(&quot;/wp-content/themes/.../Liverpool.png&quot;)"
```

reaches the regex as
`background-image:url("/wp-content/themes/.../Liverpool.png")` — the literal
`&quot;` is gone and **no team name is extracted**.

For the regex to match, the parsed attribute must still literally contain the six
characters `&quot;`, which means the source markup has to be double escaped
(`&amp;quot;`). The fixtures here therefore use `&amp;quot;` wherever team
extraction has to be exercised, and `single_escaped_team_style.html` pins the
single-escaped behaviour that yields no teams.

## Committing rules

These files are safe to commit. They contain:

- no secrets, tokens, cookies, API keys, or environment values
- no personal data or user information
- no payment, card, or account data
- no private hostnames or internal URLs

Every fixture starts with an HTML comment that identifies it as a synthetic
parser fixture.

## Refresh procedure

There is no automatic refresh, and nothing in the test suite ever re-fetches a
fixture. Replacing a fixture with a genuine capture is a **manual, reviewed**
change:

1. A human obtains the page with a deliberate, out-of-band request — never from
   a test run.
2. The markup is sanitized: strip cookies, response headers, tracking IDs, any
   account or payment fragments, and any hostname or URL the team treats as
   private.
3. The file is re-checked against the selector table above.
4. Expected values in `tests_parser_contract.py` are updated in the same change.
5. `docs/DATA_CONTRACT.md` is updated with the capture date and the observed
   differences from the synthetic fixtures.

## File index

| File | Exercises |
| --- | --- |
| `bet_of_the_day.html` | `parse_bet_of_the_day_page` — multi-card/multi-leg, `%20` team name, `vs`/`at` opponent branches, single-image + opponent fallback |
| `daily_accumulator.html` | `parse_accumulator_page` — accumulator key set, per-card odds, `matches_count` |
| `over_25_goals.html` | `parse_generic_tips_page(html, 'over_2.5_goals')`, `.jpg` image stripping, and a card with **no** `BetGrid` (all odds `None`) |
| `both_teams_to_score.html` | `parse_generic_tips_page(html, 'btts')` |
| `btts_and_win.html` | `parse_generic_tips_page(html, 'btts_and_win')` |
| `anytime_goalscorer.html` | `parse_generic_tips_page(html, 'anytime_goalscorer')` |
| `empty_page.html` | the "no cards" error envelope for all three page parsers |
| `malformed_card.html` | unparseable `option.active` stake value -> whole card discarded, sibling card survives |
| `malformed_leg.html` | empty leg, leg with no prediction, team-list arithmetic |
| `single_escaped_team_style.html` | single-escaped `&quot;` -> no teams extracted |
| `comma_returns.html` | thousands separator inside `BetGrid__returns` |
