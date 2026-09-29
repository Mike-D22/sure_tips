"""Pure mapping and snapshot-seam tests for the versioned tips API.

Why this module exists
----------------------
``/api/v1/tips/`` publishes a different envelope from the frozen legacy payloads
(``docs/DATA_CONTRACT.md`` §3): the source display text is renamed, the provenance
block carries ``label`` plus the payload's own ``date_text``, each tip is labelled
with a unit, every mapped leg carries the contract's fixed temporal markers, and
the volatile fetch metadata is dropped.
Those renames and fixed fields are the contract a client will be written against,
so they are pinned here
before any view existed — the serializers are the only thing that decides the
public shape, and a rename is exactly the kind of change that is invisible until
it breaks a client.

The snapshot seam (``readmodel_v1``) is pinned here for the same reason: it is
what decides whether a snapshot's timestamp is a ``datetime`` or text, and whether
stored state can be changed from outside the store. Both are invisible until a
later provider or a client is built on the wrong answer, and both are cheap to
pin while the store still has no other caller.

Ground rules
------------
* **No network, no database.** Nothing here can fetch and nothing here writes:
  the serializer and read-model tests are pure calls on a ``SimpleTestCase`` or on
  the process-local default snapshot provider, and the endpoint tests at the foot
  of this module run with the socket layer disabled (``OfflineGuardMixin``), so an
  accidental outbound request raises instead of passing quietly.
* **The mapping and store layers stay scraper-free.** The payloads they are
  tested with are hand-written mirrors of the frozen parser envelopes, in the
  style of ``tests.py``, so this module's mapping tests import neither the scraper
  module nor a fixture file: the public serializers must be usable without the
  scraper's import-time configuration. The registry is compared against the
  legacy configuration table in the Django-configured layer. Only the endpoint
  section imports the legacy layer, through ``from . import utils``, and only to
  drive the frozen parsers over the static synthetic fixtures — never to fetch,
  and never to hand-write a second copy of a payload shape.
* **No clock.** Every timestamp comes from a fixed constant, so no expectation
  here depends on the machine's date, timezone, or locale.
* **No formatter borrowing.** The read model stores and normalises timezone-aware
  UTC ``datetime`` objects and does not import the serializers; the only wire
  format ``YYYY-MM-DDTHH:MM:SSZ`` is produced by ``serializers_v1.format_utc_z()``,
  which the timestamp tests below pin.

Commit scope
------------
Commit 1 covered ``serializers_v1``: the pure payload-to-envelope mapping. Commit
2 adds the seam that mapping is published from — the framework-free snapshot
store in ``readmodel_v1`` and its process-local default provider — and pins both
halves of that one contract together here: the read model decides what a snapshot
*is* (an opaque key, an unparsed payload, and an authoritative aware UTC
``datetime``), and the serializers decide how it appears on the wire.

Commit 3 adds the offline endpoint: its route and namespace, its query validation
order, its ``filter`` block, its ``503``, and its error bodies, driven through
Django's test client with snapshots seeded through ``store_snapshot()`` from
fixture-derived parser payloads. It still asserts nothing about a durable or
shared provider, and it still never fetches: the endpoint's data source is the
seam.

Still left to a later commit: a durable or shared snapshot provider, provider
selection through configuration, the scraper being wired to fill a snapshot, and
any conversion of source display text into an authoritative timestamp.
"""

import ast
import json
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase
from django.urls import resolve, reverse

