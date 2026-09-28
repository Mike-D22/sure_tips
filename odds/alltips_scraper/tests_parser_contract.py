"""Offline parser-contract tests for the ``alltips_scraper`` HTML parsers.

Why this module exists
----------------------
The legacy cache/health tests in ``tests.py`` use hand-written payload mirrors,
so nothing in the suite pinned what the real parser functions actually return.
This module calls the real parsers directly against static HTML in
``alltips_scraper/fixtures/`` and asserts exact output keys, values, and error
envelopes, so future models, ingestion code, and clients cannot be built on
invented field names.

Ground rules
------------
* No network. Every test runs with the socket layer disabled (see
  ``OfflineGuardMixin``) and fixtures are read from disk with ``Path.read_bytes``.
* No database. ``SimpleTestCase`` only - no models, no migrations, no writes.
* No live source page is required, and nothing here refreshes a fixture.
* The parsers keep their frozen legacy behaviour. The only production change
  that made these tests possible is an optional *date injection* argument, so
  the ``date`` field can be pinned instead of read from the machine clock.

See ``docs/DATA_CONTRACT.md`` for the documented contract, the field
availability matrix, and the fixture provenance rule.
"""

import socket
from datetime import datetime
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from . import utils
from .utils import (
    parse_accumulator_page,
    parse_bet_of_the_day_page,
    parse_generic_tips_page,
)

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'

# A fixed injected date keeps every expected payload machine independent.
TEST_DATE = '2026-09-27'

# The exact key sets the parsers produce today. These are the contract.
LEG_KEYS = frozenset({
    'date', 'time', 'match_title', 'teams', 'prediction', 'opponent_text',
    'tip_reason', 'match_url',
})
TIP_CARD_MATCH_KEYS = LEG_KEYS | {'tip_category', 'stake', 'returns', 'odds'}
BET_OF_THE_DAY_ENVELOPE_KEYS = frozenset({
    'date', 'total_tips', 'total_cards', 'matches', 'count', 'source',
})
ACCUMULATOR_ENVELOPE_KEYS = frozenset({
    'date', 'total_accumulators', 'accumulators', 'count', 'source',
})
GENERIC_ENVELOPE_KEYS = ACCUMULATOR_ENVELOPE_KEYS | {'tip_type'}
ACCUMULATOR_KEYS = frozenset({
    'category', 'stake', 'returns', 'total_odds', 'matches', 'matches_count',
})
GENERIC_ACCUMULATOR_KEYS = ACCUMULATOR_KEYS | {'tip_type'}

# Fields the source does not provide today. The parsers must never invent them.
FORBIDDEN_FIELDS = frozenset({
    'competition', 'league', 'scores', 'score', 'result', 'home_score',
    'away_score', 'fixture_id', 'official_fixture_id', 'odds_per_leg',
    'raw_odds', 'leg_odds', 'kickoff_utc', 'kickoff_timestamp', 'team_logo',
    'home_logo', 'away_logo', 'status', 'settled', 'won', 'lost', 'void',
    'winnings', 'result_status', 'win_rate', 'accuracy',
})

# One fixture per configured source key.
FIXTURE_FOR_SOURCE = {
    'bet_of_the_day': 'bet_of_the_day.html',
    'daily_accumulator': 'daily_accumulator.html',
    'over_25_goals': 'over_25_goals.html',
    'both_teams_to_score': 'both_teams_to_score.html',
    'btts_and_win': 'btts_and_win.html',
    'anytime_goalscorer': 'anytime_goalscorer.html',
}

# (source key, fixture, tip_type, [(category, matches_count, stake, returns, total_odds)])
GENERIC_SOURCES = [
    ('over_25_goals', 'over_25_goals.html', 'over_2.5_goals', [
        ('Over 2.5 Goals Tips', 2, 10.0, 19.6, 1.96),
        ('Over 2.5 Goals Accumulator', 1, None, None, None),
    ]),
    ('both_teams_to_score', 'both_teams_to_score.html', 'btts', [
        ('Both Teams to Score Tips', 2, 10.0, 22.5, 2.25),
        ('BTTS Accumulator', 1, 10.0, 17.2, 1.72),
    ]),
    ('btts_and_win', 'btts_and_win.html', 'btts_and_win', [
        ('BTTS and Win Tips', 2, 10.0, 46.0, 4.6),
        ('BTTS and Win Accumulator', 1, 10.0, 33.8, 3.38),
    ]),
    ('anytime_goalscorer', 'anytime_goalscorer.html', 'anytime_goalscorer', [
        ('Anytime Goalscorer Tips', 2, 10.0, 48.6, 4.86),
        ('Anytime Goalscorer Accumulator', 1, 10.0, 21.3, 2.13),
    ]),
]


