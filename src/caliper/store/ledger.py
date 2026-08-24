"""The ledger: every review Caliper has ever produced, addressed by content.

This is where three of the problem's requirements are actually met.

*Reproducibility, exactly.* A review is keyed by the hash of what produced it:
the submitted bytes, the rubric, the model pin and the author's history state.
Re-review the same code under the same conditions and you do not get a similar
answer, you get the identical stored one. No model call is made.

*Memory.* Reviews are stateless today because nothing writes them down. Every
finding is recorded against its author, so the fifth time someone ships the
same defect the system knows it is the fifth — and says so.

*Growth.* Scores are stored with their rubric hash, which makes a trend line
meaningful: two scores are only comparable if the thing that produced them was
the same, and the ledger can prove whether it was.

SQLite because a rating authority should be a file you can copy, diff and hand
to an auditor, not a service you have to trust.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..hashing import digest, hash_object
from ..models import Finding, Review

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id      TEXT PRIMARY KEY,
    submission_id  TEXT NOT NULL,
    author         TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    rubric_hash    TEXT NOT NULL,
    model_pin      TEXT NOT NULL,
    history_sig    TEXT NOT NULL,
    score          REAL NOT NULL,
    band           TEXT NOT NULL,
    loc            INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reviews_author_time ON reviews(author, created_at);
CREATE INDEX IF NOT EXISTS reviews_lookup ON reviews(content_hash, rubric_hash, model_pin);

CREATE TABLE IF NOT EXISTS finding_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id    TEXT NOT NULL,
    author       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    rule         TEXT NOT NULL,
    category     TEXT NOT NULL,
    severity     TEXT NOT NULL,
    path         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (review_id) REFERENCES reviews(review_id)
);

CREATE INDEX IF NOT EXISTS findings_author_rule ON finding_history(author, rule);
CREATE INDEX IF NOT EXISTS findings_fingerprint ON finding_history(fingerprint);
CREATE INDEX IF NOT EXISTS findings_content ON finding_history(author, content_hash);

CREATE TABLE IF NOT EXISTS conventions (
    convention_id TEXT PRIMARY KEY,
    statement     TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    category      TEXT NOT NULL,
    source        TEXT NOT NULL,
    occurrences   INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
"""


@dataclass
class TrendPoint:
    created_at: str
    score: float
    band: str
    loc: int
    rubric_hash: str
    submission_id: str


