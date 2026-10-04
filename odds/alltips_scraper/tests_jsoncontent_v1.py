"""Read-only canonical-content tests for the versioned tips API.

Why this module exists
----------------------
``/api/v1/tips/`` is served from a snapshot. ``storage_v1`` answers that from a
database row a fetch wrote, and ``jsoncontent_v1`` answers it from canonical JSON
shipped inside the application package: one manifest plus one payload file per
type key it names. That second source is only trustworthy if a reader can say
exactly when it publishes a record and when it refuses one, so both halves are
pinned here - against temporary directories written by the test itself, never
against the artifacts the package ships.

Ground rules
------------
* **No network, no database, no clock.** Every test builds its own content
  directory in a temporary directory, writes the files it means to test, and
  asserts against explicit literal payloads, instants and digests: nothing here
  reads a clock, the network, or a stored row, and one test patches the socket
  layer so that an outbound call would raise instead of passing quietly.
* **The digests are pinned literals.** The digests below were computed once with
  ``canonical_json_v1`` and are written out in full; a test asserts that each
  literal still matches what the rule produces, so a change to the rule fails
  here rather than silently re-blessing every digest in this module.
* **A refusal is asserted through the logger.** Every refusal test captures the
  ``alltips_scraper.jsoncontent_v1`` channel and asserts the exact template line.
  That is how "reported, never published" and "value-free" are both pinned: a
  line that carried a payload, a digest, a file name or a path would not be that
  line.
* **Read-only is asserted from the filesystem.** The write refusals are checked
  against the directory contents taken before and after, so a refusal cannot be a
  no-op that merely raises.
* **The two silent states are pinned as silent.** An absent manifest and a type
  key no manifest names are asserted with ``assertNoLogs``: "not published yet"
  must not look like a failure, because that is the state the shipped empty
  manifest leaves the endpoint in.

See ``docs/API_V1_CONTRACT.md`` for the envelope this content feeds and
``tests_storage_v1.py`` for the durable provider's own contract.
"""

import ast
import json
import logging
import socket
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from . import jsoncontent_v1
from .canonical_json_v1 import DIGEST_HEX_LENGTH, canonical_payload_sha256
from .jsoncontent_v1 import (
    CANONICAL_CONTENT_ROOT,
    LOGGER_NAME,
    MANIFEST_FILENAME,
    MANIFEST_KEYS,
    MANIFEST_SCHEMA_VERSION,
    OPERATION_CLEAR,
    OPERATION_STORE,
    RECORD_ALLOWED_KEYS,
    RECORD_FILENAME_SUFFIX,
    RECORD_KEYS,
    RECORD_TIMESTAMP_KEY,
    REFUSED_CONTENT_MESSAGE,
    WRITE_REFUSED_MESSAGE,
    JsonSnapshotProvider,
    ReadOnlyContentError,
)
from .readmodel_v1 import (
    SNAPSHOT_KEYS,
    SNAPSHOT_SCHEMA_VERSION,
    get_snapshot_provider,
    load_snapshot,
    set_snapshot_provider,
    store_snapshot,
)


# ---------------------------------------------------------------------------
# Fixed locations: the content root this package ships and its empty manifest.
# ---------------------------------------------------------------------------

APP_PACKAGE_DIR = Path(jsoncontent_v1.__file__).resolve().parent
SHIPPED_MANIFEST_PATH = CANONICAL_CONTENT_ROOT / MANIFEST_FILENAME

# The shipped manifest, as text. It is pinned exactly: this file is tracked in
# git, so its bytes are the deployment's content decision, not an implementation
# detail. It names no type key, which is what keeps the endpoint's existing
# client-safe answer for content that has not been published.
SHIPPED_MANIFEST_TEXT = '{"schema_version": 1, "snapshots": {}}'

# ---------------------------------------------------------------------------
# Fixed inputs. Every value is an explicit literal: no test reads a clock, and
# each digest was computed once from ``canonical_json_v1`` and is embedded whole.
# ---------------------------------------------------------------------------

TYPE_KEY = 'bet_of_the_day'
OTHER_TYPE_KEY = 'daily_accumulator'

RECORD_FILENAME = 'bet_of_the_day.json'
OTHER_RECORD_FILENAME = 'daily_accumulator.json'

FETCHED_AT_TEXT = '2026-09-28T17:12:03+00:00'
WIRE_FETCHED_AT_TEXT = '2026-09-28T17:12:03Z'
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