from . import readmodel_v1
from . import urls_v1
from . import utils
from . import views_v1
from .readmodel_v1 import (
    SNAPSHOT_KEYS,
    SNAPSHOT_SCHEMA_VERSION,
    InMemorySnapshotProvider,
    clear_snapshots,
    get_snapshot_provider,
    load_snapshot,
    set_snapshot_provider,
    store_snapshot,
)
from .serializers_v1 import (
    API_VERSION,
    CARD_UNIT_TIP_KEYS,
    ERROR_CODES,
    ERROR_INVALID_DATE,
    ERROR_INVALID_TIMEZONE,
    ERROR_MESSAGES,
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
from .tests_parser_contract import (
    FIXTURE_FOR_SOURCE,
    OfflineGuardMixin,
    TEST_DATE,
    load_fixture,
)
from .views_v1 import (
    FILTER_KEYS,
    LEGACY_SOURCE_KEY,
    QUERY_PARAMETERS,
    tips_v1,
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
# The read-model seam (``readmodel_v1``)
# ---------------------------------------------------------------------------

READMODEL_V1_PATH = Path(__file__).resolve().parent / 'readmodel_v1.py'

# The read model may import these standard-library roots and nothing else.
READMODEL_ALLOWED_IMPORT_ROOTS = frozenset({'copy', 'datetime', 'threading'})

# The only four standalone names the read model may import: the deep copy, the
# two datetime vocabulary names, and the lock.
READMODEL_ALLOWED_IMPORTS = frozenset({
    'copy.deepcopy', 'datetime.datetime', 'datetime.timezone', 'threading.Lock',
})

# Tokens that must not appear anywhere in the read-model source, comments and
# docstrings included: a framework, a source fetch, an environment read, a wire
# format, or a timestamp formatter.
READMODEL_FORBIDDEN_SOURCE_TOKENS = (
    'django', 'cloudscraper', 'requests', 'aiohttp', 'decouple', 'urllib',
    'socket', 'http', 'getenv', 'os.environ', 'scrape', 'json', 'strftime',
    'isoformat', 'strptime', 'fromisoformat', 'serializers', 'cache',
)

# Tokens that would turn the store into a second copy of the v1 tip-type
# registry. A key is an opaque storage key, not a known type.
READMODEL_FORBIDDEN_REGISTRY_TOKENS = (
    'bet_of_the_day', 'daily_accumulator', 'over_25_goals',
    'both_teams_to_score', 'btts_and_win', 'anytime_goalscorer',
    'tip_type', 'SUPPORTED_TIP_TYPES', 'TIP_TYPE_UNITS', 'is_accumulator',
    'unit_for',
)

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


# ---------------------------------------------------------------------------
# The snapshot seam (``readmodel_v1``).
# ---------------------------------------------------------------------------

SNAPSHOT_RECORD_KEYS = {
    'schema_version', 'type_key', 'payload', 'fetched_at',
}


class RecordingSnapshotProvider:
    """A deliberate custom provider: it records what the seam delegates to it.

    Deliberately permissive — it validates nothing, copies nothing, and keeps the
    stamp it is handed verbatim — so a test can prove that the module-level seam
    added no rule of its own on top of the default provider's policy.
    """

    def __init__(self):
        self.calls = []
        self.records = {}

    def load(self, type_key):
        self.calls.append(('load', type_key))
        return self.records.get(type_key)

    def store(self, type_key, payload, *, fetched_at=None):
        self.calls.append(('store', type_key, payload, fetched_at))
        record = {
            'type_key': type_key,
            'payload': payload,
            'fetched_at': fetched_at,
        }
        self.records[type_key] = record
        return record

    def clear(self):
        self.calls.append(('clear',))
        self.records.clear()


class ProviderWithoutACallableLoad:
    """A provider candidate whose ``load`` attribute cannot be delegated to."""

    load = None

    def store(self, type_key, payload, *, fetched_at=None):
        return None

    def clear(self):
        pass


class ProviderWithoutClear:
    """A provider candidate that exposes only two of the three methods."""

    def load(self, type_key):
        return None

    def store(self, type_key, payload, *, fetched_at=None):
        return None


class SnapshotProviderTestCase(SimpleTestCase):
    """Give every test its own empty default provider.

    The seam holds one process-global slot, so a test that left a provider, or a
    stored record, behind would decide what the next test sees.
    """

    def setUp(self):
        super().setUp()
        set_snapshot_provider(None)

    def tearDown(self):
        set_snapshot_provider(None)
        super().tearDown()


class SnapshotVocabularyTests(SimpleTestCase):
    """One record shape, one schema version, and no second tip-type registry."""

    def test_the_stored_schema_version_is_one(self):
        self.assertEqual(SNAPSHOT_SCHEMA_VERSION, 1)

    def test_a_snapshot_is_exactly_the_four_documented_keys(self):
        self.assertEqual(set(SNAPSHOT_KEYS), SNAPSHOT_RECORD_KEYS)
        self.assertEqual(len(SNAPSHOT_KEYS), 4)
        for key in SNAPSHOT_KEYS:
            with self.subTest(key=key):
                self.assertIsInstance(key, str)

    def test_the_module_publishes_the_documented_seam_names(self):
        for name in (
            'SNAPSHOT_SCHEMA_VERSION', 'SNAPSHOT_KEYS',
            'InMemorySnapshotProvider', 'get_snapshot_provider',
            'set_snapshot_provider', 'load_snapshot', 'store_snapshot',
            'clear_snapshots',
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(readmodel_v1, name))

    def test_a_stored_record_carries_exactly_those_keys(self):
        provider = InMemorySnapshotProvider()
        record = provider.store('any_key_at_all', {'matches': []})
        self.assertEqual(set(record), SNAPSHOT_RECORD_KEYS)
        self.assertEqual(
            set(provider.load('any_key_at_all')), SNAPSHOT_RECORD_KEYS)

    def test_a_record_is_stamped_with_the_schema_version_and_its_own_key(self):
        provider = InMemorySnapshotProvider()
        record = provider.store('any_key_at_all', {'matches': []})
        self.assertEqual(record['schema_version'], SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(record['type_key'], 'any_key_at_all')


class SnapshotStorageTests(SimpleTestCase):
    """The default provider holds snapshots, or reports that it holds none."""

    def setUp(self):
        super().setUp()
        self.provider = InMemorySnapshotProvider()

    def test_an_absent_key_loads_as_none(self):
        self.assertIsNone(self.provider.load('never_stored'))

    def test_a_stored_payload_and_its_key_round_trip(self):
        payload = {'matches': ['a', 'b'], 'count': 2}
        self.provider.store('bet_of_the_day', payload)
        record = self.provider.load('bet_of_the_day')
        self.assertEqual(record['payload'], payload)
        self.assertEqual(record['type_key'], 'bet_of_the_day')

    def test_a_later_store_replaces_the_record_for_the_same_key(self):
        self.provider.store('bet_of_the_day', {'version': 1})
        self.provider.store('bet_of_the_day', {'version': 2})
        self.assertEqual(
            self.provider.load('bet_of_the_day')['payload'], {'version': 2})

    def test_a_key_the_endpoint_never_uses_is_a_legitimate_key(self):
        # The store owns storage, not the tip-type vocabulary: an unknown but
        # hashable key, an integer, a tuple and a bool are all usable keys.
        for type_key in ('not-a-v1-key', 7, ('a', 'tuple'), True):
            with self.subTest(type_key=type_key):
                self.provider.store(type_key, {'payload': type_key})
                self.assertEqual(
                    self.provider.load(type_key)['type_key'], type_key)

    def test_two_providers_never_share_a_record(self):
        other = InMemorySnapshotProvider()
        self.provider.store('bet_of_the_day', {'matches': []})
        self.assertIsNone(other.load('bet_of_the_day'))

    def test_clearing_forgets_every_record(self):
        self.provider.store('bet_of_the_day', {})
        self.provider.store('daily_accumulator', {})
        self.provider.clear()
        self.assertIsNone(self.provider.load('bet_of_the_day'))
        self.assertIsNone(self.provider.load('daily_accumulator'))

    def test_clearing_an_empty_provider_is_idempotent(self):
        self.provider.clear()
        self.provider.clear()
        self.assertIsNone(self.provider.load('bet_of_the_day'))

    def test_an_unhashable_key_is_refused_by_storage(self):
        for type_key in ([], {}, {'a': 'b'}, ['bet_of_the_day']):
            with self.subTest(type_key=type_key):
                with self.assertRaises(TypeError):
                    self.provider.store(type_key, {'matches': []})
                with self.assertRaises(TypeError):
                    self.provider.load(type_key)

    def test_a_refused_key_leaves_the_provider_usable(self):
        with self.assertRaises(TypeError):
            self.provider.store([], {'matches': []})
        self.provider.store('bet_of_the_day', {'matches': []})
        self.assertEqual(
            self.provider.load('bet_of_the_day')['payload'], {'matches': []})


class SnapshotCopyTests(SimpleTestCase):
    """Stored state is never reachable through an alias a caller holds."""

    def setUp(self):
        super().setUp()
        self.provider = InMemorySnapshotProvider()
        self.payload = {
            'date': SOURCE_DATE_TEXT,
            'matches': [{
                'match_title': 'Arsenal vs Chelsea',
                'teams': ['Arsenal', 'Chelsea'],
            }],
        }

    def test_mutating_the_payload_after_the_store_does_not_change_it(self):
        self.provider.store('bet_of_the_day', self.payload)
        self.payload['matches'][0]['teams'][0] = 'Mutated'
        self.payload['matches'].append({'match_title': 'Invented'})
        stored = self.provider.load('bet_of_the_day')['payload']
        self.assertEqual(stored['date'], SOURCE_DATE_TEXT)
        self.assertEqual(stored['matches'][0]['teams'], ['Arsenal', 'Chelsea'])
        self.assertEqual(len(stored['matches']), 1)

    def test_mutating_a_loaded_record_does_not_change_the_snapshot(self):
        self.provider.store('bet_of_the_day', self.payload)
        loaded = self.provider.load('bet_of_the_day')
        loaded['payload']['matches'].append({'match_title': 'Invented'})
        loaded['type_key'] = 'mutated'
        stored = self.provider.load('bet_of_the_day')
        self.assertEqual(len(stored['payload']['matches']), 1)
        self.assertEqual(stored['type_key'], 'bet_of_the_day')

    def test_the_record_returned_by_the_store_is_a_defensive_copy(self):
        record = self.provider.store('bet_of_the_day', self.payload)
        record['payload']['matches'].append({'match_title': 'Invented'})
        record['payload']['matches'][0]['match_title'] = 'Mutated'
        stored = self.provider.load('bet_of_the_day')['payload']
        self.assertEqual(len(stored['matches']), 1)
        self.assertEqual(stored['matches'][0]['match_title'],
                         'Arsenal vs Chelsea')

    def test_each_load_returns_a_fresh_object(self):
        self.provider.store('bet_of_the_day', self.payload)
        first = self.provider.load('bet_of_the_day')
        second = self.provider.load('bet_of_the_day')
        self.assertIsNot(first, second)
        self.assertIsNot(first['payload'], second['payload'])
        self.assertIsNot(
            first['payload']['matches'], second['payload']['matches'])


class SnapshotTimestampTests(SimpleTestCase):
    """A snapshot's stamp is an aware UTC datetime, or the store refuses it."""

    def setUp(self):
        super().setUp()
        self.provider = InMemorySnapshotProvider()

    def store(self, **kwargs):
        """Store one trivial snapshot and return the stored record."""
        return self.provider.store('bet_of_the_day', {'matches': []},
                                   **kwargs)

    def test_an_omitted_stamp_is_now_in_utc(self):
        before = datetime.now(timezone.utc)
        stamp = self.store()['fetched_at']
        after = datetime.now(timezone.utc)
        self.assertIsInstance(stamp, datetime)
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, after)

    def test_an_explicit_none_stamp_means_omitted(self):
        stamp = self.store(fetched_at=None)['fetched_at']
        self.assertIsInstance(stamp, datetime)
        self.assertEqual(stamp.utcoffset(), timedelta(0))

    def test_a_stored_stamp_is_a_datetime_and_never_the_wire_text(self):
        stamp = self.store(fetched_at=FETCHED_AT)['fetched_at']
        self.assertIsInstance(stamp, datetime)
        self.assertNotIsInstance(stamp, str)
        self.assertEqual(stamp, FETCHED_AT)
        # The wire text of the same instant is a different type and value:
        # nothing in the store rendered or parsed it.
        self.assertNotEqual(stamp, FETCHED_AT_Z)

    def test_a_stored_stamp_keeps_its_microseconds(self):
        stamp = FETCHED_AT.replace(microsecond=987654)
        stored = self.store(fetched_at=stamp)
        self.assertEqual(stored['fetched_at'], stamp)
        self.assertEqual(stored['fetched_at'].microsecond, 987654)

    def test_a_naive_datetime_is_refused_rather_than_assumed_utc(self):
        with self.assertRaisesRegex(ValueError, 'timezone-aware'):
            self.store(fetched_at=NAIVE_FETCHED_AT)
        self.assertIsNone(self.provider.load('bet_of_the_day'))

    def test_a_non_datetime_stamp_is_refused(self):
        for candidate in (FETCHED_AT_Z, '', 0, True, [], {}):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TypeError):
                    self.store(fetched_at=candidate)
        self.assertIsNone(self.provider.load('bet_of_the_day'))

    def test_a_date_is_not_a_datetime_stamp(self):
        with self.assertRaises(TypeError):
            self.store(fetched_at=date(2026, 9, 28))

    def test_an_aware_non_utc_stamp_is_normalised_to_utc(self):
        for offset in (timedelta(hours=3), timedelta(hours=-5),
                       timedelta(hours=1, minutes=30)):
            with self.subTest(offset=offset):
                supplied = FETCHED_AT.astimezone(timezone(offset))
                stamp = self.store(fetched_at=supplied)['fetched_at']
                self.assertEqual(stamp, supplied)
                self.assertEqual(stamp, FETCHED_AT)
                self.assertEqual(stamp.utcoffset(), timedelta(0))
                self.assertEqual(stamp.tzinfo, timezone.utc)

    def test_the_stored_stamp_stays_a_datetime_on_the_way_back_out(self):
        self.store(fetched_at=FETCHED_AT)
        loaded = self.provider.load('bet_of_the_day')['fetched_at']
        self.assertIsInstance(loaded, datetime)
        self.assertEqual(loaded, FETCHED_AT)
        self.assertEqual(loaded.utcoffset(), timedelta(0))

    def test_a_refused_stamp_stores_nothing_at_all(self):
        with self.assertRaises(TypeError):
            self.store(fetched_at=FETCHED_AT_Z)
        self.store()
        self.assertEqual(
            self.provider.load('bet_of_the_day')['payload'], {'matches': []})


class ModuleSeamTests(SnapshotProviderTestCase):
    """The seam forwards to the installed provider and adds no rule of its own."""

    def test_the_installed_provider_defaults_to_the_in_memory_one(self):
        self.assertIsInstance(get_snapshot_provider(),
                              InMemorySnapshotProvider)

    def test_the_seam_stores_and_loads_through_the_default_provider(self):
        record = store_snapshot('bet_of_the_day', {'matches': [1]},
                                fetched_at=FETCHED_AT)
        self.assertEqual(set(record), SNAPSHOT_RECORD_KEYS)
        loaded = load_snapshot('bet_of_the_day')
        self.assertEqual(loaded['payload'], {'matches': [1]})
        self.assertEqual(loaded['fetched_at'], FETCHED_AT)
        self.assertIsNot(loaded, record)
        clear_snapshots()
        self.assertIsNone(load_snapshot('bet_of_the_day'))

    def test_setting_none_installs_a_brand_new_empty_default_provider(self):
        previous = get_snapshot_provider()
        store_snapshot('bet_of_the_day', {'matches': [1]})
        set_snapshot_provider(None)
        replacement = get_snapshot_provider()
        self.assertIsNot(replacement, previous)
        self.assertIsInstance(replacement, InMemorySnapshotProvider)
        self.assertIsNone(load_snapshot('bet_of_the_day'))
        # The provider it replaced was discarded, not emptied: a reader already
        # holding it keeps the record it was handed.
        self.assertEqual(
            previous.load('bet_of_the_day')['payload'], {'matches': [1]})

    def test_store_and_load_delegate_to_the_installed_provider(self):
        provider = RecordingSnapshotProvider()
        set_snapshot_provider(provider)
        payload = {'matches': [1]}
        record = store_snapshot('custom_key', payload, fetched_at=FETCHED_AT)
        self.assertEqual(
            provider.calls, [('store', 'custom_key', payload, FETCHED_AT)])
        self.assertIs(record['payload'], payload)
        self.assertIs(record['fetched_at'], FETCHED_AT)
        self.assertIs(load_snapshot('custom_key'), record)
        self.assertEqual(provider.calls[-1], ('load', 'custom_key'))

    def test_the_seam_never_invents_a_stamp_for_a_custom_provider(self):
        provider = RecordingSnapshotProvider()
        set_snapshot_provider(provider)
        store_snapshot('custom_key', {'matches': []})
        self.assertEqual(provider.calls[0][0], 'store')
        self.assertIsNone(provider.calls[0][3])

    def test_the_seam_does_not_impose_the_default_stamp_policy(self):
        # The strict "aware UTC datetime or refuse" rule belongs to
        # InMemorySnapshotProvider. A deliberate provider may give the stamp its
        # own meaning, so the seam must forward it untouched.
        provider = RecordingSnapshotProvider()
        set_snapshot_provider(provider)
        record = store_snapshot('custom_key', {}, fetched_at=FETCHED_AT_Z)
        self.assertEqual(record['fetched_at'], FETCHED_AT_Z)

    def test_the_seam_does_not_impose_the_default_key_policy(self):
        provider = RecordingSnapshotProvider()
        set_snapshot_provider(provider)
        store_snapshot('not-a-v1-key', {'matches': []})
        self.assertEqual(provider.calls[0][1], 'not-a-v1-key')

    def test_clear_delegates_to_the_installed_provider(self):
        provider = RecordingSnapshotProvider()
        set_snapshot_provider(provider)
        store_snapshot('custom_key', {'matches': []})
        self.assertEqual(len(provider.records), 1)
        clear_snapshots()
        self.assertEqual(provider.records, {})
        self.assertEqual(provider.calls[-1], ('clear',))

    def test_a_custom_provider_with_the_three_methods_is_accepted(self):
        provider = RecordingSnapshotProvider()
        set_snapshot_provider(provider)
        self.assertIs(get_snapshot_provider(), provider)

    def test_a_provider_that_cannot_be_delegated_to_is_refused(self):
        candidates = ('not-a-provider', object(), 7, [], {},
                      ProviderWithoutACallableLoad(), ProviderWithoutClear())
        for candidate in candidates:
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(TypeError):
                    set_snapshot_provider(candidate)

    def test_a_refused_provider_leaves_the_installed_one_untouched(self):
        installed = get_snapshot_provider()
        store_snapshot('bet_of_the_day', {'matches': [1]})
        with self.assertRaises(TypeError):
            set_snapshot_provider(ProviderWithoutClear())
        self.assertIs(get_snapshot_provider(), installed)
        self.assertEqual(
            load_snapshot('bet_of_the_day')['payload'], {'matches': [1]})

    def test_the_refusal_message_names_the_missing_method(self):
        with self.assertRaisesRegex(TypeError, 'clear'):
            set_snapshot_provider(ProviderWithoutClear())


class ReadModelIsolationTests(SimpleTestCase):
    """The snapshot seam stays importable without a configured environment."""

    def read_source(self):
        return READMODEL_V1_PATH.read_text(encoding='utf-8')

    def imported_names(self):
        """Return (import roots, imported dotted names, relative imports)."""
        tree = ast.parse(self.read_source())
        roots = set()
        names = set()
        relative_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split('.')[0] for alias in node.names)
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative_imports.append((node.level, node.module))
                    continue
                roots.add((node.module or '').split('.')[0])
                names.update(
                    f'{node.module}.{alias.name}' for alias in node.names)
        return roots, names, relative_imports

    def test_the_read_model_imports_only_standard_library_roots(self):
        roots, _names, _relative = self.imported_names()
        self.assertTrue(roots, 'the read model must import something')
        self.assertEqual(roots, set(READMODEL_ALLOWED_IMPORT_ROOTS))

    def test_the_read_model_imports_only_the_four_allowed_names(self):
        _roots, names, _relative = self.imported_names()
        self.assertEqual(names, set(READMODEL_ALLOWED_IMPORTS))

    def test_the_read_model_makes_no_relative_import(self):
        _roots, _names, relative = self.imported_names()
        self.assertEqual(relative, [])

    def test_the_read_model_names_no_framework_source_or_formatter(self):
        source = self.read_source().lower()
        for token in READMODEL_FORBIDDEN_SOURCE_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_the_read_model_holds_no_tip_type_registry(self):
        source = self.read_source().lower()
        for token in READMODEL_FORBIDDEN_REGISTRY_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token.lower(), source)

    def test_the_read_model_records_the_standard_library_rule(self):
        source = self.read_source()
        self.assertIn('Standard library only', source)
        self.assertIn("``threading.Lock``", source)
        self.assertIn('makes no network call', source)

    def test_the_read_model_records_the_delegation_rule(self):
        source = self.read_source()
        self.assertIn(
            'The module-level seam functions delegate to the', source)
        self.assertIn(
            'installed provider and add no rule of their own', source)
        self.assertIn('InMemorySnapshotProvider', source)

    def test_the_read_model_records_the_datetime_and_wire_rule(self):
        source = self.read_source()
        self.assertIn('aware UTC', source)
        self.assertIn('never renders that', source)
        self.assertIn('never parses text into it', source)

    def test_the_read_model_records_the_commit_scope(self):
        source = self.read_source()
        self.assertIn('Commit 2 publishes the seam', source)
        self.assertIn('Nothing here is', source)
        self.assertIn('wired into a view', source)