def load_fixture(name: str) -> bytes:
    """Read one static HTML fixture from disk. Never fetches over the network."""
    return (FIXTURES_DIR / name).read_bytes()


class OfflineGuardMixin:
    """Disable the socket layer for every test method of a subclass.

    Any accidental outbound request raises instead of silently reaching the
    network, so the parser tests cannot become live scraper requests.
    """

    def setUp(self):
        super().setUp()

        def blocked(*args, **kwargs):
            raise AssertionError('network access is not allowed in parser tests')

        for target in ('socket.socket', 'socket.create_connection', 'socket.getaddrinfo'):
            patcher = mock.patch(target, side_effect=blocked)
            patcher.start()
            self.addCleanup(patcher.stop)


class BetOfTheDayParserContractTests(OfflineGuardMixin, SimpleTestCase):
    """``parse_bet_of_the_day_page`` -> one flat ``matches`` list."""

    def setUp(self):
        super().setUp()
        self.result = parse_bet_of_the_day_page(
            load_fixture('bet_of_the_day.html'), TEST_DATE
        )

    def test_top_level_envelope_keys_are_exact(self):
        self.assertEqual(set(self.result), set(BET_OF_THE_DAY_ENVELOPE_KEYS))

    def test_envelope_values(self):
        self.assertEqual(self.result['date'], TEST_DATE)
        self.assertEqual(self.result['total_tips'], 3)
        self.assertEqual(self.result['total_cards'], 2)
        self.assertEqual(self.result['count'], 3)
        self.assertEqual(self.result['source'], 'freesupertips')
        self.assertEqual(len(self.result['matches']), 3)

    def test_every_match_uses_the_exact_tip_card_key_set(self):
        for index, match in enumerate(self.result['matches']):
            with self.subTest(match=index):
                self.assertEqual(set(match), set(TIP_CARD_MATCH_KEYS))

    def test_first_match_is_fully_populated(self):
        self.assertEqual(self.result['matches'][0], {
            'date': TEST_DATE,
            'time': '19:45',
            'match_title': 'Liverpool vs Manchester United',
            'teams': ['Liverpool', 'Manchester United'],
            'prediction': 'Liverpool to win',
            'opponent_text': 'vs Manchester United',
            'tip_reason': 'Liverpool are unbeaten at home in their last eleven league games.',
            'match_url': '/tips/liverpool-vs-manchester-united/',
            'tip_category': 'Bet of the Day',
            'stake': 10.0,
            'returns': 31.2,
            'odds': 3.12,
        })

    def test_opponent_text_derives_the_second_team_when_only_one_image_exists(self):
        match = self.result['matches'][1]
        self.assertEqual(match['teams'], ['Arsenal', 'Chelsea'])
        self.assertEqual(match['opponent_text'], 'at Chelsea')
        self.assertEqual(match['match_title'], 'Arsenal vs Chelsea')
        self.assertEqual(match['match_url'], '')
        self.assertEqual(
            match['tip_reason'],
            'Arsenal have won four of their last five away trips in the league.',
        )
        self.assertEqual(match['tip_category'], 'Bet of the Day')

    def test_percent_encoded_team_name_is_decoded(self):
        match = self.result['matches'][2]
        self.assertEqual(match['teams'], ['Bayern Munich', 'Dortmund'])
        self.assertEqual(match['match_title'], 'Bayern Munich vs Dortmund')
        self.assertEqual(match['prediction'], 'Over 2.5 goals')
        self.assertEqual(match['time'], '17:30')
        self.assertEqual(
            match['tip_reason'],
            'Both sides average more than three goals per game this season.',
        )
        self.assertEqual(match['match_url'], '/tips/bayern-munich-vs-dortmund/')

    def test_card_level_odds_are_derived_from_returns_over_stake(self):
        self.assertEqual(self.result['matches'][0]['stake'], 10.0)
        self.assertEqual(self.result['matches'][0]['returns'], 31.2)
        self.assertEqual(self.result['matches'][0]['odds'], 3.12)
        self.assertEqual(self.result['matches'][2]['stake'], 10.0)
        self.assertEqual(self.result['matches'][2]['returns'], 18.5)
        self.assertEqual(self.result['matches'][2]['odds'], 1.85)

    def test_source_category_labels_are_carried_through_verbatim(self):
        self.assertEqual(
            [match['tip_category'] for match in self.result['matches']],
            ['Bet of the Day', 'Bet of the Day', 'Best Football Tips Today'],
        )

    def test_parser_invents_no_unavailable_fields(self):
        for index, match in enumerate(self.result['matches']):
            with self.subTest(match=index):
                self.assertEqual(set(match) & FORBIDDEN_FIELDS, set())


