"""The out-of-band writer that fills the v1 snapshot store.

Why this module exists
----------------------
The versioned endpoint answers only from a stored snapshot, and ``storage_v1``
implements that store, but nothing has ever *written* one: the request path reads
and never writes, which is what ``docs/API_V1_CONTRACT.md`` requires. This module
is the single writer. It is reachable only from the management command
``manage.py refresh_tips`` and never from a view, so a request still cannot start
a fetch, a scrape, a refresh or a cache fill.

The unit of work is one tip type. For each one this module fetches the source page
through the legacy fetch layer, decides whether the result may be published at
all, reads the type's current snapshot, and stores the payload through the seam
(``readmodel_v1.store_snapshot()``). Before any of those decisions is taken, the
result is stripped of the fetch layer's own bookkeeping, so what this module
digests, compares and stores is the source envelope alone. It never constructs a
provider, never imports the model, and never clears anything: which provider is
installed is the startup configuration's decision, and the seam stays the only
thing that writes a row.

Ground rules
------------
* **One refusal, one fixed reason token.** Every way this writer can decline to
  publish a type is named by a token from ``REASON_TOKENS``. No reason is built
  from an exception, a URL, a payload value, or a row, so a reason can be read in
  a log or a terminal without quoting anything the source supplied.
* **A refusal never writes.** A type is written only after its payload was
  fetched, was recognised as a publishable result, produced an envelope the
  serializer vouched for, and had its current state read. Every failure before
  that point aborts that type's write and leaves every stored row as it was;
  other types in the same run are unaffected.
* **A refusal is reported on one line and carries no values.** A failed outcome
  prints ``type=<key> outcome=failed reason=<token>`` and nothing else: no state,
  no counts, no timestamp. The failed outcome's own ``fetched_at`` is ``None``, so
  a failure can never be mistaken for a fetch that happened at a known instant.
* **A stored payload is the source envelope, without the fetch layer's own
  bookkeeping.** The fetch layer stamps every result with the instant it was
  scraped and the URL it was read from, and those two keys are removed at the top
  level of a deep copy before the payload is digested, compared or stored. A
  nested field of the same name belongs to the source and is kept, and the
  scraper's own result is never mutated.
* **A payload is checked against the shape its own parser states.** Each unit lists
  its entries in one collection and repeats their number in fixed fields, so a
  payload whose collection belongs to the other unit, whose entries are not
  mappings, or whose stated numbers disagree with the entries it lists is refused as
  malformed. The check reads the sanitized payload alone and is taken before the
  serializer, the comparison and the write, so a payload it refuses never reaches a
  stored row.
* **An empty payload is published only when it is the pinned one.** A card-free
  page is a real answer, and the frozen parsers state it as one pinned envelope per
  source key: that exact envelope is accepted, compared and stored, so the endpoint
  can answer a stored 200 with no tips. Any other payload that describes no tips is
  refused, because an empty page and a page this writer failed to read look the
  same to a reader and the stored row is the better answer to both.
* **State is compared, not trusted.** ``new`` means the seam had no record,
  ``unchanged`` means the stored payload digests identically to the fetched one,
  and ``changed`` means the two digests differ. An unchanged payload is still
  stored again with a fresh timestamp: a run publishes the freshest fetch, and the
  row's ``fetched_at`` describes that fetch rather than the previous one.
* **Only ordinary exceptions are converted.** Anything that is not an
  ``Exception`` (a ``KeyboardInterrupt``, for instance) propagates untouched, so a
  cancelled run is never reported as a source or store refusal.
* **One instant per accepted type.** Every type that reaches the write path reads
  the clock for itself, so a run's rows are stamped with the instant their own
  fetch was accepted instead of one instant taken when the run started. A type
  refused before that point reads no clock at all: a failed fetch, a result that is
  not a payload, a refusal from the classification, the raw payload check, the
  serializer, the digest or the state read is decided without one. A write the
  store refuses is the one later case, and the stamp it may already have taken
  reaches no report: every failed outcome carries ``fetched_at=None`` and prints no
  timestamp, so no refusal can be read as a fetch that happened.
* **A dry run decides everything and writes nothing.** With ``dry_run=True`` the
  fetch, the sanitisation, the serializer check, the digest and the state read all
  run, and the outcome reports the compared state with ``dry-run:`` in front of it;
  the store is never reached, so every stored row is untouched.

See ``docs/API_V1_CONTRACT.md`` for the client-facing contract and
``docs/DATA_CONTRACT.md`` §13 for the authoritative-UTC rule the stored timestamp
follows.
"""

