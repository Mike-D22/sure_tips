"""Offline contract tests for the candidate-content validator.

Why this module exists
----------------------
Publishing canonical v1 content is a reviewed change: a manifest and the payload
files it names are written by hand, read by a human, and committed so the image
ships them. ``contentcheck_v1`` is the review-time reading of those artifacts, and
it is only worth running if four things hold, so all four are pinned here.

1. **It judges a candidate exactly as the reader does.** A matrix of candidate
   directories is put to both the validator and the real ``JsonSnapshotProvider``,
   and every case asserts the two agree: a candidate the validator accepts is one
   the reader publishes, a candidate it refuses is one the reader answers ``None``
   for, and where the reader logs a refusal it logs the validator's own reason
   token. The one deliberate difference - a candidate with no manifest at all,
   which is a refusal to review and the empty deployment to a reader - is pinned
   as a difference rather than smoothed over.
2. **It says only what it checked, and it changes nothing.** Every manifest state
   and every entry state a candidate can be in is asserted through the exact
   report line and the report's verdict; the reports are asserted to carry no
   path, file name, digest, payload text or instant; and the candidate directory
   is compared byte for byte before and after every validation, so a refusal
   cannot be a write in disguise.
3. **The command that runs it prints the reading, and its status carries the
   verdict.** The one caller, ``manage.py validate_v1_content``, is asserted to
   print the validator's report verbatim, to answer zero for a usable candidate,
   to refuse a candidate that would not load only after printing the whole report,
   and to refuse an invocation that names no directory - or a path that is not one
   - with its own reason token, no report line, and a status of its own that is
   not the failure status.
4. **Its token rule is one implementation, shared with the writer.** The key a
   report prints is fitted by ``reporting_v1.safe_token`` - the same function
   object the writer re-exports, reached without importing the writer at all -
   and the module that owns the rule imports nothing outside the language. A
   hostile key in a candidate therefore reaches a command's output as one field
   of one line, and the two reporters cannot drift about what a token is.

Ground rules
------------
* **No network, no database, no clock.** Every candidate is written into a
  temporary directory by the test itself, the socket layer is disabled for every
  test method, and ``SocketIsBlockedForContentCheckTests`` shows the guard really
  does block, so the remaining tests prove something.
* **The digests are pinned literals.** Each digest below was computed once with
  ``canonical_json_v1`` and is written out in full; a test asserts every literal
  still matches what the rule produces, so a change to the rule fails here rather
  than silently re-blessing the content.
* **The reader's vocabulary is the validator's by identity.** The reason tokens
  the validator reports the reader's states with are asserted to be the reader's
  own objects rather than equal strings, so a second vocabulary cannot creep in.
* **Nothing shipped is written to.** One test validates the content directory this
  package ships with its bytes compared before and after; every other test builds
  its own candidate.

See ``jsoncontent_v1`` for the reader whose rules these are, ``reporting_v1`` for
the report-token rule the validator shares with the writer,
``management/commands/validate_v1_content.py`` for the review-time entry point
that wraps the validator, ``tests_jsoncontent_v1.py`` for the reader's own
contract, and ``tests_refresh_v1.py`` for the writer's.
"""

import ast
import json
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from . import contentcheck_v1, jsoncontent_v1, refresh_v1, reporting_v1
from .canonical_json_v1 import (
    DIGEST_HEX_LENGTH,
    canonical_payload_sha256,
    is_canonical_digest,
)
from .contentcheck_v1 import (
    FILE_KEY,
    MANIFEST_OK_LINE,
    MANIFEST_REFUSED_LINE,
    REASON_MANIFEST_MISSING,
    REASON_TOKENS,
    READER_REASON_TOKENS,
    SCHEMA_VERSION_KEY,
    SNAPSHOTS_KEY,
    STATUS_OK,
    TYPE_OK_LINE,
    TYPE_REFUSED_LINE,
    VALIDATOR_REASON_TOKENS,
    ContentCheckReport,
    validate_content_root,
)
from .jsoncontent_v1 import (
    CANONICAL_CONTENT_ROOT,
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
    RECORD_KEYS,
    RECORD_TIMESTAMP_KEY,
    REFUSED_CONTENT_MESSAGE,
    JsonSnapshotProvider,
)
from .management.commands import validate_v1_content
from .readmodel_v1 import SNAPSHOT_SCHEMA_VERSION
from .reporting_v1 import MAX_TYPE_KEY_LENGTH, safe_token
from .tests_parser_contract import OfflineGuardMixin

# ---------------------------------------------------------------------------
# Fixed locations and inputs. Every value is an explicit literal: no test reads
# a clock, and each digest was computed once from ``canonical_json_v1``.
# ---------------------------------------------------------------------------

APP_PACKAGE_DIR = Path(contentcheck_v1.__file__).resolve().parent
ODDS_DIR = APP_PACKAGE_DIR.parent

TYPE_KEY = 'bet_of_the_day'
OTHER_TYPE_KEY = 'daily_accumulator'

RECORD_FILENAME = 'bet_of_the_day.json'
OTHER_RECORD_FILENAME = 'daily_accumulator.json'

FETCHED_AT_TEXT = '2026-09-28T17:12:03+00:00'
OFFSET_FETCHED_AT_TEXT = '2026-09-28T20:42:03+03:30'
NAIVE_FETCHED_AT_TEXT = '2026-09-28T17:12:03'
FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3, tzinfo=timezone.utc)

PAYLOAD = {
    'date': '2026-09-27',
    'total_tips': 1,
    'matches': [
        {'match_title': 'Arsenal vs Chelsea', 'prediction': 'Arsenal to win'},
    ],
    'count': 1,
    'source': 'freesupertips',
}
PAYLOAD_SHA256 = (
    '36cfd8636014fb7edc478b3337e863a232957aecd19d24203abfed0044c42659')

# A payload whose text contains non-ASCII characters: the digest rule escapes
# them, so the file a test writes and the payload it holds are not byte equal.
UNICODE_PAYLOAD = {
    'date': '2026-09-27',
    'matches': [
        {'match_title': 'Olimpia vs Cerro Porteño',
         'prediction': 'Olimpia to win',
         'tip_reason': 'Aprobado por el café de la mañana'},
    ],
    'count': 1,
    'source': 'freesupertips',
}
UNICODE_PAYLOAD_SHA256 = (
    'cfaba8eed8969d6eb0a95822cd3375deebc7bf5042a7acc686e905c2346bd2d8')

# A payload that is valid JSON but not a mapping: it must be refused, so its
# correct digest is pinned to prove the shape check is what refuses it.
LIST_PAYLOAD = [1, 2]
LIST_PAYLOAD_SHA256 = (
    '49a64717d5d4cb19952e6eac2946415cf6879adacf9908e7d872332d32c6e684')