# ---------------------------------------------------------------------------
# The endpoint: ``views_v1`` and its ``/api/v1/`` route (Commit 3)
# ---------------------------------------------------------------------------

# The one public URL this commit adds, and the namespaced route name a client
# builds it from.
ENDPOINT_PATH = '/api/v1/tips/'
ENDPOINT_ROUTE_NAME = 'tips_v1:tips'

VIEWS_V1_PATH = Path(__file__).resolve().parent / 'views_v1.py'
URLS_V1_PATH = Path(__file__).resolve().parent / 'urls_v1.py'

# The legacy routes: the versioned surface is additive, so all of these must keep
# resolving to the same names they resolved to before it existed.
LEGACY_ROUTES = (
    ('/api/health/', 'health'),
    ('/api/bet-of-the-day/', 'bet_of_the_day'),
    ('/api/daily-accumulator/', 'daily_accumulator'),
    ('/api/btts-win-accumulator/', 'btts_win_accumulator'),
    ('/api/over-25-goals-accumulator/', 'over_25_goals_accumulator'),
    ('/api/BTTS/', 'both_teams_to_score'),
    ('/api/goalscorer/', 'anytime_goalscorer'),
)

# The response layer may import Django and the standard library, plus exactly two
# frozen v1 modules, and nothing else: no scraper, no parser, no transport, no
# cache helper, and no configuration reader.
VIEWS_V1_ALLOWED_IMPORT_ROOTS = frozenset({
    'collections', 'datetime', 'django', 'logging', 're', 'zoneinfo',
})

