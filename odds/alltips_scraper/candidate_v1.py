"""The pure artifact rules for preparing one candidate v1 content record.

Why this module exists
----------------------
``contentcheck_v1`` answers "would this candidate root load?" and
``validate_v1_content`` prints that answer, but neither of them produces a
candidate. A reviewer who wants to add one type key to the published content has
to write the payload file, compute the canonical digest, spell the manifest entry
and quote the authoritative instant by hand - and the four steps that go wrong
quietly are exactly those four: a digest taken over file bytes instead of the
payload, an instant with no offset, an entry key the reader does not allow, and a
file name that is not a plain name beside the manifest.

This module is the pure half of preparing that record. It turns a payload
document, a type key and an instant into the exact bytes a candidate root holds,
using the reader's own vocabulary and the one digest rule, and it answers every
state it cannot prepare with one fixed reason token. It reads nothing, writes
nothing, consults no clock and imports no framework, so the rules can be tested
without a filesystem and the command that touches files stays a thin wrapper
around them.

What it produces
----------------
One ``CandidatePlan`` per record: the record's file name, the record file's bytes,
the manifest entry, and the whole manifest as it will read once that entry is in
it. Nothing else is decided here - where those bytes go, whether the output
directory may be used at all, and what a refused run reports are the command's
questions (``management/commands/prepare_v1_candidate.py``).

Ground rules
------------
* **One vocabulary, borrowed by identity.** A payload that is not a mapping is
  refused with ``jsoncontent_v1``'s own ``payload_shape`` token and an instant
  that carries no offset with its own ``fetched_at`` token: the objects, not equal
  strings, so a review-time refusal and a runtime refusal cannot drift apart. The
  reasons this module adds are the ones the reader has no state for - a type key
  the registry does not publish (``unknown_type``), text that is not a payload
  document (``payload_json``) and an entry that would replace a different one
  (``entry_conflict``).
* **Every byte is decided here.** The record file is the canonical byte form of the
  payload plus one newline, and the manifest is two-space-indented JSON with sorted
  keys and escaped non-ASCII - the form ``canonical_json_v1`` defines, so the file
  a reviewer reads is the byte form its digest is taken over, and two runs over
  one input produce identical bytes on any machine and in any key-insertion order.
* **An existing entry is never replaced.** Preparing a type key the manifest
  already names is idempotent when the entry is identical, and refused as
  ``entry_conflict`` when it is not - including when only the instant differs. A
  replacement is a reviewed edit to the published manifest, never a side effect of
  a preparation run.
* **A refusal is a token, and nothing else.** ``CandidateRefusal`` carries one fixed
  reason token and no payload value, digest, instant, file name or path, so a
  caller can report it without quoting the content it refused.
* **Nothing is read, written, or timed.** The payload arrives as text the caller
  read, the instant as text the caller supplied, and the manifest as a mapping the
  caller validated. This module opens no file, resolves no path and never asks this
  machine what time it is.

See ``jsoncontent_v1`` for the reader whose vocabulary and rules these are,
``canonical_json_v1`` for the one byte form and digest rule, ``contentcheck_v1``
for the validator that judges the finished candidate,
``management/commands/prepare_v1_candidate.py`` for the command that writes it,
and ``docs/RUNBOOK.md`` section 4.4 for the workflow it belongs to.
"""

import json

from .canonical_json_v1 import (
    CANONICAL_TEXT_ENCODING,
    canonical_payload_bytes,
    canonical_payload_sha256,
)
from .contentcheck_v1 import FILE_KEY, SCHEMA_VERSION_KEY, SHA256_KEY, SNAPSHOTS_KEY
from .jsoncontent_v1 import (
    MANIFEST_SCHEMA_VERSION,
    RECORD_FILENAME_SUFFIX,
    RECORD_TIMESTAMP_KEY,
    REASON_FETCHED_AT,
    REASON_PAYLOAD_SHAPE,
    _is_record_filename,
    _utc_or_none,
)
from .readmodel_v1 import SNAPSHOT_SCHEMA_VERSION
from .serializers_v1 import is_supported_tip_type

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# The form a written manifest is spelled in: two spaces of indentation, keys
# sorted, non-ASCII escaped, one trailing newline. Indented rather than compact
# because the artifact is reviewed by a human before it is committed, and fixed
# rather than inherited from the file it may replace because two runs over one
# input have to produce the same bytes.
MANIFEST_INDENT = 2
DOCUMENT_NEWLINE = '\n'

