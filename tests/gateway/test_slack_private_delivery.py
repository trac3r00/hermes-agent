import asyncio  # noqa  # noqa: ANYIO_OK -- exercises asyncio task-context propagation
from types import SimpleNamespace
from typing import Final, TypedDict
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientTimeout

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from plugins.platforms.slack import adapter as slack_adapter_module
from plugins.platforms.slack.adapter import SlackAdapter


_CHANNEL: Final = "C-private"
_USER: Final = "U-private"


class _ResponseUrlPost(TypedDict):
    url: str
    json: dict[str, str | bool]


class _ResponseUrlReply:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self) -> "_ResponseUrlReply":
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def text(self) -> str:
        return f"status={self.status}"


class _ResponseUrlSession:
    def __init__(
        self,
        outcomes: list[int | BaseException],
        posts: list[_ResponseUrlPost],
    ):
        self._outcomes = outcomes
        self._posts = posts

    async def __aenter__(self) -> "_ResponseUrlSession":
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def post(
        self,
        url: str,
        *,
        json: dict[str, str | bool],
        timeout: ClientTimeout,
    ) -> _ResponseUrlReply:
        del timeout
        self._posts.append({"url": url, "json": json})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _ResponseUrlReply(outcome)


def _make_adapter() -> tuple[SlackAdapter, AsyncMock]:
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    adapter._app = MagicMock()
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "public-ts"})
    client.assistant_threads_setStatus = AsyncMock(return_value={})
    adapter._get_client = MagicMock(return_value=client)
    return adapter, client


