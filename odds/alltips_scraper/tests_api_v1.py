"""Pure mapping tests for the versioned tips serializers (``serializers_v1``).

Why this module exists
----------------------
``/api/v1/tips/`` publishes a different envelope from the frozen legacy payloads
(``docs/DATA_CONTRACT.md`` §3): the source display text is renamed, the provenance
block carries ``label`` plus the payload's own ``date_text``, each tip is labelled
with a unit, every mapped leg carries the contract's fixed temporal markers, and
the volatile fetch metadata is dropped.
Those renames and fixed fields are the contract a client will be written against,
so they are pinned here
before any view exists — the serializers are the only thing that decides the
public shape, and a rename is exactly the kind of change that is invisible until
it breaks a client.

Ground rules
------------
* **No network, no database.** Every test is a pure function call on a
  ``SimpleTestCase``; nothing here can fetch, and nothing here writes.
* **No snapshot store, no view, no URL routing.** Those are the read model and
  the endpoint, and they are not in this module's scope.
* **No scraper import.** The payloads below are hand-written mirrors of the
  frozen parser envelopes, in the style of ``tests.py``, so this module imports
  neither the scraper module nor a fixture file. That is deliberate: the public
  serializers must be usable without the scraper's import-time configuration. The
  registry is compared against the legacy configuration table in the
  Django-configured layer.
* **No clock.** Every timestamp comes from a fixed constant, so no expectation
  here depends on the machine's date, timezone, or locale.
* **No formatter borrowing.** The read model stores and normalises timezone-aware
  UTC ``datetime`` objects and does not import the serializers; the only wire
  format ``YYYY-MM-DDTHH:MM:SSZ`` is produced by ``serializers_v1.format_utc_z()``,
  which the timestamp tests below pin.

Commit 1 scope
--------------
This module currently covers ``serializers_v1`` only. Response status codes,
query validation, snapshot storage, and the date-equality filter belong to later
commits and are asserted there, not here.
"""

import ast
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from django.test import SimpleTestCase

from .serializers_v1 import (
    API_VERSION,
    CARD_UNIT_TIP_KEYS,
    ERROR_CODES,
    ERROR_INVALID_DATE,
    ERROR_MISSING_TIMEZONE,
    ERROR_MISSING_TIP_TYPE,
    ERROR_SOURCE_UNAVAILABLE,
    ERROR_TIMEZONE_REQUIRES_DATE,
    ERROR_UNKNOWN_TIP_TYPE,
    KICKOFF_AT,
    KICKOFF_MARKER_KEYS,
    KICKOFF_MARKERS,
    KICKOFF_TIME_VERIFIED,
    LEG_KEYS,
    MATCH_UNIT_TIP_KEYS,
    OPTIONAL_ENVELOPE_KEYS,
    SOURCE_DATE_KEY,
    SOURCE_TEXT_KEYS,
    SUCCESS_ENVELOPE_KEYS,
    SUPPORTED_TIP_TYPES,
    TIP_TYPE_UNITS,
    UNIT_CARD,
    UNIT_MATCH,
    UnknownErrorCode,
    UnknownTipType,
    api_error,
    format_utc_z,
    is_supported_tip_type,
    serialize_tips,
    unit_for,
)

SERIALIZERS_V1_PATH = Path(__file__).resolve().parent / 'serializers_v1.py'

# The serializers may import these standard-library roots and nothing else.
ALLOWED_IMPORT_ROOTS = frozenset({'collections', 'copy', 'datetime', 'types'})

# Roots that would couple the public serializers to a framework, a scraper, an
# HTTP client, or the environment.
FORBIDDEN_IMPORT_ROOTS = frozenset({
    'alltips_scraper', 'aiohttp', 'cloudscraper', 'decouple', 'django', 'http',
    'os', 'requests', 'socket', 'sys', 'urllib',
})

# Tokens that must not appear anywhere in the serializer source, comments
# included: they are the names of the coupling this module forbids.
FORBIDDEN_SOURCE_TOKENS = (
    'django', 'cloudscraper', 'requests', 'aiohttp', 'decouple', 'scrape_url',
    'scrape_one', 'scrape_all', '_fetch_one', 'getenv', 'os.environ', 'urllib',
    'socket', 'json_response',
)

# Keys the legacy payloads and the fetch layer use, which the versioned API
# replaces or drops, plus entitlement flags that must never appear on a tip.
FORBIDDEN_V1_KEYS = frozenset({
    'date', 'time', 'tip_category', 'matches', 'accumulators', 'matches_count',
    'total_tips', 'total_cards', 'total_accumulators', 'scraped_at',
    'source_url', 'cached', 'error',
    'free', 'premium', 'locked', 'preview', 'is_free', 'is_premium',
    'entitlement', 'access',
})

# ---------------------------------------------------------------------------
# Fixed inputs. Nothing below reads a clock or a fixture.
# ---------------------------------------------------------------------------

SOURCE_DATE_TEXT = '2026-09-27'
FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3, tzinfo=timezone.utc)
FETCHED_AT_Z = '2026-09-28T17:12:03Z'

NAIVE_FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3)

# The four generic sources and the ``tip_type`` value their cards carry.
GENERIC_TIP_TYPES = {
    'over_25_goals': 'over_2.5_goals',
    'both_teams_to_score': 'btts',
    'btts_and_win': 'btts_and_win',
    'anytime_goalscorer': 'anytime_goalscorer',
}


def all_keys(value) -> set:
    """Return every dict key in a nested structure, lists included."""
    keys = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys |= all_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            keys |= all_keys(nested)
    return keys


class KickoffMarkerAssertions:
    """Shared pins for the contract's fixed temporal markers.

    The marker pair has to be identical on a ``bet_of_the_day`` tip, on a card's
    nested leg, and on a generic leg, so the assertions live in one place: a shape
    that drifts per unit is not a contract.
    """

    def assertKickoffMarkers(self, entry, message=None):
        """Assert one mapped tip or leg carries the documented marker pair."""
        self.assertEqual(
            KICKOFF_MARKER_KEYS & set(entry),
            set(KICKOFF_MARKER_KEYS),
            message,
        )
        self.assertIsNone(entry[KICKOFF_AT], message)
        self.assertIs(entry[KICKOFF_TIME_VERIFIED], False, message)

    def assertEveryLegHasKickoffMarkers(self, envelope):
        """Assert every nested leg of every card in ``envelope`` is marked."""
        for card in envelope['tips']:
            for nested_leg in card['legs']:
                with self.subTest(match_title=nested_leg['match_title']):
                    self.assertKickoffMarkers(nested_leg)


