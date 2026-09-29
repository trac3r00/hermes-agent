"""Strict, opt-in parsing for providers that emit textual tool calls."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any

from agent.transports.types import ToolCall
from providers.base import ProviderProfile


class TextToolCallProtocol(StrEnum):
    JSON = "json"
    TOOL_CALL_TAG = "tool_call_tag"


_TOOL_CALL_TAG = re.compile(
    r"<tool_call>(?P<payload>.*?)</tool_call>",
    flags=re.DOTALL,
)


def resolve_text_tool_call_protocol(
    profile: ProviderProfile | None,
    model: str | None,
) -> TextToolCallProtocol | None:
    """Return the configured protocol for an exact model or provider default."""
    if profile is None:
        return None

    protocols = profile.text_tool_call_protocols
    model_key = str(model or "").strip().lower()
    value = protocols.get(model_key, protocols.get("*"))
    try:
        return TextToolCallProtocol(value) if value else None
    except ValueError:
        return None


def parse_text_tool_calls(
    content: Any,
    protocol: TextToolCallProtocol,
) -> list[ToolCall] | None:
    """Parse a complete, configured textual tool-call response or return None."""
    if not isinstance(content, str):
        return None

    payloads = _payloads_for_protocol(content, protocol)
    if payloads is None:
        return None

    calls: list[ToolCall] = []
    for payload in payloads:
        call = _parse_tool_call(payload)
        if call is None:
            return None
        calls.append(call)
    return calls or None


def _payloads_for_protocol(
    content: str,
    protocol: TextToolCallProtocol,
) -> list[str] | None:
    if protocol is TextToolCallProtocol.JSON:
        payload = content.strip()
        return [payload] if payload else None

    if protocol is not TextToolCallProtocol.TOOL_CALL_TAG:
        return None

    matches = list(_TOOL_CALL_TAG.finditer(content))
    if not matches:
        return None

    cursor = 0
    for match in matches:
        if content[cursor:match.start()].strip():
            return None
        cursor = match.end()
    if content[cursor:].strip():
        return None

    return [match.group("payload").strip() for match in matches]


def _parse_tool_call(payload: str) -> ToolCall | None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(value, dict):
        return None

    name = value.get("name")
    arguments = value.get("arguments")
    if not isinstance(name, str) or not name.strip():
        return None

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None

    call_id = value.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = None
    return ToolCall(
        id=call_id,
        name=name,
        arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    )
