from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from gateway.invocation_tokens import (
    Encoding,
    find_left,
    find_right,
    partial_left_suffix,
)


@dataclass(frozen=True, slots=True)
class _Tag:
    name: str
    attributes: str
    closing: bool
    self_closing: bool


_INVOCATION_TAGS: Final[frozenset[str]] = frozenset(
    "invoke tool_call tool_calls tool_result function_call function_calls function".split()
)
_MAX_TAG_CHARS: Final[int] = 4096
_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)"
)
_NAME_ATTRIBUTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\bname\s*=", re.IGNORECASE
)
_BLOCK_BOUNDARY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\n\r.!?:])[ \t]*\Z"
)
_CALL_PREAMBLE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:^|(?<=[\n\r]))[ \t]*call[ \t]*(?:\r?\n[ \t]*)?\Z"
)
_CALL_PARTIAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:^|(?<=[\n\r]))[ \t]*c(?:a(?:l(?:l)?)?)?[ \t]*(?:\r?\n[ \t]*)?\Z"
)


class StreamingInvocationScrubber:
    _blocked: bool
    _buffer: str
    _visible_tail: str

    def __init__(self) -> None:
        self._blocked = False
        self._buffer = ""
        self._encoding: Encoding | None = None
        self._stack: list[str] = []
        self._visible_tail = ""

    def feed(self, text: str) -> str:
        if self._blocked:
            return ""
        self._buffer += text
        visible: list[str] = []

        while self._buffer:
            if self._stack:
                if not self._consume_private():
                    break
                continue

            candidate = find_left(self._buffer)
            if candidate is None:
                held = self._partial_left_suffix(self._buffer)
                call_partial = _CALL_PARTIAL_RE.search(self._buffer)
                if call_partial is not None and len(call_partial.group()) > len(held):
                    held = call_partial.group()
                if len(held) > _MAX_TAG_CHARS:
                    self._blocked = True
                    self._buffer = ""
                    break
                public_end = len(self._buffer) - len(held)
                public = self._buffer[:public_end]
                call_match = _CALL_PREAMBLE_RE.search(public)
                if call_match is not None:
                    public_end = call_match.start()
                    held = self._buffer[public_end:]
                    public = self._buffer[:public_end]
                visible.append(public)
                self._buffer = held
                break

            start, left_end, encoding = candidate.start, candidate.end, candidate.encoding
            right = find_right(self._buffer, left_end, encoding)
            if right is None:
                prefix = self._buffer[:start]
                call_match = _CALL_PREAMBLE_RE.search(prefix)
                keep_from = call_match.start() if call_match is not None else start
                visible.append(self._buffer[:keep_from])
                self._buffer = self._buffer[keep_from:]
                adjusted_left_end = left_end - keep_from
                header = self._buffer[adjusted_left_end:]
                if len(header) > _MAX_TAG_CHARS:
                    tag = self._parse_partial_tag(header)
                    if tag is not None and tag.name in _INVOCATION_TAGS:
                        self._stack.append(tag.name)
                        self._encoding = encoding
                        self._buffer = ""
                    elif self._looks_like_invocation_prefix(header):
                        self._blocked = True
                        self._buffer = ""
                    else:
                        visible.append(self._buffer[:adjusted_left_end])
                        self._buffer = self._buffer[adjusted_left_end:]
                        continue
                break

            prefix = self._buffer[:start]
            right_start, right_end = right
            tag_text = self._buffer[left_end:right_start]
            tag = self._parse_tag(tag_text)
            after = right_end
            if tag is None or not self._is_invocation(tag, "".join(visible) + prefix):
                visible.append(self._buffer[:after])
                self._buffer = self._buffer[after:]
                continue

            visible.append(_CALL_PREAMBLE_RE.sub("", prefix))
            self._buffer = self._buffer[after:]
            if not tag.self_closing:
                self._encoding = encoding
                self._stack.append(tag.name)

        return self._visible_result(visible)

    def finish(self) -> str:
        if self._blocked or self._stack:
            self.reset()
            return ""

        tail = self._buffer
        candidate = find_left(tail)
        if candidate is not None:
            start = candidate.start
            header = tail[candidate.end:]
            if self._looks_like_invocation_prefix(header):
                public = _CALL_PREAMBLE_RE.sub("", tail[:start])
                self.reset()
                return public

        self.reset()
        return tail

    def reset(self) -> None:
        self._blocked = False
        self._buffer = ""
        self._encoding = None
        self._stack = []
        self._visible_tail = ""

    def on_segment_boundary(self) -> None:
        self._visible_tail = ""

    def _consume_private(self) -> bool:
        encoding = self._encoding
        if encoding is None:
            self._buffer = ""
            return False

        candidate = find_left(self._buffer, (encoding,))
        if candidate is None:
            self._buffer = partial_left_suffix(self._buffer, (encoding,))
            if len(self._buffer) > _MAX_TAG_CHARS:
                self._blocked = True
                self._buffer = ""
            return False

        right = find_right(self._buffer, candidate.end, encoding)
        if right is None:
            self._buffer = self._buffer[candidate.start:]
            if len(self._buffer) > _MAX_TAG_CHARS:
                self._buffer = ""
            return False

        right_start, right_end = right
        tag = self._parse_tag(self._buffer[candidate.end:right_start])
        self._buffer = self._buffer[right_end:]
        if tag is None:
            return True
        if tag.closing:
            if self._stack and tag.name == self._stack[-1]:
                _ = self._stack.pop()
                if not self._stack:
                    self._encoding = None
            return True
        if not tag.self_closing:
            self._stack.append(tag.name)
        return True

    def _is_invocation(self, tag: _Tag, visible_prefix: str) -> bool:
        if tag.closing or tag.name not in _INVOCATION_TAGS:
            return False
        if tag.name != "function":
            return True
        if _NAME_ATTRIBUTE_RE.search(tag.attributes) is None:
            return False
        return _BLOCK_BOUNDARY_RE.search(self._visible_tail + visible_prefix) is not None

    @staticmethod
    def _parse_tag(text: str) -> _Tag | None:
        body = text.strip()
        closing = body.startswith("/")
        if closing:
            body = body[1:].lstrip()
        self_closing = not closing and body.endswith("/")
        if self_closing:
            body = body[:-1].rstrip()
        match = _NAME_RE.match(body)
        if match is None:
            return None
        end = match.end()
        if end < len(body) and not body[end].isspace():
            return None
        return _Tag(
            name=match.group(1).casefold(),
            attributes=body[end:],
            closing=closing,
            self_closing=self_closing,
        )

    @classmethod
    def _parse_partial_tag(cls, text: str) -> _Tag | None:
        return cls._parse_tag(text)

    @staticmethod
    def _looks_like_invocation_prefix(text: str) -> bool:
        body = text.lstrip().removeprefix("/").lstrip()
        match = re.match(r"(?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)?", body)
        if match is None:
            return False
        fragment = (match.group(1) or "").casefold()
        if not fragment:
            return False
        return any(name.startswith(fragment) for name in _INVOCATION_TAGS)

    @classmethod
    def _partial_left_suffix(cls, text: str) -> str:
        return partial_left_suffix(text)

    def _visible_result(self, segments: list[str]) -> str:
        result = "".join(segments)
        if result:
            self._visible_tail = (self._visible_tail + result)[-128:]
        return result


def scrub_invocation_markup(text: str) -> str:
    scrubber = StreamingInvocationScrubber()
    return scrubber.feed(text) + scrubber.finish()


__all__ = ["StreamingInvocationScrubber", "scrub_invocation_markup"]
