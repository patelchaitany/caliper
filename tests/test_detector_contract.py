"""Regression guard on the Claude request shape.

These assertions encode facts about the current Messages API that are easy to
get wrong from memory, and expensive to discover at runtime:

  * `temperature` no longer exists. Sending it is a 400, and any design that
    planned to buy determinism with `temperature=0` is not buildable today.
  * `budget_tokens` is removed on Opus 5; thinking is configured adaptively.
  * Vertex has no automatic prompt caching, so the breakpoint must be placed on
    a content block by hand.
"""

import json
import types

import pytest

from caliper.analysis.structure import make_source_file
from caliper.providers.base import FINDINGS_SCHEMA
from caliper.providers.claude import ClaudeDetector

FILES = [make_source_file(p, f"x = {i}\n") for i, p in enumerate(["a.py", "b.py", "c.py"])]


class FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


def message(payload=None, stop_reason="end_turn"):
    text = json.dumps(payload if payload is not None else {"findings": []})
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=None,
        model="claude-opus-5",
        usage=types.SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=4000,
            cache_creation_input_tokens=0,
        ),
    )


class FakeClient:
    def __init__(self, response=None):
        self.calls = []
        self._response = response or message()
        self.messages = types.SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._response)


def detector(client, **kwargs):
    return ClaudeDetector(backend="anthropic", client=client, seed="seed", **kwargs)


def test_temperature_is_never_sent():
    client = FakeClient()
    detector(client).detect(FILES, 0, 5)
    assert "temperature" not in client.calls[0]
    assert "top_p" not in client.calls[0]
    assert "top_k" not in client.calls[0]


