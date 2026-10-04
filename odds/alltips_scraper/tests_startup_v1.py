"""Startup tests for the snapshot provider this deployment installs.

Why this module exists
----------------------
``jsoncontent_v1`` is the read-only published-content reader; nothing reads
through it until the app that owns the versioned route installs it. That install
happens in ``AlltipsScraperConfig.ready()``, which is exactly the wiring a suite
tends to leave untested: a provider that is never installed, or installed only
after a request has already been answered, stays invisible until a deployment
depends on it.

So this module pins the wiring end to end. The deployed claim is that the
versioned endpoint answers from reviewed published content through the seam it
always used, and never from the snapshot table; the wiring claim is that Django
itself selects the one declared app config, and that the config's ``ready()``
installs the read-only reader while reading neither a database nor any
configuration.

``storage_v1`` is still exercised here, in the two places this module needs it: a
row the durable store holds is invisible to the reader this process installs, and
the seam refuses a write instead of publishing a shipped artifact.

Ground rules
------------
* **No network.** Every endpoint test inherits ``OfflineGuardMixin``, so an
  accidental outbound request raises instead of passing quietly.
* **No configuration knob.** The tests assert there is nothing to configure: the
  install is unconditional, and no branch, flag or setting takes part in it.
* **The shipped file is what is pinned.** ``apps.py`` is read with the ``ast``
  module, so the tests describe the file that ships rather than a copy of its
  text.
* **No clock.** Both stamps here are fixed literals, and the only payload is a
  hand-written mirror of one frozen parser envelope.
"""

import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.test import SimpleTestCase, TestCase
from django.urls import resolve

from . import jsoncontent_v1
from .apps import AlltipsScraperConfig
from .jsoncontent_v1 import (
    CANONICAL_CONTENT_ROOT,
    JsonSnapshotProvider,
    ReadOnlyContentError,
)
from .models import SnapshotV1
from .readmodel_v1 import (
    SNAPSHOT_KEYS,
    SNAPSHOT_SCHEMA_VERSION,
    get_snapshot_provider,
    load_snapshot,
    set_snapshot_provider,
    store_snapshot,
)
from .serializers_v1 import (
    API_VERSION,
    ERROR_MESSAGES,
    ERROR_SOURCE_UNAVAILABLE,
    KICKOFF_AT,
    KICKOFF_TIME_VERIFIED,
    MATCH_UNIT_TIP_KEYS,
    OPTIONAL_ENVELOPE_KEYS,
    SUCCESS_ENVELOPE_KEYS,
    UNIT_CARD,
    UNIT_MATCH,
)
from .storage_v1 import (
    LOGGER_NAME,
    REASON_DIGEST,
    DatabaseSnapshotProvider,
    canonical_payload_sha256,
)
from .tests_parser_contract import OfflineGuardMixin
from .views_v1 import FILTER_KEYS

# ---------------------------------------------------------------------------
# Fixed locations
# ---------------------------------------------------------------------------

APP_LABEL = 'alltips_scraper'
APPS_PATH = Path(__file__).resolve().parent / 'apps.py'

ENDPOINT_PATH = '/api/v1/tips/'
ENDPOINT_ROUTE_NAME = 'tips_v1:tips'

TIP_TYPE = 'bet_of_the_day'
OTHER_TYPE_KEY = 'daily_accumulator'

# ---------------------------------------------------------------------------
# Fixed inputs. Nothing below reads a clock or a fixture.
# ---------------------------------------------------------------------------

SOURCE_DATE_TEXT = '2026-09-27'
FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3, tzinfo=timezone.utc)
FETCHED_AT_Z = '2026-09-28T17:12:03Z'

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

# The same envelope with one value changed, so a rewritten row is provably a
# different row rather than a re-read of the first one.
CHANGED_PAYLOAD = dict(
    PAYLOAD,
    total_tips=2,
    matches=[
        PAYLOAD['matches'][0],
        {'match_title': 'Inter vs Milan', 'prediction': 'Inter to win'},
    ],
    count=2,
)

