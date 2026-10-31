"""Offline contract tests for the candidate-preparation rules and command.

Why this module exists
----------------------
``prepare_v1_candidate`` exists so that publishing one canonical v1 record is not
a hand-written edit: the reviewer supplies a payload, a type key and an instant,
and the command writes the record and the manifest entry the review used to spell
by hand. That is only worth trusting if three things hold, so all three are pinned
here.

1. **The rules are the reader's rules, and the bytes are the reviewed bytes.** The
   entry a plan builds, the digest it claims, the record file it writes and the
   manifest it merges are asserted value by value, and the digests are pinned
   literals computed once with ``canonical_json_v1`` - so a change to the byte form
   fails here instead of silently re-blessing content. Two runs over one input are
   asserted to plan byte-identical artifacts, which is what makes a rerun a no-op
   rather than a rewrite.
2. **A run either finishes, or says what it could not finish.** Every refusal is
   asserted through its exact report line and its exit status, and the candidate
   directory is compared byte for byte around every run - including a run whose
   manifest replace fails, which has to take the record it added back out again. A
   directory the run created is asserted to be gone after a refusal, so a mistyped
   invocation cannot leave an empty candidate root behind either. Where a recovery,
   a cleanup or the final check cannot complete, the run is asserted to answer with
   `candidate status=inspect` and exit `3` instead of claiming an unchanged
   directory it cannot vouch for.
3. **It cannot publish by accident.** The packaged content directory, anything
   inside it and anything that contains it are asserted to be refused by the
   invocation, with the shipped tree compared byte for byte; every type key
   reaches the output through ``reporting_v1.safe_token``, so a hostile key cannot
   forge a second line; and no run is asked to write outside the directory it was
   handed.

Ground rules
------------
* **No network, no database, no clock.** Every candidate is written into a
  temporary directory by the test itself, and the socket layer is disabled for
  every test method through ``OfflineGuardMixin``, so a run that reached a network
  would raise rather than pass quietly.
* **The digests are pinned literals.** Each one below was computed once with
  ``canonical_json_v1`` and is written out in full; a test asserts every literal
  still matches what the rule produces.
* **The vocabularies are asserted as sets.** The artifact reasons are
  ``candidate_v1``'s, the invocation and directory reasons are the command's, and
  the three are asserted disjoint and complete - so a token added in one place
  cannot quietly borrow another's meaning.

See ``candidate_v1`` for the rules these tests exercise,
``management/commands/prepare_v1_candidate.py`` for the command that applies them,
``contentcheck_v1`` for the validator every run ends with,
``tests_contentcheck_v1.py`` for that validator's own contract, and
``docs/RUNBOOK.md`` section 4.4 for the publishing workflow.
"""

import ast
import errno
import json
import os
import tempfile
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from . import candidate_v1, contentcheck_v1, jsoncontent_v1
from .canonical_json_v1 import canonical_payload_bytes, canonical_payload_sha256
from .candidate_v1 import (
    REASON_ENTRY_CONFLICT,
    REASON_FETCHED_AT,
    REASON_PAYLOAD_JSON,
    REASON_PAYLOAD_SHAPE,
    REASON_TOKENS,
    REASON_UNKNOWN_TYPE,
    CandidateRefusal,
    empty_manifest,
    manifest_bytes,
    parse_authoritative_instant,
    parse_payload,
    plan_candidate,
    record_file_bytes,
    record_filename_for,
)
from .contentcheck_v1 import (
    FILE_KEY,
    MANIFEST_OK_LINE,
    SCHEMA_VERSION_KEY,
    SHA256_KEY,
    SNAPSHOTS_KEY,
    TYPE_OK_LINE,
    TYPE_REFUSED_LINE,
    validate_content_root,
)
from .jsoncontent_v1 import (
    CANONICAL_CONTENT_ROOT,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    RECORD_TIMESTAMP_KEY,
    JsonSnapshotProvider,
)
from .management.commands import prepare_v1_candidate
from .readmodel_v1 import SNAPSHOT_SCHEMA_VERSION
from .reporting_v1 import MAX_TYPE_KEY_LENGTH, safe_token
from .serializers_v1 import SUPPORTED_TIP_TYPES
from .tests_parser_contract import OfflineGuardMixin

# ---------------------------------------------------------------------------
# Fixed inputs. Every value is an explicit literal: no test reads a clock, and
# each digest was computed once from ``canonical_json_v1``.
# ---------------------------------------------------------------------------

TYPE_KEY = 'bet_of_the_day'
OTHER_TYPE_KEY = 'daily_accumulator'

RECORD_FILENAME = 'bet_of_the_day.json'

FETCHED_AT_TEXT = '2026-09-28T17:12:03+00:00'
OFFSET_FETCHED_AT_TEXT = '2026-09-28T20:42:03+03:30'
NAIVE_FETCHED_AT_TEXT = '2026-09-28T17:12:03'
FETCHED_AT = datetime(2026, 9, 28, 17, 12, 3, tzinfo=timezone.utc)

PAYLOAD = {
    'date': '2026-09-27',
    'total_tips': 1,
    'matches': [
        {'match_title': 'Arsenal vs Chelsea', 'prediction': 'Arsenal to win'},
    ],
    'count': 1,
    'source': 'freesupertips',
}
PAYLOAD_SHA256 = (
    '36cfd8636014fb7edc478b3337e863a232957aecd19d24203abfed0044c42659')

# A payload whose text contains non-ASCII characters: the canonical form escapes
# them, so the file a run writes and the payload it holds are not byte equal.
UNICODE_PAYLOAD = {
    'date': '2026-09-27',
    'matches': [
        {'match_title': 'Olimpia vs Cerro Porteño',
         'prediction': 'Olimpia to win',
         'tip_reason': 'Aprobado por el café de la mañana'},
    ],
    'count': 1,
    'source': 'freesupertips',
}
UNICODE_PAYLOAD_SHA256 = (
    'cfaba8eed8969d6eb0a95822cd3375deebc7bf5042a7acc686e905c2346bd2d8')

# The mark a Windows editor writes in front of a document the reviewer meant. It
# is tolerated on the way in and never written on the way out.
BYTE_ORDER_MARK = '\ufeff'

# A type key a command line could carry, and the token it must be reported as: the
# newline would forge a second line and the space and ``=`` would invent a field.
HOSTILE_TYPE_KEY = 'bad key=1\nsecond line'
HOSTILE_TYPE_TOKEN = 'bad?key?1?second?line'

# Every shape the command must refuse a payload document in, and every one it must
# accept, spelled as text so the parse is what is under test.
PAYLOAD_TEXT_REFUSALS = (
    ('{', REASON_PAYLOAD_JSON),
    ('', REASON_PAYLOAD_JSON),
    (BYTE_ORDER_MARK + '{', REASON_PAYLOAD_JSON),
    ('[]', REASON_PAYLOAD_SHAPE),
    ('"payload"', REASON_PAYLOAD_SHAPE),
    ('7', REASON_PAYLOAD_SHAPE),
    (BYTE_ORDER_MARK + '[1, 2]', REASON_PAYLOAD_SHAPE),
)