# The two reasons the reader has no state for. A type key outside the versioned
# registry is not a payload defect - the reader would serve it happily - and an
# entry that would replace a different one is not a defect either: both are
# decisions of this preparation step, so both are named here.
REASON_UNKNOWN_TYPE = 'unknown_type'
REASON_ENTRY_CONFLICT = 'entry_conflict'

# The one reason a *payload document* is refused for its text rather than its
# shape. The reader has ``record_json`` for a record file that is not JSON, but a
# source document handed to this command is not a record file, so the reason is
# spelled for what it describes.
REASON_PAYLOAD_JSON = 'payload_json'

# The one mark a payload document may begin with and still be read: a UTF-8
# byte-order mark, which a Windows editor writes and no document contains.
PAYLOAD_BYTE_ORDER_MARK = '\ufeff'

# Every reason this module can refuse a plan with, as the fixed set a caller may
# assert on: two borrowed from the reader and three of its own.
REASON_TOKENS = frozenset({
    REASON_PAYLOAD_SHAPE,
    REASON_FETCHED_AT,
    REASON_UNKNOWN_TYPE,
    REASON_PAYLOAD_JSON,
    REASON_ENTRY_CONFLICT,
})


class CandidateRefusal(Exception):
    """Raised when the inputs cannot produce a candidate that would load.

    ``reason`` is one of ``REASON_TOKENS``, and it is the whole message: a refusal
    never carries a payload value, a digest, an instant, a file name or a path, so
    a caller can report it verbatim without quoting the content it refused.
    """

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# The reader's rules, reached through this module
# ---------------------------------------------------------------------------


def parse_payload(text):
    """Return the payload document ``text`` holds, or refuse it.

    A payload is a JSON object: text that is not JSON is refused as
    ``payload_json``, and a document that is JSON but not a mapping - a list, a
    string, a number - as the reader's own ``payload_shape``, because that is the
    state the reader refuses a record's payload in.

    One byte-order mark is tolerated at the start of the text: a payload document is
    usually written by hand with a Windows editor, the mark is part of no document,
    and refusing a payload over it would send a reviewer looking for a defect in
    content that has none. Nothing else is normalised - the text is otherwise read
    exactly as the caller read it - and the record file this module writes never
    carries a mark, because its bytes are the canonical form's.
    """
    if text.startswith(PAYLOAD_BYTE_ORDER_MARK):
        text = text[len(PAYLOAD_BYTE_ORDER_MARK):]
    try:
        payload = json.loads(text)
    except ValueError:
        raise CandidateRefusal(REASON_PAYLOAD_JSON) from None
    if not isinstance(payload, dict):
        raise CandidateRefusal(REASON_PAYLOAD_SHAPE)
    return payload


def parse_authoritative_instant(text):
    """Return ``text`` as an aware UTC datetime, or refuse it.

    The reader's rule, borrowed rather than copied: an ISO-8601 instant that
    carries an offset names one, and a naive or unparseable text names none and is
    refused with the reader's own ``fetched_at`` token. What this function is used
    for is the *check*; the text a manifest holds is the text the reviewer
    supplied, so a published instant stays the statement its author made.
    """
    instant = _utc_or_none(text)
    if instant is None:
        raise CandidateRefusal(REASON_FETCHED_AT)
    return instant


def record_filename_for(type_key):
    """Return the record file name for ``type_key``, or refuse the key.

    The name is the type key plus ``.json``, which is what keeps a candidate
    reviewable: the file is named after the entry that claims it. A key the
    versioned registry does not publish is refused as ``unknown_type`` before a
    name is built from it, so an arbitrary string can never become a file name,
    and the name is then checked against the reader's own rule - a plain
    ``.json`` file name with no separator and no drive prefix - so the two cannot
    disagree about what a record file may be called.
    """
    if not is_supported_tip_type(type_key):
        raise CandidateRefusal(REASON_UNKNOWN_TYPE)
    name = type_key + RECORD_FILENAME_SUFFIX
    if not _is_record_filename(name):
        # Unreachable for a registry key, and refused rather than trusted: this is
        # the check that would catch a registry entry a later change spells
        # unportably.
        raise CandidateRefusal(REASON_UNKNOWN_TYPE)
    return name


