"""Storage-layer tests for the durable v1 snapshot table.

This module pins the durable store only: the table identity, the five columns,
the primary key, the read order, the schema-version constraint, the row round
trip, and the shape of the first migration. Every value below is an explicit
literal - no test reads a clock, the network, or a fixture, and no digest is
ever compared against a value the code calculated.

Commit 2 adds the provider that writes and reads exactly those rows, so this
module pins it beside the table: the canonical byte form and the one digest it
produces, what a store persists, what a read refuses, and how a refusal is
reported. The Commit 1 tests above are unchanged, and the provider's own section
at the foot of the module is where its single clock read is pinned - through a
patched Django timezone helper, never the machine's clock.
"""

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from unittest import mock

from django.db import DatabaseError, IntegrityError, models, transaction
from django.db.migrations import CreateModel
from django.db.migrations.state import ProjectState
from django.db.models import CheckConstraint, NOT_PROVIDED, Q
from django.test import SimpleTestCase, TestCase

from . import storage_v1
from .models import SnapshotV1
from .readmodel_v1 import (
    SNAPSHOT_KEYS,
    SNAPSHOT_SCHEMA_VERSION,
    get_snapshot_provider,
    set_snapshot_provider,
)
from .storage_v1 import (
    CANONICAL_JSON_SEPARATORS,
    CANONICAL_TEXT_ENCODING,
    DIGEST_ALGORITHM,
    DatabaseSnapshotProvider,
    canonical_payload_bytes,
    canonical_payload_sha256,
)

# ---------------------------------------------------------------------------
# Fixed locations: the app, the model, and the one migration this commit ships.
# ---------------------------------------------------------------------------

APP_LABEL = 'alltips_scraper'
MODEL_NAME = 'SnapshotV1'
MODEL_STATE_KEY = (APP_LABEL, 'snapshotv1')
MIGRATION_NAME = '0001_initial'
MIGRATION_MODULE = 'alltips_scraper.migrations.0001_initial'

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
INITIAL_MIGRATION_PATH = MIGRATIONS_DIR / f'{MIGRATION_NAME}.py'

# ---------------------------------------------------------------------------
# Fixed shape: the five durable columns, and the columns that must never appear.
# ---------------------------------------------------------------------------

TABLE_NAME = 'alltips_scraper_snapshot_v1'
CONSTRAINT_NAME = 'snapshot_v1_schema_version_gte_1'
TYPE_KEY_MAX_LENGTH = 64
DIGEST_MAX_LENGTH = 64

EXPECTED_FIELDS = {
    'type_key': models.CharField,
    'schema_version': models.PositiveSmallIntegerField,
    'payload': models.JSONField,
    'payload_sha256': models.CharField,
    'fetched_at': models.DateTimeField,
}
EXPECTED_FIELD_NAMES = frozenset(EXPECTED_FIELDS)

# Columns a durable row must not grow: no surrogate key, no audit clock, no
# duplicated payload field, and no derived count that could disagree with the
# payload it was derived from.
FORBIDDEN_FIELD_NAMES = frozenset({
    'id',
    'created_at',
    'updated_at',
    'stored_at',
    'first_seen_at',
    'source_url',
    'source_date_text',
    'payload_json',
    'tip_count',
    'legs_count',
    'fetched_date',
})

# Operations this first migration must not introduce: a data migration, a raw
# SQL statement, a second or renamed model, an index, or a mutation of a table.
FORBIDDEN_OPERATION_NAMES = (
    'RunPython',
    'RunSQL',
    'SeparateDatabaseAndState',
    'AddField',
    'AlterField',
    'RemoveField',
    'DeleteModel',
    'RenameModel',
    'AddIndex',
    'RemoveIndex',
    'AddConstraint',
    'RemoveConstraint',
)

# ---------------------------------------------------------------------------
# Fixed inputs. FETCHED_AT is an explicit aware-UTC instant, and the digest is
# supplied as a literal: the canonical digest rule is not part of this commit,
# so nothing here may assert a value the code calculated.
# ---------------------------------------------------------------------------

FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3, tzinfo=timezone.utc)
SUPPLIED_SHA256 = '0' * DIGEST_MAX_LENGTH
SCHEMA_VERSION = 1
TYPE_KEY = 'bet_of_the_day'

PAYLOAD = {
    'date': '2026-09-27',
    'total_tips': 1,
    'matches': [
        {'match_title': 'Arsenal vs Chelsea', 'prediction': 'Arsenal to win'},
    ],
    'count': 1,
    'source': 'freesupertips',
}

SNAPSHOT_ROW = {
    'type_key': TYPE_KEY,
    'schema_version': SCHEMA_VERSION,
    'payload': PAYLOAD,
    'payload_sha256': SUPPLIED_SHA256,
    'fetched_at': FETCHED_AT,
}