# A payload that is shape-valid but wrong: this is what a rewritten payload file
# looks like beside an unchanged digest claim.
REWRITTEN_PAYLOAD = {'date': '2026-09-27', 'count': 0}

# Shape-valid as a claim, and never the digest of anything: the entry check that
# refuses it is the claim's shape rather than the payload it describes.
MISMATCHED_SHA256 = '0' * DIGEST_HEX_LENGTH
SHORT_SHA256 = '0' * (DIGEST_HEX_LENGTH - 1)

# A type key a candidate file could hold, and the token it must be reported as:
# the newline would forge a second line and the space and ``=`` would invent a
# field, so all three become the replacement character.
HOSTILE_TYPE_KEY = 'bad key=1\nsecond line'
HOSTILE_TYPE_TOKEN = 'bad?key?1?second?line'
LONG_TYPE_KEY = 'k' * (MAX_TYPE_KEY_LENGTH + 20)

# The reader's reason tokens that describe the manifest rather than an entry, so
# a case knows whether its refusal replaces the manifest line or an entry line.
MANIFEST_LEVEL_REASONS = frozenset({
    REASON_MANIFEST_MISSING,
    REASON_MANIFEST_READ,
    REASON_MANIFEST_JSON,
    REASON_MANIFEST_SHAPE,
    REASON_MANIFEST_VERSION,
})

# What the validator may import: the standard library it needs and the four
# sibling modules that own the reader's vocabulary, the digest rule, the record
# version and the report-token sanitiser. Anything else - a framework, an ORM, a
# settings reader, a network client - would make a review need what it exists to
# avoid.
ALLOWED_SOURCE_MODULES = frozenset({
    'json', 'datetime', 'pathlib',
    'canonical_json_v1', 'jsoncontent_v1', 'readmodel_v1', 'reporting_v1',
})

# Source tokens that must not appear at all: naming one is how a review-time
# check would come to read configuration, reach the network, or write.
FORBIDDEN_SOURCE_TOKENS = (
    'django',
    'models',
    'settings',
    'getenv',
    'os.environ',
    'decouple',
    'socket',
    'urllib',
    'requests',
    'aiohttp',
    'cloudscraper',
    'cache',
    'redis',
    'subprocess',
    'write_text',
    'write_bytes',
    'open(',
    'print(',
    'input(',
)

# The fresh-interpreter probe: import the validator with nothing but the standard
# library on the path and report what came with it. A package root in the first
# list is one a review must never need; a module name in the second is one this
# package must never have to import to answer the question a reviewer is asking.
IMPORT_PROBE = '''\
import json
import sys

sys.path.insert(0, sys.argv[1])
import alltips_scraper.contentcheck_v1 as module

SUSPICIOUS_ROOTS = (
    'django', 'cloudscraper', 'requests', 'aiohttp', 'httpx', 'urllib3',
    'socket', 'ssl', 'sqlite3',
)
SUSPICIOUS_MODULES = (
    'alltips_scraper.utils', 'alltips_scraper.storage_v1',
    'alltips_scraper.models', 'alltips_scraper.apps',
)
loaded = sorted(
    name for name in sys.modules
    if name.split('.')[0] in SUSPICIOUS_ROOTS or name in SUSPICIOUS_MODULES
)
print(json.dumps({
    'loaded': loaded,
    'exported': sorted(
        name for name in ('validate_content_root', 'ContentCheckReport',
                          'REASON_TOKENS')
        if hasattr(module, name)
    ),
}))
'''

class CandidateContentTestCase(OfflineGuardMixin, SimpleTestCase):
    """Give every test its own candidate content directory.

    Nothing in this module validates the directory the package ships except the
    one test that says so: a test writes the manifest and the payload files it
    means to check, so both the usable and the refused states are explicit rather
    than inherited.
    """

    def setUp(self):
        super().setUp()
        self.fresh_root()

    def fresh_root(self):
        """Point this test at a new, empty candidate directory and return it.

        The matrix test builds one candidate per case, so the directory has to be
        replaceable within a single test; every directory made here is cleaned up
        when the test ends.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        return self.root

    # -- writing a candidate -------------------------------------------------

    def write_text(self, name, text):
        """Write ``text`` as UTF-8 under this test's candidate root."""
        (self.root / name).write_text(text, encoding='utf-8')

    def write_bytes(self, name, raw):
        """Write ``raw`` bytes under this test's candidate root, undecoded."""
        (self.root / name).write_bytes(raw)

    def write_manifest(self, manifest):
        """Write ``manifest`` as the manifest file, indented and readable."""
        self.write_text(MANIFEST_FILENAME, json.dumps(manifest, indent=2))

    def write_entries(self, entries,
                      manifest_version=MANIFEST_SCHEMA_VERSION):
        """Write a manifest whose snapshots are exactly ``entries``."""
        self.write_manifest({
            SCHEMA_VERSION_KEY: manifest_version,
            SNAPSHOTS_KEY: entries,
        })

    def entry(self, *, filename=RECORD_FILENAME, sha256=PAYLOAD_SHA256,
              fetched_at=FETCHED_AT_TEXT,
              record_version=SNAPSHOT_SCHEMA_VERSION):
        """Return one manifest entry of the documented shape.

        ``fetched_at=None`` omits the key altogether, so a test can vary one
        thing at a time.
        """
        entry = {
            SCHEMA_VERSION_KEY: record_version,
            FILE_KEY: filename,
            'sha256': sha256,
        }
        if fetched_at is not None:
            entry[RECORD_TIMESTAMP_KEY] = fetched_at
        return entry

    def publish(self, type_key=TYPE_KEY, *, payload=PAYLOAD,
                sha256=PAYLOAD_SHA256, filename=RECORD_FILENAME,
                fetched_at=FETCHED_AT_TEXT):
        """Publish one payload file and a manifest naming it under ``type_key``."""
        self.write_text(filename, json.dumps(payload))
        self.write_entries({
            type_key: self.entry(
                filename=filename, sha256=sha256, fetched_at=fetched_at),
        })

    def write_payload(self, name=RECORD_FILENAME, payload=PAYLOAD):
        """Write ``payload`` into the record file ``name``, creating directories."""
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding='utf-8')

    def write_entry(self, entry, type_key=TYPE_KEY):
        """Write a manifest whose one entry is ``entry``, exactly as given."""
        self.write_entries({type_key: entry})

    # -- reading a candidate and its report ---------------------------------

    def root_contents(self):
        """Return every file name and byte payload currently under the root."""
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob('*'))
            if path.is_file()
        }

    def assert_report(self, report, *, ok, lines):
        """Assert the report's verdict and its lines, in order."""
        self.assertEqual(report.ok, ok)
        self.assertEqual(report.lines(), lines)

    def assert_usable_candidate(self, *, entries, type_lines):
        """Validate the current candidate and assert its whole report."""
        self.assert_report(
            validate_content_root(self.root),
            ok=True,
            lines=[MANIFEST_OK_LINE.format(entries=entries)] + type_lines,
        )

