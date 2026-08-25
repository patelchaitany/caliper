"""Gemini on Vertex AI as the detection stage.

Caliper's architecture claims the model is a replaceable component: the entire
probabilistic surface is one method returning `RawFinding` objects, and every
guarantee downstream is arithmetic. This module is the test of that claim. It
shares the prompt, the schema, the grounding, the quorum and the rubric with
the Claude backend, and differs only in how the bytes reach a model.

## The one real difference, and it matters

Gemini exposes `temperature` and `seed`. Claude does not — those parameters
were removed from the Messages API. It is tempting to conclude that this hands
Gemini a reproducibility advantage. It does not, and the measurement below is
the reason this module documents it at such length: the obvious inference is
wrong, and quietly acting on it would put a guarantee in the README that the
system cannot honour.

Both backends land in the same place. Every input to every pass is a pure
function of the submission on the Gemini path, and cold re-runs still differ,
because the nondeterminism does not live in the sampler.

## What pinning the sampler actually buys — measured, not assumed

The intuition is that `temperature=0` plus a fixed seed makes the model a pure
function. On Vertex it does not. Five calls against `gemini-2.5-pro`, identical
prompt and config, varying only the seed:

    seed 42  call 1   digest cd5fb115   3 findings
    seed 42  call 2   digest cd5fb115   3 findings
    seed 42  call 3   digest 43ab1365   3 findings   <- same seed, different bytes
    seed  7           digest 43ab1365   3 findings
    seed 99           digest cd5fb115   3 findings

Read the digests carefully. There are exactly two distinct outputs, one seed
produced both of them, and different seeds landed on the same ones. The seed
does not predict which you get — so on this workload it is not merely
best-effort, it has no observable effect at all. The variation is
infrastructure-level (replicas, batching, reasoning-token paths), several
layers below anything a request parameter reaches.

End to end the gap is wider: three cold runs of a three-file submission scored
90.65, 82.65 and 88.55 — an 8.00 point spread, no tighter than a backend with
no sampling controls whatsoever. The variance concentrates at the quorum
boundary, where 14-19 marginal candidates per run sit close enough to the
threshold that a small perturbation flips them across.

So pinning the sampler is cheap and worth doing, and it is **not** a
reproducibility mechanism. Tier 1 — the content-addressed ledger — remains the
only exact guarantee, and it is backend-independent by construction. This
backend is evidence for that design rather than an exception to it.

## The part that did hold, and why it matters here

Every one of those five calls reported the same three rules —
`inefficient_prefix_invalidation`, `missing_thread_safety`,
`unenforced_cache_size_limit` — even the ones whose bytes differed. The model
re-worded itself without changing what it found.

That is precisely the level Caliper identifies findings at: a fingerprint over
rule, symbol and whitespace-normalised span, never over the model's prose. The
layer that varies is the layer we already discard.

It also means the K passes remain genuine independent samples on this backend
despite `temperature=0`, because the nondeterminism arrives from below rather
than from sampling. Quorum keeps its meaning; it is voting on real variation.

## Why the seed still varies per pass

Given the seed demonstrably does nothing here, varying it is defensive rather
than load-bearing — but it is the right default, and it costs nothing.

If the seed were ever honoured, on another model, another region, or a future
version, a single fixed seed at `temperature=0` would make all K passes return
the same answer. Every finding would trivially score K/K, and quorum would
report unanimous agreement having really sampled once. That failure is silent:
the scores keep arriving and simply mean less than they claim.

So the seed is `hash(submission_content, pass_index)` — different across passes
so they stay independent hypotheses, identical across runs so the vote would
reproduce. It is insurance against a parameter starting to work.

The pass diversity Caliper actually relies on comes from elsewhere: the file
ordering permutation, and the backend's own variation.
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