class DailyAccumulatorParserContractTests(OfflineGuardMixin, SimpleTestCase):
    """``parse_accumulator_page`` -> one object per accumulator card."""

    def setUp(self):
        super().setUp()
        self.result = parse_accumulator_page(
            load_fixture('daily_accumulator.html'), TEST_DATE
        )

    def test_top_level_envelope_keys_are_exact(self):
        self.assertEqual(set(self.result), set(ACCUMULATOR_ENVELOPE_KEYS))

    def test_envelope_values(self):
        self.assertEqual(self.result['date'], TEST_DATE)
        self.assertEqual(self.result['total_accumulators'], 2)
        self.assertEqual(self.result['count'], 5)
        self.assertEqual(self.result['source'], 'freesupertips')

    def test_accumulator_key_set_is_exact_and_has_no_tip_type(self):
        for index, accumulator in enumerate(self.result['accumulators']):
            with self.subTest(accumulator=index):
                self.assertEqual(set(accumulator), set(ACCUMULATOR_KEYS))
                self.assertNotIn('tip_type', accumulator)

    def test_leg_key_set_is_exact_and_carries_no_card_level_odds(self):
        for index, accumulator in enumerate(self.result['accumulators']):
            for leg_index, leg in enumerate(accumulator['matches']):
                with self.subTest(accumulator=index, leg=leg_index):
                    self.assertEqual(set(leg), set(LEG_KEYS))
                    self.assertEqual(set(leg) & FORBIDDEN_FIELDS, set())

    def test_first_accumulator_matches_the_fixture(self):
        accumulator = self.result['accumulators'][0]
        self.assertEqual(accumulator['category'], 'Daily Accumulator')
        self.assertEqual(accumulator['stake'], 10.0)
        self.assertEqual(accumulator['returns'], 84.5)
        self.assertEqual(accumulator['total_odds'], 8.45)
        self.assertEqual(accumulator['matches_count'], 3)
        self.assertEqual(accumulator['matches'][0], {
            'date': TEST_DATE,
            'time': '12:30',
            'match_title': 'Liverpool vs Manchester United',
            'teams': ['Liverpool', 'Manchester United'],
            'prediction': 'Liverpool to win',
            'opponent_text': 'vs Manchester United',
            'tip_reason': 'Liverpool have scored first in nine of their last ten home games.',
            'match_url': '/tips/liverpool-vs-manchester-united/',
        })

    def test_second_accumulator_matches_the_fixture(self):
        accumulator = self.result['accumulators'][1]
        self.assertEqual(accumulator['category'], 'Both Teams to Score Accumulator')
        self.assertEqual(accumulator['stake'], 10.0)
        self.assertEqual(accumulator['returns'], 26.4)
        self.assertEqual(accumulator['total_odds'], 2.64)
        self.assertEqual(accumulator['matches_count'], 2)

    def test_count_is_the_sum_of_matches_count(self):
        self.assertEqual(
            self.result['count'],
            sum(a['matches_count'] for a in self.result['accumulators']),
        )
        self.assertEqual(
            self.result['count'],
            sum(len(a['matches']) for a in self.result['accumulators']),
        )

    def test_legs_are_linked_to_their_card_and_not_reordered(self):
        self.assertEqual(
            [leg['time'] for leg in self.result['accumulators'][0]['matches']],
            ['12:30', '15:00', '20:00'],
        )
        self.assertEqual(
            [leg['teams'] for leg in self.result['accumulators'][1]['matches']],
            [['Osasuna', 'Brentford'], ['Dortmund', 'Bayern Munich']],
        )