def record_file_bytes(payload):
    """Return the bytes of the record file that holds ``payload``.

    The canonical byte form plus one newline: the form the digest is taken over,
    so a reviewer can compare a file with the entry that claims it without
    re-formatting anything, and the trailing newline keeps it a text file that
    ends where a text file ends.
    """
    return canonical_payload_bytes(payload) + DOCUMENT_NEWLINE.encode(
        CANONICAL_TEXT_ENCODING)


def manifest_bytes(manifest):
    """Return the bytes a manifest is written as, in the one documented form."""
    return (
        json.dumps(
            manifest,
            sort_keys=True,
            indent=MANIFEST_INDENT,
            ensure_ascii=True,
        )
        + DOCUMENT_NEWLINE
    ).encode(CANONICAL_TEXT_ENCODING)


def empty_manifest():
    """Return the manifest of a candidate that names no entry yet."""
    return {SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION, SNAPSHOTS_KEY: {}}


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def merged_manifest(manifest, type_key, entry):
    """Return ``manifest`` with ``entry`` under ``type_key``, or refuse it.

    The entries the manifest already names are carried over unchanged, so a run
    adds one record and never rewrites another. The one state that is refused is an
    entry for the same type key that differs from ``entry``: preparing a
    replacement - even one that changes only the instant - is not this command's
    decision to make, and answering it with silence would leave a reviewer unsure
    whether the old content or the new one is published.
    """
    snapshots = manifest[SNAPSHOTS_KEY]
    if type_key in snapshots and snapshots[type_key] != entry:
        raise CandidateRefusal(REASON_ENTRY_CONFLICT)
    merged = {
        SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
        SNAPSHOTS_KEY: dict(snapshots),
    }
    merged[SNAPSHOTS_KEY][type_key] = dict(entry)
    return merged


class CandidatePlan:
    """What one preparation run will write, decided before anything is written.

    ``entry`` and ``manifest`` are the values a reviewer would otherwise spell by
    hand, and ``record_bytes`` and ``manifest_bytes`` are the exact bytes they
    become, so a caller can stage and finalise without recomputing a rule and a
    test can compare two runs byte for byte. A plan holds no path: where it is
    written is the command's decision, which is what lets this class be built and
    compared with no filesystem at all.
    """

    __slots__ = (
        'type_key',
        'entry',
        'record_filename',
        'record_bytes',
        'manifest',
        'manifest_bytes',
    )

    def __init__(self, type_key, entry, record_bytes, manifest):
        self.type_key = type_key
        self.entry = dict(entry)
        self.record_filename = entry[FILE_KEY]
        self.record_bytes = record_bytes
        self.manifest = merged_manifest(manifest, type_key, self.entry)
        self.manifest_bytes = manifest_bytes(self.manifest)

    def entries(self):
        """Return how many type keys the manifest that results will name."""
        return len(self.manifest[SNAPSHOTS_KEY])

    def __repr__(self):
        return 'CandidatePlan(type_key=%r, entries=%r)' % (
            self.type_key, self.entries())


def plan_candidate(payload, type_key, fetched_at_text, manifest=None):
    """Return the ``CandidatePlan`` for one record, or refuse the inputs.

    The checks run in the order that decides what the artifact is: the payload's
    own shape, then the type key that names the record file, then the
    authoritative instant, and only then the digest over what is left. ``manifest``
    is the *validated* manifest of the directory being prepared into, and an absent
    one means "a candidate that names no entry yet".
    """
    if not isinstance(payload, dict):
        raise CandidateRefusal(REASON_PAYLOAD_SHAPE)
    filename = record_filename_for(type_key)
    parse_authoritative_instant(fetched_at_text)
    entry = {
        SCHEMA_VERSION_KEY: SNAPSHOT_SCHEMA_VERSION,
        FILE_KEY: filename,
        SHA256_KEY: canonical_payload_sha256(payload),
        RECORD_TIMESTAMP_KEY: fetched_at_text,
    }
    base = empty_manifest() if manifest is None else manifest
    return CandidatePlan(
        type_key=type_key,
        entry=entry,
        record_bytes=record_file_bytes(payload),
        manifest=base,
    )
