"""Command line interface."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .collect import collect
from .knowledge import extract_conventions, ingest, load_comments
from .models import Severity
from .pipeline import build_submission, review_submission
from .providers.base import Detector
from .providers.claude import ClaudeDetector
from .providers.replay import ReplayDetector
from .scoring.rubric import DEFAULT_RUBRIC, explain_penalty
from .store.ledger import Ledger

app = typer.Typer(
    add_completion=False,
    help="Caliper — a reproducible code review authority. The model detects; code judges.",
)
console = Console()

DEFAULT_LEDGER = os.environ.get("CALIPER_LEDGER", ".caliper/ledger.db")

_SEVERITY_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}


def _detector(backend: str, seed: str, nonce: str = "") -> Detector:
    if backend == "replay":
        return ReplayDetector(seed=seed, nonce=nonce)
    project = os.environ.get("CALIPER_GCP_PROJECT")
    region = os.environ.get("CALIPER_GCP_REGION", "us-central1")
    model = os.environ.get("CALIPER_MODEL", "claude-opus-5")
    effort = os.environ.get("CALIPER_EFFORT", "high")
    # auto | structured | tool. `tool` skips the structured attempt entirely on
    # projects whose org policy is known to block structured outputs.
    output_mode = os.environ.get("CALIPER_OUTPUT_MODE", "auto")
    return ClaudeDetector(
        backend=backend,
        model=model,
        project_id=project,
        region=region,
        effort=effort,
        seed=seed,
        output_mode=output_mode,
    )


def _band_style(value: float) -> str:
    if value >= 90:
        return "bold green"
    if value >= 75:
        return "green"
    if value >= 60:
        return "yellow"
    return "bold red"


@app.command()
def review(
    paths: list[str] = typer.Argument(..., help="Files or directories to review."),
    author: str = typer.Option("anonymous", "--author", "-a", help="Who wrote this."),
    backend: str = typer.Option("vertex", "--backend", "-b", help="vertex | anthropic | replay"),
    passes: int = typer.Option(5, "--passes", "-k", help="Independent detection passes."),
    quorum: int | None = typer.Option(None, "--quorum", "-q", help="Votes required. Default 60%."),
    ledger_path: str = typer.Option(DEFAULT_LEDGER, "--ledger"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Force a fresh review."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full review as JSON."),
    fail_under: float | None = typer.Option(None, "--fail-under", help="Exit 1 below this score."),
) -> None:
    """Review code and produce a reproducible rating."""
    sources = collect(paths)
    if not sources:
        console.print("[red]No reviewable source files found.[/red]")
        raise typer.Exit(2)

    submission = build_submission(sources, author=author)
    with Ledger(ledger_path) as ledger:
        report = review_submission(
            submission,
            _detector(backend, seed=submission.content_hash),
            ledger=ledger,
            passes=passes,
            quorum=quorum,
            use_cache=not no_cache,
        )

    if as_json:
        console.print_json(report.review.model_dump_json())
        raise typer.Exit(0)

    _render(report, sources)
    if fail_under is not None and report.review.score.value < fail_under:
        console.print(f"\n[red]Score below threshold {fail_under}.[/red]")
        raise typer.Exit(1)


def _render(report, sources: dict[str, str]) -> None:
    review = report.review
    score = review.score
    style = _band_style(score.value)

    console.print(
        Panel(
            f"[{style}]{score.value:.1f}[/{style}]  [bold]{score.band}[/bold]\n{review.summary}",
            title=f"Caliper — {len(sources)} file(s), {score.loc} LOC, author {review.author}",
            subtitle=(
                f"rubric {score.rubric_version}·{score.rubric_hash[:8]}  "
                f"model {review.model_pin}  "
                f"{'CACHED — identical to a prior review' if review.cached else 'fresh'}"
            ),
        )
    )

    dims = Table(show_header=True, header_style="bold", title="Dimensions")
    dims.add_column("Category")
    dims.add_column("Score", justify="right")
    dims.add_column("Penalty", justify="right")
    dims.add_column("Findings", justify="right")
    for dimension in score.dimensions:
        dims.add_row(
            dimension.category.value,
            f"{dimension.score:.1f}",
            f"-{dimension.penalty:.2f}",
            str(dimension.finding_count),
        )
    console.print(dims)

    if not review.findings:
        console.print("[green]No grounded findings.[/green]")
    for finding in review.findings:
        severity_style = _SEVERITY_STYLE[finding.severity]
        head = (
            f"[{severity_style}]{finding.severity.value.upper()}[/{severity_style}] "
            f"[bold]{finding.title}[/bold]  [dim]({finding.category.value} · {finding.rule})[/dim]"
        )
        anchor = finding.anchor
        meta = (
            f"[dim]{anchor.path}:{anchor.start_line}-{anchor.end_line}"
            + (f" in {anchor.symbol.split('::')[-1]}" if anchor.symbol else "")
            + f" · verified {anchor.verified_by}"
            f" · {finding.votes}/{finding.passes} passes"
            f" · blast {finding.blast_radius:.0%} ({finding.dependents} dependents)"
            + (
                f" · [yellow]told {finding.recurrence}x before[/yellow]"
                if finding.recurrence
                else ""
            )
            + "[/dim]"
        )
        breakdown = explain_penalty(finding)
        body = (
            f"{finding.explanation}\n\n"
            f"[bold]Fix:[/bold] {finding.remediation}\n"
            f"[dim]penalty {breakdown['total']:.2f} = "
            f"{breakdown['severity_weight']:.1f} severity"
            f" × {breakdown['category_weight']:.2f} category"
            f" × {breakdown['impact_multiplier']:.2f} impact"
            f" × {breakdown['agreement_multiplier']:.2f} agreement"
            f" × {breakdown['confidence_multiplier']:.2f} confidence"
            f" × {breakdown['recurrence_multiplier']:.2f} recurrence[/dim]"
        )
        console.print(Panel(f"{meta}\n\n{body}", title=head, border_style=severity_style))

    console.print(
        f"[dim]discarded: {review.dropped_ungrounded} ungrounded claim(s), "
        f"{review.dropped_below_quorum} below quorum ({review.quorum}/{review.passes}). "
        f"detector precision {report.detector_precision:.0%}.[/dim]"
    )
    usage = report.usage
    if usage.input_tokens:
        console.print(
            f"[dim]tokens in {usage.input_tokens:,} out {usage.output_tokens:,}"
            f" · cache reads {usage.cache_read_tokens:,}"
            f" ({usage.cache_hit_rate:.0%} of cacheable)[/dim]"
        )
        if review.passes > 1 and not usage.cache_read_tokens and not usage.cache_write_tokens:
            # Silent no-op caching is worth surfacing: nothing errors, the bill
            # is just several times larger than it should be.
            console.print(
                "[yellow]Prompt caching appears inactive — nothing was written to or "
                "read from cache across passes. On Vertex this is usually an "
                "organisation policy restricting partner-model features.[/yellow]"
            )


@app.command()
def verify(
    paths: list[str] = typer.Argument(..., help="Files or directories to review."),
    runs: int = typer.Option(3, "--runs", "-n", help="Independent cold reviews to compare."),
    passes: int = typer.Option(5, "--passes", "-k"),
    backend: str = typer.Option("replay", "--backend", "-b"),
    author: str = typer.Option("verify", "--author", "-a"),
) -> None:
    """Measure reproducibility: review the same code N times and compare.

    This is the central claim under test. A rating authority that cannot show
    its own variance is asking to be taken on faith.
    """
    sources = collect(paths)
    if not sources:
        console.print("[red]No reviewable source files found.[/red]")
        raise typer.Exit(2)

    submission = build_submission(sources, author=author)
    scores: list[float] = []
    penalties: list[float] = []
    finding_sets: list[frozenset[str]] = []

    with tempfile.TemporaryDirectory(prefix="caliper-verify-") as scratch:
        # Each cold run gets its own ledger. Sharing one would let recurrence
        # counts accumulate between runs, and we would be measuring history
        # drift rather than detector variance.
        for run in range(runs):
            with Ledger(Path(scratch) / f"cold{run}.db") as ledger:
                report = review_submission(
                    submission,
                    _detector(backend, seed=submission.content_hash, nonce=f"run{run}"),
                    ledger=ledger,
                    passes=passes,
                    use_cache=False,
                )
            review = report.review
            scores.append(review.score.value)
            penalties.append(review.score.total_penalty)
            finding_sets.append(frozenset(f.fingerprint for f in review.findings))
            console.print(
                f"  run {run + 1}: score [bold]{review.score.value:6.2f}[/bold]  "
                f"penalty {review.score.total_penalty:7.2f}  "
                f"{len(review.findings)} findings  "
                f"[dim]({review.dropped_below_quorum} below quorum, "
                f"{review.dropped_ungrounded} ungrounded)[/dim]"
            )

        # Tier 1 under controlled conditions: identical inputs, identical
        # ledger state, twice.
        with Ledger(Path(scratch) / "exact.db") as ledger:

            def once() -> object:
                return review_submission(
                    submission,
                    _detector(backend, seed=submission.content_hash, nonce="exact"),
                    ledger=ledger,
                    passes=passes,
                    use_cache=True,
                ).review

            first, second = once(), once()

    def without_cache_flag(review) -> str:
        payload = review.model_dump()
        payload.pop("cached", None)
        return json.dumps(payload, sort_keys=True, default=str)

    byte_identical = without_cache_flag(first) == without_cache_flag(second)

    spread = max(scores) - min(scores)
    stdev = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    penalty_spread = max(penalties) - min(penalties)
    union = set().union(*finding_sets) if finding_sets else set()
    intersection = set.intersection(*(set(s) for s in finding_sets)) if finding_sets else set()
    jaccard = len(intersection) / len(union) if union else 1.0
    clamped = all(s <= 0.0 for s in scores) or all(s >= 100.0 for s in scores)

    table = Table(title="Reproducibility", show_header=False)
    table.add_row("cold-run scores", ", ".join(f"{s:.2f}" for s in scores))
    table.add_row("score spread", f"{spread:.2f} points  (sd {stdev:.2f})")
    table.add_row("raw penalty spread", f"{penalty_spread:.2f}")
    table.add_row("finding-set stability", f"{jaccard:.0%} agreed by every run")
    table.add_row(
        "repeat review identical",
        "[green]yes — byte for byte[/green]" if byte_identical else "[red]no[/red]",
    )
    table.add_row("second review used cache", "yes" if second.cached else "no")
    console.print(table)

    if clamped:
        console.print(
            "[yellow]Every run hit the score floor, so the score spread is not "
            "informative here — read the raw penalty spread instead.[/yellow]"
        )

    console.print(
        Panel(
            "[bold]Tier 1 — exact.[/bold] Same bytes, same rubric, same model pin, same "
            "author history returns the stored review verbatim: "
            f"{'verified byte-identical above' if byte_identical else 'FAILED'}. No model call.\n"
            f"[bold]Tier 2 — stable.[/bold] Cold re-runs differ by {penalty_spread:.2f} penalty "
            f"({spread:.2f} score points, sd {stdev:.2f}) because the detector is a sampler and "
            f"there is no temperature knob to turn down. Quorum absorbs it; "
            f"{jaccard:.0%} of findings were agreed by every run. Raise --passes to tighten.\n"
            "[bold]Tier 3 — comparable.[/bold] Two scores carrying the same rubric hash mean the "
            "same thing, because both are arithmetic over independently verified findings.",
            title="What is actually guaranteed",
            border_style="cyan",
        )
    )


@app.command()
def history(
    author: str = typer.Argument(..., help="Author to report on."),
    ledger_path: str = typer.Option(DEFAULT_LEDGER, "--ledger"),
) -> None:
    """Show an author's score trend and their repeated mistakes."""
    with Ledger(ledger_path) as ledger:
        points = ledger.trend(author)
        repeats = ledger.repeat_offenders(author)
        profile = ledger.category_profile(author)

    if not points:
        console.print(f"[yellow]No reviews recorded for {author}.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Score history — {author}")
    table.add_column("When")
    table.add_column("Score", justify="right")
    table.add_column("Band")
    table.add_column("LOC", justify="right")
    table.add_column("Rubric")
    for point in points:
        table.add_row(
            point.created_at[:19].replace("T", " "),
            f"{point.score:.1f}",
            point.band,
            str(point.loc),
            point.rubric_hash[:8],
        )
    console.print(table)

    first, last = points[0].score, points[-1].score
    delta = last - first
    direction = "improving" if delta > 0 else ("declining" if delta < 0 else "flat")
    rubrics = {p.rubric_hash for p in points}
    caveat = (
        ""
        if len(rubrics) == 1
        else "  [yellow](spans multiple rubric versions — not directly comparable)[/yellow]"
    )
    console.print(
        f"Trend: [bold]{delta:+.1f}[/bold] points across "
        f"{len(points)} reviews — {direction}.{caveat}"
    )

    if repeats:
        repeat_table = Table(title="Repeated across submissions")
        repeat_table.add_column("Rule")
        repeat_table.add_column("Times told", justify="right")
        for rule, count in repeats:
            repeat_table.add_row(rule, str(count))
        console.print(repeat_table)
    if profile:
        console.print("Findings by category: " + ", ".join(f"{k} {v}" for k, v in profile.items()))


