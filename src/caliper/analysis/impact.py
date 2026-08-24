"""Blast radius: how much of the system depends on this code.

The problem every existing tool shares is that it scores a file in isolation.
A hardcoded credential in a throwaway migration script and the same credential
in an auth helper that forty modules import are not the same defect, and any
rating that calls them equal is not measuring what engineers actually triage on.

This module answers one question — *what fraction of the submission
transitively reaches this file* — using pure graph math. No model involvement,
so the answer is identical on every run by construction.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass

from ..models import SourceFile
from .structure import imports_of

_INDEX_FILES = ("__init__", "index", "mod", "main")


def _strip_extension(path: str) -> str:
    base, _, _ = path.rpartition(".")
    return base or path


def _candidate_keys(path: str) -> set[str]:
    """Every name by which a file might be imported.

    A submission is rarely rooted where its import paths are: files collected
    as `examples/demo/svc/auth.py` are imported as `svc.auth`. So every
    trailing sub-path is a candidate key, and ambiguous ones are discarded by
    the caller rather than resolved arbitrarily.
    """
    without_ext = _strip_extension(path)
    keys = {without_ext, path}

    directory, _, filename = without_ext.rpartition("/")
    if filename in _INDEX_FILES and directory:
        # `pkg/__init__.py` is imported as `pkg`.
        keys.add(directory)

    for base in list(keys):
        parts = base.split("/")
        for start in range(1, len(parts)):
            keys.add("/".join(parts[start:]))

    return {key.strip("/") for key in keys if key.strip("/")}


def _normalize_module(module: str) -> str:
    return module.replace("::", "/").replace(".", "/").strip("/")


@dataclass
class ImpactGraph:
    """Directed graph of file -> files it imports, plus derived metrics."""

    files: list[str]
    edges: dict[str, set[str]]
    reverse: dict[str, set[str]]
    unresolved: dict[str, list[str]]

    def transitive_dependents(self, path: str) -> set[str]:
        """Every file that reaches `path`, directly or indirectly."""
        seen: set[str] = set()
        stack = list(self.reverse.get(path, ()))
        while stack:
            current = stack.pop()
            if current in seen or current == path:
                continue
            seen.add(current)
            stack.extend(self.reverse.get(current, ()))
        return seen

    def dependents(self, path: str) -> int:
        return len(self.transitive_dependents(path))

    def blast_radius(self, path: str) -> float:
        """Fraction of the rest of the submission that depends on this file.

        Bounded [0, 1] so it composes cleanly into the rubric as a multiplier
        and cannot make a score unbounded. A lone file scores 0 — correctly, as
        nothing else can be broken by changing it.
        """
        others = len(self.files) - 1
        if others <= 0:
            return 0.0
        return min(1.0, self.dependents(path) / others)

    def is_leaf(self, path: str) -> bool:
        return not self.reverse.get(path)

    def summary(self) -> dict[str, float]:
        return {path: round(self.blast_radius(path), 4) for path in sorted(self.files)}


def build_graph(files: list[SourceFile]) -> ImpactGraph:
    # Build key -> candidates first, then keep only unambiguous keys. Guessing
    # between two files that answer to `utils` would make the graph depend on
    # iteration order, and a wrong edge silently mis-weights a real finding.
    candidates: dict[str, set[str]] = defaultdict(set)
    for file in files:
        for key in _candidate_keys(file.path):
            candidates[key].add(file.path)
    lookup: dict[str, str] = {
        key: next(iter(paths)) for key, paths in candidates.items() if len(paths) == 1
    }
    # A file's own full path is always unambiguous and must never be lost.
    for file in files:
        lookup[_strip_extension(file.path).strip("/")] = file.path
        lookup[file.path] = file.path

    edges: dict[str, set[str]] = {file.path: set() for file in files}
    reverse: dict[str, set[str]] = defaultdict(set)
    unresolved: dict[str, list[str]] = {}

    for file in files:
        misses: list[str] = []
        for module in imports_of(file):
            target = _resolve(module, file.path, lookup)
            if target and target != file.path:
                edges[file.path].add(target)
                reverse[target].add(file.path)
            elif target is None:
                # External dependency (stdlib, third party). Not a miss worth
                # reporting, but kept so the graph can be explained.
                misses.append(module)
        if misses:
            unresolved[file.path] = misses

    return ImpactGraph(
        files=[file.path for file in files],
        edges=edges,
        reverse=dict(reverse),
        unresolved=unresolved,
    )


def _resolve(module: str, importer: str, lookup: dict[str, str]) -> str | None:
    """Map an import string to a file in the submission, or None if external."""
    if module.startswith("."):
        # Python-style relative import: each leading dot walks up one package.
        level = len(module) - len(module.lstrip("."))
        remainder = _normalize_module(module.lstrip("."))
        base = posixpath.dirname(importer)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        candidate = posixpath.normpath(posixpath.join(base, remainder)) if remainder else base
        return lookup.get(candidate.strip("/"))

    if module.startswith("./") or module.startswith("../"):
        base = posixpath.dirname(importer)
        candidate = posixpath.normpath(posixpath.join(base, module))
        return lookup.get(_strip_extension(candidate).strip("/")) or lookup.get(
            candidate.strip("/")
        )

    normalized = _normalize_module(module)
    if normalized in lookup:
        return lookup[normalized]

    # An import may name a package prefix the submission does not root at
    # (`myapp.svc.auth` where the tree starts at `svc/`). Try progressively
    # shorter suffixes, longest first, so the most specific match wins.
    parts = normalized.split("/")
    for start in range(1, len(parts)):
        suffix = "/".join(parts[start:])
        if suffix in lookup:
            return lookup[suffix]
    return None