# ---------------------------------------------------------------------------
# The matrix: every candidate state, put to both readings
# ---------------------------------------------------------------------------


class ReaderEquivalenceTests(CandidateContentTestCase):
    """The validator and the reader must agree about every candidate state.

    Each row names one candidate: the method that writes it, the reason the
    validator must report (``None`` when the candidate is usable), whether the
    reader must hand back a record for the manifest's own type key, and the reason
    the reader must log. The single row whose reason has no reader counterpart is
    a candidate with no manifest at all: that is the deliberate difference - a
    refusal to review and the empty deployment - and it is pinned rather than
    smoothed over. The coverage test below is what keeps a new case from being
    added to one side and forgotten on the other.
    """

    MATRIX = (
        ('a usable entry', 'case_usable_entry', None, True, None),
        ('a usable entry with an offset instant',
         'case_usable_entry_with_an_offset_instant', None, True, None),
        ('a usable payload that is not ASCII',
         'case_usable_unicode_payload', None, True, None),
        ('no manifest at all',
         'case_no_manifest', REASON_MANIFEST_MISSING, False, None),
        ('a manifest path that cannot be read',
         'case_manifest_is_a_directory',
         REASON_MANIFEST_READ, False, REASON_MANIFEST_READ),
        ('a manifest that is not JSON',
         'case_manifest_is_not_json',
         REASON_MANIFEST_JSON, False, REASON_MANIFEST_JSON),
        ('a manifest that is not a mapping',
         'case_manifest_is_not_a_mapping',
         REASON_MANIFEST_SHAPE, False, REASON_MANIFEST_SHAPE),
        ('a manifest with an extra key',
         'case_manifest_has_an_extra_key',
         REASON_MANIFEST_SHAPE, False, REASON_MANIFEST_SHAPE),
        ('a manifest missing a key',
         'case_manifest_is_missing_a_key',
         REASON_MANIFEST_SHAPE, False, REASON_MANIFEST_SHAPE),
        ('a manifest of another version',
         'case_manifest_version_is_wrong',
         REASON_MANIFEST_VERSION, False, REASON_MANIFEST_VERSION),
        ('a manifest whose snapshots is not a mapping',
         'case_manifest_snapshots_is_not_a_mapping',
         REASON_MANIFEST_SHAPE, False, REASON_MANIFEST_SHAPE),
        ('an entry that is null',
         'case_entry_is_null',
         REASON_ENTRY_SHAPE, False, REASON_ENTRY_SHAPE),
        ('an entry with no digest claim',
         'case_entry_is_missing_the_digest',
         REASON_ENTRY_SHAPE, False, REASON_ENTRY_SHAPE),
        ('an entry with an extra key',
         'case_entry_has_an_extra_key',
         REASON_ENTRY_SHAPE, False, REASON_ENTRY_SHAPE),
        ('an entry whose file name is a path',
         'case_entry_names_a_path',
         REASON_ENTRY_SHAPE, False, REASON_ENTRY_SHAPE),
        ('an entry whose file name is not a payload name',
         'case_entry_names_a_non_json_file',
         REASON_ENTRY_SHAPE, False, REASON_ENTRY_SHAPE),
        ('an entry whose digest claim is not a digest',
         'case_entry_digest_is_malformed',
         REASON_ENTRY_SHAPE, False, REASON_ENTRY_SHAPE),
        ('an entry of another record version',
         'case_entry_record_version_is_wrong',
         REASON_SCHEMA_VERSION, False, REASON_SCHEMA_VERSION),
        ('an entry whose payload file is absent',
         'case_entry_file_is_absent',
         REASON_RECORD_READ, False, REASON_RECORD_READ),
        ('an entry whose payload path is a directory',
         'case_entry_file_is_a_directory',
         REASON_RECORD_READ, False, REASON_RECORD_READ),
        ('an entry whose payload file is not text',
         'case_payload_is_not_text',
         REASON_RECORD_JSON, False, REASON_RECORD_JSON),
        ('an entry whose payload file is not JSON',
         'case_payload_is_not_json',
         REASON_RECORD_JSON, False, REASON_RECORD_JSON),
        ('an entry whose payload was rewritten',
         'case_payload_was_rewritten',
         REASON_DIGEST, False, REASON_DIGEST),
        ('an entry whose payload is not a mapping',
         'case_payload_is_not_a_mapping',
         REASON_PAYLOAD_SHAPE, False, REASON_PAYLOAD_SHAPE),
        ('an entry with no instant',
         'case_entry_has_no_instant',
         REASON_FETCHED_AT, False, REASON_FETCHED_AT),
        ('an entry with a naive instant',
         'case_entry_instant_is_naive',
         REASON_FETCHED_AT, False, REASON_FETCHED_AT),
        ('an entry whose instant is not text',
         'case_entry_instant_is_not_text',
         REASON_FETCHED_AT, False, REASON_FETCHED_AT),
    )

    # -- one candidate per case ---------------------------------------------
    #
    # Each case writes exactly one candidate into this test's own directory. A
    # case that means to test a check also writes the thing the check reads, so a
    # refusal is never the accidental report of an absent file.

    def case_usable_entry(self):
        """One entry whose file, digest and instant are all usable."""
        self.publish()

    def case_usable_entry_with_an_offset_instant(self):
        """A usable entry whose instant carries an offset, not ``+00:00``."""
        self.publish(fetched_at=OFFSET_FETCHED_AT_TEXT)

    def case_usable_unicode_payload(self):
        """A usable entry whose payload holds characters outside ASCII."""
        self.publish(payload=UNICODE_PAYLOAD, sha256=UNICODE_PAYLOAD_SHA256)

    def case_no_manifest(self):
        """An empty candidate: the empty deployment to a reader, not to a review."""
        return None

    def case_manifest_is_a_directory(self):
        """A manifest path that exists but cannot be read as a file."""
        (self.root / MANIFEST_FILENAME).mkdir()

    def case_manifest_is_not_json(self):
        """Manifest text that is not a JSON document at all."""
        self.write_text(MANIFEST_FILENAME, '{')

    def case_manifest_is_not_a_mapping(self):
        """A manifest that is a JSON document but not a mapping."""
        self.write_manifest([1])

    def case_manifest_has_an_extra_key(self):
        """A manifest carrying a key the documented shape does not allow."""
        self.write_manifest({
            SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
            SNAPSHOTS_KEY: {},
            'extra': 1,
        })

    def case_manifest_is_missing_a_key(self):
        """A manifest that does not carry the snapshots key at all."""
        self.write_manifest({SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION})

    def case_manifest_version_is_wrong(self):
        """A manifest of a record version this reader does not speak."""
        self.write_manifest({
            SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION + 1,
            SNAPSHOTS_KEY: {},
        })

    def case_manifest_snapshots_is_not_a_mapping(self):
        """A manifest whose snapshots value is a list, not a mapping."""
        self.write_manifest({
            SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
            SNAPSHOTS_KEY: [],
        })

    def case_entry_is_null(self):
        """A type key the manifest names with no entry at all."""
        self.write_entry(None)

    def case_entry_is_missing_the_digest(self):
        """An entry with every documented key but the digest claim."""
        self.write_payload()
        self.write_entry({
            SCHEMA_VERSION_KEY: SNAPSHOT_SCHEMA_VERSION,
            FILE_KEY: RECORD_FILENAME,
            RECORD_TIMESTAMP_KEY: FETCHED_AT_TEXT,
        })

    def case_entry_has_an_extra_key(self):
        """An entry carrying a key the documented shape does not allow."""
        entry = self.entry()
        entry['extra'] = 1
        self.write_payload()
        self.write_entry(entry)

    def case_entry_names_a_path(self):
        """A file name that denotes a path rather than a file beside it."""
        self.write_payload('sub/' + RECORD_FILENAME)
        self.write_entry(self.entry(filename='sub/' + RECORD_FILENAME))

    def case_entry_names_a_non_json_file(self):
        """A file name that is not a ``.json`` payload name."""
        self.write_payload('bet_of_the_day.txt')
        self.write_entry(self.entry(filename='bet_of_the_day.txt'))

    def case_entry_digest_is_malformed(self):
        """A digest claim that could not be a digest of this rule."""
        self.write_payload()
        self.write_entry(self.entry(sha256=SHORT_SHA256))

    def case_entry_record_version_is_wrong(self):
        """An entry of a record version the reader does not speak."""
        self.write_payload()
        self.write_entry(
            self.entry(record_version=SNAPSHOT_SCHEMA_VERSION + 1))

    def case_entry_file_is_absent(self):
        """An entry whose payload file was never written."""
        self.write_entry(self.entry())

    def case_entry_file_is_a_directory(self):
        """An entry whose payload path is a directory rather than a file."""
        (self.root / RECORD_FILENAME).mkdir()
        self.write_entry(self.entry())

    def case_payload_is_not_text(self):
        """A payload file whose bytes are not decodable text."""
        self.write_bytes(RECORD_FILENAME, b'\xff\xfe\x00\x01')
        self.write_entry(self.entry())

    def case_payload_is_not_json(self):
        """A payload file whose text is not a JSON document at all."""
        self.write_text(RECORD_FILENAME, '{')
        self.write_entry(self.entry())

    def case_payload_was_rewritten(self):
        """A payload file rewritten beside an unchanged digest claim."""
        self.write_payload(payload=REWRITTEN_PAYLOAD)
        self.write_entry(self.entry())

    def case_payload_is_not_a_mapping(self):
        """A payload that really is JSON and really is not a mapping."""
        self.write_payload(payload=LIST_PAYLOAD)
        self.write_entry(self.entry(sha256=LIST_PAYLOAD_SHA256))

    def case_entry_has_no_instant(self):
        """A usable entry with no instant key at all."""
        self.write_payload()
        self.write_entry(self.entry(fetched_at=None))

    def case_entry_instant_is_naive(self):
        """A usable entry whose instant names no offset, so names no instant."""
        self.write_payload()
        self.write_entry(self.entry(fetched_at=NAIVE_FETCHED_AT_TEXT))

    def case_entry_instant_is_not_text(self):
        """A usable entry whose instant is a number instead of text."""
        self.write_payload()
        self.write_entry(self.entry(fetched_at=5))

    # -- the matrix, read both ways ------------------------------------------

    def expected_lines(self, reason):
        """Return the report lines a case with this validator reason must print."""
        if reason is None:
            return [
                MANIFEST_OK_LINE.format(entries=1),
                TYPE_OK_LINE.format(type=TYPE_KEY),
            ]
        if reason in MANIFEST_LEVEL_REASONS:
            return [MANIFEST_REFUSED_LINE.format(reason=reason)]
        return [
            MANIFEST_OK_LINE.format(entries=1),
            TYPE_REFUSED_LINE.format(type=TYPE_KEY, reason=reason),
        ]

    def test_every_candidate_state_agrees_with_the_reader(self):
        """Run each case past both readings and compare them and the matrix.

        The validator's verdict, its lines, the reader's record and the reason the
        reader logs are all asserted against the row itself. A candidate the
        validator calls usable must be one the reader publishes, a candidate it
        refuses must be one the reader answers ``None`` for, and - except for the
        candidate with no manifest, whose refusal is the reviewer's alone - the
        reason the reader logs must be the reason the validator reports.
        """
        self.assertTrue(MANIFEST_LEVEL_REASONS <= REASON_TOKENS)
        for label, method, reason, publishes, logged_reason in self.MATRIX:
            with self.subTest(case=label):
                root = self.fresh_root()
                getattr(self, method)()

                report = validate_content_root(root)
                self.assert_report(
                    report, ok=reason is None, lines=self.expected_lines(reason))

                provider = JsonSnapshotProvider(root)
                with mock.patch.object(jsoncontent_v1.logger, 'error') as log:
                    record = provider.load(TYPE_KEY)
                self.assertEqual(record is not None, publishes)
                if logged_reason is None:
                    log.assert_not_called()
                else:
                    log.assert_called_once_with(
                        REFUSED_CONTENT_MESSAGE, TYPE_KEY, logged_reason)

    def test_the_matrix_covers_every_case_this_class_declares(self):
        """Every declared candidate is in the matrix, and every row is a method.

        A case nobody runs and a row pointing at nothing are both ways for this
        file to stop covering a state while still looking like it does.
        """
        declared = {name for name in dir(self) if name.startswith('case_')}
        self.assertEqual(declared, {row[1] for row in self.MATRIX})
        for label, method, reason, publishes, logged_reason in self.MATRIX:
            with self.subTest(case=label):
                self.assertIn(reason, REASON_TOKENS | {None})
                self.assertIsInstance(publishes, bool)
                if reason is None:
                    self.assertTrue(publishes)
                    self.assertIsNone(logged_reason)
                if logged_reason is not None:
                    self.assertIn(logged_reason, READER_REASON_TOKENS)

    def test_a_usable_candidate_is_the_record_the_reader_publishes(self):
        """The one full record, asserted key by key, is what both readings agree on."""
        self.publish()
        report = validate_content_root(self.root)
        self.assert_usable_candidate(
            entries=1, type_lines=[TYPE_OK_LINE.format(type=TYPE_KEY)])
        record = JsonSnapshotProvider(self.root).load(TYPE_KEY)
        self.assertEqual(record, {
            SCHEMA_VERSION_KEY: SNAPSHOT_SCHEMA_VERSION,
            'type_key': TYPE_KEY,
            'payload': PAYLOAD,
            RECORD_TIMESTAMP_KEY: FETCHED_AT,
        })
        self.assertEqual(record['payload'], PAYLOAD)
        self.assertTrue(report.ok)

