"""``manage.py prepare_v1_candidate``: stage one validated candidate v1 record.

Why this command exists
-----------------------
``validate_v1_content`` judges a candidate root that already exists, and the
README's publishing recipe asks a reviewer to write the payload file, compute the
canonical digest and spell the manifest entry by hand. Every one of those steps is
mechanical and every one of them is easy to get almost right - a digest over the
file rather than the payload, an instant with no offset, a file name with a
separator in it - and the mistake only shows up later, as a `503` for one type
key.

This command performs the mechanical part and nothing else. It is handed a payload
document, a type key, an authoritative instant and an output directory, builds the
record and the manifest with the rules in ``candidate_v1``, proves the result would
load by validating it with the same ``contentcheck_v1`` a reviewer runs, and only
then puts it in place. It is a developer/reviewer tool: it publishes nothing,
refreshes nothing, fetches nothing, deploys nothing and reads no configuration.

What it writes
--------------
Two files inside ``--out`` and nothing else: ``<type_key>.json`` holding the
payload, and ``manifest.json`` holding every entry the directory already named plus
the new one. No other file is created, overwritten or deleted, and the record file
is the byte form its own digest is taken over, so a reviewer can check the entry
against the file by eye. An entry the manifest already names is never replaced: a
byte-identical rerun is a no-op, and anything else is refused as
``entry_conflict``, which makes re-running one command over one input
deterministic.

What it prints
--------------
The validator's own report, verbatim, for the directory as it stands after the
run::

    manifest status=ok entries=<n>
    type=<token> status=ok

A refused run prints one line and stops - either the validator's report for the
existing directory, when that is what could not be used, or the one check that
refused it, in the validator's vocabulary::

    candidate status=refused reason=<token>
    type=<token> status=refused reason=<token>

A run that cannot vouch for ``--out`` says so instead of claiming a refusal -
after an installation, a reclamation or a cleanup step did not finish, or when the
directory stops validating after installation::

    candidate status=inspect reason=<token>

Every token is fixed, every type key is sanitised by ``reporting_v1.safe_token``,
and no line carries a path, a file name, a digest, a payload value, an instant or
an exception.

What the exit status means
--------------------------
Four answers, and only the first claims the directory is right:

* ``0`` - the candidate is in place and validates at the final validation point.
  The files this run intended to write were written; that is not a claim that the
  directory is unchanged, except for a byte-identical rerun, which writes nothing.
* ``1`` - the run was refused, and this run left final output unchanged after any
  recovery it needed completed successfully.
* ``2`` - the invocation cannot be served, and that is detected before anything is
  written: an option is missing, a path is not the kind of path it has to be, or
  ``--out`` is the packaged content directory, one of its descendants, or an
  ancestor of it.
* ``3`` - an installation, a recovery or a cleanup step did not finish, or the
  directory does not validate after installation. Output needs inspection: it may
  hold installed candidate files or residual staging, and nothing is promised to be
  undone.

These are statements about this run's own actions, not about a concurrent change by
another process, and ``3`` is not rollback: the command reports that it cannot
vouch for the directory rather than restoring it. Help exits zero, as every Django
command's help does.

Options
-------
``--payload FILE`` is the payload document to publish: JSON holding one object, the
same shape a source payload has. ``--type TYPE_KEY`` is the versioned registry key
the entry is filed under; a key the registry does not publish is refused rather
than written, because a record nothing can request is not content. ``--fetched-at
INSTANT`` is the authoritative instant as ISO-8601 text that carries an offset; it
is stored as supplied and read as UTC, and a naive or unparseable text is refused
rather than assumed to be UTC. ``--out DIRECTORY`` is the candidate content root to
prepare, and all four options are required: no default is applied anywhere, so a
mistyped invocation can never be mistaken for a preparation of artifacts an
operator did not name.

Staging and finalisation
------------------------
The run is three phases, and only the last two touch ``--out``:

1. **Decide.** The payload is read and parsed, the entry and both byte forms are
   built, and the existing directory is judged - all before anything is written.
   Every refusal that needs no filesystem change happens here, and a run refused
   here leaves ``--out`` exactly as it was, including not existing.
2. **Stage.** A temporary directory is created *inside* ``--out`` (so it is on the
   same filesystem as the files it will become), the record and the merged
   manifest are written there, the record files the manifest already names are
   copied in, and the whole staged directory is validated twice: with
   ``validate_content_root`` and with the real ``JsonSnapshotProvider``. A staged
   candidate that does not load is refused and the staging directory is removed.
   The staging directory is removed on every path; when it cannot be removed the
   run reports ``3`` rather than claiming either a clean refusal or a clean success.
3. **Finalise.** Each file is moved into place with one ``os.replace``, which is
   atomic per file: the record first, the manifest last, so the manifest never
   names a record that is not there yet. A file whose bytes are already the planned
   ones is not replaced at all, so a byte-identical rerun writes nothing. If the
   manifest replace fails, the record this run added is removed again, so a failed
   finalisation whose recovery completed leaves the directory as it was found. That
   recovery is best-effort rather than transactional: a record that cannot be taken
   back makes the run report ``3``, because the directory then holds a file its
   manifest does not name.

That is per-file atomicity, not a transaction over the directory: a process killed
between the two replaces leaves the record file in place and the old manifest,
which the reader ignores and a rerun of the same command reuses byte for byte
before replacing the manifest. Two runs are not coordinated with each other either;
the command is a review-time tool, and the review is the serialisation.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ... import candidate_v1
from ...canonical_json_v1 import CANONICAL_TEXT_ENCODING
from ...contentcheck_v1 import (
    FILE_KEY,
    SNAPSHOTS_KEY,
    TYPE_REFUSED_LINE,
    validate_content_root,
)
from ...jsoncontent_v1 import (
    CANONICAL_CONTENT_ROOT,
    MANIFEST_FILENAME,
    JsonSnapshotProvider,
)
from ...reporting_v1 import safe_token

# ---------------------------------------------------------------------------
# What a run says
# ---------------------------------------------------------------------------

# The one-line message a refused run ends with, on stderr, through CommandError:
# fixed, so the exit status is decided by a string that names no artifact.
FAILURE_MESSAGE = 'the candidate v1 content change was refused'

# The one line this command adds to the validator's vocabulary, in the validator's
# shape: a state of the *candidate* rather than of a type key, reported with the
# same fixed tokens.
REFUSAL_LINE = 'candidate status=refused reason={reason}'

# The line a run prints when it cannot vouch for ``--out``: the token is one of the
# same fixed set, but the status word says the directory has to be looked at instead
# of claiming that nothing changed.
INSPECT_LINE = 'candidate status=inspect reason={reason}'

# The same for a run that cannot vouch for the directory: one fixed sentence, no
# path and no exception text, telling the reviewer to look before reusing it.
INSPECT_MESSAGE = (
    'the candidate v1 content change needs inspection: --out may have changed')

OUT_REQUIRED_MESSAGE = '--out is required (reason=out_not_given)'
OUT_NOT_A_DIRECTORY_MESSAGE = (
    '--out must name a directory, or a path a directory can be created at '
    '(reason=out_not_a_directory)'
)
OUT_PARENT_MISSING_MESSAGE = (
    '--out names a directory whose parent does not exist '
    '(reason=out_parent_missing)'
)
OUT_REFUSED_MESSAGE = '--out was refused (reason=%s)'
PAYLOAD_REQUIRED_MESSAGE = '--payload is required (reason=payload_not_given)'
PAYLOAD_UNREADABLE_MESSAGE = (
    '--payload must name a readable file (reason=payload_unreadable)'
)
TYPE_REQUIRED_MESSAGE = '--type is required (reason=type_not_given)'
FETCHED_AT_REQUIRED_MESSAGE = '--fetched-at is required (reason=fetched_at_not_given)'

# ---------------------------------------------------------------------------
# Refusal tokens
# ---------------------------------------------------------------------------

# An invocation that cannot be served: nothing was read, nothing was written, and
# the operator has to correct the command line. Exit status 2.
OUT_NOT_GIVEN = 'out_not_given'
OUT_NOT_A_DIRECTORY = 'out_not_a_directory'
OUT_PARENT_MISSING = 'out_parent_missing'
OUT_UNRESOLVABLE = 'out_unresolvable'
SHIPPED_ROOT = 'shipped_root'
OUT_CONTAINS_SHIPPED = 'out_contains_shipped'
PAYLOAD_NOT_GIVEN = 'payload_not_given'
PAYLOAD_UNREADABLE = 'payload_unreadable'
TYPE_NOT_GIVEN = 'type_not_given'
FETCHED_AT_NOT_GIVEN = 'fetched_at_not_given'

INVOCATION_RETURNCODE = 2

# A candidate that cannot be prepared, exit status 1. The reasons that are rules
# about the artifact live in ``candidate_v1``; these six are states only the output
# directory can be in, so only this command can find them. ``out_escapes`` is one of
# them rather than an invocation refusal because the directory itself may be fine:
# it is the name this run would write through that resolves outside it.
OUT_NOT_EMPTY = 'out_not_empty'
OUT_UNUSABLE = 'out_unusable'
OUT_ESCAPES = 'out_escapes'
FILE_CONFLICT = 'file_conflict'
WRITE_FAILED = 'write_failed'
CANDIDATE_UNUSABLE = 'candidate_unusable'

REFUSAL_RETURNCODE = 1

# A run that cannot say what state it left the directory in: an installation, a
# reclamation or a cleanup step did not finish, or the directory stopped validating
# after installation. Exit status 3, because ``1`` means the output is unchanged and
# ``0`` means the directory is right - and this run can vouch for neither. It is an
# answer, not a rollback: nothing is promised to be undone.
OPERATIONAL_RETURNCODE = 3

# The two vocabularies above as fixed sets, so a caller - and a test - can say which
# kind of refusal a run answered with without reading the code that raised it. Both
# are disjoint from the artifact rules in ``candidate_v1``: a state of the command
# line and a state of the directory are not states of a record.
INVOCATION_REFUSALS = frozenset({
    OUT_NOT_GIVEN,
    OUT_NOT_A_DIRECTORY,
    OUT_PARENT_MISSING,
    OUT_UNRESOLVABLE,
    SHIPPED_ROOT,
    OUT_CONTAINS_SHIPPED,
    PAYLOAD_NOT_GIVEN,
    PAYLOAD_UNREADABLE,
    TYPE_NOT_GIVEN,
    FETCHED_AT_NOT_GIVEN,
})

DIRECTORY_REFUSALS = frozenset({
    OUT_NOT_EMPTY,
    OUT_UNUSABLE,
    OUT_ESCAPES,
    FILE_CONFLICT,
    WRITE_FAILED,
    CANDIDATE_UNUSABLE,
})

# The token a type-scoped refusal is reported with, kept beside the candidate-scoped
# one so a reader sees which of the two lines a state belongs to.
TYPE_SCOPED_REFUSALS = frozenset({
    candidate_v1.REASON_UNKNOWN_TYPE,
    candidate_v1.REASON_ENTRY_CONFLICT,
    FILE_CONFLICT,
})

# The staging directory inside ``--out``. Fixed and dotted so it is recognisable in
# a directory listing after an interrupted run, and never removed by a later run:
# this command deletes only what it created itself. A leftover from an earlier run
# therefore stays where it is - with no manifest beside it its contents make the
# root unusable (``out_not_empty``), and beside a manifest the validator ignores it,
# so an earlier run's leftover is not diagnosed. One this run fails to remove is
# reported, as exit status ``3``.
STAGING_PREFIX = '.prepare-v1-candidate-'


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def normalised(path):
    """Return ``path`` resolved, with the case folding this filesystem applies.

    Resolving first is what makes the containment checks below about the directory
    a file really lands in rather than about the spelling that was typed: `..`,
    `.`, a junction and a symbolic link all collapse to one answer.
    """
    return os.path.normcase(str(Path(path).resolve()))


def is_within(inner, outer):
    """Return whether normalised ``inner`` is ``outer`` or lies inside it."""
    try:
        return os.path.commonpath([inner, outer]) == outer
    except ValueError:
        # Paths on different drives share no prefix, so one cannot hold the other.
        return False


def out_path_refusal(out_path, shipped_root=CANONICAL_CONTENT_ROOT):
    """Return the reason token that refuses ``--out``, or ``None``.

    Three answers and no fourth: the packaged content directory is refused as
    ``shipped_root``, anything that would resolve inside it as the same token - a
    candidate prepared *in* the published content is a publication that skipped its
    review, which is the one thing this command exists not to do - an ancestor of it
    as ``out_contains_shipped``, because staging inside it and replacing a file
    there would reach the shipped tree through the path an operator named, and a
    path that cannot be resolved as ``out_unresolvable``.
    """
    try:
        out = normalised(out_path)
        shipped = normalised(shipped_root)
    except OSError:
        return OUT_UNRESOLVABLE
    if is_within(shipped, out):
        return SHIPPED_ROOT if out == shipped else OUT_CONTAINS_SHIPPED
    if is_within(out, shipped):
        return SHIPPED_ROOT
    return None


def escapes_root(root, name):
    """Return whether writing ``name`` inside ``root`` would land outside it.

    The record and the manifest are the two files a run replaces, and either name
    could be a symbolic link pointing somewhere else. ``os.replace`` would replace
    the link rather than write through it, but the check is made anyway: the
    guarantee this command states is about the named directory, and a run should not
    have to depend on what one platform's rename does with a link.
    """
    try:
        return not is_within(normalised(root / name), normalised(root))
    except OSError:
        return True


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    """Prepare one validated candidate v1 record in a content directory."""

    help = (
        'Prepare one candidate v1 canonical-content record: write the record file '
        'and its manifest entry, then validate the result. It publishes nothing, '
        'refreshes nothing and reaches no network.'
    )

    def add_arguments(self, parser):
        """Add the four options: payload, type key, instant and output directory."""
        parser.add_argument(
            '--payload',
            dest='payload',
            metavar='FILE',
            help='The payload document to prepare: JSON holding one object.',
        )
        parser.add_argument(
            '--type',
            dest='type',
            metavar='TYPE_KEY',
            help='The versioned registry type key the record is filed under.',
        )
        parser.add_argument(
            '--fetched-at',
            dest='fetched_at',
            metavar='INSTANT',
            help='The authoritative instant, as ISO-8601 text carrying an offset.',
        )
        parser.add_argument(
            '--out',
            dest='out',
            metavar='DIRECTORY',
            help='The candidate content directory to prepare into; created if absent.',
        )

    def handle(self, *args, **options):
        """Settle the invocation, plan the record, stage it, then finalise it.

        The order is deliberate: every refusal that needs no filesystem change comes
        first, so a run that is going to be refused is refused before anything is
        written, and the two phases that write are entered only by a run that has
        already decided it can finish.
        """
        out = self._out_directory(options)
        type_key = self._type_key(options)
        fetched_at_text = self._fetched_at(options)
        payload_path = self._payload_path(options)
        base_manifest = self._base_manifest(out)
        payload = self._payload(payload_path, type_key)
        plan = self._plan(payload, type_key, fetched_at_text, base_manifest)
        write_record = self._record_write_needed(out, plan)
        refusal, inspection_required = self._stage_and_finalise(
            out, plan, base_manifest, write_record)
        if refusal is not None:
            raise self._refusal(
                refusal, plan.type_key, inspection_required=inspection_required)
        self._report_and_finish(out, inspection_required)

    # -- the invocation ----------------------------------------------------

    def _out_directory(self, options):
        """Return the output directory, or refuse an invocation that cannot serve."""
        given = options.get('out')
        if not given:
            raise CommandError(
                OUT_REQUIRED_MESSAGE, returncode=INVOCATION_RETURNCODE)
        refusal = out_path_refusal(given)
        if refusal is not None:
            raise CommandError(
                OUT_REFUSED_MESSAGE % refusal,
                returncode=INVOCATION_RETURNCODE,
            )
        try:
            return Path(given).resolve()
        except OSError:
            raise CommandError(
                OUT_REFUSED_MESSAGE % OUT_UNRESOLVABLE,
                returncode=INVOCATION_RETURNCODE,
            )

    def _type_key(self, options):
        """Return the type key the entry is filed under, or refuse the invocation."""
        given = options.get('type')
        if not given:
            raise CommandError(
                TYPE_REQUIRED_MESSAGE, returncode=INVOCATION_RETURNCODE)
        return given

    def _fetched_at(self, options):
        """Return the authoritative instant's text, or refuse the invocation."""
        given = options.get('fetched_at')
        if not given:
            raise CommandError(
                FETCHED_AT_REQUIRED_MESSAGE, returncode=INVOCATION_RETURNCODE)
        return given

    def _payload_path(self, options):
        """Return the payload document's path, or refuse the invocation."""
        given = options.get('payload')
        if not given:
            raise CommandError(
                PAYLOAD_REQUIRED_MESSAGE, returncode=INVOCATION_RETURNCODE)
        path = Path(given)
        if not path.is_file():
            raise CommandError(
                PAYLOAD_UNREADABLE_MESSAGE, returncode=INVOCATION_RETURNCODE)
        return path

    # -- the candidate -----------------------------------------------------

    def _base_manifest(self, out):
        """Return the manifest to merge into, or refuse the directory as found.

        A directory that is absent, empty of a manifest, or holding a manifest and
        entries that validate is a candidate root this run may prepare. The three
        states that are not are answered differently and deliberately: a path that
        is not a directory, or a directory whose parent does not exist, is an
        invocation that cannot be served (exit 2); a directory that holds files but
        no manifest is refused as ``out_not_empty``, because a manifest-less
        directory with content is not a candidate root - the validator says
        ``manifest_missing`` - and assuming it was meant as one would put a manifest
        beside files nobody named; and a manifest that is refused has its own report
        printed before the refusal, so the reviewer sees which entry has to be
        fixed.
        """
        if not out.exists():
            if not out.parent.is_dir():
                raise CommandError(
                    OUT_PARENT_MISSING_MESSAGE,
                    returncode=INVOCATION_RETURNCODE,
                )
            return candidate_v1.empty_manifest()
        if not out.is_dir():
            raise CommandError(
                OUT_NOT_A_DIRECTORY_MESSAGE, returncode=INVOCATION_RETURNCODE)
        if escapes_root(out, MANIFEST_FILENAME):
            raise self._refusal(OUT_ESCAPES)
        manifest_file = out / MANIFEST_FILENAME
        if manifest_file.exists():
            report = validate_content_root(out)
            if not report.ok:
                for line in report.lines():
                    self.stdout.write(line)
                raise CommandError(FAILURE_MESSAGE)
            try:
                # The report has just said this manifest is usable, so reading it
                # needs no second rule: the parse only has to hand the mapping on.
                return json.loads(
                    manifest_file.read_text(encoding=CANONICAL_TEXT_ENCODING))
            except (OSError, UnicodeError, ValueError):
                # It changed between the check and the read. Refused rather than
                # guessed at, because the entries this run would carry over are no
                # longer the ones it validated.
                raise self._refusal(OUT_UNUSABLE)
        if any(out.iterdir()):
            raise self._refusal(OUT_NOT_EMPTY)
        return candidate_v1.empty_manifest()

    def _payload(self, path, type_key):
        """Return the payload document ``path`` holds, or refuse it."""
        try:
            text = path.read_text(encoding=CANONICAL_TEXT_ENCODING)
        except (OSError, UnicodeError):
            raise CommandError(
                PAYLOAD_UNREADABLE_MESSAGE, returncode=INVOCATION_RETURNCODE)
        try:
            return candidate_v1.parse_payload(text)
        except candidate_v1.CandidateRefusal as refusal:
            raise self._refusal(refusal.reason, type_key)

    def _plan(self, payload, type_key, fetched_at_text, base_manifest):
        """Return the plan for one record, or refuse the inputs."""
        try:
            return candidate_v1.plan_candidate(
                payload, type_key, fetched_at_text, base_manifest)
        except candidate_v1.CandidateRefusal as refusal:
            raise self._refusal(refusal.reason, type_key)

    def _record_write_needed(self, out, plan):
        """Return whether the record file has to be written, or refuse the collision.

        An identical file is left alone, so a rerun of one command over one input
        writes nothing and the record's modification time stays the review's rather
        than this run's. Anything else in the way - a different payload under the
        same entry, a directory with the record's name, a name that resolves outside
        the directory - is refused, because the alternative is either replacing an
        artifact this run did not produce or claiming in the manifest a file whose
        bytes nobody checked.
        """
        if escapes_root(out, plan.record_filename):
            raise self._refusal(OUT_ESCAPES, plan.type_key)
        target = out / plan.record_filename
        if not target.exists():
            return True
        if not target.is_file():
            raise self._refusal(FILE_CONFLICT, plan.type_key)
        try:
            existing = target.read_bytes()
        except OSError:
            raise self._refusal(OUT_UNUSABLE, plan.type_key)
        if existing != plan.record_bytes:
            raise self._refusal(FILE_CONFLICT, plan.type_key)
        return False

    # -- the write ---------------------------------------------------------

    def _stage_and_finalise(self, out, plan, base_manifest, write_record):
        """Stage the candidate, prove it would load, then put it in place.

        Returns ``(reason, inspection_required)``: the token of a refused
        finalisation - or ``None`` when the record and the manifest are both in
        place - and whether the directory still has to be inspected. The staging
        directory is removed on every path, and an output directory this run created
        is removed again whenever the run did not finalise, so a refused preparation
        leaves no half-written candidate root behind - which is what makes it safe to
        run against a directory that does not exist yet.

        Neither removal is assumed to have worked: a staging directory this run could
        not remove, or an output directory it created and could not remove, sets
        ``inspection_required``, so the outcome is settled after cleanup rather than
        before it. Nothing this run did not create is ever removed, and no removal
        failure hides the reason the run already had.
        """
        staging = None
        created = not out.is_dir()
        finalised = False
        inspection_required = False
        refusal = None
        try:
            if created:
                out.mkdir()
            staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=str(out)))
            self._fill_staging(staging, out, plan, base_manifest)
            refusal = self._staged_refusal(staging, plan)
            if refusal is None:
                refusal, inspection_required = self._finalise(
                    out, staging, plan, write_record)
                finalised = refusal is None
        except OSError:
            # The write phase failed before any rename put a file in place, so this
            # is a refusal on its own account; the cleanup below still decides
            # whether that can be said of the directory.
            refusal = WRITE_FAILED
            inspection_required = False
        finally:
            if staging is not None and not self._remove_staging(staging):
                inspection_required = True
            if created and not finalised and not self._remove_created(out):
                inspection_required = True
        return refusal, inspection_required

    def _remove_staging(self, staging):
        """Remove this run's staging directory; return whether it is gone.

        The removal is attempted without suppressing the failure, because whether it
        worked is part of the run's outcome: a staging directory left inside
        ``--out`` is something a reviewer has to see and remove.
        """
        try:
            shutil.rmtree(staging)
        except OSError:
            return False
        return True

    def _remove_created(self, out):
        """Remove the output directory this run created; return whether it is gone.

        Only a directory this run created is ever offered here, and only once the
        run did not finalise. ``rmdir`` is what makes it safe: a directory holding a
        file - the record a failed reclamation left, or one a reviewer put there -
        is not empty, so it is kept and the run reports that it must be inspected.
        """
        try:
            out.rmdir()
        except OSError:
            return False
        return True

    def _fill_staging(self, staging, out, plan, base_manifest):
        """Write the whole candidate into ``staging``, proving nothing yet.

        The entries the manifest already names are copied in beside the new record,
        because the staged directory has to be a complete content root: validating
        only the new entry would accept a manifest whose other entries are not
        there, and the reader resolves every entry against one directory.
        """
        for entry in base_manifest[SNAPSHOTS_KEY].values():
            name = entry[FILE_KEY]
            shutil.copyfile(out / name, staging / name)
        (staging / plan.record_filename).write_bytes(plan.record_bytes)
        (staging / MANIFEST_FILENAME).write_bytes(plan.manifest_bytes)

    def _staged_refusal(self, staging, plan):
        """Return the token that refuses a staged candidate, or ``None``.

        Two independent proofs, because they answer different questions: the
        validator says every artifact in the staged directory is compatible with the
        reader's rules, and the reader's own provider says the type key this run
        prepared actually resolves to a record. A staged candidate that passes both
        is what a deployment would serve, which is the only thing worth moving into
        place.
        """
        if not validate_content_root(staging).ok:
            return CANDIDATE_UNUSABLE
        if JsonSnapshotProvider(staging).load(plan.type_key) is None:
            return CANDIDATE_UNUSABLE
        return None

    def _finalise(self, out, staging, plan, write_record):
        """Move the staged files into ``out``, or report what stopped it.

        Returns ``(reason, inspection_required)``: ``(None, False)`` when both files
        are in place, otherwise the token that stopped the run and whether the
        directory can still be vouched for. The record goes first and the manifest
        last, so the manifest never names a file that is not there yet, and a
        manifest that cannot be replaced takes the record this run added with it -
        the directory is then the one the run found. Only a record *this run*
        installed is ever taken back; a record it found is not its to remove. When
        even that reclamation fails the run says the directory must be inspected,
        because it now holds a file its manifest does not name.
        """
        record = out / plan.record_filename
        if write_record:
            try:
                os.replace(staging / plan.record_filename, record)
            except OSError:
                # The record never arrived, so nothing of this run is in place.
                return WRITE_FAILED, False
        if not self._manifest_replace_needed(out, plan):
            return None, False
        try:
            os.replace(staging / MANIFEST_FILENAME, out / MANIFEST_FILENAME)
        except OSError:
            if not write_record:
                # This run installed no record of its own, so there is nothing to
                # take back and the directory is untouched.
                return WRITE_FAILED, False
            try:
                record.unlink()
            except OSError:
                # The record is in place and the manifest is the old one. Saying
                # "refused" here would claim an unchanged directory that is not
                # unchanged.
                return WRITE_FAILED, True
            return WRITE_FAILED, False
        return None, False

    def _manifest_replace_needed(self, out, plan):
        """Return whether the manifest has to be replaced, or already says it all.

        The comparison is over bytes rather than entries, because the promise a rerun
        makes is about the file: the planned manifest is the one documented form, so
        identical entries are identical bytes, and leaving the file alone keeps its
        modification time the review's rather than this run's. A manifest that cannot
        be read is replaced: the directory was validated before the plan was made, so
        that can only be a file that changed since, and the run's own final report is
        what refuses it.
        """
        try:
            return (out / MANIFEST_FILENAME).read_bytes() != plan.manifest_bytes
        except OSError:
            return True

    # -- the report --------------------------------------------------------

    def _report_and_finish(self, out, inspection_required=False):
        """Print the validator's report for the finished directory and settle it.

        The report is the *validator's*, on the directory as it now stands, rather
        than a summary this command invents: what a reviewer reads here is exactly
        what ``manage.py validate_v1_content`` says about the same directory, so the
        next step of the review needs no second run. A directory that does not
        validate after finalisation is reported in full and answered with exit status
        ``3``: its staging check passed, so something outside this process changed it
        in between, the files this run installed are already in place, and calling
        the run refused would claim the directory was left as it was found when it
        was not. The same status answers a run whose own cleanup did not finish, even
        when the report is good.
        """
        report = validate_content_root(out)
        for line in report.lines():
            self.stdout.write(line)
        if not report.ok:
            raise self._refusal(
                CANDIDATE_UNUSABLE, inspection_required=True)
        if inspection_required:
            raise self._refusal(WRITE_FAILED, inspection_required=True)

    def _refusal(self, reason, type_key=None, inspection_required=False):
        """Print the one line that reports ``reason`` and return the run's answer.

        A reason that names a type key is reported on a ``type=`` line and every
        other reason on a ``candidate`` line, so a reviewer can tell a state of one
        artifact from a state of the directory. The key is sanitised by the same rule
        the validator and the writer use, so a key read from the command line cannot
        forge a second line or a second field.

        A run that cannot vouch for the directory is answered on the ``inspect`` line
        with exit status ``3``, whatever the token: the token still says what stopped
        the run, and the status word says the directory has to be looked at rather
        than that it was left alone. That answer is chosen first, because an
        incomplete recovery cannot say which artifact of the directory is at fault.
        """
        if inspection_required:
            self.stdout.write(INSPECT_LINE.format(reason=reason))
            return CommandError(
                INSPECT_MESSAGE, returncode=OPERATIONAL_RETURNCODE)
        if type_key is not None and reason in TYPE_SCOPED_REFUSALS:
            self.stdout.write(
                TYPE_REFUSED_LINE.format(type=safe_token(type_key), reason=reason))
        else:
            self.stdout.write(REFUSAL_LINE.format(reason=reason))
        return CommandError(FAILURE_MESSAGE, returncode=REFUSAL_RETURNCODE)
