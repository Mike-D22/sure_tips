"""``manage.py refresh_tips``: the out-of-band v1 snapshot refresh.

Why this command exists
-----------------------
``refresh_v1`` knows how to fetch, validate and store one tip type, but a writer
whose job is to outlive a request has to be started from outside the request path.
This command is that entry point: it is the only supported way to run a refresh,
and nothing in the request path imports it, so no request can start a fetch, a
scrape, a refresh, or a cache fill.

What it prints
--------------
One line per tip type, in the registry's own order::

    type=<key> outcome=<ok|empty> state=<state> count=<n> [legs=<n>] fetched_at=<instant>
    type=<key> outcome=failed reason=<token>

``legs=`` is printed for the ``card`` unit only, where it is the total the cards
state for their own legs; a ``match`` unit has no legs field at all. Every key is
sanitised and every failure is named by a fixed reason token, so a line can be read
without quoting a URL, an exception, or a payload value, and no value the source
supplied can forge a second line. A failure is also logged through
``alltips_scraper.refresh_v1``.

What the exit status means
--------------------------
Zero when every selected type was published, which includes a type that published
the pinned empty envelope of its own source key (``outcome=empty``): the endpoint
answers that type with 200 and no tips. Two when ``--type`` names a type the
registry does not publish, which is refused before anything is fetched or written
and before a single report line is printed. One, ``CommandError``'s own status,
when any selected type was refused, which is how a scheduler or a cron job learns
the outcome. Tip types are refreshed one at a time and a refusal affects only the
type it belongs to: every type that succeeded stays stored, and the run's exit
status is the only thing that reports the ones that did not.

Options
-------
``--type TIP_TYPE`` refreshes only that type, and may be repeated to refresh
several. The selection is de-duplicated and always refreshed in the registry's own
order rather than in the order the options were given, so a run and its report
cannot disagree about the order the types exist in. Without it, every type the
versioned registry publishes is refreshed.

``--dry-run`` runs the whole pipeline short of the write: every selected type is
fetched, sanitised, serialised, digested and compared, and the report states the
state it would have written with ``dry-run:`` in front of it. No row is written or
replaced, so a dry run is the safe way to ask what a refresh would do.
"""

from django.core.management.base import BaseCommand, CommandError

from ... import refresh_v1
from ...serializers_v1 import UnknownTipType

# The exit status a refused run reports, as a fixed message: it names the outcome
# rather than the types, because which types failed is what the report lines and
# the log already say.
FAILURE_MESSAGE = 'one or more tip types could not be refreshed'

# The message and the exit status an unusable ``--type`` reports. The refused value
# is never quoted: which value was given is what the argument parser already showed
# the operator, and a type key is echoed back by the report lines and nowhere else.
UNKNOWN_TYPE_MESSAGE = (
    '--type must name a tip type the versioned registry publishes'
)
UNKNOWN_TYPE_RETURNCODE = 2


class Command(BaseCommand):
    """Fetch every requested tip type and store its v1 snapshot row."""

    help = 'Refresh the versioned tip snapshots from the configured sources.'

    def add_arguments(self, parser):
        """Add the two options: which types, and whether to write."""
        parser.add_argument(
            '--type',
            dest='tip_types',
            action='append',
            metavar='TIP_TYPE',
            help=(
                'Refresh only this tip type. Repeat the option to refresh more '
                "than one; the selection is de-duplicated and refreshed in the "
                "registry's own order. Defaults to every type the versioned "
                'registry publishes.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            dest='dry_run',
            action='store_true',
            help=(
                'Run every selected type through the whole pipeline and report '
                'the state each one would write, without storing any snapshot.'
            ),
        )

    def handle(self, *args, **options):
        """Refresh, report one line per type, and refuse the run if any failed.

        The selection is settled before the first fetch, so an unusable ``--type``
        prints no report line at all and exits with its own status; every other
        refusal is a per-type outcome that leaves the run's remaining types alone.
        """
        try:
            type_keys = refresh_v1.resolve_type_keys(options['tip_types'])
        except UnknownTipType:
            raise CommandError(
                UNKNOWN_TYPE_MESSAGE, returncode=UNKNOWN_TYPE_RETURNCODE
            )
        outcomes = refresh_v1.refresh_types(
            type_keys, dry_run=options['dry_run']
        )
        for outcome in outcomes:
            self.stdout.write(outcome.line())
        if any(not outcome.ok for outcome in outcomes):
            raise CommandError(FAILURE_MESSAGE)
