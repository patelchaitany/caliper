"""Gathering files to review.

Deliberately conservative: a review is only as trustworthy as its inputs, and
silently pulling in vendored dependencies or minified bundles would both wreck
the impact graph and waste most of the context on code nobody wrote.
"""

from __future__ import annotations

from pathlib import Path

from .languages import detect

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    "coverage",
    ".terraform",
    ".caliper",
    "site-packages",
    ".tox",
    ".gradle",
    "bin",
    "obj",
}

SKIP_SUFFIXES = (".min.js", ".min.css", ".lock", ".sum", ".pb.go", "_pb2.py")

MAX_FILE_BYTES = 400_000


def collect(paths: list[str], max_files: int = 200) -> dict[str, str]:
    """Map of relative path -> text, sorted and deduplicated."""
    root = Path.cwd()
    found: dict[str, str] = {}

    def consider(file: Path) -> None:
        if len(found) >= max_files:
            return
        name = file.name
        if name.startswith(".") or any(name.endswith(s) for s in SKIP_SUFFIXES):
            return
        if detect(str(file)) == "unknown":
            return
        try:
            if file.stat().st_size > MAX_FILE_BYTES:
                return
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return  # binary or unreadable: not reviewable source
        try:
            key = str(file.resolve().relative_to(root))
        except ValueError:
            key = str(file)
        found[key] = text

    for raw in paths:
        target = Path(raw)
        if target.is_file():
            consider(target)
        elif target.is_dir():
            for file in sorted(target.rglob("*")):
                if not file.is_file():
                    continue
                if any(part in SKIP_DIRS for part in file.parts):
                    continue
                consider(file)

    return dict(sorted(found.items()))
