"""Language identification and per-language structural hints.

Caliper is polyglot by design, but it is honest about the difference between
languages it can parse exactly and languages it reads heuristically. That
distinction is carried on every `Symbol` as `exact`, and it propagates into
how much the impact model is allowed to move a score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
}


@dataclass(frozen=True)
class LanguageProfile:
    """What we know about how to read a language without a full grammar."""

    name: str
    exact_parser: bool = False
    line_comment: str = "//"
    # Regexes are intentionally shallow: they find declaration *lines*, which is
    # all the impact graph needs. They are not a substitute for a parser and are
    # never used to decide whether code is correct.
    symbol_patterns: tuple[tuple[str, str], ...] = ()
    import_patterns: tuple[str, ...] = ()
    entrypoint_hints: tuple[str, ...] = field(default=())


PROFILES: dict[str, LanguageProfile] = {
    "python": LanguageProfile(
        name="python",
        exact_parser=True,  # stdlib `ast`
        line_comment="#",
        import_patterns=(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",),
        entrypoint_hints=("main", "handler", "app"),
    ),
    "javascript": LanguageProfile(
        name="javascript",
        symbol_patterns=(
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
            (r"^\s*(?:export\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(", "function"),
        ),
        import_patterns=(
            r"^\s*import\s+.*?from\s+['\"]([^'\"]+)['\"]",
            r"require\(\s*['\"]([^'\"]+)['\"]\s*\)",
        ),
    ),
    "go": LanguageProfile(
        name="go",
        symbol_patterns=(
            (r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)", "function"),
            (r"^\s*type\s+(\w+)\s+struct", "class"),
        ),
        import_patterns=(r"^\s*(?:import\s+)?[\w.]*\s*\"([\w./-]+)\"",),
        entrypoint_hints=("main", "ServeHTTP"),
    ),
    "java": LanguageProfile(
        name="java",
        symbol_patterns=(
            (r"^\s*(?:public|private|protected).*?\bclass\s+(\w+)", "class"),
            (
                r"^\s*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\(",
                "method",
            ),
        ),
        import_patterns=(r"^\s*import\s+(?:static\s+)?([\w.]+);",),
        entrypoint_hints=("main",),
    ),
    "ruby": LanguageProfile(
        name="ruby",
        line_comment="#",
        symbol_patterns=((r"^\s*def\s+(\w+)", "method"), (r"^\s*class\s+(\w+)", "class")),
        import_patterns=(r"^\s*require(?:_relative)?\s+['\"]([^'\"]+)['\"]",),
    ),
    "rust": LanguageProfile(
        name="rust",
        symbol_patterns=(
            (r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", "function"),
            (r"^\s*(?:pub\s+)?struct\s+(\w+)", "class"),
        ),
        import_patterns=(r"^\s*use\s+([\w:]+)",),
    ),
    "c": LanguageProfile(
        name="c",
        symbol_patterns=((r"^\s*[\w*]+\s+\**(\w+)\s*\([^;]*\)\s*\{", "function"),),
        import_patterns=(r'^\s*#include\s+[<"]([^>"]+)[>"]',),
        entrypoint_hints=("main",),
    ),
    "php": LanguageProfile(
        name="php",
        symbol_patterns=((r"^\s*function\s+(\w+)", "function"), (r"^\s*class\s+(\w+)", "class")),
        import_patterns=(r"^\s*(?:require|include)(?:_once)?\s*\(?['\"]([^'\"]+)['\"]",),
    ),
}

# Languages sharing a profile with a close relative.
_ALIASES = {
    "typescript": "javascript",
    "cpp": "c",
    "csharp": "java",
    "kotlin": "java",
    "scala": "java",
    "swift": "java",
}

_FALLBACK = LanguageProfile(name="unknown")


def detect(path: str) -> str:
    """Language for a path, by extension. `unknown` is a valid answer."""
    lowered = path.lower()
    for ext, lang in sorted(EXTENSIONS.items(), key=lambda kv: -len(kv[0])):
        if lowered.endswith(ext):
            return lang
    return "unknown"


def profile(language: str) -> LanguageProfile:
    resolved = _ALIASES.get(language, language)
    prof = PROFILES.get(resolved, _FALLBACK)
    if resolved != language:
        # Keep the real language name so reports do not claim TypeScript is JS.
        return LanguageProfile(
            name=language,
            exact_parser=prof.exact_parser,
            line_comment=prof.line_comment,
            symbol_patterns=prof.symbol_patterns,
            import_patterns=prof.import_patterns,
            entrypoint_hints=prof.entrypoint_hints,
        )
    return prof


def supported() -> list[str]:
    return sorted(set(EXTENSIONS.values()))
