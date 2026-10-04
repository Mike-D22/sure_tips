"""The one canonical JSON byte form and digest rule for a v1 snapshot payload.

Why this module exists
----------------------
Two layers have to agree byte for byte about what "the digest of this payload"
means. ``storage_v1`` stamps every row it writes with a digest and refuses to
read a row back whose payload no longer matches it, and the read-only JSON
content provider validates the digest a manifest claims about a payload file.
While that rule lived in the storage module, a second caller either imported the
ORM stack to reach a pure string helper or copied the rule - and two copies of a
digest rule drift silently, because both digests still look like digests.

So the rule lives here on its own. ``storage_v1`` imports these names and
re-exports them unchanged, so ``from alltips_scraper.storage_v1 import
canonical_payload_sha256`` keeps working and that caller gets the identical
function object rather than a wrapper around it.

Ground rules
------------
* **Standard library only.** The whole module is ``hashlib`` and ``json``, and it
  imports nothing relative, so a layer that must not pull in a framework, a
  model, a settings module or a socket can import it directly.
* **One byte form.** Keys sorted, no whitespace around a separator, and every
  non-ASCII character escaped, encoded UTF-8. Two payloads that differ only in
  the order their keys were inserted digest identically, and no digest depends on
  the locale of the machine that computed it.
* **One algorithm, and one shape for its output.** SHA-256, rendered as lowercase
  hex of the fixed length ``DIGEST_HEX_LENGTH`` that the stored digest column
  holds. ``canonical_payload_sha256()`` and ``is_canonical_digest()`` are the two
  halves of the one rule, so the writer that produced a digest and a validator
  that accepts one cannot disagree about what a digest is.
* **Nothing is mutated and nothing is written.** The caller's payload is never
  modified, no file is opened, and the only bytes this module produces are the
  bytes it returns.

See ``storage_v1`` for the durable writer and reader that digest with this rule,
and ``jsoncontent_v1`` for the read-only provider that validates a manifest
against it.
"""

import hashlib
import json

# The digest algorithm, and the shape of the value it is rendered as.
# ``hashlib.new()`` is handed the name, so the algorithm is stated once, and the
# length and alphabet are stated once for every validator that reads a claim.
DIGEST_ALGORITHM = 'sha256'
DIGEST_HEX_LENGTH = 64
DIGEST_HEX_CHARACTERS = frozenset('0123456789abcdef')

# The canonical JSON form: keys sorted, no whitespace around a separator, and
# every non-ASCII character escaped.
CANONICAL_JSON_SEPARATORS = (',', ':')
CANONICAL_TEXT_ENCODING = 'utf-8'


def canonical_payload_bytes(payload):
    """Return the canonical byte form of ``payload``.

    The form is JSON with sorted keys, compact separators and every non-ASCII
    character escaped, encoded as UTF-8. It is the only form a digest is taken
    over, so a payload digests identically whatever order its keys were inserted
    in and wherever it is digested. A payload JSON cannot represent raises rather
    than being digested approximately: an approximate digest would let a
    rewritten payload keep the claim that was made about it.
    """
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=CANONICAL_JSON_SEPARATORS,
        ensure_ascii=True,
    )
    return text.encode(CANONICAL_TEXT_ENCODING)


def canonical_payload_sha256(payload):
    """Return the lowercase hex SHA-256 digest of the canonical byte form."""
    return hashlib.new(
        DIGEST_ALGORITHM, canonical_payload_bytes(payload)).hexdigest()


def is_canonical_digest(value):
    """Return whether ``value`` could be a digest of this module's rule.

    A digest that is not 64 lowercase hex characters cannot be the output of
    ``canonical_payload_sha256()``, so a claim of that shape is malformed rather
    than merely wrong, and the two cases are refused for different reasons.
    """
    return (
        isinstance(value, str)
        and len(value) == DIGEST_HEX_LENGTH
        and set(value) <= DIGEST_HEX_CHARACTERS
    )
