from __future__ import annotations

from io import StringIO
from tokenize import (
    COMMENT,
    DEDENT,
    ENDMARKER,
    INDENT,
    NEWLINE,
    NL,
    TokenError,
    generate_tokens,
)


_LAYOUT_TOKENS = frozenset((COMMENT, DEDENT, ENDMARKER, INDENT, NEWLINE, NL))


def _python_tokens(source: str) -> tuple[tuple[int, str], ...]:
    tokens: list[tuple[int, str]] = []
    try:
        for token in generate_tokens(StringIO(source).readline):
            if token.type not in _LAYOUT_TOKENS:
                tokens.append((token.type, token.string))
    except TokenError as exc:
        if not exc.args or not str(exc.args[0]).endswith("EOF in multi-line statement"):
            raise
    return tuple(tokens)


def python_source_contains(source: str, snippet: str) -> bool:
    """Match a Python token sequence without coupling a test to source layout."""

    source_tokens = _python_tokens(source)
    snippet_tokens = _python_tokens(snippet)
    if not snippet_tokens:
        raise ValueError("Python source snippet must contain at least one token")
    width = len(snippet_tokens)
    return any(
        source_tokens[index : index + width] == snippet_tokens
        for index in range(len(source_tokens) - width + 1)
    )