class GenericTipsParserContractTests(OfflineGuardMixin, SimpleTestCase):
    """``parse_generic_tips_page`` for the four remaining source keys."""

    def test_every_generic_source_matches_its_fixture(self):
        for source, fixture, tip_type, expected_cards in GENERIC_SOURCES:
            with self.subTest(source=source):
                result = parse_generic_tips_page(
                    load_fixture(fixture), tip_type, TEST_DATE
                )

                self.assertEqual(set(result), set(GENERIC_ENVELOPE_KEYS))
                self.assertEqual(result['date'], TEST_DATE)
                self.assertEqual(result['tip_type'], tip_type)
                self.assertEqual(result['source'], 'freesupertips')
                self.assertEqual(result['total_accumulators'], len(expected_cards))
                self.assertEqual(
                    result['count'], sum(card[1] for card in expected_cards)
                )

                for index, expected in enumerate(expected_cards):
                    category, matches_count, stake, returns, total_odds = expected
                    accumulator = result['accumulators'][index]
                    self.assertEqual(set(accumulator), set(GENERIC_ACCUMULATOR_KEYS))
                    self.assertEqual(accumulator['tip_type'], tip_type)
                    self.assertEqual(accumulator['category'], category)
                    self.assertEqual(accumulator['matches_count'], matches_count)
                    self.assertEqual(len(accumulator['matches']), matches_count)
                    self.assertEqual(accumulator['stake'], stake)
                    self.assertEqual(accumulator['returns'], returns)
                    self.assertEqual(accumulator['total_odds'], total_odds)
                    for leg in accumulator['matches']:
                        self.assertEqual(set(leg), set(LEG_KEYS))

    def test_generic_pages_have_no_flat_matches_list(self):
        result = parse_generic_tips_page(
            load_fixture('btts_and_win.html'), 'btts_and_win', TEST_DATE
        )
        self.assertNotIn('matches', result)
        self.assertNotIn('total_tips', result)

    def test_source_category_is_kept_verbatim_and_never_replaced_by_tip_type(self):
        result = parse_generic_tips_page(
            load_fixture('both_teams_to_score.html'), 'btts', TEST_DATE
        )
        self.assertEqual(
            [accumulator['category'] for accumulator in result['accumulators']],
            ['Both Teams to Score Tips', 'BTTS Accumulator'],
        )

    def test_missing_bet_grid_yields_none_odds_rather_than_a_guess(self):
        result = parse_generic_tips_page(
            load_fixture('over_25_goals.html'), 'over_2.5_goals', TEST_DATE
        )
        accumulator = result['accumulators'][1]
        self.assertIsNone(accumulator['stake'])
        self.assertIsNone(accumulator['returns'])
        self.assertIsNone(accumulator['total_odds'])
        self.assertEqual(accumulator['matches_count'], 1)

    def test_jpg_team_image_suffix_is_stripped_like_png(self):
        result = parse_generic_tips_page(
            load_fixture('over_25_goals.html'), 'over_2.5_goals', TEST_DATE
        )
        legs = result['accumulators'][0]['matches']
        self.assertEqual(legs[1]['teams'], ['Liverpool', 'Manchester United'])

    def test_goalscorer_predictions_stay_free_text(self):
        result = parse_generic_tips_page(
            load_fixture('anytime_goalscorer.html'), 'anytime_goalscorer', TEST_DATE
        )
        legs = result['accumulators'][0]['matches']
        self.assertEqual(legs[0]['prediction'], 'Mohamed Salah to score anytime')
        self.assertEqual(legs[1]['prediction'], 'Bruno Fernandes to score anytime')
        for leg in legs:
            self.assertEqual(set(leg), set(LEG_KEYS))


class ErrorEnvelopeTests(OfflineGuardMixin, SimpleTestCase):
    """A card-free page must produce a small, documented error envelope."""

    def test_bet_of_the_day_error_envelope(self):
        self.assertEqual(
            parse_bet_of_the_day_page(load_fixture('empty_page.html'), TEST_DATE),
            {'error': 'No tip cards found', 'matches': [], 'count': 0},
        )

    def test_accumulator_error_envelope(self):
        self.assertEqual(
            parse_accumulator_page(load_fixture('empty_page.html'), TEST_DATE),
            {'error': 'No accumulator cards found', 'accumulators': [], 'count': 0},
        )

    def test_generic_error_envelope_for_every_tip_type(self):
        for tip_type in ('over_2.5_goals', 'btts', 'btts_and_win', 'anytime_goalscorer'):
            with self.subTest(tip_type=tip_type):
                self.assertEqual(
                    parse_generic_tips_page(
                        load_fixture('empty_page.html'), tip_type, TEST_DATE
                    ),
                    {'error': 'No tip cards found', 'accumulators': [], 'count': 0},
                )

    def test_error_envelopes_stay_minimal_and_leak_nothing(self):
        envelopes = [
            parse_bet_of_the_day_page(load_fixture('empty_page.html')),
            parse_accumulator_page(load_fixture('empty_page.html')),
            parse_generic_tips_page(load_fixture('empty_page.html'), 'btts'),
        ]
        for envelope in envelopes:
            with self.subTest(keys=sorted(envelope)):
                self.assertEqual(set(envelope), {'error', 'count'} | (
                    {'matches'} if 'matches' in envelope else {'accumulators'}
                ))
                for absent in ('date', 'source', 'source_url', 'scraped_at', 'tip_type'):
                    self.assertNotIn(absent, envelope)

    def test_an_error_envelope_is_never_given_a_date(self):
        envelope = parse_bet_of_the_day_page(load_fixture('empty_page.html'), TEST_DATE)
        self.assertNotIn('date', envelope)