class SnapshotV1ColumnTests(SimpleTestCase):
    """The table is exactly the five durable columns, and nothing more."""

    def test_the_model_is_the_versioned_snapshot_model(self):
        self.assertEqual(SnapshotV1.__name__, MODEL_NAME)
        self.assertEqual(SnapshotV1.__module__, 'alltips_scraper.models')
        self.assertEqual(SnapshotV1._meta.app_label, APP_LABEL)
        self.assertEqual(SnapshotV1._meta.model_name, 'snapshotv1')

    def test_the_column_names_are_exactly_the_five_durable_columns(self):
        names = [field.name for field in SnapshotV1._meta.local_fields]

        self.assertEqual(len(names), len(EXPECTED_FIELD_NAMES))
        self.assertEqual(set(names), set(EXPECTED_FIELD_NAMES))
        self.assertEqual(SnapshotV1._meta.local_many_to_many, [])

    def test_each_column_has_the_expected_field_type(self):
        for name, expected_type in EXPECTED_FIELDS.items():
            with self.subTest(field=name):
                self.assertIsInstance(SnapshotV1._meta.get_field(name), expected_type)

    def test_type_key_is_the_primary_key_and_is_never_generated(self):
        field = SnapshotV1._meta.get_field('type_key')

        self.assertTrue(field.primary_key)
        self.assertEqual(SnapshotV1._meta.pk.name, 'type_key')
        self.assertEqual(field.max_length, TYPE_KEY_MAX_LENGTH)
        self.assertFalse(field.auto_created)
        self.assertIsNone(SnapshotV1._meta.auto_field)

    def test_payload_sha256_holds_a_sixty_four_character_digest(self):
        field = SnapshotV1._meta.get_field('payload_sha256')

        self.assertEqual(field.max_length, DIGEST_MAX_LENGTH)
        self.assertFalse(field.primary_key)
        self.assertFalse(field.null)

    def test_the_surrogate_and_derived_columns_are_absent(self):
        names = {field.name for field in SnapshotV1._meta.get_fields()}

        self.assertEqual(names & FORBIDDEN_FIELD_NAMES, set())
        self.assertEqual(SnapshotV1._meta.indexes, [])
        self.assertEqual(list(SnapshotV1._meta.unique_together), [])

    def test_no_column_defaults_to_a_clock_or_allows_a_null(self):
        for field in SnapshotV1._meta.local_fields:
            with self.subTest(field=field.name):
                self.assertIs(field.default, NOT_PROVIDED)
                self.assertFalse(field.null)
                self.assertFalse(getattr(field, 'auto_now', False))
                self.assertFalse(getattr(field, 'auto_now_add', False))


class SnapshotV1MetaTests(SimpleTestCase):
    """The table identity, the read order, and the one database rule."""

    def test_the_table_name_is_explicit_and_versioned(self):
        self.assertEqual(SnapshotV1._meta.db_table, TABLE_NAME)
        self.assertIn('snapshot_v1', SnapshotV1._meta.db_table)

    def test_the_default_ordering_is_the_single_primary_key(self):
        self.assertEqual(tuple(SnapshotV1._meta.ordering), ('type_key',))
        self.assertEqual(len(SnapshotV1._meta.ordering), 1)

    def test_there_is_exactly_one_constraint(self):
        self.assertEqual(len(SnapshotV1._meta.constraints), 1)
        self.assertIsInstance(SnapshotV1._meta.constraints[0], CheckConstraint)

    def test_the_constraint_requires_a_schema_version_of_at_least_one(self):
        constraint = SnapshotV1._meta.constraints[0]

        self.assertEqual(constraint.name, CONSTRAINT_NAME)
        self.assertEqual(constraint.condition, Q(schema_version__gte=SCHEMA_VERSION))
        self.assertEqual(
            list(constraint.condition.children),
            [('schema_version__gte', SCHEMA_VERSION)],
        )
        self.assertEqual(constraint.condition.connector, Q.AND)
        self.assertFalse(constraint.condition.negated)

    def test_the_constraint_name_is_scoped_to_this_versioned_table(self):
        constraint = SnapshotV1._meta.constraints[0]

        self.assertIn('snapshot_v1', constraint.name)
        self.assertIn('schema_version', constraint.name)


class SnapshotV1InitialMigrationTests(SimpleTestCase):
    """The first migration is one reversible CreateModel for this table only."""

    def setUp(self):
        self.source = INITIAL_MIGRATION_PATH.read_text(encoding='utf-8')
        self.migration = import_module(MIGRATION_MODULE)
        self.migration_class = self.migration.Migration

    def test_the_migrations_package_has_a_package_marker(self):
        self.assertTrue((MIGRATIONS_DIR / '__init__.py').is_file())

    def test_the_migration_is_the_apps_first_and_depends_on_nothing(self):
        self.assertIs(self.migration_class.initial, True)
        self.assertEqual(MIGRATION_MODULE.rsplit('.', 1)[-1], MIGRATION_NAME)
        self.assertEqual(list(self.migration_class.dependencies), [])
        self.assertEqual(list(self.migration_class.replaces), [])

    def test_the_migration_contains_exactly_one_create_model_operation(self):
        operations = list(self.migration_class.operations)

        self.assertEqual(len(operations), 1)
        self.assertIsInstance(operations[0], CreateModel)
        self.assertEqual(operations[0].name, MODEL_NAME)

    def test_the_migration_introduces_no_data_or_raw_sql_operation(self):
        for token in FORBIDDEN_OPERATION_NAMES:
            with self.subTest(token=token):
                self.assertNotIn(token, self.source)

    def test_every_operation_is_reversible(self):
        for operation in self.migration_class.operations:
            with self.subTest(operation=type(operation).__name__):
                self.assertTrue(operation.reversible)

    def test_the_created_columns_match_the_model_columns(self):
        fields = dict(self.migration_class.operations[0].fields)

        self.assertEqual(set(fields), set(EXPECTED_FIELD_NAMES))
        for name in EXPECTED_FIELD_NAMES:
            with self.subTest(field=name):
                self.assertEqual(
                    fields[name].deconstruct()[1:],
                    SnapshotV1._meta.get_field(name).deconstruct()[1:],
                )

    def test_the_operation_options_match_the_model_meta(self):
        options = self.migration_class.operations[0].options

        self.assertEqual(set(options), {'db_table', 'ordering', 'constraints'})
        self.assertEqual(options['db_table'], TABLE_NAME)
        self.assertEqual(options['ordering'], ('type_key',))

        self.assertEqual(len(options['constraints']), 1)
        constraint = options['constraints'][0]
        self.assertIsInstance(constraint, CheckConstraint)
        self.assertEqual(constraint.name, CONSTRAINT_NAME)
        self.assertEqual(constraint.condition, Q(schema_version__gte=SCHEMA_VERSION))

    def test_the_operation_builds_the_expected_project_state(self):
        state = ProjectState()
        for operation in self.migration_class.operations:
            operation.state_forwards(APP_LABEL, state)

        model_state = state.models[MODEL_STATE_KEY]
        self.assertEqual(sorted(model_state.fields), sorted(EXPECTED_FIELD_NAMES))
        self.assertEqual(model_state.options['db_table'], TABLE_NAME)
        self.assertEqual(model_state.options['ordering'], ('type_key',))


