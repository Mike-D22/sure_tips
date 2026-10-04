"""``manage.py validate_v1_content``: check a candidate content directory.

Why this command exists
-----------------------
Publishing canonical v1 content is a reviewed change to the content directory
inside the application package: a manifest and the payload files it names are
written by hand, read by a human, and committed and shipped with the image. The
reader that serves them, ``jsoncontent_v1``, is the right judge of that content at
runtime, but it is a poor judge of it during review: two of the states it answers
``None`` for are silent, and they are exactly the two a reviewer has to catch. A
candidate root with no manifest at all is the empty deployment to a reader, and a
type key the manifest does not name is simply not published yet, so a reviewer
would have to notice a missing manifest by hand and would only learn that a
payload file cannot be read if the endpoint happened to ask for that one key.

This command is the review-time reading of the same artifacts. It is handed the
candidate directory explicitly, checks every artifact against the reader's own
rules, prints one line per artifact, and exits non-zero when the candidate would
not load. It is a check and nothing else: it writes no file, creates no
directory, reads no configuration, reaches no network, touches no database, and
never touches the content directory this package ships unless that directory is
the one it was handed.

What it prints
--------------
One line per checked artifact, and only that line::

    manifest status=ok entries=<n>
    type=<token> status=ok
    type=<token> status=refused reason=<token>

A refused manifest is the one line that can be printed for it, because the entries
it names cannot be enumerated::

    manifest status=refused reason=<token>

The manifest line always comes first, and the type lines come in sorted key order,
so two runs over one candidate print the same report. Every type key is sanitised
by ``refresh_v1``'s report rule before it is printed, and every refusal names one
fixed reason token, so a report can be read without quoting a path, a file name, a
digest, a payload value, an instant, or an exception.

What the exit status means
--------------------------
Zero when the candidate is usable: the manifest is valid and every entry it names
would load, which includes a valid manifest that names no entry at all. One,
``CommandError``'s own status, when the manifest or one or more entries were
refused; the report lines name the artifacts and the checks, and this status is
what a script or a reviewer acts on. Two when the invocation itself cannot be
served - ``--root`` was not given, or names something that is not a directory - in
which case no artifact is checked and no report line is printed at all. Help exits
zero, as every Django command's help does.

Options
-------
``--root DIRECTORY`` names the candidate content directory to validate, and it is
required: no default is applied, because the directory this package ships is a
deployment's content decision rather than a fallback for a review. A run without it
is refused rather than defaulted, so a mistyped invocation can never be mistaken
for a validation of the artifacts an operator did not name.

See ``contentcheck_v1`` for the checks themselves, ``docs/RUNBOOK.md`` for the
publishing workflow this command belongs to, and ``docs/API_V1_CONTRACT.md`` for
the contract the content serves.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ... import contentcheck_v1

# What a run reports when a candidate is not usable, as one fixed line: the
# per-artifact reasons are the report on stdout, and this is the summary the exit
# status carries.
FAILURE_MESSAGE = 'one or more candidate v1 artifacts were refused'

# The two refusals of the invocation itself, each reported by its own token. They
# are named separately from the artifact vocabulary, because what they describe is
# the path a run was handed rather than anything about a candidate's content.
REASON_ROOT_NOT_GIVEN = 'root_not_given'
REASON_ROOT_NOT_A_DIRECTORY = 'root_not_a_directory'
ROOT_REQUIRED_MESSAGE = (
    '--root is required (reason=' + REASON_ROOT_NOT_GIVEN + ')'
)
ROOT_UNUSABLE_MESSAGE = (
    '--root must name an existing directory (reason='
    + REASON_ROOT_NOT_A_DIRECTORY + ')'
)

# The exit status a refused invocation reports, stated separately from
# ``CommandError``'s own status so a caller can tell "you handed me no usable
# directory" from "this candidate is not usable".
ROOT_RETURNCODE = 2


class Command(BaseCommand):
    """Check candidate canonical v1 content and report what would not load."""

    help = (
        'Validate candidate v1 canonical-content artifacts without publishing '
        'them.'
    )

    def add_arguments(self, parser):
        """Add the one option: the candidate directory to validate."""
        parser.add_argument(
            '--root',
            dest='root',
            metavar='DIRECTORY',
            help=(
                'Validate the candidate content directory named here. Required: '
                'the directory this package ships is never assumed, and nothing '
                'is written.'
            ),
        )

    def handle(self, *args, **options):
        """Refuse an unusable path, then validate the candidate and report it.

        The path is settled first, so a run handed no directory - or a path that
        is not one - prints no report line at all and exits with the invocation's
        own status. Only a readable directory reaches the checks, and the report
        it produces is printed in full before the exit status is decided, so a
        reviewer always sees the whole list of what has to be fixed.
        """
        root = options['root']
        if not root:
            raise CommandError(
                ROOT_REQUIRED_MESSAGE, returncode=ROOT_RETURNCODE)
        path = Path(root)
        if not path.is_dir():
            raise CommandError(
                ROOT_UNUSABLE_MESSAGE, returncode=ROOT_RETURNCODE)
        report = contentcheck_v1.validate_content_root(path)
        for line in report.lines():
            self.stdout.write(line)
        if not report.ok:
            raise CommandError(FAILURE_MESSAGE)