# Spellings a legacy payload could carry that describe a kickoff as a real value.
# The markers must never be filled from any of them: these serializers do not read
# a kickoff, they only publish its "unavailable" marker values.
UNPARSED_KICKOFF_FIELDS = (
    'kickoff', 'kickoff_at', 'kickoff_time', 'kickoff_time_verified',
    'start_time', 'ko_time',
)

# A plausible invented kickoff, used to prove the markers are not derived from the
# source display text or from any kickoff-shaped field.
INVENTED_KICKOFF = '2026-09-27T19:45:00Z'


# ---------------------------------------------------------------------------
# Payload mirrors of the frozen parser envelopes (docs/DATA_CONTRACT.md §3-4).
# ---------------------------------------------------------------------------

def leg(match_title, teams, time_text='19:45') -> dict:
    """One ``parse_leg`` selection: exactly the eight frozen leg keys."""
    return {
        'date': SOURCE_DATE_TEXT,
        'time': time_text,
        'match_title': match_title,
        'teams': teams,
        'prediction': f'{teams[0]} to win',
        'opponent_text': f'vs {teams[1]}',
        'tip_reason': f'Reason for {match_title}',
        'match_url': f'/betting-tips/{match_title.lower().replace(" ", "-")}/',
    }


def botd_match(match_title, teams, **card_values) -> dict:
    """One ``bet_of_the_day`` selection: a leg plus the card-level values."""
    selection = leg(match_title, teams)
    selection.update(card_values)
    return selection


BET_OF_THE_DAY_PAYLOAD = {
    'date': SOURCE_DATE_TEXT,
    'total_tips': 2,
    'total_cards': 3,
    'matches': [
        botd_match(
            'Arsenal vs Chelsea', ['Arsenal', 'Chelsea'],
            tip_category='Tip 1', stake=10.0, returns=18.0, odds=1.8,
        ),
        botd_match(
            'Bayern vs Dortmund', ['Bayern', 'Dortmund'], time_text='20:00',
            tip_category='Tip 2', stake=10.0, returns=25.0, odds=2.5,
        ),
    ],
    'count': 2,
    'source': 'freesupertips',
}

DAILY_ACCUMULATOR_PAYLOAD = {
    'date': SOURCE_DATE_TEXT,
    'total_accumulators': 2,
    'accumulators': [
        {
            'category': 'Daily Accumulator',
            'stake': 10.0,
            'returns': 28.56,
            'total_odds': 2.86,
            'matches': [
                leg('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'], '19:45'),
                leg('Bayern vs Dortmund', ['Bayern', 'Dortmund'], '20:00'),
            ],
            'matches_count': 2,
        },
        {
            'category': 'Both Teams to Score Accumulator',
            'stake': None,
            'returns': None,
            'total_odds': None,
            'matches': [leg('Inter vs Milan', ['Inter', 'Milan'], '21:00')],
            'matches_count': 1,
        },
    ],
    'count': 3,
    'source': 'freesupertips',
}


def generic_payload(tip_type: str) -> dict:
    """A mirror of one generic tips page envelope for ``tip_type``."""
    return {
        'date': SOURCE_DATE_TEXT,
        'tip_type': tip_type,
        'total_accumulators': 2,
        'accumulators': [
            {
                'category': f'{tip_type} Tips',
                'tip_type': tip_type,
                'stake': 10.0,
                'returns': 46.0,
                'total_odds': 4.6,
                'matches': [
                    leg('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'], '19:45'),
                    leg('Inter vs Milan', ['Inter', 'Milan'], '21:00'),
                ],
                'matches_count': 2,
            },
            {
                'category': f'{tip_type} Accumulator',
                'tip_type': tip_type,
                'stake': 10.0,
                'returns': 33.8,
                'total_odds': 3.38,
                'matches': [leg('Bayern vs Dortmund', ['Bayern', 'Dortmund'], '20:00')],
                'matches_count': 1,
            },
        ],
        'count': 3,
        'source': 'freesupertips',
    }


PAYLOAD_FOR_TYPE = {
    'bet_of_the_day': BET_OF_THE_DAY_PAYLOAD,
    'daily_accumulator': DAILY_ACCUMULATOR_PAYLOAD,
    **{key: generic_payload(value) for key, value in GENERIC_TIP_TYPES.items()},
}

# The three "no cards" error envelopes the parsers can return.
NO_TIP_CARDS_PAYLOAD = {'error': 'No tip cards found', 'matches': [], 'count': 0}
NO_ACCUMULATOR_CARDS_PAYLOAD = {
    'error': 'No accumulator cards found', 'accumulators': [], 'count': 0,
}
GENERIC_NO_CARDS_PAYLOAD = {
    'error': 'No tip cards found', 'accumulators': [], 'count': 0,
}

EMPTY_PAYLOAD_FOR_TYPE = {
    'bet_of_the_day': NO_TIP_CARDS_PAYLOAD,
    'daily_accumulator': NO_ACCUMULATOR_CARDS_PAYLOAD,
    **{key: GENERIC_NO_CARDS_PAYLOAD for key in GENERIC_TIP_TYPES},
}

LEGACY_ERROR_TEXTS = ('No tip cards found', 'No accumulator cards found')


class TipTypeRegistryTests(SimpleTestCase):
    """The published registry is the contract's front door and is immutable."""

    EXPECTED_UNITS = {
        'bet_of_the_day': 'match',
        'daily_accumulator': 'card',
        'over_25_goals': 'card',
        'both_teams_to_score': 'card',
        'btts_and_win': 'card',
        'anytime_goalscorer': 'card',
    }

    def test_registry_names_exactly_the_six_versioned_sources(self):
        self.assertEqual(set(TIP_TYPE_UNITS), set(self.EXPECTED_UNITS))

    def test_registry_publishes_one_unit_per_source_kind(self):
        self.assertEqual(dict(TIP_TYPE_UNITS), self.EXPECTED_UNITS)

    def test_supported_tip_types_mirrors_the_registry(self):
        self.assertEqual(SUPPORTED_TIP_TYPES, frozenset(self.EXPECTED_UNITS))
        self.assertEqual(SUPPORTED_TIP_TYPES, frozenset(TIP_TYPE_UNITS))

    def test_registry_cannot_be_mutated_at_runtime(self):
        with self.assertRaises(TypeError):
            TIP_TYPE_UNITS['new_source'] = UNIT_MATCH

    def test_source_text_rename_is_the_documented_pair(self):
        self.assertEqual(
            dict(SOURCE_TEXT_KEYS),
            {'date': 'source_date_text', 'time': 'source_time_text'},
        )

    def test_unit_for_returns_the_published_unit(self):
        for tip_type, unit in self.EXPECTED_UNITS.items():
            with self.subTest(tip_type=tip_type):
                self.assertEqual(unit_for(tip_type), unit)

    def test_unit_for_rejects_an_unknown_key(self):
        for candidate in ('not-a-source', '', 'Bet_of_the_day', None, 7):
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnknownTipType):
                    unit_for(candidate)

    def test_is_supported_tip_type_accepts_only_registry_keys(self):
        for tip_type in self.EXPECTED_UNITS:
            with self.subTest(tip_type=tip_type):
                self.assertTrue(is_supported_tip_type(tip_type))
        for candidate in ('not-a-source', '', None, 7, ['bet_of_the_day']):
            with self.subTest(candidate=candidate):
                self.assertFalse(is_supported_tip_type(candidate))