# A second tip type with its own shape: one accumulator card holding one leg, so
# a second key proves it is answered from its own row and its own unit.
OTHER_PAYLOAD = {
    'date': SOURCE_DATE_TEXT,
    'total_accumulators': 1,
    'accumulators': [
        {
            'category': 'Daily Accumulator',
            'stake': 10.0,
            'returns': 28.56,
            'total_odds': 2.86,
            'matches': [
                {
                    'date': SOURCE_DATE_TEXT,
                    'time': '19:45',
                    'match_title': 'Arsenal vs Chelsea',
                    'teams': ['Arsenal', 'Chelsea'],
                    'prediction': 'Arsenal to win',
                    'opponent_text': 'vs Chelsea',
                    'tip_reason': 'Reason for Arsenal vs Chelsea',
                    'match_url': '/betting-tips/arsenal-vs-chelsea/',
                },
            ],
            'matches_count': 1,
        },
    ],
    'count': 1,
    'source': 'freesupertips',
}

# What must never appear in a body or a log line this wiring can produce: a store
# detail, an exception class, a SQL fragment, or a source name. The success body
# is excluded on purpose - it publishes the source label by contract.
LEAK_TOKENS = (
    'http://', 'https://', 'freesupertips', 'Traceback', 'File "',
    'DatabaseError', 'IntegrityError', 'ValueError', 'TypeError',
    'sqlite', 'UPDATE', 'INSERT', 'DELETE', 'payload_sha256',
    'storage_v1', 'snapshot_v1', 'alltips_scraper_snapshot_v1',
)


class AppConfigSelectionTests(SimpleTestCase):
    """One app config is declared, and Django itself is the one that loads it."""

    def read_source(self):
        return APPS_PATH.read_text(encoding='utf-8')

    def module_classes(self):
        return [
            node for node in ast.parse(self.read_source()).body
            if isinstance(node, ast.ClassDef)
        ]

    def the_config_class(self):
        classes = self.module_classes()
        self.assertEqual(len(classes), 1)
        return classes[0]

    def test_the_module_declares_exactly_one_class(self):
        self.assertEqual(
            [node.name for node in self.module_classes()],
            ['AlltipsScraperConfig'],
        )

    def test_the_class_extends_app_config_and_nothing_else(self):
        bases = self.the_config_class().bases

        self.assertEqual(len(bases), 1)
        self.assertIsInstance(bases[0], ast.Name)
        self.assertEqual(bases[0].id, 'AppConfig')

    def test_the_class_body_is_the_app_name_and_ready(self):
        declared = []
        for node in self.the_config_class().body:
            if isinstance(node, ast.Assign):
                declared.extend(
                    target.id for target in node.targets
                    if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.FunctionDef):
                declared.append(node.name)

        self.assertEqual(declared, ['name', 'ready'])
        self.assertIn("name = 'alltips_scraper'", self.read_source())

    def test_the_config_declares_no_default_flag(self):
        # ``default = True`` would make this config compete with every other one
        # for the bare app entry; the class body declares the app name only, so
        # Django's own selection cannot be ambiguous.
        declared = {
            target.id
            for node in self.the_config_class().body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }

        self.assertEqual(declared, {'name'})
        self.assertNotIn('default', declared)

    def test_the_config_loads_from_the_bare_app_entry(self):
        config = apps.get_app_config(APP_LABEL)

        self.assertIsInstance(config, AlltipsScraperConfig)
        self.assertEqual(config.name, APP_LABEL)
        self.assertEqual(config.label, APP_LABEL)
        self.assertEqual(config.path, str(APPS_PATH.parent))

    def test_the_app_is_registered_under_this_config_alone(self):
        loaded = [
            config for config in apps.get_app_configs()
            if config.label == APP_LABEL
        ]

        self.assertEqual(loaded, [apps.get_app_config(APP_LABEL)])

    def test_the_durable_table_belongs_to_that_config(self):
        self.assertIs(
            SnapshotV1._meta.app_config, apps.get_app_config(APP_LABEL))

    def test_django_finished_loading_every_app(self):
        # ``ready()`` has already run for every app in this process, which is what
        # makes the install point below a real startup step, not a promise.
        self.assertTrue(apps.apps_ready)
        self.assertTrue(apps.models_ready)
        self.assertTrue(apps.ready)


