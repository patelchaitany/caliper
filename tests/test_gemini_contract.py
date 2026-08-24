"""Regression guard on the Gemini request shape.

The interesting assertions here are about the seed. Gemini exposes
`temperature` and `seed` where Claude exposes neither, and that changes what
reproducibility means on this backend — but only if the seeding is done
correctly. A single fixed seed across passes would make every pass identical
and turn quorum into theatre: unanimous agreement reported from what was really
one sample. These tests pin the property that matters — seeds differ *within* a
run and repeat *across* runs.
"""

import json
import types as pytypes

import pytest

from caliper.analysis.structure import make_source_file
from caliper.providers.base import FINDINGS_SCHEMA
from caliper.providers.gemini import GeminiDetector

FILES = [make_source_file(p, f"x = {i}\n") for i, p in enumerate(["a.py", "b.py", "c.py"])]

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


def response(findings=None, finish_reason="STOP", cached=0, prompt_tokens=500):
    return pytypes.SimpleNamespace(
        text=json.dumps({"findings": findings if findings is not None else []}),
        candidates=[pytypes.SimpleNamespace(finish_reason=finish_reason)],
        prompt_feedback=None,
        usage_metadata=pytypes.SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=42,
            cached_content_token_count=cached,
        ),
    )


class FakeClient:
    def __init__(self, resp=None):
        self.calls = []
        self._resp = resp or response()
        self.models = pytypes.SimpleNamespace(generate_content=self._generate)

    def _generate(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._resp


def detector(client, **kwargs):
    return GeminiDetector(client=client, seed="submission-hash", **kwargs)


def test_temperature_is_pinned_to_zero():
    """Unlike Claude, this backend has the knob — so use it."""
    client = FakeClient()
    detector(client).detect(FILES, 0, 5)
    assert client.calls[0]["config"].temperature == 0.0


def test_seeds_differ_between_passes():
    """Identical seeds at temperature 0 would collapse the ensemble to one sample."""
    client = FakeClient()
    engine = detector(client)
    for index in range(5):
        engine.detect(FILES, index, 5)
    seeds = [call["config"].seed for call in client.calls]
    assert len(set(seeds)) == 5, "quorum would be meaningless with repeated seeds"


def test_seeds_repeat_across_runs():
    """...but the ensemble must still be reproducible."""
    first = [detector(FakeClient())._pass_seed(i) for i in range(5)]
    second = [detector(FakeClient())._pass_seed(i) for i in range(5)]
    assert first == second


def test_seeds_depend_on_the_submission():
    a = GeminiDetector(client=FakeClient(), seed="submission-a")._pass_seed(0)
    b = GeminiDetector(client=FakeClient(), seed="submission-b")._pass_seed(0)
    assert a != b


def test_seed_fits_the_accepted_range():
    engine = detector(FakeClient())
    for index in range(50):
        assert 0 <= engine._pass_seed(index) < 2**31 - 1


def test_structured_output_uses_the_shared_schema():
    client = FakeClient()
    detector(client).detect(FILES, 0, 5)
    config = client.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == FINDINGS_SCHEMA


def test_system_instruction_is_byte_stable_across_passes():
    client = FakeClient()
    engine = detector(client)
    for index in range(4):
        engine.detect(FILES, index, 4, conventions="- [c1] Wrap errors.")
    instructions = {call["config"].system_instruction for call in client.calls}
    assert len(instructions) == 1


def test_code_is_not_placed_in_the_system_instruction():
    client = FakeClient()
    detector(client).detect(FILES, 0, 5)
    call = client.calls[0]
    assert "a.py" not in call["config"].system_instruction
    assert "a.py" in call["contents"]


def test_pass_permutation_is_deterministic_and_actually_permutes():
    engine = detector(FakeClient())
    orders = [[f.path for f in engine._ordered_files(FILES, i)] for i in range(6)]
    assert orders == [
        [f.path for f in detector(FakeClient())._ordered_files(FILES, i)] for i in range(6)
    ]
    assert len({tuple(o) for o in orders}) > 1


def test_truncation_is_surfaced_not_scored_as_clean():
    client = FakeClient(response(finish_reason="MAX_TOKENS"))
    with pytest.raises(RuntimeError, match="truncated"):
        detector(client).detect(FILES, 0, 5)


def test_safety_block_is_surfaced():
    client = FakeClient(response(finish_reason="SAFETY"))
    with pytest.raises(RuntimeError, match="blocked"):
        detector(client).detect(FILES, 0, 5)


def test_malformed_item_does_not_discard_the_whole_pass():
    client = FakeClient(response([{"rule": "broken"}, VALID_ITEM]))
    outcome = detector(client).detect(FILES, 0, 5)
    assert [f.rule for f in outcome.raw_findings] == ["sql_injection"]


def test_usage_separates_cached_from_uncached_input():
    """Reported the same way as the Claude backend, so the numbers compare."""
    client = FakeClient(response(cached=400, prompt_tokens=500))
    usage = detector(client).detect(FILES, 0, 5).usage
    assert usage.cache_read_tokens == 400
    assert usage.input_tokens == 100


def test_project_id_is_required():
    with pytest.raises(ValueError, match="project id"):
        GeminiDetector(project_id=None)


def test_model_pin_records_where_the_score_came_from():
    engine = GeminiDetector(client=FakeClient(), model="gemini-2.5-pro", region="global", seed="s")
    assert engine.model_pin == "gemini-2.5-pro@vertex:global"


def test_conventions_are_appended_after_the_base_instructions():
    client = FakeClient()
    detector(client).detect(FILES, 0, 5, conventions="- [c1] Wrap errors.")
    instruction = client.calls[0]["config"].system_instruction
    assert instruction.index("detection stage") < instruction.index("c1")