class UtcTimestampFormattingTests(SimpleTestCase):
    """One spelling for an authoritative UTC timestamp, or a refusal."""

    def test_exact_iso_8601_utc_shape(self):
        self.assertEqual(format_utc_z(FETCHED_AT), FETCHED_AT_Z)
        self.assertEqual(len(FETCHED_AT_Z), 20)
        self.assertTrue(FETCHED_AT_Z.endswith('Z'))

    def test_fractional_seconds_are_truncated_not_rounded(self):
        value = FETCHED_AT.replace(microsecond=987654)
        self.assertEqual(format_utc_z(value), FETCHED_AT_Z)

    def test_a_numeric_zero_offset_is_never_emitted(self):
        rendered = format_utc_z(FETCHED_AT)
        for forbidden in ('+00:00', '+0000', 'Z+', 'UTC'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_offset_aware_values_are_normalised_to_utc(self):
        ahead = FETCHED_AT.astimezone(timezone(timedelta(hours=3)))
        behind = FETCHED_AT.astimezone(timezone(timedelta(hours=-5)))
        self.assertEqual(format_utc_z(ahead), FETCHED_AT_Z)
        self.assertEqual(format_utc_z(behind), FETCHED_AT_Z)

    def test_a_zero_offset_timezone_object_also_renders_with_z(self):
        explicit = FETCHED_AT.replace(tzinfo=timezone(timedelta(0)))
        self.assertEqual(format_utc_z(explicit), FETCHED_AT_Z)

    def test_naive_values_are_rejected_rather_than_assumed_utc(self):
        with self.assertRaisesRegex(ValueError, 'timezone-aware'):
            format_utc_z(NAIVE_FETCHED_AT)

    def test_non_datetime_values_are_rejected(self):
        for candidate in ('2026-09-28T17:12:03Z', date(2026, 9, 28), 0, None):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TypeError):
                    format_utc_z(candidate)


class BetOfTheDayMatchUnitMappingTests(KickoffMarkerAssertions, SimpleTestCase):
    """Tip-card selections publish as ``match`` tips with renamed source text."""

    def setUp(self):
        self.envelope = serialize_tips('bet_of_the_day', BET_OF_THE_DAY_PAYLOAD)

    def test_envelope_has_exactly_the_success_keys(self):
        self.assertEqual(set(self.envelope), set(SUCCESS_ENVELOPE_KEYS))
        self.assertEqual(set(self.envelope) & OPTIONAL_ENVELOPE_KEYS, set())

    def test_envelope_identifies_version_type_and_unit(self):
        self.assertEqual(self.envelope['api_version'], API_VERSION)
        self.assertEqual(self.envelope['type'], 'bet_of_the_day')
        self.assertEqual(self.envelope['unit'], UNIT_MATCH)

    def test_count_is_the_number_of_selections_and_legs_count_is_zero(self):
        self.assertEqual(self.envelope['count'], 2)
        self.assertEqual(len(self.envelope['tips']), 2)
        self.assertEqual(self.envelope['legs_count'], 0)

    def test_every_tip_has_the_published_match_unit_keys(self):
        for tip in self.envelope['tips']:
            with self.subTest(match_title=tip['match_title']):
                self.assertEqual(set(tip), set(MATCH_UNIT_TIP_KEYS))
                self.assertKickoffMarkers(tip)

    def test_the_first_tip_is_mapped_key_for_key(self):
        self.assertEqual(self.envelope['tips'][0], {
            'match_title': 'Arsenal vs Chelsea',
            'teams': ['Arsenal', 'Chelsea'],
            'prediction': 'Arsenal to win',
            'opponent_text': 'vs Chelsea',
            'tip_reason': 'Reason for Arsenal vs Chelsea',
            'match_url': '/betting-tips/arsenal-vs-chelsea/',
            'source_date_text': SOURCE_DATE_TEXT,
            'source_time_text': '19:45',
            'category': 'Tip 1',
            'stake': 10.0,
            'returns': 18.0,
            'odds': 1.8,
            'kickoff_at': None,
            'kickoff_time_verified': False,
        })

    def test_source_display_text_is_renamed_and_not_converted(self):
        for tip in self.envelope['tips']:
            with self.subTest(match_title=tip['match_title']):
                self.assertEqual(tip['source_date_text'], SOURCE_DATE_TEXT)
                self.assertEqual(tip['source_time_text'], '19:45')
                self.assertNotIn('date', tip)
                self.assertNotIn('time', tip)

    def test_every_match_unit_tip_pins_the_temporal_markers(self):
        for tip in self.envelope['tips']:
            with self.subTest(match_title=tip['match_title']):
                self.assertIsNone(tip[KICKOFF_AT])
                self.assertIs(tip[KICKOFF_TIME_VERIFIED], False)

    def test_card_level_values_are_lifted_onto_the_tip(self):
        tip = self.envelope['tips'][0]
        self.assertEqual(tip['category'], 'Tip 1')
        self.assertEqual(tip['stake'], 10.0)
        self.assertEqual(tip['returns'], 18.0)
        self.assertEqual(tip['odds'], 1.8)
        for legacy_name in ('tip_category', 'total_odds'):
            with self.subTest(legacy_name=legacy_name):
                self.assertNotIn(legacy_name, tip)

    def test_tip_order_follows_the_payload(self):
        self.assertEqual(
            [tip['match_title'] for tip in self.envelope['tips']],
            ['Arsenal vs Chelsea', 'Bayern vs Dortmund'],
        )

    def test_source_display_time_may_be_empty_and_stays_empty(self):
        payload = dict(BET_OF_THE_DAY_PAYLOAD, matches=[
            botd_match('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'],
                       time='', tip_category='Tip 1', stake=10.0, returns=18.0,
                       odds=1.8),
        ])
        envelope = serialize_tips('bet_of_the_day', payload)
        self.assertEqual(envelope['tips'][0]['source_time_text'], '')

    def test_no_legacy_or_volatile_keys_leak_into_the_envelope(self):
        leaked = FORBIDDEN_V1_KEYS & all_keys(self.envelope)
        self.assertEqual(leaked, set())