class ReportContentTests(CandidateContentTestCase):
    """What a report says about one candidate, and what it must never say.

    The reasons themselves are pinned by the matrix; these are the properties of
    the report: its order, its completeness, the sanitising of the one value it
    does echo, and the absence of every value it must not echo.
    """

    def test_a_manifest_that_names_no_entry_is_usable(self):
        """A valid manifest naming nothing is a published nothing, not a refusal."""
        self.write_entries({})
        self.assert_usable_candidate(entries=0, type_lines=[])

    def test_the_type_lines_come_in_sorted_key_order(self):
        """Two runs over one candidate print the same report, in key order."""
        self.write_payload('zeta.json')
        self.write_payload('alpha.json')
        self.write_entries({
            'zeta': self.entry(filename='zeta.json'),
            'alpha': self.entry(filename='alpha.json'),
        })
        self.assert_usable_candidate(entries=2, type_lines=[
            TYPE_OK_LINE.format(type='alpha'),
            TYPE_OK_LINE.format(type='zeta'),
        ])

    def test_one_refused_entry_does_not_hide_another(self):
        """A refused entry is reported beside a usable one, not instead of it."""
        self.write_payload('alpha.json')
        self.write_text('zeta.json', '{')
        self.write_entries({
            'alpha': self.entry(filename='alpha.json'),
            'zeta': self.entry(filename='zeta.json'),
        })
        self.assert_report(
            validate_content_root(self.root),
            ok=False,
            lines=[
                MANIFEST_OK_LINE.format(entries=2),
                TYPE_OK_LINE.format(type='alpha'),
                TYPE_REFUSED_LINE.format(
                    type='zeta', reason=REASON_RECORD_JSON),
            ],
        )

    def test_a_key_the_manifest_does_not_name_is_not_reported_at_all(self):
        """The report enumerates the manifest, not any registry of type keys."""
        self.publish(type_key=OTHER_TYPE_KEY, filename=OTHER_RECORD_FILENAME)
        report = validate_content_root(self.root)
        self.assert_usable_candidate(
            entries=1, type_lines=[TYPE_OK_LINE.format(type=OTHER_TYPE_KEY)])
        self.assertNotIn(TYPE_KEY, '\n'.join(report.lines()))

    def test_a_type_key_is_sanitised_before_it_reaches_a_line(self):
        """A key that would forge a line or a field is reported as one token."""
        self.publish(type_key=HOSTILE_TYPE_KEY)
        self.assertEqual(safe_token(HOSTILE_TYPE_KEY), HOSTILE_TYPE_TOKEN)
        report = validate_content_root(self.root)
        self.assert_usable_candidate(
            entries=1,
            type_lines=[TYPE_OK_LINE.format(type=HOSTILE_TYPE_TOKEN)],
        )
        for line in report.lines():
            with self.subTest(line=line):
                self.assertEqual(line.splitlines(), [line])

    def test_a_very_long_type_key_is_reported_at_the_reported_width(self):
        """A key longer than the report allows is truncated, not spread out."""
        self.publish(type_key=LONG_TYPE_KEY)
        self.assert_usable_candidate(
            entries=1,
            type_lines=[
                TYPE_OK_LINE.format(type=LONG_TYPE_KEY[:MAX_TYPE_KEY_LENGTH]),
            ],
        )

    def test_a_report_carries_no_value_read_from_the_candidate(self):
        """Every value a candidate holds stays out of the report about it."""
        self.write_text(RECORD_FILENAME, json.dumps(REWRITTEN_PAYLOAD))
        self.write_entry(self.entry(sha256=MISMATCHED_SHA256))
        report = validate_content_root(self.root)
        self.assert_report(
            report,
            ok=False,
            lines=[
                MANIFEST_OK_LINE.format(entries=1),
                TYPE_REFUSED_LINE.format(type=TYPE_KEY, reason=REASON_DIGEST),
            ],
        )
        reported = '\n'.join(report.lines())
        for value in (
            str(self.root),
            MANIFEST_FILENAME,
            RECORD_FILENAME,
            MISMATCHED_SHA256,
            FETCHED_AT_TEXT,
            json.dumps(REWRITTEN_PAYLOAD),
            REWRITTEN_PAYLOAD['date'],
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, reported)

    def test_validating_a_candidate_writes_nothing(self):
        """A usable candidate is left byte for byte as it was found."""
        self.publish()
        self.write_bytes('stray.bin', b'\x00\x01')
        before = self.root_contents()
        validate_content_root(self.root)
        self.assertEqual(self.root_contents(), before)

    def test_validating_a_refused_candidate_writes_nothing(self):
        """A refusal is not a write in disguise: nothing appears, nothing changes."""
        self.write_text(MANIFEST_FILENAME, '{')
        self.write_bytes('stray.bin', b'\x00\x01')
        before = self.root_contents()
        report = validate_content_root(self.root)
        self.assertFalse(report.ok)
        self.assertEqual(self.root_contents(), before)

    def test_a_name_that_resolves_outside_the_root_is_not_a_payload_path(self):
        """The one structural refusal a candidate can make about where it reads."""
        self.assertIsNone(
            contentcheck_v1._record_path(self.root, '../outside.json'))

    def test_a_name_that_stays_inside_the_root_is_the_path_beside_it(self):
        """A plain name beside the manifest is the file it names."""
        self.write_payload('inside.json')
        self.assertEqual(
            contentcheck_v1._record_path(self.root, 'inside.json'),
            self.root / 'inside.json',
        )

    def test_a_symbolic_link_out_of_the_root_is_refused_when_one_is_allowed(self):
        """A name that looks plain but leads out of the root is still refused."""
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / 'escape.json'
        target.write_text('{}', encoding='utf-8')
        try:
            (self.root / 'escape.json').symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest('this platform does not let a test create a link')
        self.assertIsNone(contentcheck_v1._record_path(self.root, 'escape.json'))


