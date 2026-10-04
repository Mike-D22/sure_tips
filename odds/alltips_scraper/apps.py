"""The scraper app's configuration.

``ready()`` is the process-wide install point for the snapshot provider the
versioned tips API reads through. Django calls it once, after the app registry is
populated, and a seam that decides what a deployment publishes has to be wired in
somewhere that runs once per process: this is that place. The provider itself
belongs to ``jsoncontent_v1``, so this class only decides which provider the seam
gets.

The decision is deliberately unconditional and total. The published-content
reader is always the one installed, and ``ready()`` has no branch, reads nothing,
and needs nothing beyond the app's own modules to be importable. Constructing the
provider opens no file and reads no clock — the first read is what opens the
manifest beside it — and a read that finds no published key answers "no snapshot"
rather than raising, so installing it cannot stop a process from starting and a
deployment whose reviewed content names no key still starts and answers exactly as
one that never published anything.

The imports live inside ``ready()`` rather than at module level, so importing this
module still pulls in no ORM machinery: a caller that only wants the app label
does not pay for the model, and the app registry gains no import cycle with the
storage layer. Calling ``ready()`` twice installs the same kind of provider again
and changes nothing else, so it is idempotent: there is one seam slot, and it ends
up holding the read-only reader either way.
"""

from django.apps import AppConfig


class AlltipsScraperConfig(AppConfig):
    """The scraper app: its label, and the snapshot provider install point."""

    name = 'alltips_scraper'

    def ready(self):
        """Install the read-only published-content reader for this process."""
        from .readmodel_v1 import set_snapshot_provider
        from .jsoncontent_v1 import JsonSnapshotProvider

        set_snapshot_provider(JsonSnapshotProvider())