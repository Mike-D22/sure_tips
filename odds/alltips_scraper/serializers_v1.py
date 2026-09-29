"""Pure serializers for the versioned tips API (``/api/v1/tips/``).

Why this module exists
----------------------
The frozen legacy parsers emit one payload shape per source kind (see
``docs/DATA_CONTRACT.md`` §3) under legacy key names. The versioned API needs a
stable, self-describing envelope that can outlive the scraper payload, so this
module is the single mapping layer between the two: it renames the source display
text (``date``/``time`` -> ``source_date_text``/``source_time_text``), names the
unit of each tip (``match`` or ``card``), and drops the volatile fetch metadata
the parser never produces.

Ground rules
------------
* **Standard library only.** This module may not import a web framework, settings,
  a cache backend, the scraper module, an HTTP client, or an environment reader.
  It therefore has no configuration and no I/O: importing it can never require a
  ``.env`` file and can never reach the network. ``tests_api_v1.py`` parses this
  file and asserts that property.
* **No invented values.** Source-derived values are preserved verbatim except for
  documented key renames. The v1 contract may add fixed structural fields and
  explicit unavailable-value markers; it never infers a real-world value that the
  payload did not provide. A value the payload does not carry becomes an empty
  string, an empty list, or ``None`` — never a guess.
* **Provenance is quoted, not interpreted.** ``source.label`` and
  ``source.date_text`` are read from the top-level payload only and published
  verbatim. ``date_text`` is never parsed, never normalised, never converted to
  UTC, and never taken from a match, a card, a leg, or the nested
  ``source_date_text`` display text; deciding what date it names belongs to the
  read model and the filter.
* **No data access of any kind.** This module does not fetch, does not read a
  snapshot, does not consult a clock, and does not filter. Callers pass a payload
  in and get a dict out.

Where the snapshot date rule will land
--------------------------------------
Filtering a snapshot by date, and the request-time validation around it, is the
job of the view layer (Commit 3) and of the read model (Commit 2); none of it
belongs here. When it lands, the rule for deciding whether a snapshot has a
usable date is:

    A snapshot date is available only when source_date_text is exactly a strict
    ISO calendar date in YYYY-MM-DD form and datetime.date.fromisoformat()
    accepts it. Otherwise filter.available_date is null.

The optional ``filter_block`` argument below only carries that decision into the
envelope once a caller has made it. This module never computes it.

Dependency direction
--------------------
This is the bottom of the stack: it imports no application module, and no
application module may borrow a formatter from it. The read model will later hold
and normalise timezone-aware UTC ``datetime`` objects only. Turning such a value
into the public wire format ``YYYY-MM-DDTHH:MM:SSZ`` is done by ``format_utc_z()``
below and nowhere else, so the read model neither imports this module nor reuses
that helper.

See ``docs/DATA_CONTRACT.md`` §13.3-13.4 for the source-text naming rule and
§13.5 for the display-text shape.
"""

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from types import MappingProxyType


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

API_VERSION = 'v1'

# What one entry in ``tips`` represents. ``match`` is a single selection;
# ``card`` is a source card (an accumulator) whose selections live in ``legs``.
UNIT_MATCH = 'match'
UNIT_CARD = 'card'

# The v1 registry. It is deliberately local to the versioned layer instead of
# reading the legacy scraper configuration table: the public serializers must not
# depend on the scraper module, its import-time configuration, or its payload
# shape. The test module still asserts, from the framework-configured test layer,
# that this key set and the legacy ``is_accumulator`` flags agree, so the two
# cannot drift apart silently.
TIP_TYPE_UNITS = MappingProxyType({
    'bet_of_the_day': UNIT_MATCH,
    'daily_accumulator': UNIT_CARD,
    'over_25_goals': UNIT_CARD,
    'both_teams_to_score': UNIT_CARD,
    'btts_and_win': UNIT_CARD,
    'anytime_goalscorer': UNIT_CARD,
})

SUPPORTED_TIP_TYPES = frozenset(TIP_TYPE_UNITS)

# The only legacy keys this layer renames. The source display text is not a
# timestamp and is never converted (``docs/DATA_CONTRACT.md`` §13.3).
SOURCE_TEXT_KEYS = MappingProxyType({
    'date': 'source_date_text',
    'time': 'source_time_text',
})