def _install_response_url_wire(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[int | BaseException],
) -> list[_ResponseUrlPost]:
    posts: list[_ResponseUrlPost] = []
    session = _ResponseUrlSession(outcomes, posts)
    monkeypatch.setattr(
        slack_adapter_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    return posts


def _command(response_url: str) -> dict[str, str]:
    return {
        "command": "/model",
        "text": "list",
        "user_id": _USER,
        "channel_id": _CHANNEL,
        "team_id": "T-private",
        "response_url": response_url,
    }


@pytest.mark.asyncio
async def test_same_user_concurrent_slashes_keep_delayed_tasks_bound_to_own_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: two same-user/channel invocations whose processing tasks start later.
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [200, 200])
    captured_events: list[MessageEvent] = []
    delivery_tasks: list[asyncio.Task[SendResult]] = []

    async def capture_event(event: MessageEvent) -> None:
        captured_events.append(event)

    def start_later(
        self: BasePlatformAdapter,
        event: MessageEvent,
        _session_key: str,
        *,
        interrupt_event: asyncio.Event | None = None,
    ) -> bool:
        del interrupt_event
        response_url = event.raw_message["response_url"]
        delivery_tasks.append(
            asyncio.create_task(
                self.send(event.source.chat_id, f"reply for {response_url}")
            )
        )
        return True

    monkeypatch.setattr(adapter, "handle_message", capture_event)
    await asyncio.gather(
        adapter._handle_slash_command(_command("https://response.test/one")),
        adapter._handle_slash_command(_command("https://response.test/two")),
    )
    monkeypatch.setattr(BasePlatformAdapter, "_start_session_processing", start_later)

    # When: both delayed tasks are spawned after the handler ContextVars reset.
    for index, event in enumerate(captured_events):
        adapter._start_session_processing(event, f"session-{index}")
    await asyncio.gather(*delivery_tasks)

    # Then: each private reply uses its own invocation URL and no public send occurs.
    assert [post["url"] for post in posts] == [
        "https://response.test/one",
        "https://response.test/two",
    ]
    assert [post["json"]["text"] for post in posts] == [
        "reply for https://response.test/one",
        "reply for https://response.test/two",
    ]
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_public_send_cannot_steal_pending_slash_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a native command is pending while an unrelated public send runs.
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [200])
    command_started = asyncio.Event()
    release_command = asyncio.Event()
    command_results: list[SendResult] = []

    async def delayed_command(_event: MessageEvent) -> None:
        command_started.set()
        await release_command.wait()
        command_results.append(await adapter.send(_CHANNEL, "private command reply"))

    monkeypatch.setattr(adapter, "handle_message", delayed_command)
    command_task = asyncio.create_task(
        adapter._handle_slash_command(_command("https://response.test/pending"))
    )
    await command_started.wait()

    # When: an invocation-unbound send completes before the command reply.
    public_result = await adapter.send(_CHANNEL, "ordinary public reply")
    release_command.set()
    await command_task

    # Then: the ordinary path stays public and the command retains its private URL.
    assert public_result.success is True
    client.chat_postMessage.assert_awaited_once()
    assert client.chat_postMessage.await_args.kwargs["text"] == "ordinary public reply"
    assert [post["url"] for post in posts] == ["https://response.test/pending"]
    assert posts[0]["json"]["text"] == "private command reply"
    assert command_results[0].success is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [503, OSError("response-url connection failed")],
    ids=["http-503", "transport-exception"],
)
async def test_response_url_failure_fails_closed_without_public_fallback(
    monkeypatch: pytest.MonkeyPatch,
    outcome: int | BaseException,
) -> None:
    # Given: a bound native command whose private response-url write fails.
    adapter, client = _make_adapter()
    _install_response_url_wire(monkeypatch, [outcome])
    results: list[SendResult] = []

    async def deliver(_event: MessageEvent) -> None:
        results.append(await adapter.send(_CHANNEL, "must remain private"))

    monkeypatch.setattr(adapter, "handle_message", deliver)

    # When: the slash command attempts delivery.
    await adapter._handle_slash_command(_command("https://response.test/failure"))

    # Then: delivery reports failure and never invokes the public Slack API.
    assert results[0].success is False
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_url_multichunk_delivery_stays_private_and_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Slack permits three repeated writes to one command response URL.
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [200, 200, 200])
    adapter.truncate_message = MagicMock(return_value=["first", "second", "third"])
    results: list[SendResult] = []

    async def deliver(_event: MessageEvent) -> None:
        results.append(await adapter.send(_CHANNEL, "long private output"))

    monkeypatch.setattr(adapter, "handle_message", deliver)

    # When: the native command reply is delivered.
    await adapter._handle_slash_command(_command("https://response.test/chunks"))

    # Then: every chunk is written ephemerally and none is posted publicly.
    assert results[0].success is True
    assert [post["json"]["text"] for post in posts] == ["first", "second", "third"]
    assert [post["json"]["replace_original"] for post in posts] == [
        True,
        False,
        False,
    ]
    assert all(post["json"]["response_type"] == "ephemeral" for post in posts)
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_url_partial_transient_failure_retries_only_private_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [200, 503, 200])
    adapter.truncate_message = MagicMock(return_value=["head", "tail"])
    results: list[SendResult] = []
    uses: list[int] = []

    async def deliver(_event: MessageEvent) -> None:
        results.append(await adapter._send_with_retry(_CHANNEL, "ORIGINAL", max_retries=0))
        invocation = slack_adapter_module._slash_invocation.get()
        assert invocation is not None
        uses.append(invocation.successful_uses)

    monkeypatch.setattr(adapter, "handle_message", deliver)
    monkeypatch.setattr(slack_adapter_module.asyncio, "sleep", AsyncMock())

    await adapter._handle_slash_command(_command("https://response.test/partial"))

    assert results[0].success is True
    assert uses == [2]
    assert [post["json"]["text"] for post in posts] == ["head", "tail", "tail"]
    assert [post["json"]["replace_original"] for post in posts] == [True, False, False]
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_url_partial_permanent_failure_never_replays_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [200, 400, 200])
    adapter.truncate_message = MagicMock(return_value=["head", "tail"])
    results: list[SendResult] = []

    async def deliver(_event: MessageEvent) -> None:
        results.append(await adapter._send_with_retry(_CHANNEL, "ORIGINAL", max_retries=0))

    monkeypatch.setattr(adapter, "handle_message", deliver)

    await adapter._handle_slash_command(_command("https://response.test/permanent"))

    assert results[0].success is False
    assert [post["json"]["text"] for post in posts] == ["head", "tail"]
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_url_partial_transient_exhaustion_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [200, 503, 503, 503, 200])
    adapter.truncate_message = MagicMock(return_value=["head", "tail"])
    results: list[SendResult] = []

    async def deliver(_event: MessageEvent) -> None:
        results.append(await adapter._send_with_retry(_CHANNEL, "ORIGINAL"))

    monkeypatch.setattr(adapter, "handle_message", deliver)
    monkeypatch.setattr(slack_adapter_module.asyncio, "sleep", AsyncMock())

    await adapter._handle_slash_command(_command("https://response.test/exhausted"))

    assert results[0].success is False
    assert [post["json"]["text"] for post in posts] == [
        "head",
        "tail",
        "tail",
        "tail",
    ]
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_url_ambiguous_transport_failure_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(
        monkeypatch,
        [OSError("connection reset after write"), 200],
    )
    results: list[SendResult] = []

    async def deliver(_event: MessageEvent) -> None:
        results.append(await adapter._send_with_retry(_CHANNEL, "private output"))

    monkeypatch.setattr(adapter, "handle_message", deliver)

    await adapter._handle_slash_command(_command("https://response.test/ambiguous"))

    assert results[0].success is False
    assert len(posts) == 1
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_url_transport_error_never_exposes_bearer_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, _client = _make_adapter()
    secret_url = "https://response.test/bearer-secret-token"
    _install_response_url_wire(monkeypatch, [OSError(secret_url)])

    async def deliver(_event: MessageEvent) -> None:
        result = await adapter._send_with_retry(_CHANNEL, "private output")
        assert secret_url not in (result.error or "")

    monkeypatch.setattr(adapter, "handle_message", deliver)

    await adapter._handle_slash_command(_command(secret_url))

    assert secret_url not in caplog.text


