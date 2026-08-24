"""The detector boundary.

Everything downstream of this interface is deterministic. Everything upstream
of it is a language model. Keeping that line sharp — and narrow — is what makes
the rest of the system testable: the entire probabilistic surface of Caliper is
one method that returns a list of `RawFinding`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import RawFinding, SourceFile

# Inlined rather than generated from Pydantic: strict mode requires
# `additionalProperties: false` and a complete `required` list at every level,
# and `$ref`/`$defs` indirection is not worth the risk here. This schema is
# part of the cached prompt prefix, so it must also be byte-stable.
FINDINGS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "description": "Every defect found. May be empty.",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "description": "Stable snake_case identifier for this defect "
                        "class, e.g. sql_injection, unchecked_error, n_plus_one_query.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "correctness",
                            "security",
                            "performance",
                            "maintainability",
                            "architecture",
                        ],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["certain", "likely", "possible"],
                    },
                    "path": {"type": "string", "description": "Exact path as given."},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "title": {"type": "string", "description": "One line, under 90 chars."},
                    "explanation": {"type": "string"},
                    "remediation": {"type": "string"},
                    "quoted_source": {
                        "type": "string",
                        "description": "The source lines verbatim, without line-number "
                        "gutters. Mechanically verified against the file.",
                    },
                    "convention": {
                        "type": "string",
                        "description": "Organisation convention id this violates, or "
                        "an empty string.",
                    },
                },
                "required": [
                    "rule",
                    "category",
                    "severity",
                    "confidence",
                    "path",
                    "start_line",
                    "end_line",
                    "title",
                    "explanation",
                    "remediation",
                    "quoted_source",
                    "convention",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __iadd__(self, other: Usage) -> Usage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        return self

    @property
    def cache_hit_rate(self) -> float:
        cached_total = self.cache_read_tokens + self.cache_write_tokens
        return self.cache_read_tokens / cached_total if cached_total else 0.0


@dataclass
class DetectionOutcome:
    """One pass. `conventions_applied` records which org rules actually fired."""

    raw_findings: list[RawFinding]
    usage: Usage = field(default_factory=Usage)
    model_reported: str = ""
    conventions_applied: list[str] = field(default_factory=list)


class Detector(Protocol):
    """The only probabilistic component in the system."""

    name: str
    model_pin: str

    def detect(
        self,
        files: list[SourceFile],
        pass_index: int,
        total_passes: int,
        conventions: str = "",
    ) -> DetectionOutcome: ...
