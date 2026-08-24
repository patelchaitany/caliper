"""The domain model.

The type boundaries here encode the central architectural rule of this system:

    The model detects. Code judges.

`RawFinding` is the *only* thing Claude is allowed to produce. It carries no
score, no severity multiplier and no verdict — just a claim about a location,
with enough information for us to check whether the claim is true.
`Finding` is what survives grounding and quorum. `Review` is what the
deterministic rubric produces from those. There is no path through the type
system by which a number invented by the model reaches a user.
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, Field

from .hashing import digest


class Category(enum.StrEnum):
    """What kind of problem this is.

    Deliberately separated because they are not comparable: a correctness bug
    blocks a merge, a maintainability issue starts a conversation. Tools that
    flatten these into one "severity" column are why teams learn to ignore
    static analysis.
    """

    CORRECTNESS = "correctness"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    ARCHITECTURE = "architecture"


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Confidence(enum.StrEnum):
    """The model's own stated confidence, used only as a tie-breaker.

    Quorum across independent passes is the real confidence signal; this is
    self-reported and treated as such.
    """

    CERTAIN = "certain"
    LIKELY = "likely"
    POSSIBLE = "possible"


class SourceFile(BaseModel):
    path: str
    text: str
    language: str
    content_hash: str

    @property
    def loc(self) -> int:
        """Non-blank lines. Blank-line padding should not move a score."""
        return sum(1 for line in self.text.splitlines() if line.strip())

    def line_slice(self, start: int, end: int) -> str:
        """1-indexed, inclusive on both ends, clamped to the file."""
        lines = self.text.splitlines()
        lo = max(1, start) - 1
        hi = min(len(lines), max(start, end))
        return "\n".join(lines[lo:hi])


class Symbol(BaseModel):
    """A named, addressable region of code: function, class, method.

    Symbols — not line numbers — are how findings are anchored, remembered and
    compared across versions of a file.
    """

    name: str
    kind: Literal["function", "method", "class", "module"]
    path: str
    start_line: int
    end_line: int
    qualified_name: str
    exact: bool = Field(
        default=True,
        description="True if derived from a real parse, False if heuristic.",
    )


class RawFinding(BaseModel):
    """A claim from the model. Untrusted until grounded.

    Every field is something the model can observe; none of it is a judgement
    about how much the issue *counts*. Severity is a description of the failure
    mode, not a weight — the weight comes from the rubric.
    """

    rule: str = Field(description="Stable snake_case identifier, e.g. unvalidated_redirect.")
    category: Category
    severity: Severity
    confidence: Confidence
    path: str
    start_line: int
    end_line: int
    title: str
    explanation: str = Field(description="Why this is wrong, aimed at the author.")
    remediation: str
    quoted_source: str = Field(
        description="The exact source text at the claimed location, copied verbatim. "
        "This is the grounding handle: if it does not match the file, the "
        "finding is discarded."
    )


class Anchor(BaseModel):
    """A verified location. Only produced by the grounding pass."""

    path: str
    start_line: int
    end_line: int
    span_fingerprint: str
    symbol: str | None = None
    verified_by: Literal["exact_quote", "relocated_quote", "symbol_span"]


class Finding(BaseModel):
    """A grounded, quorum-confirmed finding. The unit the rubric scores."""

    fingerprint: str
    rule: str
    category: Category
    severity: Severity
    confidence: Confidence
    anchor: Anchor
    title: str
    explanation: str
    remediation: str

    votes: int = Field(description="Detection passes that independently found this.")
    passes: int = Field(description="Total detection passes run.")
    blast_radius: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Normalised structural importance of the code this sits in.",
    )
    dependents: int = Field(default=0, description="Modules transitively reaching this file.")
    recurrence: int = Field(
        default=0,
        description="Times this author has been told this before. 0 on first sight.",
    )
    convention: str | None = Field(
        default=None,
        description="Org convention id this violates, when sourced from review history.",
    )

    @property
    def agreement(self) -> float:
        return self.votes / self.passes if self.passes else 0.0


def fingerprint_finding(rule: str, qualified_symbol: str, span_fp: str) -> str:
    """Identity of a finding, independent of line numbers.

    Two findings are the same finding if the same rule fires on the same
    symbol over the same (whitespace-normalised) code. Adding an import at the
    top of the file shifts every line number and changes nothing here — which
    is what makes recurrence detection across submissions actually work.
    """
    return digest("finding", rule, qualified_symbol, span_fp)


class DimensionScore(BaseModel):
    category: Category
    score: float = Field(ge=0.0, le=100.0)
    penalty: float
    finding_count: int


class Score(BaseModel):
    """The output of a pure function. Never of a model."""

    value: float = Field(ge=0.0, le=100.0)
    band: str
    rubric_version: str
    rubric_hash: str
    dimensions: list[DimensionScore]
    total_penalty: float
    size_factor: float
    loc: int

    def explain(self) -> str:
        rows = "\n".join(
            f"  {d.category.value:<16} {d.score:6.1f}  "
            f"(-{d.penalty:.2f} from {d.finding_count} finding(s))"
            for d in self.dimensions
        )
        return (
            f"Caliper Score {self.value:.1f} ({self.band})\n"
            f"rubric {self.rubric_version} [{self.rubric_hash[:12]}]  "
            f"{self.loc} LOC  size factor {self.size_factor:.2f}\n{rows}"
        )


class Submission(BaseModel):
    submission_id: str
    author: str
    files: list[SourceFile]
    content_hash: str
    parent_id: str | None = None

    @property
    def loc(self) -> int:
        return sum(f.loc for f in self.files)


class Review(BaseModel):
    review_id: str
    submission_id: str
    author: str
    content_hash: str
    model: str
    model_pin: str
    passes: int
    quorum: int
    findings: list[Finding]
    score: Score
    dropped_ungrounded: int = 0
    dropped_below_quorum: int = 0
    cached: bool = False
    summary: str = ""

    def by_category(self, category: Category) -> list[Finding]:
        return [f for f in self.findings if f.category is category]