# A payload that is valid JSON but not a mapping: it must be refused, so its
# correct digest is pinned to prove the shape check is what refuses it.
LIST_PAYLOAD = [1, 2]
LIST_PAYLOAD_SHA256 = (
    '49a64717d5d4cb19952e6eac2946415cf6879adacf9908e7d872332d32c6e684')

# A payload whose text contains non-ASCII characters: the digest rule escapes
# them, so the file the test writes and the payload it holds are not byte equal.
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

# Shape-valid but wrong: this is what a rewritten payload file looks like.
MISMATCHED_SHA256 = '0' * DIGEST_HEX_LENGTH

# Why a value was refused, as the tokens the provider reports. They are listed
# here so a test can assert the vocabulary is the one this module pins.
REASONS = (
    jsoncontent_v1.REASON_MANIFEST_READ,
    jsoncontent_v1.REASON_MANIFEST_JSON,
    jsoncontent_v1.REASON_MANIFEST_SHAPE,
    jsoncontent_v1.REASON_MANIFEST_VERSION,
    jsoncontent_v1.REASON_ENTRY_SHAPE,
    jsoncontent_v1.REASON_SCHEMA_VERSION,
    jsoncontent_v1.REASON_RECORD_READ,
    jsoncontent_v1.REASON_RECORD_JSON,
    jsoncontent_v1.REASON_DIGEST,
    jsoncontent_v1.REASON_PAYLOAD_SHAPE,
    jsoncontent_v1.REASON_FETCHED_AT,
)

# The recorded record shape a load answers with, spelled out rather than derived.
RECORD = {
    'schema_version': SNAPSHOT_SCHEMA_VERSION,
    'type_key': TYPE_KEY,
    'payload': PAYLOAD,
    'fetched_at': FETCHED_AT,
}

# What the provider may import: the standard library it needs and the two
# sibling modules that own the record vocabulary and the digest rule. Anything
# else - a framework, an ORM, a settings reader, a client - would make this
# provider unusable where shipped content is the point.
ALLOWED_SOURCE_MODULES = frozenset({
    'json', 'logging', 'copy', 'datetime', 'pathlib',
    'canonical_json_v1', 'readmodel_v1',
})

# Source tokens that must not appear at all: naming one is how a read-only
# content provider would come to read configuration, reach the network, or write.
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
)


class CanonicalContentTestCase(SimpleTestCase):
    """Give every test its own temporary canonical-content directory.

    Nothing in this module touches the directory the package ships: a test writes
    the manifest and the payload files it means to test, so both the "published"
    and the "not published" states are explicit rather than inherited.
    """

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.provider = JsonSnapshotProvider(self.root)

    # -- writing content ----------------------------------------------------

    def write_text(self, name, text):
        """Write ``text`` as UTF-8 under this test's content root."""
        (self.root / name).write_text(text, encoding='utf-8')

    def write_bytes(self, name, raw):
        """Write ``raw`` bytes under this test's content root, undecoded."""
        (self.root / name).write_bytes(raw)

    def write_manifest(self, manifest):
        """Write ``manifest`` as the manifest file, indented and readable."""
        self.write_text(MANIFEST_FILENAME, json.dumps(manifest, indent=2))

    def publish(self, type_key=TYPE_KEY, *, payload=PAYLOAD,
                sha256=PAYLOAD_SHA256, filename=RECORD_FILENAME,
                fetched_at=FETCHED_AT_TEXT, indent=None):
        """Publish one payload file and a manifest naming it under ``type_key``.

        ``fetched_at=None`` omits the key altogether, and ``indent`` reformats
        the payload file, so a test can vary one thing at a time.
        """
        self.write_text(filename, json.dumps(payload, indent=indent))
        self.write_entries({
            type_key: self.entry(
                filename=filename, sha256=sha256, fetched_at=fetched_at),
        })

    def write_entry(self, entry, type_key=TYPE_KEY):
        """Write a manifest whose one entry is ``entry``, verbatim."""
        self.write_manifest({
            'schema_version': MANIFEST_SCHEMA_VERSION,
            'snapshots': {type_key: entry},
        })

    def write_entries(self, entries):
        """Write a manifest whose snapshots are exactly ``entries``."""
        self.write_manifest({
            'schema_version': MANIFEST_SCHEMA_VERSION,
            'snapshots': entries,
        })

    def entry(self, *, filename=RECORD_FILENAME, sha256=PAYLOAD_SHA256,
              fetched_at=FETCHED_AT_TEXT,
              record_version=SNAPSHOT_SCHEMA_VERSION):
        """Return one manifest entry of the documented shape."""
        entry = {
            'schema_version': record_version,
            'file': filename,
            'sha256': sha256,
        }
        if fetched_at is not None:
            entry[RECORD_TIMESTAMP_KEY] = fetched_at
        return entry

    # -- reading the filesystem and the logger ------------------------------

    def root_contents(self):
        """Return every file name and byte payload currently under the root."""
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob('*'))
            if path.is_file()
        }

    @contextmanager
    def refusal(self, expected_reason, type_key=TYPE_KEY):
        """Assert the read reports exactly one refusal, of ``expected_reason``.

        The body runs inside the captured channel, so a test asserts the answer
        and the report of the same call in one place.
        """
        with self.assertLogs(LOGGER_NAME, level='ERROR') as captured:
            yield
        self.assertEqual(len(captured.records), 1)
        report = captured.records[0]
        self.assertEqual(report.levelno, logging.ERROR)
        self.assertEqual(report.name, LOGGER_NAME)
        self.assertEqual(
            report.getMessage(),
            REFUSED_CONTENT_MESSAGE % (type_key, expected_reason),
        )

    def assert_write_refused(self, call, operation):
        """Assert the write ``call`` is refused: reported once, then raised.

        The refused operation is named by the fixed operation token, and the log
        line and the exception carry the same one: a caller cannot mistake a
        refused write for a published artifact.
        """
        with self.assertLogs(LOGGER_NAME, level='ERROR') as captured:
            with self.assertRaises(ReadOnlyContentError) as raised:
                call()
        self.assertEqual(len(captured.records), 1)
        message = WRITE_REFUSED_MESSAGE % operation
        self.assertEqual(captured.records[0].getMessage(), message)
        self.assertEqual(str(raised.exception), message)