class SnapshotV1RowTests(TestCase):
    """A row keeps exactly the values the caller supplied, and nothing else."""

    def test_a_row_is_created_and_read_back_with_the_supplied_values(self):
        created = SnapshotV1.objects.create(**SNAPSHOT_ROW)
        stored = SnapshotV1.objects.get(pk=TYPE_KEY)

        for label, instance in (('created', created), ('stored', stored)):
            with self.subTest(instance=label):
                self.assertEqual(instance.pk, TYPE_KEY)
                self.assertEqual(instance.type_key, TYPE_KEY)
                self.assertEqual(instance.schema_version, SCHEMA_VERSION)
                self.assertEqual(instance.payload, PAYLOAD)
                self.assertEqual(instance.payload_sha256, SUPPLIED_SHA256)
                self.assertEqual(instance.fetched_at, FETCHED_AT)

    def test_the_stored_row_keeps_the_supplied_instant_in_utc(self):
        SnapshotV1.objects.create(**SNAPSHOT_ROW)
        stored = SnapshotV1.objects.get()

        self.assertIsNotNone(stored.fetched_at.tzinfo)
        self.assertEqual(stored.fetched_at.utcoffset(), timedelta(0))
        self.assertEqual(len(stored.payload_sha256), DIGEST_MAX_LENGTH)

    def test_the_stored_payload_keeps_its_nested_shape(self):
        payload = {
            'date': '2026-09-27',
            'count': 2,
            'matches': [
                {'match_title': 'Bayern vs Dortmund', 'odds': 1.5, 'ok': True},
            ],
            'sources': ['freesupertips', 'other'],
        }
        SnapshotV1.objects.create(
            **dict(SNAPSHOT_ROW, type_key='both_teams_to_score', payload=payload)
        )

        stored = SnapshotV1.objects.get(pk='both_teams_to_score')

        self.assertEqual(stored.payload, payload)
        self.assertEqual(list(stored.payload), list(payload))

    def test_the_default_ordering_is_applied_to_queries(self):
        SnapshotV1.objects.create(**dict(SNAPSHOT_ROW, type_key='zeta_type'))
        SnapshotV1.objects.create(**dict(SNAPSHOT_ROW, type_key='alpha_type'))

        self.assertEqual(
            list(SnapshotV1.objects.values_list('type_key', flat=True)),
            ['alpha_type', 'zeta_type'],
        )

    def test_a_type_key_can_only_hold_one_row(self):
        SnapshotV1.objects.create(**SNAPSHOT_ROW)

        with self.assertRaises(IntegrityError), transaction.atomic():
            SnapshotV1.objects.create(**SNAPSHOT_ROW)

        self.assertEqual(SnapshotV1.objects.count(), 1)

    def test_the_database_refuses_a_schema_version_below_one(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SnapshotV1.objects.create(
                **dict(SNAPSHOT_ROW, type_key='unknown_shape', schema_version=0)
            )

        self.assertFalse(SnapshotV1.objects.exists())



# ---------------------------------------------------------------------------
# Commit 2: the provider that writes and reads those rows.
#
# The section below is additive. It pins the canonical digest (against fixed
# literals, never a value asserted against itself), what a store persists, what
# a read refuses, and how a refusal is reported. A stored row that a real column
# cannot hold - a naive, missing or non-datetime instant, a non-string digest -
# is injected as the row a reader finds, because a database round trip would
# repair exactly the state under test.
# ---------------------------------------------------------------------------

# The canonical digest of PAYLOAD, of PAYLOAD with ``total_tips`` raised to 2,
# and of the non-ASCII payload below. All three are literals.
PAYLOAD_SHA256 = '36cfd8636014fb7edc478b3337e863a232957aecd19d24203abfed0044c42659'
CHANGED_SHA256 = '5c57c5b9669e2f52b6273db9c01a5201b145aa209622644b91b3cfcd2b29ce58'
NON_ASCII_SHA256 = '5346448bee632285a1e9f95a3c27e69c41a23dd78e1e27912cbcf2ea5c5983e2'

# The canonical byte form of PAYLOAD, and of a payload whose text is not ASCII.
# The second one spells its non-ASCII letters as JSON escapes, which is what
# ``ensure_ascii`` means.
CANONICAL_BYTES = (
    b'{"count":1,"date":"2026-09-27","matches":['
    b'{"match_title":"Arsenal vs Chelsea","prediction":"Arsenal to win"}],'
    b'"source":"freesupertips","total_tips":1}'
)
NON_ASCII_CANONICAL_BYTES = (
    b'{"count":1,"date":"2026-09-27","matches":['
    b'{"match_title":"Bayern M\\u00fcnchen vs Dortmund",'
    b'"prediction":"M\\u00fcnchen to win"}],'
    b'"source":"freesupertips","total_tips":1}'
)

# PAYLOAD again with one value changed, so its digest must differ - and with the
# keys inserted in a different order, so its digest must not.
CHANGED_PAYLOAD = dict(PAYLOAD, total_tips=2)
REORDERED_PAYLOAD = {
    'source': 'freesupertips',
    'count': 1,
    'matches': [
        {'prediction': 'Arsenal to win', 'match_title': 'Arsenal vs Chelsea'},
    ],
    'total_tips': 1,
    'date': '2026-09-27',
}
NON_ASCII_PAYLOAD = {
    'date': '2026-09-27',
    'total_tips': 1,
    'matches': [
        {
            'match_title': 'Bayern M\u00fcnchen vs Dortmund',
            'prediction': 'M\u00fcnchen to win',
        },
    ],
    'count': 1,
    'source': 'freesupertips',
}

OTHER_TYPE_KEY = 'daily_accumulator'
NAIVE_FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3)
WIRE_FETCHED_AT = '2026-09-28T17:12:03Z'