import logging
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from types import MappingProxyType

from .readmodel_v1 import load_snapshot, store_snapshot
from .reporting_v1 import (
    MAX_TYPE_KEY_LENGTH,
    REPLACEMENT_CHARACTER,
    SAFE_TOKEN_EXTRA_CHARACTERS,
    safe_token,
)
from .serializers_v1 import (
    SUCCESS_ENVELOPE_KEYS,
    TIP_TYPE_UNITS,
    UNIT_CARD,
    UNIT_MATCH,
    UnknownTipType,
    format_utc_z,
    is_supported_tip_type,
    serialize_tips,
    unit_for,
)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# The one logger a refusal is reported through, so a reader can assert on the
# exact channel instead of the root logger.
LOGGER_NAME = 'alltips_scraper.refresh_v1'

logger = logging.getLogger(LOGGER_NAME)

# The order a full run refreshes in, and therefore the order its lines are printed
# in. It is the versioned registry's own key order rather than a second list, so
# the writer cannot drift from the serializers about which types exist, or reorder
# them. Membership is the registry's own ``SUPPORTED_TIP_TYPES``.
REFRESH_TYPE_ORDER = tuple(TIP_TYPE_UNITS)

# The outcomes a report distinguishes: a payload that was published, the pinned
# empty payload of a source key that was published, and a type that was refused.
OUTCOME_OK = 'ok'
OUTCOME_EMPTY = 'empty'
OUTCOME_FAILED = 'failed'

OUTCOME_TOKENS = (OUTCOME_OK, OUTCOME_EMPTY, OUTCOME_FAILED)

# How a fetched payload relates to the payload already stored for its type.
STATE_NEW = 'new'
STATE_UNCHANGED = 'unchanged'
STATE_CHANGED = 'changed'

STATE_TOKENS = (STATE_NEW, STATE_UNCHANGED, STATE_CHANGED)

# How a dry run reports the state it compared. Each token keeps the state it is
# about and prefixes it, so a dry-run line can never read as a state that a
# stored row actually holds.
DRY_RUN_PREFIX = 'dry-run:'
DRY_RUN_STATE_TOKENS = tuple(DRY_RUN_PREFIX + state for state in STATE_TOKENS)

# Why a type was refused. Each is a fixed token, so a line can name the failed
# step without quoting an exception, a URL, a payload value, or a row.
REASON_FETCH_FAILED = 'fetch_failed'
REASON_FETCH_EXCEPTION = 'fetch_exception'
REASON_UNKNOWN_SOURCE_KEY = 'unknown_source_key'
REASON_UNRECOGNIZED_ERROR_ENVELOPE = 'unrecognized_error_envelope'
REASON_NON_DICT_RESULT = 'non_dict_result'
REASON_EMPTY_SUCCESS_ENVELOPE = 'empty_success_envelope'
REASON_MALFORMED_SUCCESS_ENVELOPE = 'malformed_success_envelope'
REASON_SERIALIZER_REJECTED = 'serializer_rejected'
REASON_STATE_READ_FAILED = 'state_read_failed'
REASON_STORE_REJECTED = 'store_rejected'
REASON_STORE_FAILED = 'store_failed'

REASON_TOKENS = frozenset({
    REASON_FETCH_FAILED,
    REASON_FETCH_EXCEPTION,
    REASON_UNKNOWN_SOURCE_KEY,
    REASON_UNRECOGNIZED_ERROR_ENVELOPE,
    REASON_NON_DICT_RESULT,
    REASON_EMPTY_SUCCESS_ENVELOPE,
    REASON_MALFORMED_SUCCESS_ENVELOPE,
    REASON_SERIALIZER_REJECTED,
    REASON_STATE_READ_FAILED,
    REASON_STORE_REJECTED,
    REASON_STORE_FAILED,
})