# The top-level legacy payload key the provenance block's ``date_text`` is read
# from. That is the payload's own date, not a selection's: the nested
# ``date``→``source_date_text`` rename above is a different rule. The value is
# published exactly as the payload states it — never parsed, never normalised,
# never converted to UTC.
SOURCE_DATE_KEY = 'date'

# Which payload list feeds ``tips``, per unit.
_COLLECTION_KEY_FOR_UNIT = MappingProxyType({
    UNIT_MATCH: 'matches',
    UNIT_CARD: 'accumulators',
})

# Temporal contract markers. The source publishes a kickoff as display text only,
# so every mapped leg — and therefore every ``match`` tip built from one — carries
# these two field names with their fixed "no kickoff available" values. They are
# structural markers, not data: this module never parses that display text, never
# computes a kickoff, never applies a timezone, and never derives an offset.
KICKOFF_AT = 'kickoff_at'
KICKOFF_TIME_VERIFIED = 'kickoff_time_verified'

# ``None`` means the source provided no kickoff; ``False`` means no kickoff time
# has been verified. One mapping, so the pair cannot drift apart or be re-typed.
KICKOFF_MARKERS = MappingProxyType({
    KICKOFF_AT: None,
    KICKOFF_TIME_VERIFIED: False,
})

KICKOFF_MARKER_KEYS = frozenset(KICKOFF_MARKERS)

MATCH_UNIT_TIP_KEYS = frozenset({
    'match_title', 'teams', 'prediction', 'opponent_text', 'tip_reason',
    'match_url', 'source_date_text', 'source_time_text', 'category', 'stake',
    'returns', 'odds', KICKOFF_AT, KICKOFF_TIME_VERIFIED,
})

LEG_KEYS = frozenset({
    'match_title', 'teams', 'prediction', 'opponent_text', 'tip_reason',
    'match_url', 'source_date_text', 'source_time_text',
    KICKOFF_AT, KICKOFF_TIME_VERIFIED,
})

CARD_UNIT_TIP_KEYS = frozenset({
    'category', 'stake', 'returns', 'total_odds', 'legs', 'legs_count',
})

# Envelope keys. ``filter`` is optional here only because making the decision it
# describes is not this module's job (see the module docstring).
SUCCESS_ENVELOPE_KEYS = frozenset({
    'api_version', 'type', 'unit', 'count', 'legs_count', 'tips', 'source',
})

OPTIONAL_ENVELOPE_KEYS = frozenset({'filter'})

# ---------------------------------------------------------------------------
# Error vocabulary
# ---------------------------------------------------------------------------

ERROR_MISSING_TIP_TYPE = 'missing_tip_type'
ERROR_UNKNOWN_TIP_TYPE = 'unknown_tip_type'
ERROR_INVALID_DATE = 'invalid_date'
ERROR_TIMEZONE_REQUIRES_DATE = 'timezone_requires_date'
ERROR_MISSING_TIMEZONE = 'missing_timezone'
ERROR_INVALID_TIMEZONE = 'invalid_timezone'
ERROR_SOURCE_UNAVAILABLE = 'source_unavailable'

# Client-safe messages, pinned here so no caller can interpolate a host, an
# upstream URL, or an exception string into a response body.
ERROR_MESSAGES = MappingProxyType({
    ERROR_MISSING_TIP_TYPE: 'the type query parameter is required',
    ERROR_UNKNOWN_TIP_TYPE: 'the type query parameter is not a supported tip type',
    ERROR_INVALID_DATE: 'date must be an ISO calendar date in YYYY-MM-DD form',
    ERROR_TIMEZONE_REQUIRES_DATE: 'timezone is only accepted together with date',
    ERROR_MISSING_TIMEZONE: 'timezone is required when date is supplied',
    ERROR_INVALID_TIMEZONE: 'timezone must be a valid IANA timezone name',
    ERROR_SOURCE_UNAVAILABLE: 'tip data is temporarily unavailable',
})

ERROR_CODES = frozenset(ERROR_MESSAGES)


class UnknownTipType(ValueError):
    """Raised when a tip type is not in the v1 registry."""