class AccumulatorCardMappingTests(KickoffMarkerAssertions, SimpleTestCase):
    """Accumulator cards publish as ``card`` tips with nested legs."""

    def setUp(self):
        self.envelope = serialize_tips(
            'daily_accumulator', DAILY_ACCUMULATOR_PAYLOAD)

    def test_envelope_identifies_version_type_and_unit(self):
        self.assertEqual(self.envelope['api_version'], API_VERSION)
        self.assertEqual(self.envelope['type'], 'daily_accumulator')
        self.assertEqual(self.envelope['unit'], UNIT_CARD)

    def test_count_is_the_number_of_cards(self):
        self.assertEqual(self.envelope['count'], 2)
        self.assertEqual(len(self.envelope['tips']), 2)

    def test_legs_count_is_the_sum_of_every_card_s_legs(self):
        self.assertEqual(self.envelope['legs_count'], 3)
        self.assertEqual(
            [card['legs_count'] for card in self.envelope['tips']], [2, 1])

    def test_every_card_has_the_published_card_unit_keys(self):
        for card in self.envelope['tips']:
            with self.subTest(category=card['category']):
                self.assertEqual(set(card), set(CARD_UNIT_TIP_KEYS))

    def test_the_daily_accumulator_card_has_no_tip_type(self):
        for card in self.envelope['tips']:
            with self.subTest(category=card['category']):
                self.assertNotIn('tip_type', card)

    def test_the_first_card_keeps_its_card_level_values(self):
        card = self.envelope['tips'][0]
        self.assertEqual(card['category'], 'Daily Accumulator')
        self.assertEqual(card['stake'], 10.0)
        self.assertEqual(card['returns'], 28.56)
        self.assertEqual(card['total_odds'], 2.86)

    def test_a_card_without_odds_keeps_its_empty_values(self):
        card = self.envelope['tips'][1]
        self.assertEqual(card['category'], 'Both Teams to Score Accumulator')
        self.assertIsNone(card['stake'])
        self.assertIsNone(card['returns'])
        self.assertIsNone(card['total_odds'])

    def test_every_leg_has_the_published_leg_keys(self):
        for card in self.envelope['tips']:
            for nested_leg in card['legs']:
                with self.subTest(match_title=nested_leg['match_title']):
                    self.assertEqual(set(nested_leg), set(LEG_KEYS))
                    self.assertKickoffMarkers(nested_leg)

    def test_the_first_leg_is_mapped_key_for_key(self):
        self.assertEqual(self.envelope['tips'][0]['legs'][0], {
            'match_title': 'Arsenal vs Chelsea',
            'teams': ['Arsenal', 'Chelsea'],
            'prediction': 'Arsenal to win',
            'opponent_text': 'vs Chelsea',
            'tip_reason': 'Reason for Arsenal vs Chelsea',
            'match_url': '/betting-tips/arsenal-vs-chelsea/',
            'source_date_text': SOURCE_DATE_TEXT,
            'source_time_text': '19:45',
            'kickoff_at': None,
            'kickoff_time_verified': False,
        })

    def test_every_nested_leg_pins_the_temporal_markers(self):
        for card in self.envelope['tips']:
            for nested_leg in card['legs']:
                with self.subTest(match_title=nested_leg['match_title']):
                    self.assertIsNone(nested_leg[KICKOFF_AT])
                    self.assertIs(nested_leg[KICKOFF_TIME_VERIFIED], False)

    def test_legs_carry_no_card_level_odds_or_metadata(self):
        card_level_names = ('stake', 'returns', 'odds', 'total_odds',
                            'tip_category', 'matches_count', 'category')
        for card in self.envelope['tips']:
            for nested_leg in card['legs']:
                for name in card_level_names:
                    with self.subTest(match_title=nested_leg['match_title'],
                                      name=name):
                        self.assertNotIn(name, nested_leg)

    def test_no_legacy_or_volatile_keys_leak_into_the_envelope(self):
        self.assertEqual(FORBIDDEN_V1_KEYS & all_keys(self.envelope), set())


class GenericCardMappingTests(KickoffMarkerAssertions, SimpleTestCase):
    """Every generic source publishes the same card shape with its tip type."""

    def test_each_generic_source_keeps_its_card_tip_type(self):
        for key, tip_type in GENERIC_TIP_TYPES.items():
            with self.subTest(tip_type=key):
                envelope = serialize_tips(key, generic_payload(tip_type))
                self.assertEqual(envelope['unit'], UNIT_CARD)
                self.assertEqual(envelope['count'], 2)
                self.assertEqual(envelope['legs_count'], 3)
                self.assertEqual(
                    [card['tip_type'] for card in envelope['tips']],
                    [tip_type, tip_type],
                )

    def test_a_generic_card_adds_only_the_tip_type_key(self):
        for key in GENERIC_TIP_TYPES:
            with self.subTest(tip_type=key):
                envelope = serialize_tips(key, PAYLOAD_FOR_TYPE[key])
                self.assertEqual(
                    set(envelope['tips'][0]),
                    set(CARD_UNIT_TIP_KEYS) | {'tip_type'},
                )

    def test_the_payload_tip_type_is_not_echoed_as_an_envelope_key(self):
        envelope = serialize_tips(
            'over_25_goals', generic_payload('over_2.5_goals'))
        self.assertNotIn('tip_type', envelope)
        self.assertEqual(envelope['type'], 'over_25_goals')

    def test_generic_legs_keep_the_published_leg_keys(self):
        for key in GENERIC_TIP_TYPES:
            with self.subTest(tip_type=key):
                envelope = serialize_tips(key, PAYLOAD_FOR_TYPE[key])
                for card in envelope['tips']:
                    for nested_leg in card['legs']:
                        self.assertEqual(set(nested_leg), set(LEG_KEYS))
                        self.assertKickoffMarkers(nested_leg)

    def test_generic_legs_pin_the_temporal_markers(self):
        for key in GENERIC_TIP_TYPES:
            with self.subTest(tip_type=key):
                self.assertEveryLegHasKickoffMarkers(
                    serialize_tips(key, PAYLOAD_FOR_TYPE[key]))

    def test_no_legacy_or_volatile_keys_leak_for_any_generic_source(self):
        for key in GENERIC_TIP_TYPES:
            with self.subTest(tip_type=key):
                envelope = serialize_tips(key, PAYLOAD_FOR_TYPE[key])
                self.assertEqual(FORBIDDEN_V1_KEYS & all_keys(envelope), set())


