"""The rubric: a pure function from findings to a number.

This module is the reason Caliper can claim a reproducible rating. Nothing in
here can call a model, read a clock, hit a network or consult a random source.
Given the same findings and the same rubric version, it returns the same score
on any machine, forever — and because the rubric is versioned and hashed, a
score from last month is still interpretable today, and can be *replayed*
under a new rubric to see what changed.

Two details that look pedantic and are not:

  * Findings are summed in fingerprint order. IEEE-754 addition is not
    associative, so an unordered sum can differ in the last bit between runs.
  * Every intermediate is rounded only at the end, once, at a fixed precision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..hashing import hash_object
from ..models import Category, DimensionScore, Finding, Score, Severity

RUBRIC_VERSION = "1.0.0"


@dataclass(frozen=True)
class Rubric:
    """Every constant that can move a score, in one auditable place."""

    version: str = RUBRIC_VERSION

    # How much a defect of each severity costs, before any modifier.
    severity_weight: dict[str, float] = field(
        default_factory=lambda: {
            Severity.CRITICAL.value: 24.0,
            Severity.HIGH.value: 11.0,
            Severity.MEDIUM.value: 4.5,
            Severity.LOW.value: 1.5,
            Severity.INFO.value: 0.0,
        }
    )

    # Categories are not equally load-bearing. A security defect costs more than
    # a naming inconsistency, and saying so explicitly beats burying it in
    # per-rule severities that drift.
    category_weight: dict[str, float] = field(
        default_factory=lambda: {
            Category.SECURITY.value: 1.15,
            Category.CORRECTNESS.value: 1.0,
            Category.ARCHITECTURE.value: 0.8,
            Category.PERFORMANCE.value: 0.7,
            Category.MAINTAINABILITY.value: 0.5,
        }
    )

    # Blast radius multiplier: 1.0 for an isolated file, up to 1 + gain for code
    # the whole system depends on. This is the context-awareness that isolated
    # per-file analysis cannot express.
    impact_gain: float = 0.75

    # A finding found by 3 of 5 independent passes should not cost the same as
    # one found by 5 of 5, but it should still cost something.
    agreement_floor: float = 0.55

    # The model's self-reported confidence, used only as a mild tie-breaker.
    confidence_weight: dict[str, float] = field(
        default_factory=lambda: {"certain": 1.0, "likely": 0.9, "possible": 0.75}
    )

    # Being told the same thing for the fifth time is worse than the first.
    recurrence_gain: float = 0.15
    recurrence_cap: int = 4

    # Defect *density* is the fair measure; a 2000-line submission should not be
    # doomed for having more findings than a 50-line one. Sub-linear so large
    # submissions cannot dilute real problems away.
    baseline_loc: int = 200
    size_exponent: float = 0.5
    size_factor_max: float = 4.0

    bands: tuple[tuple[float, str], ...] = (
        (90.0, "A — ship"),
        (75.0, "B — minor revisions"),
        (60.0, "C — revisions required"),
        (40.0, "D — significant rework"),
        (0.0, "F — blocked"),
    )

    def fingerprint(self) -> str:
        """Hash of every weight, so a score can prove which rubric made it."""
        return hash_object(
            {
                "version": self.version,
                "severity_weight": self.severity_weight,
                "category_weight": self.category_weight,
                "impact_gain": self.impact_gain,
                "agreement_floor": self.agreement_floor,
                "confidence_weight": self.confidence_weight,
                "recurrence_gain": self.recurrence_gain,
                "recurrence_cap": self.recurrence_cap,
                "baseline_loc": self.baseline_loc,
                "size_exponent": self.size_exponent,
                "size_factor_max": self.size_factor_max,
                "bands": [list(b) for b in self.bands],
            }
        )

    def band_for(self, value: float) -> str:
        for threshold, label in self.bands:
            if value >= threshold:
                return label
        return self.bands[-1][1]

    def size_factor(self, loc: int) -> float:
        raw = (max(loc, 1) / self.baseline_loc) ** self.size_exponent
        return min(max(raw, 1.0), self.size_factor_max)


DEFAULT_RUBRIC = Rubric()


def penalty_for(finding: Finding, rubric: Rubric = DEFAULT_RUBRIC) -> float:
    """What one finding costs. Every factor is inspectable and bounded."""
    base = rubric.severity_weight.get(finding.severity.value, 0.0)
    if base == 0.0:
        return 0.0

    category = rubric.category_weight.get(finding.category.value, 1.0)
    impact = 1.0 + rubric.impact_gain * finding.blast_radius
    agreement = rubric.agreement_floor + (1.0 - rubric.agreement_floor) * finding.agreement
    confidence = rubric.confidence_weight.get(finding.confidence.value, 1.0)
    recurrence = 1.0 + rubric.recurrence_gain * min(finding.recurrence, rubric.recurrence_cap)

    return base * category * impact * agreement * confidence * recurrence


def explain_penalty(finding: Finding, rubric: Rubric = DEFAULT_RUBRIC) -> dict[str, float]:
    """The factor breakdown, so a developer can audit any number we show them."""
    return {
        "severity_weight": rubric.severity_weight.get(finding.severity.value, 0.0),
        "category_weight": rubric.category_weight.get(finding.category.value, 1.0),
        "impact_multiplier": 1.0 + rubric.impact_gain * finding.blast_radius,
        "agreement_multiplier": (
            rubric.agreement_floor + (1.0 - rubric.agreement_floor) * finding.agreement
        ),
        "confidence_multiplier": rubric.confidence_weight.get(finding.confidence.value, 1.0),
        "recurrence_multiplier": (
            1.0 + rubric.recurrence_gain * min(finding.recurrence, rubric.recurrence_cap)
        ),
        "total": penalty_for(finding, rubric),
    }


def score_findings(
    findings: list[Finding],
    loc: int,
    rubric: Rubric = DEFAULT_RUBRIC,
) -> Score:
    """Reduce a grounded finding set to a rating. Pure, total, deterministic."""
    # Fixed summation order — see the module docstring.
    ordered = sorted(findings, key=lambda f: f.fingerprint)
    size_factor = rubric.size_factor(loc)

    per_category: dict[Category, list[float]] = {category: [] for category in Category}
    for finding in ordered:
        per_category[finding.category].append(penalty_for(finding, rubric))

    dimensions: list[DimensionScore] = []
    total_penalty = 0.0
    for category in Category:  # Enum order is declaration order: stable.
        penalties = per_category[category]
        raw = sum(penalties)
        total_penalty += raw
        dimensions.append(
            DimensionScore(
                category=category,
                score=round(_clamp(100.0 - raw / size_factor), 2),
                penalty=round(raw, 4),
                finding_count=len(penalties),
            )
        )

    value = round(_clamp(100.0 - total_penalty / size_factor), 2)
    return Score(
        value=value,
        band=rubric.band_for(value),
        rubric_version=rubric.version,
        rubric_hash=rubric.fingerprint(),
        dimensions=dimensions,
        total_penalty=round(total_penalty, 4),
        size_factor=round(size_factor, 4),
        loc=loc,
    )


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))
