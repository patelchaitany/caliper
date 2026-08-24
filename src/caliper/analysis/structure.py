"""Extract the structure of a file: its symbols and what it depends on.

Two tiers, and the difference is recorded rather than hidden:

  * Python goes through the stdlib `ast`, so spans are exact.
  * Everything else uses declaration-line heuristics from the language
    profile, producing symbols marked `exact=False`.

Nothing here decides whether code is *good*. Structure only answers "what is
this and what reaches it", which is the input the impact model needs and the
coordinate system findings are anchored in.
"""

from __future__ import annotations

import ast
import re

from ..languages import detect, profile
from ..models import SourceFile, Symbol


def _python_symbols(file: SourceFile) -> list[Symbol]:
    try:
        tree = ast.parse(file.text)
    except SyntaxError:
        # A file that does not parse still gets reviewed — we simply lose the
        # exact coordinate system and fall back to heuristics.
        return _heuristic_symbols(file)

    symbols: list[Symbol] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_class = isinstance(child, ast.ClassDef)
                kind = "class" if is_class else ("method" if prefix else "function")
                qualified = f"{prefix}.{child.name}" if prefix else child.name
                symbols.append(
                    Symbol(
                        name=child.name,
                        kind=kind,
                        path=file.path,
                        start_line=child.lineno,
                        end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                        qualified_name=f"{file.path}::{qualified}",
                        exact=True,
                    )
                )
                walk(child, qualified)

    walk(tree, "")
    return symbols


def _python_imports(file: SourceFile) -> list[str]:
    try:
        tree = ast.parse(file.text)
    except SyntaxError:
        return _heuristic_imports(file)

    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Relative imports carry their level so the resolver can walk up.
                modules.append("." * node.level + node.module)
            elif node.level:
                modules.append("." * node.level)
    return modules


def _heuristic_symbols(file: SourceFile) -> list[Symbol]:
    prof = profile(file.language)
    if not prof.symbol_patterns:
        return []

    lines = file.text.splitlines()
    hits: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines, start=1):
        if line.lstrip().startswith(prof.line_comment):
            continue
        for pattern, kind in prof.symbol_patterns:
            match = re.search(pattern, line)
            if match and match.group(1):
                hits.append((index, match.group(1), kind))
                break

    # Without a parser we cannot know where a declaration ends, so a symbol runs
    # until the next declaration. Crude, but stable and good enough to attribute
    # a line to an owner.
    symbols: list[Symbol] = []
    for position, (line_no, name, kind) in enumerate(hits):
        end = hits[position + 1][0] - 1 if position + 1 < len(hits) else len(lines)
        symbols.append(
            Symbol(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                path=file.path,
                start_line=line_no,
                end_line=max(line_no, end),
                qualified_name=f"{file.path}::{name}",
                exact=False,
            )
        )
    return symbols


def _heuristic_imports(file: SourceFile) -> list[str]:
    prof = profile(file.language)
    modules: list[str] = []
    for line in file.text.splitlines():
        for pattern in prof.import_patterns:
            match = re.search(pattern, line)
            if match:
                modules.extend(group for group in match.groups() if group)
    return modules


def symbols_of(file: SourceFile) -> list[Symbol]:
    """All named regions in a file, outermost first, in source order."""
    if file.language == "python":
        found = _python_symbols(file)
    else:
        found = _heuristic_symbols(file)
    return sorted(found, key=lambda s: (s.start_line, s.end_line))


def imports_of(file: SourceFile) -> list[str]:
    """Raw module references, unresolved. Resolution happens in the graph."""
    raw = _python_imports(file) if file.language == "python" else _heuristic_imports(file)
    seen: set[str] = set()
    ordered: list[str] = []
    for module in raw:
        if module not in seen:
            seen.add(module)
            ordered.append(module)
    return ordered


def owning_symbol(symbols: list[Symbol], line: int) -> Symbol | None:
    """The tightest symbol containing `line`.

    Innermost wins: a line inside a method belongs to the method, not the class,
    so that a finding fingerprints against the smallest meaningful unit.
    """
    best: Symbol | None = None
    for symbol in symbols:
        if symbol.start_line <= line <= symbol.end_line:
            if best is None or (symbol.end_line - symbol.start_line) < (
                best.end_line - best.start_line
            ):
                best = symbol
    return best


def make_source_file(path: str, text: str) -> SourceFile:
    from ..hashing import digest

    return SourceFile(
        path=path,
        text=text,
        language=detect(path),
        content_hash=digest("file", path, text),
    )
