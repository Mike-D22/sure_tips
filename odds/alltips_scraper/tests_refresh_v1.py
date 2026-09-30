"""Tests for the v1 snapshot writer and the refresh command that drives it.

``refresh_v1`` is the only code that writes a stored snapshot, and a stored
snapshot is the only thing ``/api/v1/tips/`` answers from: what a run stores is
exactly what clients are served until the next run. Each of the writer's
decisions is therefore worth pinning — which key it accepts, which payload it is
willing to publish, whether the instant its own fetch was accepted reaches the
row, what it does with a source that fails, and what it prints when it refuses.

Ground rules
------------
* No network. Every class runs with the socket layer disabled through
  ``OfflineGuardMixin``, and the fetch layer is a double wherever a test would
  otherwise fetch.
* Only the module's published surface is asserted: its constants, the entry
  points ``sanitize_payload()``, ``resolve_type_keys()``, ``refresh_type()`` and
  ``refresh_types()``, the report lines, and the management command. No private
  helper is called by name.
* Variants of one rule are one test method driven by a table and ``subTest()``, so
  a new malformed field or a new exception kind is a new row rather than a method.
* Every refusal is asserted by its reason token, and every report line by its
  whole text, so a renamed reason or an added field in a line fails here instead
  of reaching a scheduler.
* Nothing here reads the clock. The instant a row is stamped with is always handed
  in, and the one clock the module reads for itself — ``refresh_v1._now()``, which
  a run reaches once per accepted type — is the one thing stood in for.
* A stored row is asserted through the model's columns, through the seam, and
  through the endpoint: the durable class is the only one here that proves what a
  client is served after a run.
"""

from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest import mock

from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase

from . import refresh_v1, storage_v1, utils
from .management.commands import refresh_tips
from .models import SnapshotV1
from .readmodel_v1 import (
    SNAPSHOT_SCHEMA_VERSION,
    get_snapshot_provider,
    load_snapshot,
    set_snapshot_provider,
)
from .serializers_v1 import (
    SUCCESS_ENVELOPE_KEYS,
    TIP_TYPE_UNITS,
    UNIT_CARD,
    UNIT_MATCH,
    UnknownTipType,
    format_utc_z,
    serialize_tips,
)
from .storage_v1 import DatabaseSnapshotProvider, canonical_payload_sha256
from .tests_parser_contract import OfflineGuardMixin

# ---------------------------------------------------------------------------
# Fixed inputs. Nothing below reads a clock, a fixture, or a source.
# ---------------------------------------------------------------------------

# The one public URL a stored snapshot is served from.
ENDPOINT_PATH = '/api/v1/tips/'

SOURCE_DATE_TEXT = '2026-09-27'
SOURCE_LABEL = 'freesupertips'
MATCH_PATH = '/betting-tips/arsenal-vs-chelsea/'

FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3, tzinfo=timezone.utc)
FETCHED_AT_Z = '2026-09-28T17:12:03Z'
LATER_FETCHED_AT = datetime(2026, 9, 28, 18, 30, 0, tzinfo=timezone.utc)
LATER_FETCHED_AT_Z = '2026-09-28T18:30:00Z'

# The fetch layer's own bookkeeping, stamped on every result it hands over. The
# writer strips both from the copy it digests, compares and stores.
SCRAPED_AT_TEXT = '2026-09-28T17:12:01.500000+00:00'
SOURCE_URL = 'https://example.invalid/betting-tips/'

# A text that must never reach a log line, a report line, or a response body. It
# stands in for anything an exception, a URL, or a payload value could carry.
HOSTILE_TEXT = 'hostile-text-from-a-source'


def leg(match_title='Arsenal vs Chelsea'):
    """Return one source selection, with the keys the frozen parsers produce."""
    return {
        'date': SOURCE_DATE_TEXT,
        'time': '15:00',
        'match_title': match_title,
        'teams': ['Arsenal', 'Chelsea'],
        'prediction': 'Arsenal to win',
        'opponent_text': 'vs Chelsea',
        'tip_reason': 'Reason for ' + match_title,
        'match_url': MATCH_PATH,
    }


def match_payload(title='Arsenal vs Chelsea'):
    """Return a payload shaped like the frozen bet-of-the-day envelope."""
    return {
        'date': SOURCE_DATE_TEXT,
        'total_tips': 1,
        'total_cards': 1,
        'matches': [leg(title)],
        'count': 1,
        'source': SOURCE_LABEL,
    }


def card_payload(legs=2):
    """Return a payload shaped like a frozen accumulator envelope.

    One card holds ``legs`` legs, and the payload states its own totals the way the
    frozen parser does: ``total_accumulators`` is how many cards there are and
    ``count`` is the legs all of them state.
    """
    return {
        'date': SOURCE_DATE_TEXT,
        'total_accumulators': 1,
        'accumulators': [
            {
                'category': 'Accumulator',
                'stake': 10.0,
                'returns': 26.5,
                'total_odds': 2.65,
                'matches': [
                    leg('Match %d vs Rival %d' % (number, number))
                    for number in range(1, legs + 1)
                ],
                'matches_count': legs,
            },
        ],
        'count': legs,
        'source': SOURCE_LABEL,
    }


def payload_for(tip_type, *, legs=2):
    """Return a payload whose shape matches the unit the registry gives a type."""
    if TIP_TYPE_UNITS[tip_type] == UNIT_MATCH:
        return match_payload()
    return card_payload(legs)


def published_counts(tip_type, *, legs=2):
    """Return the ``(count, legs)`` a filled payload reports for a tip type.

    ``legs`` is ``None`` for a ``match`` unit, which keeps ``legs=`` out of its
    report line, and the total its cards state for a ``card`` unit.
    """
    if TIP_TYPE_UNITS[tip_type] == UNIT_MATCH:
        return 1, None
    return 1, legs


def instants(count):
    """Return ``count`` distinct instants, one second apart and in order."""
    return [FETCHED_AT + timedelta(seconds=number) for number in range(count)]


def pinned_empty_payload(tip_type):
    """Return the pinned empty envelope a type's own frozen parser produces."""
    return dict(refresh_v1.EMPTY_ENVELOPES[tip_type])


def unpinned_empty_payload(tip_type='bet_of_the_day'):
    """Return a believable empty payload that is not a type's pinned envelope.

    A card-free page reads exactly like this, and this writer must refuse it rather
    than publish a row that would answer a reader with no tips at all.
    """
    collection = (
        'matches' if TIP_TYPE_UNITS[tip_type] == UNIT_MATCH else 'accumulators')
    return {
        'date': SOURCE_DATE_TEXT,
        collection: [],
        'count': 0,
        'source': SOURCE_LABEL,
    }


def with_volatile_keys(payload):
    """Return a payload carrying the fetch layer's own bookkeeping as an extra."""
    return dict(payload, scraped_at=SCRAPED_AT_TEXT, source_url=SOURCE_URL)


class UnhashableKey:
    """A key that cannot be asked about at all.

    A value whose own ``__hash__`` raises something other than ``TypeError`` meets the
    registry predicate's membership test with its own exception rather than with an
    answer, so a selection containing this must be refused by the writer's own string
    check, before the registry is asked anything.
    """

    def __hash__(self):
        raise ValueError('a key this registry cannot be asked about')


def row_values(type_key):
    """Return every stored column of one row, as one comparable tuple."""
    row = SnapshotV1.objects.get(pk=type_key)
    return (
        row.type_key,
        row.schema_version,
        row.payload,
        row.payload_sha256,
        row.fetched_at,
    )


# ---------------------------------------------------------------------------
# The published vocabulary and the report lines
# ---------------------------------------------------------------------------