class ReturnsParsingTests(OfflineGuardMixin, SimpleTestCase):
    """``parse_card`` derives odds only from the BetGrid values it can read."""

    def test_thousands_separator_is_stripped_from_returns(self):
        result = parse_bet_of_the_day_page(load_fixture('comma_returns.html'), TEST_DATE)
        match = result['matches'][0]
        self.assertEqual(match['stake'], 2000.0)
        self.assertEqual(match['returns'], 2856.9)
        self.assertEqual(match['odds'], 1.43)

    def test_currency_symbol_is_never_parsed_into_a_number(self):
        result = parse_bet_of_the_day_page(load_fixture('comma_returns.html'), TEST_DATE)
        self.assertIsInstance(result['matches'][0]['returns'], float)
        self.assertIsInstance(result['matches'][0]['stake'], float)

    def test_only_the_active_option_is_read_as_the_stake(self):
        result = parse_bet_of_the_day_page(load_fixture('comma_returns.html'), TEST_DATE)
        # The card also offers a 10 option; the active 2000 option must win.
        self.assertEqual(result['matches'][0]['stake'], 2000.0)
        self.assertEqual(result['matches'][0]['odds'], 1.43)


class MalformedCardTests(OfflineGuardMixin, SimpleTestCase):
    """One unreadable card is dropped whole; its siblings are untouched."""

    def test_unparseable_stake_discards_only_that_card(self):
        result = parse_bet_of_the_day_page(load_fixture('malformed_card.html'), TEST_DATE)
        self.assertEqual(result['total_cards'], 2)  # both cards were found
        self.assertEqual(result['count'], 1)  # only the healthy one produced a tip
        self.assertEqual(
            [match['tip_category'] for match in result['matches']],
            ['Working Card'],
        )
        self.assertEqual(result['matches'][0]['match_title'], 'Arsenal vs Chelsea')
        self.assertEqual(result['matches'][0]['odds'], 2.0)

    def test_unparseable_stake_card_is_absent_from_accumulator_output(self):
        result = parse_accumulator_page(load_fixture('malformed_card.html'), TEST_DATE)
        self.assertEqual(result['total_accumulators'], 1)
        self.assertEqual(result['accumulators'][0]['category'], 'Working Card')
        self.assertEqual(result['accumulators'][0]['matches_count'], 1)
        self.assertEqual(result['count'], 1)

    def test_generic_parser_drops_the_same_card(self):
        result = parse_generic_tips_page(
            load_fixture('malformed_card.html'), 'btts', TEST_DATE
        )
        self.assertEqual(result['total_accumulators'], 1)
        self.assertEqual(result['accumulators'][0]['category'], 'Working Card')

    def test_a_bad_card_never_appears_as_a_half_filled_record(self):
        result = parse_accumulator_page(load_fixture('malformed_card.html'), TEST_DATE)
        categories = [a['category'] for a in result['accumulators']]
        self.assertNotIn('Broken Card', categories)
        self.assertNotIn('', categories)