class VocabularyTests(OfflineGuardMixin, SimpleTestCase):
    """The names, the tokens and the line templates this module is read through.

    A report is read by a reviewer and matched by a script, so its four line
    templates are pinned literally, the reader's tokens are asserted to be the
    reader's own objects rather than equal strings, and the one token this module
    adds is asserted to be the only one it adds.
    """

    # The reader's reason tokens, by the name the reader declares them under, so a
    # token cannot be checked against the wrong attribute.
    READER_TOKENS = {
        'REASON_MANIFEST_READ': REASON_MANIFEST_READ,
        'REASON_MANIFEST_JSON': REASON_MANIFEST_JSON,
        'REASON_MANIFEST_SHAPE': REASON_MANIFEST_SHAPE,
        'REASON_MANIFEST_VERSION': REASON_MANIFEST_VERSION,
        'REASON_ENTRY_SHAPE': REASON_ENTRY_SHAPE,
        'REASON_SCHEMA_VERSION': REASON_SCHEMA_VERSION,
        'REASON_RECORD_READ': REASON_RECORD_READ,
        'REASON_RECORD_JSON': REASON_RECORD_JSON,
        'REASON_DIGEST': REASON_DIGEST,
        'REASON_PAYLOAD_SHAPE': REASON_PAYLOAD_SHAPE,
        'REASON_FETCHED_AT': REASON_FETCHED_AT,
    }

    def test_the_four_report_line_templates_are_exact(self):
        """Every line a report can print, spelled out in full."""
        self.assertEqual(
            MANIFEST_OK_LINE, 'manifest status=ok entries={entries}')
        self.assertEqual(
            MANIFEST_REFUSED_LINE, 'manifest status=refused reason={reason}')
        self.assertEqual(TYPE_OK_LINE, 'type={type} status=ok')
        self.assertEqual(
            TYPE_REFUSED_LINE, 'type={type} status=refused reason={reason}')
        self.assertEqual(STATUS_OK, 'ok')

    def test_the_readers_reason_tokens_are_the_readers_own_objects(self):
        """Identity, not equality: a second vocabulary may not creep in."""
        for name, token in sorted(self.READER_TOKENS.items()):
            with self.subTest(token=name):
                self.assertIs(token, getattr(jsoncontent_v1, name))
        self.assertEqual(
            set(READER_REASON_TOKENS), set(self.READER_TOKENS.values()))

    def test_the_validator_adds_exactly_one_reason_of_its_own(self):
        """A candidate with no manifest: the one state only a review can see."""
        self.assertEqual(VALIDATOR_REASON_TOKENS, (REASON_MANIFEST_MISSING,))
        self.assertEqual(REASON_MANIFEST_MISSING, 'manifest_missing')
        self.assertEqual(
            set(REASON_TOKENS) - set(READER_REASON_TOKENS),
            {REASON_MANIFEST_MISSING},
        )
        self.assertTrue(set(READER_REASON_TOKENS) <= set(REASON_TOKENS))

    def test_every_reason_token_is_one_distinct_printable_token(self):
        """No token may be empty, doubled, or able to forge a field."""
        self.assertEqual(len(REASON_TOKENS), len(set(REASON_TOKENS)))
        for token in sorted(REASON_TOKENS):
            with self.subTest(token=token):
                self.assertTrue(token)
                self.assertEqual(token, token.lower().strip())
                self.assertNotIn('%', token)
                self.assertNotIn(' ', token)
                self.assertNotIn('=', token)

    def test_the_documented_keys_are_the_readers_own(self):
        """The manifest shape and the entry shape are the reader's, not copies."""
        self.assertEqual({SCHEMA_VERSION_KEY, SNAPSHOTS_KEY}, set(MANIFEST_KEYS))
        self.assertTrue({SCHEMA_VERSION_KEY, FILE_KEY} <= set(RECORD_KEYS))
        self.assertEqual(RECORD_TIMESTAMP_KEY, 'fetched_at')
        self.assertEqual(MANIFEST_SCHEMA_VERSION, SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(SNAPSHOT_SCHEMA_VERSION, 1)

    def test_the_report_hands_out_a_fresh_copy_of_its_lines(self):
        """A caller cannot change what a report holds by changing what it got."""
        report = ContentCheckReport(True, ['a'])
        self.assertTrue(report.ok)
        report.lines().append('b')
        self.assertEqual(report.lines(), ['a'])
        self.assertFalse(ContentCheckReport(False, []).ok)

    def test_the_validator_has_no_default_root_of_its_own(self):
        """Nothing shipped can be validated by accident: the root is an argument."""
        with self.assertRaises(TypeError):
            validate_content_root()

    def test_an_empty_directory_is_a_candidate_and_is_refused(self):
        """An empty directory is a candidate: refused, once, and named as such."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        report = validate_content_root(Path(directory.name))
        self.assertFalse(report.ok)
        self.assertEqual(
            report.lines(),
            [MANIFEST_REFUSED_LINE.format(reason=REASON_MANIFEST_MISSING)],
        )


class PinnedDigestTests(OfflineGuardMixin, SimpleTestCase):
    """Every digest this module claims is the digest the rule produces.

    The matrix pins a digest mismatch as a refusal, so a literal that went stale
    would turn a usable candidate into a refused one - and the failure would look
    like a defect in the check rather than in the test data.
    """

    def test_every_pinned_digest_is_the_digest_the_rule_produces(self):
        for payload, digest in (
            (PAYLOAD, PAYLOAD_SHA256),
            (UNICODE_PAYLOAD, UNICODE_PAYLOAD_SHA256),
            (LIST_PAYLOAD, LIST_PAYLOAD_SHA256),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(canonical_payload_sha256(payload), digest)

    def test_every_pinned_digest_is_a_canonical_digest(self):
        for digest in (
            PAYLOAD_SHA256, UNICODE_PAYLOAD_SHA256, LIST_PAYLOAD_SHA256,
            MISMATCHED_SHA256,
        ):
            with self.subTest(digest=digest):
                self.assertEqual(len(digest), DIGEST_HEX_LENGTH)
                self.assertTrue(is_canonical_digest(digest))

    def test_the_short_digest_is_the_only_malformed_claim(self):
        """The malformed claim differs from a digest in its length alone."""
        self.assertFalse(is_canonical_digest(SHORT_SHA256))
        self.assertEqual(len(SHORT_SHA256), DIGEST_HEX_LENGTH - 1)


class SourceHygieneTests(OfflineGuardMixin, SimpleTestCase):
    """What the validator's source may name, and what it may not.

    A review-time check is offline only if nothing it imports can reach out, so
    the module's imports are pinned to the standard library and the four sibling
    modules that own the reader's vocabulary, the digest rule, the record version
    and the sanitiser, and the source is asserted not to name a framework, an ORM,
    a settings reader, a network client or a write.
    """

    def source(self):
        """Return the validator's own source text, decoded."""
        return Path(contentcheck_v1.__file__).read_text(encoding='utf-8')

    def imported_module_roots(self):
        """Return the root of every module the validator imports.

        A relative import is named by the module it names, and an absolute import
        by its first segment, so ``from .jsoncontent_v1 import ...`` and
        ``import json`` are both accounted for.
        """
        roots = set()
        for node in ast.walk(ast.parse(self.source())):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or '').split('.')[0])
        return roots

    def test_the_validator_imports_only_what_a_review_can_afford(self):
        self.assertEqual(
            self.imported_module_roots(), set(ALLOWED_SOURCE_MODULES))

    def test_the_source_names_no_framework_no_client_and_no_write(self):
        source = self.source().lower()
        for token in FORBIDDEN_SOURCE_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_the_validator_reads_files_and_never_writes_one(self):
        """The only file call is an explicit text read, with its encoding named."""
        source = self.source()
        self.assertIn('read_text(encoding=', source)
        for token in (
            'write_text', 'write_bytes', 'mkdir', 'unlink', 'rename',
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


class SharedReportTokenTests(OfflineGuardMixin, SimpleTestCase):
    """The token rule: one implementation, and the stdlib is all it needs.

    The reviewer's lines and the writer's are fitted the same way, so neither
    module may grow a second copy of the rule: the validator imports the shared
    function, the writer re-exports the very same objects, and the module the
    rule lives in imports no import at all, which is the shape the digest rule
    has: ``storage_v1`` re-exports it from ``canonical_json_v1``.
    """

    def source_of(self, module):
        """Return ``module``'s own source text, decoded."""
        return Path(module.__file__).read_text(encoding='utf-8')

    def imported_names_of(self, module):
        """Return (top-level roots, relative module names) of ``module``.

        A relative import is named by the module it names, so a
        ``from .refresh_v1 import ...`` would show up here as ``refresh_v1``.
        """
        roots = set()
        relative = []
        for node in ast.walk(ast.parse(self.source_of(module))):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative.append(node.module or '')
                else:
                    roots.add((node.module or '').split('.')[0])
        return roots, relative

    def test_the_shared_module_imports_only_the_standard_library(self):
        """It depends on no sibling, no framework and no client."""
        roots, relative = self.imported_names_of(reporting_v1)
        self.assertEqual(relative, [])
        self.assertEqual(roots - set(sys.stdlib_module_names), set())
        # The rule needs nothing it is not handed: ``str``, ``frozenset`` and
        # the built-in string methods are the whole of it, so an import
        # appearing here is a dependency this module did not have before.
        self.assertEqual(roots, set())

    def test_the_shared_module_names_no_framework_client_or_write(self):
        """The module that fits every token is itself names-only."""
        source = self.source_of(reporting_v1).lower()
        for token in FORBIDDEN_SOURCE_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_the_writer_re_exports_the_shared_rule_itself(self):
        """``refresh_v1`` hands out ``reporting_v1``'s objects, not copies."""
        self.assertIs(refresh_v1.safe_token, reporting_v1.safe_token)
        self.assertIs(
            refresh_v1.MAX_TYPE_KEY_LENGTH, reporting_v1.MAX_TYPE_KEY_LENGTH)
        self.assertIs(
            refresh_v1.SAFE_TOKEN_EXTRA_CHARACTERS,
            reporting_v1.SAFE_TOKEN_EXTRA_CHARACTERS)
        self.assertIs(
            refresh_v1.REPLACEMENT_CHARACTER,
            reporting_v1.REPLACEMENT_CHARACTER)

        for value in (
            'bet_of_the_day', 'bad key=1\nsecond line', '', 'k' * 200,
        ):
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    refresh_v1.safe_token(value),
                    reporting_v1.safe_token(value))

    def test_the_validator_imports_the_rule_and_never_the_writer(self):
        """The function is reached directly, so no writer comes with it."""
        self.assertIs(contentcheck_v1.safe_token, reporting_v1.safe_token)

        _roots, relative = self.imported_names_of(contentcheck_v1)
        self.assertEqual(sorted(relative), [
            'canonical_json_v1', 'jsoncontent_v1', 'readmodel_v1',
            'reporting_v1',
        ])
        self.assertNotIn('refresh_v1', self.source_of(contentcheck_v1))


