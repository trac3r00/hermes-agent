from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable

from agent.runtime_cwd import resolve_agent_cwd


_MAX_ITEMS = 20


def _run_git(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.rstrip() if proc.returncode == 0 else ""


def _bounded(values: Iterable[str], limit: int = _MAX_ITEMS) -> str:
    items = [value for value in values if value]
    shown = items[:limit]
    suffix = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return ", ".join(shown) + suffix if shown else "none"


def _tool_history(messages: list[dict[str, Any]]) -> str:
    outcomes: dict[str, str] = {}
    calls: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            content = str(message.get("content") or "").lower()
            outcomes[call_id] = "failed" if any(
                token in content for token in ("error", "failed", "traceback", "exception")
            ) else "completed"
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or "unknown")
            calls.append((name, str(call.get("id") or "")))
    recent = [f"{name}:{outcomes.get(call_id, 'attempted')}" for name, call_id in calls[-8:]]
    return _bounded(recent, 8)


def _active_sessions(current_session_id: str | None) -> str:
    try:
        from hermes_cli.active_sessions import active_session_registry_snapshot

        entries = active_session_registry_snapshot()
    except Exception:
        return "unavailable"
    others = [
        str(entry.get("surface") or "unknown")
        for entry in entries
        if str(entry.get("session_id") or "") != str(current_session_id or "")
    ]
    return f"{len(others)} other active ({_bounded(others, 8)})"


def build_execution_preflight(agent, messages: list[dict[str, Any]]) -> str:
    cwd = resolve_agent_cwd().resolve()
    root_raw = _run_git(cwd, "rev-parse", "--show-toplevel").strip()
    root = Path(root_raw) if root_raw else cwd
    branch = _run_git(root, "branch", "--show-current").strip() or "detached/non-git"
    dirty = [
        line[3:].strip()
        for line in _run_git(root, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
        if len(line) > 3 and line[3:].strip()
    ]
    tool_names = sorted(str(name) for name in getattr(agent, "valid_tool_names", set()) if name)

    return (
        "<execution-preflight>\n"
        "Fresh state for this turn (never assume the session-start snapshot is current):\n"
        f"- Workspace: {root}\n"
        f"- Branch: {branch}\n"
        f"- Dirty paths now: {_bounded(dirty)}\n"
        f"- Available tools now ({len(tool_names)}): {_bounded(tool_names, 30)}\n"
        f"- Recent tool outcomes in this session: {_tool_history(messages)}\n"
        f"- Concurrent sessions: {_active_sessions(getattr(agent, 'session_id', None))}\n"
        "Operating contract: inspect current state before changing it; use the tools that are actually listed; "
        "do not repeat completed work; re-check files before editing because another session may have changed them; "
        "continue through implementation and real verification until the user's observable outcome is met. "
        "Stop only with the completed result or a precise blocker backed by attempted alternatives.\n"
        "</execution-preflight>"
    )


__all__ = ["build_execution_preflight"]
