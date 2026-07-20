from __future__ import annotations

from pathlib import Path
from typing import Iterable


_NON_CODE_VERIFY_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".rst",
        ".txt",
        ".text",
        ".adoc",
        ".asciidoc",
        ".org",
        ".log",
        ".csv",
        ".tsv",
        ".mdown",
        ".mkd",
    }
)

_NON_CODE_VERIFY_FILENAMES = frozenset(
    {
        "license",
        "licence",
        "notice",
        "authors",
        "contributors",
        "changelog",
        "codeowners",
    }
)


def is_non_code_path(raw: str) -> bool:
    try:
        path = Path(str(raw))
    except TypeError:
        return False
    suffix = path.suffix.lower()
    if suffix in _NON_CODE_VERIFY_EXTENSIONS:
        return True
    return not suffix and path.name.lower() in _NON_CODE_VERIFY_FILENAMES


def filter_verifiable_paths(paths: Iterable[str]) -> list[str]:
    return [path for path in paths if path and not is_non_code_path(path)]