class UnknownErrorCode(ValueError):
    """Raised when an error code is not one of the documented v1 codes."""


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def format_utc_z(value: datetime) -> str:
    """Serialise an aware datetime as ISO-8601 UTC with a ``Z`` suffix.

    Seconds precision, no fractional part, never ``+00:00``: the versioned API
    has exactly one spelling for an authoritative UTC timestamp, and this function
    is the only place that produces it. The read model stores aware ``datetime``
    objects and neither calls this helper nor imports this module. A naive value
    is rejected rather than assumed to be UTC, because assuming is how a local
    wall-clock reading becomes a documented lie.
    """
    if not isinstance(value, datetime):
        raise TypeError(
            f'format_utc_z() requires a datetime, got {type(value).__name__}'
        )
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            'format_utc_z() requires a timezone-aware datetime; '
            'refusing to assume UTC for a naive value'
        )
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Type registry lookup
# ---------------------------------------------------------------------------

def is_supported_tip_type(tip_type) -> bool:
    """Return True when ``tip_type`` is one of the six versioned source keys.

    Total by design: an unhashable value is simply not supported, so a caller can
    use this predicate directly on anything a request can carry.
    """
    try:
        return tip_type in SUPPORTED_TIP_TYPES
    except TypeError:
        return False


def unit_for(tip_type) -> str:
    """Return the published unit for a tip type, or raise ``UnknownTipType``."""
    try:
        unit = TIP_TYPE_UNITS[tip_type]
    except (KeyError, TypeError) as exc:
        raise UnknownTipType(f'unsupported tip type: {tip_type!r}') from exc
    return unit


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------

def api_error(code: str, *, field: str | None = None) -> dict:
    """Build the versioned error body for a documented error code.

    An undocumented code raises instead of being serialised, so a sloppy caller
    cannot publish an ad-hoc error code or paste an exception string into a
    response body.
    """
    message = ERROR_MESSAGES.get(code)
    if message is None:
        raise UnknownErrorCode(f'undocumented v1 error code: {code!r}')
    return {
        'api_version': API_VERSION,
        'error': {
            'code': code,
            'message': message,
            'field': field,
        },
    }


# ---------------------------------------------------------------------------
# Payload mapping
# ---------------------------------------------------------------------------

def _text(value) -> str:
    """Return a source text value, or ``''`` when the payload carries none."""
    if value is None:
        return ''
    return value


def _teams(value):
    """Return a team list, or ``[]`` when the payload carries none."""
    if isinstance(value, list):
        return value
    return []


