"""Claude as the detection stage, on Vertex AI or the first-party API.

Request shape, and why each part is the way it is:

  output       `output_config.format` by default: a guaranteed-shape response.
               Some organisations disable that feature by Vertex org policy
               (`constraints/vertexai.allowedPartnerModelFeatures`), so the
               detector falls back to a forced tool call, which the same policy
               permits. See `OutputMode` below.
  system       byte-stable instructions + org conventions, with an explicit
               `cache_control` breakpoint on the last block. Vertex does not
               support top-level automatic caching, so the breakpoint is placed
               by hand — the QR table says `Automatic prompt caching: ❌` there.
  messages     the code, permuted per pass. Volatile, and deliberately after
               the breakpoint so it never invalidates the cached prefix.
  thinking     adaptive. `budget_tokens` is removed on Opus 5 and returns 400.
  temperature  not passed, because it no longer exists. See the note below.

On determinism: there is no sampling knob to turn down. `temperature` is not a
parameter of the current Messages API — the Python SDK does not accept it and
the API rejects it. Any design that planned to buy reproducibility with
`temperature=0` cannot be built today. Caliper does not try: it treats each
call as a sample and recovers stability at the quorum and rubric stages, where
it can actually be guaranteed rather than hoped for.
"""

from __future__ import annotations

import json
import random
import threading
from typing import Any, Literal

from ..models import RawFinding, SourceFile
from ..prompts import (
    CONVENTIONS_PREAMBLE,
    DETECTOR_SYSTEM,
    render_file_block,
    render_sweep,
)
from .base import FINDINGS_SCHEMA, DetectionOutcome, Usage

DEFAULT_MODEL = "claude-opus-5"

TOOL_NAME = "report_findings"

# How the findings come back.
#
#   structured  `output_config.format` — the API guarantees the response is one
#               text block of schema-valid JSON. Strongest, and the default.
#   tool        a forced call to a single tool whose input schema is the same
#               shape. Shape is strongly steered but not guaranteed, so items
#               are validated individually and bad ones dropped.
#   auto        try `structured`, and on the specific org-policy rejection fall
#               back to `tool` for the rest of this detector's life.
#
# The fallback exists because Vertex org policy can disable the structured
# outputs feature for partner models — including `strict: true` on tools —
# while still permitting ordinary tool use. Without it, Caliper simply does not
# run on those projects.
OutputMode = Literal["auto", "structured", "tool"]

_POLICY_MARKERS = ("structured_output", "structured_outputs")


