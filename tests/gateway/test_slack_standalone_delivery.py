import json
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter, _standalone_send
from tools.send_message_tool import _handle_send, _send_to_platform


class _SlackResponseData(TypedDict, total=False):
    ok: bool
    ts: str
    error: str
    response_metadata: dict[str, list[str]]


class _FakeResponse:
    def __init__(self, data: _SlackResponseData) -> None:
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self) -> _SlackResponseData:
        return self._data


class _FakeSession:
    def __init__(self, responses: list[_SlackResponseData]) -> None:
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, *, headers, json, **kwargs):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": dict(json),
                "kwargs": kwargs,
            }
        )
        if not self.responses:
            raise AssertionError("Unexpected Slack HTTP request")
        return _FakeResponse(self.responses.pop(0))


def _install_http(session: _FakeSession):
    return patch(
        "plugins.platforms.slack.adapter.aiohttp.ClientSession",
        return_value=session,
    )


@pytest.mark.asyncio
async def test_send_message_prefers_live_slack_adapter(monkeypatch):
    # Given a live Slack adapter and an available standalone sender.
    live_adapter = SimpleNamespace(
        send=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="live-message")
        )
    )
    standalone = AsyncMock(
        return_value={"success": True, "message_id": "standalone-message"}
    )
    runner = SimpleNamespace(adapters={Platform.SLACK: live_adapter})
    registry_entry = SimpleNamespace(
        max_message_length=SlackAdapter.MAX_MESSAGE_LENGTH,
        standalone_sender_fn=standalone,
    )
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    monkeypatch.setattr(
        "gateway.platform_registry.platform_registry.get",
        lambda _name: registry_entry,
    )

    # When send_message routes a threaded Slack delivery.
    result = await _send_to_platform(
        Platform.SLACK,
        PlatformConfig(enabled=True, token="token-one,token-two"),
        "C123",
        "live route",
        thread_id="171.1",
    )

    # Then only the live adapter handles it.
    assert result == {"success": True, "message_id": "live-message"}
    live_adapter.send.assert_awaited_once_with(
        chat_id="C123",
        content="live route",
        metadata={"thread_id": "171.1"},
    )
    standalone.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_formats_before_chunking(monkeypatch):
    # Given formatting that expands a raw message beyond Slack's wire limit.
    formatted = "F" * (SlackAdapter.MAX_MESSAGE_LENGTH + 25) + "::tail::"
    expected_chunks = SlackAdapter.truncate_message(
        formatted,
        SlackAdapter.MAX_MESSAGE_LENGTH,
    )
    session = _FakeSession(
        [{"ok": True, "ts": str(index)} for index in range(len(expected_chunks))]
    )
    monkeypatch.setattr(
        "plugins.platforms.slack.adapter._standalone_render",
        lambda _config, _message: (formatted, [{"type": "section"}]),
    )

    # When standalone delivery sends the raw short input.
    with _install_http(session):
        result = await _standalone_send(
            PlatformConfig(enabled=True, token="token-one"),
            "C123",
            "raw input",
        )

    # Then every final wire payload is bounded and the full formatted output is sent.
    payloads = [call["payload"] for call in session.calls]
    assert result["success"] is True
    assert [payload["text"] for payload in payloads] == expected_chunks
    assert all(
        len(payload["text"]) <= SlackAdapter.MAX_MESSAGE_LENGTH
        for payload in payloads
    )
    assert all("blocks" not in payload for payload in payloads)


@pytest.mark.asyncio
async def test_standalone_invalid_blocks_retries_once_with_complete_text(monkeypatch):
    # Given Slack rejects a single rich-block request as invalid.
    session = _FakeSession(
        [
            {"ok": False, "error": "invalid_blocks"},
            {"ok": True, "ts": "retry-message"},
        ]
    )
    monkeypatch.setattr(
        "plugins.platforms.slack.adapter._standalone_render",
        lambda _config, _message: (
            "complete formatted text",
            [{"type": "section"}],
        ),
    )

    # When standalone delivery handles the response.
    with _install_http(session):
        result = await _standalone_send(
            PlatformConfig(enabled=True, token="token-one"),
            "C123",
            "raw input",
        )

    # Then it retries exactly once with identical text and no blocks.
    payloads = [call["payload"] for call in session.calls]
    assert result["success"] is True
    assert len(payloads) == 2
    assert payloads[0]["text"] == payloads[1]["text"] == "complete formatted text"
    assert "blocks" in payloads[0]
    assert "blocks" not in payloads[1]