class RefreshVocabularyTests(SimpleTestCase):
    """The tokens, the templates and the line renderer, pinned as published."""

    def test_the_refresh_order_is_the_registry_order_without_repeats(self):
        order = refresh_v1.REFRESH_TYPE_ORDER

        self.assertEqual(order, tuple(TIP_TYPE_UNITS))
        self.assertEqual(set(order), set(TIP_TYPE_UNITS))
        self.assertEqual(len(order), 6)
        self.assertEqual(len(set(order)), len(order))

    def test_the_tokens_are_the_published_ones(self):
        self.assertEqual(
            refresh_v1.REASON_TOKENS,
            frozenset({
                'fetch_failed', 'fetch_exception', 'unknown_source_key',
                'unrecognized_error_envelope', 'non_dict_result',
                'empty_success_envelope', 'malformed_success_envelope',
                'serializer_rejected', 'state_read_failed', 'store_rejected',
                'store_failed',
            }),
        )
        self.assertEqual(
            (refresh_v1.STATE_NEW, refresh_v1.STATE_UNCHANGED,
             refresh_v1.STATE_CHANGED),
            ('new', 'unchanged', 'changed'),
        )
        self.assertEqual(
            refresh_v1.STATE_TOKENS,
            (refresh_v1.STATE_NEW, refresh_v1.STATE_UNCHANGED,
             refresh_v1.STATE_CHANGED),
        )
        self.assertEqual(
            (refresh_v1.OUTCOME_OK, refresh_v1.OUTCOME_EMPTY,
             refresh_v1.OUTCOME_FAILED),
            ('ok', 'empty', 'failed'),
        )
        self.assertEqual(
            refresh_v1.OUTCOME_TOKENS,
            (refresh_v1.OUTCOME_OK, refresh_v1.OUTCOME_EMPTY,
             refresh_v1.OUTCOME_FAILED),
        )
        self.assertEqual(refresh_v1.DRY_RUN_PREFIX, 'dry-run:')
        self.assertEqual(
            refresh_v1.DRY_RUN_STATE_TOKENS,
            tuple(refresh_v1.DRY_RUN_PREFIX + state
                  for state in refresh_v1.STATE_TOKENS),
        )
        for token in refresh_v1.DRY_RUN_STATE_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, refresh_v1.STATE_TOKENS)
        self.assertEqual(refresh_v1.VOLATILE_RESULT_KEYS, ('scraped_at', 'source_url'))
        self.assertEqual(refresh_v1.MAX_TYPE_KEY_LENGTH, 64)
        self.assertEqual(refresh_v1.LOGGER_NAME, 'alltips_scraper.refresh_v1')
        self.assertEqual(refresh_v1.logger.name, refresh_v1.LOGGER_NAME)

    def test_the_report_templates_are_the_published_lines(self):
        self.assertEqual(
            refresh_v1.SUCCESS_LINE,
            'type={type} outcome={outcome} state={state} count={count}')
        self.assertEqual(
            refresh_v1.FAILURE_LINE,
            'type={type} outcome=failed reason={reason}')
        self.assertEqual(refresh_v1.LEGS_FIELD, ' legs={legs}')
        self.assertEqual(refresh_v1.STAMP_FIELD, ' fetched_at={fetched_at}')
        # A refusal states no state, no counts and no instant, and an accepted
        # line states no reason: each template carries only its own fields.
        for field in ('{state}', '{count}', '{legs}', '{fetched_at}'):
            with self.subTest(field=field):
                self.assertNotIn(field, refresh_v1.FAILURE_LINE)
        self.assertNotIn(
            '{reason}',
            refresh_v1.SUCCESS_LINE + refresh_v1.LEGS_FIELD
            + refresh_v1.STAMP_FIELD,
        )

    def test_the_result_field_names_are_the_published_ones(self):
        self.assertEqual(refresh_v1.ERROR_KEY, 'error')
        self.assertEqual(refresh_v1.STATUS_CODE_KEY, 'status_code')
        self.assertEqual(
            refresh_v1.FETCH_FAILURE_PREFIXES,
            ('Failed to fetch ', 'Exception fetching '),
        )
        self.assertEqual(refresh_v1.UNKNOWN_KEY_PREFIX, 'Unknown scraper key:')
        self.assertEqual(refresh_v1.MATCHES_COUNT_KEY, 'matches_count')
        self.assertEqual(refresh_v1.TOTAL_TIPS_KEY, 'total_tips')
        self.assertEqual(refresh_v1.TOTAL_ACCUMULATORS_KEY, 'total_accumulators')
        self.assertEqual(
            dict(refresh_v1.UNIT_COLLECTIONS),
            {UNIT_MATCH: 'matches', UNIT_CARD: 'accumulators'},
        )

    def test_every_type_pins_the_empty_envelope_the_endpoint_can_publish(self):
        self.assertEqual(
            set(refresh_v1.EMPTY_ENVELOPES), set(refresh_v1.REFRESH_TYPE_ORDER))

        for tip_type in refresh_v1.REFRESH_TYPE_ORDER:
            with self.subTest(tip_type=tip_type):
                envelope = refresh_v1.EMPTY_ENVELOPES[tip_type]
                collection = (
                    'matches' if TIP_TYPE_UNITS[tip_type] == UNIT_MATCH
                    else 'accumulators')
                self.assertEqual(envelope['count'], 0)
                self.assertEqual(envelope[collection], [])
                self.assertIn(envelope['error'], refresh_v1.NO_TIPS_ERROR_TEXTS)
                # The serializer the endpoint answers with agrees that this is a
                # valid, tip-free result rather than an error.
                published = serialize_tips(tip_type, envelope)
                self.assertEqual(
                    (published['count'], published['legs_count'],
                     published['tips']), (0, 0, []))

    def test_a_pinned_empty_envelope_cannot_be_edited_or_replaced(self):
        for tip_type in refresh_v1.REFRESH_TYPE_ORDER:
            with self.subTest(tip_type=tip_type):
                with self.assertRaises(TypeError):
                    refresh_v1.EMPTY_ENVELOPES[tip_type]['count'] = 1
                with self.assertRaises(TypeError):
                    refresh_v1.EMPTY_ENVELOPES[tip_type] = {}


    def test_every_accepted_outcome_renders_the_documented_line(self):
        self.assertEqual(FETCHED_AT_Z, '2026-09-28T17:12:03Z')
        cases = (
            (
                refresh_v1.RefreshOutcome(
                    'bet_of_the_day', state='new', count=1,
                    fetched_at=FETCHED_AT),
                'type=bet_of_the_day outcome=ok state=new count=1 '
                'fetched_at=%s' % FETCHED_AT_Z,
                None,
            ),
            (
                refresh_v1.RefreshOutcome(
                    'daily_accumulator', state='unchanged', count=2, legs=4,
                    fetched_at=FETCHED_AT),
                'type=daily_accumulator outcome=ok state=unchanged count=2 '
                'legs=4 fetched_at=%s' % FETCHED_AT_Z,
                4,
            ),
            (
                refresh_v1.RefreshOutcome(
                    'daily_accumulator', outcome=refresh_v1.OUTCOME_EMPTY,
                    state='new', count=0, legs=0, fetched_at=FETCHED_AT),
                'type=daily_accumulator outcome=empty state=new count=0 legs=0 '
                'fetched_at=%s' % FETCHED_AT_Z,
                0,
            ),
            (
                refresh_v1.RefreshOutcome(
                    'over_25_goals', outcome=refresh_v1.OUTCOME_EMPTY,
                    state='dry-run:changed', count=0, legs=0,
                    fetched_at=FETCHED_AT),
                'type=over_25_goals outcome=empty state=dry-run:changed count=0 '
                'legs=0 fetched_at=%s' % FETCHED_AT_Z,
                0,
            ),
        )

        for outcome, expected, legs in cases:
            with self.subTest(expected=expected):
                self.assertTrue(outcome.ok)
                self.assertEqual(outcome.line(), expected)
                self.assertNotIn('\n', outcome.line())
                self.assertIn('count=', outcome.line())
                self.assertEqual('legs=' in outcome.line(), legs is not None)

    def test_a_refused_outcome_renders_one_line_and_states_no_values(self):
        outcome = refresh_v1.RefreshOutcome(
            'daily_accumulator', outcome=refresh_v1.OUTCOME_FAILED,
            reason=refresh_v1.REASON_FETCH_FAILED)

        self.assertEqual(
            outcome.line(),
            'type=daily_accumulator outcome=failed reason=fetch_failed',
        )
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.fetched_at)
        self.assertIsNone(outcome.state)
        self.assertIsNone(outcome.count)
        self.assertIsNone(outcome.legs)
        for field in ('state=', 'fetched_at=', 'count=', 'legs='):
            with self.subTest(field=field):
                self.assertNotIn(field, outcome.line())
        self.assertNotIn('\n', outcome.line())

    def test_an_accepted_outcome_without_an_instant_cannot_be_rendered(self):
        # An accepted type was stamped by definition, so the renderer asks the
        # shared formatter for a real instant and refuses to print a line without
        # one rather than printing a plausible-looking timestamp.
        outcome = refresh_v1.RefreshOutcome(
            'bet_of_the_day', state='new', count=1)

        with self.assertRaises(TypeError):
            outcome.line()

    def test_safe_token_is_field_safe_bounded_and_falls_back(self):
        self.assertEqual(
            refresh_v1.SAFE_TOKEN_EXTRA_CHARACTERS, frozenset('._-'))
        self.assertEqual(refresh_v1.REPLACEMENT_CHARACTER, '?')
        cases = (
            ('bet of the day=ok\nreason=x', 'bet?of?the?day?ok?reason?x'),
            ('tab\tand\tnewline\n', 'tab?and?newline?'),
            ('kept-and.dots_ok/x', 'kept-and.dots_ok?x'),
            ('', '?'),
            ('   ', '???'),
            (None, 'None'),
            ('a' * 200, 'a' * 64),
        )

        for tip_type in refresh_v1.REFRESH_TYPE_ORDER:
            with self.subTest(type_key=tip_type):
                self.assertEqual(refresh_v1.safe_token(tip_type), tip_type)
        for value, expected in cases:
            with self.subTest(value=repr(value)):
                self.assertEqual(refresh_v1.safe_token(value), expected)

    def test_a_hostile_key_renders_one_safe_line(self):
        key = 'x\n\ntype=bet_of_the_day outcome=ok state=new'
        outcome = refresh_v1.RefreshOutcome(
            key, outcome=refresh_v1.OUTCOME_FAILED,
            reason=refresh_v1.REASON_UNKNOWN_SOURCE_KEY,
        )

        self.assertEqual(outcome.line().count('\n'), 0)
        # One line, one type field and one outcome field: the key cannot forge a
        # success line of its own.
        self.assertEqual(
            outcome.line(),
            'type=%s outcome=failed reason=%s'
            % (refresh_v1.safe_token(key), refresh_v1.REASON_UNKNOWN_SOURCE_KEY),
        )

    def test_two_outcomes_compare_by_the_values_they_report(self):
        first = refresh_v1.RefreshOutcome(
            'bet_of_the_day', state='new', count=1, fetched_at=FETCHED_AT)
        same = refresh_v1.RefreshOutcome(
            'bet_of_the_day', state='new', count=1, fetched_at=FETCHED_AT)
        changed = refresh_v1.RefreshOutcome(
            'bet_of_the_day', state='changed', count=1, fetched_at=FETCHED_AT)
        two_legs = refresh_v1.RefreshOutcome(
            'daily_accumulator', state='new', count=1, legs=2,
            fetched_at=FETCHED_AT)
        three_legs = refresh_v1.RefreshOutcome(
            'daily_accumulator', state='new', count=1, legs=3,
            fetched_at=FETCHED_AT)

        self.assertEqual(first, same)
        self.assertEqual(len({first, same}), 1)
        self.assertNotEqual(first, changed)
        self.assertNotEqual(two_legs, three_legs)
        self.assertEqual(len({two_legs, three_legs}), 2)


# ---------------------------------------------------------------------------
# The writer, driven with the call sites it reaches replaced by doubles
# ---------------------------------------------------------------------------

