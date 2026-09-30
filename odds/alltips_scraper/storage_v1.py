"""The durable snapshot provider for the versioned tips API (``/api/v1/tips/``).

Why this module exists
----------------------
``readmodel_v1`` publishes the snapshot seam and one process-local provider, so a
snapshot lives exactly as long as the process that holds it. The versioned
endpoint is served from a snapshot that a fetch wrote once, so that snapshot has
to outlive the process: this module is the provider that keeps it in the database
row ``models.SnapshotV1`` describes, and ``apps.AlltipsScraperConfig.ready()``
installs it at startup. The seam's signature does not change, so nothing that
reads through ``load_snapshot()`` notices which provider is installed.

Ground rules
------------
* **One row per type key, and only rows this provider writes.** ``type_key`` is
  the primary key, so a store is an upsert: a second store of the same key
  replaces that one row and cannot touch another.
* **The digest is the row's own claim about its payload.** Every write stores the
  digest of the payload exactly as it is stored, and every read recomputes that
  digest before the payload is handed on. A row whose payload no longer matches
  its own digest is refused rather than published.
* **A stored row is either usable or absent.** A read validates the row's record
  version, digest, payload shape and timestamp, and answers ``None`` for a row
  that fails any of them — the same answer as "no row at all". ``None`` is
  already what the seam and the endpoint treat as "no snapshot".
* **A refusal is reported, never published.** A refused or unreadable row is
  logged once, at ``ERROR``, through ``LOGGER_NAME``, and the line carries the
  type key and a fixed reason token: never the payload, the digest, the exception
  text, or an upstream detail. The client sees the endpoint's existing
  client-safe answer, so a broken row cannot become a ``500``.
* **A read never raises for stored state.** A table that cannot be read at all is
  a refusal like any other, so a locked database degrades to "no snapshot".
  A key the storage itself cannot accept (an unhashable one) still raises from
  the query, exactly as it does for the default provider.
* **Copies, never aliases.** The payload is deep copied on the way in and the
  record is deep copied on the way out, so stored state is reachable only through
  this provider's own methods.
* **Writes refuse instead of guessing.** A payload that is not a mapping and a
  timestamp that is not an aware ``datetime`` are refused with the same
  ``TypeError`` and ``ValueError`` the default provider raises, and a refused
  write touches no row. An omitted timestamp is the one case the clock is read:
  it is taken as aware UTC, which is the rule ``docs/DATA_CONTRACT.md`` §13 states
  for an authoritative instant.

See ``docs/DATA_CONTRACT.md`` §13 for the authoritative-UTC storage rule.
"""

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone

from django.db import DatabaseError, transaction
from django.utils import timezone as django_timezone

from .models import SnapshotV1
from .readmodel_v1 import SNAPSHOT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# The one logger a refusal is reported through. It is a module constant so a
# reader can assert on the exact channel instead of the root logger.
LOGGER_NAME = 'alltips_scraper.storage_v1'

logger = logging.getLogger(LOGGER_NAME)

# The digest algorithm the stored ``payload_sha256`` column is written with.
# ``hashlib.new()`` is handed this name, so the algorithm is stated once.
DIGEST_ALGORITHM = 'sha256'

# The canonical JSON form: keys sorted, no whitespace around a separator, and
# every non-ASCII character escaped. Two payloads that differ only in the dict
# insertion order they were built with therefore digest identically, and no
# digest depends on the locale of the machine that computed it.
CANONICAL_JSON_SEPARATORS = (',', ':')
CANONICAL_TEXT_ENCODING = 'utf-8'

# Why a stored row was refused. Each is a fixed token, never a value a row
# supplied, so a log line can name the failed check without quoting the row.
REASON_SCHEMA_VERSION = 'schema_version'
REASON_DIGEST = 'digest'
REASON_PAYLOAD_SHAPE = 'payload_shape'
REASON_FETCHED_AT = 'fetched_at'
REASON_DATABASE = 'database'

# The two log lines, as templates. Both take the type key and a reason token and
# nothing else, so no row content, exception text or upstream detail can reach a
# log line by accident.
REFUSED_ROW_MESSAGE = 'stored v1 snapshot row refused (type_key=%s, reason=%s)'
FAILED_READ_MESSAGE = 'stored v1 snapshot read failed (type_key=%s, reason=%s)'


# ---------------------------------------------------------------------------
# The canonical payload form and its one digest
# ---------------------------------------------------------------------------


def canonical_payload_bytes(payload):
    """Return the canonical byte form of ``payload``.

    The form is JSON with sorted keys, compact separators and every non-ASCII
    character escaped, encoded as UTF-8. It is the only form a digest is taken
    over, so a payload digests identically whatever order its keys were inserted
    in and wherever it is digested.
    """
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=CANONICAL_JSON_SEPARATORS,
        ensure_ascii=True,
    )
    return text.encode(CANONICAL_TEXT_ENCODING)