class MalformedLegTests(OfflineGuardMixin, SimpleTestCase):
    """Partially broken legs are kept, with empty fields, and still counted."""

    def setUp(self):
        super().setUp()
        self.result = parse_accumulator_page(
            load_fixture('malformed_leg.html'), TEST_DATE
        )
        self.accumulator = self.result['accumulators'][0]

    def test_all_three_legs_are_counted(self):
        self.assertEqual(self.accumulator['matches_count'], 3)
        self.assertEqual(self.result['count'], 3)
        self.assertEqual(len(self.accumulator['matches']), 3)

    def test_first_leg_is_complete(self):
        leg = self.accumulator['matches'][0]
        self.assertEqual(leg['time'], '19:45')
        self.assertEqual(leg['match_title'], 'Liverpool vs Manchester United')
        self.assertEqual(leg['teams'], ['Liverpool', 'Manchester United'])
        self.assertEqual(leg['prediction'], 'Liverpool to win')
        self.assertEqual(
            leg['tip_reason'],
            'Liverpool are unbeaten at home in their last eleven league games.',
        )
        self.assertEqual(leg['match_url'], '/tips/liverpool-vs-manchester-united/')

    def test_empty_leg_is_kept_with_empty_defaults(self):
        leg = self.accumulator['matches'][1]
        self.assertEqual(set(leg), set(LEG_KEYS))
        self.assertEqual(leg, {
            'date': TEST_DATE,
            'time': '',
            'match_title': '',
            'teams': [],
            'prediction': '',
            'opponent_text': '',
            'tip_reason': '',
            'match_url': '',
        })

    def test_leg_without_a_prediction_keeps_an_empty_match_title(self):
        leg = self.accumulator['matches'][2]
        self.assertEqual(leg['time'], '21:00')
        self.assertEqual(leg['teams'], ['Everton'])
        self.assertEqual(leg['opponent_text'], 'vs Everton')
        self.assertEqual(leg['prediction'], '')
        # Known gap: match_title falls back to the (empty) prediction even though
        # one team name is known. Documented in docs/DATA_CONTRACT.md.
        self.assertEqual(leg['match_title'], '')

    def test_card_level_odds_survive_broken_legs(self):
        self.assertEqual(self.accumulator['stake'], 10.0)
        self.assertEqual(self.accumulator['returns'], 30.0)
        self.assertEqual(self.accumulator['total_odds'], 3.0)

    def test_a_leg_that_raises_is_skipped_without_breaking_the_card(self):
        calls = {'count': 0}
        real_extract = utils.extract_team_from_style

        def flaky(style_str):
            calls['count'] += 1
            if calls['count'] == 1:
                raise ValueError('simulated malformed leg')
            return real_extract(style_str)

        with mock.patch.object(utils, 'extract_team_from_style', side_effect=flaky):
            with mock.patch.object(utils.logger, 'warning') as warning:
                result = parse_accumulator_page(
                    load_fixture('malformed_leg.html'), TEST_DATE
                )

        warning.assert_called_once()
        self.assertEqual(result['accumulators'][0]['matches_count'], 2)
        self.assertEqual(result['count'], 2)


class TeamNameExtractionTests(OfflineGuardMixin, SimpleTestCase):
    """``extract_team_from_style`` in isolation, including its known limits."""

    def test_a_literal_quot_entity_is_required_to_match(self):
        self.assertEqual(
            utils.extract_team_from_style(
                'background-image:url(&quot;/wp-content/themes/freesupertips'
                '/image/team/Getafe.png&quot;)'
            ),
            'Getafe',
        )

    def test_decoded_quotes_do_not_match(self):
        self.assertEqual(
            utils.extract_team_from_style(
                'background-image:url("/wp-content/themes/freesupertips'
                '/image/team/Getafe.png")'
            ),
            '',
        )

    def test_percent_encoded_spaces_become_spaces(self):
        self.assertEqual(
            utils.extract_team_from_style(
                'background-image:url(&quot;/x/team/Inter%20Milan.png&quot;)'
            ),
            'Inter Milan',
        )

    def test_only_png_and_jpg_suffixes_are_stripped(self):
        self.assertEqual(
            utils.extract_team_from_style(
                'background-image:url(&quot;/x/team/Getafe.svg&quot;)'
            ),
            'Getafe.svg',
        )

    def test_missing_or_unrelated_styles_return_an_empty_string(self):
        self.assertEqual(utils.extract_team_from_style(''), '')
        self.assertEqual(utils.extract_team_from_style('background-color:red;'), '')

    def test_ampersand_in_a_team_url_is_outside_the_regex(self):
        # The capture group is [^&]+, so a name containing "&" never matches.
        self.assertEqual(
            utils.extract_team_from_style(
                'background-image:url(&quot;/x/team/A&B.png&quot;)'
            ),
            '',
        )

    def test_a_pre_encoded_amp_quot_entity_does_not_match(self):
        # Only the literal six characters &quot; match; &amp;quot; does not.
        self.assertEqual(
            utils.extract_team_from_style(
                'background-image:url(&amp;quot;/x/team/Getafe.png&amp;quot;)'
            ),
            '',
        )

    def test_single_escaped_source_loses_the_team_name(self):
        result = parse_accumulator_page(
            load_fixture('single_escaped_team_style.html'), TEST_DATE
        )
        leg = result['accumulators'][0]['matches'][0]
        self.assertEqual(leg['teams'], ['Everton'])  # opponent text only
        self.assertEqual(leg['prediction'], 'Liverpool to win')
        self.assertEqual(leg['match_title'], 'Liverpool to win')  # prediction fallback

    def test_double_escaped_source_keeps_both_team_names(self):
        result = parse_bet_of_the_day_page(
            load_fixture('bet_of_the_day.html'), TEST_DATE
        )
        self.assertEqual(
            result['matches'][0]['teams'], ['Liverpool', 'Manchester United']
        )


