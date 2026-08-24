"""Gemini on Vertex AI as the detection stage.

Caliper's architecture claims the model is a replaceable component: the entire
probabilistic surface is one method returning `RawFinding` objects, and every
guarantee downstream is arithmetic. This module is the test of that claim. It
shares the prompt, the schema, the grounding, the quorum and the rubric with
the Claude backend, and differs only in how the bytes reach a model.

## The one real difference, and it matters

Gemini exposes `temperature` and `seed`. Claude does not — those parameters
were removed from the Messages API. That difference cuts in an interesting
direction for reproducibility:

  Claude   no sampling control. Each pass is an uncontrolled sample, so cold
           re-runs of the same submission differ, and quorum exists to bound
           how much. Tier 2 is a measured band.

  Gemini   `temperature=0` plus a seed derived from the submission's own
           content hash. Every input to every pass is a pure function of the
           submission, so a cold re-run should reproduce the *whole ensemble*,
           not merely land near it. Tier 2 collapses toward Tier 1.

## Why the seed varies per pass, and must

The tempting move is one fixed seed for the run. That would be wrong. With
`temperature=0` and an identical seed, all K passes return the same answer,
every finding trivially scores K/K votes, and quorum becomes theatre — it would
report unanimous agreement while actually having sampled once.

So the seed is `hash(submission_content, pass_index)`: different across passes,
so the passes are genuinely independent hypotheses worth voting between; and
identical across runs, so the vote is reproducible. Diversity and determinism
are not in tension here as long as the diversity is itself deterministic.
"""

from __future__ import annotations

import json
from typing import Any

from ..hashing import digest
from ..models import RawFinding, SourceFile
from ..prompts import (
    CONVENTIONS_PREAMBLE,
    DETECTOR_SYSTEM,
    render_file_block,
    render_sweep,
)
from .base import FINDINGS_SCHEMA, DetectionOutcome, Usage

DEFAULT_MODEL = "gemini-2.5-pro"

# Vertex accepts a 32-bit signed seed.
_SEED_MODULUS = 2**31 - 1


class GeminiDetector:
    """Detection backed by Gemini on Vertex AI. Auth is Google ADC."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        project_id: str | None = None,
        region: str = "global",
        max_tokens: int = 32000,
        seed: str = "",
        thinking_budget: int | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.seed = seed
        self.thinking_budget = thinking_budget
        self.name = "gemini:vertex"
        self.model_pin = f"{model}@vertex:{region}"
        self._client = client or self._build_client(project_id, region)

    @staticmethod
    def _build_client(project_id: str | None, region: str) -> Any:
        from google import genai

        if not project_id:
            raise ValueError(
                "Gemini backend requires a GCP project id (CALIPER_GCP_PROJECT or --project)."
            )
        # Auth is Google ADC — `gcloud auth application-default login`.
        return genai.Client(vertexai=True, project=project_id, location=region)

    def _pass_seed(self, pass_index: int) -> int:
        """Deterministic per-pass seed. See the module docstring."""
        return int(digest("gemini-seed", self.seed, str(pass_index))[:8], 16) % _SEED_MODULUS

    def _ordered_files(self, files: list[SourceFile], pass_index: int) -> list[SourceFile]:
        """Same deterministic permutation strategy as the Claude backend."""
        if pass_index == 0 or len(files) < 2:
            return list(files)
        import random

        ordering = list(files)
        random.Random(f"{self.seed}:{pass_index}").shuffle(ordering)
        return ordering

    def _system_instruction(self, conventions: str) -> str:
        """Byte-stable. Gemini 2.5 caches long prefixes implicitly, and an
        unstable instruction would defeat that just as it defeats Claude's."""
        if conventions.strip():
            return f"{DETECTOR_SYSTEM}\n\n{CONVENTIONS_PREAMBLE}\n{conventions.strip()}"
        return DETECTOR_SYSTEM

    def detect(
        self,
        files: list[SourceFile],
        pass_index: int,
        total_passes: int,
        conventions: str = "",
    ) -> DetectionOutcome:
        from google.genai import types

        blocks = [
            render_file_block(f.path, f.language, f.text)
            for f in self._ordered_files(files, pass_index)
        ]
        prompt = render_sweep(blocks, pass_index, total_passes)

        config: dict[str, Any] = {
            "system_instruction": self._system_instruction(conventions),
            "max_output_tokens": self.max_tokens,
            # Pinned, unlike Claude, which no longer accepts these at all.
            "temperature": 0.0,
            "seed": self._pass_seed(pass_index),
            "response_mime_type": "application/json",
            "response_json_schema": FINDINGS_SCHEMA,
        }
        if self.thinking_budget is not None:
            config["thinking_config"] = types.ThinkingConfig(thinking_budget=self.thinking_budget)

        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(**config),
        )

        payload = _payload_of(response)
        return DetectionOutcome(
            raw_findings=_parse_findings(payload),
            usage=_usage_of(response),
            model_reported=self.model,
            conventions_applied=_conventions_of(payload),
        )


def _payload_of(response: Any) -> dict:
    """Extract the findings payload, refusing to treat a blocked or truncated
    response as a clean review — silently scoring 100 because the model was cut
    off is the worst failure available to a rating authority."""
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        reason = str(getattr(candidates[0], "finish_reason", "") or "")
        if "MAX_TOKENS" in reason:
            raise RuntimeError(
                "detection truncated at max_output_tokens; raise it or split the submission"
            )
        if "SAFETY" in reason or "BLOCK" in reason or "PROHIBITED" in reason:
            raise RuntimeError(f"detection blocked by safety filter (finish_reason={reason})")

    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        raise RuntimeError(f"prompt blocked (reason={feedback.block_reason})")

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("no structured output returned from Gemini")
    return json.loads(text)


def _parse_findings(payload: dict) -> list[RawFinding]:
    findings: list[RawFinding] = []
    for item in payload.get("findings", []):
        item.pop("convention", None)  # tracked separately; not part of identity
        try:
            findings.append(RawFinding.model_validate(item))
        except Exception:
            # A malformed item is a detector defect, not a reason to lose the
            # rest of the pass.
            continue
    return findings


def _conventions_of(payload: dict) -> list[str]:
    return sorted(
        {
            item.get("convention", "").strip()
            for item in payload.get("findings", [])
            if item.get("convention", "").strip()
        }
    )


def _usage_of(response: Any) -> Usage:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return Usage()
    cached = getattr(meta, "cached_content_token_count", 0) or 0
    prompt_tokens = getattr(meta, "prompt_token_count", 0) or 0
    return Usage(
        # Report uncached input only, so the number means the same thing as it
        # does on the Claude backend.
        input_tokens=max(0, prompt_tokens - cached),
        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        cache_read_tokens=cached,
        cache_write_tokens=0,
    )
