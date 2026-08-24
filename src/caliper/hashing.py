"""Content addressing.

Every artefact Caliper reasons about is identified by the hash of its bytes,
never by a path or a line number. This is what makes a review citable: the
same bytes always resolve to the same review, and a finding that claims to be
at line 40 can be checked against the bytes that were actually there.

blake2b is used with a fixed digest size and no keying so that digests are
stable across processes, machines and Python versions. `hash()` is not: it is
salted per process and must never be used for anything persisted.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

DIGEST_BYTES = 16  # 128 bits -> 32 hex chars. Collision risk is negligible here.


def digest(*parts: str | bytes) -> str:
    """Stable hex digest over an ordered sequence of parts.

    Parts are length-prefixed so that ("ab", "c") and ("a", "bc") differ.
    """
    h = hashlib.blake2b(digest_size=DIGEST_BYTES)
    for part in parts:
        raw = part.encode("utf-8") if isinstance(part, str) else part
        h.update(len(raw).to_bytes(8, "big"))
        h.update(raw)
    return h.hexdigest()


def canonical_json(value: Any) -> str:
    """JSON with every source of ordering nondeterminism removed.

    Unsorted keys are the single most common silent cache invalidator, both for
    our own ledger and for Claude's prompt cache.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_object(value: Any) -> str:
    """Digest of any JSON-serialisable object, order-independent."""
    return digest(canonical_json(value))


def normalize_source(text: str) -> str:
    """Strip formatting-only variation before hashing a code span.

    Two spans that differ only in indentation width, trailing whitespace or
    line endings are the *same* span for fingerprinting purposes. This is what
    lets a finding survive a reformat without being re-reported as new.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    stripped = [line.strip() for line in lines]
    return "\n".join(line for line in stripped if line)


def span_fingerprint(text: str) -> str:
    """Digest of a code span, insensitive to whitespace-only churn."""
    return digest("span", normalize_source(text))