class ReadySourceTests(SimpleTestCase):
    """``ready()`` imports what it needs, and installs one unconditional provider."""

    def read_source(self):
        return APPS_PATH.read_text(encoding='utf-8')

    def ready_node(self):
        classes = [
            node for node in ast.parse(self.read_source()).body
            if isinstance(node, ast.ClassDef)
        ]
        self.assertEqual(len(classes), 1)
        ready = [
            node for node in classes[0].body
            if isinstance(node, ast.FunctionDef) and node.name == 'ready'
        ]
        self.assertEqual(len(ready), 1)
        return ready[0]

    def imported_names(self, node):
        names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                names.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                names.add(child.module)
                names.update(alias.name for alias in child.names)
        return names

    def test_the_module_imports_only_the_app_config_base(self):
        module_imports = set()
        for node in ast.parse(self.read_source()).body:
            if isinstance(node, ast.ImportFrom):
                module_imports.add(node.module)
            elif isinstance(node, ast.Import):
                module_imports.update(alias.name for alias in node.names)

        self.assertEqual(module_imports, {'django.apps'})

    def test_ready_takes_only_self_and_has_no_decorators(self):
        ready = self.ready_node()

        self.assertEqual([arg.arg for arg in ready.args.args], ['self'])
        self.assertEqual(ready.decorator_list, [])
        self.assertEqual(ready.args.defaults, [])
        self.assertEqual(ready.args.kwonlyargs, [])
        self.assertIsNone(ready.args.vararg)
        self.assertIsNone(ready.args.kwarg)

    def test_ready_imports_the_provider_it_installs(self):
        self.assertEqual(
            self.imported_names(self.ready_node()),
            {
                'readmodel_v1',
                'set_snapshot_provider',
                'jsoncontent_v1',
                'JsonSnapshotProvider',
            },
        )

    def test_ready_is_the_only_place_the_provider_is_named(self):
        module_imports = set()
        for node in ast.parse(self.read_source()).body:
            if isinstance(node, ast.ImportFrom):
                module_imports.add(node.module)
            elif isinstance(node, ast.Import):
                module_imports.update(alias.name for alias in node.names)

        for name in ('storage_v1', 'readmodel_v1', 'models', 'SnapshotV1'):
            with self.subTest(name=name):
                self.assertNotIn(name, module_imports)

    def test_ready_installs_one_provider_with_no_condition(self):
        ready = self.ready_node()

        self.assertEqual(
            [node.__class__.__name__ for node in ready.body],
            ['Expr', 'ImportFrom', 'ImportFrom', 'Expr'],
        )

        call = ready.body[-1].value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Name)
        self.assertEqual(call.func.id, 'set_snapshot_provider')
        self.assertEqual(call.keywords, [])
        self.assertEqual(len(call.args), 1)

        provider = call.args[0]
        self.assertIsInstance(provider, ast.Call)
        self.assertIsInstance(provider.func, ast.Name)
        self.assertEqual(provider.func.id, 'JsonSnapshotProvider')
        self.assertEqual(provider.args, [])
        self.assertEqual(provider.keywords, [])

    def test_the_config_names_no_configuration_or_transport_token(self):
        source = self.read_source()

        for token in ('os.', 'environ', 'getenv', 'settings', 'cache', 'flag',
                      'migrat', 'socket', 'http', 'requests', 'open('):
            with self.subTest(token=token):
                self.assertNotIn(token, source.lower())

        self.assertIn(
            'set_snapshot_provider(JsonSnapshotProvider())', source)