class RefreshPipelineTestCase(OfflineGuardMixin, SimpleTestCase):
    """Base for the writer's own decisions, with no database involved.

    The fetch, the state read and the write are replaced in ``setUp``, so a test
    can force exactly the step it is about and assert that no later step ran; the
    serializer stays real unless a test replaces it.
    """

    tip_type = 'bet_of_the_day'

    def setUp(self):
        super().setUp()
        self.fetch = self.stub(utils, 'scrape_one')
        self.state = self.stub(refresh_v1, 'load_snapshot')
        self.store = self.stub(refresh_v1, 'store_snapshot')

    def stub(self, target, attribute, *, value=None, error=None):
        """Replace one call site for the rest of the test and return the double."""
        patcher = mock.patch.object(
            target, attribute, return_value=value, side_effect=error)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def serve(self, *tip_types, legs=2):
        """Let the fetch double answer each named type with its own payload.

        Returns the payloads by type key. A type that was not served fetches
        ``None``, which is a refusal of its own.
        """
        payloads = {
            tip_type: payload_for(tip_type, legs=legs) for tip_type in tip_types
        }
        self.fetch.side_effect = lambda type_key: payloads.get(type_key)
        return payloads

    def stored_record(self, payload, *, type_key=None):
        """Return a stored record of the shape the seam hands back."""
        return {
            'schema_version': SNAPSHOT_SCHEMA_VERSION,
            'type_key': type_key or self.tip_type,
            'payload': payload,
            'fetched_at': FETCHED_AT,
        }

    def store_call(self):
        """Return the ``(type_key, payload, fetched_at)`` the seam was handed."""
        args, kwargs = self.store.call_args
        return args[0], args[1], kwargs.get('fetched_at')

    def expected_line(self, type_key, *, state, count, legs, fetched_at,
                      outcome=refresh_v1.OUTCOME_OK):
        """Return the one report line an accepted type of a run must print.

        A copy of the published template, kept beside ``assert_refused`` so a line
        is asserted by its whole text: a renamed field, an added field or another
        field order fails in this module instead of reaching a scheduler.
        """
        fields = [
            'type=%s' % refresh_v1.safe_token(type_key),
            'outcome=%s' % outcome,
            'state=%s' % state,
            'count=%d' % count,
        ]
        if legs is not None:
            fields.append('legs=%d' % legs)
        fields.append('fetched_at=%s' % format_utc_z(fetched_at))
        return ' '.join(fields)

    def assert_refused(self, outcome, reason):
        """Assert one outcome is a refusal with exactly one reason token."""
        self.assertEqual(outcome.reason, reason)
        self.assertFalse(outcome.ok)
        self.assertIn(reason, refresh_v1.REASON_TOKENS)
        self.assertEqual(
            outcome.line(),
            'type=%s outcome=failed reason=%s'
            % (refresh_v1.safe_token(outcome.type_key), reason),
        )

    def assert_no_refusal_leaks(self, outcome):
        """Assert a refusal line quotes nothing the source supplied."""
        for text in (HOSTILE_TEXT, 'RuntimeError', 'Traceback'):
            with self.subTest(text=text):
                self.assertNotIn(text, outcome.line())

    def assert_wrote_nothing(self):
        """Assert the type reached neither the state read nor the write."""
        self.assertFalse(self.state.called, 'a refused type must not read state')
        self.assertFalse(self.store.called, 'a refused type must not write')

    def assert_wrote_no_row(self):
        """Assert nothing was stored, though the state compared may be read.

        A dry run takes every decision a written run takes, so it does read the row
        it would compare against and stops short of the write.
        """
        self.assertFalse(self.store.called, 'a dry run must not write a row')

    def envelope_for(self, tip_type, **overrides):
        """Return the real serialized envelope for a type, with fields replaced.

        Built from the module's own serializer and the same payloads the fetch
        double serves, so a test that changes one field compares a real envelope
        against the rule being pinned rather than against an invented body.
        """
        envelope = serialize_tips(tip_type, payload_for(tip_type))
        envelope.update(overrides)
        return envelope


class RefreshPipelineSuccessTests(RefreshPipelineTestCase):
    """A healthy fetch reaches the store once, with the payload that was fetched."""

    def test_a_healthy_fetch_is_stored_once_for_every_registered_type(self):
        for tip_type in refresh_v1.REFRESH_TYPE_ORDER:
            with self.subTest(tip_type=tip_type):
                payload = payload_for(tip_type)
                self.fetch.return_value = payload
                # Nothing is stored for this type yet, which is the one thing that
                # makes the comparison report ``new``.
                self.state.return_value = None
                count, legs = published_counts(tip_type)

                outcome = refresh_v1.refresh_type(tip_type, fetched_at=FETCHED_AT)

                self.fetch.assert_called_once_with(tip_type)
                self.state.assert_called_once_with(tip_type)
                self.store.assert_called_once_with(
                    tip_type, payload, fetched_at=FETCHED_AT)
                self.assertEqual(
                    outcome,
                    refresh_v1.RefreshOutcome(
                        tip_type, state='new', count=count, legs=legs,
                        fetched_at=FETCHED_AT),
                )
                self.assertEqual(
                    outcome.line(),
                    self.expected_line(
                        tip_type, state='new', count=count, legs=legs,
                        fetched_at=FETCHED_AT),
                )
                self.fetch.reset_mock()
                self.state.reset_mock()
                self.store.reset_mock()

    def test_only_the_fetched_payload_is_stored_never_the_envelope(self):
        payloads = self.serve(self.tip_type)
        envelope = serialize_tips(self.tip_type, payloads[self.tip_type])

        refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        stored = self.store_call()[1]
        self.assertEqual(stored, payloads[self.tip_type])
        self.assertEqual(stored['matches'], payloads[self.tip_type]['matches'])
        self.assertTrue(SUCCESS_ENVELOPE_KEYS <= set(envelope))
        for envelope_key in ('api_version', 'tips', 'unit'):
            with self.subTest(envelope_key=envelope_key):
                self.assertNotIn(envelope_key, stored)

    def test_the_state_is_read_after_the_fetch_and_before_the_write(self):
        order = []
        self.fetch.side_effect = lambda type_key: (
            order.append('fetch') or payload_for(type_key))
        self.state.side_effect = lambda type_key: order.append('state') or None
        self.store.side_effect = lambda *args, **kwargs: order.append('store')

        refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        self.assertEqual(order, ['fetch', 'state', 'store'])


class RefreshStateComparisonTests(RefreshPipelineTestCase):
    """What a payload is compared against, and what that comparison reports."""

    def test_a_new_state_is_reported_when_the_seam_holds_no_record(self):
        self.serve(self.tip_type)

        outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        count, legs = published_counts(self.tip_type)

        self.assertEqual(outcome.state, 'new')
        self.assertEqual(
            outcome.line(),
            self.expected_line(
                self.tip_type, state='new', count=count, legs=legs,
                fetched_at=FETCHED_AT),
        )
        self.state.assert_called_once_with(self.tip_type)

    def test_an_unchanged_state_is_reported_when_the_stored_payload_matches(self):
        payloads = self.serve(self.tip_type)
        # The stored payload carries the same values in a different insertion
        # order, which is not a change: the comparison is over content.
        reordered = dict(reversed(list(payloads[self.tip_type].items())))
        self.state.return_value = self.stored_record(reordered)

        outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        count, legs = published_counts(self.tip_type)

        self.assertEqual(outcome.state, 'unchanged')
        self.assertEqual(
            outcome.line(),
            self.expected_line(
                self.tip_type, state='unchanged', count=count, legs=legs,
                fetched_at=FETCHED_AT),
        )
        self.assertEqual(self.store_call()[1], payloads[self.tip_type])

    def test_a_changed_state_is_reported_when_the_stored_payload_differs(self):
        self.serve(self.tip_type)
        self.state.return_value = self.stored_record(match_payload('Inter vs Milan'))

        outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        count, legs = published_counts(self.tip_type)

        self.assertEqual(outcome.state, 'changed')
        self.assertEqual(
            outcome.line(),
            self.expected_line(
                self.tip_type, state='changed', count=count, legs=legs,
                fetched_at=FETCHED_AT),
        )

    def test_an_unchanged_payload_is_stored_again_with_the_runs_own_instant(self):
        tip_type = 'daily_accumulator'
        payloads = self.serve(tip_type)
        self.state.return_value = self.stored_record(
            payloads[tip_type], type_key=tip_type)

        outcome = refresh_v1.refresh_type(tip_type, fetched_at=LATER_FETCHED_AT)

        count, legs = published_counts(tip_type)

        self.assertEqual(outcome.state, 'unchanged')
        self.assertEqual(outcome.fetched_at, LATER_FETCHED_AT)
        self.assertEqual(self.store_call()[2], LATER_FETCHED_AT)
        self.assertEqual(
            outcome.line(),
            self.expected_line(
                tip_type, state='unchanged', count=count, legs=legs,
                fetched_at=LATER_FETCHED_AT),
        )


