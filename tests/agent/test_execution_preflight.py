from types import SimpleNamespace

from agent import execution_preflight


def test_preflight_recomputes_workspace_tools_and_prior_outcomes(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("old = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Hermes Test",
            "-c",
            "user.email=hermes@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    monkeypatch.setattr(execution_preflight, "resolve_agent_cwd", lambda: tmp_path)
    monkeypatch.setattr(execution_preflight, "_active_sessions", lambda _sid: "1 other active (cli)")

    agent = SimpleNamespace(valid_tool_names={"terminal", "read_file"}, session_id="current")
    history = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "command failed: exit 1"},
    ]
    first = execution_preflight.build_execution_preflight(agent, history)

    assert "Available tools now (2): read_file, terminal" in first
    assert "terminal:failed" in first
    assert "Dirty paths now: none" in first
    assert "1 other active (cli)" in first

    tracked.write_text("new = True\n", encoding="utf-8")
    agent.valid_tool_names.add("search")
    second = execution_preflight.build_execution_preflight(agent, history)

    assert "Dirty paths now: tracked.py" in second
    assert "Available tools now (3): read_file, search, terminal" in second
    assert "do not repeat completed work" in second