class TemporalMarkerContractTests(KickoffMarkerAssertions, SimpleTestCase):
    """The temporal fields are fixed structural markers, never a derived kickoff."""

    def test_the_marker_vocabulary_is_the_documented_pair(self):
        self.assertEqual(KICKOFF_AT, 'kickoff_at')
        self.assertEqual(KICKOFF_TIME_VERIFIED, 'kickoff_time_verified')
        self.assertEqual(dict(KICKOFF_MARKERS), {
            'kickoff_at': None,
            'kickoff_time_verified': False,
        })
        self.assertEqual(
            KICKOFF_MARKER_KEYS, {'kickoff_at', 'kickoff_time_verified'})

    def test_both_published_key_sets_include_the_markers(self):
        self.assertEqual(KICKOFF_MARKER_KEYS - LEG_KEYS, set())
        self.assertEqual(KICKOFF_MARKER_KEYS - MATCH_UNIT_TIP_KEYS, set())

    def test_the_card_unit_key_set_has_no_temporal_markers(self):
        self.assertEqual(set(CARD_UNIT_TIP_KEYS) & KICKOFF_MARKER_KEYS, set())

    def test_the_markers_are_the_single_declared_unavailable_values(self):
        envelope = serialize_tips('bet_of_the_day', BET_OF_THE_DAY_PAYLOAD)
        for tip in envelope['tips']:
            with self.subTest(match_title=tip['match_title']):
                self.assertIs(tip[KICKOFF_AT], KICKOFF_MARKERS[KICKOFF_AT])
                self.assertIs(
                    tip[KICKOFF_TIME_VERIFIED],
                    KICKOFF_MARKERS[KICKOFF_TIME_VERIFIED],
                )

    def test_a_match_unit_tip_never_reads_a_kickoff_from_the_payload(self):
        selection = botd_match(
            'Arsenal vs Chelsea', ['Arsenal', 'Chelsea'],
            tip_category='Tip 1', stake=10.0, returns=18.0, odds=1.8,
        )
        selection.update({field: INVENTED_KICKOFF
                          for field in UNPARSED_KICKOFF_FIELDS})
        envelope = serialize_tips(
            'bet_of_the_day', dict(BET_OF_THE_DAY_PAYLOAD, matches=[selection]))
        tip = envelope['tips'][0]
        self.assertEqual(set(tip), set(MATCH_UNIT_TIP_KEYS))
        self.assertKickoffMarkers(tip)
        self.assertNotIn(INVENTED_KICKOFF, json.dumps(tip))

    def test_a_nested_leg_never_reads_a_kickoff_from_the_payload(self):
        selection = leg('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'], '19:45')
        selection.update({field: INVENTED_KICKOFF
                          for field in UNPARSED_KICKOFF_FIELDS})
        card = {
            'category': 'Daily Accumulator', 'stake': 10.0, 'returns': 28.56,
            'total_odds': 2.86, 'matches': [selection], 'matches_count': 1,
        }
        envelope = serialize_tips(
            'daily_accumulator',
            dict(DAILY_ACCUMULATOR_PAYLOAD, accumulators=[card]),
        )
        mapped = envelope['tips'][0]['legs'][0]
        self.assertEqual(set(mapped), set(LEG_KEYS))
        self.assertKickoffMarkers(mapped)
        self.assertNotIn(INVENTED_KICKOFF, json.dumps(mapped))

    def test_every_source_kind_marks_the_markers_as_unavailable(self):
        for tip_type in SUPPORTED_TIP_TYPES:
            with self.subTest(tip_type=tip_type):
                envelope = serialize_tips(tip_type, PAYLOAD_FOR_TYPE[tip_type])
                entries = [
                    entry
                    for card in envelope['tips']
                    for entry in ([card] if 'legs' not in card else card['legs'])
                ]
                self.assertTrue(entries)
                for entry in entries:
                    self.assertKickoffMarkers(entry)


class EmptyPayloadMappingTests(SimpleTestCase):
    """No tips is an empty result, not an error, and not a legacy-shaped body."""

    def test_bet_of_the_day_no_cards_maps_to_an_empty_envelope(self):
        envelope = serialize_tips('bet_of_the_day', NO_TIP_CARDS_PAYLOAD)
        self.assertEqual(envelope['tips'], [])
        self.assertEqual(envelope['count'], 0)
        self.assertEqual(envelope['legs_count'], 0)
        self.assertNotIn('error', envelope)
        self.assertEqual(envelope['unit'], UNIT_MATCH)

    def test_every_source_kind_maps_a_no_cards_payload_to_an_empty_envelope(self):
        for tip_type, payload in EMPTY_PAYLOAD_FOR_TYPE.items():
            with self.subTest(tip_type=tip_type):
                envelope = serialize_tips(tip_type, payload)
                self.assertEqual(envelope['api_version'], API_VERSION)
                self.assertEqual(envelope['tips'], [])
                self.assertEqual(envelope['count'], 0)
                self.assertEqual(envelope['legs_count'], 0)
                self.assertNotIn('error', envelope)
                self.assertEqual(set(envelope), set(SUCCESS_ENVELOPE_KEYS))

    def test_the_legacy_error_message_is_not_carried_into_the_envelope(self):
        for tip_type, payload in EMPTY_PAYLOAD_FOR_TYPE.items():
            with self.subTest(tip_type=tip_type):
                body = json.dumps(serialize_tips(tip_type, payload))
                for text in LEGACY_ERROR_TEXTS:
                    self.assertNotIn(text, body)

    def test_an_empty_payload_reports_no_provenance_it_never_stated(self):
        envelope = serialize_tips('bet_of_the_day', NO_TIP_CARDS_PAYLOAD)
        self.assertEqual(envelope['source'], {'label': None, 'date_text': None})

    def test_a_payload_without_the_expected_collection_is_empty_not_an_error(self):
        for tip_type in SUPPORTED_TIP_TYPES:
            with self.subTest(tip_type=tip_type):
                envelope = serialize_tips(tip_type, {'source': 'freesupertips'})
                self.assertEqual(envelope['count'], 0)
                self.assertEqual(envelope['legs_count'], 0)
                self.assertNotIn('error', envelope)

    def test_an_empty_card_still_counts_as_a_card(self):
        payload = {
            'matches': [],
            'accumulators': [
                {'category': 'Empty Accumulator', 'stake': None, 'returns': None,
                 'total_odds': None, 'matches': [], 'matches_count': 0},
            ],
            'source': 'freesupertips',
        }
        envelope = serialize_tips('daily_accumulator', payload)
        self.assertEqual(envelope['count'], 1)
        self.assertEqual(envelope['legs_count'], 0)
        self.assertEqual(envelope['tips'][0]['legs'], [])


