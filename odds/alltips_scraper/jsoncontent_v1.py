"""The read-only canonical-content provider for the versioned tips API.

Why this module exists
----------------------
``readmodel_v1`` publishes the snapshot seam and ``storage_v1`` answers it from a
database row a fetch wrote. A deployed tips page does not have to come from a
fetch at all: a reviewed content publication can put canonical JSON under the
application package, ship it with the image, and have the endpoint read it with
no database, no clock and no network involved. This module is that second,
read-only source, expressed as one more provider for the same seam.

What it reads
-------------
One manifest, ``content/v1/manifest.json`` under this package, and one payload
file per type key that the manifest names::

    {"schema_version": 1,
     "snapshots": {
        "<type_key>": {"schema_version": 1,
                       "file": "<name>.json",
                       "sha256": "<canonical digest of the payload it holds>",
                       "fetched_at": "<ISO-8601 instant with an offset>"}}}

``file`` names one file beside the manifest: a plain ``.json`` file name, never a
path, with no separator or drive prefix in it, and the file it resolves to has to
stay inside the content root. ``sha256`` is the digest ``canonical_json_v1``
produces for the payload the file holds, so reformatting a file cannot change its
digest and rewriting what it holds cannot go unnoticed. ``fetched_at`` is the
authoritative instant, and it is the only text this module parses: it has to be an
ISO-8601 instant that carries an offset, it is read as UTC, and a naive or absent
instant is refused rather than assumed to be UTC - which is the rule
``docs/DATA_CONTRACT.md`` §13 states for an authoritative instant.

Ground rules
------------
* **Read-only, and it says so out loud.** ``load()`` is the only operation that
  reads anything. ``store()`` and ``clear()`` raise ``ReadOnlyContentError`` and
  report a fixed line, so a caller that tries to write through this provider
  learns it instead of believing a shipped artifact was published.
* **No framework in the picture.** The imports are the standard library and the
  canonical digest rule, so this module is importable and readable without an
  ORM, a database, a configuration read or a network client. It is deliberately
  not a second durable store: it has no model to write to and no session.
* **A value is either usable or absent.** The manifest, the entry, the payload
  file, the digest claim and the instant are each validated, and a value that
  fails its check is answered with ``None`` - the same answer the seam and the
  endpoint already treat as "no snapshot". An absent manifest and an absent entry
  are answered with ``None`` too, and deliberately not reported: those are the two
  states an empty deployment is allowed to be in.
* **A refusal is reported, never published.** A refusal is logged once, at
  ``ERROR``, through ``LOGGER_NAME``, as one of two fixed templates: a refusal
  line carries the type key and a fixed reason token and nothing else - never the
  payload, the digest, a file name, a path, an exception or any other value read
  from the content directory. The client sees the endpoint's existing client-safe
  answer, so malformed content cannot become a ``500``.
* **Nothing is written, and no clock is read.** No file is opened for writing, no
  directory is created, and the record's instant always comes from the manifest
  rather than from this machine.

See ``docs/API_V1_CONTRACT.md`` for the envelope this feeds and ``storage_v1`` for
the durable provider it stands beside.
"""

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .canonical_json_v1 import (
    CANONICAL_TEXT_ENCODING,
    canonical_payload_sha256,
    is_canonical_digest,
)
from .readmodel_v1 import SNAPSHOT_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# The one logger a refusal is reported through. It is a module constant so a
# reader can assert on the exact channel instead of the root logger.
LOGGER_NAME = 'alltips_scraper.jsoncontent_v1'

logger = logging.getLogger(LOGGER_NAME)

# Where the canonical content lives: a directory inside this package, because an
# application-package path is the one location a deployment ships and a provider
# can be sure of without reading a configuration value.
CANONICAL_CONTENT_ROOT = Path(__file__).resolve().parent / 'content' / 'v1'

MANIFEST_FILENAME = 'manifest.json'

# The manifest's own shape: one record version and one mapping of type keys.
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KEYS = frozenset({'schema_version', 'snapshots'})

# The keys one manifest entry has to carry, and the one key it must also carry
# for the entry to be usable. The instant is listed separately because an absent
# instant is reported as an absent instant, not as a malformed entry.
RECORD_KEYS = frozenset({'schema_version', 'file', 'sha256'})
RECORD_TIMESTAMP_KEY = 'fetched_at'
RECORD_ALLOWED_KEYS = RECORD_KEYS | {RECORD_TIMESTAMP_KEY}

# A published record is one ``.json`` file beside the manifest, and its name may
# not contain a separator or a drive prefix - on the platform this image runs on
# and on the one it is developed on, so a name cannot denote a path.
RECORD_FILENAME_SUFFIX = '.json'
RECORD_FILENAME_FORBIDDEN_CHARACTERS = ('/', '\\', ':')