class ReadyInstallationTests(TestCase):
    """What the install leaves in the seam, and what it costs to put it there."""

    def setUp(self):
        super().setUp()
        set_snapshot_provider(None)
        self.addCleanup(set_snapshot_provider, None)
        self.config = apps.get_app_config(APP_LABEL)

    def test_ready_installs_the_published_content_reader(self):
        self.assertNotIsInstance(
            get_snapshot_provider(), JsonSnapshotProvider)

        self.config.ready()

        self.assertIsInstance(
            get_snapshot_provider(), JsonSnapshotProvider)

    def test_the_installed_reader_is_rooted_at_the_shipped_content_directory(self):
        self.config.ready()

        self.assertEqual(
            get_snapshot_provider().root, CANONICAL_CONTENT_ROOT)

    def test_the_installed_reader_is_not_the_durable_store(self):
        self.config.ready()

        self.assertNotIsInstance(
            get_snapshot_provider(), DatabaseSnapshotProvider)

    def test_ready_replaces_the_default_provider_rather_than_keeping_it(self):
        default = get_snapshot_provider()

        self.config.ready()

        self.assertIsNot(get_snapshot_provider(), default)

    def test_ready_is_idempotent(self):
        self.config.ready()
        first = get_snapshot_provider()
        self.config.ready()
        second = get_snapshot_provider()

        self.assertIsInstance(second, JsonSnapshotProvider)
        self.assertIsNot(first, second)
        self.assertIs(get_snapshot_provider(), second)

    def test_ready_runs_no_database_query(self):
        with self.assertNumQueries(0):
            self.config.ready()

        with self.assertNumQueries(0):
            self.config.ready()

    def test_ready_needs_no_row_to_exist(self):
        self.config.ready()

        self.assertFalse(SnapshotV1.objects.exists())
        with self.assertNoLogs(LOGGER_NAME, level='ERROR'):
            self.assertIsNone(load_snapshot(TIP_TYPE))

    def test_the_seam_reads_published_content_and_refuses_a_write(self):
        self.config.ready()

        # Nothing is published under the shipped manifest, so the seam answers
        # "no snapshot" for every key, and the empty deployment is not a failure.
        with self.assertNoLogs(jsoncontent_v1.LOGGER_NAME, level='ERROR'):
            self.assertIsNone(load_snapshot(TIP_TYPE))
            self.assertIsNone(load_snapshot(OTHER_TYPE_KEY))

        # The installed reader has nothing to write to, so the seam refuses the
        # write instead of letting a caller believe an artifact was published.
        with self.assertRaises(ReadOnlyContentError):
            store_snapshot(TIP_TYPE, PAYLOAD, fetched_at=FETCHED_AT)
        self.assertFalse(SnapshotV1.objects.exists())

    def test_a_row_the_durable_store_holds_is_not_what_this_reader_publishes(self):
        record = DatabaseSnapshotProvider().store(
            TIP_TYPE, PAYLOAD, fetched_at=FETCHED_AT)

        self.config.ready()

        # The durable store's own record is intact, and it is simply not what a
        # reader installed by startup answers from.
        self.assertEqual(set(record), set(SNAPSHOT_KEYS))
        self.assertEqual(record['schema_version'], SNAPSHOT_SCHEMA_VERSION)
        row = SnapshotV1.objects.get(pk=TIP_TYPE)
        self.assertEqual(row.payload_sha256, PAYLOAD_SHA256)
        with self.assertNoLogs(jsoncontent_v1.LOGGER_NAME, level='ERROR'):
            self.assertIsNone(load_snapshot(TIP_TYPE))

    def test_a_later_install_changes_nothing_the_reader_holds(self):
        self.config.ready()

        self.config.ready()
        self.config.ready()

        # Every install leaves the same kind of reader behind, and the one thing
        # it reads is still the content the package ships.
        self.assertIsInstance(
            get_snapshot_provider(), JsonSnapshotProvider)
        with self.assertNoLogs(jsoncontent_v1.LOGGER_NAME, level='ERROR'):
            self.assertIsNone(load_snapshot(TIP_TYPE))
        self.assertFalse(SnapshotV1.objects.exists())

    def test_every_install_hands_the_seam_a_provider_it_accepts(self):
        for _ in range(3):
            with self.subTest():
                self.config.ready()
                provider = get_snapshot_provider()

                self.assertEqual(
                    sorted(name for name in ('load', 'store', 'clear')
                           if not callable(getattr(provider, name, None))),
                    [],
                )


class DurableEndpointTestCase(OfflineGuardMixin, TestCase):
    """The endpoint answering from a durable row, socket layer disabled.

    The provider here is the durable store, installed explicitly rather than by
    startup, because a stored row is what the out-of-band writer publishes into
    and this module keeps proving the endpoint still answers it. The reader a
    deployment actually installs is pinned in
    ``tests_publishedcontent_reader_v1.py``.
    """

    def setUp(self):
        super().setUp()
        self.provider = DatabaseSnapshotProvider()
        set_snapshot_provider(self.provider)
        self.addCleanup(set_snapshot_provider, None)

    def seed(self, payload=None, *, type_key=TIP_TYPE, fetched_at=FETCHED_AT):
        """File the fixed payload for a tip type and return its record."""
        return self.provider.store(
            type_key,
            PAYLOAD if payload is None else payload,
            fetched_at=fetched_at,
        )

    def stored_row(self, type_key=TIP_TYPE):
        return SnapshotV1.objects.get(pk=type_key)

    def row_values(self, type_key=TIP_TYPE):
        """Return every stored column of one row, as one comparable tuple."""
        row = self.stored_row(type_key)
        return (
            row.type_key,
            row.schema_version,
            row.payload,
            row.payload_sha256,
            row.fetched_at,
        )

    def get(self, **params):
        return self.client.get(ENDPOINT_PATH, params)