class StubRow:
    """A stored row as a reader finds it, without a database round trip."""

    def __init__(self, **values):
        self.type_key = values.get('type_key', TYPE_KEY)
        self.schema_version = values.get('schema_version', SCHEMA_VERSION)
        self.payload = values.get('payload', PAYLOAD)
        self.payload_sha256 = values.get('payload_sha256', PAYLOAD_SHA256)
        self.fetched_at = values.get('fetched_at', FETCHED_AT)


class StubSnapshotManager:
    """A manager stand-in holding one row, or one failure, and no database."""

    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.filters = []

    def filter(self, **kwargs):
        self.filters.append(kwargs)
        return self

    def first(self):
        if self.error is not None:
            raise self.error
        return self.row


class StubSnapshotModel:
    """A ``SnapshotV1`` stand-in, so a provider read can be handed any row."""

    def __init__(self, row=None, error=None):
        self.objects = StubSnapshotManager(row=row, error=error)


class TransactionCountingModule:
    """A stand-in for the ``transaction`` module that counts its own calls.

    Patching the provider's own module reference is what makes the count the
    provider's own: a transaction the ORM opens internally is counted nowhere,
    so the number is the number of blocks this provider enters.
    """

    def __init__(self, real_atomic):
        self._real_atomic = real_atomic
        self.calls = 0

    def atomic(self, *args, **kwargs):
        self.calls += 1
        return self._real_atomic(*args, **kwargs)


class CanonicalPayloadDigestTests(SimpleTestCase):
    """One canonical byte form, one digest rule, and two fixed literals."""

    def test_the_canonical_form_is_sorted_compact_and_ascii(self):
        self.assertEqual(canonical_payload_bytes(PAYLOAD), CANONICAL_BYTES)
        self.assertEqual(CANONICAL_JSON_SEPARATORS, (',', ':'))
        self.assertEqual(CANONICAL_TEXT_ENCODING, 'utf-8')
        self.assertNotIn(b'", "', CANONICAL_BYTES)
        self.assertNotIn(b': ', CANONICAL_BYTES)
        self.assertNotIn(b'\n', CANONICAL_BYTES)
        self.assertNotIn(b'\t', CANONICAL_BYTES)

    def test_the_digest_of_the_fixed_payload_is_a_fixed_literal(self):
        self.assertEqual(canonical_payload_sha256(PAYLOAD), PAYLOAD_SHA256)
        self.assertEqual(len(PAYLOAD_SHA256), DIGEST_MAX_LENGTH)

    def test_the_digest_is_lowercase_hex_of_the_stated_algorithm(self):
        digest = canonical_payload_sha256(PAYLOAD)

        self.assertEqual(DIGEST_ALGORITHM, 'sha256')
        self.assertEqual(digest, PAYLOAD_SHA256)
        self.assertEqual(digest, digest.lower())
        self.assertEqual(set(digest) - set('0123456789abcdef'), set())

    def test_the_digest_follows_the_documented_rule(self):
        blob = json.dumps(
            PAYLOAD,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
        ).encode('utf-8')

        self.assertEqual(canonical_payload_bytes(PAYLOAD), blob)
        self.assertEqual(
            canonical_payload_sha256(PAYLOAD), hashlib.sha256(blob).hexdigest())

    def test_the_digest_is_stable_across_repeated_calls(self):
        digests = {canonical_payload_sha256(deepcopy(PAYLOAD)) for _ in range(4)}

        self.assertEqual(digests, {PAYLOAD_SHA256})

    def test_a_different_insertion_order_yields_the_same_digest(self):
        self.assertNotEqual(list(PAYLOAD), list(REORDERED_PAYLOAD))
        self.assertEqual(
            canonical_payload_bytes(REORDERED_PAYLOAD), CANONICAL_BYTES)
        self.assertEqual(
            canonical_payload_sha256(REORDERED_PAYLOAD), PAYLOAD_SHA256)

        naive = json.dumps(REORDERED_PAYLOAD).encode('utf-8')
        self.assertNotEqual(naive, CANONICAL_BYTES)
        self.assertNotEqual(hashlib.sha256(naive).hexdigest(), PAYLOAD_SHA256)

    def test_a_changed_payload_changes_the_digest(self):
        changes = {
            'value': CHANGED_PAYLOAD,
            'nested value': dict(
                PAYLOAD,
                matches=[{
                    'match_title': 'Arsenal vs Chelsea',
                    'prediction': 'Chelsea to win',
                }],
            ),
            'added key': dict(PAYLOAD, source_date_text='2026-09-27'),
            'removed key': {
                key: value for key, value in PAYLOAD.items() if key != 'count'
            },
            'emptied list': dict(PAYLOAD, matches=[]),
        }

        for label, changed in changes.items():
            with self.subTest(change=label):
                self.assertNotEqual(
                    canonical_payload_sha256(changed), PAYLOAD_SHA256)

        self.assertEqual(
            canonical_payload_sha256(CHANGED_PAYLOAD), CHANGED_SHA256)

    def test_a_non_ascii_payload_digests_to_its_fixed_literal(self):
        self.assertEqual(
            canonical_payload_bytes(NON_ASCII_PAYLOAD),
            NON_ASCII_CANONICAL_BYTES,
        )
        self.assertEqual(
            canonical_payload_sha256(NON_ASCII_PAYLOAD), NON_ASCII_SHA256)
        self.assertNotEqual(NON_ASCII_SHA256, PAYLOAD_SHA256)
        self.assertNotIn(
            'M\u00fcnchen'.encode('utf-8'), NON_ASCII_CANONICAL_BYTES)