def _entries(value) -> list:
    """Return the mapping entries of a payload list, ignoring anything else."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


def _source_label(payload) -> str | None:
    """Return the payload's source label text, or ``None`` when it states none.

    ``None`` — rather than a constant — is used because the versioned API must
    never claim a provenance the payload did not state: a legacy error envelope
    carries no source at all, and filling in a source name there would publish a
    fabricated fact. The public key this feeds is ``label``.
    """
    if not isinstance(payload, Mapping):
        return None
    label = payload.get('source')
    if isinstance(label, str) and label:
        return label
    return None


def _source_date_text(payload) -> str | None:
    """Return the payload's own date text, or ``None`` when it states none.

    Only a non-empty string is published: a missing ``date`` key, ``None``, ``''``
    and every non-text value become ``None``, because the versioned API must not
    claim a date the payload did not state. The text is quoted exactly as it
    arrived — parsing, normalising, or converting it is not this layer's job (see
    the module docstring), and the public key this feeds is ``date_text``.
    """
    if not isinstance(payload, Mapping):
        return None
    date_text = payload.get(SOURCE_DATE_KEY)
    if isinstance(date_text, str) and date_text:
        return date_text
    return None


def _with_source_text(mapped: dict, entry: Mapping) -> dict:
    """Copy the renamed source display text onto an already mapped entry."""
    for legacy_key, v1_key in SOURCE_TEXT_KEYS.items():
        mapped[v1_key] = _text(entry.get(legacy_key))
    return mapped


def _mapped_leg(entry: Mapping) -> dict:
    """Map one legacy selection entry, keeping its source values verbatim.

    The temporal markers are attached here rather than by each caller, so the
    ``legs`` of a card unit and the tips of a ``match`` unit cannot drift apart.
    """
    leg = {
        'match_title': _text(entry.get('match_title')),
        'teams': _teams(entry.get('teams')),
        'prediction': _text(entry.get('prediction')),
        'opponent_text': _text(entry.get('opponent_text')),
        'tip_reason': _text(entry.get('tip_reason')),
        'match_url': _text(entry.get('match_url')),
    }
    leg = _with_source_text(leg, entry)
    leg.update(KICKOFF_MARKERS)
    return leg


def _match_unit_tip(entry: Mapping) -> dict:
    """Map one legacy tip-card selection to a ``match`` tip.

    The card-level values the legacy parser repeats onto every selection
    (``docs/DATA_CONTRACT.md`` §9.6) keep one name here: ``category`` for
    ``tip_category``, plus the card's ``stake``/``returns``/``odds``. The legacy
    ``total_odds`` spelling is not used for this unit.
    """
    tip = _mapped_leg(entry)
    tip['category'] = _text(entry.get('tip_category'))
    tip['stake'] = entry.get('stake')
    tip['returns'] = entry.get('returns')
    tip['odds'] = entry.get('odds')
    return tip


def _card_unit_tip(entry: Mapping) -> dict:
    """Map one legacy accumulator card to a ``card`` tip with nested legs.

    ``tip_type`` is copied only when the legacy card carries it, which is exactly
    the generic sources; the daily accumulator has no such concept, and an
    invented ``null`` would be a field the source never had.
    """
    legs = [_mapped_leg(leg) for leg in _entries(entry.get('matches'))]
    card = {
        'category': _text(entry.get('category')),
        'stake': entry.get('stake'),
        'returns': entry.get('returns'),
        'total_odds': entry.get('total_odds'),
        'legs': legs,
        'legs_count': len(legs),
    }
    if 'tip_type' in entry:
        card['tip_type'] = _text(entry.get('tip_type'))
    return card


def serialize_tips(
    tip_type: str,
    payload,
    *,
    fetched_at: datetime | None = None,
    filter_block: dict | None = None,
) -> dict:
    """Map a parser payload to the v1 success envelope.

    ``fetched_at`` is an aware datetime describing when the snapshot being
    serialised was read; it is serialised as ``source.fetched_at`` and omitted
    entirely when the caller has no snapshot to describe.

    The ``source`` block always carries the payload's own provenance — ``label``
    and ``date_text`` — and the top-level payload is the only place either is read
    from. ``date_text`` is the payload's ``date`` text quoted verbatim: this
    function never parses it, never normalises it, never converts it to UTC, and
    never derives it from a tip, a leg, or a nested ``source_date_text``. A
    payload that states no usable date gets ``date_text: null``.

    ``filter_block`` is an optional, already-decided filter description. It is
    carried through untouched (defensively copied); this module never inspects
    it, never normalises a date inside it, and never derives it.

    A payload that describes no tips — including a legacy "no cards" error
    payload — maps to an envelope with ``tips: []``, ``count: 0`` and
    ``legs_count: 0``. The legacy error key and its message text are dropped: an
    empty result is a result, not an error, and the pinned legacy wording is not
    part of the versioned contract.
    """
    unit = unit_for(tip_type)

    collection = None
    if isinstance(payload, Mapping):
        collection = payload.get(_COLLECTION_KEY_FOR_UNIT[unit])
    entries = _entries(collection)

    if unit == UNIT_MATCH:
        tips = [_match_unit_tip(entry) for entry in entries]
        legs_count = 0
    else:
        tips = [_card_unit_tip(entry) for entry in entries]
        legs_count = sum(len(tip['legs']) for tip in tips)

    # The public provenance block is exactly ``label`` plus the payload's own date
    # text, and both are read from the top-level payload only — never from a match,
    # a card, a leg, or a leg's ``source_date_text``. The fetch stamp is added last,
    # and only when the caller has a snapshot to describe.
    source = {
        'label': _source_label(payload),
        'date_text': _source_date_text(payload),
    }
    if fetched_at is not None:
        source['fetched_at'] = format_utc_z(fetched_at)

    envelope = {
        'api_version': API_VERSION,
        'type': tip_type,
        'unit': unit,
        'count': len(tips),
        'legs_count': legs_count,
        'tips': tips,
        'source': source,
    }
    if filter_block is not None:
        envelope['filter'] = deepcopy(filter_block)
    return envelope