def canonical_payload_sha256(payload):
    """Return the lowercase hex SHA-256 digest of the canonical byte form.

    A payload JSON cannot represent raises rather than being digested
    approximately: an approximate digest would let a rewritten payload keep the
    claim its row makes about it.
    """
    return hashlib.new(
        DIGEST_ALGORITHM, canonical_payload_bytes(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def _normalise_fetched_at(value):
    """Return ``value`` as an aware UTC datetime, or refuse it.

    The write path uses this. An aware datetime in another zone is the same
    instant, so it is converted; a naive one is refused rather than assumed to be
    UTC, because guessing a zone would fabricate an authoritative timestamp. The
    wire text of a timestamp is not a datetime, so it is refused with
    ``TypeError``: nothing here parses text.
    """
    if not isinstance(value, datetime):
        raise TypeError('fetched_at must be a datetime')
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('fetched_at must be timezone-aware')
    return value.astimezone(timezone.utc)


def _clock_now():
    """Return the current instant as an aware UTC datetime.

    The one place this module reads a clock. ``django.utils.timezone.now()`` is
    aware UTC under ``USE_TZ``, which is what this project configures. A process
    configured without it hands back a naive local reading, and that reading is
    converted from the system's local zone rather than published as a value this
    contract does not allow.
    """
    now = django_timezone.now()
    # A naive reading is a local one, and ``astimezone()`` reads exactly that as
    # system local time; an aware reading is simply converted.
    return now.astimezone(timezone.utc)


def _utc_or_none(value):
    """Return ``value`` as an aware UTC datetime, or ``None`` when it is not one.

    The read path uses this instead of refusing, because a row with a missing,
    naive or non-datetime stamp is simply a row this provider cannot use, and the
    answer for such a row is no snapshot. Nothing is assumed to be UTC here and
    nothing is rendered as text.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _record(type_key, payload, fetched_at):
    """Return the four-key stored record, with its payload deep copied.

    The key set and the key order are ``readmodel_v1``'s, so a record this
    provider returns is indistinguishable from the default provider's, and the
    endpoint cannot tell which of the two it is answering from.
    """
    return {
        'schema_version': SNAPSHOT_SCHEMA_VERSION,
        'type_key': type_key,
        'payload': deepcopy(payload),
        'fetched_at': fetched_at,
    }


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class DatabaseSnapshotProvider:
    """The durable provider: one row per type key, in the v1 snapshot table.

    It holds no state of its own, so one instance can be installed once and used
    from every thread: each call reads or writes the row for the key it was given
    and touches nothing else. ``load``, ``store`` and ``clear`` are the seam's
    whole protocol.
    """

    def load(self, type_key):
        """Return the record for ``type_key``, or ``None`` when it is unusable.

        A key with no row, a row this provider refuses, and a table it cannot
        read are all one and the same answer, and each refused or unreadable row
        is reported once through ``LOGGER_NAME``.
        """
        try:
            row = self._stored_row(type_key)
        except DatabaseError:
            logger.error(FAILED_READ_MESSAGE, type_key, REASON_DATABASE)
            return None
        if row is None:
            return None
        return self._usable_record(row, type_key)

    def store(self, type_key, payload, *, fetched_at=None):
        """Write ``payload`` under ``type_key`` and return the stored record.

        The payload must be a mapping, because a stored payload is the source
        envelope and a reader reads it by key. ``fetched_at`` defaults to now in
        UTC and must otherwise be an aware datetime, which is normalised to UTC.
        Both are validated, and the digest is computed, before any row is
        touched, so a refused write is a no-op; the row itself is written inside
        one transaction, so a failed write leaves the previous row as it was. The
        returned record describes exactly the values this call persisted.
        """
        if not isinstance(payload, dict):
            raise TypeError('payload must be a dictionary')
        stored_payload = deepcopy(payload)
        stored_fetched_at = (
            _clock_now() if fetched_at is None
            else _normalise_fetched_at(fetched_at)
        )
        digest = canonical_payload_sha256(stored_payload)
        with transaction.atomic():
            row, _created = SnapshotV1.objects.update_or_create(
                type_key=type_key,
                defaults={
                    'schema_version': SNAPSHOT_SCHEMA_VERSION,
                    'payload': stored_payload,
                    'payload_sha256': digest,
                    'fetched_at': stored_fetched_at,
                },
            )
        return _record(row.type_key, stored_payload, stored_fetched_at)

    def clear(self):
        """Delete every snapshot row. Clearing an empty table is a no-op."""
        with transaction.atomic():
            SnapshotV1.objects.all().delete()

    @staticmethod
    def _stored_row(type_key):
        """Return the stored row for ``type_key``, or ``None`` when there is none.

        The primary key is the whole lookup: one type key is one row, and a key
        that was never written is not a row this read can mistake for another.
        """
        return SnapshotV1.objects.filter(pk=type_key).first()

    def _usable_record(self, row, type_key):
        """Return the record for ``row``, or refuse the row and return ``None``.

        The checks are independent and run in a fixed order, so a row with more
        than one defect reports the first: the record version this table
        describes, the digest of the payload that is actually stored, the
        payload's own shape, and finally the instant it was fetched. Only a row
        that passes all four becomes a record, and a refused row is reported by
        its type key and the one check it failed.
        """
        if row.schema_version != SNAPSHOT_SCHEMA_VERSION:
            return self._reject(type_key, REASON_SCHEMA_VERSION)
        if row.payload_sha256 != canonical_payload_sha256(row.payload):
            return self._reject(type_key, REASON_DIGEST)
        if not isinstance(row.payload, dict):
            return self._reject(type_key, REASON_PAYLOAD_SHAPE)
        fetched_at = _utc_or_none(row.fetched_at)
        if fetched_at is None:
            return self._reject(type_key, REASON_FETCHED_AT)
        return _record(row.type_key, row.payload, fetched_at)

    @staticmethod
    def _reject(type_key, reason):
        """Report the refusal and answer ``None``, the answer for a bad row."""
        logger.error(REFUSED_ROW_MESSAGE, type_key, reason)
        return None