# Tokens that must not appear anywhere in the response layer's source, comments
# and docstrings included. The read half of the seam is deliberately absent from
# this scan: the AST test pins the single name that may be imported from it.
VIEWS_V1_FORBIDDEN_SOURCE_TOKENS = (
    'utils', 'handlers', 'parsers', 'cache_matches', 'cloudscraper',
    'requests', 'aiohttp', 'urllib', 'socket', 'http.client', 'scrape_one',
    'scrape_all', 'InMemorySnapshotProvider', 'timezone.activate', 'localtime',
    'astimezone', 'timedelta', 'utcnow', 'fromtimestamp', 'strftime',
    'strptime', 'settings', 'TIME_ZONE', 'getenv', 'environ', 'scrape_url',
    'scraped_at', 'source_url', 'SOURCE_LABEL_KEY',
)

# Request dates that are not the documented strict spelling. The first two are the
# reason the endpoint checks the spelling before it parses anything:
# ``date.fromisoformat()`` alone accepts both of them on Python 3.11+.
UNSTRICT_DATE_TEXTS = (
    '20260927', '2026-W39-1', '2026-9-27', '27-09-2026', '2026/09/27',
    '2026-09-27T00:00:00', '2026-09-27Z', ' 2026-09-27', '2026-09-27 ',
    '', 'None', 'yesterday', '2026-02-30',
)

# Zone names no tz database can load: an unknown area, an unknown city, a
# repeated slash, and two keys that are not names at all.
UNLOADABLE_TIMEZONE_NAMES = (
    'Not/AZone', 'Europe/Nowhere', 'Antarctica/Nowhere', 'UTC+1',
    'Europe London', 'Europe/London/extra', '', '../..', '/etc/passwd',
)

# Tokens that must not appear anywhere in an error body: a host, an upstream URL,
# a fixture name, an exception class, a traceback, or a source name. An error body
# carrying any of them is exactly the leak the error vocabulary prevents.
CLIENT_UNSAFE_BODY_TOKENS = (
    'http://', 'https://', 'freesupertips', 'Traceback', 'File "',
    'ZoneInfo', 'ValueError', 'TypeError', 'KeyError', '.html',
    'cloudscraper', 'requests', 'scraper', 'Exception',
)

# The legacy and entitlement keys a v1 response must never carry. ``date`` is the
# one exception: the endpoint's own ``filter.date`` echo reuses that spelling.
FORBIDDEN_RESPONSE_KEYS = frozenset(FORBIDDEN_V1_KEYS - {'date'})


class ErrorBodyAssertions:
    """Shared pins for a client-safe versioned error body.

    One shape, one key set, one content type: an error body is the serializers'
    own ``api_error()`` mapping published at its documented status, with the
    documented message text, and there is no room in it for a host, an upstream
    URL, an exception class, or a traceback.
    """

    def assertApiError(self, response, code, field, status=400):
        """Assert one error body is exactly the documented envelope."""
        self.assertEqual(response.status_code, status)
        self.assertEqual(response['Content-Type'], 'application/json')
        body = response.json()
        self.assertEqual(set(body), {'api_version', 'error'})
        self.assertEqual(body['api_version'], API_VERSION)
        self.assertEqual(set(body['error']), {'code', 'message', 'field'})
        self.assertEqual(body['error']['code'], code)
        self.assertEqual(body['error']['field'], field)
        self.assertEqual(body['error']['message'], ERROR_MESSAGES[code])
        return body


class EndpointTestCase(OfflineGuardMixin, SnapshotProviderTestCase):
    """A fixture-derived snapshot in the seam, with the socket layer disabled.

    The payload comes from the frozen parsers reading the static synthetic
    fixtures — never from a fetch — and is filed with the injected test date
    through ``store_snapshot()``, so the endpoint answers exactly the way a later
    installed provider will make it answer. Every class below also inherits the
    socket guard, so no request in this section can reach a network by accident.
    """

    SOURCE_KEY = 'bet_of_the_day'

    def setUp(self):
        super().setUp()
        self.payload = self.parse_fixture_payload()
        self.seed(self.payload)

    def parse_fixture_payload(self):
        """Return this source's payload, parsed from its static fixture."""
        return utils.parse_bet_of_the_day_page(
            load_fixture(FIXTURE_FOR_SOURCE[self.SOURCE_KEY]),
            TEST_DATE,
        )

    def seed(self, payload, source_key=None):
        """File ``payload`` under ``source_key`` and return its snapshot record."""
        return store_snapshot(
            source_key or self.SOURCE_KEY, payload, fetched_at=FETCHED_AT)

    def use_recording_provider(self):
        """Install a recording provider holding this source's fixture payload."""
        provider = RecordingSnapshotProvider()
        set_snapshot_provider(provider)
        self.addCleanup(set_snapshot_provider, None)
        self.seed(self.payload)
        provider.calls.clear()
        return provider

    def seed_record(self, record):
        """Answer this source with a deliberate raw record, not a stored snapshot."""
        provider = RecordingSnapshotProvider()
        provider.records[self.SOURCE_KEY] = record
        set_snapshot_provider(provider)
        self.addCleanup(set_snapshot_provider, None)
        return provider

    def get(self, **params):
        """Return the endpoint's response for the given query parameters."""
        return self.client.get(ENDPOINT_PATH, params)


class EndpointRoutingTests(OfflineGuardMixin, SimpleTestCase):
    """One namespaced route is added, and every legacy route still resolves."""

    def test_the_route_reverses_to_the_documented_path(self):
        self.assertEqual(reverse(ENDPOINT_ROUTE_NAME), ENDPOINT_PATH)

    def test_the_path_resolves_to_the_view_in_its_own_module(self):
        match = resolve(ENDPOINT_PATH)
        self.assertEqual(match.view_name, ENDPOINT_ROUTE_NAME)
        self.assertIs(match.func, tips_v1)
        self.assertEqual(match.func.__module__, 'alltips_scraper.views_v1')

    def test_the_url_module_publishes_one_namespaced_route(self):
        self.assertEqual(urls_v1.app_name, 'tips_v1')
        self.assertEqual(len(urls_v1.urlpatterns), 1)
        pattern = urls_v1.urlpatterns[0]
        self.assertEqual(pattern.name, 'tips')
        self.assertEqual(str(pattern.pattern), 'tips/')

    def test_the_url_module_imports_only_django_and_the_view(self):
        tree = ast.parse(URLS_V1_PATH.read_text(encoding='utf-8'))
        imports = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.assertEqual(len(imports), 2)
        self.assertEqual(
            {(node.module, alias.name)
             for node in imports for alias in node.names},
            {('django.urls', 'path'), ('views_v1', 'tips_v1')},
        )

    def test_every_legacy_route_still_resolves_to_the_same_name(self):
        for path, name in LEGACY_ROUTES:
            with self.subTest(path=path):
                self.assertEqual(resolve(path).url_name, name)

    def test_the_versioned_route_shadows_no_legacy_route(self):
        legacy_names = [resolve(path).url_name for path, _ in LEGACY_ROUTES]
        self.assertEqual(len(legacy_names), len(set(legacy_names)))
        self.assertNotIn('tips', legacy_names)
        self.assertEqual(resolve(ENDPOINT_PATH).view_name, ENDPOINT_ROUTE_NAME)