class Ledger:
    def __init__(self, path: str | Path = ".caliper/ledger.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reproducibility ---------------------------------------------------

    def history_signature(self, author: str, exclude_content: str = "") -> str:
        """Hash of everything about this author that could change a score.

        Recurrence escalates penalties, so an author's history is an *input* to
        the rubric. Folding it into the cache key keeps the guarantee honest: it
        would be a lie to return a cached score after the history that produced
        it moved on.

        `exclude_content` omits prior reviews of *this same submission*, and it
        is not optional for correctness. Without it, recording a review changes
        the history that keys it, so re-reviewing identical code would never hit
        the cache and — worse — an author would accumulate recurrence against
        themselves for a single unchanged submission reviewed twice.
        """
        rows = self.conn.execute(
            "SELECT rule, COUNT(DISTINCT review_id) AS n FROM finding_history "
            "WHERE author = ? AND content_hash != ? GROUP BY rule ORDER BY rule",
            (author, exclude_content),
        ).fetchall()
        return hash_object({row["rule"]: row["n"] for row in rows})

    def review_key(
        self, content_hash: str, rubric_hash: str, model_pin: str, history_sig: str
    ) -> str:
        return digest("review", content_hash, rubric_hash, model_pin, history_sig)

    def find_review(self, review_id: str) -> Review | None:
        row = self.conn.execute(
            "SELECT payload FROM reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        if row is None:
            return None
        review = Review.model_validate_json(row["payload"])
        review.cached = True
        return review

    def record(self, review: Review, history_sig: str) -> None:
        now = datetime.now(UTC).isoformat()
        payload = review.model_dump_json()
        self.conn.execute(
            "INSERT OR REPLACE INTO reviews (review_id, submission_id, author, "
            "content_hash, rubric_hash, model_pin, history_sig, score, band, loc, "
            "created_at, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review.review_id,
                review.submission_id,
                review.author,
                review.content_hash,
                review.score.rubric_hash,
                review.model_pin,
                history_sig,
                review.score.value,
                review.score.band,
                review.score.loc,
                now,
                payload,
            ),
        )
        # Replace rather than accumulate, so re-recording a review does not
        # inflate that author's recurrence counts.
        self.conn.execute("DELETE FROM finding_history WHERE review_id = ?", (review.review_id,))
        self.conn.executemany(
            "INSERT INTO finding_history (review_id, author, content_hash, "
            "fingerprint, rule, category, severity, path, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    review.review_id,
                    review.author,
                    review.content_hash,
                    finding.fingerprint,
                    finding.rule,
                    finding.category.value,
                    finding.severity.value,
                    finding.anchor.path,
                    now,
                )
                for finding in review.findings
            ],
        )
        self.conn.commit()

    # -- memory ------------------------------------------------------------

    def recurrence_for(
        self, author: str, rules: list[str], exclude_content: str = ""
    ) -> dict[str, int]:
        """How many *prior reviews* told this author about each rule.

        Counted per review, not per finding: three instances of one mistake in
        a single submission is one lesson, not three. Reviews of the same
        submission are excluded so resubmitting unchanged code cannot escalate
        its own penalties.
        """
        if not rules:
            return {}
        placeholders = ",".join("?" for _ in rules)
        rows = self.conn.execute(
            f"SELECT rule, COUNT(DISTINCT review_id) AS n FROM finding_history "
            f"WHERE author = ? AND content_hash != ? AND rule IN ({placeholders}) "
            f"GROUP BY rule",
            (author, exclude_content, *rules),
        ).fetchall()
        return {row["rule"]: row["n"] for row in rows}

    def annotate_recurrence(
        self, author: str, findings: list[Finding], exclude_content: str = ""
    ) -> list[Finding]:
        counts = self.recurrence_for(author, sorted({f.rule for f in findings}), exclude_content)
        for finding in findings:
            finding.recurrence = counts.get(finding.rule, 0)
        return findings

    def repeat_offenders(self, author: str, minimum: int = 2) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT rule, COUNT(DISTINCT review_id) AS n FROM finding_history "
            "WHERE author = ? GROUP BY rule HAVING n >= ? ORDER BY n DESC, rule",
            (author, minimum),
        ).fetchall()
        return [(row["rule"], row["n"]) for row in rows]

    def trend(self, author: str, limit: int = 50) -> list[TrendPoint]:
        rows = self.conn.execute(
            "SELECT created_at, score, band, loc, rubric_hash, submission_id "
            "FROM reviews WHERE author = ? ORDER BY created_at DESC LIMIT ?",
            (author, limit),
        ).fetchall()
        return [
            TrendPoint(
                created_at=row["created_at"],
                score=row["score"],
                band=row["band"],
                loc=row["loc"],
                rubric_hash=row["rubric_hash"],
                submission_id=row["submission_id"],
            )
            for row in reversed(rows)
        ]

    def category_profile(self, author: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) AS n FROM finding_history WHERE author = ? "
            "GROUP BY category ORDER BY n DESC",
            (author,),
        ).fetchall()
        return {row["category"]: row["n"] for row in rows}

    # -- institutional knowledge -------------------------------------------

    def upsert_convention(
        self,
        convention_id: str,
        statement: str,
        rationale: str,
        category: str,
        source: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO conventions (convention_id, statement, rationale, category, "
            "source, occurrences, created_at) VALUES (?,?,?,?,?,1,?) "
            "ON CONFLICT(convention_id) DO UPDATE SET occurrences = occurrences + 1",
            (convention_id, statement, rationale, category, source, now),
        )
        self.conn.commit()

    def conventions(self, limit: int = 40) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM conventions ORDER BY occurrences DESC, convention_id LIMIT ?",
            (limit,),
        ).fetchall()

    def conventions_block(self, limit: int = 40) -> str:
        """Render conventions for the cached half of the prompt.

        Sorted deterministically: an unstable ordering here would silently
        invalidate the prompt cache on every request.
        """
        rows = self.conventions(limit)
        if not rows:
            return ""
        return "\n".join(
            f"- [{row['convention_id']}] {row['statement']}\n"
            f"    rationale: {row['rationale']}  (seen {row['occurrences']}x in review history)"
            for row in rows
        )

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

        return {
            "reviews": count("reviews"),
            "findings": count("finding_history"),
            "conventions": count("conventions"),
            "authors": self.conn.execute(
                "SELECT COUNT(DISTINCT author) AS n FROM reviews"
            ).fetchone()["n"],
        }
