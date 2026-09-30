"""The scraper app's configuration.

``ready()`` is the process-wide install point for the durable v1 snapshot
provider. Django calls it once, after the app registry is populated, and a seam
whose job is to outlive the process has to be wired in somewhere that runs once
per process: this is that place. The provider itself belongs to ``storage_v1``,
so this class only decides which provider the seam gets.

The decision is deliberately unconditional and total. The durable provider is
always the one installed, and ``ready()`` has no branch, reads nothing, and needs
nothing beyond the app's own modules to be importable. Constructing the provider
runs no query — the first durable read or write is what touches the database —
and a read that fails degrades to "no snapshot" rather than raising, so
installing it cannot stop a process from starting because the row it will look
for does not exist yet.

The imports live inside ``ready()`` rather than at module level, so importing this
module still pulls in no ORM machinery: a caller that only wants the app label
does not pay for the model, and the app registry gains no import cycle with the
storage layer. Calling ``ready()`` twice installs the same kind of provider again
and changes nothing else, so it is idempotent: there is one seam slot, and it
ends up holding a durable provider either way.
"""

from django.apps import AppConfig


class AlltipsScraperConfig(AppConfig):
    """The scraper app: its label, and the snapshot provider install point."""

    name = 'alltips_scraper'

    def ready(self):
        """Install the durable snapshot provider for this process."""
        from .readmodel_v1 import set_snapshot_provider
        from .storage_v1 import DatabaseSnapshotProvider

        set_snapshot_provider(DatabaseSnapshotProvider())