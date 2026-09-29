"""The offline, versioned tips endpoint (``GET /api/v1/tips/``).

What this module is
-------------------
One view plus the helpers it needs. It reads the three documented query
parameters, decides the ``filter`` block, reads a snapshot through the readmodel
seam, and publishes the versioned envelope the serializers build. It owns no tip
vocabulary of its own, and it never invents a value the snapshot did not state.

Ground rules
------------
* **Offline, always.** The one data source is ``readmodel_v1.load_snapshot()``.
  This module imports no scraper module, no parser, no handler, no cache helper
  and no HTTP client; it can never trigger a fetch or a cache refresh; and it
  never writes. Only the read half of the seam is imported, no provider is
  installed, and the record the seam hands back is only read.
* **The query is validated before the snapshot is read.** The validation order is
  part of the public contract, so a query that can be answered with a ``400``
  never touches the seam: a malformed query can never report source availability.
  A method other than ``GET`` is refused before either happens.
* **A date parameter is a snapshot-availability filter, not a time window.** The
  requested date is accepted only in strict ``YYYY-MM-DD`` form, is compared for
  equality against the snapshot's own date text, and nothing else is computed from
  it: no UTC window, no DST logic, no date arithmetic, no offset arithmetic.
* **A timezone parameter is validated, never applied.** Loadability through
  ``zoneinfo.ZoneInfo`` is the whole requirement; the loaded zone is discarded,
  Django's server time zone is not consulted, and no zone is activated. The name
  is only echoed back inside the ``filter`` block.
* **A snapshot that is absent, or that is not a mapping, is one and the same
  client-safe answer**, and an exception raised by the installed provider is
  deliberately not turned into that answer: it stays the provider's own failure.
* **Errors are the serializers' own vocabulary.** Every error body comes from
  ``api_error(code, field=...)``, so no host, upstream URL, exception text or
  traceback can reach a client, and an error body carries no ``tips``, ``source``
  or ``filter`` block.

See ``docs/DATA_CONTRACT.md`` §13 for the timezone rules and §12 for the change
rules this endpoint is additive under.
"""

