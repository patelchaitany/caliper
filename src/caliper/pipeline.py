"""The review pipeline.

    normalise -> cache check -> parse -> impact -> detect(K) -> ground
              -> quorum -> recurrence -> score -> record

Exactly one stage calls a model. Every stage after `detect` is a pure function
of its inputs, which is why a Caliper score can be defended: the probabilistic
part produces observations, and the number is arithmetic over observations that
were each independently verified against the source.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .analysis.consensus import consolidate, required_votes
from .analysis.grounding import ground_all
from .analysis.impact import ImpactGraph, build_graph
from .analysis.structure import make_source_file, symbols_of
from .hashing import digest
from .models import Finding, Review, SourceFile, Submission
from .providers.base import Detector, Usage
from .scoring.rubric import DEFAULT_RUBRIC, Rubric, score_findings
from .store.ledger import Ledger


@dataclass
class ReviewReport:
    """A review plus the diagnostics that justify trusting it."""

    review: Review
    graph: ImpactGraph
    usage: Usage = field(default_factory=Usage)
    grounding_rejections: list[tuple[str, str]] = field(default_factory=list)
    quorum_required: int = 1

    @property
    def detector_precision(self) -> float:
        """Share of the detector's claims that survived verification.

        Reported rather than hidden: it is the honest measure of how much of
        the model's output was real, and it is the number to watch when
        changing the model pin or the prompt.
        """
        kept = sum(f.votes for f in self.review.findings)
        total = kept + len(self.grounding_rejections)
        return kept / total if total else 1.0


def build_submission(
    sources: dict[str, str], author: str, parent_id: str | None = None
) -> Submission:
    """Normalise raw text into a content-addressed submission.

    Paths are sorted so that the submission hash depends on the code, not on
    the order the filesystem happened to hand it to us.
    """
    files: list[SourceFile] = [make_source_file(path, sources[path]) for path in sorted(sources)]
    content_hash = digest("submission", *(f.content_hash for f in files))
    return Submission(
        submission_id=content_hash[:16],
        author=author,
        files=files,
        content_hash=content_hash,
        parent_id=parent_id,
    )


def review_submission(
    submission: Submission,
    detector: Detector,
    *,
    ledger: Ledger | None = None,
    passes: int = 5,
    quorum: int | None = None,
    rubric: Rubric = DEFAULT_RUBRIC,
    use_cache: bool = True,
    max_workers: int = 5,
) -> ReviewReport:
    rubric_hash = rubric.fingerprint()
    history_sig = (
        ledger.history_signature(submission.author, submission.content_hash) if ledger else "none"
    )

    files_by_path = {f.path: f for f in submission.files}
    symbols = {f.path: symbols_of(f) for f in submission.files}
    graph = build_graph(submission.files)

    # -- exact reproducibility, when we have seen this before ---------------
    review_id = digest(
        "review", submission.content_hash, rubric_hash, detector.model_pin, history_sig
    )
    if ledger is not None and use_cache:
        cached = ledger.find_review(review_id)
        if cached is not None:
            return ReviewReport(
                review=cached,
                graph=graph,
                quorum_required=quorum or required_votes(passes),
            )

    conventions = ledger.conventions_block() if ledger else ""

    # -- the one probabilistic stage ----------------------------------------
    outcomes: list = [None] * passes
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, passes))) as pool:
        futures = {
            pool.submit(detector.detect, submission.files, index, passes, conventions): index
            for index in range(passes)
        }
        for future in futures:
            # Results are stored by pass index, never by completion order, so
            # thread scheduling cannot affect the outcome.
            outcomes[futures[future]] = future.result()

    usage = Usage()
    anchored: list = []
    rejections: list[tuple[str, str]] = []
    for outcome in outcomes:
        usage += outcome.usage
        grounded = ground_all(outcome.raw_findings, files_by_path, symbols)
        anchored.extend(grounded.anchored)
        rejections.extend((raw.rule, reason) for raw, reason in grounded.rejected)

    # -- everything below here is deterministic -----------------------------
    needed = quorum if quorum is not None else required_votes(passes)
    findings, below_quorum = consolidate(anchored, passes=passes, quorum=needed)

    findings = _attach_impact(findings, graph)
    if ledger is not None:
        findings = ledger.annotate_recurrence(submission.author, findings, submission.content_hash)

    score = score_findings(findings, submission.loc, rubric)

    review = Review(
        review_id=review_id,
        submission_id=submission.submission_id,
        author=submission.author,
        content_hash=submission.content_hash,
        model=getattr(detector, "name", "unknown"),
        model_pin=detector.model_pin,
        passes=passes,
        quorum=needed,
        findings=findings,
        score=score,
        dropped_ungrounded=len(rejections),
        dropped_below_quorum=below_quorum,
        summary=_summarise(findings, score.value),
    )

    if ledger is not None:
        ledger.record(review, history_sig)

    return ReviewReport(
        review=review,
        graph=graph,
        usage=usage,
        grounding_rejections=rejections,
        quorum_required=needed,
    )


def _attach_impact(findings: list[Finding], graph: ImpactGraph) -> list[Finding]:
    for finding in findings:
        finding.blast_radius = graph.blast_radius(finding.anchor.path)
        finding.dependents = graph.dependents(finding.anchor.path)
    return findings


def _summarise(findings: list[Finding], score: float) -> str:
    if not findings:
        return "No grounded findings. Nothing in this submission failed verification."
    from .models import Severity

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    ordered = [s.value for s in Severity if s.value in counts]
    breakdown = ", ".join(f"{counts[name]} {name}" for name in ordered)
    repeats = sum(1 for f in findings if f.recurrence > 0)
    tail = f"; {repeats} previously flagged for this author" if repeats else ""
    return f"{len(findings)} finding(s) at score {score:.1f} — {breakdown}{tail}."


def rescore(review: Review, rubric: Rubric) -> Review:
    """Re-rate a stored review under a different rubric.

    Because the rubric is a pure function of stored findings, an entire history
    of reviews can be replayed under a new one — which is what makes changing
    the rubric a safe, measurable act rather than a break in continuity.
    """
    updated = review.model_copy(deep=True)
    updated.score = score_findings(updated.findings, review.score.loc, rubric)
    updated.review_id = digest("rescore", review.review_id, rubric.fingerprint())
    return updated
