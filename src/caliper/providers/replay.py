"""A stand-in detector for tests, CI and offline demos.

This is not the product. It is a small pattern matcher that produces
`RawFinding` objects of the same shape Claude produces, so that the parts of
Caliper that matter — grounding, quorum, impact, the rubric, the ledger — can
be exercised without credentials, without network and without cost.

It also models the thing this whole architecture exists to handle: **a
detector that does not return the same set twice.** Each candidate carries a
`stability` value, and each pass independently drops candidates below a
per-pass draw. Set `nonce` to a fixed value and the whole ensemble is
reproducible; vary it and you get the run-to-run flicker of a real model,
which is what `caliper verify` measures.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from ..models import Category, Confidence, RawFinding, Severity, SourceFile
from .base import DetectionOutcome, Usage


@dataclass(frozen=True)
class Pattern:
    rule: str
    regex: str
    category: Category
    severity: Severity
    confidence: Confidence
    title: str
    explanation: str
    remediation: str
    # How reliably a real detector would surface this. Blatant defects are
    # found every time; subtle ones flicker — which is exactly why a single
    # pass cannot be a rating.
    stability: float = 1.0
    languages: tuple[str, ...] = ()


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        rule="hardcoded_credential",
        regex=r"""(?i)\w*(password|passwd|secret|api_?key|access_?token)\w*\s*[:=]\s*["'][^"']{6,}["']""",
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        title="Credential committed to source",
        explanation=(
            "A live secret is embedded in the source. Anyone with repository "
            "read access has it, and rotating it means a code change and a "
            "deploy rather than a config update."
        ),
        remediation=(
            "Read the value from the environment or a secret manager, and "
            "rotate the exposed credential."
        ),
    ),
    Pattern(
        rule="sql_string_concatenation",
        regex=r"""(?i)(SELECT|INSERT|UPDATE|DELETE)\b[^;\n]*?["']\s*[+%]\s*\w|f["'](?i:SELECT|INSERT|UPDATE|DELETE)\b[^"']*\{""",
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        title="SQL built by string concatenation",
        explanation=(
            "User-controlled text is interpolated directly into a SQL "
            "statement, so any value containing a quote changes the shape of "
            "the query rather than its data."
        ),
        remediation="Use a parameterised query and pass the value as a bound parameter.",
    ),
    Pattern(
        rule="shell_injection",
        regex=r"""os\.system\(|subprocess\.\w+\([^)]*shell\s*=\s*True""",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        title="Shell invoked with an interpolated command",
        explanation=(
            "The command string is handed to a shell, so metacharacters in any "
            "interpolated value are interpreted rather than passed through."
        ),
        remediation="Pass an argument list without shell=True.",
        stability=0.9,
    ),
    Pattern(
        rule="dynamic_code_execution",
        regex=r"""(?<![\w.])(eval|exec)\s*\(""",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        title="Dynamic code execution",
        explanation="Input reaching this call is executed as code, not read as data.",
        remediation="Parse the value explicitly, or dispatch through an allow-list.",
        stability=0.85,
    ),
    Pattern(
        rule="swallowed_exception",
        regex=r"""except\s*(?:\w+\s*)?:\s*(?:#.*)?$""",
        category=Category.CORRECTNESS,
        severity=Severity.MEDIUM,
        confidence=Confidence.LIKELY,
        title="Exception caught without handling",
        explanation=(
            "The failure is discarded, so the caller sees success and the "
            "operator sees nothing. The next bug in this path will be invisible."
        ),
        remediation="Catch the specific exception and either log it with context or re-raise.",
        stability=0.75,
        languages=("python",),
    ),
    Pattern(
        rule="request_without_timeout",
        regex=r"""requests\.(get|post|put|delete)\((?![^)]*timeout)""",
        category=Category.CORRECTNESS,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        title="Outbound request has no timeout",
        explanation=(
            "With no timeout this call blocks indefinitely if the peer stops "
            "responding, holding its worker and eventually the whole pool."
        ),
        remediation="Pass an explicit timeout.",
        stability=0.8,
    ),
    Pattern(
        rule="query_in_loop",
        regex=r"""for\s+\w+\s+in\s+.*:\s*$""",
        category=Category.PERFORMANCE,
        severity=Severity.MEDIUM,
        confidence=Confidence.POSSIBLE,
        title="Possible per-iteration query",
        explanation=(
            "A database call inside this loop turns one request into one query "
            "per row, which degrades linearly with data size."
        ),
        remediation="Fetch the set in one query before the loop.",
        stability=0.45,  # genuinely uncertain — should often fail quorum
        languages=("python",),
    ),
    Pattern(
        rule="ignored_error_return",
        regex=r"""(?m)^\s*(?:_|\w+),\s*_\s*(?::=|=)""",
        category=Category.CORRECTNESS,
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        title="Error return discarded",
        explanation=(
            "The error value is assigned to _, so a failed call proceeds as if it succeeded."
        ),
        remediation="Check the error and return or wrap it.",
        stability=0.85,
        languages=("go",),
    ),
    Pattern(
        rule="mutable_default_argument",
        regex=r"""def\s+\w+\([^)]*=\s*(\[\]|\{\})""",
        category=Category.CORRECTNESS,
        severity=Severity.MEDIUM,
        confidence=Confidence.CERTAIN,
        title="Mutable default argument",
        explanation=(
            "The default is created once at definition time and shared by every "
            "call, so mutations leak between unrelated invocations."
        ),
        remediation="Default to None and construct the container inside the function.",
        stability=0.9,
        languages=("python",),
    ),
)


class ReplayDetector:
    """Deterministic, offline, and deliberately non-repeatable across nonces."""

    def __init__(self, *, seed: str = "", nonce: str = "", stability_floor: float = 0.0):
        self.seed = seed
        self.nonce = nonce
        self.stability_floor = stability_floor
        self.name = "replay"
        self.model_pin = f"replay@local:{nonce or 'fixed'}"

    def detect(
        self,
        files: list[SourceFile],
        pass_index: int,
        total_passes: int,
        conventions: str = "",
    ) -> DetectionOutcome:
        rng = random.Random(f"{self.seed}|{self.nonce}|{pass_index}")
        findings: list[RawFinding] = []

        for file in files:
            for pattern in PATTERNS:
                if pattern.languages and file.language not in pattern.languages:
                    continue
                for line_no, line in enumerate(file.text.splitlines(), start=1):
                    if not re.search(pattern.regex, line):
                        continue
                    stability = max(pattern.stability, self.stability_floor)
                    if rng.random() > stability:
                        continue  # this pass simply did not see it
                    findings.append(
                        RawFinding(
                            rule=pattern.rule,
                            category=pattern.category,
                            severity=pattern.severity,
                            confidence=pattern.confidence,
                            path=file.path,
                            start_line=line_no,
                            end_line=line_no,
                            title=pattern.title,
                            explanation=pattern.explanation,
                            remediation=pattern.remediation,
                            quoted_source=line,
                        )
                    )

        return DetectionOutcome(
            raw_findings=findings,
            usage=Usage(input_tokens=sum(len(f.text) // 4 for f in files)),
            model_reported="replay",
        )