# The instant texts that name no instant, so the reader's own rule refuses them
# rather than this command's defaulting to UTC.
INSTANT_TEXT_REFUSALS = (
    NAIVE_FETCHED_AT_TEXT,
    '',
    'yesterday',
    '2026-13-01T00:00:00+00:00',
)

# A manifest entry's shape, so a test can write the directory it means to hand a
# run rather than producing it with the code under test.
def manifest_entry(type_key=TYPE_KEY, sha256=PAYLOAD_SHA256,
                   fetched_at=FETCHED_AT_TEXT):
    """Return one manifest entry of the documented shape for ``type_key``."""
    return {
        SCHEMA_VERSION_KEY: SNAPSHOT_SCHEMA_VERSION,
        FILE_KEY: type_key + '.json',
        SHA256_KEY: sha256,
        RECORD_TIMESTAMP_KEY: fetched_at,
    }


def broken_staged_fill(real_fill):
    """Return a ``_fill_staging`` replacement whose staged manifest is wrong.

    The staged directory is written by the command's own code and then made to fail
    validation for exactly one reason: the digest its manifest claims is not the
    digest of the record beside it. That is the only way to reach the staged refusal
    with a directory the run itself built, so the check under test is the staged
    proof rather than the state of ``--out``.
    """

    def fill(command, staging, out, plan, base_manifest):
        real_fill(command, staging, out, plan, base_manifest)
        broken = json.loads(json.dumps(plan.manifest))
        broken[SNAPSHOTS_KEY][TYPE_KEY][SHA256_KEY] = '0' * 64
        (staging / MANIFEST_FILENAME).write_bytes(manifest_bytes(broken))

    return fill


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


