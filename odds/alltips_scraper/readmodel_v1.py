"""Framework-free snapshot seam for the versioned tips API (``/api/v1/tips/``).

Why this module exists
----------------------
The versioned endpoint must answer from a snapshot that was fetched once rather
than by running a live fetch per request, and it must stay importable and
testable without a configured environment. This module is the seam between those
two requirements: it owns *where a snapshot lives* and publishes one accessor
pair the endpoint reads and writes through.

Commit 2 publishes the seam and one default implementation. Nothing here is
wired into a view, no setting selects a provider, and no provider other than
``InMemorySnapshotProvider`` exists yet: a later commit may install a deliberate
provider through ``set_snapshot_provider()`` without touching callers written
against ``load_snapshot()`` and ``store_snapshot()``.

Ground rules
------------
* **Standard library only.** The only imports are ``copy.deepcopy``,
  ``datetime.datetime``, ``datetime.timezone`` and ``threading.Lock``, so this
  module reads no setting, performs no I/O, and makes no network call.
  ``tests_api_v1.py`` parses this file and asserts that property.
* **A snapshot is four keys and a datetime.** A stored record carries exactly
  ``schema_version``, ``type_key``, ``payload`` and ``fetched_at``, and
  ``fetched_at`` is an aware UTC ``datetime``. This module never renders that
  value as text, never parses text into it, and never turns a record into a wire
  body: text rendering and response serialisation belong to the response layer.
* **Type keys are opaque.** ``type_key`` is any hashable value used as a storage
  key. This module holds no list of supported tip types, imports no vocabulary,
  and validates no key: a key the endpoint never uses is as legitimate as one it
  does. An unhashable key is refused by the storage itself (``TypeError``)
  rather than by a policy check here.
* **Copies, never aliases.** Deep copying on write and on load means no caller
  can reach stored state through a payload it passed in or a record it got back,
  and a reader never observes a record another thread is part-way through
  mutating.
* **The seam delegates.** The module-level seam functions delegate to the
  installed provider and add no rule of their own, so the strict key handling
  and timestamp policy above belong to ``InMemorySnapshotProvider`` alone and a
  future deliberate provider stays free to implement its own storage semantics.

See ``docs/DATA_CONTRACT.md`` §13 for the authoritative-UTC storage rule the
snapshot timestamp follows.
"""

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock


# ---------------------------------------------------------------------------
# Snapshot vocabulary
# ---------------------------------------------------------------------------

# The stored-record schema. It is versioned so a later storage change can be
# recognised by its readers instead of guessed from a record's shape.
SNAPSHOT_SCHEMA_VERSION = 1

# A stored snapshot is exactly these four keys: the schema it was written with,
# the opaque key it was filed under, the source payload, and the authoritative
# UTC instant the snapshot was fetched.
SNAPSHOT_KEYS = frozenset({
    'schema_version', 'type_key', 'payload', 'fetched_at',
})

# The provider protocol the module-level seam functions delegate through.
_PROVIDER_METHODS = ('load', 'store', 'clear')


def _normalise_fetched_at(value):
    """Return ``value`` as an aware UTC datetime, or refuse it.

    An aware datetime in another zone is the same instant, so it is converted; a
    naive datetime is refused rather than assumed to be UTC, because guessing a
    zone would fabricate an authoritative timestamp. A value that is not a
    datetime at all is refused with ``TypeError`` — including the wire text form
    of a timestamp, which this module never parses. The result is always a
    ``datetime``; nothing here formats it.
    """
    if not isinstance(value, datetime):
        raise TypeError('fetched_at must be a datetime')
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('fetched_at must be timezone-aware')
    return value.astimezone(timezone.utc)


class InMemorySnapshotProvider:
    """The default provider: one process-local dictionary of snapshots.

    It owns its own ``threading.Lock``, so two providers never contend for the
    same lock and never share a record. Every payload is deep copied on the way
    in and every record is deep copied on the way out, so stored state is
    reachable only through this class's own methods.
    """

    def __init__(self):
        self._lock = Lock()
        self._snapshots = {}

    def store(self, type_key, payload, *, fetched_at=None):
        """File ``payload`` under ``type_key`` and return the stored record.

        ``fetched_at`` defaults to now in UTC and must otherwise be an aware
        datetime, which is normalised to UTC. The returned record is the same
        shape a later ``load()`` returns, so a caller can describe what it just
        stored without a second read.
        """
        if fetched_at is None:
            fetched_at = datetime.now(timezone.utc)
        record = {
            'schema_version': SNAPSHOT_SCHEMA_VERSION,
            'type_key': type_key,
            'payload': deepcopy(payload),
            'fetched_at': _normalise_fetched_at(fetched_at),
        }
        with self._lock:
            self._snapshots[type_key] = record
        return deepcopy(record)

    def load(self, type_key):
        """Return a deep copy of the stored snapshot, or ``None`` when absent.

        A key that was never stored and a provider that was just cleared are the
        same answer: no snapshot, not an empty one.
        """
        with self._lock:
            record = self._snapshots.get(type_key)
        if record is None:
            return None
        return deepcopy(record)

    def clear(self):
        """Forget every stored snapshot. Clearing an empty provider is a no-op."""
        with self._lock:
            self._snapshots.clear()


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

# The installed provider. It is replaced, never mutated in place, by
# ``set_snapshot_provider()``.
_provider = InMemorySnapshotProvider()


def get_snapshot_provider():
    """Return the installed provider, which is the default one until replaced."""
    return _provider


def set_snapshot_provider(provider):
    """Install ``provider`` for this process, replacing the current one.

    ``None`` installs a brand-new empty default provider: that is how a caller
    discards stored snapshots without emptying the provider that held them, so a
    reader already holding the old provider keeps the record it was handed.

    Any other value must expose callable ``load``, ``store`` and ``clear``
    methods, which are the whole protocol the seam uses. A provider that does
    not is refused with ``TypeError`` before anything is installed, so the seam
    can never hold something it cannot delegate to.
    """
    global _provider
    if provider is not None:
        for name in _PROVIDER_METHODS:
            if not callable(getattr(provider, name, None)):
                raise TypeError(
                    f'snapshot provider must define a callable {name}() method')
    _provider = InMemorySnapshotProvider() if provider is None else provider


def load_snapshot(type_key):
    """Return the installed provider's snapshot for ``type_key``."""
    return _provider.load(type_key)


def store_snapshot(type_key, payload, *, fetched_at=None):
    """File ``payload`` with the installed provider and return its record.

    The keyword-only ``fetched_at`` is part of the provider protocol and is
    passed through untouched, so the strict "aware UTC datetime or refuse" rule
    stays a property of the default provider rather than of this seam.
    """
    return _provider.store(type_key, payload, fetched_at=fetched_at)


def clear_snapshots():
    """Clear the snapshots held by the installed provider."""
    return _provider.clear()