class EndpointIsolationTests(EndpointTestCase):
    """The response layer is offline by construction, not by discipline."""

    def read_source(self):
        return VIEWS_V1_PATH.read_text(encoding='utf-8')

    def imported_names(self):
        """Return (import roots, imported names, relative module names)."""
        tree = ast.parse(self.read_source())
        roots = set()
        names = set()
        relative = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split('.')[0] for alias in node.names)
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if node.level:
                    relative.append(module)
                else:
                    roots.add(module.split('.')[0])
                names.update(f'{module}.{alias.name}' for alias in node.names)
        return roots, names, relative

    def test_the_view_imports_only_django_the_stdlib_and_the_frozen_modules(self):
        roots, _names, relative = self.imported_names()
        self.assertEqual(roots, set(VIEWS_V1_ALLOWED_IMPORT_ROOTS))
        self.assertEqual(sorted(relative), ['readmodel_v1', 'serializers_v1'])

    def test_the_view_imports_the_read_half_of_the_seam_and_nothing_else(self):
        _roots, names, _relative = self.imported_names()
        readmodel_names = {
            name.split('.', 1)[1] for name in names
            if name.startswith('readmodel_v1.')
        }
        self.assertEqual(readmodel_names, {'load_snapshot'})

    def test_the_view_source_names_no_scraper_transport_cache_or_configuration(self):
        source = self.read_source().lower()
        for token in VIEWS_V1_FORBIDDEN_SOURCE_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token.lower(), source)
        self.assertIn('load_snapshot', source)

    def test_the_view_reuses_the_frozen_key_vocabulary(self):
        self.assertEqual(LEGACY_SOURCE_KEY, 'source')
        self.assertEqual(SOURCE_DATE_KEY, 'date')
        self.assertNotIn('SOURCE_DATE_KEY =', self.read_source())
        self.assertFalse(hasattr(views_v1, 'SOURCE_LABEL_KEY'))

    def test_the_endpoint_reads_through_the_installed_provider_only(self):
        provider = self.use_recording_provider()
        self.assertEqual(self.get(type=self.SOURCE_KEY).status_code, 200)
        self.assertEqual(provider.calls, [('load', self.SOURCE_KEY)])
        self.assertIs(get_snapshot_provider(), provider)

    def test_the_endpoint_reads_the_snapshot_once_per_request(self):
        provider = self.use_recording_provider()
        self.get(type=self.SOURCE_KEY)
        self.get(
            type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT, timezone='Europe/London')
        self.assertEqual(provider.calls, [('load', self.SOURCE_KEY)] * 2)

    def test_the_endpoint_neither_fetches_nor_parses(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('the endpoint must not fetch or parse')

        with mock.patch.object(utils, 'scrape_one', forbidden), \
                mock.patch.object(utils, 'scrape_all', forbidden), \
                mock.patch.object(utils, 'parse_bet_of_the_day_page', forbidden):
            self.assertEqual(self.get(type=self.SOURCE_KEY).status_code, 200)
            set_snapshot_provider(None)
            self.assertEqual(self.get(type=self.SOURCE_KEY).status_code, 503)

    def test_the_endpoint_answers_with_the_socket_layer_disabled(self):
        with self.assertRaises(AssertionError):
            socket.socket()
        self.assertEqual(self.get(type=self.SOURCE_KEY).status_code, 200)

    def test_a_request_leaves_the_stored_snapshot_untouched(self):
        before = load_snapshot(self.SOURCE_KEY)
        self.get(type=self.SOURCE_KEY)
        self.get(type=self.SOURCE_KEY, date='1999-01-01', timezone='Etc/UTC')
        self.assertEqual(load_snapshot(self.SOURCE_KEY), before)


class QueryValidationOrderTests(ErrorBodyAssertions, EndpointTestCase):
    """The validation order is the contract, and a ``400`` never reads a snapshot.

    No snapshot is installed for these tests, so every answer below is a
    validation result: a ``503`` here would mean the query had been accepted,
    which is exactly the confusion the ordering exists to prevent.
    """

    def setUp(self):
        super().setUp()
        set_snapshot_provider(None)

    def test_a_missing_type_is_refused_whatever_else_is_asked(self):
        queries = (
            {},
            {'date': SOURCE_DATE_TEXT},
            {'timezone': 'Europe/London'},
            {'date': SOURCE_DATE_TEXT, 'timezone': 'Europe/London'},
            {'date': 'yesterday', 'timezone': 'Not/AZone'},
        )
        for params in queries:
            with self.subTest(params=params):
                self.assertApiError(
                    self.client.get(ENDPOINT_PATH, params),
                    ERROR_MISSING_TIP_TYPE,
                    'type',
                )

    def test_an_empty_type_is_unknown_rather_than_missing(self):
        self.assertApiError(self.get(type=''), ERROR_UNKNOWN_TIP_TYPE, 'type')

    def test_an_unknown_type_is_refused_before_the_date_is_looked_at(self):
        for value in ('not-a-source', 'Bet_of_the_day', 'bet-of-the-day',
                      'bet of the day', 'BET_OF_THE_DAY', 'bet_of_the_day ',
                      'daily_accumulators'):
            with self.subTest(value=value):
                self.assertApiError(
                    self.get(
                        type=value, date='yesterday', timezone='Not/AZone'),
                    ERROR_UNKNOWN_TIP_TYPE,
                    'type',
                )

    def test_a_date_that_is_not_strictly_spelled_is_refused(self):
        for value in UNSTRICT_DATE_TEXTS:
            with self.subTest(value=value):
                self.assertApiError(
                    self.get(
                        type=self.SOURCE_KEY, date=value,
                        timezone='Europe/London'),
                    ERROR_INVALID_DATE,
                    'date',
                )

    def test_an_invalid_date_is_refused_before_the_timezone_is_validated(self):
        self.assertApiError(
            self.get(
                type=self.SOURCE_KEY, date='yesterday', timezone='Not/AZone'),
            ERROR_INVALID_DATE,
            'date',
        )

    def test_an_invalid_date_is_refused_before_the_timezone_is_required(self):
        self.assertApiError(
            self.get(type=self.SOURCE_KEY, date='2026-13-01'),
            ERROR_INVALID_DATE,
            'date',
        )

    def test_a_timezone_without_a_date_is_refused(self):
        for value in ('Europe/London', 'UTC', 'Not/AZone', ''):
            with self.subTest(value=value):
                self.assertApiError(
                    self.get(type=self.SOURCE_KEY, timezone=value),
                    ERROR_TIMEZONE_REQUIRES_DATE,
                    'timezone',
                )

    def test_a_date_without_a_timezone_is_refused(self):
        self.assertApiError(
            self.get(type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT),
            ERROR_MISSING_TIMEZONE,
            'timezone',
        )

    def test_a_timezone_no_database_can_load_is_refused(self):
        for value in UNLOADABLE_TIMEZONE_NAMES:
            with self.subTest(value=value):
                self.assertApiError(
                    self.get(
                        type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT,
                        timezone=value),
                    ERROR_INVALID_TIMEZONE,
                    'timezone',
                )

    def test_a_loadable_timezone_is_accepted_and_then_finds_no_snapshot(self):
        for value in ('Europe/London', 'UTC', 'Etc/UTC', 'Pacific/Kiritimati',
                      'America/Argentina/Buenos_Aires'):
            with self.subTest(value=value):
                self.assertEqual(
                    self.get(
                        type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT,
                        timezone=value).status_code,
                    503,
                )

    def test_the_whole_query_is_validated_before_the_snapshot_is_read(self):
        with mock.patch.object(views_v1, 'load_snapshot') as load:
            for params in (
                {},
                {'type': ''},
                {'type': 'nope'},
                {'type': self.SOURCE_KEY, 'date': 'yesterday',
                 'timezone': 'Europe/London'},
                {'type': self.SOURCE_KEY, 'timezone': 'Europe/London'},
                {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT},
                {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT,
                 'timezone': 'Not/AZone'},
            ):
                with self.subTest(params=params):
                    self.client.get(ENDPOINT_PATH, params)
            load.assert_not_called()
            self.client.get(ENDPOINT_PATH, {'type': self.SOURCE_KEY})
            self.assertEqual(load.call_args_list, [mock.call(self.SOURCE_KEY)])

    def test_a_rejected_query_never_reports_availability(self):
        body = self.assertApiError(
            self.get(type=self.SOURCE_KEY, date='yesterday'),
            ERROR_INVALID_DATE,
            'date',
        )
        self.assertNotEqual(body['error']['code'], ERROR_SOURCE_UNAVAILABLE)
        self.assertEqual(self.get(type=self.SOURCE_KEY).status_code, 503)

    def test_a_rejected_query_carries_only_the_error_envelope(self):
        queries = (
            {},
            {'type': ''},
            {'type': 'nope'},
            {'type': self.SOURCE_KEY, 'date': 'yesterday',
             'timezone': 'Europe/London'},
            {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT},
            {'type': self.SOURCE_KEY, 'timezone': 'Europe/London'},
            {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT,
             'timezone': 'Not/AZone'},
        )
        for params in queries:
            with self.subTest(params=params):
                response = self.client.get(ENDPOINT_PATH, params)
                self.assertEqual(response.status_code, 400)
                body = response.json()
                self.assertEqual(set(body), {'api_version', 'error'})
                self.assertEqual(
                    all_keys(body),
                    {'api_version', 'error', 'code', 'message', 'field'},
                )
                raw = response.content.decode()
                for token in CLIENT_UNSAFE_BODY_TOKENS:
                    self.assertNotIn(token, raw)

    def test_a_rejected_query_uses_only_the_documented_error_codes(self):
        seen = set()
        queries = (
            {},
            {'type': ''},
            {'type': 'nope'},
            {'type': self.SOURCE_KEY, 'date': 'yesterday',
             'timezone': 'Europe/London'},
            {'type': self.SOURCE_KEY, 'timezone': 'Europe/London'},
            {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT},
            {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT,
             'timezone': 'Not/AZone'},
        )
        for params in queries:
            with self.subTest(params=params):
                code = self.client.get(
                    ENDPOINT_PATH, params).json()['error']['code']
                seen.add(code)
                self.assertIn(code, ERROR_CODES)
        self.assertEqual(seen, {
            ERROR_MISSING_TIP_TYPE,
            ERROR_UNKNOWN_TIP_TYPE,
            ERROR_INVALID_DATE,
            ERROR_MISSING_TIMEZONE,
            ERROR_TIMEZONE_REQUIRES_DATE,
            ERROR_INVALID_TIMEZONE,
        })


class SourceUnavailableTests(ErrorBodyAssertions, EndpointTestCase):
    """A valid query with no usable snapshot is a client-safe ``503``, nothing else."""

    def test_a_valid_query_without_a_snapshot_is_unavailable(self):
        set_snapshot_provider(None)
        self.assertApiError(
            self.get(type=self.SOURCE_KEY),
            ERROR_SOURCE_UNAVAILABLE,
            None,
            status=503,
        )

    def test_a_valid_filtered_query_without_a_snapshot_is_unavailable(self):
        set_snapshot_provider(None)
        for params in (
            {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT,
             'timezone': 'Etc/UTC'},
            {'type': self.SOURCE_KEY, 'date': '1999-01-01',
             'timezone': 'Etc/UTC'},
        ):
            with self.subTest(params=params):
                self.assertApiError(
                    self.client.get(ENDPOINT_PATH, params),
                    ERROR_SOURCE_UNAVAILABLE,
                    None,
                    status=503,
                )

    def test_an_installed_snapshot_turns_the_same_query_into_a_200(self):
        params = {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT,
                  'timezone': 'Etc/UTC'}
        set_snapshot_provider(None)
        self.assertEqual(self.client.get(ENDPOINT_PATH, params).status_code, 503)
        self.seed(self.payload)
        self.assertEqual(self.client.get(ENDPOINT_PATH, params).status_code, 200)

    def test_a_failed_read_stores_nothing_and_asks_only_once(self):
        provider = RecordingSnapshotProvider()
        set_snapshot_provider(provider)
        self.addCleanup(set_snapshot_provider, None)
        self.assertEqual(self.get(type=self.SOURCE_KEY).status_code, 503)
        self.assertEqual(provider.calls, [('load', self.SOURCE_KEY)])
        self.assertEqual(provider.records, {})
        self.assertIsNone(load_snapshot(self.SOURCE_KEY))

    def test_a_record_that_is_not_a_mapping_is_the_same_answer(self):
        for record in ('not a record', ['not', 'a', 'record'], 42, b'bytes'):
            with self.subTest(record=record):
                self.seed_record(record)
                self.assertApiError(
                    self.get(type=self.SOURCE_KEY),
                    ERROR_SOURCE_UNAVAILABLE,
                    None,
                    status=503,
                )

    def test_the_unavailable_path_logs_a_safe_reason(self):
        set_snapshot_provider(None)
        with self.assertLogs('alltips_scraper.views_v1', level='WARNING') as logs:
            self.get(
                type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT, timezone='Etc/UTC')
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.levelname, 'WARNING')
        message = record.getMessage()
        self.assertIn(self.SOURCE_KEY, message)
        self.assertIn('date_filtered=True', message)
        self.assertIn('record_present=False', message)
        for token in CLIENT_UNSAFE_BODY_TOKENS:
            self.assertNotIn(token, message)

    def test_an_unavailable_body_leaks_no_reason_or_internal_detail(self):
        set_snapshot_provider(None)
        for params in ({'type': self.SOURCE_KEY},
                       {'type': self.SOURCE_KEY, 'date': SOURCE_DATE_TEXT,
                        'timezone': 'Etc/UTC'}):
            with self.subTest(params=params):
                response = self.client.get(ENDPOINT_PATH, params)
                self.assertEqual(response.status_code, 503)
                raw = response.content.decode()
                for token in CLIENT_UNSAFE_BODY_TOKENS:
                    self.assertNotIn(token, raw)
                self.assertEqual(
                    all_keys(response.json()),
                    {'api_version', 'error', 'code', 'message', 'field'},
                )

    def test_a_provider_failure_stays_the_providers_own_failure(self):
        class ExplodingProvider:
            """A provider whose read fails: the endpoint must not hide it."""

            def load(self, type_key):
                raise RuntimeError('the provider itself failed')

            def store(self, type_key, payload, *, fetched_at=None):
                return None

            def clear(self):
                pass

        set_snapshot_provider(ExplodingProvider())
        with self.assertRaises(RuntimeError):
            self.get(type=self.SOURCE_KEY)