class ReadOnlyProviderVocabularyTests(SimpleTestCase):
    """The provider's public surface, and the names its contract is stated in."""

    def test_the_module_publishes_the_documented_names(self):
        for name in (
            'LOGGER_NAME', 'CANONICAL_CONTENT_ROOT', 'MANIFEST_FILENAME',
            'MANIFEST_SCHEMA_VERSION', 'MANIFEST_KEYS', 'RECORD_KEYS',
            'RECORD_TIMESTAMP_KEY', 'RECORD_ALLOWED_KEYS',
            'RECORD_FILENAME_SUFFIX', 'RECORD_FILENAME_FORBIDDEN_CHARACTERS',
            'REFUSED_CONTENT_MESSAGE', 'WRITE_REFUSED_MESSAGE',
            'OPERATION_STORE', 'OPERATION_CLEAR', 'ReadOnlyContentError',
            'JsonSnapshotProvider',
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(jsoncontent_v1, name))

    def test_every_reason_is_a_distinct_fixed_token(self):
        self.assertEqual(len(set(REASONS)), len(REASONS))
        for reason in REASONS:
            with self.subTest(reason=reason):
                self.assertIsInstance(reason, str)
                self.assertEqual(reason, reason.lower())
                self.assertEqual(reason.strip(), reason)
                self.assertNotIn('%', reason)

    def test_the_logger_is_the_documented_channel(self):
        self.assertEqual(LOGGER_NAME, 'alltips_scraper.jsoncontent_v1')
        self.assertIs(
            jsoncontent_v1.logger, logging.getLogger(LOGGER_NAME))

    def test_the_public_surface_is_the_seams_three_methods_and_the_root(self):
        public = sorted(
            name for name in dir(JsonSnapshotProvider)
            if not name.startswith('_')
        )

        self.assertEqual(public, ['clear', 'load', 'root', 'store'])
        for name in ('clear', 'load', 'store'):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(JsonSnapshotProvider, name)))
        self.assertIsInstance(
            JsonSnapshotProvider.__dict__['root'], property)

    def test_the_log_templates_take_exactly_what_they_claim(self):
        self.assertEqual(REFUSED_CONTENT_MESSAGE.count('%s'), 2)
        self.assertIn('type_key=', REFUSED_CONTENT_MESSAGE)
        self.assertIn('reason=', REFUSED_CONTENT_MESSAGE)
        self.assertEqual(WRITE_REFUSED_MESSAGE.count('%s'), 1)
        self.assertIn('operation=', WRITE_REFUSED_MESSAGE)

    def test_the_two_write_operations_are_distinct_named_tokens(self):
        self.assertEqual(OPERATION_STORE, 'store')
        self.assertEqual(OPERATION_CLEAR, 'clear')

    def test_a_refused_write_is_a_runtime_error(self):
        self.assertTrue(issubclass(ReadOnlyContentError, RuntimeError))

    def test_the_manifest_and_entry_vocabularies_are_the_documented_ones(self):
        self.assertEqual(MANIFEST_KEYS, frozenset({'schema_version', 'snapshots'}))
        self.assertEqual(MANIFEST_SCHEMA_VERSION, 1)
        self.assertEqual(
            RECORD_KEYS, frozenset({'schema_version', 'file', 'sha256'}))
        self.assertEqual(RECORD_ALLOWED_KEYS, RECORD_KEYS | {'fetched_at'})
        self.assertEqual(RECORD_TIMESTAMP_KEY, 'fetched_at')
        self.assertEqual(RECORD_FILENAME_SUFFIX, '.json')

    def test_the_module_imports_only_the_standard_library_and_its_two_siblings(self):
        source = Path(jsoncontent_v1.__file__).read_text(encoding='utf-8')
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or '').split('.')[0])

        self.assertEqual(names - ALLOWED_SOURCE_MODULES, set())
        self.assertTrue(names)

    def test_the_module_names_nothing_that_could_read_configuration_or_write(self):
        source = Path(jsoncontent_v1.__file__).read_text(
            encoding='utf-8').lower()

        for token in FORBIDDEN_SOURCE_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, source)


