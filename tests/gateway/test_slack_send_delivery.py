from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


def _make_adapter(*, extra: dict[str, bool] | None = None) -> tuple[SlackAdapter, AsyncMock]:
    adapter = SlackAdapter(
        PlatformConfig(enabled=True, token="xoxb-fake", extra=extra or {})
    )
    adapter._app = MagicMock()
    client = AsyncMock()
    adapter._get_client = MagicMock(return_value=client)
    adapter.stop_typing = AsyncMock()
    return adapter, client


def _slack_api_error(
    *,
    status_code: int,
    error: str,
    retry_after: str | None = None,
) -> SlackApiError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    response = SlackResponse(
        client=MagicMock(),
        http_verb="POST",
        api_url="https://slack.com/api/chat.postMessage",
        req_args={},
        data={"ok": False, "error": error},
        headers=headers,
        status_code=status_code,
    )
    return SlackApiError(f"Slack API error: {error}", response)


class TestSlackPrimarySendDelivery:
    @pytest.mark.asyncio
    async def test_multichunk_posts_in_order_and_keeps_last_message_id(self):
        adapter, client = _make_adapter()
        adapter.truncate_message = MagicMock(return_value=["first chunk", "second chunk"])
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.0"}, {"ts": "2.0"}]
        )

        result = await adapter.send("C1", "full response")

        assert [call.kwargs["text"] for call in client.chat_postMessage.await_args_list] == [
            "first chunk",
            "second chunk",
        ]
        assert result.success is True
        assert result.message_id == "2.0"

    @pytest.mark.asyncio
    async def test_invalid_blocks_retries_as_full_text_only_delivery(self):
        adapter, client = _make_adapter(extra={"rich_blocks": True})
        content = "x" * (adapter.MAX_MESSAGE_LENGTH - 50) + " ::fallback-sentinel::"
        adapter._maybe_blocks = MagicMock(return_value=[{"type": "section"}])
        client.chat_postMessage = AsyncMock(
            side_effect=[
                _slack_api_error(status_code=400, error="invalid_blocks"),
                {"ts": "fallback-1"},
                {"ts": "fallback-2"},
                {"ts": "fallback-3"},
                {"ts": "fallback-4"},
            ]
        )

        result = await adapter._send_with_retry("C1", content)

        calls = client.chat_postMessage.await_args_list
        assert result.success is True
        assert "blocks" in calls[0].kwargs
        assert len(calls) >= 3
        assert all("blocks" not in call.kwargs for call in calls[1:])
        assert all(call.kwargs["mrkdwn"] is False for call in calls[1:])
        assert any("::fallback-sentinel::" in call.kwargs["text"] for call in calls[1:])

    @pytest.mark.asyncio
    async def test_slack_429_retries_after_server_delay_with_original_content(self):
        adapter, client = _make_adapter(extra={"rich_blocks": True})
        content = "# Retry sentinel"
        client.chat_postMessage = AsyncMock(
            side_effect=[
                _slack_api_error(
                    status_code=429,
                    error="ratelimited",
                    retry_after="3",
                ),
                {"ts": "retry-ts"},
            ]
        )

        with (
            patch("gateway.platforms.base.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("gateway.platforms.base.random.uniform", return_value=0.0),
        ):
            result = await adapter._send_with_retry("C1", content)

        calls = client.chat_postMessage.await_args_list
        assert result.success is True
        sleep.assert_awaited_once_with(3.0)
        assert [call.kwargs["text"] for call in calls] == [
            adapter.format_message(content),
            adapter.format_message(content),
        ]

    @pytest.mark.asyncio
    async def test_multichunk_rate_limit_retries_only_missing_tail(self):
        adapter, client = _make_adapter()
        content = "first chunk second chunk"

        def chunks(value: str, _maximum: int) -> list[str]:
            if value == content:
                return ["first chunk", "second chunk"]
            return [value]

        adapter.truncate_message = MagicMock(side_effect=chunks)
        client.chat_postMessage = AsyncMock(
            side_effect=[
                {"ts": "1.0"},
                _slack_api_error(
                    status_code=429,
                    error="ratelimited",
                    retry_after="1",
                ),
                {"ts": "2.0"},
                {"ts": "unexpected-replay"},
            ]
        )

        with (
            patch("gateway.platforms.base.asyncio.sleep", new_callable=AsyncMock),
            patch("gateway.platforms.base.random.uniform", return_value=0.0),
        ):
            result = await adapter._send_with_retry("C1", content)

        assert [call.kwargs["text"] for call in client.chat_postMessage.await_args_list] == [
            "first chunk",
            "second chunk",
            "second chunk",
        ]
        assert result.success is True
        assert result.message_id == "2.0"
        assert result.continuation_message_ids == ("1.0",)

    @pytest.mark.asyncio
    async def test_formatted_multichunk_rate_limit_retries_exact_formatted_tail(self):
        adapter, client = _make_adapter()
        content = "# Header\nfirst chunk second chunk"
        formatted = adapter.format_message(content)

        def chunks(value: str, _maximum: int) -> list[str]:
            if value == formatted:
                return ["*Header*\nfirst chunk ", "second chunk"]
            return [value]

        adapter.truncate_message = MagicMock(side_effect=chunks)
        client.chat_postMessage = AsyncMock(
            side_effect=[
                {"ts": "1.0"},
                _slack_api_error(
                    status_code=429,
                    error="ratelimited",
                    retry_after="1",
                ),
                {"ts": "2.0"},
            ]
        )

        with (
            patch("plugins.platforms.slack.adapter.asyncio.sleep", new_callable=AsyncMock),
            patch("gateway.platforms.base.random.uniform", return_value=0.0),
        ):
            result = await adapter._send_with_retry("C1", content, max_retries=1)

        assert [call.kwargs["text"] for call in client.chat_postMessage.await_args_list] == [
            "*Header*\nfirst chunk ",
            "second chunk",
            "second chunk",
        ]
        assert result.success is True
        assert result.message_id == "2.0"
        assert result.continuation_message_ids == ("1.0",)

    @pytest.mark.asyncio
    async def test_multichunk_tracks_every_posted_timestamp(self):
        adapter, client = _make_adapter()
        adapter.truncate_message = MagicMock(return_value=["one", "two", "three"])
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "1.0"}, {"ts": "2.0"}, {"ts": "3.0"}]
        )

        result = await adapter.send("C1", "one two three")

        assert result.message_id == "3.0"
        assert result.continuation_message_ids == ("1.0", "2.0")
        assert {"1.0", "2.0", "3.0"} <= adapter._bot_message_ts

    @pytest.mark.asyncio
    async def test_connection_reset_is_not_replayed_after_ambiguous_send(self):
        adapter, client = _make_adapter()
        client.chat_postMessage = AsyncMock(
            side_effect=[ConnectionResetError("connection reset"), {"ts": "retry-ts"}]
        )

        result = await adapter._send_with_retry("C1", "ambiguous send")

        assert result.success is False
        assert client.chat_postMessage.await_count == 1