class BetOfTheDayEndpointEnvelopeTests(
        KickoffMarkerAssertions, EndpointTestCase):
    """The published ``match``-unit envelope, its ``filter`` block, and its empties."""

    def test_the_full_envelope_is_the_documented_shape(self):
        body = self.get(type=self.SOURCE_KEY).json()
        self.assertEqual(
            set(body), set(SUCCESS_ENVELOPE_KEYS) | set(OPTIONAL_ENVELOPE_KEYS))
        self.assertEqual(body['api_version'], API_VERSION)
        self.assertEqual(body['type'], self.SOURCE_KEY)
        self.assertEqual(body['unit'], UNIT_MATCH)
        self.assertEqual(body['count'], 3)
        self.assertEqual(body['legs_count'], 0)
        self.assertEqual(len(body['tips']), 3)
        self.assertEqual(body['source']['label'], 'freesupertips')
        self.assertEqual(body['source']['date_text'], SOURCE_DATE_TEXT)
        self.assertEqual(body['source']['fetched_at'], FETCHED_AT_Z)

    def test_every_published_tip_is_a_mapped_match(self):
        body = self.get(type=self.SOURCE_KEY).json()
        for tip in body['tips']:
            with self.subTest(match_title=tip['match_title']):
                self.assertEqual(set(tip), set(MATCH_UNIT_TIP_KEYS))
                self.assertKickoffMarkers(tip)
                self.assertTrue(tip['match_title'])
                self.assertEqual(tip['source_date_text'], SOURCE_DATE_TEXT)

    def test_an_unfiltered_request_decides_nothing(self):
        body = self.get(type=self.SOURCE_KEY).json()
        self.assertEqual(tuple(body['filter']), FILTER_KEYS)
        self.assertEqual(body['filter'], {
            'date': None,
            'timezone': None,
            'applied': False,
            'matched': None,
            'available_date': SOURCE_DATE_TEXT,
        })

    def test_an_applied_filter_echoes_the_request_and_matches(self):
        body = self.get(
            type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT,
            timezone='Etc/UTC').json()
        self.assertEqual(tuple(body['filter']), FILTER_KEYS)
        self.assertEqual(body['filter'], {
            'date': SOURCE_DATE_TEXT,
            'timezone': 'Etc/UTC',
            'applied': True,
            'matched': True,
            'available_date': SOURCE_DATE_TEXT,
        })

    def test_a_matching_filter_changes_nothing_but_the_filter_block(self):
        plain = self.get(type=self.SOURCE_KEY).json()
        filtered = self.get(
            type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT,
            timezone='Europe/London').json()
        self.assertEqual(filtered['filter']['timezone'], 'Europe/London')
        self.assertEqual(filtered['filter']['matched'], True)
        self.assertEqual(
            {key: value for key, value in filtered.items() if key != 'filter'},
            {key: value for key, value in plain.items() if key != 'filter'},
        )

    def test_a_non_matching_date_is_an_empty_envelope_not_an_error(self):
        response = self.get(
            type=self.SOURCE_KEY, date='1999-01-01', timezone='Etc/UTC')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body), set(SUCCESS_ENVELOPE_KEYS) | set(OPTIONAL_ENVELOPE_KEYS))
        self.assertEqual(body['tips'], [])
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['legs_count'], 0)
        self.assertEqual(body['filter'], {
            'date': '1999-01-01',
            'timezone': 'Etc/UTC',
            'applied': True,
            'matched': False,
            'available_date': SOURCE_DATE_TEXT,
        })

    def test_an_empty_envelope_keeps_the_provenance_and_the_unit(self):
        empty = self.get(
            type=self.SOURCE_KEY, date='1999-01-01', timezone='Etc/UTC').json()
        full = self.get(type=self.SOURCE_KEY).json()
        for key in ('api_version', 'type', 'unit', 'source'):
            with self.subTest(key=key):
                self.assertEqual(empty[key], full[key])

    def test_the_timezone_is_echoed_and_never_applied(self):
        unfiltered = self.get(type=self.SOURCE_KEY).json()['tips']
        body = self.get(
            type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT,
            timezone='Pacific/Kiritimati').json()
        self.assertEqual(body['filter']['timezone'], 'Pacific/Kiritimati')
        self.assertEqual(body['filter']['matched'], True)
        self.assertEqual(body['tips'], unfiltered)

    def test_a_snapshot_date_the_payload_does_not_state_is_unusable(self):
        self.seed(dict(self.payload, **{SOURCE_DATE_KEY: '27 September 2026'}))
        unfiltered = self.get(type=self.SOURCE_KEY).json()
        self.assertEqual(unfiltered['count'], 3)
        self.assertIsNone(unfiltered['filter']['available_date'])
        self.assertEqual(unfiltered['source']['date_text'], '27 September 2026')
        filtered = self.get(
            type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT,
            timezone='Etc/UTC').json()
        self.assertEqual(filtered['filter']['applied'], True)
        self.assertEqual(filtered['filter']['matched'], False)
        self.assertIsNone(filtered['filter']['available_date'])
        self.assertEqual(filtered['count'], 0)
        self.assertEqual(filtered['source']['date_text'], '27 September 2026')

    def test_a_missing_or_unstrict_snapshot_date_is_unusable(self):
        for value in (None, '', '20260927', '2026-W39-1', 20260927, [], {}):
            with self.subTest(value=value):
                payload = dict(self.payload)
                if value is None:
                    payload.pop(SOURCE_DATE_KEY, None)
                else:
                    payload[SOURCE_DATE_KEY] = value
                self.seed(payload)
                body = self.get(type=self.SOURCE_KEY).json()
                self.assertIsNone(body['filter']['available_date'])
                self.assertEqual(body['count'], 3)

    def test_no_nested_date_is_ever_used_as_the_snapshot_date(self):
        payload = dict(self.payload)
        payload.pop(SOURCE_DATE_KEY, None)
        for match in payload['matches']:
            match[SOURCE_DATE_KEY] = SOURCE_DATE_TEXT
        self.seed(payload)
        body = self.get(type=self.SOURCE_KEY).json()
        self.assertIsNone(body['filter']['available_date'])
        self.assertIsNone(body['source']['date_text'])
        self.assertEqual(body['tips'][0]['source_date_text'], SOURCE_DATE_TEXT)

    def test_the_response_never_carries_a_legacy_or_entitlement_key(self):
        body = self.get(
            type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT,
            timezone='Etc/UTC').json()
        self.assertEqual(
            all_keys(body) & FORBIDDEN_RESPONSE_KEYS, set(), 'forbidden key')
        self.assertEqual(
            set(body['source']), {'label', 'date_text', 'fetched_at'})

    def test_a_record_without_a_payload_is_an_empty_but_valid_envelope(self):
        self.seed_record({
            'schema_version': 1,
            'type_key': self.SOURCE_KEY,
            'fetched_at': FETCHED_AT,
        })
        response = self.get(type=self.SOURCE_KEY)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['tips'], [])
        self.assertIsNone(body['source']['label'])
        self.assertIsNone(body['source']['date_text'])
        self.assertEqual(body['source']['fetched_at'], FETCHED_AT_Z)

    def test_a_naive_or_missing_fetch_stamp_is_simply_omitted(self):
        for fetched_at in (NAIVE_FETCHED_AT, '2026-09-28T17:12:03Z', None, 0):
            with self.subTest(fetched_at=fetched_at):
                self.seed_record({
                    'schema_version': 1,
                    'type_key': self.SOURCE_KEY,
                    'payload': self.payload,
                    'fetched_at': fetched_at,
                })
                body = self.get(type=self.SOURCE_KEY).json()
                self.assertEqual(set(body['source']), {'label', 'date_text'})
                self.assertEqual(body['count'], 3)


