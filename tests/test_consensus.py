"""Quorum is what converts a sampler into a statistic."""

import random

from conftest import raw_finding

from caliper.analysis.consensus import consolidate, required_votes
from caliper.models import Anchor, Severity


def anchor(symbol="svc/auth.py::login", span="span0"):
    return Anchor(
        path="svc/auth.py",
        start_line=2,
        end_line=2,
        span_fingerprint=span,
        symbol=symbol,
        verified_by="exact_quote",
    )


def test_quorum_is_a_supermajority():
    assert required_votes(1) == 1
    assert required_votes(3) == 2
    assert required_votes(5) == 3
    assert required_votes(9) == 6


def test_unanimous_finding_is_admitted_with_full_votes():
    observations = [(raw_finding(), anchor()) for _ in range(5)]
    findings, dropped = consolidate(observations, passes=5)
    assert len(findings) == 1
    assert findings[0].votes == 5
    assert findings[0].agreement == 1.0
    assert dropped == 0


def test_single_pass_noise_is_rejected():
    observations = [(raw_finding(), anchor()) for _ in range(5)]
    observations.append((raw_finding(rule="flaky"), anchor(span="span9")))
    findings, dropped = consolidate(observations, passes=5)
    assert [f.rule for f in findings] == ["sql_injection"]
    assert dropped == 1


def test_output_is_identical_regardless_of_pass_completion_order():
    observations = [(raw_finding(), anchor()) for _ in range(4)]
    observations += [
        (raw_finding(rule="other", severity=Severity.LOW), anchor(span="s2")) for _ in range(3)
    ]

    def signature(items):
        found, _ = consolidate(list(items), passes=5)
        return tuple((f.fingerprint, f.severity.value, f.votes, f.title) for f in found)

    baseline = signature(observations)
    rng = random.Random(0)
    for _ in range(100):
        shuffled = list(observations)
        rng.shuffle(shuffled)
        assert signature(shuffled) == baseline


def test_line_numbers_do_not_affect_identity():
    """The same defect after an unrelated edit above it is not a new defect."""
    observations = [
        (raw_finding(start_line=2), anchor()),
        (raw_finding(start_line=48), anchor()),
        (raw_finding(start_line=99), anchor()),
    ]
    findings, _ = consolidate(observations, passes=3, quorum=2)
    assert len(findings) == 1
    assert findings[0].votes == 3


def test_disagreement_on_severity_resolves_to_the_milder_reading():
    observations = [
        (raw_finding(severity=Severity.CRITICAL), anchor()),
        (raw_finding(severity=Severity.HIGH), anchor()),
        (raw_finding(severity=Severity.HIGH), anchor()),
        (raw_finding(severity=Severity.CRITICAL), anchor()),
    ]
    findings, _ = consolidate(observations, passes=4, quorum=2)
    assert findings[0].severity is Severity.HIGH


def test_findings_are_sorted_most_severe_first():
    observations = []
    for severity in (Severity.LOW, Severity.CRITICAL, Severity.MEDIUM):
        for _ in range(3):
            observations.append(
                (raw_finding(rule=severity.value, severity=severity), anchor(span=severity.value))
            )
    findings, _ = consolidate(observations, passes=3, quorum=2)
    assert [f.severity for f in findings] == [Severity.CRITICAL, Severity.MEDIUM, Severity.LOW]