class _FrozenDateTime:
    """Stand-in for the ``datetime`` class that pins the clock to one instant."""

    @staticmethod
    def now():
        return datetime(2031, 1, 1, 3, 4, 5)


class ParserDateInjectionTests(OfflineGuardMixin, SimpleTestCase):
    """The optional date injection must not change the legacy call path."""

    def test_injected_date_is_used_verbatim(self):
        cases = (
            ('bet_of_the_day.html', parse_bet_of_the_day_page, ()),
            ('daily_accumulator.html', parse_accumulator_page, ()),
            ('both_teams_to_score.html', parse_generic_tips_page, ('btts',)),
        )
        for fixture, parser, args in cases:
            with self.subTest(fixture=fixture):
                self.assertEqual(
                    parser(load_fixture(fixture), *args, TEST_DATE)['date'], TEST_DATE
                )

    def test_injected_date_is_independent_of_the_machine_clock(self):
        html = load_fixture('bet_of_the_day.html')
        baseline = parse_bet_of_the_day_page(html, TEST_DATE)

        with mock.patch.object(utils, 'datetime', _FrozenDateTime):
            frozen = parse_bet_of_the_day_page(html, TEST_DATE)

        self.assertEqual(frozen, baseline)
        self.assertEqual(frozen['date'], TEST_DATE)
        self.assertEqual(frozen['matches'][0]['date'], TEST_DATE)

    def test_only_the_date_changes_between_two_injected_dates(self):
        html = load_fixture('daily_accumulator.html')
        first = parse_accumulator_page(html, '2026-09-27')
        second = parse_accumulator_page(html, '2031-01-01')
        self.assertEqual(first['date'], '2026-09-27')
        self.assertEqual(second['date'], '2031-01-01')

        def without_dates(payload):
            """The date is stamped on the envelope AND on every leg."""
            payload.pop('date')
            for accumulator in payload['accumulators']:
                for leg in accumulator['matches']:
                    leg.pop('date')
            return payload

        self.assertEqual(without_dates(first), without_dates(second))

    def test_every_leg_date_follows_the_injected_date(self):
        result = parse_generic_tips_page(
            load_fixture('btts_and_win.html'), 'btts_and_win', '2031-01-01'
        )
        for accumulator in result['accumulators']:
            for leg in accumulator['matches']:
                self.assertEqual(leg['date'], '2031-01-01')

    def test_no_injected_date_falls_back_to_the_machine_clock(self):
        with mock.patch.object(utils, 'datetime', _FrozenDateTime):
            legacy = parse_bet_of_the_day_page(load_fixture('bet_of_the_day.html'))
        self.assertEqual(legacy['date'], '2031-01-01')

    def test_positional_and_keyword_injection_agree(self):
        html = load_fixture('bet_of_the_day.html')
        self.assertEqual(
            parse_bet_of_the_day_page(html, TEST_DATE),
            parse_bet_of_the_day_page(html, current_date=TEST_DATE),
        )

    def test_resolve_source_date_contract(self):
        self.assertEqual(utils.resolve_source_date('2026-09-27'), '2026-09-27')
        # Only None means "use the clock"; '' is a deliberate injection.
        self.assertEqual(utils.resolve_source_date(''), '')
        with mock.patch.object(utils, 'datetime', _FrozenDateTime):
            self.assertEqual(utils.resolve_source_date(), '2031-01-01')
            self.assertEqual(utils.resolve_source_date(None), '2031-01-01')