def test_thinking_is_adaptive_without_a_token_budget():
    client = FakeClient()
    detector(client).detect(FILES, 0, 5)
    assert client.calls[0]["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in client.calls[0]["thinking"]


def test_structured_output_is_requested_with_the_strict_schema():
    client = FakeClient()
    detector(client).detect(FILES, 0, 5)
    output_config = client.calls[0]["output_config"]
    assert output_config["format"] == {"type": "json_schema", "schema": FINDINGS_SCHEMA}
    assert output_config["effort"] == "high"


def test_schema_is_strict_at_every_level():
    assert FINDINGS_SCHEMA["additionalProperties"] is False
    item = FINDINGS_SCHEMA["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])


def test_cache_breakpoint_is_placed_explicitly_on_the_last_system_block():
    client = FakeClient()
    detector(client).detect(FILES, 0, 5, conventions="- [c1] Wrap errors.")
    system = client.calls[0]["system"]
    assert len(system) == 2
    assert "cache_control" not in system[0]
    assert system[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_system_prefix_is_byte_stable_across_passes():
    """Any variation here silently costs a full cache miss on every request."""
    client = FakeClient()
    engine = detector(client)
    for index in range(4):
        engine.detect(FILES, index, 4, conventions="- [c1] Wrap errors.")
    prefixes = {json.dumps(call["system"], sort_keys=True) for call in client.calls}
    assert len(prefixes) == 1


def test_code_goes_after_the_breakpoint_not_into_the_system_prompt():
    client = FakeClient()
    detector(client).detect(FILES, 0, 5)
    system = json.dumps(client.calls[0]["system"])
    assert "a.py" not in system
    assert "a.py" in client.calls[0]["messages"][0]["content"]


def test_pass_permutation_is_deterministic_and_actually_permutes():
    engine = detector(FakeClient())
    orders = [[f.path for f in engine._ordered_files(FILES, i)] for i in range(6)]
    repeat = [[f.path for f in detector(FakeClient())._ordered_files(FILES, i)] for i in range(6)]
    assert orders == repeat
    assert len({tuple(o) for o in orders}) > 1


def test_refusal_is_surfaced_not_silently_scored_as_clean():
    client = FakeClient(message(stop_reason="refusal"))
    with pytest.raises(RuntimeError, match="refused"):
        detector(client).detect(FILES, 0, 5)


def test_truncation_is_surfaced():
    client = FakeClient(message(stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="truncated"):
        detector(client).detect(FILES, 0, 5)


def test_malformed_finding_does_not_discard_the_whole_pass():
    payload = {
        "findings": [
            {"rule": "broken", "category": "not_a_category"},
            {
                "rule": "sql_injection",
                "category": "security",
                "severity": "critical",
                "confidence": "certain",
                "path": "a.py",
                "start_line": 1,
                "end_line": 1,
                "title": "t",
                "explanation": "e",
                "remediation": "r",
                "quoted_source": "x = 0",
                "convention": "",
            },
        ]
    }
    client = FakeClient(message(payload))
    outcome = detector(client).detect(FILES, 0, 5)
    assert [f.rule for f in outcome.raw_findings] == ["sql_injection"]


def test_usage_is_reported_including_cache_reads():
    client = FakeClient()
    outcome = detector(client).detect(FILES, 0, 5)
    assert outcome.usage.cache_read_tokens == 4000
    assert outcome.usage.cache_hit_rate == 1.0


def test_vertex_backend_requires_a_project_id():
    with pytest.raises(ValueError, match="project id"):
        ClaudeDetector(backend="vertex", project_id=None)


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown backend"):
        ClaudeDetector(backend="bedrock")


def test_model_pin_records_where_the_score_came_from():
    engine = ClaudeDetector(
        backend="anthropic", client=FakeClient(), model="claude-opus-5", seed="s"
    )
    assert engine.model_pin == "claude-opus-5@anthropic:api"


class PolicyBlockedClient(FakeClient):
    """Mimics a Vertex project whose org policy disables structured outputs."""

    def __init__(self, response=None):
        super().__init__(response)
        self.rejected = 0

    def _stream(self, **kwargs):
        if "output_config" in kwargs and "format" in kwargs["output_config"]:
            self.rejected += 1
            error = Exception(
                "Error code: 400 - Organization Policy constraint "
                "constraints/vertexai.allowedPartnerModelFeatures violated attempting "
                "to use a disallowed feature structured_outputs for Partner model"
            )
            error.status_code = 400
            raise error
        return super()._stream(**kwargs)


def tool_message(findings):
    return types.SimpleNamespace(
        content=[
            types.SimpleNamespace(
                type="tool_use", name="report_findings", input={"findings": findings}
            )
        ],
        stop_reason="tool_use",
        stop_details=None,
        model="claude-opus-4-6",
        usage=types.SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=1000,
        ),
    )


VALID_ITEM = {
    "rule": "sql_injection",
    "category": "security",
    "severity": "critical",
    "confidence": "certain",
    "path": "a.py",
    "start_line": 1,
    "end_line": 1,
    "title": "t",
    "explanation": "e",
    "remediation": "r",
    "quoted_source": "x = 0",
    "convention": "",
}


def test_policy_block_falls_back_to_forced_tool_use():
    """Vertex org policy can disable structured outputs. Caliper must still run."""
    client = PolicyBlockedClient(tool_message([VALID_ITEM]))
    outcome = detector(client).detect(FILES, 0, 5)

    assert [f.rule for f in outcome.raw_findings] == ["sql_injection"]
    fallback = client.calls[-1]
    assert fallback["tool_choice"] == {"type": "tool", "name": "report_findings"}
    assert "format" not in fallback["output_config"]


def test_fallback_never_sends_strict_which_the_same_policy_blocks():
    client = PolicyBlockedClient(tool_message([]))
    detector(client).detect(FILES, 0, 5)
    assert "strict" not in client.calls[-1]["tools"][0]


def test_fallback_is_sticky_so_it_is_paid_for_once():
    client = PolicyBlockedClient(tool_message([]))
    engine = detector(client)
    for index in range(4):
        engine.detect(FILES, index, 4)
    # The policy wall is hit once, not once per pass.
    assert client.rejected == 1
    assert len(client.calls) == 4, "every pass still produced a tool call"
    assert engine._mode == "tool"


def test_concurrent_passes_all_fall_back_not_just_the_first():
    """The first pass to hit the wall must not lock the others out of the fallback."""
    import concurrent.futures

    client = PolicyBlockedClient(tool_message([VALID_ITEM]))
    engine = detector(client)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(lambda i: engine.detect(FILES, i, 5), range(5)))

    assert all(len(o.raw_findings) == 1 for o in outcomes)
    assert len(client.calls) == 5


def test_explicit_structured_mode_does_not_fall_back():
    client = PolicyBlockedClient(tool_message([]))
    with pytest.raises(Exception, match="disallowed feature"):
        detector(client, output_mode="structured").detect(FILES, 0, 5)


def test_unrelated_400_is_not_silently_downgraded():
    """Only the known policy rejection is worked around; real bugs must surface."""

    class BadRequestClient(FakeClient):
        def _stream(self, **kwargs):
            error = Exception("Error code: 400 - max_tokens must be less than 64000")
            error.status_code = 400
            raise error

    with pytest.raises(Exception, match="max_tokens"):
        detector(BadRequestClient()).detect(FILES, 0, 5)


def test_tool_mode_still_validates_each_finding():
    """Without `strict` the shape is steered, not guaranteed — so validate."""
    client = PolicyBlockedClient(tool_message([{"rule": "broken"}, VALID_ITEM]))
    outcome = detector(client).detect(FILES, 0, 5)
    assert [f.rule for f in outcome.raw_findings] == ["sql_injection"]


def test_tool_mode_can_be_selected_directly():
    client = FakeClient(tool_message([VALID_ITEM]))
    engine = detector(client, output_mode="tool")
    outcome = engine.detect(FILES, 0, 5)
    assert len(outcome.raw_findings) == 1
    assert "format" not in client.calls[0]["output_config"]
    assert client.calls[0]["output_config"]["effort"] == "high"
