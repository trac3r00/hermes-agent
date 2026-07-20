from __future__ import annotations

from types import SimpleNamespace

from agent.chat_completion_helpers import (
    _rate_limit_cooldown_seconds,
    remember_rate_limit_cooldown,
    try_activate_fallback,
)
from agent.error_classifier import FailoverReason


class _RateLimitError(Exception):
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("rate limited")
        self.response = SimpleNamespace(headers=headers)


def test_parses_iso_quota_reset_header() -> None:
    error = _RateLimitError({"x-hermes-rate-limit-reset": "2026-07-19T17:00:00Z"})

    seconds = _rate_limit_cooldown_seconds(error, now=1784480340.0)

    assert seconds == 60.0


def test_retry_after_takes_precedence() -> None:
    error = _RateLimitError(
        {
            "retry-after": "3600",
            "x-hermes-rate-limit-reset": "2026-07-20T17:00:00Z",
        }
    )

    assert _rate_limit_cooldown_seconds(error, now=0) == 3600.0


def test_cooldown_is_stored_on_agent_instance_only() -> None:
    agent = SimpleNamespace()
    error = _RateLimitError({"retry-after": "7200"})

    remember_rate_limit_cooldown(agent, error)

    assert agent._provider_rate_limit_cooldown_s == 7200.0


def test_fallback_consumes_reset_hint_without_leaking_it_to_later_429s(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(
        _fallback_activated=False,
        provider="custom:llm-pool",
        _primary_runtime={"provider": "custom:llm-pool"},
        _provider_rate_limit_cooldown_s=7200.0,
        _rate_limited_until=0.0,
        _fallback_index=0,
        _fallback_chain=[],
    )
    monkeypatch.setattr("agent.chat_completion_helpers.time.monotonic", lambda: 100.0)

    activated = try_activate_fallback(agent, FailoverReason.rate_limit)

    assert activated is False
    assert agent._rate_limited_until == 7300.0
    assert agent._provider_rate_limit_cooldown_s == 0.0
