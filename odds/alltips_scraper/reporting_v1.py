"""The report-token rule that both the writer and the reviewer print with.

Why this module exists
----------------------
Two modules print a line per tip type, and neither may print the key it holds
as it stands. ``refresh_v1`` reports what a run did with the key a command
line handed it, and ``contentcheck_v1`` reports what a candidate manifest a
reviewer wrote claims. Either key came from outside, so either one can hold
a newline that forges a second line, a space that makes a reader see two
fields, or an ``=`` that invents one. Both lines are read by a person and
parsed by a script, so both fit the key into a token the same way: one
substitution, one width, one replacement character.

While that rule lived in the writer, the reviewer had two bad choices - import
the writer, and with it the fetch layer, the store seam and the serializer, to
reach a pure string helper; or copy the rule, and let the two copies drift
silently, because both still look like a token. This module is the rule in
one place, and it is there for the same reason the digest rule lives in
``canonical_json_v1`` rather than in the store that first needed it.

Ground rules
------------
* **Nothing but the language itself.** This module imports no module at all,
  the rule is reachable wherever a release can run: no framework, no store,
  no network client and no clock is in the picture.
* **One token, whatever it is handed.** Every character outside letters,
  digits and ``._-`` becomes ``?``, the result is cut to the width a stored
  type key is bounded by, and a value with nothing safe left in it becomes
  ``?`` as well, so a line never loses the field the token stands in for.
* **No side effect, and no refusal of a value.** The rule reads what it was
  handed, builds a string and returns it: nothing is written anywhere, and
  nothing is logged, and a value that is not text is described, not rejected.
* **The rule is stated once.** ``refresh_v1`` and ``contentcheck_v1`` import
  these names instead of redeclaring them, so a change to the safe set or the
  width reaches every line that carries a token.

See ``refresh_v1`` for the run report, ``contentcheck_v1`` for the candidate
review whose lines these tokens appear in.
"""


# A reported type key is one token: the storage column's own width bounds it, and
# anything outside the safe set (a newline, a space, an ``=``) becomes ``?``, so a
# key can never terminate a line or forge a field of its own.
MAX_TYPE_KEY_LENGTH = 64
SAFE_TOKEN_EXTRA_CHARACTERS = frozenset('._-')
REPLACEMENT_CHARACTER = '?'


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def safe_token(value):
    """Return ``value`` as one printable token, fitted for a report line.

    A type key normally comes from the versioned registry, but a command line
    hands one in, so the key that reaches a report or a log line is sanitised
    first: every character outside letters, digits and ``._-`` becomes ``?``, and
    the result is truncated to the stored key's own width. A key therefore cannot
    contain a newline that forges a second report line, a space that makes a
    reader see two fields, or an ``=`` that invents one. A value with nothing safe
    left in it becomes ``?``, so a line never loses the ``type=`` field itself.
    """
    text = value if isinstance(value, str) else str(value)
    token = ''.join(
        character if (character.isalnum() or character in SAFE_TOKEN_EXTRA_CHARACTERS)
        else REPLACEMENT_CHARACTER
        for character in text
    )[:MAX_TYPE_KEY_LENGTH]
    return token or REPLACEMENT_CHARACTER