class ShippedCanonicalContentTests(SimpleTestCase):
    """The artifacts this package ships, and the provider's default root."""

    def test_the_default_root_is_the_content_directory_of_this_package(self):
        self.assertEqual(JsonSnapshotProvider().root, CANONICAL_CONTENT_ROOT)
        self.assertEqual(
            CANONICAL_CONTENT_ROOT, APP_PACKAGE_DIR / 'content' / 'v1')
        self.assertTrue(CANONICAL_CONTENT_ROOT.is_dir())

    def test_a_root_may_be_given_explicitly_as_a_path(self):
        with tempfile.TemporaryDirectory() as name:
            with self.subTest(root=name):
                self.assertEqual(
                    JsonSnapshotProvider(name).root, Path(name))

    def test_the_shipped_manifest_is_the_pinned_empty_manifest(self):
        self.assertEqual(
            SHIPPED_MANIFEST_PATH.read_text(encoding='utf-8'),
            SHIPPED_MANIFEST_TEXT,
        )

    def test_the_shipped_manifest_names_no_type_key(self):
        manifest = json.loads(SHIPPED_MANIFEST_TEXT)

        self.assertEqual(set(manifest), set(MANIFEST_KEYS))
        self.assertEqual(manifest['schema_version'], MANIFEST_SCHEMA_VERSION)
        self.assertEqual(manifest['snapshots'], {})

    def test_the_shipped_content_directory_holds_only_the_manifest_and_its_readme(self):
        self.assertEqual(
            sorted(path.name for path in CANONICAL_CONTENT_ROOT.iterdir()),
            ['README.md', 'manifest.json'],
        )

    def test_the_shipped_provider_publishes_nothing_and_reports_nothing(self):
        provider = JsonSnapshotProvider()

        for type_key in (TYPE_KEY, OTHER_TYPE_KEY):
            with self.subTest(type_key=type_key):
                with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
                    self.assertIsNone(provider.load(type_key))

    def test_the_shipped_provider_is_accepted_by_the_seam(self):
        provider = JsonSnapshotProvider()
        set_snapshot_provider(provider)
        self.addCleanup(set_snapshot_provider, None)

        self.assertIs(get_snapshot_provider(), provider)
        with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
            self.assertIsNone(load_snapshot(TYPE_KEY))

    def test_the_pinned_digests_are_the_ones_the_canonical_rule_produces(self):
        pinned = (
            (PAYLOAD, PAYLOAD_SHA256),
            (LIST_PAYLOAD, LIST_PAYLOAD_SHA256),
            (UNICODE_PAYLOAD, UNICODE_PAYLOAD_SHA256),
        )

        for payload, digest in pinned:
            with self.subTest(payload=payload):
                self.assertEqual(len(digest), DIGEST_HEX_LENGTH)
                self.assertEqual(canonical_payload_sha256(payload), digest)