class RefreshFetchRefusalTests(RefreshPipelineTestCase):
    """A fetch that failed, a result that is not a payload, and each envelope."""

    # Every result the fetch layer hands back that is not a payload, and the one
    # reason token each is refused by.
    REFUSED_RESULTS = (
        ({'error': 'Failed to fetch https://example.invalid/tips'},
         refresh_v1.REASON_FETCH_FAILED),
        ({'error': 'Exception fetching https://example.invalid/tips: boom'},
         refresh_v1.REASON_FETCH_FAILED),
        ({'error': 'upstream said no', 'status_code': 503},
         refresh_v1.REASON_FETCH_FAILED),
        ({'error': HOSTILE_TEXT, 'status_code': 200},
         refresh_v1.REASON_FETCH_FAILED),
        ({'error': 'Unknown scraper key: golf_tips'},
         refresh_v1.REASON_UNKNOWN_SOURCE_KEY),
        ({'error': 'something new went wrong'},
         refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
        ({'error': ''}, refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
        ({'error': 'failed'}, refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
        ({'error': 'No tips at all'},
         refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
        ({'error': None}, refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
        ({'error': {}}, refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
        ({'error': []}, refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
        ({'error': 7}, refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
        ({'error': True}, refresh_v1.REASON_UNRECOGNIZED_ERROR_ENVELOPE),
    )

    def test_every_result_that_is_not_a_payload_is_one_refusal(self):
        for result, reason in self.REFUSED_RESULTS:
            with self.subTest(result=repr(result)):
                self.fetch.return_value = result

                outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

                self.assert_refused(outcome, reason)
                self.assert_no_refusal_leaks(outcome)
                self.assert_wrote_nothing()

    def test_a_fetch_that_raises_is_one_refusal_for_every_exception_kind(self):
        errors = (
            RuntimeError(HOSTILE_TEXT), ValueError(HOSTILE_TEXT),
            KeyError(HOSTILE_TEXT), ConnectionError(HOSTILE_TEXT),
            TimeoutError(HOSTILE_TEXT), OSError(HOSTILE_TEXT),
        )

        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.fetch.side_effect = error

                outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

                self.assert_refused(outcome, refresh_v1.REASON_FETCH_EXCEPTION)
                self.assert_no_refusal_leaks(outcome)
                self.assertIsNone(outcome.fetched_at)
                self.assert_wrote_nothing()

    def test_a_result_that_is_not_a_payload_is_refused(self):
        for result in (None, [], ['tip'], 'text', 7, 3.5, b'bytes', object()):
            with self.subTest(result=repr(result)):
                self.fetch.return_value = result

                outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

                self.assert_refused(outcome, refresh_v1.REASON_NON_DICT_RESULT)
                self.assert_wrote_nothing()

    def test_a_cancelled_fetch_is_not_reported_as_a_source_refusal(self):
        self.fetch.side_effect = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        self.assert_wrote_nothing()

    def test_a_refusal_is_logged_once_and_an_accepted_run_logs_nothing(self):
        with self.assertLogs(refresh_v1.LOGGER_NAME, level='ERROR') as captured:
            outcome = refresh_v1.refresh_type('not_a_type', fetched_at=FETCHED_AT)

        self.assertFalse(outcome.ok)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelname, 'ERROR')
        self.assertEqual(record.name, refresh_v1.LOGGER_NAME)
        self.assertIn('not_a_type', record.getMessage())
        self.assertIn(refresh_v1.REASON_UNKNOWN_SOURCE_KEY, record.getMessage())
        self.assertNotIn('\n', record.getMessage())

        self.serve(*refresh_v1.REFRESH_TYPE_ORDER)
        with mock.patch.object(refresh_v1.logger, 'error') as logged, \
                mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
            outcomes = refresh_v1.refresh_types()

        self.assertTrue(all(outcome.ok for outcome in outcomes))
        self.assertFalse(logged.called)


class RefreshUnknownKeyTests(RefreshPipelineTestCase):
    """A key the registry does not publish is refused before anything is fetched."""

    # Keys a caller could plausibly hand in, including the paths of the versioned
    # registry itself and values that are not strings at all: none of them names a
    # source, so none of them may be fetched.
    UNKNOWN_KEYS = (
        '', 'bet of the day', 'BET_OF_THE_DAY', 'bet_of_the_day ',
        ' bet_of_the_day', 'not_a_type', '__class__', 'tips', 'api_version',
        'true', None, 7, 3.5, True, [], {}, set(),
    )

    def test_a_key_the_registry_does_not_publish_is_refused_before_any_io(self):
        for key in self.UNKNOWN_KEYS:
            with self.subTest(key=repr(key)):
                with mock.patch.object(utils, 'scrape_one') as fetch:
                    outcome = refresh_v1.refresh_type(key, fetched_at=FETCHED_AT)

                    self.assert_refused(outcome, refresh_v1.REASON_UNKNOWN_SOURCE_KEY)
                    self.assertFalse(fetch.called, 'nothing may be fetched')
                    self.assert_wrote_nothing()

        # A key that could forge a report line is refused as one safe line: one
        # type field, one outcome field, and no fabricated success in it.
        outcome = refresh_v1.refresh_type(
            'x\n\ntype=bet_of_the_day outcome=ok state=new',
            fetched_at=FETCHED_AT)

        self.assert_refused(outcome, refresh_v1.REASON_UNKNOWN_SOURCE_KEY)
        self.assertEqual(outcome.line().count('\n'), 0)
        self.assertEqual(outcome.line().count('type='), 1)
        self.assertEqual(outcome.line().count('outcome='), 1)
        self.assertNotIn('outcome=ok', outcome.line())


class RefreshPayloadSanitisationTests(RefreshPipelineTestCase):
    """What is digested, compared and stored is the source envelope alone."""

    def test_only_the_fetch_layers_top_level_keys_are_removed(self):
        payload = with_volatile_keys(payload_for(self.tip_type))
        payload['meta'] = {'scraped_at': 'kept', 'source_url': 'kept'}
        payload['matches'][0]['scraped_at'] = 'kept as well'

        sanitized = refresh_v1.sanitize_payload(payload)

        self.assertEqual(
            set(sanitized),
            set(payload) - set(refresh_v1.VOLATILE_RESULT_KEYS))
        for key in refresh_v1.VOLATILE_RESULT_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, sanitized)
        # A nested field of the same name belongs to the source and is kept.
        self.assertEqual(
            sanitized['meta'], {'scraped_at': 'kept', 'source_url': 'kept'})
        self.assertEqual(sanitized['matches'][0]['scraped_at'], 'kept as well')

    def test_the_scrapers_result_is_untouched_and_the_stored_payload_is_sanitised(self):
        payload = payload_for(self.tip_type)
        self.fetch.return_value = with_volatile_keys(payload)

        refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        # The scraper's own result is never edited by the writer.
        result = self.fetch.return_value
        self.assertEqual(result, with_volatile_keys(payload))
        self.assertEqual(result['scraped_at'], SCRAPED_AT_TEXT)
        self.assertEqual(result['source_url'], SOURCE_URL)
        # What is stored is the copy without the fetch layer's own bookkeeping.
        stored = self.store_call()[1]
        self.assertEqual(stored, payload)
        for key in refresh_v1.VOLATILE_RESULT_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, stored)

    def test_a_fetch_that_differs_only_by_bookkeeping_is_unchanged(self):
        payload = payload_for(self.tip_type)
        self.state.return_value = self.stored_record(payload)
        self.fetch.return_value = with_volatile_keys(payload)

        outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=LATER_FETCHED_AT)

        self.assertEqual(outcome.state, 'unchanged')
        self.assertEqual(self.store_call()[1], payload)

    def test_a_result_that_is_not_a_mapping_is_refused_instead_of_copied(self):
        for result in (None, [], ['tip'], 'payload', 7, b'bytes', object()):
            with self.subTest(result=repr(result)):
                with self.assertRaises(TypeError):
                    refresh_v1.sanitize_payload(result)


class RefreshPublishRefusalTests(RefreshPipelineTestCase):
    """Only a payload that describes tips, or the type's own pinned empty
    envelope, is published."""

    def test_a_payload_that_describes_no_tips_is_refused_as_empty(self):
        for tip_type in ('bet_of_the_day', 'daily_accumulator'):
            with self.subTest(tip_type=tip_type):
                with mock.patch.object(
                        refresh_v1, '_now', return_value=FETCHED_AT) as clock:
                    self.fetch.return_value = unpinned_empty_payload(tip_type)

                    outcome = refresh_v1.refresh_type(tip_type, fetched_at=FETCHED_AT)

                    self.assert_refused(
                        outcome, refresh_v1.REASON_EMPTY_SUCCESS_ENVELOPE)
                    # A refused type reaches neither the clock nor the store.
                    self.assertEqual(clock.call_count, 0)
                    self.assert_wrote_nothing()

        # An unpinned empty result never replaces a row that holds tips.
        tip_type = 'daily_accumulator'
        self.state.return_value = self.stored_record(
            payload_for(tip_type), type_key=tip_type)
        self.fetch.return_value = unpinned_empty_payload(tip_type)

        outcome = refresh_v1.refresh_type(tip_type, fetched_at=FETCHED_AT)

        self.assert_refused(outcome, refresh_v1.REASON_EMPTY_SUCCESS_ENVELOPE)
        self.assertFalse(self.state.called)
        self.assertFalse(self.store.called)

    def test_a_pinned_empty_envelope_is_only_its_own_type_s(self):
        cases = (
            # The card units do not publish each other's pinned envelopes either: the
            # wording belongs to the parser that produced it and cannot stand in, so
            # this is an empty payload rather than that type's own pinned one.
            ('over_25_goals', pinned_empty_payload('daily_accumulator')),
            # A pinned envelope the source edited is no longer pinned.
            ('daily_accumulator',
             dict(pinned_empty_payload('daily_accumulator'), count=1)),
        )

        for tip_type, payload in cases:
            with self.subTest(tip_type=tip_type, payload=payload):
                self.fetch.return_value = payload

                outcome = refresh_v1.refresh_type(tip_type, fetched_at=FETCHED_AT)

                self.assert_refused(outcome, refresh_v1.REASON_EMPTY_SUCCESS_ENVELOPE)
                self.assert_wrote_nothing()

    def test_a_payload_that_is_not_its_units_shape_is_refused_as_malformed(self):
        """A raw payload is the shape its own parser states, or it is not published.

        The check is the payload's own entries and the numbers it states about them,
        so it is taken before the serializer, the comparison and the write. Each row
        here is a payload a source could hand over and no parser for this type would
        produce: another unit's shape, an entry that is not a mapping, a total that
        contradicts the entries it is made of, or a count that is not the whole
        number the parser states.
        """
        card = card_payload(legs=2)
        first_card = card['accumulators'][0]
        match = match_payload()
        cases = (
            # The other unit's shape, including the other unit's pinned envelope:
            # the only collection either carries belongs to the other parser.
            ('bet_of_the_day', dict(card)),
            ('bet_of_the_day', pinned_empty_payload('daily_accumulator')),
            ('daily_accumulator', match),
            ('daily_accumulator', pinned_empty_payload('bet_of_the_day')),
            # No collection of any unit at all.
            ('bet_of_the_day', {'date': SOURCE_DATE_TEXT, 'count': 1}),
            # A collection that is not the list the unit publishes.
            ('bet_of_the_day', dict(match, matches='one tip')),
            ('bet_of_the_day', dict(match, matches={'prediction': 'Arsenal'})),
            ('bet_of_the_day', dict(match, matches=tuple(match['matches']))),
            ('daily_accumulator', dict(card, accumulators='one card')),
            ('daily_accumulator', dict(card, accumulators={'card': first_card})),
            # Entries that are not mappings.
            ('bet_of_the_day', dict(match, matches=[1, 2])),
            ('bet_of_the_day', dict(match, matches=[[{'prediction': 'A'}]])),
            ('daily_accumulator', dict(card, accumulators=['one card'])),
            # A card whose own legs are not the list of mappings it states.
            ('daily_accumulator',
             dict(card, accumulators=[dict(first_card, matches='one leg')])),
            ('daily_accumulator',
             dict(card, accumulators=[dict(first_card, matches=[1])])),
            ('daily_accumulator',
             dict(card, accumulators=[{'category': 'Accumulator'}])),
            # A count that is not the number the entries themselves state.
            ('bet_of_the_day', dict(match, count=2)),
            ('bet_of_the_day', dict(match, count=0)),
            ('bet_of_the_day', dict(match, total_tips=2)),
            ('bet_of_the_day', dict(match, total_tips=None)),
            ('bet_of_the_day', dict(match, count=True)),
            ('bet_of_the_day', dict(match, count='1')),
            ('bet_of_the_day', dict(match, count=1.0)),
            # A card's own leg count is the number of legs it lists.
            ('daily_accumulator',
             dict(card, accumulators=[dict(first_card, matches_count=3)])),
            ('daily_accumulator',
             dict(card, accumulators=[dict(first_card, matches_count=0,
                                           matches=[])])),
            ('daily_accumulator',
             dict(card, accumulators=[dict(first_card, matches_count=True)])),
            ('daily_accumulator',
             dict(card, accumulators=[dict(first_card, matches_count='2')])),
            # The payload's own totals describe what it holds.
            ('daily_accumulator', dict(card, count=1)),
            ('daily_accumulator', dict(card, count=True)),
            ('daily_accumulator', dict(card, count='2')),
            ('daily_accumulator', dict(card, total_accumulators=2)),
            ('daily_accumulator', dict(card, total_accumulators=None)),
        )

        for tip_type, payload in cases:
            with self.subTest(tip_type=tip_type, payload=repr(payload)):
                self.fetch.return_value = payload
                with mock.patch.object(
                        refresh_v1, '_now', return_value=FETCHED_AT) as clock:
                    outcome = refresh_v1.refresh_type(
                        tip_type, fetched_at=FETCHED_AT)

                self.assert_refused(
                    outcome, refresh_v1.REASON_MALFORMED_SUCCESS_ENVELOPE)
                self.assert_no_refusal_leaks(outcome)
                self.assertIsNone(outcome.fetched_at)
                self.assertNotIn('fetched_at=', outcome.line())
                # The shape is decided on the payload alone: a payload refused for it
                # reaches neither the clock, nor the comparison, nor the store.
                self.assertEqual(clock.call_count, 0)
                self.assert_wrote_nothing()

    def test_a_serializer_that_raises_is_reported_as_rejected(self):
        errors = (
            UnknownTipType('unsupported tip type: x'),
            ValueError(HOSTILE_TEXT), TypeError(HOSTILE_TEXT),
            KeyError(HOSTILE_TEXT), RuntimeError(HOSTILE_TEXT),
        )

        for error in errors:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(utils, 'scrape_one') as fetch, \
                        mock.patch.object(
                            refresh_v1, 'serialize_tips') as serializer, \
                        mock.patch.object(
                            refresh_v1, 'load_snapshot') as state, \
                        mock.patch.object(
                            refresh_v1, 'store_snapshot') as store:
                    fetch.return_value = payload_for(self.tip_type)
                    serializer.side_effect = error

                    outcome = refresh_v1.refresh_type(
                        self.tip_type, fetched_at=FETCHED_AT)

                    self.assert_refused(outcome, refresh_v1.REASON_SERIALIZER_REJECTED)
                    self.assert_no_refusal_leaks(outcome)
                    self.assertFalse(state.called)
                    self.assertFalse(store.called)


class RefreshMalformedEnvelopeTests(RefreshPipelineTestCase):
    """A serializer that answers with anything else is refused, never trusted."""

    tip_type = 'daily_accumulator'

    def bad_bodies(self):
        """Return every body this writer must decline to publish."""
        without_a_key = self.envelope_for(self.tip_type)
        del without_a_key['legs_count']
        return (
            None, [], 'envelope', 7, b'body',
            without_a_key,
            self.envelope_for(self.tip_type, type='bet_of_the_day'),
            self.envelope_for(self.tip_type, tips=None),
            self.envelope_for(self.tip_type, tips={'a': 'tip'}),
            # A body with no tips but a leg count contradicts itself.
            self.envelope_for(self.tip_type, tips=[], count=0, legs_count=3),
            self.envelope_for(self.tip_type, count=2),
            self.envelope_for(self.tip_type, count=0),
            self.envelope_for(self.tip_type, count=True),
            self.envelope_for(self.tip_type, count='1'),
            self.envelope_for(self.tip_type, legs_count='three'),
            self.envelope_for(self.tip_type, legs_count=-1),
            self.envelope_for(self.tip_type, legs_count=1.5),
        )

    def test_a_body_this_writer_cannot_vouch_for_is_refused(self):
        for body in self.bad_bodies():
            with self.subTest(body=repr(body)):
                with mock.patch.object(utils, 'scrape_one') as fetch, \
                        mock.patch.object(
                            refresh_v1, 'serialize_tips') as serializer, \
                        mock.patch.object(
                            refresh_v1, 'load_snapshot') as state, \
                        mock.patch.object(
                            refresh_v1, 'store_snapshot') as store:
                    fetch.return_value = payload_for(self.tip_type)
                    serializer.return_value = body

                    outcome = refresh_v1.refresh_type(
                        self.tip_type, fetched_at=FETCHED_AT)

                    self.assert_refused(
                        outcome, refresh_v1.REASON_MALFORMED_SUCCESS_ENVELOPE)
                    self.assertEqual(outcome.line().count('outcome='), 1)
                    self.assertFalse(state.called)
                    self.assertFalse(store.called)

    def test_the_serializer_is_asked_about_the_payload_that_was_fetched(self):
        self.tip_type = 'btts_and_win'
        payloads = self.serve(self.tip_type)
        envelope = serialize_tips(self.tip_type, payloads[self.tip_type])
        serializer = self.stub(refresh_v1, 'serialize_tips', value=envelope)

        outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        serializer.assert_called_once_with(self.tip_type, payloads[self.tip_type])
        self.assertEqual(
            (outcome.count, outcome.legs),
            (envelope['count'], envelope['legs_count']),
        )
        self.assertEqual(self.store_call()[1], payloads[self.tip_type])


class RefreshStateReadRefusalTests(RefreshPipelineTestCase):
    """A writer that cannot tell what is stored must never overwrite it."""

    def test_a_state_read_that_raises_blocks_the_write(self):
        errors = (
            DatabaseError(HOSTILE_TEXT), TypeError(HOSTILE_TEXT),
            KeyError(HOSTILE_TEXT), ValueError(HOSTILE_TEXT),
            RuntimeError(HOSTILE_TEXT),
        )

        for error in errors:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(utils, 'scrape_one') as fetch, \
                        mock.patch.object(
                            refresh_v1, 'load_snapshot') as state, \
                        mock.patch.object(
                            refresh_v1, 'store_snapshot') as store:
                    fetch.return_value = payload_for(self.tip_type)
                    state.side_effect = error

                    outcome = refresh_v1.refresh_type(
                        self.tip_type, fetched_at=FETCHED_AT)

                    self.assert_refused(outcome, refresh_v1.REASON_STATE_READ_FAILED)
                    self.assert_no_refusal_leaks(outcome)
                    state.assert_called_once_with(self.tip_type)
                    self.assertFalse(store.called)

    def test_a_record_that_cannot_be_compared_is_refused(self):
        records = (
            {'schema_version': SNAPSHOT_SCHEMA_VERSION,
             'type_key': self.tip_type},
            'record', 7, ['payload'],
        )

        for record in records:
            with self.subTest(record=repr(record)):
                with mock.patch.object(utils, 'scrape_one') as fetch, \
                        mock.patch.object(
                            refresh_v1, 'load_snapshot') as state, \
                        mock.patch.object(
                            refresh_v1, 'store_snapshot') as store:
                    fetch.return_value = payload_for(self.tip_type)
                    state.return_value = record

                    outcome = refresh_v1.refresh_type(
                        self.tip_type, fetched_at=FETCHED_AT)

                    self.assert_refused(outcome, refresh_v1.REASON_STATE_READ_FAILED)
                    # The read is the step that refused: it was asked for this
                    # type's row once, and the store was never handed anything.
                    state.assert_called_once_with(self.tip_type)
                    self.assertFalse(store.called)

    def test_a_stored_payload_that_cannot_be_compared_counts_as_changed(self):
        records = (
            # A stored payload the store's digest rule cannot represent.
            self.stored_record({'when': object()}),
            # A stored payload of a shape this writer never stored.
            {'payload': 'not-a-payload'},
        )

        for record in records:
            with self.subTest(record=repr(record)):
                with mock.patch.object(utils, 'scrape_one') as fetch, \
                        mock.patch.object(
                            refresh_v1, 'load_snapshot') as state, \
                        mock.patch.object(
                            refresh_v1, 'store_snapshot') as store:
                    fetch.return_value = payload_for(self.tip_type)
                    state.return_value = record

                    outcome = refresh_v1.refresh_type(
                        self.tip_type, fetched_at=FETCHED_AT)

                    self.assertTrue(outcome.ok)
                    self.assertEqual(outcome.state, 'changed')
                    self.assertEqual(store.call_count, 1)


class RefreshWriteRefusalTests(RefreshPipelineTestCase):
    """The store has the last word: a write it refuses is reported by kind."""

    def test_a_store_that_rejects_the_payload_is_reported_as_rejected(self):
        for error in (TypeError(HOSTILE_TEXT), ValueError(HOSTILE_TEXT)):
            with self.subTest(error=type(error).__name__):
                self.serve(self.tip_type)
                self.store.side_effect = error

                outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

                self.assert_refused(outcome, refresh_v1.REASON_STORE_REJECTED)
                self.assert_no_refusal_leaks(outcome)
                self.assertEqual(self.store.call_count, 1)
                self.assertIsNone(outcome.fetched_at)
                self.store.reset_mock()

    def test_a_payload_the_store_cannot_digest_is_refused_before_the_write(self):
        payload = payload_for(self.tip_type)

        for value in ({'a', 'b'}, object()):
            with self.subTest(value=type(value).__name__):
                with mock.patch.object(utils, 'scrape_one') as fetch, \
                        mock.patch.object(
                            refresh_v1, 'load_snapshot') as state, \
                        mock.patch.object(
                            refresh_v1, 'store_snapshot') as store:
                    fetch.return_value = dict(payload, tags=value)
                    state.return_value = None

                    outcome = refresh_v1.refresh_type(
                        self.tip_type, fetched_at=FETCHED_AT)

                    self.assert_refused(outcome, refresh_v1.REASON_STORE_REJECTED)
                    self.assertFalse(store.called)

    def test_any_other_store_exception_is_reported_as_a_store_failure(self):
        errors = (
            DatabaseError(HOSTILE_TEXT), RuntimeError(HOSTILE_TEXT),
            OSError(HOSTILE_TEXT), Exception(HOSTILE_TEXT),
        )

        for error in errors:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(utils, 'scrape_one') as fetch, \
                        mock.patch.object(
                            refresh_v1, 'load_snapshot') as state, \
                        mock.patch.object(
                            refresh_v1, 'store_snapshot') as store:
                    fetch.return_value = payload_for(self.tip_type)
                    state.return_value = None
                    store.side_effect = error

                    outcome = refresh_v1.refresh_type(
                        self.tip_type, fetched_at=FETCHED_AT)

                    self.assert_refused(outcome, refresh_v1.REASON_STORE_FAILED)
                    self.assert_no_refusal_leaks(outcome)
                    self.assertEqual(store.call_count, 1)

    def test_a_digest_that_fails_for_another_reason_is_a_store_failure(self):
        self.serve(self.tip_type)

        with mock.patch.object(storage_v1, 'canonical_payload_sha256',
                               side_effect=RuntimeError(HOSTILE_TEXT)):
            outcome = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        self.assert_refused(outcome, refresh_v1.REASON_STORE_FAILED)
        self.assert_no_refusal_leaks(outcome)
        self.assert_wrote_nothing()

    def test_a_cancelled_write_is_not_reported_as_a_store_refusal(self):
        self.serve(self.tip_type)
        self.store.side_effect = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        self.assertEqual(self.store.call_count, 1)

    def test_a_refused_write_has_no_instant_though_the_clock_was_read(self):
        self.serve(self.tip_type)
        self.store.side_effect = DatabaseError(HOSTILE_TEXT)

        with mock.patch.object(
                refresh_v1, '_now', return_value=FETCHED_AT) as clock, \
                mock.patch.object(refresh_v1.logger, 'error'):
            outcome = refresh_v1.refresh_type(self.tip_type)

        self.assertEqual(clock.call_count, 1)
        self.assertIsNone(outcome.fetched_at)
        self.assertNotIn('fetched_at=', outcome.line())


class RefreshSelectionTests(RefreshPipelineTestCase):
    """Which types a run may touch, settled in full before the first fetch."""

    def test_no_selection_and_an_empty_selection_choose_the_whole_registry(self):
        for selection in (None, [], ()):
            with self.subTest(selection=repr(selection)):
                self.assertEqual(
                    refresh_v1.resolve_type_keys(selection),
                    refresh_v1.REFRESH_TYPE_ORDER,
                )

    def test_a_bare_key_is_a_selection_of_one_and_repeats_are_deduplicated(self):
        self.assertEqual(
            refresh_v1.resolve_type_keys('bet_of_the_day'), ('bet_of_the_day',))

        requested = ('anytime_goalscorer', 'bet_of_the_day',
                     'anytime_goalscorer', 'bet_of_the_day')
        self.assertEqual(
            refresh_v1.resolve_type_keys(requested),
            ('bet_of_the_day', 'anytime_goalscorer'),
        )
        self.assertEqual(
            refresh_v1.resolve_type_keys(
                ('daily_accumulator', 'bet_of_the_day', 'daily_accumulator')),
            ('bet_of_the_day', 'daily_accumulator'),
        )

    def test_a_key_the_registry_does_not_publish_is_refused_before_any_io(self):
        # Every value a command line or a caller could hand in as one entry of a
        # selection, including values that are not strings at all: none of them
        # names a source, so none of them is fetched, read or written.
        for type_key in ('not_a_type', '', 'BET_OF_THE_DAY', None, 7, True,
                         3.5, [], {}, set(), UnhashableKey()):
            with self.subTest(type_key=repr(type_key)):
                with self.assertRaises(UnknownTipType):
                    refresh_v1.resolve_type_keys((type_key, 'bet_of_the_day'))

                self.assertFalse(self.fetch.called)
                self.assertFalse(self.state.called)
                self.assertFalse(self.store.called)

        # The message is one fixed sentence of its own: it quotes no value, so a key
        # that could forge a report line cannot appear in a log as anything at all.
        with self.assertRaises(UnknownTipType) as caught:
            refresh_v1.resolve_type_keys(('x\n\ntype=ok outcome=ok',))

        self.assertEqual(str(caught.exception), 'unsupported tip type')
        self.assertNotIn('\n', str(caught.exception))
        self.assertNotIn('type=', str(caught.exception))


class RefreshRunTests(RefreshPipelineTestCase):
    """One run: the registry's types, in its order, each on its own instant."""

    def test_a_run_with_no_selection_refreshes_the_whole_registry_in_order(self):
        self.serve(*refresh_v1.REFRESH_TYPE_ORDER)

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
            outcomes = refresh_v1.refresh_types()

        self.assertEqual(
            [outcome.type_key for outcome in outcomes],
            list(refresh_v1.REFRESH_TYPE_ORDER),
        )
        self.assertEqual(self.store.call_count, len(refresh_v1.REFRESH_TYPE_ORDER))
        for outcome in outcomes:
            with self.subTest(type_key=outcome.type_key):
                self.assertTrue(outcome.ok)
                self.assertEqual(outcome.state, 'new')

    def test_an_empty_selection_refreshes_the_whole_registry_too(self):
        order = list(refresh_v1.REFRESH_TYPE_ORDER)
        self.serve(*order)

        for selection in ((), []):
            with self.subTest(selection=repr(selection)):
                with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
                    outcomes = refresh_v1.refresh_types(selection)

                self.assertEqual([outcome.type_key for outcome in outcomes], order)
                self.assertTrue(all(outcome.ok for outcome in outcomes))
                self.assertEqual(self.store.call_count, len(order))
                self.store.reset_mock()

    def test_the_registrys_order_decides_the_order_and_a_repeat_runs_once(self):
        requested = ('daily_accumulator', 'bet_of_the_day', 'daily_accumulator')
        self.serve('daily_accumulator', 'bet_of_the_day')

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
            lines = [outcome.line() for outcome in refresh_v1.refresh_types(requested)]

        self.assertEqual(
            lines,
            [
                'type=bet_of_the_day outcome=ok state=new count=1 '
                'fetched_at=%s' % FETCHED_AT_Z,
                'type=daily_accumulator outcome=ok state=new count=1 legs=2 '
                'fetched_at=%s' % FETCHED_AT_Z,
            ],
        )
        self.assertEqual(self.store.call_count, 2)

    def test_each_accepted_type_reads_the_clock_for_itself(self):
        order = list(refresh_v1.REFRESH_TYPE_ORDER)
        self.serve(*order)
        handed_out = instants(len(order))

        with mock.patch.object(refresh_v1, '_now', side_effect=handed_out) as clock:
            outcomes = refresh_v1.refresh_types()

        self.assertEqual(clock.call_count, len(order))
        for outcome, instant in zip(outcomes, handed_out):
            with self.subTest(type_key=outcome.type_key):
                count, legs = published_counts(outcome.type_key)

                self.assertEqual(outcome.fetched_at, instant)
                self.assertEqual(
                    outcome.line(),
                    self.expected_line(
                        outcome.type_key, state='new', count=count, legs=legs,
                        fetched_at=instant),
                )
        self.assertEqual(
            [call.kwargs['fetched_at'] for call in self.store.call_args_list],
            handed_out,
        )

    def test_one_failed_type_does_not_stop_the_later_types(self):
        order = list(refresh_v1.REFRESH_TYPE_ORDER)
        refusing = order[2]
        payloads = self.serve(*order)

        def fetch(type_key):
            if type_key == refusing:
                raise RuntimeError(HOSTILE_TEXT)
            return payloads[type_key]

        self.fetch.side_effect = fetch

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT), \
                mock.patch.object(refresh_v1.logger, 'error'):
            outcomes = refresh_v1.refresh_types()

        self.assertEqual([outcome.type_key for outcome in outcomes], order)
        failed = [outcome for outcome in outcomes if not outcome.ok]
        self.assertEqual([outcome.type_key for outcome in failed], [refusing])
        self.assertEqual(failed[0].reason, refresh_v1.REASON_FETCH_EXCEPTION)
        self.assertEqual(self.store.call_count, len(order) - 1)

    def test_a_run_whose_selection_is_unusable_fetches_and_writes_nothing(self):
        with self.assertRaises(UnknownTipType):
            refresh_v1.refresh_types(('bet_of_the_day', 'not_a_type'))

        self.assertFalse(self.fetch.called)
        self.assert_wrote_nothing()

    def test_a_run_of_refusals_states_no_instant_for_any_type(self):
        requested = ('bet_of_the_day', 'daily_accumulator')
        self.fetch.side_effect = RuntimeError(HOSTILE_TEXT)

        with mock.patch.object(refresh_v1.logger, 'error'):
            outcomes = refresh_v1.refresh_types(requested)

        self.assertEqual({outcome.fetched_at for outcome in outcomes}, {None})
        for outcome in outcomes:
            with self.subTest(type_key=outcome.type_key):
                self.assertNotIn(HOSTILE_TEXT, outcome.line())
                self.assertIn(outcome.reason, refresh_v1.REASON_TOKENS)
                self.assertNotIn('fetched_at=', outcome.line())


class RefreshEmptyPublicationTests(RefreshPipelineTestCase):
    """A card-free page is an answer of its own, not a refusal.

    Each frozen parser states "no tips at all" as exactly one pinned envelope, and
    that envelope is the only zero-count payload this writer publishes: the endpoint
    then answers its type with a 200 and no tips instead of leaving yesterday's row
    in place forever.
    """

    def expected_legs(self, tip_type):
        """Return the legs a pinned empty envelope states for a type.

        ``None`` for a ``match`` unit, which is what keeps ``legs=`` out of its line.
        """
        return 0 if TIP_TYPE_UNITS[tip_type] == UNIT_CARD else None

    def test_the_pinned_empty_envelope_of_every_type_is_published_and_stored(self):
        for tip_type in refresh_v1.REFRESH_TYPE_ORDER:
            with self.subTest(tip_type=tip_type):
                payload = pinned_empty_payload(tip_type)
                self.fetch.return_value = payload

                outcome = refresh_v1.refresh_type(tip_type, fetched_at=FETCHED_AT)

                legs = self.expected_legs(tip_type)
                self.assertEqual(
                    outcome,
                    refresh_v1.RefreshOutcome(
                        tip_type, outcome=refresh_v1.OUTCOME_EMPTY,
                        state='new', count=0, legs=legs,
                        fetched_at=FETCHED_AT),
                )
                self.assertEqual(
                    outcome.line(),
                    self.expected_line(
                        tip_type, outcome=refresh_v1.OUTCOME_EMPTY,
                        state='new', count=0, legs=legs, fetched_at=FETCHED_AT),
                )
                # A reader is served the stored envelope as an empty result rather
                # than as an error, so the row is a truthful answer of its own.
                self.assertEqual(self.store_call()[1], payload)
                self.assertEqual(
                    serialize_tips(tip_type, self.store_call()[1])['count'], 0)

    def test_an_empty_payload_is_compared_like_any_other_payload(self):
        tip_type = 'daily_accumulator'
        payload = pinned_empty_payload(tip_type)
        self.fetch.return_value = payload
        self.state.return_value = self.stored_record(payload, type_key=tip_type)

        with mock.patch.object(refresh_v1.logger, 'error') as logged:
            outcome = refresh_v1.refresh_type(tip_type, fetched_at=LATER_FETCHED_AT)

        self.assertEqual(outcome.outcome, refresh_v1.OUTCOME_EMPTY)
        self.assertEqual(outcome.state, 'unchanged')
        self.assertEqual(outcome.fetched_at, LATER_FETCHED_AT)
        self.assertEqual(self.store_call()[1], payload)
        # An empty result is not a reason to log an error.
        self.assertFalse(logged.called)

    def test_an_empty_type_does_not_stop_the_types_that_come_after_it(self):
        order = list(refresh_v1.REFRESH_TYPE_ORDER)
        payloads = {key: pinned_empty_payload(key) for key in order}
        self.fetch.side_effect = lambda type_key: payloads.get(type_key)

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
            outcomes = refresh_v1.refresh_types()

        self.assertEqual([outcome.type_key for outcome in outcomes], order)
        self.assertEqual(
            {outcome.outcome for outcome in outcomes},
            {refresh_v1.OUTCOME_EMPTY},
        )
        self.assertTrue(all(outcome.ok for outcome in outcomes))
        self.assertEqual(self.store.call_count, len(order))


class RefreshDryRunTests(RefreshPipelineTestCase):
    """A dry run takes every decision a written run takes, and stores nothing.

    The state it reports is prefixed, so a dry-run line can never be read as the
    state a stored row holds; the instant it reports is the one a row would have been
    stamped with; and its refusals are the same refusals, because whether a payload
    may be published is decided before anything reaches the store.
    """

    def test_a_dry_run_writes_no_row_and_states_a_prefixed_state(self):
        order = list(refresh_v1.REFRESH_TYPE_ORDER)
        self.serve(*order)
        handed_out = instants(len(order))

        with mock.patch.object(refresh_v1, '_now', side_effect=handed_out) as clock:
            outcomes = refresh_v1.refresh_types(dry_run=True)

        self.assertEqual(clock.call_count, len(order))
        self.assertEqual([outcome.type_key for outcome in outcomes], order)
        for outcome, instant in zip(outcomes, handed_out):
            with self.subTest(type_key=outcome.type_key):
                self.assertTrue(outcome.ok)
                self.assertIn(outcome.state, refresh_v1.DRY_RUN_STATE_TOKENS)
                self.assertNotIn(outcome.state, refresh_v1.STATE_TOKENS)
                self.assertEqual(outcome.fetched_at, instant)
        self.assertTrue(self.state.called)
        self.assert_wrote_no_row()

    def test_a_dry_run_states_the_state_it_would_have_written(self):
        cases = (
            # (tip type, what the seam holds, the state a dry run reports)
            ('bet_of_the_day', None, 'dry-run:new'),
            ('bet_of_the_day', 'same', 'dry-run:unchanged'),
            ('daily_accumulator', 'other', 'dry-run:changed'),
        )

        for tip_type, stored, expected in cases:
            with self.subTest(tip_type=tip_type, stored=repr(stored)):
                payload = payload_for(tip_type)
                self.fetch.return_value = payload
                if stored == 'same':
                    self.state.return_value = self.stored_record(
                        payload, type_key=tip_type)
                elif stored == 'other':
                    self.state.return_value = self.stored_record(
                        payload_for(tip_type, legs=3), type_key=tip_type)
                else:
                    self.state.return_value = None
                count, legs = published_counts(tip_type)

                outcome = refresh_v1.refresh_type(
                    tip_type, fetched_at=FETCHED_AT, dry_run=True)

                self.assertEqual(
                    outcome,
                    refresh_v1.RefreshOutcome(
                        tip_type, state=expected, count=count, legs=legs,
                        fetched_at=FETCHED_AT),
                )
                self.assert_wrote_no_row()
                if expected == 'dry-run:changed':
                    self.assertEqual(
                        outcome.line(),
                        'type=daily_accumulator outcome=ok '
                        'state=dry-run:changed count=1 legs=2 fetched_at=%s'
                        % FETCHED_AT_Z,
                    )

    def test_a_dry_run_of_the_pinned_empty_envelope_is_an_accepted_empty(self):
        tip_type = self.tip_type
        self.fetch.return_value = pinned_empty_payload(tip_type)

        outcome = refresh_v1.refresh_type(tip_type, fetched_at=FETCHED_AT, dry_run=True)

        self.assertEqual(outcome.outcome, refresh_v1.OUTCOME_EMPTY)
        self.assertEqual(
            outcome.line(),
            'type=bet_of_the_day outcome=empty state=dry-run:new count=0 '
            'fetched_at=%s' % FETCHED_AT_Z,
        )
        self.assertEqual(self.state.call_count, 1)
        self.assert_wrote_no_row()

    def test_a_dry_run_refuses_exactly_what_a_written_run_refuses(self):
        self.fetch.side_effect = RuntimeError(HOSTILE_TEXT)

        with mock.patch.object(refresh_v1.logger, 'error') as logged:
            dry = refresh_v1.refresh_type(
                self.tip_type, fetched_at=FETCHED_AT, dry_run=True)
            written = refresh_v1.refresh_type(self.tip_type, fetched_at=FETCHED_AT)

        self.assertEqual(dry, written)
        self.assertEqual(dry.outcome, refresh_v1.OUTCOME_FAILED)
        self.assertEqual(
            dry.line(),
            'type=bet_of_the_day outcome=failed reason=fetch_exception',
        )
        self.assertEqual(logged.call_count, 2)
        self.assert_wrote_nothing()


class RefreshCommandTests(RefreshPipelineTestCase):
    """``manage.py refresh_tips``: what it prints, and what its exit status means."""

    def test_the_command_is_registered_and_offers_its_two_switches(self):
        self.assertEqual(get_commands()['refresh_tips'], 'alltips_scraper')

        parser = refresh_tips.Command().create_parser('manage.py', 'refresh_tips')
        parsed = parser.parse_args(
            ['--type', 'daily_accumulator', '--type', 'bet_of_the_day'])

        self.assertEqual(parsed.tip_types, ['daily_accumulator', 'bet_of_the_day'])
        self.assertIsNone(parser.parse_args([]).tip_types)
        self.assertFalse(parser.parse_args([]).dry_run)
        self.assertTrue(parser.parse_args(['--dry-run']).dry_run)

        documented = refresh_tips.Command.help + (refresh_tips.__doc__ or '')
        for text in ('--type', '--dry-run'):
            with self.subTest(text=text):
                self.assertIn(text, documented)

    def test_a_clean_run_prints_one_line_per_type_and_exits_zero(self):
        order = list(refresh_v1.REFRESH_TYPE_ORDER)
        self.serve(*order)
        handed_out = instants(len(order))
        out = StringIO()

        with mock.patch.object(refresh_v1, '_now', side_effect=handed_out):
            call_command('refresh_tips', stdout=out)

        expected = []
        for type_key, instant in zip(order, handed_out):
            count, legs = published_counts(type_key)
            expected.append(self.expected_line(
                type_key, state='new', count=count, legs=legs,
                fetched_at=instant))

        self.assertEqual(out.getvalue().splitlines(), expected)

    def test_the_command_refreshes_the_requested_types_in_registry_order(self):
        self.serve('bet_of_the_day', 'daily_accumulator')
        out = StringIO()

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
            call_command(
                'refresh_tips',
                '--type', 'daily_accumulator',
                '--type', 'bet_of_the_day',
                '--type', 'daily_accumulator',
                stdout=out,
            )

        self.assertEqual(
            out.getvalue().splitlines(),
            [
                'type=bet_of_the_day outcome=ok state=new count=1 '
                'fetched_at=%s' % FETCHED_AT_Z,
                'type=daily_accumulator outcome=ok state=new count=1 legs=2 '
                'fetched_at=%s' % FETCHED_AT_Z,
            ],
        )
        self.assertEqual(self.store.call_count, 2)

    def test_a_run_that_published_only_an_empty_envelope_exits_zero(self):
        self.fetch.return_value = pinned_empty_payload('bet_of_the_day')
        out = StringIO()

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
            call_command('refresh_tips', '--type', 'bet_of_the_day', stdout=out)

        self.assertEqual(
            out.getvalue(),
            'type=bet_of_the_day outcome=empty state=new count=0 '
            'fetched_at=%s\n' % FETCHED_AT_Z,
        )
        self.assertEqual(self.store.call_count, 1)


    def test_a_dry_run_reports_the_prefixed_state_and_writes_no_row(self):
        self.serve('bet_of_the_day', 'daily_accumulator')
        out = StringIO()

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
            call_command(
                'refresh_tips',
                '--dry-run',
                '--type', 'daily_accumulator',
                '--type', 'bet_of_the_day',
                stdout=out,
            )

        self.assertEqual(
            out.getvalue().splitlines(),
            [
                'type=bet_of_the_day outcome=ok state=dry-run:new count=1 '
                'fetched_at=%s' % FETCHED_AT_Z,
                'type=daily_accumulator outcome=ok state=dry-run:new count=1 '
                'legs=2 fetched_at=%s' % FETCHED_AT_Z,
            ],
        )
        self.assert_wrote_no_row()

    def test_a_refused_type_fails_the_run_with_one_after_every_line(self):
        payloads = self.serve('bet_of_the_day', 'daily_accumulator')

        def fetch(type_key):
            if type_key == 'daily_accumulator':
                raise RuntimeError(HOSTILE_TEXT)
            return payloads[type_key]

        self.fetch.side_effect = fetch
        out = StringIO()

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT), \
                mock.patch.object(refresh_v1.logger, 'error') as logged:
            with self.assertRaises(CommandError) as caught:
                call_command(
                    'refresh_tips',
                    '--type', 'bet_of_the_day',
                    '--type', 'daily_accumulator',
                    stdout=out,
                )

        self.assertEqual(str(caught.exception), refresh_tips.FAILURE_MESSAGE)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                'type=bet_of_the_day outcome=ok state=new count=1 '
                'fetched_at=%s' % FETCHED_AT_Z,
                'type=daily_accumulator outcome=failed reason=fetch_exception',
            ],
        )
        self.assertEqual(self.store.call_count, 1)
        # The failure is logged as one template and two safe tokens.
        self.assertEqual(
            logged.call_args.args,
            (refresh_v1.FAILED_REFRESH_MESSAGE, 'daily_accumulator',
             refresh_v1.REASON_FETCH_EXCEPTION),
        )

    def test_an_unusable_type_fails_with_a_two_before_any_io_or_output(self):
        selections = (
            ('--type', 'not_a_type'),
            ('--type', 'bet_of_the_day', '--type', 'not_a_type'),
        )

        for selection in selections:
            with self.subTest(selection=selection):
                with mock.patch.object(utils, 'scrape_one') as fetch, \
                        mock.patch.object(
                            refresh_v1, 'load_snapshot') as state, \
                        mock.patch.object(
                            refresh_v1, 'store_snapshot') as store, \
                        mock.patch.object(
                            refresh_v1.logger, 'error') as logged:
                    out = StringIO()

                    with self.assertRaises(CommandError) as caught:
                        call_command('refresh_tips', *selection, stdout=out)

                    self.assertEqual(
                        str(caught.exception), refresh_tips.UNKNOWN_TYPE_MESSAGE)
                    self.assertEqual(
                        caught.exception.returncode,
                        refresh_tips.UNKNOWN_TYPE_RETURNCODE,
                    )
                    self.assertEqual(caught.exception.returncode, 2)
                    self.assertEqual(out.getvalue(), '')
                    self.assertFalse(logged.called)
                    self.assertFalse(fetch.called)
                    self.assertFalse(state.called)
                    self.assertFalse(store.called)