class DurableSnapshotWriteTests(TestCase):
    """A store writes one usable row for its key and returns a defensive copy."""

    def setUp(self):
        super().setUp()
        self.provider = DatabaseSnapshotProvider()

    def stored_row(self, type_key=TYPE_KEY):
        return SnapshotV1.objects.get(pk=type_key)

    def test_a_stored_payload_round_trips_through_the_provider(self):
        record = self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        loaded = self.provider.load(TYPE_KEY)

        self.assertEqual(set(record), set(SNAPSHOT_KEYS))
        self.assertEqual(record['schema_version'], SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(record['type_key'], TYPE_KEY)
        self.assertEqual(record['payload'], PAYLOAD)
        self.assertEqual(record['fetched_at'], FETCHED_AT)
        self.assertEqual(loaded, record)
        self.assertIsNot(loaded, record)
        self.assertIsNot(loaded['payload'], record['payload'])

    def test_the_stored_row_carries_the_five_durable_columns(self):
        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        row = self.stored_row()

        self.assertEqual(row.type_key, TYPE_KEY)
        self.assertEqual(row.schema_version, SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(row.payload, PAYLOAD)
        self.assertEqual(row.payload_sha256, PAYLOAD_SHA256)
        self.assertEqual(row.fetched_at, FETCHED_AT)
        self.assertEqual(row.fetched_at.utcoffset(), timedelta(0))
        self.assertEqual(SnapshotV1.objects.count(), 1)

    def test_the_stored_digest_is_over_the_payload_that_is_stored(self):
        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        row = self.stored_row()

        self.assertEqual(row.payload_sha256, PAYLOAD_SHA256)
        self.assertEqual(
            canonical_payload_sha256(row.payload), row.payload_sha256)

    def test_a_later_store_replaces_that_one_row(self):
        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        later = FETCHED_AT + timedelta(minutes=5)
        self.provider.store(TYPE_KEY, CHANGED_PAYLOAD, fetched_at=later)

        row = self.stored_row()
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(row.payload, CHANGED_PAYLOAD)
        self.assertEqual(row.payload_sha256, CHANGED_SHA256)
        self.assertEqual(row.fetched_at, later)
        self.assertEqual(
            self.provider.load(TYPE_KEY)['payload'], CHANGED_PAYLOAD)
        self.assertEqual(
            self.provider.load(TYPE_KEY)['fetched_at'], later)

    def test_replacing_one_key_leaves_the_other_row_alone(self):
        self.provider.store(OTHER_TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        before = self.stored_row(OTHER_TYPE_KEY)

        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        self.provider.store(
            TYPE_KEY, CHANGED_PAYLOAD, fetched_at=FETCHED_AT + timedelta(days=1))

        after = self.stored_row(OTHER_TYPE_KEY)
        self.assertEqual(SnapshotV1.objects.count(), 2)
        self.assertEqual(after.type_key, before.type_key)
        self.assertEqual(after.schema_version, before.schema_version)
        self.assertEqual(after.payload, before.payload)
        self.assertEqual(after.payload_sha256, before.payload_sha256)
        self.assertEqual(after.fetched_at, before.fetched_at)
        self.assertEqual(
            self.provider.load(OTHER_TYPE_KEY)['payload'], PAYLOAD)

    def test_a_mutated_caller_payload_cannot_reach_the_stored_row(self):
        payload = deepcopy(PAYLOAD)
        self.provider.store(TYPE_KEY, payload, fetched_at=FETCHED_AT)

        payload['count'] = 99
        payload['matches'][0]['prediction'] = 'Chelsea to win'
        payload['matches'].append({'match_title': 'Inter vs Milan'})
        payload['added'] = True

        row = self.stored_row()
        self.assertEqual(row.payload, PAYLOAD)
        self.assertEqual(row.payload_sha256, PAYLOAD_SHA256)
        self.assertEqual(self.provider.load(TYPE_KEY)['payload'], PAYLOAD)

    def test_a_mutated_returned_record_cannot_reach_the_stored_row(self):
        record = self.provider.store(
            TYPE_KEY, deepcopy(PAYLOAD), fetched_at=FETCHED_AT)

        self.assertIsNot(record['payload'], PAYLOAD)
        record['payload']['count'] = 99
        record['payload']['matches'][0]['match_title'] = 'Inter vs Milan'
        record['schema_version'] = 99
        record['fetched_at'] = FETCHED_AT + timedelta(days=1)

        row = self.stored_row()
        self.assertEqual(row.payload, PAYLOAD)
        self.assertEqual(row.schema_version, SCHEMA_VERSION)
        self.assertEqual(row.fetched_at, FETCHED_AT)
        self.assertEqual(
            self.provider.load(TYPE_KEY),
            {
                'schema_version': SNAPSHOT_SCHEMA_VERSION,
                'type_key': TYPE_KEY,
                'payload': PAYLOAD,
                'fetched_at': FETCHED_AT,
            },
        )

    def test_a_mutated_loaded_record_cannot_reach_the_stored_row(self):
        self.provider.store(TYPE_KEY, deepcopy(PAYLOAD), fetched_at=FETCHED_AT)

        loaded = self.provider.load(TYPE_KEY)
        loaded['payload']['count'] = 99
        loaded['payload']['matches'].clear()

        self.assertEqual(self.stored_row().payload, PAYLOAD)
        self.assertEqual(self.provider.load(TYPE_KEY)['payload'], PAYLOAD)

    def test_an_omitted_stamp_is_the_clock_in_utc(self):
        with mock.patch.object(
                storage_v1.django_timezone, 'now', return_value=FETCHED_AT):
            record = self.provider.store(TYPE_KEY, PAYLOAD)

        row = self.stored_row()
        self.assertEqual(record['fetched_at'], FETCHED_AT)
        self.assertEqual(row.fetched_at, FETCHED_AT)
        self.assertEqual(row.fetched_at.utcoffset(), timedelta(0))
        self.assertEqual(self.provider.load(TYPE_KEY)['fetched_at'], FETCHED_AT)

    def test_a_non_utc_clock_reading_is_normalised_to_utc(self):
        reading = FETCHED_AT.astimezone(timezone(timedelta(hours=10)))
        with mock.patch.object(
                storage_v1.django_timezone, 'now', return_value=reading):
            record = self.provider.store(TYPE_KEY, PAYLOAD)

        self.assertEqual(record['fetched_at'], FETCHED_AT)
        self.assertEqual(record['fetched_at'].utcoffset(), timedelta(0))
        self.assertEqual(self.stored_row().fetched_at, FETCHED_AT)

    def test_an_aware_stamp_in_another_zone_is_the_same_instant(self):
        supplied = FETCHED_AT.astimezone(timezone(timedelta(hours=-5)))
        record = self.provider.store(
            TYPE_KEY, PAYLOAD, fetched_at=supplied)

        self.assertEqual(record['fetched_at'], FETCHED_AT)
        self.assertEqual(record['fetched_at'].utcoffset(), timedelta(0))
        self.assertEqual(self.stored_row().fetched_at, FETCHED_AT)

    def test_a_naive_stamp_is_refused_and_writes_nothing(self):
        for candidate in (NAIVE_FETCHED_AT, datetime(2026, 9, 28)):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    self.provider.store(
                        TYPE_KEY, PAYLOAD, fetched_at=candidate)

        self.assertFalse(SnapshotV1.objects.exists())
        self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_non_datetime_stamp_is_refused_and_writes_nothing(self):
        candidates = (
            WIRE_FETCHED_AT, '', 0, 1.5, True, [], {},
            datetime(2026, 9, 28).date(),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(TypeError):
                    self.provider.store(
                        TYPE_KEY, PAYLOAD, fetched_at=candidate)

        self.assertFalse(SnapshotV1.objects.exists())

    def test_a_payload_that_is_not_a_dictionary_is_refused(self):
        candidates = ([], (), ['Arsenal vs Chelsea'], 'text', b'{}', 42, 4.5,
                      True, None, set())
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(TypeError):
                    self.provider.store(
                        TYPE_KEY, candidate, fetched_at=FETCHED_AT)

        self.assertFalse(SnapshotV1.objects.exists())
        self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_refused_write_leaves_the_previous_row_untouched(self):
        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)

        with self.assertRaises(TypeError):
            self.provider.store(TYPE_KEY, ['not', 'a', 'mapping'])
        with self.assertRaises(ValueError):
            self.provider.store(
                TYPE_KEY, CHANGED_PAYLOAD, fetched_at=NAIVE_FETCHED_AT)

        row = self.stored_row()
        self.assertEqual(row.payload, PAYLOAD)
        self.assertEqual(row.payload_sha256, PAYLOAD_SHA256)
        self.assertEqual(row.fetched_at, FETCHED_AT)
        self.assertEqual(SnapshotV1.objects.count(), 1)

    def test_two_instances_write_and_read_the_same_row(self):
        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        other = DatabaseSnapshotProvider()

        self.assertEqual(other.load(TYPE_KEY)['payload'], PAYLOAD)
        later = FETCHED_AT + timedelta(hours=1)
        other.store(TYPE_KEY, CHANGED_PAYLOAD, fetched_at=later)

        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(
            self.provider.load(TYPE_KEY)['payload'], CHANGED_PAYLOAD)
        self.assertEqual(self.provider.load(TYPE_KEY)['fetched_at'], later)

    def test_a_store_opens_exactly_one_transaction(self):
        spy = TransactionCountingModule(storage_v1.transaction.atomic)

        with mock.patch.object(storage_v1, 'transaction', spy):
            self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)

        self.assertEqual(spy.calls, 1)
        self.assertEqual(self.stored_row().payload, PAYLOAD)

    def test_a_clear_opens_exactly_one_transaction(self):
        spy = TransactionCountingModule(storage_v1.transaction.atomic)

        with mock.patch.object(storage_v1, 'transaction', spy):
            self.provider.clear()

        self.assertEqual(spy.calls, 1)
        self.assertFalse(SnapshotV1.objects.exists())

    def test_a_failed_write_keeps_the_previous_row(self):
        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)

        failing = mock.MagicMock()
        failing.update_or_create.side_effect = DatabaseError('table is locked')
        with mock.patch.object(
                storage_v1, 'SnapshotV1', mock.MagicMock(objects=failing)):
            with self.assertRaises(DatabaseError):
                self.provider.store(
                    TYPE_KEY,
                    CHANGED_PAYLOAD,
                    fetched_at=FETCHED_AT + timedelta(minutes=5),
                )

        row = self.stored_row()
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(row.payload, PAYLOAD)
        self.assertEqual(row.payload_sha256, PAYLOAD_SHA256)
        self.assertEqual(row.fetched_at, FETCHED_AT)


class DurableSnapshotReadTests(TestCase):
    """A read either returns the row's record or refuses that row, quietly."""

    def setUp(self):
        super().setUp()
        self.provider = DatabaseSnapshotProvider()

    def create_row(self, **overrides):
        """Insert one deliberate row, with the fixed columns filled in."""
        values = dict(SNAPSHOT_ROW, **overrides)
        return SnapshotV1.objects.create(**values)

    def assertRefused(self, reason, type_key=TYPE_KEY):
        """Assert one read refuses the row and reports exactly that reason."""
        with self.assertLogs(storage_v1.LOGGER_NAME, level='ERROR') as logs:
            self.assertIsNone(self.provider.load(type_key))

        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.levelname, 'ERROR')
        self.assertEqual(record.name, storage_v1.LOGGER_NAME)
        self.assertIsNone(record.exc_info)
        message = record.getMessage()
        self.assertIn(type_key, message)
        self.assertIn(f'reason={reason}', message)
        return message

    def test_an_empty_table_loads_as_none_without_reporting_anything(self):
        with self.assertNoLogs(storage_v1.LOGGER_NAME, level='ERROR'):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_row_for_another_key_is_not_a_row_for_this_key(self):
        self.provider.store(OTHER_TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)

        with self.assertNoLogs(storage_v1.LOGGER_NAME, level='ERROR'):
            self.assertIsNone(self.provider.load(TYPE_KEY))

    def test_a_usable_row_loads_without_reporting_anything(self):
        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)

        with self.assertNoLogs(storage_v1.LOGGER_NAME, level='ERROR'):
            record = self.provider.load(TYPE_KEY)

        self.assertEqual(record['payload'], PAYLOAD)
        self.assertEqual(record['type_key'], TYPE_KEY)

    def test_each_read_returns_a_fresh_record(self):
        self.provider.store(TYPE_KEY, deepcopy(PAYLOAD), fetched_at=FETCHED_AT)

        first = self.provider.load(TYPE_KEY)
        second = self.provider.load(TYPE_KEY)

        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first['payload'], second['payload'])

    def test_a_row_with_another_record_version_is_refused(self):
        for version in (SCHEMA_VERSION + 1, SCHEMA_VERSION + 2, 99):
            with self.subTest(schema_version=version):
                SnapshotV1.objects.all().delete()
                self.create_row(schema_version=version)

                self.assertRefused(storage_v1.REASON_SCHEMA_VERSION)

    def test_a_row_with_a_blank_digest_is_refused(self):
        self.create_row(payload_sha256=SUPPLIED_SHA256)

        self.assertRefused(storage_v1.REASON_DIGEST)

    def test_a_row_with_a_mismatched_digest_is_refused(self):
        for digest in ('f' * DIGEST_MAX_LENGTH, CHANGED_SHA256,
                       PAYLOAD_SHA256.upper(), PAYLOAD_SHA256[:-1]):
            with self.subTest(payload_sha256=digest):
                SnapshotV1.objects.all().delete()
                self.create_row(payload_sha256=digest)

                self.assertRefused(storage_v1.REASON_DIGEST)

    def test_a_row_with_a_digest_of_another_payload_is_refused(self):
        self.create_row(payload=CHANGED_PAYLOAD, payload_sha256=PAYLOAD_SHA256)

        self.assertRefused(storage_v1.REASON_DIGEST)

    def test_a_row_whose_payload_is_not_a_dictionary_is_refused(self):
        payloads = ([], ['Arsenal vs Chelsea'], 'not a payload', 42, 4.5, True)
        for payload in payloads:
            with self.subTest(payload=payload):
                SnapshotV1.objects.all().delete()
                self.create_row(
                    payload=payload,
                    payload_sha256=canonical_payload_sha256(payload),
                )

                self.assertRefused(storage_v1.REASON_PAYLOAD_SHAPE)

    def test_a_refusal_quotes_no_row_content(self):
        self.create_row(payload_sha256='f' * DIGEST_MAX_LENGTH)

        message = self.assertRefused(storage_v1.REASON_DIGEST)

        self.assertEqual(
            message,
            storage_v1.REFUSED_ROW_MESSAGE % (
                TYPE_KEY, storage_v1.REASON_DIGEST),
        )
        self.assertNotIn('f' * DIGEST_MAX_LENGTH, message)
        self.assertNotIn(PAYLOAD_SHA256, message)
        self.assertNotIn('Arsenal vs Chelsea', message)
        self.assertNotIn('freesupertips', message)


