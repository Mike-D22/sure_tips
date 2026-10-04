"""Offline validation of candidate canonical v1 content artifacts.

Why this module exists
----------------------
The versioned tips API is served from canonical JSON content that ships inside
this application package, and ``jsoncontent_v1`` is the reader that answers for
it: it loads a record when the manifest and the payload file that entry names are
usable, and it answers ``None`` - the seam's "no snapshot" answer - for every
other state. That is exactly right at runtime, where a refusal has to become the
endpoint's existing client-safe answer instead of a ``500``. It is the wrong
answer before a publication is reviewed, because two of the states it answers
``None`` for are silent: a content root with no manifest at all, and a type key
the manifest does not name. A candidate directory in either state is
indistinguishable, to a reader, from a directory whose content is simply not
published yet.

Reviewing a candidate publication therefore needs a second reading of the same
artifacts: one that enumerates everything the candidate claims, refuses the whole
directory when anything it claims would not load, and says which artifact failed
which check. This module is that reading, and it is deliberately not a second
rule book: it judges a candidate by the reader's own checks and in the reader's
own vocabulary, and it answers only the question a reviewer is asking:

    Can the current JSON reader safely load this candidate artifact?

It does not answer whether the versioned registry publishes a type key, whether
the payload describes tips, or whether the instant is recent: those are the
serializer's and the reviewer's questions, and a validator that guessed at them
would refuse candidates the deployment would happily serve.

What it reports
---------------
One line per checked artifact, and only that line. The manifest is checked first
and reported as one line, and then every type key the manifest names is checked
and reported as one line, in sorted key order so a report is deterministic::

    manifest status=ok entries=<n>
    type=<token> status=ok
    type=<token> status=refused reason=<token>

A manifest that is refused is reported as the one manifest line, because the
entries it names cannot be enumerated::

    manifest status=refused reason=<token>

Every type key is passed through ``reporting_v1.safe_token`` first, so a key that
came from a candidate file cannot contain a newline that forges a second line, a
space that makes a reader see a second field, or an ``=`` that invents one - and
a line therefore never carries a value the candidate supplied verbatim.

Ground rules
------------
* **Offline, and nothing else in the picture.** The imports are the standard
  library and the sibling modules that own the reader's vocabulary, the digest
  rule and the report-token sanitiser. There is no framework, no model, no
  configuration read, no clock, no network client and no database, so a review
  can run where none of those may.
* **The reader's rules, not a second opinion.** The manifest keys, entry keys,
  file-name rule, digest rule, payload shape and instant rule are the reader's
  own names, imported from it, and the checks run in the reader's own order, so a
  candidate the reader would refuse is refused here for the same reason and a
  candidate the reader would load is accepted here. The only check this module
  adds is the one a candidate may not be in and a deployment may: an absent
  manifest is reported as ``manifest_missing`` rather than as the empty
  deployment the reader is allowed to be.
* **A refusal names a check, never a value.** Every reason is one fixed token.
  No line carries a path, a file name, a digest, a payload value, an instant, a
  refusal from the source, or any other value read out of the candidate
  directory.
* **Nothing is written, and nothing is published.** No file is opened for
  writing, no directory is created, and nothing here makes a candidate part of
  the deployment: the reviewed change to the content directory is still the only
  publication there is.
* **A report is complete rather than fatal.** Every artifact is checked and
  reported even after one of them has been refused, because a reviewer wants the
  whole list of what has to be fixed. The report states whether the candidate is
  usable as one boolean, and the command that prints it turns that into an exit
  status.

See ``jsoncontent_v1`` for the reader whose rules these are, ``canonical_json_v1``
for the digest rule, and ``docs/API_V1_CONTRACT.md`` and ``docs/RUNBOOK.md`` for
the contract the content serves and the workflow that reviews it.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .canonical_json_v1 import (
    CANONICAL_TEXT_ENCODING,
    canonical_payload_sha256,
    is_canonical_digest,
)
from .jsoncontent_v1 import (
    MANIFEST_FILENAME,
    MANIFEST_KEYS,
    MANIFEST_SCHEMA_VERSION,
    REASON_DIGEST,
    REASON_ENTRY_SHAPE,
    REASON_FETCHED_AT,
    REASON_MANIFEST_JSON,
    REASON_MANIFEST_READ,
    REASON_MANIFEST_SHAPE,
    REASON_MANIFEST_VERSION,
    REASON_PAYLOAD_SHAPE,
    REASON_RECORD_JSON,
    REASON_RECORD_READ,
    REASON_SCHEMA_VERSION,
    RECORD_ALLOWED_KEYS,
    RECORD_FILENAME_FORBIDDEN_CHARACTERS,
    RECORD_FILENAME_SUFFIX,
    RECORD_KEYS,
    RECORD_TIMESTAMP_KEY,
)
from .readmodel_v1 import SNAPSHOT_SCHEMA_VERSION
from .reporting_v1 import safe_token

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# The two statuses a checked artifact is reported with, and the field names a
# line states them under. A line is a fixed template plus these tokens, so a
# reader can parse one without knowing anything at all about the candidate.
STATUS_OK = 'ok'
STATUS_REFUSED = 'refused'

MANIFEST_FIELD = 'manifest'
TYPE_FIELD = 'type'
STATUS_FIELD = 'status'
REASON_FIELD = 'reason'
ENTRIES_FIELD = 'entries'

# The four report lines, as templates. A manifest line names the manifest; when
# it is usable it also states how many entries the manifest names. A type line
# names the type and then either its status, or its status and the one check that
# failed. A refused line carries no count: it has none to state.
MANIFEST_OK_LINE = (
    MANIFEST_FIELD + ' ' + STATUS_FIELD + '=' + STATUS_OK
    + ' ' + ENTRIES_FIELD + '={entries}'
)
MANIFEST_REFUSED_LINE = (
    MANIFEST_FIELD + ' ' + STATUS_FIELD + '=' + STATUS_REFUSED
    + ' ' + REASON_FIELD + '={reason}'
)
TYPE_OK_LINE = TYPE_FIELD + '={type} ' + STATUS_FIELD + '=' + STATUS_OK
TYPE_REFUSED_LINE = (
    TYPE_FIELD + '={type} ' + STATUS_FIELD + '=' + STATUS_REFUSED
    + ' ' + REASON_FIELD + '={reason}'
)

# The one reason this module adds to the reader's vocabulary: a candidate content
# root has to hold a manifest, where a deployment is allowed not to. The two
# states are deliberately different, because "nothing is published" is a valid
# deployment and never a valid candidate to review.
REASON_MANIFEST_MISSING = 'manifest_missing'

# The manifest's own two keys, spelled once for the checks below. They are pinned
# against ``MANIFEST_KEYS`` by the tests, so a change to the manifest shape fails
# there rather than turning a check here into one that quietly stopped looking.
SCHEMA_VERSION_KEY = 'schema_version'
SNAPSHOTS_KEY = 'snapshots'

# The entry's own key for the file it names, beside the version, the digest and
# the instant the reader's vocabulary already names.
FILE_KEY = 'file'
SHA256_KEY = 'sha256'

# Every reason token a report can carry: the reader's own, so a review and a
# runtime refusal are described in one vocabulary, plus the candidate-only one.
READER_REASON_TOKENS = (
    REASON_MANIFEST_READ,
    REASON_MANIFEST_JSON,
    REASON_MANIFEST_SHAPE,
    REASON_MANIFEST_VERSION,
    REASON_ENTRY_SHAPE,
    REASON_SCHEMA_VERSION,
    REASON_RECORD_READ,
    REASON_RECORD_JSON,
    REASON_DIGEST,
    REASON_PAYLOAD_SHAPE,
    REASON_FETCHED_AT,
)
VALIDATOR_REASON_TOKENS = (REASON_MANIFEST_MISSING,)
REASON_TOKENS = frozenset(READER_REASON_TOKENS + VALIDATOR_REASON_TOKENS)

# The marker for text that is not JSON. A valid document can be any JSON value,
# so ``None`` cannot be the failure value here - the same reason the reader keeps
# the same marker for the same job.
UNPARSEABLE = object()


# ---------------------------------------------------------------------------
# Parsing and timestamps: the reader's rules, applied to a candidate
# ---------------------------------------------------------------------------


def _json_or_marker(text):
    """Return the document ``text`` holds, or ``UNPARSEABLE`` if it holds none."""
    try:
        return json.loads(text)
    except ValueError:
        return UNPARSEABLE


def _utc_or_none(value):
    """Return ``value`` as an aware UTC datetime, or ``None`` when it is not one.

    The reader's rule, applied here so a candidate is judged by it: an ISO-8601
    instant that carries an offset names an instant, so its offset is applied and
    the result is stated in UTC, while a naive text, a number, a boolean or an
    absent value names none and is refused rather than read as UTC.
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

    The reader's rule: a record name is one non-empty ``.json`` file name beside
    the manifest, it is not a directory marker, and it contains no separator or
    drive prefix, so a manifest cannot denote a path by spelling one.
    """
    if not isinstance(name, str) or not name.endswith(RECORD_FILENAME_SUFFIX):
        return False
    if name in ('.', '..'):
        return False
    return not any(
        character in name for character in RECORD_FILENAME_FORBIDDEN_CHARACTERS)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class ContentCheckReport:
    """What one candidate content root checked out as, and the lines that say so.

    ``ok`` is the verdict a reviewer acts on: true when the manifest is usable
    and every entry it names would load. The lines are the report itself, in the
    order they have to be read in - the manifest first, then one line per entry
    in sorted key order - and they are kept as a tuple so a report cannot be
    edited after it has been produced.
    """

    __slots__ = ('ok', '_lines')

    def __init__(self, ok, lines):
        self.ok = ok
        self._lines = tuple(lines)

    def lines(self):
        """Return the report lines as a list, in the order they are printed."""
        return list(self._lines)

    def __repr__(self):
        return 'ContentCheckReport(ok=%r, lines=%r)' % (
            self.ok, list(self._lines))

# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def validate_content_root(root):
    """Return the report for the candidate content root ``root``.

    The manifest is settled first, because nothing else can be enumerated until
    it is usable: a root whose manifest is refused produces the one manifest line
    and no type lines at all. Every entry the manifest names is then checked and
    reported independently, so one bad artifact cannot hide another.
    """
    root = Path(root)
    manifest, manifest_reason = _manifest(root)
    if manifest_reason is not None:
        return ContentCheckReport(
            False, [MANIFEST_REFUSED_LINE.format(reason=manifest_reason)])

    entries = manifest[SNAPSHOTS_KEY]
    lines = [MANIFEST_OK_LINE.format(entries=len(entries))]
    refused = False
    for type_key in sorted(entries):
        reason = _entry_reason(root, entries[type_key])
        if reason is None:
            lines.append(TYPE_OK_LINE.format(type=safe_token(type_key)))
        else:
            refused = True
            lines.append(TYPE_REFUSED_LINE.format(
                type=safe_token(type_key), reason=reason))
    return ContentCheckReport(not refused, lines)


def _manifest(root):
    """Return ``(manifest, None)``, or ``(None, reason)`` for a refused manifest.

    A manifest that is not there is a refusal here rather than the empty
    deployment: a candidate is a claim about what should be published, and a
    candidate that claims nothing is one a reviewer cannot check. Everything else
    that stops this read is reported with the same token the reader uses for it,
    so the two layers cannot describe the same defect in two vocabularies.
    """
    path = root / MANIFEST_FILENAME
    try:
        text = path.read_text(encoding=CANONICAL_TEXT_ENCODING)
    except FileNotFoundError:
        return None, REASON_MANIFEST_MISSING
    except OSError:
        return None, REASON_MANIFEST_READ
    except UnicodeError:
        # Not text, so not the JSON document a manifest has to be.
        return None, REASON_MANIFEST_JSON
    manifest = _json_or_marker(text)
    if manifest is UNPARSEABLE:
        return None, REASON_MANIFEST_JSON
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        return None, REASON_MANIFEST_SHAPE
    if manifest[SCHEMA_VERSION_KEY] != MANIFEST_SCHEMA_VERSION:
        return None, REASON_MANIFEST_VERSION
    if not isinstance(manifest[SNAPSHOTS_KEY], dict):
        return None, REASON_MANIFEST_SHAPE
    return manifest, None


def _entry_reason(root, entry):
    """Return why ``entry`` would not load, or ``None`` when it would.

    The checks are the reader's, in the reader's order, so an entry with more than
    one defect reports the first one the reader would have refused: the entry's
    shape and record version, the file it names, the document that file holds,
    the digest the entry claims about it, the payload's own shape, and finally the
    instant. Only an entry that passes every one of them is answered with
    ``None``.
    """
    if not _entry_shape_ok(entry):
        return REASON_ENTRY_SHAPE
    if entry[SCHEMA_VERSION_KEY] != SNAPSHOT_SCHEMA_VERSION:
        return REASON_SCHEMA_VERSION
    path = _record_path(root, entry[FILE_KEY])
    if path is None:
        return REASON_ENTRY_SHAPE
    try:
        text = path.read_text(encoding=CANONICAL_TEXT_ENCODING)
    except OSError:
        return REASON_RECORD_READ
    except UnicodeError:
        # Not text, so not the JSON document a payload has to be.
        return REASON_RECORD_JSON
    payload = _json_or_marker(text)
    if payload is UNPARSEABLE:
        return REASON_RECORD_JSON
    if canonical_payload_sha256(payload) != entry[SHA256_KEY]:
        return REASON_DIGEST
    if not isinstance(payload, dict):
        return REASON_PAYLOAD_SHAPE
    if _utc_or_none(entry.get(RECORD_TIMESTAMP_KEY)) is None:
        return REASON_FETCHED_AT
    return None


def _entry_shape_ok(entry):
    """Return whether one manifest entry is an entry of the documented shape."""
    if not isinstance(entry, dict):
        return False
    if set(entry) - RECORD_ALLOWED_KEYS or not RECORD_KEYS <= set(entry):
        return False
    if not _is_record_filename(entry[FILE_KEY]):
        return False
    return is_canonical_digest(entry[SHA256_KEY])


def _record_path(root, name):
    """Return the file ``name`` denotes inside the content root, or ``None``.

    ``name`` has already been checked to be a plain file name; this is the
    structural half of the same check, so a name that resolves to a location
    outside the content root - through a symbolic link or a junction, for
    instance - is refused although the name itself looks harmless.
    """
    try:
        resolved_root = root.resolve()
        resolved = (root / name).resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(resolved_root):
        return None
    return root / name