@pytest.mark.asyncio
async def test_expired_response_url_fails_closed_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [])
    results: list[SendResult] = []
    clock_values = iter([0.0, 1801.0])
    monkeypatch.setattr(
        slack_adapter_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock_values)),
    )

    async def deliver(_event: MessageEvent) -> None:
        results.append(await adapter._send_with_retry(_CHANNEL, "private output"))

    monkeypatch.setattr(adapter, "handle_message", deliver)

    await adapter._handle_slash_command(_command("https://response.test/expired"))

    assert results[0].success is False
    assert results[0].raw_response["fail_closed"] is True
    assert posts == []
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_url_lifecycle_persists_across_repeated_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one invocation has five successful uses around one permanent failure.
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [200, 400, 200, 200, 200, 200, 200])
    results: list[SendResult] = []

    async def deliver(_event: MessageEvent) -> None:
        for text in "prefix failed second third fourth fifth overflow".split():
            results.append(await adapter.send(_CHANNEL, text))

    monkeypatch.setattr(adapter, "handle_message", deliver)

    # When: delivery retries after failure and then exceeds Slack's five-use cap.
    await adapter._handle_slash_command(_command("https://response.test/lifecycle"))

    # Then: only the first success replaces the ack; overflow fails closed.
    assert (results[0].success, results[1].success) == (True, False)
    assert all(result.success for result in results[2:6])
    assert results[6].success is False
    assert (len(posts), posts[0]["json"]["replace_original"]) == (6, True)
    assert all(post["json"]["replace_original"] is False for post in posts[1:])
    assert all(post["json"]["response_type"] == "ephemeral" for post in posts)
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_url_remains_bound_for_slacks_thirty_minute_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a slash invocation created at t=0 and delivered at t=1799 seconds.
    adapter, client = _make_adapter()
    posts = _install_response_url_wire(monkeypatch, [200])
    clock_values = iter([0.0, 1799.0])
    monkeypatch.setattr(
        slack_adapter_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock_values)),
    )

    async def deliver(_event: MessageEvent) -> None:
        await adapter.send(_CHANNEL, "still private")

    monkeypatch.setattr(adapter, "handle_message", deliver)

    # When: delivery occurs one second before Slack's documented expiry.
    await adapter._handle_slash_command(_command("https://response.test/lifetime"))

    # Then: the response URL is still used and public chat remains untouched.
    assert [post["url"] for post in posts] == ["https://response.test/lifetime"]
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_status_clear_isolated_by_channel_and_thread() -> None:
    # Given: two active Assistant statuses in separate threads of one channel.
    adapter, client = _make_adapter()
    await adapter.send_typing(_CHANNEL, metadata={"thread_ts": "thread-a"})
    await adapter.send_typing(_CHANNEL, metadata={"thread_ts": "thread-b"})

    # When: only thread A completes.
    await adapter.stop_typing(_CHANNEL, metadata={"thread_ts": "thread-a"})

    # Then: Slack clears A, while B remains tracked for its own completion.
    assert client.assistant_threads_setStatus.await_args_list[-1].kwargs == {
        "channel_id": _CHANNEL,
        "thread_ts": "thread-a",
        "status": "",
    }
    assert (_CHANNEL, "thread-a") not in adapter._active_status_threads
    assert (_CHANNEL, "thread-b") in adapter._active_status_threads


@pytest.mark.asyncio
async def test_thread_status_clear_failure_remains_retryable() -> None:
    # Given: Slack accepts a status, transiently rejects its first clear, then recovers.
    adapter, client = _make_adapter()
    client.assistant_threads_setStatus = AsyncMock(
        side_effect=[{}, RuntimeError("transient clear failure"), {}]
    )
    metadata = {"thread_ts": "thread-retry"}
    await adapter.send_typing(_CHANNEL, metadata=metadata)

    # When: the same thread clear is attempted twice.
    await adapter.stop_typing(_CHANNEL, metadata=metadata)
    await adapter.stop_typing(_CHANNEL, metadata=metadata)

    # Then: the failed clear kept state so the second call could retry successfully.
    assert client.assistant_threads_setStatus.await_count == 3
    assert (_CHANNEL, "thread-retry") not in adapter._active_status_threads