class DurableSnapshotInjectedRowTests(SimpleTestCase):
    """Rows a real column cannot hold, read exactly as a reader finds them.

    ``SimpleTestCase`` is deliberate here: these are the states a database round
    trip repairs - a naive timestamp comes back aware, a missing one cannot be
    written at all - so the row is injected instead, and a read that reached a
    database would fail these tests rather than pass them.
    """

    def setUp(self):
        super().setUp()
        self.provider = DatabaseSnapshotProvider()
        self.model = StubSnapshotModel(row=StubRow())

    def injected_load(self, **overrides):
        """Read the injected row, with the given columns overwritten."""
        self.model.objects.row = StubRow(**overrides)
        with mock.patch.object(storage_v1, 'SnapshotV1', self.model):
            return self.provider.load(TYPE_KEY)

    def assertRefusedOn(self, reason, **overrides):
        """Assert one injected row is refused, and reported, for ``reason``."""
        with self.assertLogs(storage_v1.LOGGER_NAME, level='ERROR') as logs:
            self.assertIsNone(self.injected_load(**overrides))

        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertIn(TYPE_KEY, message)
        self.assertIn(f'reason={reason}', message)
        return message

    def test_the_read_is_the_primary_key_lookup(self):
        with mock.patch.object(storage_v1, 'SnapshotV1', self.model):
            self.provider.load(TYPE_KEY)

        self.assertEqual(self.model.objects.filters, [{'pk': TYPE_KEY}])

    def test_a_usable_injected_row_yields_the_seam_record(self):
        with self.assertNoLogs(storage_v1.LOGGER_NAME, level='ERROR'):
            record = self.injected_load()

        self.assertEqual(set(record), set(SNAPSHOT_KEYS))
        self.assertEqual(record['schema_version'], SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(record['type_key'], TYPE_KEY)
        self.assertEqual(record['payload'], PAYLOAD)
        self.assertEqual(record['fetched_at'], FETCHED_AT)

    def test_an_injected_naive_timestamp_is_refused(self):
        self.assertRefusedOn(
            storage_v1.REASON_FETCHED_AT, fetched_at=NAIVE_FETCHED_AT)

    def test_an_injected_missing_timestamp_is_refused(self):
        self.assertRefusedOn(storage_v1.REASON_FETCHED_AT, fetched_at=None)

    def test_an_injected_non_datetime_timestamp_is_refused(self):
        values = (WIRE_FETCHED_AT, '', 0, 1.5, True, [], {},
                  datetime(2026, 9, 28).date())

        for value in values:
            with self.subTest(fetched_at=value):
                self.assertRefusedOn(
                    storage_v1.REASON_FETCHED_AT, fetched_at=value)

    def test_an_injected_non_utc_timestamp_is_read_as_utc(self):
        supplied = FETCHED_AT.astimezone(timezone(timedelta(hours=9)))

        record = self.injected_load(fetched_at=supplied)

        self.assertEqual(record['fetched_at'], FETCHED_AT)
        self.assertEqual(record['fetched_at'].utcoffset(), timedelta(0))

    def test_an_injected_non_string_digest_is_refused(self):
        values = (None, 12345, b'x' * DIGEST_MAX_LENGTH, ['digest'], '')

        for value in values:
            with self.subTest(payload_sha256=value):
                self.assertRefusedOn(
                    storage_v1.REASON_DIGEST, payload_sha256=value)

    def test_an_error_of_another_kind_is_not_swallowed(self):
        self.model.objects.error = ValueError('not a database failure')

        with self.assertRaises(ValueError):
            with mock.patch.object(storage_v1, 'SnapshotV1', self.model):
                self.provider.load(TYPE_KEY)

    def test_an_unreadable_table_is_refused_rather_than_raised(self):
        self.model.objects.error = DatabaseError('table is locked')

        message = self.assertRefusedOn(storage_v1.REASON_DATABASE)

        self.assertEqual(
            message,
            storage_v1.FAILED_READ_MESSAGE % (
                TYPE_KEY, storage_v1.REASON_DATABASE),
        )
        self.assertNotIn('table is locked', message)
        self.assertNotIn('locked', message)


class DurableSnapshotClearTests(TestCase):
    """Clearing removes every row, and clearing an empty table is a no-op."""

    def setUp(self):
        super().setUp()
        self.provider = DatabaseSnapshotProvider()

    def test_clear_removes_every_stored_row(self):
        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        self.provider.store(OTHER_TYPE_KEY, CHANGED_PAYLOAD,
                            fetched_at=FETCHED_AT)
        self.assertEqual(SnapshotV1.objects.count(), 2)

        self.provider.clear()

        self.assertFalse(SnapshotV1.objects.exists())
        self.assertIsNone(self.provider.load(TYPE_KEY))
        self.assertIsNone(self.provider.load(OTHER_TYPE_KEY))

    def test_clearing_an_empty_table_is_a_no_op(self):
        self.provider.clear()
        self.provider.clear()

        self.assertFalse(SnapshotV1.objects.exists())

        self.provider.store(TYPE_KEY, PAYLOAD, fetched_at=FETCHED_AT)
        self.assertEqual(SnapshotV1.objects.count(), 1)
        self.assertEqual(self.provider.load(TYPE_KEY)['payload'], PAYLOAD)


class DurableProviderVocabularyTests(SimpleTestCase):
    """The provider's public surface, and the names its contract is stated in."""

    def test_the_module_publishes_the_documented_names(self):
        for name in (
            'LOGGER_NAME', 'DIGEST_ALGORITHM', 'CANONICAL_JSON_SEPARATORS',
            'CANONICAL_TEXT_ENCODING', 'REASON_SCHEMA_VERSION', 'REASON_DIGEST',
            'REASON_PAYLOAD_SHAPE', 'REASON_FETCHED_AT', 'REASON_DATABASE',
            'REFUSED_ROW_MESSAGE', 'FAILED_READ_MESSAGE',
            'canonical_payload_bytes', 'canonical_payload_sha256',
            'DatabaseSnapshotProvider',
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(storage_v1, name))

    def test_the_logger_is_the_documented_channel(self):
        self.assertEqual(storage_v1.LOGGER_NAME, 'alltips_scraper.storage_v1')
        self.assertIs(
            storage_v1.logger, logging.getLogger(storage_v1.LOGGER_NAME))

    def test_the_public_surface_is_the_seams_three_methods(self):
        public = sorted(
            name for name in dir(DatabaseSnapshotProvider)
            if not name.startswith('_')
        )

        self.assertEqual(public, ['clear', 'load', 'store'])
        for name in public:
            with self.subTest(name=name):
                self.assertTrue(
                    callable(getattr(DatabaseSnapshotProvider, name)))

    def test_the_five_reasons_are_distinct_fixed_tokens(self):
        reasons = (
            storage_v1.REASON_SCHEMA_VERSION,
            storage_v1.REASON_DIGEST,
            storage_v1.REASON_PAYLOAD_SHAPE,
            storage_v1.REASON_FETCHED_AT,
            storage_v1.REASON_DATABASE,
        )

        self.assertEqual(len(set(reasons)), len(reasons))
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIsInstance(reason, str)
                self.assertEqual(reason, reason.lower())
                self.assertEqual(reason.strip(), reason)
                self.assertNotIn('%', reason)

    def test_both_log_templates_take_exactly_two_arguments(self):
        for template in (storage_v1.REFUSED_ROW_MESSAGE,
                         storage_v1.FAILED_READ_MESSAGE):
            with self.subTest(template=template):
                self.assertEqual(template.count('%s'), 2)
                self.assertEqual(
                    template % (TYPE_KEY, storage_v1.REASON_DIGEST),
                    template % (TYPE_KEY, storage_v1.REASON_DIGEST),
                )
                self.assertIn('type_key=', template)
                self.assertIn('reason=', template)

    def test_a_durable_provider_is_accepted_by_the_seam(self):
        provider = DatabaseSnapshotProvider()
        set_snapshot_provider(provider)
        self.addCleanup(set_snapshot_provider, None)

        self.assertIs(get_snapshot_provider(), provider)
        self.assertEqual(
            sorted(name for name in ('load', 'store', 'clear')
                   if not callable(getattr(provider, name, None))),
            [],
        )