# The two report lines, as templates. A failed line states the type and the reason
# and stops: a refusal has no state, no counts and no instant to report. An
# accepted line states the outcome, the state that was compared, the counts the
# payload itself states, and the instant its row holds — or, in a dry run, the
# instant it would have been stamped with. The legs field belongs to the ``card``
# unit only, where it is the total the cards state for their own legs.
FAILURE_LINE = 'type={type} outcome=failed reason={reason}'
SUCCESS_LINE = 'type={type} outcome={outcome} state={state} count={count}'
LEGS_FIELD = ' legs={legs}'
STAMP_FIELD = ' fetched_at={fetched_at}'

# The one log line, as a template: a type key and a reason token, nothing else.
FAILED_REFRESH_MESSAGE = 'v1 tip refresh refused (type=%s, reason=%s)'

# The fetch layer's error envelope shape. A result that states one of these is a
# fetch that did not happen rather than a payload this writer may publish.
ERROR_KEY = 'error'
STATUS_CODE_KEY = 'status_code'
FETCH_FAILURE_PREFIXES = ('Failed to fetch ', 'Exception fetching ')
UNKNOWN_KEY_PREFIX = 'Unknown scraper key:'

# The pinned "no cards" envelopes the frozen parsers return for a card-free page.
# They are empty results rather than failed fetches, so the error-envelope test
# must not swallow them: what may be published is decided by comparing the payload
# against ``EMPTY_ENVELOPES`` below, and by nothing else.
NO_TIPS_ERROR_TEXTS = frozenset({
    'No tip cards found',
    'No accumulator cards found',
})

# The fetch layer's own bookkeeping, stripped from every result before this module
# decides anything about it. The instant a page was scraped and the URL it was read
# from are facts about the request rather than about the tips, so a stored row
# keeps neither — and because both change on every fetch, keeping them would make
# every state comparison report ``changed``.
VOLATILE_RESULT_KEYS = ('scraped_at', 'source_url')

# The pinned empty envelope of each source key, exactly as the frozen parsers
# return it for a card-free page. Only an exact match is published: a source key
# answers for the shape and the wording its own parser produces, so one type's
# empty envelope can never stand in for another's. The ``match`` unit lists
# ``matches`` and the ``card`` unit lists ``accumulators``; both carry the pinned
# message and a count of zero, which is what lets the endpoint answer 200 with no
# tips instead of leaving the previous row in place forever.
EMPTY_ENVELOPES = MappingProxyType({
    'bet_of_the_day': MappingProxyType({
        'error': 'No tip cards found',
        'matches': [],
        'count': 0,
    }),
    'daily_accumulator': MappingProxyType({
        'error': 'No accumulator cards found',
        'accumulators': [],
        'count': 0,
    }),
    'over_25_goals': MappingProxyType({
        'error': 'No tip cards found',
        'accumulators': [],
        'count': 0,
    }),
    'both_teams_to_score': MappingProxyType({
        'error': 'No tip cards found',
        'accumulators': [],
        'count': 0,
    }),
    'btts_and_win': MappingProxyType({
        'error': 'No tip cards found',
        'accumulators': [],
        'count': 0,
    }),
    'anytime_goalscorer': MappingProxyType({
        'error': 'No tip cards found',
        'accumulators': [],
        'count': 0,
    }),
})

# Where each unit lists its entries, and the key a ``card`` entry uses to state how
# many legs it holds, so the report can state the counts the payload itself carries
# instead of a total this module worked out for it.
UNIT_COLLECTIONS = MappingProxyType({
    UNIT_MATCH: 'matches',
    UNIT_CARD: 'accumulators',
})
MATCHES_COUNT_KEY = 'matches_count'

# The key each unit's own parser repeats its entry count under, beside the
# ``count`` it states: a ``match`` parser counts the tips it listed, and a ``card``
# parser counts the legs all of its cards hold. Both are read only to check that a
# raw payload's own numbers describe the entries it lists, never to report from.
TOTAL_TIPS_KEY = 'total_tips'
TOTAL_ACCUMULATORS_KEY = 'total_accumulators'

