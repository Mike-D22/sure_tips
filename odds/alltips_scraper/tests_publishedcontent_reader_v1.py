"""The deployed reader: the versioned endpoint served from published content.

Why this module exists
----------------------
``apps.AlltipsScraperConfig.ready()`` installs the read-only published-content
reader, so a deployed process answers ``GET /api/v1/tips/`` from the canonical
JSON that ships inside the application package. ``tests_jsoncontent_v1.py`` pins
that provider's own reading and refusing behaviour against temporary
directories; ``tests_startup_v1.py`` pins the install itself. What neither covers
is the deployed combination: a request served *through the endpoint* by the
reader the process actually installs, with the snapshot table present and unused.

So this module pins that path end to end. The claims are that the install point
really is the one a deployment runs, that a published record reaches a client as
the documented envelope without a single database query, that a stored row is
neither read nor needed while the reader is installed, and that a refused
publication is the endpoint's existing client-safe answer rather than a server
error.

Ground rules
------------
* **No network.** Every endpoint test inherits ``OfflineGuardMixin``, so an
  accidental outbound request raises instead of passing quietly.
* **No clock, no fixture.** The content here is written by the test into its own
  temporary directory, and every instant is a fixed literal. The shipped content
  directory is read, never written.
* **The digests are pinned literals.** They were computed once with
  ``canonical_json_v1`` and are written out in full; a test asserts each literal
  still matches what the rule produces, so a change to the rule fails here rather
  than silently re-blessing the manifest this module writes.
* **The endpoint's own vocabulary is what is asserted.** Envelope keys, the
  mapped tip fields and the error body all come from ``serializers_v1``, so a
  change to the envelope fails here instead of passing by coincidence.
* **The table is asserted to be untouched, not merely unread.** Where a row
  exists, its payload, digest and instant are compared, so "the reader ignores
  the row" cannot hide a write.

See ``docs/API_V1_CONTRACT.md`` for the envelope and ``docs/DEPLOYMENT.md`` for
how the content directory reaches the image.
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django.apps import apps
from django.test import SimpleTestCase, TestCase

from . import jsoncontent_v1
from .canonical_json_v1 import DIGEST_HEX_LENGTH, canonical_payload_sha256
from .jsoncontent_v1 import (
    CANONICAL_CONTENT_ROOT,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    RECORD_TIMESTAMP_KEY,
    JsonSnapshotProvider,
    ReadOnlyContentError,
)
from .models import SnapshotV1
from .readmodel_v1 import (
    SNAPSHOT_SCHEMA_VERSION,
    clear_snapshots,
    get_snapshot_provider,
    load_snapshot,
    set_snapshot_provider,
    store_snapshot,
)
from .serializers_v1 import (
    API_VERSION,
    ERROR_MESSAGES,
    ERROR_SOURCE_UNAVAILABLE,
    OPTIONAL_ENVELOPE_KEYS,
    SUCCESS_ENVELOPE_KEYS,
    UNIT_MATCH,
)
from .storage_v1 import DatabaseSnapshotProvider
from .tests_parser_contract import OfflineGuardMixin

# ---------------------------------------------------------------------------
# Fixed locations, and the one published record this module writes for itself.
# ---------------------------------------------------------------------------

APP_LABEL = 'alltips_scraper'
ENDPOINT_PATH = '/api/v1/tips/'

TIP_TYPE = 'bet_of_the_day'
OTHER_TYPE_KEY = 'daily_accumulator'
RECORD_FILENAME = 'bet_of_the_day.json'

SOURCE_DATE_TEXT = '2026-09-27'
FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3, tzinfo=timezone.utc)
FETCHED_AT_TEXT = '2026-09-28T17:12:03+00:00'
FETCHED_AT_Z = '2026-09-28T17:12:03Z'
OFFSET_FETCHED_AT_TEXT = '2026-09-28T20:42:03+03:30'

# A hand-written mirror of one frozen parser envelope: the payload a published
# record holds, and the canonical digest of it that the manifest claims.
PAYLOAD = {
    'date': SOURCE_DATE_TEXT,
    'total_tips': 1,
    'matches': [
        {'match_title': 'Arsenal vs Chelsea', 'prediction': 'Arsenal to win'},
    ],
    'count': 1,
    'source': 'freesupertips',
}
PAYLOAD_SHA256 = '36cfd8636014fb7edc478b3337e863a232957aecd19d24203abfed0044c42659'

# What a durable row holds while the published content says something else: only
# one of the two can be what a request is answered from.
STORE_ONLY_PAYLOAD = dict(
    PAYLOAD,
    total_tips=2,
    matches=[
        PAYLOAD['matches'][0],
        {'match_title': 'Inter vs Milan', 'prediction': 'Inter to win'},
    ],
    count=2,
)
STORE_ONLY_SHA256 = (
    'a2ad5ff026db2457b1e55113ed1a35e5350b50f226cb4145a5ea047f09302c4b'
)


class InstalledReaderTests(SimpleTestCase):
    """What the install point hands the seam, asserted through the seam."""

    def setUp(self):
        super().setUp()
        self.config = apps.get_app_config(APP_LABEL)
        installed = get_snapshot_provider()
        self.addCleanup(set_snapshot_provider, installed)
        set_snapshot_provider(None)

    def test_the_install_point_installs_the_published_content_reader(self):
        self.assertNotIsInstance(
            get_snapshot_provider(), JsonSnapshotProvider)

        self.config.ready()

        self.assertIsInstance(
            get_snapshot_provider(), JsonSnapshotProvider)

    def test_the_installed_reader_reads_the_content_directory_of_this_package(self):
        self.config.ready()

        self.assertEqual(
            get_snapshot_provider().root, CANONICAL_CONTENT_ROOT)

    def test_the_installed_reader_is_not_the_durable_store(self):
        self.config.ready()

        self.assertNotIsInstance(
            get_snapshot_provider(), DatabaseSnapshotProvider)

    def test_the_shipped_content_publishes_nothing_and_reports_nothing(self):
        self.config.ready()

        with self.assertNoLogs(jsoncontent_v1.LOGGER_NAME, level='ERROR'):
            self.assertIsNone(load_snapshot(TIP_TYPE))
            self.assertIsNone(load_snapshot(OTHER_TYPE_KEY))

    def test_the_installed_reader_refuses_both_of_the_seams_writes(self):
        self.config.ready()

        with self.assertRaises(ReadOnlyContentError):
            store_snapshot(TIP_TYPE, PAYLOAD, fetched_at=FETCHED_AT)
        with self.assertRaises(ReadOnlyContentError):
            clear_snapshots()


class PublishedContentTestCase(OfflineGuardMixin, TestCase):
    """The endpoint driven by a reader whose content this test controls.

    The reader is the deployed one, installed exactly as startup installs it;
    only its content root is this test's own temporary directory, so a published
    record can be written while the shipped directory stays untouched.
    """

    def setUp(self):
        super().setUp()
        installed = get_snapshot_provider()
        self.addCleanup(set_snapshot_provider, installed)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        set_snapshot_provider(JsonSnapshotProvider(self.root))

    def publish(self, *, payload=PAYLOAD, sha256=PAYLOAD_SHA256,
                filename=RECORD_FILENAME, fetched_at=FETCHED_AT_TEXT,
                type_key=TIP_TYPE):
        """Write one record and the manifest entry that names it."""
        (self.root / filename).write_text(
            json.dumps(payload), encoding='utf-8')
        manifest = {
            'schema_version': MANIFEST_SCHEMA_VERSION,
            'snapshots': {
                type_key: {
                    'schema_version': SNAPSHOT_SCHEMA_VERSION,
                    'file': filename,
                    'sha256': sha256,
                    RECORD_TIMESTAMP_KEY: fetched_at,
                },
            },
        }
        (self.root / MANIFEST_FILENAME).write_text(
            json.dumps(manifest), encoding='utf-8')

    def get(self, **params):
        return self.client.get(ENDPOINT_PATH, params)

    def seed_durable_row(self, payload=STORE_ONLY_PAYLOAD):
        """Store a durable row through the durable provider, not the reader."""
        return DatabaseSnapshotProvider().store(
            TIP_TYPE, payload, fetched_at=FETCHED_AT)

    def stored_row(self):
        return SnapshotV1.objects.get(pk=TIP_TYPE)

    def root_contents(self):
        """Return every file name and byte payload currently under the root."""
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob('*'))
            if path.is_file()
        }


class PublishedContentEndpointTests(PublishedContentTestCase):
    """A published record reaches a client as the documented envelope."""

    def test_a_published_record_is_served_as_the_documented_envelope(self):
        self.publish()

        response = self.get(type=TIP_TYPE)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body), set(SUCCESS_ENVELOPE_KEYS) | set(OPTIONAL_ENVELOPE_KEYS))
        self.assertEqual(body['api_version'], API_VERSION)
        self.assertEqual(body['type'], TIP_TYPE)
        self.assertEqual(body['unit'], UNIT_MATCH)
        self.assertEqual(body['count'], 1)
        self.assertEqual(len(body['tips']), 1)
        self.assertEqual(body['tips'][0]['match_title'], 'Arsenal vs Chelsea')
        self.assertEqual(body['tips'][0]['prediction'], 'Arsenal to win')
        self.assertEqual(body['source']['date_text'], SOURCE_DATE_TEXT)

    def test_the_published_instant_is_the_manifests_and_not_this_machines(self):
        self.publish(fetched_at=OFFSET_FETCHED_AT_TEXT)

        body = self.get(type=TIP_TYPE).json()

        # The manifest states an offset instant, and the envelope publishes that
        # same moment in UTC: the reader read the artifact, not a clock.
        self.assertEqual(body['source']['fetched_at'], FETCHED_AT_Z)

    def test_the_answer_is_read_without_a_single_database_query(self):
        self.publish()

        with self.assertNumQueries(0):
            response = self.get(type=TIP_TYPE)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SnapshotV1.objects.exists())

    def test_a_request_writes_nothing_into_the_content_directory(self):
        self.publish()
        before = self.root_contents()

        self.get(type=TIP_TYPE)

        self.assertEqual(self.root_contents(), before)

    def test_a_key_the_manifest_does_not_name_is_the_documented_503(self):
        self.publish()

        with self.assertNoLogs(jsoncontent_v1.LOGGER_NAME, level='ERROR'):
            response = self.get(type=OTHER_TYPE_KEY)

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(set(body), {'api_version', 'error'})
        self.assertEqual(body['api_version'], API_VERSION)
        self.assertEqual(body['error']['code'], ERROR_SOURCE_UNAVAILABLE)
        self.assertEqual(
            body['error']['message'], ERROR_MESSAGES[ERROR_SOURCE_UNAVAILABLE])
        self.assertFalse(SnapshotV1.objects.exists())

    def test_an_empty_content_root_is_the_documented_503(self):
        with self.assertNoLogs(jsoncontent_v1.LOGGER_NAME, level='ERROR'):
            response = self.get(type=TIP_TYPE)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()['error']['code'], ERROR_SOURCE_UNAVAILABLE)

    def test_a_payload_the_digest_claim_does_not_match_is_a_503(self):
        self.publish(sha256='f' * 64)

        with self.assertLogs(jsoncontent_v1.LOGGER_NAME, level='ERROR') as logs:
            response = self.get(type=TIP_TYPE)

        # Malformed content is reported and refused, never published as a 500.
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(logs.records), 1)
        self.assertIn('reason=digest', logs.records[0].getMessage())


class PublishedContentIsTheOnlySourceTests(PublishedContentTestCase):
    """While this reader is installed, a stored row is neither read nor needed."""

    def test_a_stored_row_does_not_override_the_published_record(self):
        self.seed_durable_row()
        self.publish()

        body = self.get(type=TIP_TYPE).json()

        # The published record holds one tip; the stored row holds two.
        self.assertEqual(body['count'], 1)
        self.assertEqual(len(body['tips']), 1)
        self.assertEqual(SnapshotV1.objects.count(), 1)

    def test_a_stored_row_is_never_read_and_stays_exactly_as_it_was(self):
        self.seed_durable_row()
        before = self.stored_row()

        response = self.get(type=TIP_TYPE)

        # Nothing is published here, so the documented answer is a 503 even
        # though a usable row exists: that row is not this process's source.
        self.assertEqual(response.status_code, 503)
        row = self.stored_row()
        self.assertEqual(row.payload, before.payload)
        self.assertEqual(row.payload_sha256, before.payload_sha256)
        self.assertEqual(row.fetched_at, before.fetched_at)

    def test_a_write_through_the_seam_is_refused_and_stores_no_row(self):
        self.publish()

        with self.assertRaises(ReadOnlyContentError):
            store_snapshot(TIP_TYPE, PAYLOAD, fetched_at=FETCHED_AT)

        self.assertFalse(SnapshotV1.objects.exists())


class PublishedContentVocabularyTests(SimpleTestCase):
    """The literals this module writes are the ones the shared rule produces."""

    def test_the_pinned_digests_are_the_ones_the_canonical_rule_produces(self):
        self.assertEqual(len(PAYLOAD_SHA256), DIGEST_HEX_LENGTH)
        self.assertEqual(canonical_payload_sha256(PAYLOAD), PAYLOAD_SHA256)
        self.assertEqual(len(STORE_ONLY_SHA256), DIGEST_HEX_LENGTH)
        self.assertEqual(
            canonical_payload_sha256(STORE_ONLY_PAYLOAD), STORE_ONLY_SHA256)
