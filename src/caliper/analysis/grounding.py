"""Grounding: prove every claim against the bytes that were actually reviewed.

Language models hallucinate line numbers. Asking them not to does not work.
The fix is to make an unverifiable claim *inexpressible*: every finding must
carry the source text it is talking about, and that text is checked against
the file before the finding is allowed to exist.

Three outcomes, in descending order of trust:

  exact_quote       the quote is where the model said it was
  relocated_quote   the quote is real but the line numbers were wrong — we fix
                    them and keep the finding, because the *observation* was
                    sound even though the arithmetic was not
  symbol_span       the quote is a partial or lightly-paraphrased match inside
                    one symbol; we anchor to that symbol and keep it

Anything else is discarded and counted. That count is a quality metric for the
detector, reported on every review rather than swept up.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..hashing import span_fingerprint
from ..models import Anchor, RawFinding, SourceFile, Symbol
from .structure import owning_symbol

# Fraction of a quote's lines that must appear inside a single symbol for the
# fuzzy fallback to accept it. Tuned to admit reformatted quotes while
# rejecting invented ones.
FUZZY_THRESHOLD = 0.6

# How far from the claimed location a quote may be found and still count as
# "exact" rather than "relocated". Absorbs off-by-a-few drift without hiding
# genuinely wrong locations.
EXACT_TOLERANCE = 2


@dataclass
class GroundingResult:
    anchored: list[tuple[RawFinding, Anchor]]
    rejected: list[tuple[RawFinding, str]]

    @property
    def rejection_rate(self) -> float:
        total = len(self.anchored) + len(self.rejected)
        return len(self.rejected) / total if total else 0.0


def _normalized_lines(text: str) -> list[tuple[int, str]]:
    """(original 1-indexed line number, stripped text) for non-blank lines."""
    return [
        (number, stripped)
        for number, line in enumerate(text.splitlines(), start=1)
        if (stripped := line.strip())
    ]


def _find_contiguous(haystack: list[tuple[int, str]], needle: list[str]) -> tuple[int, int] | None:
    """Locate a contiguous run of normalized lines. Returns original line span."""
    if not needle or len(needle) > len(haystack):
        return None
    texts = [text for _, text in haystack]
    for start in range(len(texts) - len(needle) + 1):
        if texts[start : start + len(needle)] == needle:
            return haystack[start][0], haystack[start + len(needle) - 1][0]
    return None


def _best_symbol_overlap(
    needle: set[str], symbols: list[Symbol], file: SourceFile
) -> tuple[Symbol, float] | None:
    """The symbol whose body contains the largest share of the quote's lines."""
    best: tuple[Symbol, float] | None = None
    for symbol in symbols:
        body = {
            text
            for _, text in _normalized_lines(file.line_slice(symbol.start_line, symbol.end_line))
        }
        if not body:
            continue
        overlap = len(needle & body) / len(needle)
        if best is None or overlap > best[1]:
            best = (symbol, overlap)
    return best


def ground_finding(
    raw: RawFinding, file: SourceFile, symbols: list[Symbol]
) -> tuple[Anchor | None, str]:
    """Verify one claim. Returns (anchor, reason) — anchor is None if rejected."""
    quote_lines = [text for _, text in _normalized_lines(raw.quoted_source)]
    if not quote_lines:
        return None, "empty_quote"

    file_lines = _normalized_lines(file.text)
    located = _find_contiguous(file_lines, quote_lines)

    if located is not None:
        start, end = located
        drift = abs(start - raw.start_line)
        symbol = owning_symbol(symbols, start)
        return (
            Anchor(
                path=file.path,
                start_line=start,
                end_line=end,
                span_fingerprint=span_fingerprint(file.line_slice(start, end)),
                symbol=symbol.qualified_name if symbol else None,
                verified_by="exact_quote" if drift <= EXACT_TOLERANCE else "relocated_quote",
            ),
            "verbatim" if drift <= EXACT_TOLERANCE else f"relocated_{drift}_lines",
        )

    # The quote is not present verbatim. Fall back to per-symbol containment so
    # a lightly reformatted quote is not thrown away.
    best = _best_symbol_overlap(set(quote_lines), symbols, file)
    if best and best[1] >= FUZZY_THRESHOLD:
        symbol, overlap = best
        return (
            Anchor(
                path=file.path,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                span_fingerprint=span_fingerprint(
                    file.line_slice(symbol.start_line, symbol.end_line)
                ),
                symbol=symbol.qualified_name,
                verified_by="symbol_span",
            ),
            f"fuzzy_symbol_match_{overlap:.2f}",
        )

    return None, "quote_not_found_in_source"


def ground_all(
    raws: list[RawFinding],
    files: dict[str, SourceFile],
    symbols: dict[str, list[Symbol]],
) -> GroundingResult:
    anchored: list[tuple[RawFinding, Anchor]] = []
    rejected: list[tuple[RawFinding, str]] = []

    for raw in raws:
        file = files.get(raw.path)
        if file is None:
            # The model named a file that was not in the submission at all.
            rejected.append((raw, "unknown_path"))
            continue
        anchor, reason = ground_finding(raw, file, symbols.get(raw.path, []))
        if anchor is None:
            rejected.append((raw, reason))
        else:
            anchored.append((raw, anchor))

    return GroundingResult(anchored=anchored, rejected=rejected)
