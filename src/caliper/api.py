"""HTTP API.

Deliberately thin. Everything interesting happens in `pipeline`, and the API
adds nothing to it — no re-ranking, no re-scoring, no per-caller tuning. A
rating authority whose answer depends on which endpoint you asked is not one.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .models import Review
from .pipeline import build_submission, review_submission
from .providers.base import Detector
from .providers.claude import ClaudeDetector
from .providers.gemini import GeminiDetector
from .providers.replay import ReplayDetector
from .scoring.rubric import DEFAULT_RUBRIC
from .store.ledger import Ledger

api = FastAPI(
    title="Caliper",
    version="0.1.0",
    description="A reproducible code review authority. The model detects; code judges.",
)

LEDGER_PATH = os.environ.get("CALIPER_LEDGER", ".caliper/ledger.db")
BACKEND = os.environ.get("CALIPER_BACKEND", "vertex")


def get_ledger():
    ledger = Ledger(LEDGER_PATH)
    try:
        yield ledger
    finally:
        ledger.close()


def make_detector(seed: str) -> Detector:
    if BACKEND == "replay":
        return ReplayDetector(seed=seed, nonce=os.environ.get("CALIPER_NONCE", "fixed"))
    if BACKEND == "gemini":
        return GeminiDetector(
            model=os.environ.get("CALIPER_MODEL", "gemini-2.5-pro"),
            project_id=os.environ.get("CALIPER_GCP_PROJECT"),
            region=os.environ.get("CALIPER_GCP_REGION", "global"),
            seed=seed,
        )
    return ClaudeDetector(
        backend=BACKEND,
        model=os.environ.get("CALIPER_MODEL", "claude-opus-5"),
        project_id=os.environ.get("CALIPER_GCP_PROJECT"),
        region=os.environ.get("CALIPER_GCP_REGION", "us-central1"),
        effort=os.environ.get("CALIPER_EFFORT", "high"),
        output_mode=os.environ.get("CALIPER_OUTPUT_MODE", "auto"),
        seed=seed,
    )


class ReviewRequest(BaseModel):
    author: str = Field(description="Identity the review is recorded against.")
    files: dict[str, str] = Field(description="Map of path -> full file text.")
    passes: int = Field(default=5, ge=1, le=9)
    quorum: int | None = Field(default=None, ge=1, le=9)
    use_cache: bool = True


class ReviewResponse(BaseModel):
    review: Review
    cached: bool
    detector_precision: float
    blast_radius: dict[str, float]
    quorum_required: int


@api.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "backend": BACKEND, "rubric": DEFAULT_RUBRIC.fingerprint()}


@api.get("/v1/rubric")
def get_rubric() -> dict[str, Any]:
    """The scoring function, in full. Every score cites this hash."""
    return {
        "version": DEFAULT_RUBRIC.version,
        "hash": DEFAULT_RUBRIC.fingerprint(),
        "severity_weight": DEFAULT_RUBRIC.severity_weight,
        "category_weight": DEFAULT_RUBRIC.category_weight,
        "impact_gain": DEFAULT_RUBRIC.impact_gain,
        "agreement_floor": DEFAULT_RUBRIC.agreement_floor,
        "recurrence_gain": DEFAULT_RUBRIC.recurrence_gain,
        "recurrence_cap": DEFAULT_RUBRIC.recurrence_cap,
        "baseline_loc": DEFAULT_RUBRIC.baseline_loc,
        "bands": [list(band) for band in DEFAULT_RUBRIC.bands],
    }


@api.post("/v1/reviews", response_model=ReviewResponse)
def create_review(request: ReviewRequest, ledger: Ledger = Depends(get_ledger)) -> ReviewResponse:
    if not request.files:
        raise HTTPException(status_code=400, detail="no files supplied")

    submission = build_submission(request.files, author=request.author)
    try:
        report = review_submission(
            submission,
            make_detector(submission.content_hash),
            ledger=ledger,
            passes=request.passes,
            quorum=request.quorum,
            use_cache=request.use_cache,
        )
    except ValueError as exc:  # misconfiguration, e.g. no GCP project set
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:  # refusal, truncation, malformed detection
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ReviewResponse(
        review=report.review,
        cached=report.review.cached,
        detector_precision=round(report.detector_precision, 4),
        blast_radius=report.graph.summary(),
        quorum_required=report.quorum_required,
    )


@api.get("/v1/reviews/{review_id}", response_model=Review)
def get_review(review_id: str, ledger: Ledger = Depends(get_ledger)) -> Review:
    review = ledger.find_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="no such review")
    return review


@api.get("/v1/authors/{author}/history")
def get_history(author: str, ledger: Ledger = Depends(get_ledger)) -> dict[str, Any]:
    points = ledger.trend(author)
    return {
        "author": author,
        "reviews": len(points),
        "trend": [
            {
                "at": point.created_at,
                "score": point.score,
                "band": point.band,
                "loc": point.loc,
                "rubric": point.rubric_hash,
                "submission": point.submission_id,
            }
            for point in points
        ],
        # Comparing scores across rubric versions is not meaningful; say so
        # rather than let a caller draw a trend line through a rubric change.
        "comparable": len({p.rubric_hash for p in points}) <= 1,
        "repeated_mistakes": [
            {"rule": rule, "times_told": count} for rule, count in ledger.repeat_offenders(author)
        ],
        "by_category": ledger.category_profile(author),
    }


@api.get("/v1/conventions")
def get_conventions(ledger: Ledger = Depends(get_ledger)) -> dict[str, Any]:
    return {
        "conventions": [
            {
                "id": row["convention_id"],
                "statement": row["statement"],
                "rationale": row["rationale"],
                "category": row["category"],
                "occurrences": row["occurrences"],
            }
            for row in ledger.conventions()
        ]
    }


@api.get("/v1/stats")
def get_stats(ledger: Ledger = Depends(get_ledger)) -> dict[str, int]:
    return ledger.stats()