class DailyAccumulatorEndpointEnvelopeTests(
        KickoffMarkerAssertions, EndpointTestCase):
    """The same endpoint on the ``card`` unit: nested legs, one contract."""

    SOURCE_KEY = 'daily_accumulator'

    def parse_fixture_payload(self):
        return utils.parse_accumulator_page(
            load_fixture(FIXTURE_FOR_SOURCE[self.SOURCE_KEY]),
            TEST_DATE,
        )

    def test_the_card_unit_publishes_cards_with_nested_legs(self):
        body = self.get(type=self.SOURCE_KEY).json()
        self.assertEqual(body['unit'], UNIT_CARD)
        self.assertEqual(body['count'], len(self.payload['accumulators']))
        self.assertEqual(
            body['legs_count'],
            sum(len(card['matches']) for card in self.payload['accumulators']),
        )
        self.assertTrue(body['tips'])
        for card in body['tips']:
            with self.subTest(category=card['category']):
                self.assertEqual(set(card), set(CARD_UNIT_TIP_KEYS))
                self.assertEqual(card['legs_count'], len(card['legs']))
                self.assertNotIn('tip_type', card)
                for leg in card['legs']:
                    self.assertEqual(set(leg), set(LEG_KEYS))
                    self.assertKickoffMarkers(leg)

    def test_the_card_unit_filter_behaves_like_the_match_unit(self):
        full = self.get(
            type=self.SOURCE_KEY, date=SOURCE_DATE_TEXT,
            timezone='Etc/UTC').json()
        self.assertEqual(full['filter']['matched'], True)
        self.assertEqual(full['count'], len(self.payload['accumulators']))
        empty = self.get(
            type=self.SOURCE_KEY, date='1999-01-01',
            timezone='Etc/UTC').json()
        self.assertEqual(empty['filter']['matched'], False)
        self.assertEqual(empty['count'], 0)
        self.assertEqual(empty['legs_count'], 0)
        self.assertEqual(empty['tips'], [])