# ---------------------------------------------------------------------------
# The writer against the durable provider: the rows clients are actually served
# ---------------------------------------------------------------------------

class RefreshStoredRowTests(OfflineGuardMixin, TestCase):
    """What a run leaves in the real snapshot table, and what it must not touch.

    The seam's durable provider is installed for the length of each test, so this
    is the only class here that exercises the writer's row instead of a double's
    call record: the hash, the stamp and the payload a reader is served are the
    store's own, and the endpoint's answer is read from the row the run wrote.
    """

    tip_type = 'bet_of_the_day'

    def setUp(self):
        super().setUp()
        installed = get_snapshot_provider()
        set_snapshot_provider(DatabaseSnapshotProvider())
        self.addCleanup(set_snapshot_provider, installed)
        patcher = mock.patch.object(utils, 'scrape_one')
        self.addCleanup(patcher.stop)
        self.fetch = patcher.start()

    def run_type(self, *, fetched_at=FETCHED_AT):
        """Fetch for the type under test and refresh it on the given instant."""
        return refresh_v1.refresh_type(self.tip_type, fetched_at=fetched_at)

    def served(self, type_key=None):
        """Return the endpoint's answer for one stored type."""
        response = self.client.get(ENDPOINT_PATH, {'type': type_key or self.tip_type})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_first_run_writes_the_row_a_reader_is_served_from(self):
        payload = payload_for(self.tip_type)
        self.fetch.return_value = payload
        count, legs = published_counts(self.tip_type)

        outcome = self.run_type()

        self.assertEqual(
            outcome,
            refresh_v1.RefreshOutcome(
                self.tip_type, state='new', count=count, legs=legs,
                fetched_at=FETCHED_AT),
        )
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(
            row_values(self.tip_type),
            (self.tip_type, SNAPSHOT_SCHEMA_VERSION, payload,
             canonical_payload_sha256(payload), FETCHED_AT),
        )
        record = load_snapshot(self.tip_type)
        self.assertEqual(record['type_key'], self.tip_type)
        self.assertEqual(record['schema_version'], SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(record['payload'], payload)
        self.assertEqual(record['fetched_at'], FETCHED_AT)
        # The row the run wrote is the row the endpoint publishes.
        body = self.served()
        self.assertEqual(body['type'], self.tip_type)
        self.assertEqual(body['count'], count)
        self.assertEqual(len(body['tips']), count)

    def test_the_same_payload_again_is_unchanged_and_only_restamps_the_row(self):
        payload = payload_for(self.tip_type)
        self.fetch.return_value = payload
        self.run_type()

        outcome = self.run_type(fetched_at=LATER_FETCHED_AT)

        self.assertEqual(outcome.state, 'unchanged')
        self.assertEqual(outcome.fetched_at, LATER_FETCHED_AT)
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(
            row_values(self.tip_type),
            (self.tip_type, SNAPSHOT_SCHEMA_VERSION, payload,
             canonical_payload_sha256(payload), LATER_FETCHED_AT),
        )

    def test_a_changed_payload_replaces_the_same_row_rather_than_adding_one(self):
        self.fetch.return_value = payload_for(self.tip_type)
        self.run_type()
        changed = match_payload('Inter vs Milan')
        self.fetch.return_value = changed

        outcome = self.run_type(fetched_at=LATER_FETCHED_AT)

        self.assertEqual(outcome.state, 'changed')
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(
            row_values(self.tip_type),
            (self.tip_type, SNAPSHOT_SCHEMA_VERSION, changed,
             canonical_payload_sha256(changed), LATER_FETCHED_AT),
        )
        self.assertEqual(self.served()['count'], 1)


    def test_a_refused_run_leaves_the_stored_row_exactly_as_it_was(self):
        self.fetch.return_value = payload_for(self.tip_type)
        self.run_type()
        before = row_values(self.tip_type)
        cases = (
            (RuntimeError(HOSTILE_TEXT), None, 'fetch_exception'),
            (None, unpinned_empty_payload(self.tip_type),
             'empty_success_envelope'),
        )

        for error, payload, reason in cases:
            with self.subTest(reason=reason):
                self.fetch.side_effect = error
                self.fetch.return_value = payload

                with mock.patch.object(refresh_v1.logger, 'error'):
                    outcome = self.run_type(fetched_at=LATER_FETCHED_AT)

                self.assertEqual(
                    outcome.line(),
                    'type=%s outcome=failed reason=%s' % (self.tip_type, reason),
                )
                self.assertEqual(row_values(self.tip_type), before)
                self.assertEqual(self.served()['count'], 1)

    def test_an_instant_the_store_cannot_stamp_is_refused_before_the_row(self):
        self.fetch.return_value = payload_for(self.tip_type)

        with mock.patch.object(refresh_v1.logger, 'error'):
            outcome = refresh_v1.refresh_type(
                self.tip_type, fetched_at=datetime(2026, 9, 28, 17, 12, 3))

        self.assertEqual(
            outcome.line(),
            'type=bet_of_the_day outcome=failed reason=store_rejected',
        )
        self.assertFalse(SnapshotV1.objects.exists())
        self.assertIsNone(load_snapshot(self.tip_type))

    def test_the_pinned_empty_envelope_is_the_row_served_for_no_tips(self):
        payload = pinned_empty_payload(self.tip_type)
        self.fetch.return_value = payload

        outcome = self.run_type()

        self.assertEqual(outcome.outcome, refresh_v1.OUTCOME_EMPTY)
        self.assertEqual(outcome.count, 0)
        self.assertEqual(
            row_values(self.tip_type),
            (self.tip_type, SNAPSHOT_SCHEMA_VERSION, payload,
             canonical_payload_sha256(payload), FETCHED_AT),
        )
        self.assertEqual(load_snapshot(self.tip_type)['payload'], payload)
        # A stored empty envelope is a 200 with no tips, not an unavailable source.
        body = self.served()
        self.assertEqual((body['count'], body['tips']), (0, []))

    def test_a_full_run_writes_one_sanitised_row_per_type(self):
        order = list(refresh_v1.REFRESH_TYPE_ORDER)
        self.fetch.side_effect = lambda type_key: with_volatile_keys(
            payload_for(type_key))

        with mock.patch.object(refresh_v1, '_now', return_value=FETCHED_AT):
            outcomes = refresh_v1.refresh_types()

        self.assertEqual([outcome.type_key for outcome in outcomes], order)
        self.assertEqual(
            list(SnapshotV1.objects.values_list('type_key', flat=True).order_by(
                'type_key')),
            sorted(order),
        )
        for tip_type in order:
            with self.subTest(tip_type=tip_type):
                stored = load_snapshot(tip_type)['payload']
                # The rows a client is served hold the source envelope and none of
                # the fetch layer's own bookkeeping.
                self.assertEqual(stored, payload_for(tip_type))
                for key in refresh_v1.VOLATILE_RESULT_KEYS:
                    self.assertNotIn(key, stored)
                body = self.served(tip_type)
                self.assertEqual(body['count'], published_counts(tip_type)[0])
                self.assertEqual(len(body['tips']), 1)