class SourceBlockTests(SimpleTestCase):
    """Provenance comes from the payload, and the fetch stamp is UTC or absent."""

    def test_source_label_comes_from_the_payload(self):
        envelope = serialize_tips('bet_of_the_day', BET_OF_THE_DAY_PAYLOAD)
        self.assertEqual(envelope['source']['label'], 'freesupertips')

    def test_the_source_block_uses_label_and_never_name(self):
        envelope = serialize_tips(
            'bet_of_the_day', BET_OF_THE_DAY_PAYLOAD, fetched_at=FETCHED_AT)
        self.assertIn('label', envelope['source'])
        self.assertNotIn('name', envelope['source'])

    def test_a_source_value_that_is_not_text_yields_a_null_label(self):
        for value in ('', None, 7, ['freesupertips'], {'name': 'freesupertips'}):
            with self.subTest(source=value):
                payload = dict(BET_OF_THE_DAY_PAYLOAD, source=value)
                envelope = serialize_tips('bet_of_the_day', payload)
                self.assertEqual(
                    envelope['source'],
                    {'label': None, 'date_text': SOURCE_DATE_TEXT},
                )

    def test_no_source_shortage_ever_yields_a_hard_coded_label(self):
        for tip_type, payload in EMPTY_PAYLOAD_FOR_TYPE.items():
            with self.subTest(tip_type=tip_type):
                envelope = serialize_tips(tip_type, payload)
                self.assertEqual(
                    envelope['source'],
                    {'label': None, 'date_text': None},
                )

    def test_fetched_at_is_omitted_when_no_snapshot_is_described(self):
        envelope = serialize_tips('bet_of_the_day', BET_OF_THE_DAY_PAYLOAD)
        self.assertEqual(set(envelope['source']), {'label', 'date_text'})

    def test_fetched_at_is_serialised_as_seconds_precision_utc(self):
        envelope = serialize_tips(
            'bet_of_the_day', BET_OF_THE_DAY_PAYLOAD, fetched_at=FETCHED_AT)
        self.assertEqual(envelope['source'], {
            'label': 'freesupertips',
            'date_text': SOURCE_DATE_TEXT,
            'fetched_at': FETCHED_AT_Z,
        })

    def test_fetched_at_rejects_a_naive_datetime(self):
        with self.assertRaisesRegex(ValueError, 'timezone-aware'):
            serialize_tips(
                'bet_of_the_day', BET_OF_THE_DAY_PAYLOAD,
                fetched_at=NAIVE_FETCHED_AT,
            )

    def test_the_source_block_never_carries_fetch_metadata_or_urls(self):
        payload = dict(
            BET_OF_THE_DAY_PAYLOAD,
            scraped_at='2026-09-27T20:12:03',
            source_url='/bet-of-the-day-tips/',
        )
        envelope = serialize_tips('bet_of_the_day', payload, fetched_at=FETCHED_AT)
        self.assertEqual(
            set(envelope['source']), {'label', 'date_text', 'fetched_at'})
        self.assertEqual(
            FORBIDDEN_V1_KEYS & all_keys(envelope['source']), set())