class FreshInterpreterTests(OfflineGuardMixin, SimpleTestCase):
    """The validator answers in an interpreter that has nothing else loaded.

    The same question the deployment tests ask of the settings module, asked the
    same way: import it in a fresh interpreter with a fixed path and observe what
    came with it, rather than matching the text of the file that imports it.
    """

    def probe(self):
        """Import the validator in a fresh interpreter and return its findings."""
        completed = subprocess.run(
            [sys.executable, '-c', IMPORT_PROBE, str(ODDS_DIR)],
            cwd=str(ODDS_DIR), capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_the_probe_carries_nothing_a_review_must_not_need(self):
        """No framework, no ORM, no socket layer, no network client."""
        self.assertEqual(self.probe()['loaded'], [])

    def test_the_probe_finds_the_names_a_reviewer_calls(self):
        self.assertEqual(
            self.probe()['exported'],
            ['ContentCheckReport', 'REASON_TOKENS', 'validate_content_root'],
        )


class ShippedContentTests(OfflineGuardMixin, SimpleTestCase):
    """The content directory this package ships: read, reported, left alone.

    The published content is the deployment's, not this tool's. It may be an empty
    deployment - it is until content is published - and reading it may not change
    it, which is what makes the validator safe to point at the tracked directory
    from a review. The report is also checked against the manifest it describes, so
    a line that stopped corresponding to a type key would fail here.
    """

    def contents_of(self, directory):
        """Return every file name and byte payload under ``directory``."""
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob('*'))
            if path.is_file()
        }

    def test_the_shipped_content_root_is_where_the_reader_reads_from(self):
        self.assertEqual(
            CANONICAL_CONTENT_ROOT,
            Path(jsoncontent_v1.__file__).resolve().parent / 'content' / 'v1',
        )

    def test_the_shipped_content_root_is_usable_and_left_alone(self):
        before = self.contents_of(CANONICAL_CONTENT_ROOT)
        report = validate_content_root(CANONICAL_CONTENT_ROOT)
        self.assertTrue(report.ok)
        self.assertEqual(
            report.lines()[0],
            MANIFEST_OK_LINE.format(entries=len(report.lines()) - 1),
        )
        self.assertEqual(self.contents_of(CANONICAL_CONTENT_ROOT), before)

    def test_every_shipped_type_line_names_a_key_the_reader_loads(self):
        """The report's keys, in order, are the manifest's, and each one loads."""
        manifest = json.loads(
            (CANONICAL_CONTENT_ROOT / MANIFEST_FILENAME).read_text(
                encoding='utf-8')
        )
        keys = sorted(manifest[SNAPSHOTS_KEY])
        report = validate_content_root(CANONICAL_CONTENT_ROOT)
        tokens = [
            line.split('type=')[1].split(' ')[0] for line in report.lines()[1:]
        ]
        self.assertEqual(len(tokens), len(keys))
        provider = JsonSnapshotProvider(CANONICAL_CONTENT_ROOT)
        for token, key in zip(tokens, keys):
            with self.subTest(type=key):
                self.assertEqual(token, safe_token(key))
                self.assertIsNotNone(provider.load(key))


