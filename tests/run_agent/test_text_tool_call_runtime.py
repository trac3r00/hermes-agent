"""Runtime wiring for opt-in textual tool calls via the provider profile."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.text_tool_call_parser import TextToolCallProtocol
from providers.base import ProviderProfile
from run_agent import AIAgent


def _make_agent() -> AIAgent:
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "web_search tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="dummy",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=5,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._disable_streaming = True
    return agent


def _text_response(content: str) -> SimpleNamespace:
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _run(profile: ProviderProfile | None):
    agent = _make_agent()
    tool_text = json.dumps({"name": "web_search", "arguments": {"query": "hermes"}})
    agent.client.chat.completions.create.side_effect = [
        _text_response(tool_text),
        _text_response("done"),
    ]
    with (
        patch("providers.get_provider_profile", return_value=profile),
        patch("run_agent.handle_function_call", return_value=json.dumps({"ok": True})) as mock_hfc,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search")
    return result, mock_hfc, tool_text


def test_profile_protocol_turns_textual_call_into_executed_tool_call():
    profile = ProviderProfile(
        name="text-tools",
        text_tool_call_protocols={"*": TextToolCallProtocol.JSON.value},
    )

    result, mock_hfc, _ = _run(profile)

    mock_hfc.assert_called_once()
    assert mock_hfc.call_args.args[:2] == ("web_search", {"query": "hermes"})
    assert result["final_response"] == "done"
    assistant_calls = [
        m for m in result["messages"] if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(assistant_calls) == 1
    call_ids = [tc["id"] for tc in assistant_calls[0]["tool_calls"]]
    assert all(call_ids)
    tool_results = [m for m in result["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == call_ids


def test_without_profile_protocol_textual_call_stays_plain_text():
    result, mock_hfc, tool_text = _run(ProviderProfile(name="plain"))

    mock_hfc.assert_not_called()
    assert result["final_response"] == tool_text
