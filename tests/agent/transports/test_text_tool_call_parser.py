"""Tests for opt-in textual tool-call parsing."""

from types import SimpleNamespace

import pytest

from agent.text_tool_call_parser import (
    TextToolCallProtocol,
    parse_text_tool_calls,
    resolve_text_tool_call_protocol,
)
from agent.transports import get_transport
from providers.base import ProviderProfile


@pytest.fixture
def transport():
    import agent.transports.chat_completions  # noqa: F401

    return get_transport("chat_completions")


class TestTextToolCallParser:
    def test_json_protocol_returns_canonical_tool_call(self):
        calls = parse_text_tool_calls(
            '{"name":"read_file","arguments":{"path":"README.md"}}',
            TextToolCallProtocol.JSON,
        )

        assert calls is not None
        assert [(call.id, call.name, call.arguments) for call in calls] == [
            (None, "read_file", '{"path":"README.md"}')
        ]

    def test_tool_call_tag_protocol_returns_canonical_tool_call(self):
        calls = parse_text_tool_calls(
            '<tool_call>{"name":"read_file","arguments":{"path":"README.md"}}</tool_call>',
            TextToolCallProtocol.TOOL_CALL_TAG,
        )

        assert calls is not None
        assert [(call.id, call.name, call.arguments) for call in calls] == [
            (None, "read_file", '{"path":"README.md"}')
        ]

    @pytest.mark.parametrize(
        ("content", "protocol"),
        [
            ('{"name":"read_file","arguments":"not json"}', TextToolCallProtocol.JSON),
            ('<tool_call>{"name":"read_file"}</tool_call>', TextToolCallProtocol.TOOL_CALL_TAG),
            ('before <tool_call>{"name":"read_file","arguments":{}}</tool_call>', TextToolCallProtocol.TOOL_CALL_TAG),
        ],
    )
    def test_malformed_text_returns_no_tool_calls(self, content, protocol):
        assert parse_text_tool_calls(content, protocol) is None


class TestTextToolCallProtocolSelection:
    def test_exact_model_overrides_provider_default(self):
        profile = ProviderProfile(
            name="text-tools",
            text_tool_call_protocols={
                "*": TextToolCallProtocol.JSON.value,
                "qwen3-coder": TextToolCallProtocol.TOOL_CALL_TAG.value,
            },
        )

        assert (
            resolve_text_tool_call_protocol(profile, "qwen3-coder")
            is TextToolCallProtocol.TOOL_CALL_TAG
        )
        assert (
            resolve_text_tool_call_protocol(profile, "other-model")
            is TextToolCallProtocol.JSON
        )


class TestChatCompletionsTextToolCalls:
    def test_enabled_protocol_normalizes_text_to_tool_calls(self, transport):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content='{"name":"read_file","arguments":{"path":"README.md"}}',
                        tool_calls=None,
                    ),
                )
            ],
            usage=None,
        )

        normalized = transport.normalize_response(
            response,
            text_tool_call_protocol=TextToolCallProtocol.JSON,
        )

        assert normalized.content is None
        assert normalized.finish_reason == "tool_calls"
        assert normalized.tool_calls is not None
        assert [(call.name, call.arguments) for call in normalized.tool_calls] == [
            ("read_file", '{"path":"README.md"}')
        ]

    def test_disabled_protocol_retains_text_as_assistant_content(self, transport):
        content = '{"name":"read_file","arguments":{"path":"README.md"}}'
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content, tool_calls=None),
                )
            ],
            usage=None,
        )

        normalized = transport.normalize_response(response)

        assert normalized.content == content
        assert normalized.tool_calls is None
        assert normalized.finish_reason == "stop"