class CandidateRulesTests(OfflineGuardMixin, SimpleTestCase):
    """``candidate_v1``: the artifacts one record is made of, decided without a disk.

    A payload, a type key and an instant go in, and the entry, the record bytes and
    the manifest bytes come out. Nothing here writes anything, which is what makes
    the module worth testing apart from the command that stages its plans: the rules
    can be read and pinned without a filesystem in the picture.
    """

    def test_the_vocabulary_is_the_readers_plus_three_of_its_own(self):
        """Two reasons are the reader's objects, and three are this module's."""
        borrowed = {jsoncontent_v1.REASON_PAYLOAD_SHAPE,
                    jsoncontent_v1.REASON_FETCHED_AT}
        for token in borrowed:
            self.assertTrue(
                any(token is own for own in REASON_TOKENS), token)

        self.assertEqual(
            REASON_TOKENS - borrowed,
            {REASON_UNKNOWN_TYPE, REASON_ENTRY_CONFLICT, REASON_PAYLOAD_JSON},
        )
        self.assertEqual(len(REASON_TOKENS), 5)
        for token in REASON_TOKENS:
            with self.subTest(token=token):
                self.assertRegex(token, r'\A[a-z][a-z0-9_]*\Z')

    def test_a_payload_is_read_as_the_document_it_holds(self):
        """A JSON object is the payload, one mark is tolerated, and the rest is not."""
        text = json.dumps(PAYLOAD)

        self.assertEqual(parse_payload(text), PAYLOAD)
        self.assertEqual(parse_payload(BYTE_ORDER_MARK + text), PAYLOAD)
        self.assertEqual(parse_payload('  ' + text), PAYLOAD)
        self.assertEqual(parse_payload(json.dumps(UNICODE_PAYLOAD)),
                         UNICODE_PAYLOAD)

        for refused_text, reason in PAYLOAD_TEXT_REFUSALS:
            with self.subTest(text=refused_text):
                with self.assertRaises(CandidateRefusal) as caught:
                    parse_payload(refused_text)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(str(caught.exception), reason)

    def test_the_instant_is_read_as_the_reader_reads_it(self):
        """An instant with an offset names one; a text that names none is refused."""
        self.assertEqual(parse_authoritative_instant(FETCHED_AT_TEXT), FETCHED_AT)
        self.assertEqual(
            parse_authoritative_instant(OFFSET_FETCHED_AT_TEXT), FETCHED_AT)

        for text in INSTANT_TEXT_REFUSALS:
            with self.subTest(text=text):
                with self.assertRaises(CandidateRefusal) as caught:
                    parse_authoritative_instant(text)
                self.assertEqual(caught.exception.reason, REASON_FETCHED_AT)
                self.assertEqual(str(caught.exception), REASON_FETCHED_AT)

    def test_a_record_filename_is_a_registry_key_and_nothing_else(self):
        """The name is the key plus ``.json``; a key the registry lacks is refused."""
        for type_key in sorted(SUPPORTED_TIP_TYPES):
            with self.subTest(type_key=type_key):
                self.assertEqual(
                    record_filename_for(type_key), type_key + '.json')

        for type_key in (
            'nope',
            '',
            'Bet_of_the_day',
            'bet of the day',
            'bet_of_the_day.json',
            '../bet_of_the_day',
            'bet_of_the_day/../bet_of_the_day',
        ):
            with self.subTest(type_key=type_key):
                with self.assertRaises(CandidateRefusal) as caught:
                    record_filename_for(type_key)
                self.assertEqual(caught.exception.reason, REASON_UNKNOWN_TYPE)

    def test_the_record_bytes_are_the_form_its_digest_is_taken_over(self):
        """The record is the canonical bytes plus a newline, and the digest agrees."""
        record = record_file_bytes(PAYLOAD)

        self.assertEqual(record, canonical_payload_bytes(PAYLOAD) + b'\n')
        self.assertEqual(record[:-1], canonical_payload_bytes(PAYLOAD))
        self.assertTrue(record.endswith(b'}\n'))
        self.assertNotIn(b'\r', record)
        self.assertNotIn(BYTE_ORDER_MARK.encode('utf-8'), record)
        self.assertEqual(
            canonical_payload_sha256(json.loads(record)), PAYLOAD_SHA256)
        self.assertEqual(canonical_payload_sha256(PAYLOAD), PAYLOAD_SHA256)

        unicode_record = record_file_bytes(UNICODE_PAYLOAD)
        self.assertEqual(
            unicode_record, unicode_record.decode('ascii').encode('ascii'))
        self.assertIn(b'\\u00f1', unicode_record)
        self.assertEqual(
            canonical_payload_sha256(UNICODE_PAYLOAD), UNICODE_PAYLOAD_SHA256)

    def test_the_manifest_bytes_are_one_fixed_document_form(self):
        """Indented, key-sorted, ASCII, newline-terminated - and the same twice."""
        manifest = {
            SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
            SNAPSHOTS_KEY: {TYPE_KEY: manifest_entry()},
        }
        written = manifest_bytes(manifest)
        text = written.decode('utf-8')

        self.assertEqual(json.loads(text), manifest)
        self.assertEqual(
            text, json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        self.assertTrue(text.endswith('\n'))
        self.assertNotIn('\r', text)
        self.assertEqual(manifest_bytes(manifest), written)
        entry_text = text[text.index('"snapshots"'):]
        self.assertLess(
            entry_text.index('"fetched_at"'), entry_text.index('"file"'))
        self.assertLess(
            entry_text.index('"file"'), entry_text.index('"schema_version"'))
        self.assertLess(
            entry_text.index('"schema_version"'), entry_text.index('"sha256"'))
        self.assertEqual(
            empty_manifest(),
            {SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION, SNAPSHOTS_KEY: {}},
        )

    def test_a_plan_names_one_record_and_carries_the_others_over(self):
        """The plan holds the entry a reviewer would spell, and only adds to it."""
        base = {
            SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
            SNAPSHOTS_KEY: {
                OTHER_TYPE_KEY: manifest_entry(
                    OTHER_TYPE_KEY, sha256=UNICODE_PAYLOAD_SHA256),
            },
        }
        plan = plan_candidate(PAYLOAD, TYPE_KEY, FETCHED_AT_TEXT, base)

        self.assertEqual(plan.type_key, TYPE_KEY)
        self.assertEqual(plan.record_filename, RECORD_FILENAME)
        self.assertEqual(plan.entry, manifest_entry())
        self.assertEqual(plan.entry[RECORD_TIMESTAMP_KEY], FETCHED_AT_TEXT)
        self.assertEqual(plan.entries(), 2)
        self.assertEqual(
            plan.manifest[SCHEMA_VERSION_KEY], MANIFEST_SCHEMA_VERSION)
        self.assertEqual(
            plan.manifest[SNAPSHOTS_KEY][OTHER_TYPE_KEY],
            base[SNAPSHOTS_KEY][OTHER_TYPE_KEY],
        )
        self.assertEqual(plan.manifest[SNAPSHOTS_KEY][TYPE_KEY], plan.entry)
        self.assertEqual(plan.record_bytes, record_file_bytes(PAYLOAD))
        self.assertEqual(plan.manifest_bytes, manifest_bytes(plan.manifest))
        self.assertNotIn(TYPE_KEY, base[SNAPSHOTS_KEY])
        self.assertEqual(
            repr(plan), "CandidatePlan(type_key='bet_of_the_day', entries=2)")

        fresh = plan_candidate(PAYLOAD, TYPE_KEY, FETCHED_AT_TEXT)
        self.assertEqual(fresh.entries(), 1)
        self.assertEqual(
            fresh.manifest,
            {SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
             SNAPSHOTS_KEY: {TYPE_KEY: fresh.entry}},
        )
        self.assertEqual(fresh.manifest[SNAPSHOTS_KEY], {TYPE_KEY: fresh.entry})

    def test_two_plans_over_one_input_are_byte_equal(self):
        """The same input plans the same artifacts, whatever it was read from."""
        first = plan_candidate(PAYLOAD, TYPE_KEY, OFFSET_FETCHED_AT_TEXT)
        second = plan_candidate(
            json.loads(json.dumps(PAYLOAD)), TYPE_KEY, OFFSET_FETCHED_AT_TEXT,
            json.loads(json.dumps(empty_manifest())))

        self.assertEqual(first.entry, second.entry)
        self.assertEqual(first.record_bytes, second.record_bytes)
        self.assertEqual(first.manifest_bytes, second.manifest_bytes)

        again = plan_candidate(
            PAYLOAD, TYPE_KEY, OFFSET_FETCHED_AT_TEXT, first.manifest)
        self.assertEqual(again.entry, first.entry)
        self.assertEqual(again.record_bytes, first.record_bytes)
        self.assertEqual(again.manifest_bytes, first.manifest_bytes)
        self.assertEqual(again.entries(), 1)

    def test_a_plan_refuses_what_it_cannot_file(self):
        """A shape, a key, an instant and a replaced entry are the four refusals."""
        conflict = {
            SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
            SNAPSHOTS_KEY: {
                TYPE_KEY: manifest_entry(fetched_at=OFFSET_FETCHED_AT_TEXT),
            },
        }
        cases = (
            (([1], TYPE_KEY, FETCHED_AT_TEXT, None), REASON_PAYLOAD_SHAPE),
            ((PAYLOAD, 'nope', FETCHED_AT_TEXT, None), REASON_UNKNOWN_TYPE),
            ((PAYLOAD, TYPE_KEY, NAIVE_FETCHED_AT_TEXT, None), REASON_FETCHED_AT),
            ((PAYLOAD, TYPE_KEY, FETCHED_AT_TEXT, conflict), REASON_ENTRY_CONFLICT),
        )

        for arguments, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(CandidateRefusal) as caught:
                    plan_candidate(*arguments)
                self.assertEqual(caught.exception.reason, reason)

        replayed = plan_candidate(
            PAYLOAD, TYPE_KEY, FETCHED_AT_TEXT,
            {SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
             SNAPSHOTS_KEY: {TYPE_KEY: manifest_entry()}})
        self.assertEqual(replayed.entry, manifest_entry())
        self.assertEqual(replayed.entries(), 1)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class PrepareV1CandidateTests(OfflineGuardMixin, SimpleTestCase):
    """``manage.py prepare_v1_candidate``: what it writes, and what it refuses.

    Every run here is over a directory this class made, inside its own temporary
    root, so nothing shipped is ever prepared by accident and no two tests share a
    candidate. The candidate directory is compared byte for byte around every run,
    so a refusal cannot be a write in disguise and a rerun cannot be a rewrite.
    """

    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name)
        self.payload_path = self.write_payload(PAYLOAD)
        self.out = self.base / 'candidate'

    # -- inputs ------------------------------------------------------------

    def write_payload(self, payload, name='payload.json'):
        """Write ``payload`` as a JSON document under this test's root."""
        path = self.base / name
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def write_candidate(self, name, raw):
        """Write ``raw`` bytes into this test's candidate directory."""
        self.out.mkdir(exist_ok=True)
        (self.out / name).write_bytes(raw)

    def make_link(self, target, link, directory=True):
        """Link ``link`` to ``target``, or skip where the platform cannot.

        Only a recognised platform limitation is skipped: a Python build without
        symbolic links, or the Windows privilege that creating one needs. Any other
        failure is a broken fixture and is raised, so an unexpected error cannot be
        recorded as an unsupported platform. A test that skips here asserts nothing,
        so the coverage it would have added is reported as skipped, never as passed.
        """
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (AttributeError, NotImplementedError):
            self.skipTest('symbolic links are unavailable in this Python build')
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314 or error.errno in (
                    errno.ENOTSUP, errno.EOPNOTSUPP):
                self.skipTest(
                    'symbolic links need a privilege this account lacks')
            raise

    def options(self, **overrides):
        """Return the four options a complete invocation carries."""
        options = {
            'payload': str(self.payload_path),
            'type': TYPE_KEY,
            'fetched_at': FETCHED_AT_TEXT,
            'out': str(self.out),
        }
        options.update(overrides)
        return options

    # -- running -----------------------------------------------------------

    def invoke(self, *argv, **options):
        """Run the command and return its status, its lines and its message."""
        out = StringIO()
        err = StringIO()
        try:
            call_command(
                'prepare_v1_candidate', *argv, stdout=out, stderr=err, **options)
        except CommandError as refusal:
            self.assertEqual(err.getvalue(), '')
            return refusal.returncode, out.getvalue().splitlines(), str(refusal)
        self.assertEqual(err.getvalue(), '')
        return 0, out.getvalue().splitlines(), ''

    def run_command(self, **overrides):
        """Run over complete options, overriding the ones a test varies."""
        return self.invoke(**self.options(**overrides))

    # -- looking -----------------------------------------------------------

    def contents(self, root=None):
        """Return every file under ``root`` (the candidate by default), as bytes."""
        root = self.out if root is None else Path(root)
        if not root.exists():
            return {}
        return {
            file.relative_to(root).as_posix(): file.read_bytes()
            for file in sorted(root.rglob('*')) if file.is_file()
        }

    def directory_names(self, root=None):
        """Return every directory name under ``root``, to catch a staging leftover."""
        root = self.out if root is None else Path(root)
        if not root.exists():
            return []
        return sorted(entry.name for entry in root.rglob('*') if entry.is_dir())

    def manifest(self):
        """Return the manifest as the value a reviewer would read."""
        return json.loads(
            (self.out / MANIFEST_FILENAME).read_text(encoding='utf-8'))

    # -- what a run writes -------------------------------------------------

    def test_the_command_is_registered_and_offers_its_four_options(self):
        """It is this app's command, and none of its four options has a default."""
        self.assertEqual(
            get_commands()['prepare_v1_candidate'], 'alltips_scraper')

        parser = prepare_v1_candidate.Command().create_parser(
            'manage.py', 'prepare_v1_candidate')
        parsed = parser.parse_args([])
        for option in ('payload', 'type', 'fetched_at', 'out'):
            with self.subTest(option=option):
                self.assertIsNone(getattr(parsed, option))

        documented = (
            prepare_v1_candidate.Command.help
            + (prepare_v1_candidate.__doc__ or ''))
        for option in ('--payload', '--type', '--fetched-at', '--out'):
            with self.subTest(option=option):
                self.assertIn(option, documented)

    def test_a_fresh_directory_gets_one_record_and_one_manifest(self):
        """Two files, the planned bytes, and the validator's own report on stdout."""
        status, lines, message = self.run_command()

        self.assertEqual((status, message), (0, ''))
        self.assertEqual(lines, [
            MANIFEST_OK_LINE.format(entries=1),
            TYPE_OK_LINE.format(type=TYPE_KEY),
        ])

        expected = plan_candidate(PAYLOAD, TYPE_KEY, FETCHED_AT_TEXT)
        self.assertEqual(self.contents(), {
            RECORD_FILENAME: expected.record_bytes,
            MANIFEST_FILENAME: expected.manifest_bytes,
        })
        self.assertEqual(
            sorted(entry.name for entry in self.out.iterdir()),
            [RECORD_FILENAME, MANIFEST_FILENAME],
        )
        self.assertEqual(self.directory_names(), [])
        self.assertEqual(
            self.manifest()[SNAPSHOTS_KEY], {TYPE_KEY: expected.entry})
        self.assertTrue(validate_content_root(self.out).ok)
        self.assertIsNotNone(JsonSnapshotProvider(self.out).load(TYPE_KEY))

    def test_a_rerun_writes_nothing_at_all(self):
        """The second run prints the same report and leaves both files untouched."""
        self.run_command()
        before = self.contents()
        stamps = {name: (self.out / name).stat().st_mtime_ns for name in before}

        status, lines, message = self.run_command()

        self.assertEqual((status, message), (0, ''))
        self.assertEqual(lines, [
            MANIFEST_OK_LINE.format(entries=1),
            TYPE_OK_LINE.format(type=TYPE_KEY),
        ])
        self.assertEqual(self.contents(), before)
        self.assertEqual(
            {name: (self.out / name).stat().st_mtime_ns for name in before},
            stamps,
        )

    def test_a_second_type_is_added_beside_the_first(self):
        """A run adds one entry and carries the entry it found over unchanged."""
        self.run_command()
        first = self.manifest()

        status, lines, _ = self.run_command(type=OTHER_TYPE_KEY)

        self.assertEqual(status, 0)
        self.assertEqual(lines, [
            MANIFEST_OK_LINE.format(entries=2),
            TYPE_OK_LINE.format(type=TYPE_KEY),
            TYPE_OK_LINE.format(type=OTHER_TYPE_KEY),
        ])
        manifest = self.manifest()
        self.assertEqual(
            sorted(manifest[SNAPSHOTS_KEY]), [TYPE_KEY, OTHER_TYPE_KEY])
        self.assertEqual(
            manifest[SNAPSHOTS_KEY][TYPE_KEY], first[SNAPSHOTS_KEY][TYPE_KEY])
        self.assertEqual(
            manifest[SNAPSHOTS_KEY][OTHER_TYPE_KEY],
            manifest_entry(OTHER_TYPE_KEY),
        )
        self.assertEqual(sorted(self.contents()), [
            RECORD_FILENAME, OTHER_TYPE_KEY + '.json', MANIFEST_FILENAME])

    def test_a_file_the_run_did_not_write_is_left_alone(self):
        """A reviewer's own file survives a rerun, a second type and a refusal."""
        self.run_command()
        self.write_candidate('notes.txt', b'a reviewer wrote this\n')
        with_note = self.contents()

        status, lines, _ = self.run_command()

        self.assertEqual(status, 0)
        self.assertEqual(lines, [
            MANIFEST_OK_LINE.format(entries=1),
            TYPE_OK_LINE.format(type=TYPE_KEY),
        ])
        self.assertEqual(self.contents(), with_note)

        status, _, _ = self.run_command(type=OTHER_TYPE_KEY)

        self.assertEqual(status, 0)
        self.assertEqual(
            sorted(self.contents()),
            sorted([RECORD_FILENAME, OTHER_TYPE_KEY + '.json',
                    MANIFEST_FILENAME, 'notes.txt']))
        self.assertEqual(
            self.contents()['notes.txt'], b'a reviewer wrote this\n')

        before_refusal = self.contents()

        status, lines, _ = self.run_command(fetched_at=OFFSET_FETCHED_AT_TEXT)

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            TYPE_REFUSED_LINE.format(
                type=TYPE_KEY, reason=REASON_ENTRY_CONFLICT),
        ])
        self.assertEqual(self.contents(), before_refusal)
        self.assertEqual(self.directory_names(), [])

    def test_a_changed_entry_is_refused_and_the_directory_is_untouched(self):
        """Preparing a replacement is not this command's decision to make."""
        self.run_command()
        before = self.contents()

        status, lines, message = self.run_command(
            fetched_at=OFFSET_FETCHED_AT_TEXT)

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.FAILURE_MESSAGE)
        self.assertEqual(lines, [
            TYPE_REFUSED_LINE.format(
                type=TYPE_KEY, reason=REASON_ENTRY_CONFLICT),
        ])
        self.assertEqual(self.contents(), before)

    def test_a_record_name_already_in_use_is_refused_rather_than_taken_over(self):
        """A file - or a directory - wearing the record's name is never adopted.

        The manifest is valid and names nothing, so nothing in the validator's
        report is about the stray name: what refuses it is this command's own
        collision check, reached after the directory was judged usable.
        """
        self.write_candidate(
            MANIFEST_FILENAME, manifest_bytes(empty_manifest()))
        self.write_candidate(RECORD_FILENAME, b'{}\n')
        before = self.contents()

        status, lines, message = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.FAILURE_MESSAGE)
        self.assertEqual(lines, [
            TYPE_REFUSED_LINE.format(
                type=TYPE_KEY, reason=prepare_v1_candidate.FILE_CONFLICT),
        ])
        self.assertEqual(self.contents(), before)
        self.assertEqual((self.out / RECORD_FILENAME).read_bytes(), b'{}\n')
        self.assertEqual(self.directory_names(), [])

        (self.out / RECORD_FILENAME).unlink()
        (self.out / RECORD_FILENAME).mkdir()
        before = self.contents()

        status, lines, _ = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            TYPE_REFUSED_LINE.format(
                type=TYPE_KEY, reason=prepare_v1_candidate.FILE_CONFLICT),
        ])
        self.assertTrue((self.out / RECORD_FILENAME).is_dir())
        self.assertEqual(self.contents(), before)
        self.assertEqual(self.directory_names(), [RECORD_FILENAME])

    def test_a_record_name_that_resolves_outside_the_directory_is_refused(self):
        """A link under the record's name would write outside ``--out``: refused."""
        self.out.mkdir()
        (self.out / MANIFEST_FILENAME).write_bytes(
            manifest_bytes(empty_manifest()))
        outside = self.base / 'outside.json'
        outside.write_bytes(b'{}\n')
        self.make_link(outside, self.out / RECORD_FILENAME, directory=False)
        manifest_before = (self.out / MANIFEST_FILENAME).read_bytes()

        status, lines, message = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.FAILURE_MESSAGE)
        self.assertEqual(lines, [
            prepare_v1_candidate.REFUSAL_LINE.format(
                reason=prepare_v1_candidate.OUT_ESCAPES),
        ])
        self.assertEqual(outside.read_bytes(), b'{}\n')
        self.assertEqual(
            (self.out / MANIFEST_FILENAME).read_bytes(), manifest_before)
        self.assertEqual(
            sorted(entry.name for entry in self.out.iterdir()),
            sorted([MANIFEST_FILENAME, RECORD_FILENAME]))
        self.assertEqual(self.directory_names(), [])

    def test_a_manifest_that_names_a_record_that_is_not_there_is_refused(self):
        """The directory's report is printed, and nothing is added to the directory."""
        self.write_candidate(MANIFEST_FILENAME, manifest_bytes({
            SCHEMA_VERSION_KEY: MANIFEST_SCHEMA_VERSION,
            SNAPSHOTS_KEY: {TYPE_KEY: manifest_entry()},
        }))
        before = self.contents()

        status, lines, message = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.FAILURE_MESSAGE)
        self.assertEqual(lines, validate_content_root(self.out).lines())
        self.assertIn(
            TYPE_REFUSED_LINE.format(
                type=TYPE_KEY, reason=jsoncontent_v1.REASON_RECORD_READ),
            lines,
        )
        self.assertEqual(self.contents(), before)

    # -- what a run refuses -------------------------------------------------

    def test_a_payload_that_is_not_one_object_is_refused_before_anything_is_written(self):
        """A document that is not a JSON object names no record, and creates nothing."""
        for text, reason in PAYLOAD_TEXT_REFUSALS:
            with self.subTest(text=text):
                self.payload_path.write_text(text, encoding='utf-8')

                status, lines, message = self.run_command()

                self.assertEqual(
                    status, prepare_v1_candidate.REFUSAL_RETURNCODE)
                self.assertEqual(message, prepare_v1_candidate.FAILURE_MESSAGE)
                self.assertEqual(lines, [
                    prepare_v1_candidate.REFUSAL_LINE.format(reason=reason)])
                self.assertFalse(self.out.exists())

    def test_a_mark_in_front_of_the_payload_is_read_and_never_written(self):
        """The document a Windows editor saved is prepared as the reviewer meant it."""
        self.payload_path.write_text(
            BYTE_ORDER_MARK + json.dumps(UNICODE_PAYLOAD), encoding='utf-8')

        status, _, _ = self.run_command()

        self.assertEqual(status, 0)
        record = (self.out / RECORD_FILENAME).read_bytes()
        self.assertNotIn(BYTE_ORDER_MARK.encode('utf-8'), record)
        self.assertEqual(record, record_file_bytes(UNICODE_PAYLOAD))
        self.assertEqual(
            self.manifest()[SNAPSHOTS_KEY][TYPE_KEY][SHA256_KEY],
            UNICODE_PAYLOAD_SHA256,
        )

    def test_an_unknown_type_key_is_refused_and_named_as_a_token(self):
        """A key the registry does not publish never becomes a file name."""
        for type_key in ('nope', 'bet_of_the_day.json', '../etc/passwd',
                         'Bet_of_the_day'):
            with self.subTest(type_key=type_key):
                status, lines, _ = self.run_command(type=type_key)

                self.assertEqual(
                    status, prepare_v1_candidate.REFUSAL_RETURNCODE)
                self.assertEqual(lines, [
                    TYPE_REFUSED_LINE.format(
                        type=safe_token(type_key), reason=REASON_UNKNOWN_TYPE),
                ])
                self.assertFalse(self.out.exists())

    def test_a_hostile_type_key_is_one_field_of_one_line(self):
        """A key a command line supplies cannot forge a second line or field."""
        status, lines, _ = self.run_command(type=HOSTILE_TYPE_KEY)

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            TYPE_REFUSED_LINE.format(
                type=HOSTILE_TYPE_TOKEN, reason=REASON_UNKNOWN_TYPE),
        ])

        long_key = 'k' * (MAX_TYPE_KEY_LENGTH + 20)
        _, lines, _ = self.run_command(type=long_key)

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('type='))
        self.assertNotIn(long_key, lines[0])
        self.assertLessEqual(
            len(lines[0].split(' ')[0]), MAX_TYPE_KEY_LENGTH + len('type='))

    def test_a_naive_instant_is_refused_and_an_offset_one_is_stored_as_supplied(self):
        """An instant names one instant; the text published is the author's own."""
        status, lines, _ = self.run_command(fetched_at=NAIVE_FETCHED_AT_TEXT)

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            prepare_v1_candidate.REFUSAL_LINE.format(reason=REASON_FETCHED_AT)])
        self.assertFalse(self.out.exists())

        status, _, _ = self.run_command(fetched_at=OFFSET_FETCHED_AT_TEXT)

        self.assertEqual(status, 0)
        self.assertEqual(
            self.manifest()[SNAPSHOTS_KEY][TYPE_KEY][RECORD_TIMESTAMP_KEY],
            OFFSET_FETCHED_AT_TEXT,
        )
        self.assertTrue(validate_content_root(self.out).ok)

    def test_an_empty_directory_is_prepared_and_one_with_content_is_not(self):
        """An empty directory is a candidate root; a manifest-less one is not."""
        self.out.mkdir()

        status, lines, _ = self.run_command()

        self.assertEqual(status, 0)
        self.assertEqual(lines, [
            MANIFEST_OK_LINE.format(entries=1),
            TYPE_OK_LINE.format(type=TYPE_KEY),
        ])

        stray = self.base / 'stray'
        stray.mkdir()
        (stray / 'notes.txt').write_text('x', encoding='utf-8')
        before = self.contents(stray)

        status, lines, _ = self.run_command(out=str(stray))

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            prepare_v1_candidate.REFUSAL_LINE.format(
                reason=prepare_v1_candidate.OUT_NOT_EMPTY),
        ])
        self.assertEqual(self.contents(stray), before)

    def test_a_stale_staging_directory_is_reported_rather_than_deleted(self):
        """A run deletes only what it created, so a leftover makes a root unusable."""
        self.out.mkdir()
        stale = self.out / (prepare_v1_candidate.STAGING_PREFIX + 'stale')
        stale.mkdir()
        (stale / RECORD_FILENAME).write_bytes(b'{}')
        before = self.contents()

        status, lines, _ = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            prepare_v1_candidate.REFUSAL_LINE.format(
                reason=prepare_v1_candidate.OUT_NOT_EMPTY),
        ])
        self.assertTrue(stale.is_dir())
        self.assertEqual(self.contents(), before)

    def test_a_manifest_that_does_not_validate_is_printed_before_the_refusal(self):
        """The directory's own report reaches the reviewer; nothing is replaced."""
        self.run_command()
        broken = self.manifest()
        broken[SNAPSHOTS_KEY][TYPE_KEY][SHA256_KEY] = '0' * 64
        (self.out / MANIFEST_FILENAME).write_bytes(manifest_bytes(broken))
        before = self.contents()
        self.assertFalse(validate_content_root(self.out).ok)

        status, lines, message = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.FAILURE_MESSAGE)
        self.assertEqual(lines, validate_content_root(self.out).lines())
        self.assertIn(
            TYPE_REFUSED_LINE.format(
                type=TYPE_KEY, reason=jsoncontent_v1.REASON_DIGEST),
            lines,
        )
        self.assertEqual(self.contents(), before)

    # -- what a run needs before it may start --------------------------------

    def test_every_option_is_required_and_none_is_defaulted(self):
        """Four options, four refusals with no report, and no directory created."""
        complete = (
            ('--payload', str(self.payload_path)),
            ('--type', TYPE_KEY),
            ('--fetched-at', FETCHED_AT_TEXT),
            ('--out', str(self.out)),
        )
        messages = {
            '--payload': prepare_v1_candidate.PAYLOAD_REQUIRED_MESSAGE,
            '--type': prepare_v1_candidate.TYPE_REQUIRED_MESSAGE,
            '--fetched-at': prepare_v1_candidate.FETCHED_AT_REQUIRED_MESSAGE,
            '--out': prepare_v1_candidate.OUT_REQUIRED_MESSAGE,
        }

        for missing, message in messages.items():
            with self.subTest(missing=missing):
                argv = [
                    item for pair in complete if pair[0] != missing
                    for item in pair
                ]
                status, lines, given = self.invoke(*argv)

                self.assertEqual(
                    status, prepare_v1_candidate.INVOCATION_RETURNCODE)
                self.assertEqual(given, message)
                self.assertEqual(lines, [])
                self.assertFalse(self.out.exists())

    def test_a_path_that_is_not_the_kind_of_path_it_has_to_be_is_refused(self):
        """A file where a directory belongs, a missing parent, a missing payload."""
        cases = (
            (
                {'out': str(self.payload_path)},
                prepare_v1_candidate.OUT_NOT_A_DIRECTORY_MESSAGE,
            ),
            (
                {'out': str(self.base / 'no' / 'such' / 'dir')},
                prepare_v1_candidate.OUT_PARENT_MISSING_MESSAGE,
            ),
            (
                {'payload': str(self.base / 'absent.json')},
                prepare_v1_candidate.PAYLOAD_UNREADABLE_MESSAGE,
            ),
        )

        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                status, lines, given = self.run_command(**overrides)

                self.assertEqual(
                    status, prepare_v1_candidate.INVOCATION_RETURNCODE)
                self.assertEqual(given, message)
                self.assertEqual(lines, [])
                self.assertFalse(self.out.exists())

    def test_the_packaged_content_directory_is_never_prepared(self):
        """The shipped root, its descendants and its ancestors are all refused."""
        shipped = CANONICAL_CONTENT_ROOT.resolve()
        before = self.contents(shipped)
        cases = (
            (shipped, prepare_v1_candidate.SHIPPED_ROOT),
            (shipped / 'candidate', prepare_v1_candidate.SHIPPED_ROOT),
            (shipped.parent, prepare_v1_candidate.OUT_CONTAINS_SHIPPED),
        )

        for path, token in cases:
            with self.subTest(path=str(path)):
                status, lines, given = self.run_command(out=str(path))

                self.assertEqual(
                    status, prepare_v1_candidate.INVOCATION_RETURNCODE)
                self.assertEqual(
                    given, prepare_v1_candidate.OUT_REFUSED_MESSAGE % token)
                self.assertEqual(lines, [])

        self.assertEqual(self.contents(shipped), before)
        self.assertFalse((shipped / 'candidate').exists())

    def test_the_packaged_root_is_refused_however_its_path_is_spelled(self):
        """A ``..`` alias is resolved before it is judged, so it is the shipped root.

        This one needs no platform capability, so it never skips: whatever the
        spelling an operator types, the directory a file would land in is the one
        that decides.
        """
        shipped = CANONICAL_CONTENT_ROOT.resolve()
        before = self.contents(shipped)

        status, lines, given = self.run_command(
            out=str(shipped / 'not-a-candidate' / '..'))

        self.assertEqual(status, prepare_v1_candidate.INVOCATION_RETURNCODE)
        self.assertEqual(
            given,
            prepare_v1_candidate.OUT_REFUSED_MESSAGE
            % prepare_v1_candidate.SHIPPED_ROOT)
        self.assertEqual(lines, [])
        self.assertEqual(self.contents(shipped), before)

    def test_the_packaged_root_is_refused_through_a_link(self):
        """A link that resolves to the shipped root is the shipped root.

        Windows only makes a symbolic link with a privilege the account may lack, so
        this asserts the same rule as the test above through a capability that may
        not be there; where it is not, the test is reported as skipped.
        """
        shipped = CANONICAL_CONTENT_ROOT.resolve()
        link = self.base / 'shipped-link'
        self.make_link(shipped, link)
        before = self.contents(shipped)

        for path in (link, link / 'candidate'):
            with self.subTest(path=str(path)):
                status, lines, given = self.run_command(out=str(path))

                self.assertEqual(
                    status, prepare_v1_candidate.INVOCATION_RETURNCODE)
                self.assertEqual(
                    given,
                    prepare_v1_candidate.OUT_REFUSED_MESSAGE
                    % prepare_v1_candidate.SHIPPED_ROOT)
                self.assertEqual(lines, [])

        self.assertEqual(self.contents(shipped), before)
        self.assertFalse((shipped / 'candidate').exists())

    # -- what a run does when the last step fails ---------------------------

    def test_a_manifest_that_cannot_be_replaced_takes_the_new_record_with_it(self):
        """Nothing was published, so the record a run wrote is taken back."""
        real_replace = os.replace

        def refuse_the_manifest(source, destination):
            if Path(destination).name == MANIFEST_FILENAME:
                raise OSError('the manifest could not be replaced')
            return real_replace(source, destination)

        with mock.patch('os.replace', side_effect=refuse_the_manifest):
            status, lines, _ = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            prepare_v1_candidate.REFUSAL_LINE.format(
                reason=prepare_v1_candidate.WRITE_FAILED),
        ])
        self.assertFalse(self.out.exists())

        status, _, _ = self.run_command()
        self.assertEqual(status, 0)
        self.assertTrue(validate_content_root(self.out).ok)

    def test_a_failed_replacement_over_a_finished_candidate_changes_nothing(self):
        """The manifest is last, so a run that cannot replace it leaves the review."""
        self.run_command()
        before = self.contents()
        real_replace = os.replace

        def refuse_the_manifest(source, destination):
            if Path(destination).name == MANIFEST_FILENAME:
                raise OSError('the manifest could not be replaced')
            return real_replace(source, destination)

        with mock.patch('os.replace', side_effect=refuse_the_manifest):
            status, lines, _ = self.run_command(type=OTHER_TYPE_KEY)

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            prepare_v1_candidate.REFUSAL_LINE.format(
                reason=prepare_v1_candidate.WRITE_FAILED),
        ])
        self.assertEqual(self.contents(), before)
        self.assertEqual(self.directory_names(), [])

    def test_a_staged_candidate_that_does_not_validate_is_refused_without_a_trace(self):
        """The staged proof is what stops it, and nothing reaches ``--out``.

        Two directories, because the two refusals differ in what is left behind: a
        directory that did not exist must not be created by a refused run, and one
        that existed empty must still be empty and hold no staging.
        """
        real_fill = prepare_v1_candidate.Command._fill_staging

        with mock.patch.object(
                prepare_v1_candidate.Command, '_fill_staging',
                broken_staged_fill(real_fill)):
            status, lines, message = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.FAILURE_MESSAGE)
        self.assertEqual(lines, [
            prepare_v1_candidate.REFUSAL_LINE.format(
                reason=prepare_v1_candidate.CANDIDATE_UNUSABLE),
        ])
        self.assertFalse(self.out.exists())

        self.out.mkdir()
        with mock.patch.object(
                prepare_v1_candidate.Command, '_fill_staging',
                broken_staged_fill(real_fill)):
            status, lines, _ = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        self.assertEqual(lines, [
            prepare_v1_candidate.REFUSAL_LINE.format(
                reason=prepare_v1_candidate.CANDIDATE_UNUSABLE),
        ])
        self.assertEqual(self.contents(), {})
        self.assertEqual(self.directory_names(), [])

    def test_a_record_that_cannot_be_taken_back_asks_for_inspection(self):
        """The record is installed and cannot be reclaimed: 3, and the file is there."""
        real_replace = os.replace

        def refuse_the_manifest(source, destination):
            if Path(destination).name == MANIFEST_FILENAME:
                raise OSError('the manifest could not be replaced')
            return real_replace(source, destination)

        def refuse_to_unlink(self):
            raise OSError('the record could not be removed')

        with mock.patch('os.replace', side_effect=refuse_the_manifest), \
                mock.patch.object(Path, 'unlink', refuse_to_unlink):
            status, lines, message = self.run_command()

        plan = plan_candidate(PAYLOAD, TYPE_KEY, FETCHED_AT_TEXT)
        self.assertEqual(status, prepare_v1_candidate.OPERATIONAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.INSPECT_MESSAGE)
        self.assertEqual(lines, [
            prepare_v1_candidate.INSPECT_LINE.format(
                reason=prepare_v1_candidate.WRITE_FAILED),
        ])
        self.assertEqual(
            self.contents(), {RECORD_FILENAME: plan.record_bytes})
        self.assertEqual(self.directory_names(), [])

    def test_staging_this_run_cannot_remove_asks_for_inspection(self):
        """The candidate is in place, the leftover staging is reported, status 3."""
        with mock.patch.object(
                prepare_v1_candidate.shutil, 'rmtree',
                side_effect=OSError('the staging directory is busy')):
            status, lines, message = self.run_command()

        plan = plan_candidate(PAYLOAD, TYPE_KEY, FETCHED_AT_TEXT)
        self.assertEqual(status, prepare_v1_candidate.OPERATIONAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.INSPECT_MESSAGE)
        self.assertEqual(lines, [
            MANIFEST_OK_LINE.format(entries=1),
            TYPE_OK_LINE.format(type=TYPE_KEY),
            prepare_v1_candidate.INSPECT_LINE.format(
                reason=prepare_v1_candidate.WRITE_FAILED),
        ])
        self.assertEqual(self.contents(), {
            RECORD_FILENAME: plan.record_bytes,
            MANIFEST_FILENAME: plan.manifest_bytes,
        })
        leftovers = self.directory_names()
        self.assertEqual(len(leftovers), 1)
        self.assertTrue(leftovers[0].startswith(
            prepare_v1_candidate.STAGING_PREFIX))

    def test_a_created_directory_that_cannot_be_removed_asks_for_inspection(self):
        """The root this run made is left behind empty, and the run says so."""
        real_fill = prepare_v1_candidate.Command._fill_staging

        def refuse_to_remove(self):
            raise OSError('the directory could not be removed')

        with mock.patch.object(
                prepare_v1_candidate.Command, '_fill_staging',
                broken_staged_fill(real_fill)), \
                mock.patch.object(Path, 'rmdir', refuse_to_remove):
            status, lines, message = self.run_command()

        self.assertEqual(status, prepare_v1_candidate.OPERATIONAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.INSPECT_MESSAGE)
        self.assertEqual(lines, [
            prepare_v1_candidate.INSPECT_LINE.format(
                reason=prepare_v1_candidate.CANDIDATE_UNUSABLE),
        ])
        self.assertTrue(self.out.is_dir())
        self.assertEqual(self.contents(), {})
        self.assertEqual(self.directory_names(), [])

    def test_a_directory_that_stops_validating_after_installation_asks_for_inspection(self):
        """The installed files stay, and no line claims the directory is unchanged."""
        real_validate = contentcheck_v1.validate_content_root

        def validate_a_directory_that_changed(root):
            path = Path(root)
            if not path.name.startswith(prepare_v1_candidate.STAGING_PREFIX):
                # Something outside this run rewrote the record after the staged
                # proof passed and before the final check ran.
                (path / RECORD_FILENAME).write_bytes(b'{}\n')
            return real_validate(root)

        with mock.patch.object(
                prepare_v1_candidate, 'validate_content_root',
                validate_a_directory_that_changed):
            status, lines, message = self.run_command()

        plan = plan_candidate(PAYLOAD, TYPE_KEY, FETCHED_AT_TEXT)
        self.assertEqual(status, prepare_v1_candidate.OPERATIONAL_RETURNCODE)
        self.assertEqual(message, prepare_v1_candidate.INSPECT_MESSAGE)
        self.assertIn(
            TYPE_REFUSED_LINE.format(
                type=TYPE_KEY, reason=jsoncontent_v1.REASON_DIGEST),
            lines)
        self.assertEqual(
            lines[-1],
            prepare_v1_candidate.INSPECT_LINE.format(
                reason=prepare_v1_candidate.CANDIDATE_UNUSABLE))
        self.assertNotEqual(
            status, prepare_v1_candidate.REFUSAL_RETURNCODE)
        # The installation really happened, so neither "refused" nor "unchanged"
        # may be reported: the manifest this run planned is the one in place.
        self.assertEqual(
            (self.out / MANIFEST_FILENAME).read_bytes(), plan.manifest_bytes)
        self.assertEqual(
            (self.out / RECORD_FILENAME).read_bytes(), b'{}\n')
        self.assertEqual(self.directory_names(), [])

    # -- the vocabulary ----------------------------------------------------

    def test_the_refusal_vocabularies_are_complete_and_disjoint(self):
        """Three sets of tokens, no overlap, and every one of them reportable."""
        invocation = prepare_v1_candidate.INVOCATION_REFUSALS
        directory = prepare_v1_candidate.DIRECTORY_REFUSALS

        self.assertEqual(len(invocation), 10)
        self.assertEqual(len(directory), 6)
        self.assertEqual(invocation & directory, frozenset())
        self.assertEqual(invocation & REASON_TOKENS, frozenset())
        self.assertEqual(directory & REASON_TOKENS, frozenset())
        self.assertEqual(
            prepare_v1_candidate.TYPE_SCOPED_REFUSALS,
            frozenset({REASON_UNKNOWN_TYPE, REASON_ENTRY_CONFLICT,
                       prepare_v1_candidate.FILE_CONFLICT}),
        )
        self.assertLessEqual(
            prepare_v1_candidate.TYPE_SCOPED_REFUSALS, directory | REASON_TOKENS)

        self.assertIs(prepare_v1_candidate.TYPE_REFUSED_LINE, TYPE_REFUSED_LINE)
        self.assertIs(prepare_v1_candidate.FILE_KEY, contentcheck_v1.FILE_KEY)
        self.assertEqual(
            prepare_v1_candidate.OUT_NOT_EMPTY, 'out_not_empty')
        self.assertEqual(
            prepare_v1_candidate.REFUSAL_LINE,
            'candidate status=refused reason={reason}',
        )
        self.assertEqual(
            prepare_v1_candidate.INSPECT_LINE,
            'candidate status=inspect reason={reason}',
        )
        self.assertEqual(
            (prepare_v1_candidate.REFUSAL_RETURNCODE,
             prepare_v1_candidate.INVOCATION_RETURNCODE,
             prepare_v1_candidate.OPERATIONAL_RETURNCODE),
            (1, 2, 3),
        )

        for token in invocation | directory:
            with self.subTest(token=token):
                self.assertRegex(token, r'\A[a-z][a-z0-9_]*\Z')

        named = (
            prepare_v1_candidate.OUT_REQUIRED_MESSAGE,
            prepare_v1_candidate.OUT_NOT_A_DIRECTORY_MESSAGE,
            prepare_v1_candidate.OUT_PARENT_MISSING_MESSAGE,
            prepare_v1_candidate.PAYLOAD_REQUIRED_MESSAGE,
            prepare_v1_candidate.PAYLOAD_UNREADABLE_MESSAGE,
            prepare_v1_candidate.TYPE_REQUIRED_MESSAGE,
            prepare_v1_candidate.FETCHED_AT_REQUIRED_MESSAGE,
        )
        for token in invocation - {
            prepare_v1_candidate.OUT_UNRESOLVABLE,
            prepare_v1_candidate.SHIPPED_ROOT,
            prepare_v1_candidate.OUT_CONTAINS_SHIPPED,
        }:
            with self.subTest(token=token):
                self.assertTrue(
                    any('reason=' + token in message for message in named),
                    token,
                )
        for token in (prepare_v1_candidate.OUT_UNRESOLVABLE,
                      prepare_v1_candidate.SHIPPED_ROOT,
                      prepare_v1_candidate.OUT_CONTAINS_SHIPPED):
            with self.subTest(token=token):
                self.assertIn(
                    'reason=' + token,
                    prepare_v1_candidate.OUT_REFUSED_MESSAGE % token,
                )