class DurableEndpointWiringTests(DurableEndpointTestCase):
    """A request reads one durable row, and it never writes."""

    def test_the_route_still_resolves_to_the_versioned_view(self):
        match = resolve(ENDPOINT_PATH)

        self.assertEqual(match.view_name, ENDPOINT_ROUTE_NAME)
        self.assertEqual(match.func.__module__, 'alltips_scraper.views_v1')

    def test_the_installed_provider_is_the_one_that_answers(self):
        self.seed()

        self.assertIs(get_snapshot_provider(), self.provider)
        self.assertEqual(self.get(type=TIP_TYPE).status_code, 200)
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(self.stored_row().payload, PAYLOAD)

    def test_a_request_neither_stores_nor_clears_a_row(self):
        self.seed()
        before = self.row_values()

        def forbidden(*args, **kwargs):
            raise AssertionError('an endpoint read must not write')

        params = (
            {'type': TIP_TYPE},
            {'type': TIP_TYPE, 'date': SOURCE_DATE_TEXT, 'timezone': 'Etc/UTC'},
            {'type': TIP_TYPE, 'date': '1999-01-01', 'timezone': 'Etc/UTC'},
        )
        with mock.patch.object(self.provider, 'store', forbidden), \
                mock.patch.object(self.provider, 'clear', forbidden):
            for query in params:
                with self.subTest(query=query):
                    self.assertEqual(self.get(**query).status_code, 200)

        self.assertEqual(self.row_values(), before)
        self.assertEqual(SnapshotV1.objects.count(), 1)

    def test_two_requests_answer_the_same_body_and_keep_the_stamp(self):
        self.seed()
        before = self.row_values()

        first = self.get(type=TIP_TYPE)
        second = self.get(type=TIP_TYPE)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertEqual(self.row_values(), before)


class DurableEndpointUnavailableTests(DurableEndpointTestCase):
    """With no usable row, the endpoint keeps its existing client-safe answer."""

    def test_a_query_with_no_row_is_the_documented_503(self):
        response = self.get(type=TIP_TYPE)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response['Content-Type'], 'application/json')

        body = response.json()
        self.assertEqual(set(body), {'api_version', 'error'})
        self.assertEqual(body['api_version'], API_VERSION)
        self.assertEqual(set(body['error']), {'code', 'message', 'field'})
        self.assertEqual(body['error']['code'], ERROR_SOURCE_UNAVAILABLE)
        self.assertEqual(
            body['error']['message'], ERROR_MESSAGES[ERROR_SOURCE_UNAVAILABLE])
        self.assertIsNone(body['error']['field'])
        self.assertFalse(SnapshotV1.objects.exists())

    def test_the_unavailable_body_carries_no_store_detail(self):
        raw = self.get(type=TIP_TYPE).content.decode()

        for token in LEAK_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, raw)

    def test_the_same_query_becomes_a_200_once_a_row_exists(self):
        query = {'type': TIP_TYPE, 'date': SOURCE_DATE_TEXT,
                 'timezone': 'Etc/UTC'}

        self.assertEqual(self.get(**query).status_code, 503)
        self.seed()
        self.assertEqual(self.get(**query).status_code, 200)

    def test_an_unusable_row_is_a_503_and_not_a_server_error(self):
        self.seed()
        SnapshotV1.objects.filter(pk=TIP_TYPE).update(
            payload_sha256='f' * 64)

        with self.assertLogs(LOGGER_NAME, level='ERROR') as logs:
            response = self.get(type=TIP_TYPE)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()['error']['code'], ERROR_SOURCE_UNAVAILABLE)
        self.assertEqual(len(logs.records), 1)
        self.assertIn(f'reason={REASON_DIGEST}', logs.records[0].getMessage())
        self.assertIn(TIP_TYPE, logs.records[0].getMessage())

    def test_a_refused_row_is_not_repaired_by_a_read(self):
        self.seed()
        SnapshotV1.objects.filter(pk=TIP_TYPE).update(
            payload_sha256='f' * 64)

        self.assertEqual(self.get(type=TIP_TYPE).status_code, 503)
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(self.stored_row().payload_sha256, 'f' * 64)
        self.assertEqual(self.stored_row().payload, PAYLOAD)