class GenericSourceEndpointEnvelopeTests(EndpointTestCase):
    """Every generic source answers on the same endpoint, keyed by its own type."""

    def parse_source_payload(self, source):
        return utils.parse_generic_tips_page(
            load_fixture(FIXTURE_FOR_SOURCE[source]),
            GENERIC_TIP_TYPES[source],
            TEST_DATE,
        )

    def test_every_generic_source_publishes_its_own_type_and_cards(self):
        for source in sorted(GENERIC_TIP_TYPES):
            with self.subTest(source=source):
                payload = self.parse_source_payload(source)
                self.seed(payload, source)
                body = self.get(type=source).json()
                self.assertEqual(body['type'], source)
                self.assertEqual(body['unit'], UNIT_CARD)
                self.assertEqual(body['count'], len(payload['accumulators']))
                self.assertEqual(
                    {card['tip_type'] for card in body['tips']},
                    {GENERIC_TIP_TYPES[source]},
                )

    def test_every_generic_source_filters_by_its_own_payload_date(self):
        for source in sorted(GENERIC_TIP_TYPES):
            with self.subTest(source=source):
                payload = self.parse_source_payload(source)
                self.seed(payload, source)
                matched = self.get(
                    type=source, date=SOURCE_DATE_TEXT,
                    timezone='Etc/UTC').json()
                self.assertEqual(matched['filter']['matched'], True)
                self.assertEqual(matched['count'], len(payload['accumulators']))
                empty = self.get(
                    type=source, date='1999-01-01',
                    timezone='Etc/UTC').json()
                self.assertEqual(empty['filter']['matched'], False)
                self.assertEqual(empty['count'], 0)
                self.assertEqual(empty['source']['date_text'], SOURCE_DATE_TEXT)

    def test_the_page_tip_type_is_not_a_request_key(self):
        """``type`` is the source key, not the payload's own ``tip_type`` text.

        Two sources happen to spell both the same way (``btts_and_win`` and
        ``anytime_goalscorer``), so only the sources whose two spellings differ
        can pin this: for those, the page's ``tip_type`` is an unknown type even
        though a snapshot for that page is installed.
        """
        for source, page_tip_type in sorted(GENERIC_TIP_TYPES.items()):
            if page_tip_type in SUPPORTED_TIP_TYPES:
                continue
            with self.subTest(page_tip_type=page_tip_type):
                self.seed(self.parse_source_payload(source), source)
                response = self.get(
                    type=page_tip_type, date=SOURCE_DATE_TEXT,
                    timezone='Etc/UTC')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()['error']['code'], ERROR_UNKNOWN_TIP_TYPE)


class UnknownAndDuplicateParameterTests(EndpointTestCase):
    """Three query keys, and only three: anything else is ignored, not guessed."""

    def test_an_unknown_query_parameter_is_ignored(self):
        plain = self.get(type=self.SOURCE_KEY).json()
        for params in (
            {'type': self.SOURCE_KEY, 'limit': '10'},
            {'type': self.SOURCE_KEY, 'page': '2', 'offset': '0'},
            {'type': self.SOURCE_KEY, 'debug': '1', 'api_key': 'x'},
            {'type': self.SOURCE_KEY, 'filter': 'applied=true', 'unit': 'card'},
            {'type': self.SOURCE_KEY, 'source': 'other', 'sort': 'odds'},
        ):
            with self.subTest(params=params):
                self.assertEqual(
                    self.client.get(ENDPOINT_PATH, params).json(), plain)

    def test_an_unknown_parameter_cannot_change_the_filter_or_the_unit(self):
        body = self.get(
            type=self.SOURCE_KEY, filter='x', source='y', unit='card').json()
        self.assertEqual(body['unit'], UNIT_MATCH)
        self.assertEqual(body['filter'], {
            'date': None,
            'timezone': None,
            'applied': False,
            'matched': None,
            'available_date': SOURCE_DATE_TEXT,
        })

    def test_no_pagination_entitlement_or_legacy_key_is_invented(self):
        body = self.get(type=self.SOURCE_KEY, limit='1').json()
        self.assertEqual(all_keys(body) & FORBIDDEN_RESPONSE_KEYS, set())
        self.assertEqual(
            set(body), set(SUCCESS_ENVELOPE_KEYS) | set(OPTIONAL_ENVELOPE_KEYS))

    def test_a_duplicated_type_parameter_uses_the_final_value(self):
        url = f'{ENDPOINT_PATH}?type=not-a-source&type={self.SOURCE_KEY}'
        body = self.client.get(url).json()
        self.assertEqual(body['type'], self.SOURCE_KEY)
        self.assertEqual(body['count'], 3)

    def test_a_duplicated_date_parameter_uses_the_final_value(self):
        url = (
            f'{ENDPOINT_PATH}?type={self.SOURCE_KEY}&timezone=Etc/UTC'
            f'&date=1999-01-01&date={SOURCE_DATE_TEXT}'
        )
        body = self.client.get(url).json()
        self.assertEqual(body['filter']['date'], SOURCE_DATE_TEXT)
        self.assertEqual(body['filter']['matched'], True)
        self.assertEqual(body['count'], 3)

    def test_a_duplicated_timezone_parameter_uses_the_final_value(self):
        url = (
            f'{ENDPOINT_PATH}?type={self.SOURCE_KEY}&date={SOURCE_DATE_TEXT}'
            f'&timezone=Not/AZone&timezone=Europe/London'
        )
        body = self.client.get(url).json()
        self.assertEqual(body['filter']['timezone'], 'Europe/London')
        self.assertEqual(body['filter']['matched'], True)

    def test_a_duplicated_parameter_that_stays_invalid_is_still_refused(self):
        url = (
            f'{ENDPOINT_PATH}?type={self.SOURCE_KEY}'
            f'&date=1999-01-01&date=yesterday'
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], ERROR_INVALID_DATE)


class MethodRestrictionTests(EndpointTestCase):
    """Only ``GET`` is allowed, and that refusal is not a versioned body."""

    def test_every_method_other_than_get_is_refused_with_405(self):
        for method in ('post', 'put', 'patch', 'delete', 'head'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    ENDPOINT_PATH, {'type': self.SOURCE_KEY})
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.headers['Allow'], 'GET')

    def test_a_405_carries_no_versioned_error_body(self):
        response = self.client.post(ENDPOINT_PATH, {'type': self.SOURCE_KEY})
        self.assertEqual(response.content, b'')
        self.assertNotIn('api_version', response.content.decode())


class EndpointVocabularyTests(OfflineGuardMixin, SimpleTestCase):
    """The endpoint's own vocabulary: three query keys and one legacy source key."""

    def test_the_query_surface_is_exactly_three_parameters(self):
        self.assertEqual(QUERY_PARAMETERS, ('type', 'date', 'timezone'))
        self.assertEqual(
            set(QUERY_PARAMETERS),
            {
                views_v1.QUERY_TIP_TYPE,
                views_v1.QUERY_DATE,
                views_v1.QUERY_TIMEZONE,
            },
        )

    def test_the_filter_block_names_the_documented_keys_in_order(self):
        self.assertEqual(
            FILTER_KEYS,
            ('date', 'timezone', 'applied', 'matched', 'available_date'))
        self.assertEqual(len(FILTER_KEYS), 5)

    def test_the_legacy_source_key_keeps_its_legacy_name(self):
        self.assertEqual(LEGACY_SOURCE_KEY, 'source')
        self.assertEqual(SOURCE_DATE_KEY, 'date')
        self.assertNotEqual(LEGACY_SOURCE_KEY, 'label')

    def test_the_endpoint_uses_only_the_documented_error_codes(self):
        codes = (
            ERROR_MISSING_TIP_TYPE,
            ERROR_UNKNOWN_TIP_TYPE,
            ERROR_INVALID_DATE,
            ERROR_TIMEZONE_REQUIRES_DATE,
            ERROR_MISSING_TIMEZONE,
            ERROR_INVALID_TIMEZONE,
            ERROR_SOURCE_UNAVAILABLE,
        )
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(set(codes), set(ERROR_CODES))
        for code in codes:
            with self.subTest(code=code):
                self.assertEqual(
                    set(api_error(code, field='type')), {'api_version', 'error'})