@pytest.mark.asyncio
async def test_standalone_message_truncated_warning_is_not_success():
    # Given Slack reports semantic truncation despite ok=true.
    session = _FakeSession(
        [
            {
                "ok": True,
                "ts": "truncated-message",
                "response_metadata": {"warnings": ["message_truncated"]},
            }
        ]
    )

    # When standalone delivery receives the response.
    with _install_http(session):
        result = await _standalone_send(
            PlatformConfig(enabled=True, token="token-one"),
            "C123",
            "must remain complete",
        )

    # Then semantic data loss is surfaced as an error.
    assert "success" not in result
    error = result.get("error")
    assert isinstance(error, str)
    assert "message_truncated" in error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_token",
    ["token-one,token-two", "token-one\ntoken-two"],
)
async def test_standalone_multi_workspace_token_fails_without_leaking(
    configured_token: str,
):
    # Given standalone routing cannot map a channel to one of two workspace tokens.
    session_factory = MagicMock(side_effect=AssertionError("HTTP must not be called"))

    # When standalone delivery is requested.
    with patch(
        "plugins.platforms.slack.adapter.aiohttp.ClientSession",
        session_factory,
    ):
        result = await _standalone_send(
            PlatformConfig(enabled=True, token=configured_token),
            "C123",
            "private content",
        )

    # Then it fails before the wire and does not expose either credential.
    assert "error" in result
    error = result.get("error")
    assert isinstance(error, str)
    assert "workspace" in error.lower()
    assert "token-one" not in error
    assert "token-two" not in error
    session_factory.assert_not_called()


def test_send_message_slack_dm_never_concatenates_workspace_tokens():
    # Given a Slack user target and a multi-workspace token configuration.
    configured_token = "token-one,token-two"
    slack_config = SimpleNamespace(enabled=True, token=configured_token, extra={})
    gateway_config = SimpleNamespace(
        platforms={Platform.SLACK: slack_config},
        get_home_channel=lambda _platform: None,
    )
    session_factory = MagicMock(side_effect=AssertionError("HTTP must not be called"))

    # When send_message resolves the user target to a DM.
    with (
        patch("gateway.config.load_gateway_config", return_value=gateway_config),
        patch("gateway.channel_directory.resolve_channel_name", return_value="U123ABCDEF"),
        patch("tools.interrupt.is_interrupted", return_value=False),
        patch("aiohttp.ClientSession", session_factory),
    ):
        result = json.loads(
            _handle_send(
                {
                    "action": "send",
                    "target": "slack:workspace-user",
                    "message": "private content",
                }
            )
        )

    # Then routing fails explicitly before any bearer header is constructed.
    error = result.get("error")
    assert isinstance(error, str)
    assert "workspace" in error.lower()
    assert "token-one" not in error
    assert "token-two" not in error
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_plain_threaded_send_remains_supported():
    # Given a normal single-workspace threaded delivery.
    session = _FakeSession([{"ok": True, "ts": "thread-message"}])

    # When standalone delivery posts it.
    with _install_http(session):
        result = await _standalone_send(
            PlatformConfig(enabled=True, token="token-one"),
            "C123",
            "plain message",
            thread_id="171.1",
        )

    # Then the existing channel/thread payload contract is preserved.
    assert result == {
        "success": True,
        "platform": "slack",
        "chat_id": "C123",
        "message_id": "thread-message",
    }
    assert session.calls[0]["payload"]["channel"] == "C123"
    assert session.calls[0]["payload"]["thread_ts"] == "171.1"