import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .readmodel_v1 import load_snapshot
from .serializers_v1 import (
    ERROR_INVALID_DATE,
    ERROR_INVALID_TIMEZONE,
    ERROR_MISSING_TIMEZONE,
    ERROR_MISSING_TIP_TYPE,
    ERROR_SOURCE_UNAVAILABLE,
    ERROR_TIMEZONE_REQUIRES_DATE,
    ERROR_UNKNOWN_TIP_TYPE,
    SOURCE_DATE_KEY,
    api_error,
    is_supported_tip_type,
    serialize_tips,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Query vocabulary
# ---------------------------------------------------------------------------

# The whole public query surface. A parameter that is not named here is ignored,
# and a duplicated parameter uses QueryDict's final value.
QUERY_TIP_TYPE = 'type'
QUERY_DATE = 'date'
QUERY_TIMEZONE = 'timezone'
QUERY_PARAMETERS = (QUERY_TIP_TYPE, QUERY_DATE, QUERY_TIMEZONE)

# ---------------------------------------------------------------------------
# Filter vocabulary
# ---------------------------------------------------------------------------

FILTER_DATE = 'date'
FILTER_TIMEZONE = 'timezone'
FILTER_APPLIED = 'applied'
FILTER_MATCHED = 'matched'
FILTER_AVAILABLE_DATE = 'available_date'
FILTER_KEYS = (
    FILTER_DATE,
    FILTER_TIMEZONE,
    FILTER_APPLIED,
    FILTER_MATCHED,
    FILTER_AVAILABLE_DATE,
)

# ---------------------------------------------------------------------------
# Snapshot payload keys
# ---------------------------------------------------------------------------

# The frozen legacy top-level key carrying the source label. Its public v1
# spelling is ``source.label``, and only ``serializers_v1.py`` creates that key:
# this module merely re-supplies the legacy value the serializers read.
LEGACY_SOURCE_KEY = 'source'

# A date is accepted only in this exact spelling. ``date.fromisoformat()`` alone
# is too permissive on Python 3.11+: it also reads ``20260927`` and
# ``2026-W39-1``, neither of which is the documented request format.
STRICT_DATE_PATTERN = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def _strict_date(value):
    """Return ``value`` when it is exactly a strict ISO ``YYYY-MM-DD`` date.

    The spelling check runs first, and ``date.fromisoformat()`` second, which is
    what rejects a well-spelled but impossible day such as ``2026-02-30``. Only
    the text is returned: the value is never parsed into a date for use, and no
    window, offset, or arithmetic is derived from it.
    """
    if not isinstance(value, str):
        return None
    if STRICT_DATE_PATTERN.fullmatch(value) is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _loadable_timezone(name):
    """Return ``True`` when ``name`` is an IANA zone the database can load.

    The loaded zone is deliberately discarded: this endpoint validates the
    supplied name and echoes it, and never converts, activates or computes with
    it. An unknown zone and an unusable key are the same answer.
    """
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _snapshot_available_date(payload):
    """Return the snapshot's usable date text, or ``None`` when it has none.

    Only the top-level payload date text is considered, and only in the same
    strict ``YYYY-MM-DD`` form a request must use. A nested tip, leg or card, a
    nested source date text, a source time, a match time, or a clock is never a
    source for this value, and nothing is parsed or inferred from it.
    """
    if not isinstance(payload, Mapping):
        return None
    return _strict_date(payload.get(SOURCE_DATE_KEY))


def _snapshot_fetched_at(record):
    """Return the record's fetch stamp when it is an aware datetime, else None.

    ``readmodel_v1`` documents ``fetched_at`` as an aware UTC datetime and the
    default provider keeps that promise. A provider that hands back a missing,
    non-datetime or naive stamp must not be able to turn a read into a server
    error or publish an unformattable value, so the envelope simply omits
    ``source.fetched_at`` — exactly as ``serialize_tips()`` does when no snapshot
    stamp is described. No stamp is invented and no detail about the record is
    reported.
    """
    fetched_at = record.get('fetched_at')
    if not isinstance(fetched_at, datetime):
        return None
    if fetched_at.tzinfo is None:
        return None
    if fetched_at.tzinfo.utcoffset(fetched_at) is None:
        return None
    return fetched_at


def _provenance_payload(payload):
    """Return a payload carrying the snapshot's provenance and no tips.

    This is how an empty result is published without touching stored state: the
    serializers read the provenance from the top-level payload only, so a payload
    that keeps just those two frozen legacy keys maps to ``count: 0``,
    ``legs_count: 0`` and ``tips: []`` while the public provenance, type and unit
    stay exactly as they were. Nothing is mutated and nothing is parsed.
    """
    if not isinstance(payload, Mapping):
        return {}
    keys = (SOURCE_DATE_KEY, LEGACY_SOURCE_KEY)
    return {key: payload[key] for key in keys if key in payload}


def _filter_block(*, date_text, timezone_text, applied, matched, available_date):
    """Return the published ``filter`` block, in the documented key order.

    ``applied`` states whether a date filter was applied at all, and ``matched``
    is ``None`` until one is. The echoed strings are the validated request values
    verbatim, never rewritten; ``available_date`` is the snapshot's own usable
    date text, or ``None`` when the snapshot states none.
    """
    return {
        FILTER_DATE: date_text,
        FILTER_TIMEZONE: timezone_text,
        FILTER_APPLIED: applied,
        FILTER_MATCHED: matched,
        FILTER_AVAILABLE_DATE: available_date,
    }


def _unavailable_response(tip_type, *, date_filtered, record_present):
    """Return the client-safe ``503`` for a snapshot that cannot be used."""
    logger.warning(
        'no usable v1 snapshot for the requested tip type '
        '(type=%s, date_filtered=%s, record_present=%s)',
        tip_type,
        date_filtered,
        record_present,
    )
    return JsonResponse(api_error(ERROR_SOURCE_UNAVAILABLE), status=503)


@require_GET
def tips_v1(request):
    """Answer ``GET /api/v1/tips/`` from a stored snapshot, or refuse the query.

    The query is validated in the documented order, and completely, before the
    snapshot is read: a malformed query is always a ``400`` and can never report
    source availability. A valid query with no usable snapshot is a ``503``; a
    valid query with a snapshot is a ``200``; and a date filter the snapshot does
    not satisfy is an empty ``200`` rather than an error, because an empty result
    is a result.
    """
    query = request.GET

    tip_type = query.get(QUERY_TIP_TYPE)
    if tip_type is None:
        return JsonResponse(
            api_error(ERROR_MISSING_TIP_TYPE, field=QUERY_TIP_TYPE), status=400)
    if not is_supported_tip_type(tip_type):
        return JsonResponse(
            api_error(ERROR_UNKNOWN_TIP_TYPE, field=QUERY_TIP_TYPE), status=400)

    has_date = QUERY_DATE in query
    has_timezone = QUERY_TIMEZONE in query
    requested_date = None
    requested_timezone = None

    if has_date:
        requested_date = _strict_date(query[QUERY_DATE])
        if requested_date is None:
            return JsonResponse(
                api_error(ERROR_INVALID_DATE, field=QUERY_DATE), status=400)
    if has_timezone and not has_date:
        return JsonResponse(
            api_error(ERROR_TIMEZONE_REQUIRES_DATE, field=QUERY_TIMEZONE),
            status=400,
        )
    if has_date and not has_timezone:
        return JsonResponse(
            api_error(ERROR_MISSING_TIMEZONE, field=QUERY_TIMEZONE), status=400)
    if has_timezone:
        requested_timezone = query[QUERY_TIMEZONE]
        if not _loadable_timezone(requested_timezone):
            return JsonResponse(
                api_error(ERROR_INVALID_TIMEZONE, field=QUERY_TIMEZONE),
                status=400,
            )

    record = load_snapshot(tip_type)
    if not isinstance(record, Mapping):
        return _unavailable_response(
            tip_type,
            date_filtered=has_date,
            record_present=record is not None,
        )

    payload = record.get('payload')
    available_date = _snapshot_available_date(payload)

    matched = None
    mapped_payload = payload
    if has_date:
        matched = available_date is not None and available_date == requested_date
        if not matched:
            mapped_payload = _provenance_payload(payload)

    envelope = serialize_tips(
        tip_type,
        mapped_payload,
        fetched_at=_snapshot_fetched_at(record),
        filter_block=_filter_block(
            date_text=requested_date,
            timezone_text=requested_timezone,
            applied=has_date,
            matched=matched,
            available_date=available_date,
        ),
    )
    return JsonResponse(envelope)
