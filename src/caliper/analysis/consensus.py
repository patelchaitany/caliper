"""Quorum across independent detection passes.

A single model pass is a sample, not a verdict. Run the same submission
several times and you get overlapping but non-identical finding sets — which
is precisely the property that disqualifies a raw model from acting as a
rating authority.

Consensus turns that sample into a statistic. Findings are clustered by a
fingerprint that ignores line numbers and whitespace, and only those that
independently surface in at least `quorum` passes are admitted. Everything
that survives carries its vote count, which the rubric then uses to weight it:
a 5-of-5 finding costs more than a 3-of-5 one.

Every tie-break in this module is total and deterministic. Where two candidates
are equally supported, the *less severe* reading wins — a rating authority
should not resolve its own uncertainty against the author.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from ..hashing import digest
from ..models import (
    Anchor,
    Confidence,
    Finding,
    RawFinding,
    Severity,
    fingerprint_finding,
)

# Ascending harm. Used for "least severe wins" tie-breaks.
_SEVERITY_ORDER = [
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
]
_CONFIDENCE_ORDER = [Confidence.POSSIBLE, Confidence.LIKELY, Confidence.CERTAIN]

# Trust ranking for anchors, best first.
_ANCHOR_TRUST = {"exact_quote": 0, "relocated_quote": 1, "symbol_span": 2}


def required_votes(passes: int, ratio: float = 0.6) -> int:
    """Votes needed to admit a finding.

    A single pass trivially admits everything (ratio is meaningless with one
    sample); beyond that, a strict majority of `ratio` is required.
    """
    if passes <= 1:
        return 1
    return max(2, math.ceil(passes * ratio))


def _modal_least(values: list, order: list):
    """Most common value; ties broken toward the *earlier* entry in `order`."""
    counts = Counter(values)
    top = max(counts.values())
    tied = [value for value in values if counts[value] == top]
    return min(tied, key=order.index)


def cluster(
    anchored: list[tuple[RawFinding, Anchor]],
) -> dict[str, list[tuple[RawFinding, Anchor]]]:
    """Group observations of the same defect across passes.

    Identity is (rule, owning symbol, whitespace-normalised span) — never a
    line number. A finding survives reformatting and unrelated edits above it,
    which is what makes cross-submission recurrence tracking possible.
    """
    groups: dict[str, list[tuple[RawFinding, Anchor]]] = defaultdict(list)
    for raw, anchor in anchored:
        key = fingerprint_finding(
            raw.rule,
            anchor.symbol or f"{anchor.path}::<module>",
            anchor.span_fingerprint,
        )
        groups[key].append((raw, anchor))
    return dict(groups)


def _pick_anchor(observations: list[tuple[RawFinding, Anchor]]) -> Anchor:
    return min(
        (anchor for _, anchor in observations),
        key=lambda a: (_ANCHOR_TRUST.get(a.verified_by, 9), a.start_line, a.end_line),
    )


def _pick_prose(observations: list[tuple[RawFinding, Anchor]]) -> RawFinding:
    """Choose whose wording to show.

    Prefer the most confident observation; break ties on a hash of the text so
    the choice is arbitrary but fixed, rather than dependent on pass ordering.
    """
    return max(
        (raw for raw, _ in observations),
        key=lambda r: (
            _CONFIDENCE_ORDER.index(r.confidence),
            len(r.explanation),
            digest(r.title, r.explanation),
        ),
    )


def consolidate(
    anchored: list[tuple[RawFinding, Anchor]],
    passes: int,
    quorum: int | None = None,
) -> tuple[list[Finding], int]:
    """Collapse observations into confirmed findings.

    Returns the admitted findings (sorted deterministically) and the number of
    distinct candidates rejected for insufficient support.
    """
    needed = quorum if quorum is not None else required_votes(passes)
    groups = cluster(anchored)

    admitted: list[Finding] = []
    dropped = 0

    for key in sorted(groups):  # sorted: fixed iteration order
        observations = groups[key]
        # One pass may report the same defect twice; a pass gets one vote.
        votes = len({id(raw) for raw, _ in observations})
        votes = min(votes, passes)
        if votes < needed:
            dropped += 1
            continue

        prose = _pick_prose(observations)
        admitted.append(
            Finding(
                fingerprint=key,
                rule=prose.rule,
                category=_modal_least(
                    [raw.category for raw, _ in observations], list(type(prose.category))
                ),
                severity=_modal_least([raw.severity for raw, _ in observations], _SEVERITY_ORDER),
                confidence=_modal_least(
                    [raw.confidence for raw, _ in observations], _CONFIDENCE_ORDER
                ),
                anchor=_pick_anchor(observations),
                title=prose.title,
                explanation=prose.explanation,
                remediation=prose.remediation,
                votes=votes,
                passes=passes,
            )
        )

    admitted.sort(
        key=lambda f: (
            -_SEVERITY_ORDER.index(f.severity),
            -f.votes,
            f.anchor.path,
            f.anchor.start_line,
            f.fingerprint,
        )
    )
    return admitted, dropped