@app.command()
def ingest_history(
    comments: str = typer.Argument(..., help="JSONL or JSON array of past review comments."),
    backend: str = typer.Option("vertex", "--backend", "-b"),
    ledger_path: str = typer.Option(DEFAULT_LEDGER, "--ledger"),
) -> None:
    """Absorb an organisation's review history into reusable conventions."""
    records = load_comments(comments)
    if not records:
        console.print("[red]No usable comments found.[/red]")
        raise typer.Exit(2)
    console.print(f"Read {len(records)} comment(s) from {comments}.")

    detector = _detector(backend, seed="ingest")
    client = getattr(detector, "_client", None)
    if client is None:
        console.print("[red]Convention extraction requires the vertex or anthropic backend.[/red]")
        raise typer.Exit(2)

    conventions = extract_conventions(
        client, records, model=getattr(detector, "model", "claude-opus-5")
    )
    with Ledger(ledger_path) as ledger:
        ingest(ledger, conventions, source=comments)

    table = Table(title=f"Conventions extracted from {len(records)} comments")
    table.add_column("Id")
    table.add_column("Statement")
    table.add_column("Evidence", justify="right")
    for convention in conventions:
        table.add_row(
            convention.convention_id, convention.statement, str(convention.evidence_count)
        )
    console.print(table)
    console.print(
        "[dim]These are now injected into the cached prefix of every future review.[/dim]"
    )