# Why a value was refused. Each is a fixed token, never a value the content
# supplied, so a log line can name the failed check without quoting the content.
REASON_MANIFEST_READ = 'manifest_read'
REASON_MANIFEST_JSON = 'manifest_json'
REASON_MANIFEST_SHAPE = 'manifest_shape'
REASON_MANIFEST_VERSION = 'manifest_version'
REASON_ENTRY_SHAPE = 'entry_shape'
REASON_SCHEMA_VERSION = 'schema_version'
REASON_RECORD_READ = 'record_read'
REASON_RECORD_JSON = 'record_json'
REASON_DIGEST = 'digest'
REASON_PAYLOAD_SHAPE = 'payload_shape'
REASON_FETCHED_AT = 'fetched_at'

# The two log templates. The refusal template takes the type key and a reason
# token; the write template takes the refused operation. Neither takes anything
# else, so no content value can reach a log line by accident.
REFUSED_CONTENT_MESSAGE = 'canonical v1 content refused (type_key=%s, reason=%s)'
WRITE_REFUSED_MESSAGE = 'canonical v1 content is read-only (operation=%s)'

# The seam's two write operations, as the tokens a refusal names them by.
OPERATION_STORE = 'store'
OPERATION_CLEAR = 'clear'

# The marker for text that is not JSON. A valid document can be any JSON value,
# so ``None`` cannot be the failure value here.
UNPARSEABLE = object()


class ReadOnlyContentError(RuntimeError):
    """Raised when a write is attempted through the read-only content provider.

    A shipped artifact is published by a reviewed change to the content
    directory, never by a process writing at runtime, so this is a programming
    error rather than a failure a caller could retry.
    """


# ---------------------------------------------------------------------------
# Parsing and timestamps
# ---------------------------------------------------------------------------


def _json_or_marker(text):
    """Return the document ``text`` holds, or ``UNPARSEABLE`` if it holds none."""
    try:
        return json.loads(text)
    except ValueError:
        return UNPARSEABLE


