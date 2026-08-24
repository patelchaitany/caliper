"""Memory: the ledger is what makes reviews stateful and growth visible."""

from conftest import finding

from caliper.models import Review
from caliper.scoring.rubric import score_findings


def store(ledger, author, content_hash, rules, review_id):
    findings = [finding(fingerprint=f"{review_id}:{r}", rule=r) for r in rules]
    review = Review(
        review_id=review_id,
        submission_id=review_id,
        author=author,
        content_hash=content_hash,
        model="replay",
        model_pin="replay@local",
        passes=5,
        quorum=3,
        findings=findings,
        score=score_findings(findings, 100),
    )
    ledger.record(review, ledger.history_signature(author, content_hash))
    return review


def test_recurrence_counts_prior_reviews(ledger):
    for i in range(3):
        store(ledger, "dev", f"c{i}", ["hardcoded_credential"], f"r{i}")
    counts = ledger.recurrence_for("dev", ["hardcoded_credential"], exclude_content="c-new")
    assert counts["hardcoded_credential"] == 3


def test_repeated_instances_in_one_submission_count_once(ledger):
    findings = [finding(fingerprint=f"f{i}", rule="swallowed_exception") for i in range(4)]
    review = Review(
        review_id="r0",
        submission_id="s0",
        author="dev",
        content_hash="c0",
        model="replay",
        model_pin="p",
        passes=5,
        quorum=3,
        findings=findings,
        score=score_findings(findings, 100),
    )
    ledger.record(review, "sig")
    assert ledger.recurrence_for("dev", ["swallowed_exception"], "c1") == {"swallowed_exception": 1}


def test_a_submission_does_not_escalate_against_itself(ledger):
    """Re-reviewing unchanged code must not make it look like a repeat offence."""
    store(ledger, "dev", "same", ["sql_injection"], "r0")
    assert ledger.recurrence_for("dev", ["sql_injection"], exclude_content="same") == {}


def test_history_signature_excludes_the_submission_under_review(ledger):
    before = ledger.history_signature("dev", "same")
    store(ledger, "dev", "same", ["sql_injection"], "r0")
    assert ledger.history_signature("dev", "same") == before, (
        "recording a review must not change the key that identifies it"
    )


def test_history_signature_moves_when_other_submissions_land(ledger):
    before = ledger.history_signature("dev", "mine")
    store(ledger, "dev", "other", ["sql_injection"], "r0")
    assert ledger.history_signature("dev", "mine") != before


def test_authors_are_isolated(ledger):
    store(ledger, "alice", "c0", ["sql_injection"], "r0")
    assert ledger.recurrence_for("bob", ["sql_injection"], "c1") == {}


def test_trend_is_chronological(ledger):
    for i in range(3):
        store(ledger, "dev", f"c{i}", ["sql_injection"], f"r{i}")
    points = ledger.trend("dev")
    assert len(points) == 3
    assert [p.created_at for p in points] == sorted(p.created_at for p in points)


def test_rerecording_does_not_inflate_counts(ledger):
    review = store(ledger, "dev", "c0", ["sql_injection"], "r0")
    ledger.record(review, "sig")
    ledger.record(review, "sig")
    assert ledger.recurrence_for("dev", ["sql_injection"], "cX") == {"sql_injection": 1}


def test_conventions_block_is_stable_and_renderable(ledger):
    ledger.upsert_convention(
        "errors_wrapped", "Wrap errors with the operation name.", "why", "correctness", "prs.jsonl"
    )
    ledger.upsert_convention(
        "no_globals", "No module-level mutable state.", "why", "architecture", "prs.jsonl"
    )
    first = ledger.conventions_block()
    assert "errors_wrapped" in first and "no_globals" in first
    assert ledger.conventions_block() == first, "prompt prefix must be byte-stable"


def test_upsert_increments_occurrences(ledger):
    for _ in range(3):
        ledger.upsert_convention("c1", "s", "r", "correctness", "src")
    assert ledger.conventions()[0]["occurrences"] == 3