# A reported type key is one token: the width that bounds it, the safe set it may
# hold, and the character everything outside that set becomes are owned by
# ``reporting_v1`` and imported above. They stay on this module's surface because
# ``refresh_v1.safe_token`` and the names beside it are how a caller has always
# named them, and because the reviewer in ``contentcheck_v1`` imports the very
# same objects - so one token means one thing in every report that prints one.


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class RefreshOutcome:
    """What one tip type's refresh produced, and the one line that reports it.

    An accepted outcome — ``ok`` for a payload that describes tips, ``empty`` for
    the pinned empty payload of its own source key — carries the state that was
    compared, the counts the payload itself states, and the instant its row was (or,
    in a dry run, would have been) stamped with. A refused outcome carries one
    reason token and nothing else: no state, no counts and no timestamp, so a
    refusal cannot be read as a fetch that happened at a known instant.
    """

    __slots__ = ('type_key', 'outcome', 'state', 'reason', 'count', 'legs',
                 'fetched_at')

    def __init__(self, type_key, *, outcome=OUTCOME_OK, state=None, reason=None,
                 count=None, legs=None, fetched_at=None):
        self.type_key = type_key
        self.outcome = outcome
        self.state = state
        self.reason = reason
        self.count = count
        self.legs = legs
        self.fetched_at = fetched_at

    @property
    def ok(self):
        """True when this type was accepted, whether or not it wrote a row.

        A pinned empty payload is accepted rather than refused: the endpoint answers
        200 for it, and a run that only published empties is a run that succeeded. A
        dry run wrote nothing, but it refused nothing either.
        """
        return self.outcome != OUTCOME_FAILED

    def line(self):
        """Return the single report line this outcome is summarised by."""
        type_token = safe_token(self.type_key)
        if not self.ok:
            return FAILURE_LINE.format(type=type_token, reason=self.reason)
        line = SUCCESS_LINE.format(
            type=type_token,
            outcome=self.outcome,
            state=self.state,
            count=self.count,
        )
        if self.legs is not None:
            line += LEGS_FIELD.format(legs=self.legs)
        return line + STAMP_FIELD.format(
            fetched_at=format_utc_z(self.fetched_at))

    def _values(self):
        return (self.type_key, self.outcome, self.state, self.reason, self.count,
                self.legs, self.fetched_at)

    def __eq__(self, other):
        if not isinstance(other, RefreshOutcome):
            return NotImplemented
        return self._values() == other._values()

    def __hash__(self):
        return hash(self._values())

    def __repr__(self):
        return f'RefreshOutcome({self.line()!r})'