# ---------------------------------------------------------------------------
# What the two modules are allowed to reach for
# ---------------------------------------------------------------------------


class CandidateSourceTests(OfflineGuardMixin, SimpleTestCase):
    """Neither module fetches, configures or publishes to decide anything.

    A rules module that read a setting, and a command that reached a network, would
    both still pass the tests above - the tests hand them everything they need. What
    they import is therefore asserted directly: the language, this app's own
    siblings, and the framework base a management command cannot avoid.
    """

    def imported_names(self, module):
        """Return every module name ``module`` imports, relatives included."""
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    # ``from ... import candidate_v1``: the sibling is named by
                    # the alias alone, so the level on its own records nothing.
                    names.update(
                        '.' * node.level + alias.name for alias in node.names)
                else:
                    names.add('.' * node.level + node.module)
        return names

    def test_the_rules_module_imports_the_language_and_its_siblings_only(self):
        """``candidate_v1`` needs JSON and the reader's own constants, nothing more."""
        names = self.imported_names(candidate_v1)
        siblings = {name.lstrip('.') for name in names if name.startswith('.')}

        self.assertEqual(
            {name for name in names if not name.startswith('.')}, {'json'})
        self.assertIn('canonical_json_v1', siblings)
        self.assertIn('serializers_v1', siblings)

    def test_the_command_imports_its_framework_base_and_the_language_only(self):
        """No socket, no settings, no model, no fetcher: a command that only files."""
        allowed = {
            'json', 'os', 'shutil', 'tempfile', 'pathlib',
            'django.core.management.base',
        }
        names = self.imported_names(prepare_v1_candidate)
        outside = {name for name in names if not name.startswith('.')}
        siblings = {name.lstrip('.') for name in names if name.startswith('.')}

        self.assertEqual(outside - allowed, set())
        for sibling in ('candidate_v1', 'contentcheck_v1', 'jsoncontent_v1',
                        'reporting_v1'):
            with self.subTest(sibling=sibling):
                self.assertIn(sibling, siblings)
