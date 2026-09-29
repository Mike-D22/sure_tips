"""Storage-layer tests for the durable v1 snapshot table.

This module pins the durable store only: the table identity, the five columns,
the primary key, the read order, the schema-version constraint, the row round
trip, and the shape of the first migration. Every value below is an explicit
literal - no test reads a clock, the network, or a fixture, and no digest is
ever compared against a value the code calculated.
"""

from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path

from django.db import IntegrityError, models, transaction
from django.db.migrations import CreateModel
from django.db.migrations.state import ProjectState
from django.db.models import CheckConstraint, NOT_PROVIDED, Q
from django.test import SimpleTestCase, TestCase

from .models import SnapshotV1

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
