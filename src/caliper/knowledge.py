"""Turning an organisation's review history into rules the reviewer applies.

Conventions learned the hard way live scattered across old pull request
comments, style guides and chat threads, and are essentially never reapplied
to new code. Nobody rereads four years of review comments before approving a
diff.

This module does. It reads past review comments, distils the ones that recur
into explicit conventions, and stores them in the ledger — from where they are
injected into the *cached* half of the detection prompt, so applying an
organisation's entire accumulated standard to every future review costs
approximately nothing per request.

The extraction runs once per ingest, not once per review, and its output is
plain text in a table a human can read, edit and delete from. An institutional
standard that cannot be audited is not a standard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .store.ledger import Ledger

CONVENTIONS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "conventions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "convention_id": {
                        "type": "string",
                        "description": "Stable snake_case id, e.g. errors_wrap_with_context.",
                    },
                    "statement": {
                        "type": "string",
                        "description": "The rule, imperative, one sentence. What a "
                        "reviewer should check for.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this team holds it, from the evidence.",
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
                    "evidence_count": {
                        "type": "integer",
                        "description": "How many distinct comments support this.",
                    },
                },
                "required": [
                    "convention_id",
                    "statement",
                    "rationale",
                    "category",
                    "evidence_count",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["conventions"],
    "additionalProperties": False,
}

EXTRACTOR_SYSTEM = """\
You are extracting an engineering organisation's review conventions from its \
own history of code review comments.

You will be given review comments written by that team's engineers. Find the \
standards they are actually enforcing, and state each one as a rule a reviewer \
could check.

Include a convention only when several independent comments point at the same \
underlying expectation. One person's passing remark is not a standard; the \
same objection raised by different reviewers across different pull requests \
is. Prefer the specific over the generic — "wrap errors with the operation \
name before returning" is usable, "write clean code" is not.

Exclude anything a formatter or linter already enforces mechanically, anything \
that is purely about this one change rather than a general expectation, and \
anything that is a question rather than a standard.

Where the team's convention contradicts common practice elsewhere, keep the \
team's version and say so in the rationale. That divergence is the entire \
value of this exercise."""


@dataclass
class Convention:
    convention_id: str
    statement: str
    rationale: str
    category: str
    evidence_count: int


def load_comments(path: str) -> list[dict[str, Any]]:
    """Read review comments from JSONL or a JSON array.

    Expected fields per record, all optional except `body`:
    `body`, `author`, `path`, `repo`, `pr`.
    """
    text = open(path, encoding="utf-8").read().strip()
    if not text:
        return []
    if text.startswith("["):
        records = json.loads(text)
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [r for r in records if isinstance(r, dict) and r.get("body", "").strip()]


def render_corpus(records: list[dict[str, Any]], limit: int = 400) -> str:
    """Format comments for the extractor, newest-agnostic and stable."""
    chunks = []
    for index, record in enumerate(records[:limit], start=1):
        location = record.get("path") or "unknown file"
        pull = record.get("pr", "")
        header = f"[{index}] on {location}" + (f" (PR {pull})" if pull else "")
        chunks.append(f"{header}\n{record['body'].strip()}")
    return "\n\n---\n\n".join(chunks)


def extract_conventions(
    client: Any,
    records: list[dict[str, Any]],
    *,
    model: str = "claude-opus-5",
    max_tokens: int = 16000,
    effort: str = "high",
) -> list[Convention]:
    """One model call over the whole corpus. Not on any review's hot path."""
    if not records:
        return []

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": EXTRACTOR_SYSTEM}],
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": CONVENTIONS_SCHEMA},
        },
        messages=[
            {
                "role": "user",
                "content": "Review comments from this organisation:\n\n" + render_corpus(records),
            }
        ],
    ) as stream:
        message = stream.get_final_message()

    if getattr(message, "stop_reason", None) == "refusal":
        raise RuntimeError("convention extraction refused by safety classifier")

    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("no structured output returned from convention extraction")

    payload = json.loads(text)
    return [Convention(**item) for item in payload.get("conventions", [])]


def ingest(
    ledger: Ledger,
    conventions: list[Convention],
    source: str,
) -> int:
    for convention in conventions:
        ledger.upsert_convention(
            convention_id=convention.convention_id,
            statement=convention.statement,
            rationale=convention.rationale,
            category=convention.category,
            source=source,
        )
    return len(conventions)