def _utc_or_none(value):
    """Return ``value`` as an aware UTC datetime, or ``None`` when it is not one.

    The only text this module parses, and the only read of an instant it performs.
    An ISO-8601 instant that carries an offset names an instant, so its offset is
    applied and the result is stated in UTC. A naive text names no instant, so it
    is refused rather than read as UTC; a value that is not text at all - a number,
    a boolean, an absent key - is refused the same way.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _is_record_filename(name):
    """Return whether ``name`` is a plain file name of a published record.

    A record name is one file name beside the manifest that ends in ``.json``. It
    is not empty, it is not a directory marker, and it contains no separator or
    drive prefix, so a manifest cannot denote a path and cannot name a file
    outside the content root by spelling one.
    """
    if not isinstance(name, str) or not name.endswith(RECORD_FILENAME_SUFFIX):
        return False
    if name in ('.', '..'):
        return False
    return not any(
        character in name for character in RECORD_FILENAME_FORBIDDEN_CHARACTERS)


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class JsonSnapshotProvider:
    """The read-only provider: canonical JSON content under this package.

    ``root`` is the canonical-content directory to read from. A caller gives it
    explicitly - a test points it at its own temporary directory - and it defaults
    to the directory this package ships, so the deployed default reads the
    artifacts the image contains and nothing else. The provider holds no other
    state: a ``load()`` call reads the files it needs when it needs them, so a
    content change is visible to the next call without any invalidation step.
    """

    def __init__(self, root=None):
        self._root = CANONICAL_CONTENT_ROOT if root is None else Path(root)

    @property
    def root(self):
        """Return the canonical-content directory this provider reads from."""
        return self._root

    def load(self, type_key):
        """Return the record for ``type_key``, or ``None`` when there is none.

        ``None`` covers every state that is not a usable record: no manifest, no
        entry for the key, a refused manifest or entry, and a payload file that
        cannot be read or validated. Each refusal is reported once; the two absent
        states are not, because they are the documented empty deployment.
        """
        manifest = self._manifest(type_key)
        if manifest is None:
            return None
        entry = self._entry(manifest, type_key)
        if entry is None:
            return None
        return self._record(entry, type_key)

    def store(self, type_key, payload, *, fetched_at=None):
        """Refuse to write. Published content is reviewed, never written at runtime."""
        self._refuse_write(OPERATION_STORE)

    def clear(self):
        """Refuse to clear. Published content is reviewed, never cleared at runtime."""
        self._refuse_write(OPERATION_CLEAR)

    def _manifest(self, type_key):
        """Return the validated manifest, or ``None`` when it is absent or unusable.

        A manifest that is not there is the empty deployment and is not reported.
        Anything else that stops this read - a file that cannot be opened, text
        that is not JSON, a manifest of another shape or version - is a refusal.
        """
        path = self._manifest_path()
        try:
            text = path.read_text(encoding=CANONICAL_TEXT_ENCODING)
        except FileNotFoundError:
            return None
        except OSError:
            return self._reject(type_key, REASON_MANIFEST_READ)
        except UnicodeError:
            # Not text, so not the JSON document a manifest has to be.
            return self._reject(type_key, REASON_MANIFEST_JSON)
        manifest = _json_or_marker(text)
        if manifest is UNPARSEABLE:
            return self._reject(type_key, REASON_MANIFEST_JSON)
        if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
            return self._reject(type_key, REASON_MANIFEST_SHAPE)
        if manifest['schema_version'] != MANIFEST_SCHEMA_VERSION:
            return self._reject(type_key, REASON_MANIFEST_VERSION)
        if not isinstance(manifest['snapshots'], dict):
            return self._reject(type_key, REASON_MANIFEST_SHAPE)
        return manifest

    def _entry(self, manifest, type_key):
        """Return the validated entry for ``type_key``, or ``None``.

        A type key the manifest does not name is not published yet, which is not a
        failure: it is the state of every key until a reviewed publication names
        it. A key it does name has to name an entry of the documented shape and
        record version, because one that is not - including a name whose value is
        ``null`` - cannot be a record this seam can use, and reading it as an
        unpublished key would answer for a reviewed publication with silence.
        """
        if type_key not in manifest['snapshots']:
            return None
        entry = manifest['snapshots'][type_key]
        if not self._entry_shape_ok(entry):
            return self._reject(type_key, REASON_ENTRY_SHAPE)
        if entry['schema_version'] != SNAPSHOT_SCHEMA_VERSION:
            return self._reject(type_key, REASON_SCHEMA_VERSION)
        return entry

    def _record(self, entry, type_key):
        """Return the record ``entry`` describes, or refuse and return ``None``.

        The checks are independent and run in a fixed order, so an entry with more
        than one defect reports the first: the payload file is read, the document
        it holds is parsed, its canonical digest is compared with the digest the
        entry claims about it, the payload's own shape is checked, and finally the
        instant is read. Only an entry that passes all of them becomes a record,
        and a refused entry is reported by its type key and the one check it
        failed.
        """
        path = self._record_path(entry['file'])
        if path is None:
            return self._reject(type_key, REASON_ENTRY_SHAPE)
        try:
            text = path.read_text(encoding=CANONICAL_TEXT_ENCODING)
        except OSError:
            return self._reject(type_key, REASON_RECORD_READ)
        except UnicodeError:
            # Not text, so not the JSON document a payload has to be.
            return self._reject(type_key, REASON_RECORD_JSON)
        payload = _json_or_marker(text)
        if payload is UNPARSEABLE:
            return self._reject(type_key, REASON_RECORD_JSON)
        if canonical_payload_sha256(payload) != entry['sha256']:
            return self._reject(type_key, REASON_DIGEST)
        if not isinstance(payload, dict):
            return self._reject(type_key, REASON_PAYLOAD_SHAPE)
        fetched_at = _utc_or_none(entry.get(RECORD_TIMESTAMP_KEY))
        if fetched_at is None:
            return self._reject(type_key, REASON_FETCHED_AT)
        return {
            'schema_version': SNAPSHOT_SCHEMA_VERSION,
            'type_key': type_key,
            'payload': deepcopy(payload),
            'fetched_at': fetched_at,
        }

    @staticmethod
    def _entry_shape_ok(entry):
        """Return whether one manifest entry is an entry of the documented shape."""
        if not isinstance(entry, dict):
            return False
        if set(entry) - RECORD_ALLOWED_KEYS or not RECORD_KEYS <= set(entry):
            return False
        if not _is_record_filename(entry['file']):
            return False
        return is_canonical_digest(entry['sha256'])

    def _manifest_path(self):
        """Return the path of the manifest inside this provider's content root."""
        return self._root / MANIFEST_FILENAME

    def _record_path(self, name):
        """Return the file ``name`` denotes inside the content root, or ``None``.

        ``name`` has already been checked to be a plain file name. This is the
        structural half of the same check: the path has to resolve to a location
        that is still inside the content root, so a name that turns out to lead out
        of the directory is refused although the name itself looks harmless.
        """
        try:
            root = self._root.resolve()
            resolved = (self._root / name).resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(root):
            return None
        return self._root / name

    @staticmethod
    def _reject(type_key, reason):
        """Report the refusal and answer ``None``, the answer for bad content."""
        logger.error(REFUSED_CONTENT_MESSAGE, type_key, reason)
        return None

    @staticmethod
    def _refuse_write(operation):
        """Report the refused write and raise, because this source cannot write.

        The refused operation is named by one of the two fixed operation tokens,
        and the log line and the exception are built separately: the log call is
        parameterized, so the template and its one argument reach the logging
        layer unrendered, and the exception message is rendered from the same
        fixed template and the same fixed token. Both therefore carry the same
        wording, and neither can carry a value read from the content directory.
        """
        logger.error(WRITE_REFUSED_MESSAGE, operation)
        raise ReadOnlyContentError(WRITE_REFUSED_MESSAGE % operation)
