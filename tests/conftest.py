import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caliper.analysis.structure import make_source_file  # noqa: E402
from caliper.models import (  # noqa: E402
    Anchor,
    Category,
    Confidence,
    Finding,
    RawFinding,
    Severity,
)
from caliper.store.ledger import Ledger  # noqa: E402

VULNERABLE = {
    "svc/auth.py": (
        "import requests\n"
        "\n"
        'SESSION_SECRET = "prod-signing-key-9f2a"\n'
        "\n"
        "def verify(db, name, pw, seen=[]):\n"
        '    q = "SELECT id FROM users WHERE n = \'" + name + "\'"\n'
        "    seen.append(name)\n"
        "    return db.execute(q)\n"
    ),
    "svc/db.py": (
        "from svc.auth import verify\n"
        "\n"
        "def run(conn, sql):\n"
        "    try:\n"
        "        return conn.execute(sql)\n"
        "    except:\n"
        "        pass\n"
    ),
    "svc/api.py": (
        "from svc.db import run\n"
        "from svc.auth import verify\n"
        "\n"
        "def handler(r):\n"
        "    return run(r, r.sql)\n"
    ),
    "scripts/oneoff.py": 'DB_PASSWORD = "temp-backfill-pw"\n',
}


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as store:
        yield store


@pytest.fixture
def source_file():
    return make_source_file(
        "svc/auth.py",
        "def login(user):\n"
        '    q = "SELECT * FROM u WHERE n=\'" + user + "\'"\n'
        "    return db.exec(q)\n",
    )


def raw_finding(**overrides) -> RawFinding:
    base = dict(
        rule="sql_injection",
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        path="svc/auth.py",
        start_line=2,
        end_line=2,
        title="SQL injection",
        explanation="User text is concatenated into SQL.",
        remediation="Parameterise.",
        quoted_source='    q = "SELECT * FROM u WHERE n=\'" + user + "\'"',
    )
    base.update(overrides)
    return RawFinding(**base)


def finding(**overrides) -> Finding:
    base = dict(
        fingerprint="fp0",
        rule="sql_injection",
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        confidence=Confidence.CERTAIN,
        anchor=Anchor(
            path="svc/auth.py",
            start_line=2,
            end_line=2,
            span_fingerprint="span0",
            symbol="svc/auth.py::login",
            verified_by="exact_quote",
        ),
        title="SQL injection",
        explanation="e",
        remediation="r",
        votes=5,
        passes=5,
    )
    base.update(overrides)
    return Finding(**base)