class DurableEndpointEnvelopeTests(DurableEndpointTestCase):
    """A durable row is published as the documented envelope, filters included."""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_the_endpoint_publishes_the_documented_envelope(self):
        body = self.get(type=TIP_TYPE).json()

        self.assertEqual(
            set(body), set(SUCCESS_ENVELOPE_KEYS) | set(OPTIONAL_ENVELOPE_KEYS))
        self.assertEqual(body['api_version'], API_VERSION)
        self.assertEqual(body['type'], TIP_TYPE)
        self.assertEqual(body['unit'], UNIT_MATCH)
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['legs_count'], 0)
        self.assertEqual(len(body['tips']), 1)
        self.assertEqual(body['source']['label'], 'freesupertips')
        self.assertEqual(body['source']['date_text'], SOURCE_DATE_TEXT)
        self.assertEqual(body['source']['fetched_at'], FETCHED_AT_Z)

    def test_the_stored_payload_is_mapped_tip_by_tip(self):
        body = self.get(type=TIP_TYPE).json()
        tip = body['tips'][0]

        self.assertEqual(set(tip), set(MATCH_UNIT_TIP_KEYS))
        self.assertEqual(tip['match_title'], 'Arsenal vs Chelsea')
        self.assertEqual(tip['prediction'], 'Arsenal to win')
        self.assertIsNone(tip[KICKOFF_AT])
        self.assertIs(tip[KICKOFF_TIME_VERIFIED], False)

    def test_the_unfiltered_envelope_carries_the_filter_block(self):
        body = self.get(type=TIP_TYPE).json()

        self.assertEqual(set(body['filter']), set(FILTER_KEYS))
        self.assertIsNone(body['filter']['date'])
        self.assertIsNone(body['filter']['timezone'])
        self.assertFalse(body['filter']['applied'])
        self.assertIsNone(body['filter']['matched'])
        self.assertEqual(body['filter']['available_date'], SOURCE_DATE_TEXT)

    def test_a_matching_date_filter_is_applied_and_matched(self):
        body = self.get(
            type=TIP_TYPE, date=SOURCE_DATE_TEXT, timezone='Etc/UTC').json()

        self.assertEqual(body['count'], 1)
        self.assertEqual(
            body['filter'],
            {
                'date': SOURCE_DATE_TEXT,
                'timezone': 'Etc/UTC',
                'applied': True,
                'matched': True,
                'available_date': SOURCE_DATE_TEXT,
            },
        )

    def test_a_date_filter_that_does_not_match_is_an_empty_200(self):
        body = self.get(
            type=TIP_TYPE, date='1999-01-01', timezone='Etc/UTC').json()

        self.assertEqual(body['count'], 0)
        self.assertEqual(body['legs_count'], 0)
        self.assertEqual(body['tips'], [])
        self.assertEqual(body['filter']['date'], '1999-01-01')
        self.assertEqual(body['filter']['applied'], True)
        self.assertEqual(body['filter']['matched'], False)
        self.assertEqual(body['filter']['available_date'], SOURCE_DATE_TEXT)
        self.assertEqual(body['source']['label'], 'freesupertips')
        self.assertEqual(body['source']['date_text'], SOURCE_DATE_TEXT)
        self.assertEqual(body['source']['fetched_at'], FETCHED_AT_Z)

    def test_a_rewritten_row_is_what_the_next_request_answers_from(self):
        self.seed(CHANGED_PAYLOAD)

        body = self.get(type=TIP_TYPE).json()
        row = self.stored_row()

        self.assertEqual(body['count'], 2)
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(row.payload, CHANGED_PAYLOAD)
        self.assertEqual(
            row.payload_sha256, canonical_payload_sha256(CHANGED_PAYLOAD))

    def test_a_second_key_is_answered_from_its_own_row_and_unit(self):
        self.seed(OTHER_PAYLOAD, type_key=OTHER_TYPE_KEY)

        body = self.get(type=TIP_TYPE).json()
        other = self.get(type=OTHER_TYPE_KEY).json()

        self.assertEqual(SnapshotV1.objects.count(), 2)
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['unit'], UNIT_MATCH)
        self.assertEqual(other['count'], 1)
        self.assertEqual(other['unit'], UNIT_CARD)
        self.assertEqual(other['legs_count'], 1)
        self.assertEqual(
            other['tips'][0]['category'], 'Daily Accumulator')
        self.assertEqual(len(other['tips'][0]['legs']), 1)