class PublishedRecordReadTests(CanonicalContentTestCase):
    """A published entry is read back as the seam's record, or not at all."""

    def test_a_published_record_is_read_back_as_the_seam_record(self):
        self.publish()

        self.assertEqual(self.provider.load(TYPE_KEY), RECORD)

    def test_the_record_carries_the_seams_key_set_and_version(self):
        self.publish()
        record = self.provider.load(TYPE_KEY)

        self.assertEqual(set(record), set(SNAPSHOT_KEYS))
        self.assertEqual(record['schema_version'], SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(record['type_key'], TYPE_KEY)
        self.assertEqual(record['payload'], PAYLOAD)
        self.assertEqual(record['fetched_at'], FETCHED_AT)

    def test_the_instant_is_the_manifests_and_is_stated_in_utc(self):
        for text in (FETCHED_AT_TEXT, WIRE_FETCHED_AT_TEXT,
                     OFFSET_FETCHED_AT_TEXT):
            with self.subTest(text=text):
                self.publish(fetched_at=text)
                self.assertEqual(
                    self.provider.load(TYPE_KEY)['fetched_at'], FETCHED_AT)

    def test_the_instant_is_a_datetime_and_not_the_manifest_text(self):
        self.publish()
        fetched_at = self.provider.load(TYPE_KEY)['fetched_at']

        self.assertIsInstance(fetched_at, datetime)
        self.assertIsNot(fetched_at, FETCHED_AT_TEXT)
        self.assertIs(fetched_at.tzinfo, timezone.utc)
        self.assertEqual(fetched_at.utcoffset(), timedelta(0))

    def test_a_reformatted_payload_file_still_matches_its_digest(self):
        self.publish(indent=4)

        self.assertEqual(self.provider.load(TYPE_KEY)['payload'], PAYLOAD)

    def test_a_payload_file_that_is_not_canonical_bytes_still_loads(self):
        # Pretty printed and left unescaped: the file is not the canonical byte
        # form, and the digest is a claim about the payload, not about the bytes.
        self.write_text(
            RECORD_FILENAME,
            json.dumps(UNICODE_PAYLOAD, indent=2, ensure_ascii=False),
        )
        self.write_entry(self.entry(sha256=UNICODE_PAYLOAD_SHA256))

        record = self.provider.load(TYPE_KEY)

        self.assertEqual(record['payload'], UNICODE_PAYLOAD)
        self.assertEqual(
            record['payload']['matches'][0]['match_title'],
            'Olimpia vs Cerro Porteño',
        )

    def test_two_loads_agree_and_hand_back_independent_records(self):
        self.publish()
        first = self.provider.load(TYPE_KEY)

        first['type_key'] = OTHER_TYPE_KEY
        first['payload']['count'] = 99
        first['payload']['matches'].append({'match_title': 'Injected'})
        second = self.provider.load(TYPE_KEY)

        self.assertEqual(second, RECORD)
        self.assertIsNot(second, first)
        self.assertEqual(second['payload'], PAYLOAD)

    def test_every_load_reads_the_files_again_rather_than_holding_a_record(self):
        self.publish()
        self.assertEqual(self.provider.load(TYPE_KEY), RECORD)

        self.write_entries({})
        with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
            self.assertIsNone(self.provider.load(TYPE_KEY))

        self.publish()
        self.assertEqual(self.provider.load(TYPE_KEY), RECORD)

    def test_two_providers_never_share_a_content_directory(self):
        self.publish()
        other_directory = tempfile.TemporaryDirectory()
        self.addCleanup(other_directory.cleanup)
        other = JsonSnapshotProvider(Path(other_directory.name))

        with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
            self.assertIsNone(other.load(TYPE_KEY))
        self.assertEqual(self.provider.load(TYPE_KEY), RECORD)

    def test_a_key_a_manifest_cannot_name_is_absent(self):
        self.publish()

        for type_key in (7, True, None, ('bet_of_the_day',)):
            with self.subTest(type_key=type_key):
                with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
                    self.assertIsNone(self.provider.load(type_key))

    def test_an_unhashable_key_raises_exactly_as_the_default_provider_does(self):
        self.publish()

        for type_key in ([], {}):
            with self.subTest(type_key=type_key):
                with self.assertRaises(TypeError):
                    self.provider.load(type_key)

    def test_an_absent_manifest_and_an_absent_entry_are_silent(self):
        with self.subTest(state='no manifest at all'):
            with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
                self.assertIsNone(self.provider.load(TYPE_KEY))

        self.write_entries({})
        with self.subTest(state='a manifest with no snapshots'):
            with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
                self.assertIsNone(self.provider.load(TYPE_KEY))

        self.publish(OTHER_TYPE_KEY)
        with self.subTest(state='another key published'):
            with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
                self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_content_root_that_does_not_exist_is_not_a_snapshot(self):
        missing = JsonSnapshotProvider(self.root / 'not-there')

        with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
            self.assertIsNone(missing.load(TYPE_KEY))
        self.assertEqual(self.root_contents(), {})

    def test_a_successful_read_writes_nothing_into_the_content_directory(self):
        self.publish()
        before = self.root_contents()

        self.assertEqual(self.provider.load(TYPE_KEY), RECORD)

        self.assertEqual(self.root_contents(), before)

    def test_a_refused_read_writes_nothing_into_the_content_directory(self):
        self.publish(payload=LIST_PAYLOAD, sha256=MISMATCHED_SHA256)
        before = self.root_contents()

        with self.assertLogs(LOGGER_NAME, level='ERROR'):
            self.assertIsNone(self.provider.load(TYPE_KEY))

        self.assertEqual(self.root_contents(), before)

    def test_a_refused_key_leaves_a_published_one_readable(self):
        self.write_text(RECORD_FILENAME, json.dumps(PAYLOAD))
        self.write_entries({
            TYPE_KEY: self.entry(
                filename='gone.json', sha256=MISMATCHED_SHA256),
            OTHER_TYPE_KEY: self.entry(),
        })

        with self.refusal(jsoncontent_v1.REASON_RECORD_READ):
            self.assertIsNone(self.provider.load(TYPE_KEY))
        self.assertEqual(self.provider.load(OTHER_TYPE_KEY)['payload'], PAYLOAD)

    def test_a_read_opens_no_socket_to_answer(self):
        self.publish()
        calls = []

        def forbidden(*args, **kwargs):
            calls.append(args)
            raise AssertionError('the provider reached outside its directory')

        for target in (socket.socket, socket.create_connection,
                       socket.getaddrinfo):
            patcher = mock.patch.object(socket, target.__name__, forbidden)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.assertEqual(self.provider.load(TYPE_KEY), RECORD)
        self.assertIsNone(self.provider.load(OTHER_TYPE_KEY))
        self.assertEqual(calls, [])


class ManifestRefusalTests(CanonicalContentTestCase):
    """A manifest that cannot be trusted is refused, never half-read."""

    def test_a_manifest_that_is_not_json_is_refused(self):
        for text, label in (
            ('not json at all', 'text that is not JSON'),
            ('{', 'a document that stops early'),
            ('{"schema_version": 1,}', 'a document with a trailing comma'),
        ):
            with self.subTest(manifest=label):
                self.write_text(MANIFEST_FILENAME, text)
                with self.refusal(jsoncontent_v1.REASON_MANIFEST_JSON):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_manifest_that_is_not_utf8_text_is_refused(self):
        self.write_bytes(
            MANIFEST_FILENAME, b'\xff\xfe{"schema_version": 1}')

        with self.refusal(jsoncontent_v1.REASON_MANIFEST_JSON):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_manifest_file_that_cannot_be_read_is_refused(self):
        # A directory where the manifest should be is not the manifest: the read
        # fails with an OS error instead of finding no file there.
        (self.root / MANIFEST_FILENAME).mkdir()

        with self.refusal(jsoncontent_v1.REASON_MANIFEST_READ):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_manifest_of_another_shape_is_refused(self):
        for text, label in (
            ('{}', 'an empty document'),
            ('[]', 'a list'),
            ('null', 'null'),
            ('"a string"', 'a string'),
            ('1', 'a number'),
            ('{"schema_version": 1}', 'a manifest with no snapshots key'),
            ('{"snapshots": {}}', 'a manifest with no version key'),
            ('{"schema_version": 1, "snapshots": {}, "extra": true}',
             'a manifest with a key nobody reads'),
            ('{"schema_version": 1, "snapshots": null}', 'null snapshots'),
            ('{"schema_version": 1, "snapshots": []}', 'a list of snapshots'),
            ('{"schema_version": 1, "snapshots": "snapshots"}',
             'text where snapshots should be'),
        ):
            with self.subTest(manifest=label):
                self.write_text(MANIFEST_FILENAME, text)
                with self.refusal(jsoncontent_v1.REASON_MANIFEST_SHAPE):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_manifest_of_another_version_is_refused(self):
        for version in (0, 2, -1, '1', None, 1.5, False):
            with self.subTest(schema_version=version):
                self.write_manifest({
                    'schema_version': version,
                    'snapshots': {},
                })
                with self.refusal(jsoncontent_v1.REASON_MANIFEST_VERSION):
                    self.assertIsNone(self.provider.load(TYPE_KEY))


class RecordRefusalTests(CanonicalContentTestCase):
    """An entry a reader cannot trust is refused, by the check it failed."""

    def test_an_entry_of_another_shape_is_refused(self):
        entry_of_shape = self.entry()
        cases = (
            (None, 'a named key whose value is null'),
            ('an entry', 'text where an entry should be'),
            ([], 'a list where an entry should be'),
            ({}, 'an entry with no keys'),
            ({'schema_version': SNAPSHOT_SCHEMA_VERSION,
              'file': RECORD_FILENAME}, 'an entry with no digest claim'),
            ({'schema_version': SNAPSHOT_SCHEMA_VERSION,
              'sha256': PAYLOAD_SHA256}, 'an entry with no file name'),
            ({'file': RECORD_FILENAME, 'sha256': PAYLOAD_SHA256},
             'an entry with no record version'),
            (dict(entry_of_shape, extra=True),
             'an entry with a key nobody reads'),
        )

        for entry, label in cases:
            with self.subTest(entry=label):
                self.write_entry(entry)
                with self.refusal(jsoncontent_v1.REASON_ENTRY_SHAPE):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_file_name_that_is_not_a_plain_json_name_is_refused(self):
        names = (
            'bet_of_the_day',
            'bet_of_the_day.txt',
            'bet_of_the_day.JSON',
            'bet_of_the_day.json.bak',
            '../manifest.json',
            'nested/bet_of_the_day.json',
            'nested\\bet_of_the_day.json',
            'C:bet_of_the_day.json',
            '/bet_of_the_day.json',
            '',
            '.',
            '..',
        )

        for name in names:
            with self.subTest(file=name):
                self.write_entry(self.entry(filename=name))
                with self.refusal(jsoncontent_v1.REASON_ENTRY_SHAPE):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_digest_claim_that_is_not_a_digest_is_refused(self):
        claims = (
            PAYLOAD_SHA256.upper(),
            PAYLOAD_SHA256[:-1],
            PAYLOAD_SHA256 + '0',
            'z' * DIGEST_HEX_LENGTH,
            '',
            None,
            7,
            True,
        )

        for claim in claims:
            with self.subTest(sha256=claim):
                self.write_entry(self.entry(sha256=claim))
                with self.refusal(jsoncontent_v1.REASON_ENTRY_SHAPE):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_an_entry_of_another_record_version_is_refused(self):
        self.publish()

        for version in (0, 2, None, '1'):
            with self.subTest(schema_version=version):
                self.write_entry(self.entry(record_version=version))
                with self.refusal(jsoncontent_v1.REASON_SCHEMA_VERSION):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_payload_file_that_is_not_there_is_refused(self):
        self.write_entry(self.entry())

        with self.refusal(jsoncontent_v1.REASON_RECORD_READ):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_payload_file_that_cannot_be_read_is_refused(self):
        (self.root / RECORD_FILENAME).mkdir()
        self.write_entry(self.entry())

        with self.refusal(jsoncontent_v1.REASON_RECORD_READ):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_payload_file_that_is_not_json_is_refused(self):
        for text, label in (
            ('', 'an empty file'),
            ('not json at all', 'text that is not JSON'),
            ('{', 'a document that stops early'),
        ):
            with self.subTest(payload=label):
                self.write_text(RECORD_FILENAME, text)
                self.write_entry(self.entry())
                with self.refusal(jsoncontent_v1.REASON_RECORD_JSON):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_payload_file_that_is_not_utf8_text_is_refused(self):
        self.write_bytes(RECORD_FILENAME, b'\xff\xfe{}')
        self.write_entry(self.entry())

        with self.refusal(jsoncontent_v1.REASON_RECORD_JSON):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_payload_the_digest_claim_does_not_match_is_refused(self):
        cases = (
            (PAYLOAD, MISMATCHED_SHA256, 'a claim that is not the payload\'s'),
            (LIST_PAYLOAD, PAYLOAD_SHA256, 'the payload and the claim swapped'),
        )

        for payload, claim, label in cases:
            with self.subTest(payload=label):
                self.publish(payload=payload, sha256=claim)
                with self.refusal(jsoncontent_v1.REASON_DIGEST):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_payload_that_is_not_a_mapping_is_refused(self):
        self.publish(payload=LIST_PAYLOAD, sha256=LIST_PAYLOAD_SHA256)
        with self.refusal(jsoncontent_v1.REASON_PAYLOAD_SHAPE):
            self.assertIsNone(self.provider.load(TYPE_KEY))

        for payload, label in (('text', 'text'), (7, 'a number')):
            with self.subTest(payload=label):
                # The claim is computed from the rule the pinned digests above
                # guard, so the shape check is what refuses the entry.
                self.publish(
                    payload=payload,
                    sha256=canonical_payload_sha256(payload),
                )
                with self.refusal(jsoncontent_v1.REASON_PAYLOAD_SHAPE):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_an_instant_that_is_not_an_offset_instant_is_refused(self):
        # ``None`` omits the key; the other texts name no instant, and a naive
        # text names one only if a reader assumes a zone it was never given.
        for text, label in (
            (NAIVE_FETCHED_AT_TEXT, 'a naive instant'),
            ('', 'an empty instant'),
            ('not an instant', 'text that is not an instant'),
            ('2026-09-28T17:12:03+00:00Z', 'text with a trailing Z'),
            ('2026-13-01T00:00:00+00:00', 'a month that does not exist'),
            (7, 'a number'),
            (True, 'a boolean'),
            ([], 'a list'),
            (None, 'no instant key at all'),
        ):
            with self.subTest(fetched_at=label):
                self.publish(fetched_at=text)
                with self.refusal(jsoncontent_v1.REASON_FETCHED_AT):
                    self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_an_instant_key_that_is_present_and_null_is_refused(self):
        self.publish()
        # Written by hand, because ``publish()`` omits the key when it is None.
        self.write_entry(dict(self.entry(), fetched_at=None))

        with self.refusal(jsoncontent_v1.REASON_FETCHED_AT):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_an_entry_with_several_defects_reports_the_first_check(self):
        # A mismatched digest and an absent instant are both defects; the digest
        # is reported, because the checks run in the documented order.
        self.publish(sha256=MISMATCHED_SHA256, fetched_at=None)

        with self.refusal(jsoncontent_v1.REASON_DIGEST):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_refusal_line_carries_no_value_from_the_content_directory(self):
        self.write_text(RECORD_FILENAME, json.dumps(LIST_PAYLOAD))
        self.write_entry(self.entry(sha256=MISMATCHED_SHA256))

        with self.assertLogs(LOGGER_NAME, level='ERROR') as captured:
            self.assertIsNone(self.provider.load(TYPE_KEY))
        message = captured.records[0].getMessage()

        self.assertEqual(
            message,
            REFUSED_CONTENT_MESSAGE % (TYPE_KEY, jsoncontent_v1.REASON_DIGEST),
        )
        for leaked in (
            RECORD_FILENAME,
            MANIFEST_FILENAME,
            str(self.root),
            MISMATCHED_SHA256,
            LIST_PAYLOAD_SHA256,
            json.dumps(LIST_PAYLOAD),
            'sha256',
            'file',
        ):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, message)


