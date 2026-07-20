"""Tests for /moa one-shot validation (validate_moa_payload + frontend wiring)."""

import queue
from unittest.mock import patch

from cli import HermesCLI
from hermes_cli.moa_config import moa_usage, validate_moa_payload


# ---------------------------------------------------------------------------
# Unit tests for validate_moa_payload
# ---------------------------------------------------------------------------

def test_empty_string_returns_usage():
    assert validate_moa_payload("") == moa_usage()


def test_none_returns_usage():
    assert validate_moa_payload(None) == moa_usage()


def test_whitespace_only_returns_usage():
    assert validate_moa_payload("   \t\n  ") == moa_usage()


def test_valid_prompt_returns_none():
    assert validate_moa_payload("explain this code") is None


def test_valid_prompt_with_dashes_is_fine():
    # A normal prompt that happens to contain dashes is NOT a flag.
    assert validate_moa_payload("explain the trade-off") is None


def test_unsupported_flag_preset():
    err = validate_moa_payload("--preset review some prompt")
    assert err is not None
    assert "Unsupported option" in err
    assert "--preset" in err


def test_unsupported_flag_short_preset():
    err = validate_moa_payload("-p review some prompt")
    assert err is not None
    assert "-p" in err


def test_unsupported_flag_model():
    err = validate_moa_payload("--model gpt-4 summarize this")
    assert err is not None
    assert "--model" in err


def test_unsupported_flag_provider():
    err = validate_moa_payload("--provider openai explain")
    assert err is not None
    assert "--provider" in err


def test_unsupported_flag_temperature():
    err = validate_moa_payload("--temperature 0.7 write a haiku")
    assert err is not None
    assert "--temperature" in err


def test_unsupported_flag_max_tokens():
    err = validate_moa_payload("--max-tokens 1000 summarize")
    assert err is not None
    assert "--max-tokens" in err


def test_unsupported_flag_case_insensitive():
    err = validate_moa_payload("--PRESET default hello")
    assert err is not None
    assert "Unsupported option" in err


def test_flag_mid_prompt_still_caught():
    err = validate_moa_payload("summarize --model gpt-4 this thing")
    assert err is not None
    assert "--model" in err


# ---------------------------------------------------------------------------
# CLI integration: running-agent guard
# ---------------------------------------------------------------------------

def _make_cli(**overrides):
    cli = HermesCLI.__new__(HermesCLI)
    cli.config = {
        "moa": {
            "default_preset": "default",
            "presets": {
                "default": {
                    "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                    "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                },
            },
        }
    }
    cli._pending_input = queue.Queue()
    cli._pending_agent_seed = None
    cli._pending_moa_config = None
    cli._pending_moa_disable_after_turn = False
    cli._pending_moa_restore_model = None
    cli._agent_running = False
    cli.agent = None
    cli.provider = "openrouter"
    cli.requested_provider = "openrouter"
    cli.model = "anthropic/claude-opus-4.8"
    cli.api_key = "test-key"
    cli.base_url = "https://openrouter.ai/api/v1"
    cli.api_mode = "chat_completions"
    for k, v in overrides.items():
        setattr(cli, k, v)
    return cli


def test_cli_rejects_moa_while_agent_running():
    cli = _make_cli(_agent_running=True)
    printed = []
    with patch("cli._cprint", side_effect=printed.append):
        result = cli.process_command("/moa explain this")
    assert result is True
    assert any("running" in s.lower() for s in printed)
    # Must NOT mutate model state
    assert cli.provider == "openrouter"
    assert cli._pending_agent_seed is None


def test_cli_rejects_unsupported_flag():
    cli = _make_cli()
    printed = []
    with patch("cli._cprint", side_effect=printed.append):
        result = cli.process_command("/moa --preset review hello")
    assert result is True
    assert any("Unsupported option" in s for s in printed)
    # Must NOT mutate model state
    assert cli.provider == "openrouter"
    assert cli._pending_agent_seed is None


def test_cli_whitespace_only_shows_usage():
    cli = _make_cli()
    printed = []
    with patch("cli._cprint", side_effect=printed.append):
        result = cli.process_command("/moa    ")
    assert result is True
    assert any("Usage" in s for s in printed)
    assert cli.provider != "moa"