class SourceDateTextTests(SimpleTestCase):
    """The provenance block quotes the payload's own date; it never interprets it.

    ``source.date_text`` lets a client see which source date the payload claims
    without this layer deciding what that text means: the mapping is a quote, not a
    parse, and the value is only ever the top-level payload's own ``date``.
    """

    def test_the_payload_date_key_is_the_documented_top_level_key(self):
        self.assertEqual(SOURCE_DATE_KEY, 'date')

    def test_every_source_kind_publishes_the_two_provenance_keys(self):
        for tip_type, payload in PAYLOAD_FOR_TYPE.items():
            with self.subTest(tip_type=tip_type):
                envelope = serialize_tips(tip_type, payload)
                self.assertEqual(
                    set(envelope['source']), {'label', 'date_text'})
                self.assertEqual(
                    envelope['source']['date_text'], SOURCE_DATE_TEXT)

    def test_a_valid_top_level_date_text_is_preserved_verbatim(self):
        payload = dict(BET_OF_THE_DAY_PAYLOAD, date='2026-09-28')
        envelope = serialize_tips('bet_of_the_day', payload)
        self.assertEqual(
            envelope['source'],
            {'label': 'freesupertips', 'date_text': '2026-09-28'},
        )

    def test_a_non_iso_date_text_is_quoted_verbatim(self):
        # Parsing is deferred: nothing here validates, normalises, or converts the
        # text, so a value that is not a calendar date comes back as it arrived.
        for value in ('27/09/2026', '2026-9-7', 'next Tuesday', ' 2026-09-27 ',
                      '2026-09-27T19:45:00+01:00'):
            with self.subTest(date=value):
                payload = dict(BET_OF_THE_DAY_PAYLOAD, date=value)
                envelope = serialize_tips('bet_of_the_day', payload)
                self.assertEqual(envelope['source']['date_text'], value)

    def test_a_missing_payload_date_yields_a_null_date_text(self):
        payload = dict(BET_OF_THE_DAY_PAYLOAD)
        payload.pop(SOURCE_DATE_KEY)
        envelope = serialize_tips('bet_of_the_day', payload)
        self.assertEqual(
            envelope['source'],
            {'label': 'freesupertips', 'date_text': None},
        )

    def test_an_unusable_payload_date_yields_a_null_date_text(self):
        for value in ('', None, 7, 20260927, True, ['2026-09-27'],
                      {'date': '2026-09-27'}):
            with self.subTest(date=value):
                payload = dict(BET_OF_THE_DAY_PAYLOAD, date=value)
                envelope = serialize_tips('bet_of_the_day', payload)
                self.assertIsNone(envelope['source']['date_text'])
                self.assertEqual(
                    set(envelope['source']), {'label', 'date_text'})

    def test_the_source_date_is_never_derived_from_a_match_card_or_leg(self):
        # No top-level date at all, while every nested entry states one and one
        # card states a date of its own: the provenance block must stay null.
        payload = {
            'source': 'freesupertips',
            'total_tips': 1,
            'matches': [
                botd_match('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'],
                           tip_category='Tip 1', stake=10.0, returns=18.0,
                           odds=1.8),
            ],
            'accumulators': [
                {
                    'category': 'Daily Accumulator',
                    'date': SOURCE_DATE_TEXT,
                    'stake': 10.0,
                    'returns': 28.56,
                    'total_odds': 2.86,
                    'matches': [
                        leg('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'],
                            '19:45'),
                    ],
                    'matches_count': 1,
                },
            ],
        }
        for tip_type in SUPPORTED_TIP_TYPES:
            with self.subTest(tip_type=tip_type):
                envelope = serialize_tips(tip_type, payload)
                self.assertIsNone(envelope['source']['date_text'])
                self.assertEqual(
                    set(envelope['source']), {'label', 'date_text'})

    def test_a_nested_date_text_or_card_date_is_never_read(self):
        payload = {
            'source': 'freesupertips',
            'date_text': SOURCE_DATE_TEXT,
            'filter': {'date': SOURCE_DATE_TEXT},
            'accumulators': [
                {
                    'category': 'Daily Accumulator',
                    'date_text': SOURCE_DATE_TEXT,
                    'matches': [
                        leg('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'],
                            '19:45'),
                    ],
                },
            ],
        }
        envelope = serialize_tips('daily_accumulator', payload)
        self.assertIsNone(envelope['source']['date_text'])
        self.assertEqual(
            envelope['tips'][0]['legs'][0]['source_date_text'],
            SOURCE_DATE_TEXT,
        )

    def test_the_payload_date_is_not_copied_onto_any_tip_or_leg(self):
        for tip_type in SUPPORTED_TIP_TYPES:
            with self.subTest(tip_type=tip_type):
                payload = PAYLOAD_FOR_TYPE[tip_type]
                envelope = serialize_tips(tip_type, payload)
                for tip in envelope['tips']:
                    self.assertNotIn('date_text', tip)
                    # A ``match`` tip has no legs at all; only card tips nest them.
                    for nested in tip.get('legs', []):
                        self.assertNotIn('date_text', nested)

    def test_the_payload_date_is_quoted_without_parsing_or_converting(self):
        payload = dict(
            BET_OF_THE_DAY_PAYLOAD, date='2026-09-27T19:45:00+01:00')
        envelope = serialize_tips(
            'bet_of_the_day', payload, fetched_at=FETCHED_AT)
        self.assertEqual(
            envelope['source']['date_text'], '2026-09-27T19:45:00+01:00')
        self.assertEqual(envelope['source']['fetched_at'], FETCHED_AT_Z)

    def test_the_serializer_carries_no_date_parser(self):
        source = SERIALIZERS_V1_PATH.read_text(encoding='utf-8')
        tokens = ('dateutil', 'pytz', 'zoneinfo', 'strptime', 'timestamp(')
        for token in tokens:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_the_source_date_leaves_neighbouring_contracts_unchanged(self):
        # A filter block speaks about the requested date, so it is the one place a
        # legacy-shaped key is legitimate: it is the caller's decision carried
        # through, not a key this layer publishes.
        filter_block = {
            'timezone': 'Europe/London',
            'available_date': None,
            'matched': False,
        }
        envelope = serialize_tips(
            'bet_of_the_day', BET_OF_THE_DAY_PAYLOAD,
            fetched_at=FETCHED_AT, filter_block=filter_block,
        )
        self.assertEqual(envelope['source']['label'], 'freesupertips')
        self.assertEqual(
            set(envelope),
            set(SUCCESS_ENVELOPE_KEYS) | OPTIONAL_ENVELOPE_KEYS,
        )
        self.assertEqual(FORBIDDEN_V1_KEYS & all_keys(envelope), set())
        self.assertEqual(envelope['filter'], filter_block)
        for tip in envelope['tips']:
            with self.subTest(match_title=tip['match_title']):
                self.assertEqual(set(tip), set(MATCH_UNIT_TIP_KEYS))
                self.assertIsNone(tip['kickoff_at'])
                self.assertIs(tip['kickoff_time_verified'], False)
        self.assertEqual(TIP_TYPE_UNITS['bet_of_the_day'], UNIT_MATCH)


class FilterSeamTests(SimpleTestCase):
    """The serializer carries a decided filter block; it never computes one."""

    FILTER_BLOCK = {
        'date': '2026-09-27',
        'timezone': 'Europe/London',
        'available_date': '2026-09-27',
        'matched': True,
    }

    def test_the_filter_block_is_absent_when_the_caller_supplies_none(self):
        envelope = serialize_tips('bet_of_the_day', BET_OF_THE_DAY_PAYLOAD)
        self.assertNotIn('filter', envelope)

    def test_a_supplied_filter_block_is_carried_through_unchanged(self):
        envelope = serialize_tips(
            'bet_of_the_day', BET_OF_THE_DAY_PAYLOAD,
            filter_block=self.FILTER_BLOCK,
        )
        self.assertEqual(envelope['filter'], self.FILTER_BLOCK)

    def test_the_serializer_never_normalises_or_derives_filter_values(self):
        undecided = {
            'date': '2026-99-99',
            'timezone': 'not-a-zone',
            'available_date': None,
            'matched': False,
        }
        envelope = serialize_tips(
            'daily_accumulator', DAILY_ACCUMULATOR_PAYLOAD,
            filter_block=undecided,
        )
        self.assertEqual(envelope['filter'], undecided)

    def test_the_filter_block_is_defensively_copied(self):
        supplied = dict(self.FILTER_BLOCK)
        envelope = serialize_tips(
            'bet_of_the_day', BET_OF_THE_DAY_PAYLOAD, filter_block=supplied)
        supplied['matched'] = False
        self.assertTrue(envelope['filter']['matched'])