class ClaudeDetector:
    """Detection backed by Claude. Backend is Vertex AI unless told otherwise."""

    def __init__(
        self,
        *,
        backend: str = "vertex",
        model: str = DEFAULT_MODEL,
        project_id: str | None = None,
        region: str = "us-central1",
        effort: str = "high",
        max_tokens: int = 32000,
        seed: str = "",
        output_mode: OutputMode = "auto",
        client: Any | None = None,
    ) -> None:
        self.backend = backend
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.seed = seed
        self.name = f"claude:{backend}"
        self._mode: OutputMode = "structured" if output_mode == "auto" else output_mode
        # Whether falling back is permitted at all. Fixed at construction: an
        # explicit `structured` must fail loudly rather than quietly downgrade.
        self._auto = output_mode == "auto"
        # Passes run concurrently and each may hit the policy wall independently
        # before any of them has flipped the shared mode.
        self._mode_lock = threading.Lock()
        # What a score is pinned to. A model change is a rubric-level event: two
        # scores produced under different pins are not directly comparable, and
        # the pin travels with every stored review so that is always visible.
        self.model_pin = f"{model}@{backend}:{region if backend == 'vertex' else 'api'}"
        self._client = client or self._build_client(backend, project_id, region)

    @staticmethod
    def _build_client(backend: str, project_id: str | None, region: str) -> Any:
        if backend == "vertex":
            from anthropic import AnthropicVertex

            if not project_id:
                raise ValueError(
                    "Vertex backend requires a GCP project id (CALIPER_GCP_PROJECT or --project)."
                )
            # Auth is Google ADC — `gcloud auth application-default login`.
            # No Anthropic API key is involved on this path.
            return AnthropicVertex(project_id=project_id, region=region)
        if backend == "anthropic":
            from anthropic import Anthropic

            return Anthropic()
        raise ValueError(f"unknown backend {backend!r} (expected 'vertex' or 'anthropic')")

    def _system_blocks(self, conventions: str) -> list[dict]:
        """Stable prefix. The breakpoint goes on the final block."""
        blocks: list[dict] = [{"type": "text", "text": DETECTOR_SYSTEM}]
        if conventions.strip():
            blocks.append(
                {"type": "text", "text": CONVENTIONS_PREAMBLE + "\n" + conventions.strip()}
            )
        # 1h TTL: a review runs several passes over the same prefix within
        # seconds, and repeat submissions from the same team reuse it for the
        # rest of the session.
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        return blocks

    def _ordered_files(self, files: list[SourceFile], pass_index: int) -> list[SourceFile]:
        """Deterministic per-pass permutation.

        Seeded from the submission's own content hash, so pass 3 of a given
        submission always sees the same order on every machine and every rerun.
        Position bias is decorrelated without introducing real randomness.
        """
        if pass_index == 0 or len(files) < 2:
            return list(files)
        ordering = list(files)
        rng = random.Random(f"{self.seed}:{pass_index}")
        rng.shuffle(ordering)
        return ordering

    def detect(
        self,
        files: list[SourceFile],
        pass_index: int,
        total_passes: int,
        conventions: str = "",
    ) -> DetectionOutcome:
        blocks = [
            render_file_block(f.path, f.language, f.text)
            for f in self._ordered_files(files, pass_index)
        ]
        prompt = render_sweep(blocks, pass_index, total_passes)

        try:
            message = self._call(prompt, conventions, self._mode)
        except Exception as exc:
            if not self._blocked_by_policy(exc):
                raise
            with self._mode_lock:
                # Idempotent: later passes that hit the same wall concurrently
                # just re-assert it. Flipping it here means passes that have not
                # started yet skip the doomed structured attempt entirely.
                self._mode = "tool"
            message = self._call(prompt, conventions, "tool")

        payload = _payload_of(message)
        return DetectionOutcome(
            raw_findings=_parse_findings(payload),
            usage=_usage_of(message),
            model_reported=getattr(message, "model", self.model) or self.model,
            conventions_applied=_conventions_of(payload),
        )

    def _blocked_by_policy(self, exc: Exception) -> bool:
        """Is this the org-policy rejection we know how to work around?

        Narrow on purpose: any other 400 is a real defect in the request and
        must surface rather than be silently retried in a weaker mode.
        """
        if not self._auto:
            return False
        if getattr(exc, "status_code", None) != 400:
            return False
        return any(marker in str(exc) for marker in _POLICY_MARKERS)

    def _call(self, prompt: str, conventions: str, mode: OutputMode) -> Any:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self._system_blocks(conventions),
            "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": prompt}],
        }
        if mode == "structured":
            request["output_config"] = {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": FINDINGS_SCHEMA},
            }
        else:
            # `strict` is deliberately absent: it is part of the same structured
            # outputs feature the policy blocks. Shape is steered by the schema
            # and the forced tool choice, and validated per item on the way out.
            request["output_config"] = {"effort": self.effort}
            request["tools"] = [
                {
                    "name": TOOL_NAME,
                    "description": "Report every defect found in the reviewed files.",
                    "input_schema": FINDINGS_SCHEMA,
                }
            ]
            request["tool_choice"] = {"type": "tool", "name": TOOL_NAME}

        with self._client.messages.stream(**request) as stream:
            return stream.get_final_message()


def _payload_of(message: Any) -> dict:
    """Extract the findings payload from either output mode.

    A refusal or a token cap can end a turn early with no payload at all, so the
    stop reason is checked explicitly rather than optimistically indexing into
    the content list — a truncated review that silently scores as clean would be
    the worst possible failure for a rating authority.
    """
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise RuntimeError(f"detection refused by safety classifier (category={category})")
    if stop == "max_tokens":
        raise RuntimeError(
            "detection truncated at max_tokens; raise --max-tokens or split the submission"
        )

    for block in message.content:
        if block.type == "tool_use" and block.name == TOOL_NAME:
            return block.input if isinstance(block.input, dict) else {}

    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError(f"no findings payload in response (stop_reason={stop})")
    return json.loads(text)


def _parse_findings(payload: dict) -> list[RawFinding]:
    findings: list[RawFinding] = []
    for item in payload.get("findings", []):
        item.pop("convention", None)  # tracked separately; not part of identity
        try:
            findings.append(RawFinding.model_validate(item))
        except Exception:
            # A malformed item is a detector defect, not a reason to lose the
            # other findings in the pass. It is dropped and shows up as a lower
            # grounded-finding count.
            continue
    return findings


def _conventions_of(payload: dict) -> list[str]:
    cited = {
        item.get("convention", "").strip()
        for item in payload.get("findings", [])
        if item.get("convention", "").strip()
    }
    return sorted(cited)


def _usage_of(message: Any) -> Usage:
    usage = getattr(message, "usage", None)
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )
