"""End to end. These are the guarantees the README claims."""

import pytest
from conftest import VULNERABLE

from caliper.pipeline import build_submission, rescore, review_submission
from caliper.providers.replay import ReplayDetector
from caliper.scoring.rubric import DEFAULT_RUBRIC, Rubric


def run(ledger, sources=VULNERABLE, author="dev", nonce="fixed", **kwargs):
    submission = build_submission(sources, author=author)
    return review_submission(
        submission,
        ReplayDetector(seed=submission.content_hash, nonce=nonce),
        ledger=ledger,
        passes=kwargs.pop("passes", 5),
        **kwargs,
    )


def test_review_produces_grounded_findings_and_a_score(ledger):
    report = run(ledger)
    assert report.review.findings
    assert 0.0 <= report.review.score.value <= 100.0
    assert report.review.score.rubric_hash == DEFAULT_RUBRIC.fingerprint()
    assert all(f.anchor.verified_by for f in report.review.findings)


def test_every_finding_anchors_to_real_source(ledger):
    report = run(ledger)
    for finding in report.review.findings:
        source = VULNERABLE[finding.anchor.path]
        line_count = len(source.splitlines())
        assert 1 <= finding.anchor.start_line <= line_count
        assert finding.anchor.end_line <= line_count


def test_identical_resubmission_returns_the_identical_review(ledger):
    """Tier 1: the reproducibility guarantee, in one assertion."""
    first = run(ledger).review
    second = run(ledger).review

    assert second.cached is True
    assert second.review_id == first.review_id
    assert second.score.value == first.score.value
    assert second.model_dump(exclude={"cached"}) == first.model_dump(exclude={"cached"})


def test_cache_can_be_bypassed(ledger):
    run(ledger)
    assert run(ledger, use_cache=False).review.cached is False


def test_changing_one_byte_produces_a_different_review(ledger):
    first = run(ledger).review
    mutated = dict(VULNERABLE)
    mutated["svc/api.py"] += "\n# a comment\n"
    second = run(ledger, sources=mutated).review
    assert second.review_id != first.review_id
    assert second.content_hash != first.content_hash


def test_file_ordering_does_not_change_the_submission_hash():
    forward = build_submission(dict(VULNERABLE), author="dev")
    backward = build_submission(dict(reversed(list(VULNERABLE.items()))), author="dev")
    assert forward.content_hash == backward.content_hash


def test_impact_is_attached_from_the_dependency_graph(ledger):
    report = run(ledger)
    by_path = {}
    for finding in report.review.findings:
        by_path.setdefault(finding.anchor.path, finding)
    assert by_path["svc/auth.py"].blast_radius > by_path["scripts/oneoff.py"].blast_radius
    assert by_path["svc/auth.py"].dependents == 2


def test_the_same_defect_costs_more_in_a_hub_than_in_a_throwaway_script(ledger):
    """The problem statement's central example, end to end."""
    from caliper.scoring.rubric import penalty_for

    report = run(ledger)
    creds = [f for f in report.review.findings if f.rule == "hardcoded_credential"]
    assert len(creds) == 2, "fixture should hold the same defect in two places"
    hub = max(creds, key=lambda f: f.blast_radius)
    throwaway = min(creds, key=lambda f: f.blast_radius)
    assert hub.severity is throwaway.severity
    assert penalty_for(hub) > penalty_for(throwaway)


def test_recurrence_is_recorded_across_submissions(ledger):
    run(ledger, author="repeat")
    mutated = dict(VULNERABLE)
    mutated["svc/api.py"] += "\n# second submission\n"
    report = run(ledger, sources=mutated, author="repeat")
    assert any(f.recurrence > 0 for f in report.review.findings)
    assert ledger.repeat_offenders("repeat")


def test_quorum_filters_unstable_findings(ledger):
    strict = run(ledger, quorum=5, use_cache=False).review
    loose = run(ledger, quorum=1, use_cache=False).review
    assert len(strict.findings) <= len(loose.findings)
    assert strict.score.value >= loose.score.value


def test_review_reports_its_own_discards(ledger):
    report = run(ledger)
    assert report.review.dropped_ungrounded >= 0
    assert 0.0 <= report.detector_precision <= 1.0


def test_review_without_a_ledger_still_works():
    submission = build_submission(VULNERABLE, author="anon")
    report = review_submission(
        submission, ReplayDetector(seed=submission.content_hash, nonce="x"), ledger=None
    )
    assert report.review.findings
    assert all(f.recurrence == 0 for f in report.review.findings)


def test_clean_submission_scores_full_marks(ledger):
    report = run(ledger, sources={"clean.py": "def add(a, b):\n    return a + b\n"})
    assert report.review.score.value == 100.0
    assert report.review.findings == []
    assert "No grounded findings" in report.review.summary


def test_stored_reviews_can_be_replayed_under_a_new_rubric(ledger):
    """Changing the rubric must be a measurable act, not a break in continuity."""
    original = run(ledger).review
    harsher = Rubric(version="2.0.0", impact_gain=2.0)
    replayed = rescore(original, harsher)

    assert replayed.score.rubric_version == "2.0.0"
    assert replayed.score.rubric_hash != original.score.rubric_hash
    assert replayed.findings == original.findings, "findings are evidence, not opinion"
    assert replayed.score.total_penalty > original.score.total_penalty


def test_score_carries_the_model_pin(ledger):
    review = run(ledger).review
    assert review.model_pin.startswith("replay@local")


@pytest.mark.parametrize("passes", [1, 3, 5])
def test_pass_count_is_honoured(ledger, passes):
    report = run(ledger, passes=passes, use_cache=False)
    assert report.review.passes == passes
    assert all(f.votes <= passes for f in report.review.findings)