class ReadOnlyWriteTests(CanonicalContentTestCase):
    """The two write operations refuse, and a refused write changes nothing."""

    def test_storing_through_the_read_only_provider_is_refused(self):
        self.assert_write_refused(
            lambda: self.provider.store(TYPE_KEY, PAYLOAD), OPERATION_STORE)

    def test_clearing_through_the_read_only_provider_is_refused(self):
        self.assert_write_refused(self.provider.clear, OPERATION_CLEAR)

    def test_a_refused_write_leaves_the_content_directory_untouched(self):
        self.publish()
        before = self.root_contents()

        self.assert_write_refused(
            lambda: self.provider.store(TYPE_KEY, PAYLOAD), OPERATION_STORE)
        self.assert_write_refused(self.provider.clear, OPERATION_CLEAR)

        self.assertEqual(self.root_contents(), before)

    def test_a_refused_write_leaves_a_published_record_readable(self):
        self.publish()

        with self.assertLogs(LOGGER_NAME, level='ERROR') as captured:
            with self.assertRaises(ReadOnlyContentError):
                self.provider.store(OTHER_TYPE_KEY, LIST_PAYLOAD)
            with self.assertRaises(ReadOnlyContentError):
                self.provider.clear()

        self.assertEqual(len(captured.records), 2)
        self.assertEqual(self.provider.load(TYPE_KEY), RECORD)

    def test_a_write_with_arguments_no_store_could_accept_is_refused(self):
        # The refusal is about this provider, not about the arguments: a call
        # that could never be valid is refused exactly the same way.
        self.assert_write_refused(
            lambda: self.provider.store(
                TYPE_KEY, LIST_PAYLOAD, fetched_at=NAIVE_FETCHED_AT_TEXT),
            OPERATION_STORE,
        )
        self.assert_write_refused(
            lambda: self.provider.store('', {}), OPERATION_STORE)

    def test_the_seam_hands_a_write_through_to_the_refusal(self):
        set_snapshot_provider(JsonSnapshotProvider(self.root))
        self.addCleanup(set_snapshot_provider, None)

        self.assert_write_refused(
            lambda: store_snapshot(TYPE_KEY, PAYLOAD), OPERATION_STORE)