class SocketIsBlockedForContentCheckTests(OfflineGuardMixin, SimpleTestCase):
    """The guard has to be effective, or the other tests prove nothing."""

    def test_outbound_socket_creation_is_blocked(self):
        with self.assertRaises(AssertionError):
            socket.socket()

    def test_outbound_connections_are_blocked(self):
        with self.assertRaises(AssertionError):
            socket.create_connection(
                ('content-check-must-not-connect.invalid', 80))

    def test_dns_resolution_is_blocked(self):
        with self.assertRaises(AssertionError):
            socket.getaddrinfo('content-check-must-not-connect.invalid', 80)


# ---------------------------------------------------------------------------
# The command: the validator's only caller
# ---------------------------------------------------------------------------


class ValidateV1ContentCommandTests(CandidateContentTestCase):
    """``manage.py validate_v1_content``: the report it prints, and its status.

    The command owns the three things the validator cannot say for itself: which
    directory a run is about, what a run hands back to whoever ran it, and what a
    run does when it was handed no usable directory at all. Every run here is over
    a candidate this class wrote - so no shipped content is validated by accident -
    and the candidate is compared byte for byte around every run, so neither the
    report nor the refusal can be a write in disguise. The socket layer is disabled
    for every test as it is for the validator's own, so a run that reached a network
    would raise here rather than pass quietly.
    """

    def run_command(self, *argv):
        """Run the command over ``argv`` and return the lines it printed.

        The report is the command's whole output, so the error stream is captured
        too and asserted empty here rather than in every test that runs it.
        """
        out = StringIO()
        err = StringIO()
        call_command('validate_v1_content', *argv, stdout=out, stderr=err)
        self.assertEqual(err.getvalue(), '')
        return out.getvalue().splitlines()

    def test_the_command_is_registered_and_offers_its_one_option(self):
        """It is this app's command, it names ``--root``, and it has no default."""
        self.assertEqual(get_commands()['validate_v1_content'], 'alltips_scraper')

        parser = validate_v1_content.Command().create_parser(
            'manage.py', 'validate_v1_content')

        self.assertIsNone(parser.parse_args([]).root)
        self.assertEqual(
            parser.parse_args(['--root', 'somewhere']).root, 'somewhere')

        documented = (
            validate_v1_content.Command.help
            + (validate_v1_content.__doc__ or ''))
        self.assertIn('--root', documented)

    def test_a_usable_candidate_is_printed_in_full_and_the_run_answers_zero(self):
        """The command's stdout is the validator's report, line for line."""
        self.publish()
        before = self.root_contents()
        report = validate_content_root(self.root)
        self.assertTrue(report.ok)

        self.assertEqual(
            self.run_command('--root', str(self.root)), report.lines())

        self.assertEqual(self.root_contents(), before)

    def test_a_manifest_that_names_no_entry_is_a_clean_run(self):
        """A published nothing: the one manifest line, and the run answers zero."""
        self.write_entries({})

        self.assertEqual(
            self.run_command('--root', str(self.root)),
            [MANIFEST_OK_LINE.format(entries=0)],
        )

    def test_a_refused_candidate_is_printed_in_full_before_the_run_refuses(self):
        """The whole report reaches a reviewer, and the status carries the verdict."""
        self.publish(sha256=MISMATCHED_SHA256)
        before = self.root_contents()
        out = StringIO()

        with self.assertRaises(CommandError) as caught:
            call_command(
                'validate_v1_content', '--root', str(self.root), stdout=out)

        self.assertEqual(
            str(caught.exception), validate_v1_content.FAILURE_MESSAGE)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertNotEqual(
            caught.exception.returncode, validate_v1_content.ROOT_RETURNCODE)
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                MANIFEST_OK_LINE.format(entries=1),
                TYPE_REFUSED_LINE.format(type=TYPE_KEY, reason=REASON_DIGEST),
            ],
        )
        self.assertEqual(self.root_contents(), before)

    def test_a_run_handed_no_directory_names_the_option_and_prints_no_line(self):
        """The refusal is the invocation's own, and no artifact was reported."""
        self.publish()
        before = self.root_contents()
        out = StringIO()

        with self.assertRaises(CommandError) as caught:
            call_command('validate_v1_content', stdout=out)

        self.assertEqual(
            str(caught.exception), validate_v1_content.ROOT_REQUIRED_MESSAGE)
        self.assertEqual(caught.exception.returncode, 2)
        self.assertEqual(
            caught.exception.returncode, validate_v1_content.ROOT_RETURNCODE)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(self.root_contents(), before)

    def test_a_run_handed_a_path_that_is_not_a_directory_checks_nothing(self):
        """A record file, and a path that is not there: the path's own refusal."""
        self.publish()
        before = self.root_contents()

        for path in (
            self.root / RECORD_FILENAME,
            self.root / 'not' / 'a' / 'directory',
        ):
            with self.subTest(path=str(path)):
                out = StringIO()

                with self.assertRaises(CommandError) as caught:
                    call_command(
                        'validate_v1_content', '--root', str(path), stdout=out)

                self.assertEqual(
                    str(caught.exception),
                    validate_v1_content.ROOT_UNUSABLE_MESSAGE)
                self.assertEqual(
                    caught.exception.returncode,
                    validate_v1_content.ROOT_RETURNCODE)
                self.assertEqual(out.getvalue(), '')

        self.assertEqual(self.root_contents(), before)

    def test_the_two_refusals_of_the_path_are_named_apart_from_the_reports(self):
        """Each path refusal carries its own token, in its own message and status."""
        messages = {
            validate_v1_content.REASON_ROOT_NOT_GIVEN:
                validate_v1_content.ROOT_REQUIRED_MESSAGE,
            validate_v1_content.REASON_ROOT_NOT_A_DIRECTORY:
                validate_v1_content.ROOT_UNUSABLE_MESSAGE,
        }

        self.assertEqual(
            sorted(messages), ['root_not_a_directory', 'root_not_given'])
        self.assertEqual(set(messages) & REASON_TOKENS, set())
        self.assertEqual(validate_v1_content.ROOT_RETURNCODE, 2)

        for token, message in messages.items():
            with self.subTest(token=token):
                self.assertIn('reason=' + token, message)

    def test_a_hostile_type_key_reaches_the_output_as_one_field(self):
        """A key a candidate file supplied cannot forge a line or a field.

        The candidate is usable, so the run prints its report and answers zero:
        the hostile key it names is reported inside one field of one line.
        """
        self.publish(type_key=HOSTILE_TYPE_KEY)
        before = self.root_contents()

        lines = self.run_command('--root', str(self.root))

        self.assertEqual(
            lines,
            [
                MANIFEST_OK_LINE.format(entries=1),
                TYPE_OK_LINE.format(type=HOSTILE_TYPE_TOKEN),
            ],
        )
        # A report line states two fields and nothing else: had the key's own
        # newline, space or ``=`` reached the output as itself, the run would
        # have printed a third line or a third field here.
        self.assertEqual(len(lines), 2)
        self.assertEqual([line.count('=') for line in lines], [2, 2])
        for fragment in (HOSTILE_TYPE_KEY, 'second line', 'key=1'):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, '\n'.join(lines))
        self.assertEqual(self.root_contents(), before)