@app.command()
def conventions(ledger_path: str = typer.Option(DEFAULT_LEDGER, "--ledger")) -> None:
    """Show the conventions currently applied to every review."""
    with Ledger(ledger_path) as ledger:
        rows = ledger.conventions()
    if not rows:
        console.print("[yellow]No conventions ingested yet. See `caliper ingest-history`.[/yellow]")
        raise typer.Exit(0)
    table = Table(title="Organisation conventions")
    table.add_column("Id")
    table.add_column("Statement")
    table.add_column("Category")
    table.add_column("Seen", justify="right")
    for row in rows:
        table.add_row(
            row["convention_id"], row["statement"], row["category"], str(row["occurrences"])
        )
    console.print(table)


@app.command()
def rubric(as_json: bool = typer.Option(False, "--json")) -> None:
    """Print the scoring rubric and its hash. Every score cites this."""
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "version": DEFAULT_RUBRIC.version,
                    "hash": DEFAULT_RUBRIC.fingerprint(),
                    "severity_weight": DEFAULT_RUBRIC.severity_weight,
                    "category_weight": DEFAULT_RUBRIC.category_weight,
                    "impact_gain": DEFAULT_RUBRIC.impact_gain,
                    "agreement_floor": DEFAULT_RUBRIC.agreement_floor,
                    "recurrence_gain": DEFAULT_RUBRIC.recurrence_gain,
                    "baseline_loc": DEFAULT_RUBRIC.baseline_loc,
                }
            )
        )
        raise typer.Exit(0)

    console.print(
        Panel(
            f"version [bold]{DEFAULT_RUBRIC.version}[/bold]   "
            f"hash [bold]{DEFAULT_RUBRIC.fingerprint()}[/bold]",
            title="Rubric",
        )
    )
    weights = Table(title="Severity weight")
    weights.add_column("Severity")
    weights.add_column("Weight", justify="right")
    for name, value in DEFAULT_RUBRIC.severity_weight.items():
        weights.add_row(name, f"{value:g}")
    console.print(weights)

    categories = Table(title="Category weight")
    categories.add_column("Category")
    categories.add_column("Weight", justify="right")
    for name, value in DEFAULT_RUBRIC.category_weight.items():
        categories.add_row(name, f"{value:g}")
    console.print(categories)

    console.print(
        f"penalty = severity × category"
        f" × (1 + {DEFAULT_RUBRIC.impact_gain:.2f}·blast_radius)"
        f" × ({DEFAULT_RUBRIC.agreement_floor:.2f}"
        f" + {1 - DEFAULT_RUBRIC.agreement_floor:.2f}·agreement)"
        f" × confidence"
        f" × (1 + {DEFAULT_RUBRIC.recurrence_gain:.2f}"
        f"·min(recurrence, {DEFAULT_RUBRIC.recurrence_cap}))"
    )
    console.print(
        f"score   = 100 − Σpenalty"
        f" / max(1, (LOC/{DEFAULT_RUBRIC.baseline_loc})"
        f"^{DEFAULT_RUBRIC.size_exponent:.2f})"
    )


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8080, "--port"),
) -> None:
    """Run the HTTP API."""
    import uvicorn

    uvicorn.run("caliper.api:api", host=host, port=port, log_level="info")


@app.command()
def stats(ledger_path: str = typer.Option(DEFAULT_LEDGER, "--ledger")) -> None:
    """Ledger summary."""
    with Ledger(ledger_path) as ledger:
        console.print(ledger.stats())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
