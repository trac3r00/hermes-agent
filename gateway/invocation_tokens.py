from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Encoding:
    left: str
    right: str
    numeric: str = ""


@dataclass(frozen=True, slots=True)
class TokenMatch:
    start: int
    end: int
    encoding: Encoding


ENCODINGS: Final[tuple[Encoding, ...]] = (
    Encoding("&amp;lt;", "&amp;gt;"),
    Encoding("&#x3c;", "&#x3e;", "hex"),
    Encoding("&#60;", "&#62;", "decimal"),
    Encoding("&lt;", "&gt;"),
    Encoding("<", ">"),
)
_DECIMAL_LEFT_RE: Final[re.Pattern[str]] = re.compile(r"&#0*60;", re.IGNORECASE)
_DECIMAL_RIGHT_RE: Final[re.Pattern[str]] = re.compile(r"&#0*62;", re.IGNORECASE)
_HEX_LEFT_RE: Final[re.Pattern[str]] = re.compile(r"&#x0*3c;", re.IGNORECASE)
_HEX_RIGHT_RE: Final[re.Pattern[str]] = re.compile(r"&#x0*3e;", re.IGNORECASE)
def find_left(
    text: str,
    encodings: tuple[Encoding, ...] = ENCODINGS,
) -> TokenMatch | None:
    matches: list[TokenMatch] = []
    folded = text.casefold()
    for encoding in encodings:
        if encoding.numeric:
            pattern = _HEX_LEFT_RE if encoding.numeric == "hex" else _DECIMAL_LEFT_RE
            match = pattern.search(text)
            if match is not None:
                matches.append(TokenMatch(match.start(), match.end(), encoding))
            continue
        start = folded.find(encoding.left.casefold())
        if start >= 0:
            matches.append(TokenMatch(start, start + len(encoding.left), encoding))
    return min(
        matches,
        key=lambda match: (match.start, -(match.end - match.start)),
        default=None,
    )


def find_right(text: str, start: int, encoding: Encoding) -> tuple[int, int] | None:
    if encoding.numeric:
        pattern = _HEX_RIGHT_RE if encoding.numeric == "hex" else _DECIMAL_RIGHT_RE
        match = pattern.search(text, start)
        return (match.start(), match.end()) if match is not None else None
    index = text.casefold().find(encoding.right.casefold(), start)
    return (index, index + len(encoding.right)) if index >= 0 else None


def partial_left_suffix(
    text: str,
    encodings: tuple[Encoding, ...] = ENCODINGS,
) -> str:
    folded = text.casefold()
    best = ""
    for encoding in encodings:
        if encoding.numeric:
            continue
        token = encoding.left.casefold()
        for length in range(1, min(len(text), len(token) - 1) + 1):
            if folded.endswith(token[:length]) and length > len(best):
                best = text[-length:]
    ampersand = text.rfind("&")
    if ampersand >= 0:
        suffix = text[ampersand:]
        numeric_kinds = {encoding.numeric for encoding in encodings}
        body = suffix[2:].casefold() if suffix.startswith("&#") else None
        decimal_tail = body.lstrip("0") if body is not None else None
        hex_tail = (
            body[1:].lstrip("0")
            if body is not None and body.startswith("x")
            else None
        )
        if suffix == "&" or (
            body == "" and numeric_kinds
        ) or (
            "decimal" in numeric_kinds and decimal_tail in {"", "6", "60"}
        ) or (
            "hex" in numeric_kinds and hex_tail in {"", "3", "3c"}
        ):
            if len(suffix) > len(best):
                best = suffix
    return best


__all__ = ["ENCODINGS", "Encoding", "TokenMatch", "find_left", "find_right", "partial_left_suffix"]