def _now():
    """Return the current instant as an aware UTC datetime.

    The one place this module reads a clock, kept in one function so a test can
    stand in for it and a run's stamp is produced the same way every time. The
    value is aware UTC because that is the only value the store accepts, which is
    the rule ``docs/DATA_CONTRACT.md`` §13 states for an authoritative instant.
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The pipeline: one type, one fetch, one decision at a time
# ---------------------------------------------------------------------------

def _is_supported(type_key):
    """Return True when ``type_key`` is one of the versioned source keys.

    The predicate itself belongs to the versioned registry, so this writer asks
    that module rather than keeping a second opinion about which keys exist.
    """
    return is_supported_tip_type(type_key)


def sanitize_payload(result):
    """Return a deep copy of a fetch result without the fetch layer's own keys.

    The fetch layer stamps every result with ``scraped_at`` and ``source_url``.
    Both are facts about the request rather than about the tips: they change on
    every fetch, so a stored row that kept them would be reported as ``changed``
    forever, and neither is part of the payload the serializers publish. Only those
    two top-level keys are removed, and only from a copy, so what is digested,
    compared and stored is the source's own payload — including a nested field that
    happens to share either name — and the scraper's result is left exactly as it
    was handed over.

    A result that is not a mapping is refused instead of copied: this writer has no
    payload to sanitise, and inventing one is how something the source never said
    would reach a stored row.
    """
    if not isinstance(result, Mapping):
        raise TypeError(
            'sanitize_payload() requires a mapping, got '
            f'{type(result).__name__}'
        )
    sanitized = deepcopy(dict(result))
    for key in VOLATILE_RESULT_KEYS:
        sanitized.pop(key, None)
    return sanitized


def _fetch(type_key):
    """Fetch one source key through the legacy fetch layer.

    Returns ``(payload, None)`` when the fetch produced a result this writer may
    read, and ``(None, reason)`` when it did not. The fetch layer is imported
    inside the call rather than at module level: importing this writer must not
    pull in an HTTP client or an environment reader, and a test can stand in for
    the fetch at the module the fetch layer actually lives in.
    """
    from . import utils

    try:
        result = utils.scrape_one(type_key)
    except Exception:
        return None, REASON_FETCH_EXCEPTION
    if not isinstance(result, Mapping):
        return None, REASON_NON_DICT_RESULT
    return _classify(sanitize_payload(result))


def _classify(result):
    """Read one fetch result for the reason it cannot be published, if any.

    The fetch layer reports a failed request as an error envelope rather than by
    raising, so the envelope has to be told apart from a payload: a request that
    failed at the transport, an error this writer has no vocabulary for, and a
    source that does not know the key are each a refusal, while the pinned "no
    cards" envelope is passed on to the serializer, which is what decides whether
    a payload describes any tips at all.
    """
    if ERROR_KEY not in result:
        return result, None
    error_text = result[ERROR_KEY]
    if not isinstance(error_text, str):
        return None, REASON_UNRECOGNIZED_ERROR_ENVELOPE
    if STATUS_CODE_KEY in result or error_text.startswith(FETCH_FAILURE_PREFIXES):
        return None, REASON_FETCH_FAILED
    if error_text.startswith(UNKNOWN_KEY_PREFIX):
        return None, REASON_UNKNOWN_SOURCE_KEY
    if error_text in NO_TIPS_ERROR_TEXTS:
        return result, None
    return None, REASON_UNRECOGNIZED_ERROR_ENVELOPE


def _publication(type_key, payload):
    """Return ``(outcome, None)`` for a payload this writer may publish.

    The serializer is the only thing that decides what a payload holds, so it is
    consulted here rather than a second reader written in this module. Its answer is
    then checked for the shape this writer is willing to report on, and the outcome
    the payload implies is returned: ``ok`` for a payload that describes tips, and
    ``empty`` for the pinned empty envelope of this source key, which is the answer
    a card-free page really gives. Any other payload that holds no tips is refused,
    because a fetch that produced nothing must not replace a row that holds
    something: an empty page and a page this writer failed to read look the same to
    a reader, and the stored row is the better answer to both.
    """
    try:
        envelope = serialize_tips(type_key, payload)
    except Exception:
        return None, REASON_SERIALIZER_REJECTED
    if not _well_formed(envelope, type_key):
        return None, REASON_MALFORMED_SUCCESS_ENVELOPE
    if _is_pinned_empty(type_key, payload):
        return OUTCOME_EMPTY, None
    if envelope['count'] == 0:
        return None, REASON_EMPTY_SUCCESS_ENVELOPE
    return OUTCOME_OK, None


def _is_pinned_empty(type_key, payload):
    """Return True when a payload is the pinned empty envelope of its own key.

    The comparison is exact, because the pinned envelope is the only zero-count
    payload this writer publishes, and it is published for the key whose parser
    produces it and for no other key: one source's empty answer can never stand in
    for another's.
    """
    pinned = EMPTY_ENVELOPES.get(type_key)
    return pinned is not None and payload == pinned


def _raw_payload_refusal(type_key, payload):
    """Return why a raw sanitized payload may not be published, or ``None``.

    Each unit's parser states its entries and its counts in fixed fields, so this is
    the payload's own shape, read before anything is digested, compared or stored. A
    ``match`` payload lists its tips in ``matches`` and states the same non-zero
    number in ``count`` and ``total_tips``. A ``card`` payload lists its cards in
    ``accumulators``, states each card's own legs in ``matches_count``, how many
    cards there are in ``total_accumulators``, and the legs of all of them in
    ``count``. A collection of the wrong unit is not this unit's shape, and a payload
    whose stated numbers disagree with the entries it lists contradicts itself: both
    are refused as malformed, because neither can be reported from or stored as the
    answer its source gave.

    Only the pinned empty envelope of this type's own source key bypasses the
    non-empty rules: it is the one payload a frozen parser produces for a card-free
    page, and the publishability step publishes it as ``empty``. Any other payload
    that lists no entries of its own unit describes no tips, which is a refusal of
    its own kind rather than a malformed payload. Nothing here fetches, reads a
    clock, reads state or writes: the decision is taken on the payload alone, before
    the serializer, the comparison and the write are reached.
    """
    if _is_pinned_empty(type_key, payload):
        return None
    unit = unit_for(type_key)
    valid = (
        _match_payload_valid(payload) if unit == UNIT_MATCH
        else _card_payload_valid(payload)
    )
    if valid:
        return None
    entries = payload.get(UNIT_COLLECTIONS[unit])
    if isinstance(entries, list) and not entries:
        return REASON_EMPTY_SUCCESS_ENVELOPE
    return REASON_MALFORMED_SUCCESS_ENVELOPE


def _match_payload_valid(payload):
    """Return True when ``payload`` is the shape a non-empty ``match`` parser states.

    The tips are listed as mappings and counted three times over — by the list
    itself, by ``count`` and by ``total_tips`` — so a payload this writer may
    publish lists at least one tip and states that one number three times. A payload
    that lists tips but contradicts itself about how many is not a payload whose
    count can be reported: it is refused as malformed instead.
    """
    matches = payload.get(UNIT_COLLECTIONS[UNIT_MATCH])
    if not isinstance(matches, list):
        return False
    if not all(isinstance(entry, Mapping) for entry in matches):
        return False
    count = payload.get('count')
    total = payload.get(TOTAL_TIPS_KEY)
    if not _is_stated_count(count) or not _is_stated_count(total):
        return False
    return count == total == len(matches) and count >= 1


def _card_payload_valid(payload):
    """Return True when ``payload`` is the shape a non-empty ``card`` parser states.

    Every card is a mapping that lists its legs as mappings and states how many legs
    it holds, and the payload states how many cards there are and the legs of all of
    them. A card that holds no legs is not an accumulator, and a total that does not
    describe the entries it is made of is not a figure the source stated, so both
    are refused rather than reported from.
    """
    accumulators = payload.get(UNIT_COLLECTIONS[UNIT_CARD])
    if not isinstance(accumulators, list):
        return False
    if not all(isinstance(card, Mapping) for card in accumulators):
        return False
    legs = []
    for card in accumulators:
        matches = card.get(UNIT_COLLECTIONS[UNIT_MATCH])
        if not isinstance(matches, list):
            return False
        if not all(isinstance(leg, Mapping) for leg in matches):
            return False
        stated = card.get(MATCHES_COUNT_KEY)
        if not _is_stated_count(stated) or stated < 1 or stated != len(matches):
            return False
        legs.append(stated)
    count = payload.get('count')
    total = payload.get(TOTAL_ACCUMULATORS_KEY)
    if not _is_stated_count(count) or not _is_stated_count(total):
        return False
    if count != sum(legs) or total != len(accumulators):
        return False
    return count >= 1


def _is_stated_count(value):
    """Return True when ``value`` is a whole number a parser could have stated.

    ``True`` is an ``int`` in Python but states no quantity, so it is excluded here
    exactly as the serializer's own body check excludes it: a payload that states it
    states no count this writer may report or store.
    """
    return not isinstance(value, bool) and isinstance(value, int)


def _counts(type_key, payload):
    """Return the ``(count, legs)`` a payload states for the unit its key publishes.

    ``count`` is the number of entries the payload lists under the collection its
    unit publishes, and ``legs`` is the total the cards state for their own legs,
    which only the ``card`` unit has: for a ``match`` unit the second value is
    ``None``, and that is what keeps a ``legs=`` field out of its report line. Both
    values are read from the payload rather than from the serialized envelope, so
    the report states what the source said.
    """
    unit = unit_for(type_key)
    entries = payload.get(UNIT_COLLECTIONS[unit])
    if not isinstance(entries, list):
        return 0, None
    if unit == UNIT_MATCH:
        return len(entries), None
    return len(entries), sum(
        _stated_legs(entry) for entry in entries if isinstance(entry, Mapping)
    )


def _stated_legs(entry):
    """Return one card's own leg count, or ``0`` when it states no usable one.

    A card that says nothing about its legs contributes nothing to the total: this
    is a figure the source states, so a missing, negative or non-integer value is
    reported as no legs rather than guessed at.
    """
    value = entry.get(MATCHES_COUNT_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _well_formed(envelope, type_key):
    """Return True when a serialized body is one this writer can vouch for.

    The body must be a mapping that carries every key the versioned envelope
    publishes, must answer for the type that was asked for, must list its tips,
    and must state counts that agree with that list. A body that fails any of those
    is a serializer this writer cannot report on, and a body that reports legs but
    no tips contradicts its own counts.
    """
    if not isinstance(envelope, Mapping):
        return False
    if not SUCCESS_ENVELOPE_KEYS <= set(envelope):
        return False
    if envelope['type'] != type_key:
        return False
    tips = envelope['tips']
    count = envelope['count']
    legs_count = envelope['legs_count']
    if not isinstance(tips, list):
        return False
    if isinstance(count, bool) or not isinstance(count, int) or count != len(tips):
        return False
    if isinstance(legs_count, bool) or not isinstance(legs_count, int):
        return False
    if legs_count < 0 or (count == 0 and legs_count != 0):
        return False
    return True


def _canonical_sha256(payload):
    """Return the store's own canonical digest of ``payload``.

    Imported inside the call rather than at module level, so importing this writer
    still pulls in no ORM machinery and the digest rule stays stated in exactly one
    module: the store's.
    """
    from .storage_v1 import canonical_payload_sha256

    return canonical_payload_sha256(payload)


def _fingerprint(payload):
    """Return ``(digest, None)`` for a payload about to be written, else a refusal.

    A payload the store's canonical form cannot represent is reported as rejected
    rather than failing later: ``TypeError`` and ``ValueError`` are the store's own
    refusal kinds for a payload it cannot digest, so the write could not have
    succeeded had it been attempted.
    """
    try:
        return _canonical_sha256(payload), None
    except (TypeError, ValueError):
        return None, REASON_STORE_REJECTED
    except Exception:
        return None, REASON_STORE_FAILED


def _stored_fingerprint(payload):
    """Return the digest of an already stored payload, or ``None`` when it has none.

    ``None`` is only ever an answer to "this stored payload cannot be digested",
    and that simply makes the comparison unequal: a row the writer cannot recognise
    as equal to what it is holding is a changed row, not a reason to refuse a write
    that this payload's own digest already allowed.
    """
    try:
        return _canonical_sha256(payload)
    except Exception:
        return None


def _read_state(type_key, fingerprint):
    """Return ``(state, None)`` for a type's stored payload, else a refusal.

    The record comes from the seam, so it is the installed provider that decides
    what is stored, and the record's payload — not its digest — is what this module
    digests for itself. A state that cannot be read is a refusal of the whole type:
    a writer that cannot tell what is stored must not overwrite it.
    """
    try:
        record = load_snapshot(type_key)
        if record is None:
            return STATE_NEW, None
        stored_payload = record['payload']
    except Exception:
        return None, REASON_STATE_READ_FAILED
    if _stored_fingerprint(stored_payload) == fingerprint:
        return STATE_UNCHANGED, None
    return STATE_CHANGED, None


def _write(type_key, payload, fetched_at):
    """Store one payload through the seam, or return why the store refused it.

    The stored payload is the sanitised payload this module received, and the stamp
    is the instant its own fetch was accepted, so a row holds what the source said
    and when that answer was read. The store's own rules are the last word: a
    payload or a stamp the store refuses is reported by kind, and anything else it
    raises is reported as a store failure.
    """
    try:
        store_snapshot(type_key, payload, fetched_at=fetched_at)
    except (TypeError, ValueError):
        return REASON_STORE_REJECTED
    except Exception:
        return REASON_STORE_FAILED
    return None


# ---------------------------------------------------------------------------
# The writer path
# ---------------------------------------------------------------------------

def _refused(type_key, reason):
    """Report one refusal once, and return the outcome that names it.

    The log line is the operator's channel and the returned outcome is the report's
    line; both carry the sanitised type key and the fixed reason token and nothing
    else, so neither can leak an exception, a URL, or a payload value.
    """
    logger.error(FAILED_REFRESH_MESSAGE, safe_token(type_key), reason)
    return RefreshOutcome(type_key, outcome=OUTCOME_FAILED, reason=reason)


def refresh_type(type_key, *, fetched_at=None, dry_run=False):
    """Refresh one tip type, returning its outcome instead of raising.

    The steps run in a fixed order, and each may only refuse what it is responsible
    for: whether the key is a versioned source key, the fetch and the sanitisation
    of its result, whether the raw payload is the shape its unit publishes, whether
    the payload may be published, whether that payload can be digested, what is
    stored for the type today, and finally the write itself. A refusal at any step
    returns one failed outcome with one reason token, and the type's stored row is
    untouched.

    ``fetched_at`` is the instant the row is stamped with, and defaults to now in
    UTC. The clock is read for a type only once its payload has been accepted and
    its state read, so a refusal from the fetch, the classification, the raw payload
    check, the serializer, the digest or the state read is decided without one. A
    write the store refuses is the one later case: the stamp it may already have
    taken reaches no outcome, because a failed outcome carries ``fetched_at=None``
    and prints no timestamp, so no refusal can be read as a fetch that happened.

    ``dry_run`` stops the pipeline one step short of the write: every decision is
    still taken, the outcome reports the state that was compared with ``dry-run:``
    in front of it, and the store is never reached.
    """
    if not _is_supported(type_key):
        return _refused(type_key, REASON_UNKNOWN_SOURCE_KEY)

    payload, reason = _fetch(type_key)
    if reason is not None:
        return _refused(type_key, reason)

    reason = _raw_payload_refusal(type_key, payload)
    if reason is not None:
        return _refused(type_key, reason)

    outcome, reason = _publication(type_key, payload)
    if reason is not None:
        return _refused(type_key, reason)

    fingerprint, reason = _fingerprint(payload)
    if reason is not None:
        return _refused(type_key, reason)

    state, reason = _read_state(type_key, fingerprint)
    if reason is not None:
        return _refused(type_key, reason)

    stamp = _now() if fetched_at is None else fetched_at
    count, legs = _counts(type_key, payload)

    if dry_run:
        return RefreshOutcome(
            type_key,
            outcome=outcome,
            state=DRY_RUN_PREFIX + state,
            count=count,
            legs=legs,
            fetched_at=stamp,
        )

    reason = _write(type_key, payload, stamp)
    if reason is not None:
        return _refused(type_key, reason)

    return RefreshOutcome(
        type_key,
        outcome=outcome,
        state=state,
        count=count,
        legs=legs,
        fetched_at=stamp,
    )


# ---------------------------------------------------------------------------
# The run: which types, in which order, and each on its own instant
# ---------------------------------------------------------------------------

def resolve_type_keys(types=None):
    """Return the tip types a run refreshes, in the registry's own order.

    ``None`` selects the whole registry, and so does an empty selection: a caller
    that supplied no keys asked for no restriction, not for a run that refreshes
    nothing. A bare string is one key. Any other value is read as a sequence of
    requested keys, which are validated and de-duplicated: every value must be a
    string the registry publishes, and anything else raises ``UnknownTipType`` — a
    value that is not a string is refused before the registry is asked about it, and
    a key that was requested twice is refreshed once. The order is always
    ``REFRESH_TYPE_ORDER`` rather than the caller's, so a run cannot refresh its
    types in one order and report them in another.

    Nothing here fetches, reads or writes anything: validation is a decision about
    the request, taken before the first I/O of the run, so a mistyped key costs no
    request and touches no row.
    """
    if types is None:
        return REFRESH_TYPE_ORDER
    if isinstance(types, str):
        types = (types,)
    requested = []
    for type_key in types:
        if not isinstance(type_key, str) or not _is_supported(type_key):
            raise UnknownTipType('unsupported tip type')
        if type_key not in requested:
            requested.append(type_key)
    if not requested:
        # An empty selection is no selection: it asked for no restriction.
        return REFRESH_TYPE_ORDER
    return tuple(key for key in REFRESH_TYPE_ORDER if key in requested)


def refresh_types(type_keys=None, *, dry_run=False):
    """Refresh every selected type once, in order, and return their outcomes.

    The selection is resolved by ``resolve_type_keys()``, so a key the registry does
    not publish is refused before anything is fetched and the order is always the
    registry's own. Each accepted type reads the clock for itself, so a run's rows
    are stamped with the instant their own fetch was accepted instead of one instant
    taken when the run started.

    A refusal affects only the type it belongs to: every later selected type is
    still refreshed, so one failing source cannot hide the state of the others.
    """
    return [
        refresh_type(type_key, dry_run=dry_run)
        for type_key in resolve_type_keys(type_keys)
    ]