class LegacyConfigCallPathTests(OfflineGuardMixin, SimpleTestCase):
    """``SCRAPER_CONFIGS[key]['parser'](html)`` must keep working unchanged."""

    def test_the_source_keys_in_scope_are_exactly_six(self):
        self.assertEqual(set(utils.SCRAPER_CONFIGS), {
            'bet_of_the_day', 'daily_accumulator', 'over_25_goals',
            'both_teams_to_score', 'btts_and_win', 'anytime_goalscorer',
        })

    def test_every_source_key_has_a_fixture(self):
        self.assertEqual(set(FIXTURE_FOR_SOURCE), set(utils.SCRAPER_CONFIGS))

    def test_every_configured_parser_accepts_a_single_html_argument(self):
        for key, config in utils.SCRAPER_CONFIGS.items():
            with self.subTest(source=key):
                result = config['parser'](load_fixture(FIXTURE_FOR_SOURCE[key]))
                self.assertNotIn('error', result)
                self.assertEqual(result['source'], 'freesupertips')
                self.assertEqual(len(result['date']), 10)
                self.assertGreater(result['count'], 0)

    def test_envelope_shape_follows_the_configured_page_kind(self):
        for key, config in utils.SCRAPER_CONFIGS.items():
            with self.subTest(source=key):
                result = config['parser'](load_fixture(FIXTURE_FOR_SOURCE[key]))
                if config['is_accumulator']:
                    expected = GENERIC_ENVELOPE_KEYS if 'tip_type' in result \
                        else ACCUMULATOR_ENVELOPE_KEYS
                    self.assertEqual(set(result), set(expected))
                else:
                    self.assertEqual(set(result), set(BET_OF_THE_DAY_ENVELOPE_KEYS))

    def test_config_lambdas_deliberately_keep_a_single_argument_signature(self):
        """The generator lambdas must not start forwarding a date of their own."""
        html = load_fixture('over_25_goals.html')
        config = utils.SCRAPER_CONFIGS['over_25_goals']
        with self.assertRaises(TypeError):
            config['parser'](html, TEST_DATE)

    def test_the_underlying_generic_parser_still_accepts_an_injected_date(self):
        html = load_fixture('over_25_goals.html')
        self.assertEqual(
            parse_generic_tips_page(html, 'over_2.5_goals', TEST_DATE)['date'],
            TEST_DATE,
        )


class FixtureHygieneTests(OfflineGuardMixin, SimpleTestCase):
    """Fixtures must stay static, local, ASCII, labelled, and secret-free."""

    EXPECTED_FIXTURES = frozenset(FIXTURE_FOR_SOURCE.values()) | {
        'empty_page.html',
        'malformed_card.html',
        'malformed_leg.html',
        'single_escaped_team_style.html',
        'comma_returns.html',
        'README.md',
    }

    FORBIDDEN_SUBSTRINGS = (
        'scrape_url', 'secret', 'api_key', 'apikey', 'authorization',
        'password', 'cookie', 'access_token', 'bearer ',
    )

    def html_fixtures(self):
        return sorted(self.EXPECTED_FIXTURES - {'README.md'})

    def test_every_expected_fixture_exists(self):
        for name in sorted(self.EXPECTED_FIXTURES):
            with self.subTest(fixture=name):
                self.assertTrue((FIXTURES_DIR / name).is_file())

    def test_the_fixture_directory_has_no_unexpected_files(self):
        found = {path.name for path in FIXTURES_DIR.iterdir() if path.is_file()}
        self.assertEqual(found, set(self.EXPECTED_FIXTURES))

    def test_html_fixtures_stay_ascii_only(self):
        for name in self.html_fixtures():
            with self.subTest(fixture=name):
                try:
                    load_fixture(name).decode('ascii')
                except UnicodeDecodeError as exc:
                    self.fail(f'{name} must stay ASCII-only: {exc}')

    def test_html_fixtures_open_with_a_synthetic_provenance_comment(self):
        for name in self.html_fixtures():
            with self.subTest(fixture=name):
                head = load_fixture(name).decode('ascii').splitlines()[:3]
                self.assertIn('<!--', head[0])
                self.assertIn('SYNTHETIC PARSER FIXTURE', ' '.join(head))

    def test_html_fixtures_contain_no_external_urls(self):
        for name in self.html_fixtures():
            text = load_fixture(name).decode('ascii')
            with self.subTest(fixture=name):
                self.assertNotIn('http://', text)
                self.assertNotIn('https://', text)
                self.assertNotIn('www.', text)

    def test_html_fixtures_contain_no_secrets_or_personal_data(self):
        for name in self.html_fixtures():
            text = load_fixture(name).decode('ascii').lower()
            for needle in self.FORBIDDEN_SUBSTRINGS:
                with self.subTest(fixture=name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_the_test_modules_do_not_import_a_live_http_client(self):
        module_dir = Path(__file__).resolve().parent
        # The needles are assembled from fragments so this test's own source
        # cannot accidentally match the very strings it is looking for.
        needles = (
            'import ' + 'requests',
            'import ' + 'urllib',
            'ur' + 'lopen',
            'cloudscraper' + '.get',
        )
        for module in ('tests_parser_contract.py', 'tests_offline_guard.py'):
            with self.subTest(module=module):
                source = (module_dir / module).read_text(encoding='utf-8')
                for needle in needles:
                    with self.subTest(module=module, needle=needle):
                        self.assertTrue(needle not in source)
