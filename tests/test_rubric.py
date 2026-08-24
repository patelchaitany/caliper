"""The rubric must be a pure function. These tests are what makes the rating defensible."""

import itertools

import pytest
from conftest import finding

from caliper.models import Category, Confidence, Severity
from caliper.scoring.rubric import (
    DEFAULT_RUBRIC,
    Rubric,
    penalty_for,
    score_findings,
)


def test_score_is_independent_of_finding_order():
    findings = [
        finding(fingerprint=f"fp{i}", severity=s)
        for i, s in enumerate([Severity.CRITICAL, Severity.HIGH, Severity.LOW])
    ]
    values = {score_findings(list(p), 300).value for p in itertools.permutations(findings)}
    assert len(values) == 1, "float summation order leaked into the score"


def test_score_is_stable_across_repeated_calls():
    findings = [finding(fingerprint=f"fp{i}") for i in range(20)]
    assert len({score_findings(findings, 500).value for _ in range(50)}) == 1


def test_rubric_hash_changes_when_any_weight_changes():
    tweaked = Rubric(impact_gain=DEFAULT_RUBRIC.impact_gain + 0.01)
    assert tweaked.fingerprint() != DEFAULT_RUBRIC.fingerprint()


def test_rubric_hash_is_stable_for_identical_config():
    assert Rubric().fingerprint() == Rubric().fingerprint()


def test_blast_radius_raises_the_cost_of_an_identical_defect():
    """The problem this system exists to solve, expressed as a test."""
    hub = finding(fingerprint="a", blast_radius=1.0)
    throwaway = finding(fingerprint="b", blast_radius=0.0)
    assert penalty_for(hub) > penalty_for(throwaway)
    expected = 1.0 + DEFAULT_RUBRIC.impact_gain
    assert penalty_for(hub) / penalty_for(throwaway) == pytest.approx(expected)


def test_recurrence_escalates_but_is_capped():
    penalties = [penalty_for(finding(fingerprint="a", recurrence=n)) for n in range(8)]
    assert penalties == sorted(penalties), "being told again must never cost less"
    cap = DEFAULT_RUBRIC.recurrence_cap
    assert penalties[cap] == penalties[cap + 1] == penalties[-1], "escalation must be bounded"


def test_weak_agreement_costs_less_than_unanimous():
    unanimous = finding(fingerprint="a", votes=5, passes=5)
    split = finding(fingerprint="b", votes=3, passes=5)
    assert penalty_for(split) < penalty_for(unanimous)
    assert penalty_for(split) > 0, "a quorum finding must still count"


def test_info_severity_is_free():
    assert penalty_for(finding(severity=Severity.INFO)) == 0.0


def test_score_is_clamped_to_the_valid_range():
    catastrophic = [finding(fingerprint=f"fp{i}", severity=Severity.CRITICAL) for i in range(200)]
    assert score_findings(catastrophic, 50).value == 0.0
    assert score_findings([], 50).value == 100.0


def test_larger_submissions_are_judged_on_density_not_count():
    findings = [finding(fingerprint=f"fp{i}") for i in range(4)]
    assert score_findings(findings, 2000).value > score_findings(findings, 100).value


def test_size_factor_is_bounded():
    assert DEFAULT_RUBRIC.size_factor(1) == 1.0
    assert DEFAULT_RUBRIC.size_factor(10_000_000) == DEFAULT_RUBRIC.size_factor_max


def test_dimensions_cover_every_category_and_sum_to_the_total():
    findings = [
        finding(fingerprint=f"fp{i}", category=c, severity=Severity.MEDIUM)
        for i, c in enumerate(Category)
    ]
    score = score_findings(findings, 400)
    assert {d.category for d in score.dimensions} == set(Category)
    assert sum(d.penalty for d in score.dimensions) == pytest.approx(score.total_penalty, abs=1e-6)


def test_confidence_orders_penalties():
    certain = penalty_for(finding(fingerprint="a", confidence=Confidence.CERTAIN))
    possible = penalty_for(finding(fingerprint="b", confidence=Confidence.POSSIBLE))
    assert possible < certain


def test_band_boundaries_are_exact():
    assert DEFAULT_RUBRIC.band_for(90.0).startswith("A")
    assert DEFAULT_RUBRIC.band_for(89.99).startswith("B")
    assert DEFAULT_RUBRIC.band_for(0.0).startswith("F")
