"""Grounding is the defence against invented findings. These tests are the spec."""

from conftest import raw_finding

from caliper.analysis.grounding import ground_all, ground_finding
from caliper.analysis.structure import symbols_of


def test_accurate_claim_is_anchored_exactly(source_file):
    anchor, reason = ground_finding(raw_finding(), source_file, symbols_of(source_file))
    assert anchor is not None
    assert anchor.verified_by == "exact_quote"
    assert (anchor.start_line, anchor.end_line) == (2, 2)
    assert anchor.symbol == "svc/auth.py::login"
    assert reason == "verbatim"


def test_hallucinated_line_number_is_corrected_not_trusted(source_file):
    """The observation is real; the arithmetic is not. Keep one, fix the other."""
    anchor, reason = ground_finding(
        raw_finding(start_line=417, end_line=417), source_file, symbols_of(source_file)
    )
    assert anchor is not None
    assert anchor.verified_by == "relocated_quote"
    assert anchor.start_line == 2, "should snap to where the quote actually is"
    assert "relocated" in reason


def test_invented_finding_is_discarded(source_file):
    anchor, reason = ground_finding(
        raw_finding(quoted_source="os.system(request.args['cmd'])"),
        source_file,
        symbols_of(source_file),
    )
    assert anchor is None
    assert reason == "quote_not_found_in_source"


def test_reformatted_quote_survives_via_symbol_overlap(source_file):
    """A quote the model tidied up should not be thrown away outright."""
    anchor, _ = ground_finding(
        raw_finding(quoted_source="def login(user):\n    return db.exec(q)"),
        source_file,
        symbols_of(source_file),
    )
    assert anchor is not None
    assert anchor.verified_by == "symbol_span"
    assert anchor.symbol == "svc/auth.py::login"


def test_whitespace_only_change_still_grounds(source_file):
    anchor, _ = ground_finding(
        raw_finding(quoted_source='q = "SELECT * FROM u WHERE n=\'" + user + "\'"'),
        source_file,
        symbols_of(source_file),
    )
    assert anchor is not None


def test_empty_quote_is_rejected(source_file):
    anchor, reason = ground_finding(
        raw_finding(quoted_source="   \n  "), source_file, symbols_of(source_file)
    )
    assert anchor is None
    assert reason == "empty_quote"


def test_unknown_path_is_rejected(source_file):
    result = ground_all(
        [raw_finding(path="does/not/exist.py")],
        {source_file.path: source_file},
        {source_file.path: symbols_of(source_file)},
    )
    assert result.anchored == []
    assert result.rejected[0][1] == "unknown_path"
    assert result.rejection_rate == 1.0