class MalformedPayloadTests(KickoffMarkerAssertions, SimpleTestCase):
    """A bad payload yields an empty envelope or a refusal, never an exception."""

    def test_a_non_mapping_payload_is_treated_as_empty(self):
        for tip_type in SUPPORTED_TIP_TYPES:
            for payload in (None, [], 'not-a-payload', 42):
                with self.subTest(tip_type=tip_type, payload=payload):
                    envelope = serialize_tips(tip_type, payload)
                    self.assertEqual(envelope['tips'], [])
                    self.assertEqual(envelope['count'], 0)
                    self.assertEqual(
                        envelope['source'],
                        {'label': None, 'date_text': None},
                    )

    def test_non_mapping_entries_are_skipped(self):
        payload = dict(BET_OF_THE_DAY_PAYLOAD, matches=[
            None, 'not-a-selection', 7,
            botd_match('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'],
                       tip_category='Tip 1', stake=10.0, returns=18.0, odds=1.8),
        ])
        envelope = serialize_tips('bet_of_the_day', payload)
        self.assertEqual(envelope['count'], 1)
        self.assertEqual(envelope['tips'][0]['match_title'], 'Arsenal vs Chelsea')

    def test_an_unknown_field_on_a_selection_is_not_copied(self):
        selection = botd_match('Arsenal vs Chelsea', ['Arsenal', 'Chelsea'],
                               tip_category='Tip 1', stake=10.0, returns=18.0,
                               odds=1.8)
        selection['competition'] = 'Premier League'
        selection['home_score'] = 2
        envelope = serialize_tips(
            'bet_of_the_day', dict(BET_OF_THE_DAY_PAYLOAD, matches=[selection]))
        self.assertEqual(set(envelope['tips'][0]), set(MATCH_UNIT_TIP_KEYS))

    def test_missing_text_fields_default_to_empty_values(self):
        tip = serialize_tips('bet_of_the_day', {'matches': [{}]})['tips'][0]
        self.assertEqual(tip['match_title'], '')
        self.assertEqual(tip['teams'], [])
        self.assertEqual(tip['prediction'], '')
        self.assertEqual(tip['opponent_text'], '')
        self.assertEqual(tip['tip_reason'], '')
        self.assertEqual(tip['match_url'], '')
        self.assertEqual(tip['source_date_text'], '')
        self.assertEqual(tip['source_time_text'], '')
        self.assertEqual(tip['category'], '')
        self.assertIsNone(tip['stake'])
        self.assertIsNone(tip['returns'])
        self.assertIsNone(tip['odds'])
        self.assertIsNone(tip['kickoff_at'])
        self.assertIs(tip['kickoff_time_verified'], False)

    def test_a_card_without_a_leg_list_has_no_legs(self):
        payload = {'accumulators': [
            {'category': 'Daily Accumulator', 'stake': 10.0, 'returns': 28.56,
             'total_odds': 2.86, 'matches': None, 'matches_count': 2},
        ]}
        envelope = serialize_tips('daily_accumulator', payload)
        self.assertEqual(envelope['count'], 1)
        self.assertEqual(envelope['legs_count'], 0)
        self.assertEqual(envelope['tips'][0]['legs'], [])

    def test_an_unknown_tip_type_is_rejected_not_guessed(self):
        for candidate in ('not-a-source', '', 'Bet_of_the_day', None, 7,
                          ['bet_of_the_day'], {'tip_type': 'bet_of_the_day'}):
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnknownTipType):
                    serialize_tips(candidate, BET_OF_THE_DAY_PAYLOAD)


class ErrorEnvelopeTests(SimpleTestCase):
    """Error bodies are documented, client-safe, and free of internal detail."""

    EXPECTED_CODES = frozenset({
        'missing_tip_type', 'unknown_tip_type', 'invalid_date',
        'timezone_requires_date', 'missing_timezone', 'invalid_timezone',
        'source_unavailable',
    })

    def test_error_codes_are_the_documented_set(self):
        self.assertEqual(ERROR_CODES, self.EXPECTED_CODES)

    def test_error_body_has_the_versioned_shape(self):
        body = api_error(ERROR_MISSING_TIP_TYPE)
        self.assertEqual(set(body), {'api_version', 'error'})
        self.assertEqual(body['api_version'], API_VERSION)
        self.assertEqual(set(body['error']), {'code', 'message', 'field'})
        self.assertEqual(body['error']['code'], ERROR_MISSING_TIP_TYPE)
        self.assertIsNone(body['error']['field'])

    def test_every_documented_code_builds_a_body_of_the_same_shape(self):
        for code in sorted(ERROR_CODES):
            with self.subTest(code=code):
                body = api_error(code, field='date')
                self.assertEqual(set(body['error']), {'code', 'message', 'field'})
                self.assertEqual(body['error']['code'], code)
                self.assertEqual(body['error']['field'], 'date')
                self.assertTrue(body['error']['message'])

    def test_the_codes_the_endpoint_will_publish_are_present(self):
        for code in (ERROR_MISSING_TIP_TYPE, ERROR_UNKNOWN_TIP_TYPE,
                     ERROR_INVALID_DATE, ERROR_TIMEZONE_REQUIRES_DATE,
                     ERROR_MISSING_TIMEZONE, ERROR_SOURCE_UNAVAILABLE):
            with self.subTest(code=code):
                self.assertIn(code, ERROR_CODES)

    def test_messages_carry_no_host_url_environment_or_exception_detail(self):
        forbidden = ('http', '://', '.com', 'freesupertips', '.env', 'Traceback',
                     'Exception', 'scrape', 'requests', 'SECRET', 'sqlite')
        for code in sorted(ERROR_CODES):
            with self.subTest(code=code):
                body = json.dumps(api_error(code))
                for token in forbidden:
                    self.assertNotIn(token, body)

    def test_an_undocumented_code_is_refused(self):
        for candidate in ('not_a_code', '', 'SOURCE_UNAVAILABLE', None):
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnknownErrorCode):
                    api_error(candidate)
                with self.assertRaises(ValueError):
                    api_error(candidate)


class StandardLibraryOnlyTests(SimpleTestCase):
    """The serializers must stay importable without a configured environment."""

    def imported_roots(self):
        """Return (top-level import roots, relative imports) of the module."""
        tree = ast.parse(SERIALIZERS_V1_PATH.read_text(encoding='utf-8'))
        roots = set()
        relative_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative_imports.append((node.level, node.module))
                    continue
                roots.add((node.module or '').split('.')[0])
        return roots, relative_imports

    def test_serializers_import_only_standard_library_modules(self):
        roots, _relative = self.imported_roots()
        self.assertTrue(roots, 'the serializers must import something')
        self.assertEqual(roots - ALLOWED_IMPORT_ROOTS, set())

    def test_serializers_make_no_relative_import(self):
        _roots, relative_imports = self.imported_roots()
        self.assertEqual(relative_imports, [])

    def test_serializers_never_import_a_forbidden_root(self):
        roots, _relative = self.imported_roots()
        self.assertEqual(roots & FORBIDDEN_IMPORT_ROOTS, set())

    def test_serializer_source_names_no_framework_scraper_or_environment(self):
        source = SERIALIZERS_V1_PATH.read_text(encoding='utf-8').lower()
        for token in FORBIDDEN_SOURCE_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_the_serializer_never_hard_codes_a_source_label(self):
        source = SERIALIZERS_V1_PATH.read_text(encoding='utf-8')
        self.assertNotIn('freesupertips', source)

    def test_the_module_records_the_read_model_dependency_direction(self):
        source = SERIALIZERS_V1_PATH.read_text(encoding='utf-8')
        self.assertIn('Dependency direction', source)
        self.assertIn(
            'the read model neither imports this module nor reuses', source)
        self.assertIn('YYYY-MM-DDTHH:MM:SSZ', source)

    def test_the_module_records_the_deferred_snapshot_date_rule(self):
        source = SERIALIZERS_V1_PATH.read_text(encoding='utf-8')
        self.assertIn('source_date_text is exactly a strict', source)
        self.assertIn('filter.available_date is null', source)
