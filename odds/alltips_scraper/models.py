"""The durable snapshot row for the versioned tips API.

One row per stored tip type: the type the row is filed under, the record
version that wrote it, the source envelope exactly as the parser returned it,
the digest of that envelope, and the authoritative instant it was fetched. The
table holds no derived counts and no fetch metadata, so a reader either finds a
usable row or finds nothing at all.
"""

from django.db import models

# The explicit table name. This shape is versioned, so the name carries the
# version and a later record shape gets its own table.
SNAPSHOT_V1_TABLE = 'alltips_scraper_snapshot_v1'

# The oldest record version this table accepts. Anything below it belongs to a
# shape this table does not describe, so the database refuses it outright.
SNAPSHOT_V1_SCHEMA_VERSION = 1

SNAPSHOT_V1_SCHEMA_VERSION_CONSTRAINT = 'snapshot_v1_schema_version_gte_1'


class SnapshotV1(models.Model):
    """One stored tip-type snapshot.

    ``type_key`` is the primary key, so a tip type has exactly one row and a
    rewrite replaces that row in place. ``payload`` is the source envelope as
    received. ``payload_sha256`` is the hex digest of the canonical form of
    that payload, so a reader can tell whether a row still describes the
    payload it claims to. ``fetched_at`` is the instant the payload was
    fetched, written as an aware UTC value.
    """

    type_key = models.CharField(max_length=64, primary_key=True)
    schema_version = models.PositiveSmallIntegerField()
    payload = models.JSONField()
    payload_sha256 = models.CharField(max_length=64)
    fetched_at = models.DateTimeField()

    class Meta:
        db_table = SNAPSHOT_V1_TABLE
        ordering = ('type_key',)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(schema_version__gte=SNAPSHOT_V1_SCHEMA_VERSION),
                name=SNAPSHOT_V1_SCHEMA_VERSION_CONSTRAINT,
            ),
        ]
