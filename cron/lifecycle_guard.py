"""Gateway lifecycle guard for cron job creation (#30719).

An agent running inside a gateway can schedule a cron job that calls
``hermes gateway restart`` (or ``launchctl kickstart ai.hermes.gateway``
or ``systemctl restart hermes-gateway``).  When the cron fires, the
gateway dies, the supervisor (launchd KeepAlive / systemd Restart=)
revives it, auto-resume picks up the offending session, and the resumed
turn re-runs the same logic — a SIGTERM-respawn loop every ~10 seconds
until manually broken.

This module rejects cron job specs whose prompt or script contains a
direct shell-level gateway-lifecycle command.  It is enforced at
``cron.jobs.create_job`` so it fires on every job-creation path: the
``hermes cron create`` CLI subcommand AND the agent's ``cronjob`` model
tool (which calls ``create_job`` directly, bypassing the CLI layer).

The pattern is intentionally command-shaped: it anchors on a concrete
command identifier (``hermes gateway``, ``launchctl ... hermes-gateway``,
``systemctl ... hermes-gateway``, ``pkill`` against the gateway) so it
cannot fire on prose.  A cron ``prompt`` is fed to a future LLM, not a
shell, so an over-broad substring match on English ("Kong API gateway
autoscaling and restart behavior") would produce a high false-positive
rate without preventing the actual foot-gun, which requires a real
command shape.

This is a defence-in-depth layer.  ``tools/terminal_tool.py`` already
blocks these commands at *execution* time when ``_HERMES_GATEWAY=1``, and
``hermes gateway stop|restart`` refuse to self-target from inside the
gateway.  Blocking at *creation* time as well means the agent gets an
immediate, informative rejection instead of scheduling a job that will
only fail (silently) when it fires.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
from pathlib import Path
from typing import Any, Optional


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Shell-level command shapes that target the gateway lifecycle. Each branch
# is anchored on a concrete command identifier so a match can only fire on
# actual shell-command-shaped strings, not on prose.
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: `hermes gateway restart|stop` — the canonical foot-gun.
    # `start` is intentionally excluded: starting a gateway from inside a
    # gateway is benign (a no-op or "already running" error), and a
    # legitimate cron job might start a sibling profile's gateway.
    r"(?:hermes\s+gateway\s+(?:restart|stop))"
    # Branch B: launchctl ops on a hermes-gateway label. macOS launchd
    # labels look like `ai.hermes.gateway` / `hermes-gateway`. Requiring the
    # gateway identifier prevents blocking unrelated hermes services (e.g.
    # `launchctl unload ai.hermes.update-checker.plist`).
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch B2: transient launchd submission whose label/payload names the
    # gateway. This catches delayed one-shot wrappers such as
    # ``launchctl submit -l ai.hermes.gateway-restart-once -- ...``.
    r"|(?:launchctl\s+submit\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch C: systemctl ops on a hermes-gateway unit.
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch D: pkill / kill targeting the hermes gateway process. Both
    # token orders because real reproductions show both.
    r"|(?:p?kill\b[^\n]*\bhermes\b[^\n]*\bgateway)"
    r"|(?:p?kill\b[^\n]*\bgateway\b[^\n]*\bhermes)"
)


def _local_payload_scripts(text: str) -> tuple[Path, ...]:
    """Return readable local script paths referenced by command-like text.

    This covers both transient launchd commands and ``ProgramArguments`` read
    from a plist. Inspecting the payload closes the wrapper indirection without
    relying on a suspicious filename or label.
    """
    paths: list[Path] = []
    for line in text.splitlines():
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            tokens = line.split()
        for token in tokens:
            candidate = Path(os.path.expandvars(token)).expanduser()
            if candidate.suffix.lower() not in {".sh", ".bash", ".zsh", ".command"}:
                continue
            if candidate.is_file() and candidate not in paths:
                paths.append(candidate)
    return tuple(paths)


def _payload_contains_gateway_lifecycle(payload: str, *, depth: int = 0) -> bool:
    """Scan command text and readable local script/plist payloads recursively."""
    if _GATEWAY_LIFECYCLE_PATTERN.search(payload):
        return True
    if depth >= 2:
        return False
    for script_path in _local_payload_scripts(payload):
        try:
            script = script_path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        if _payload_contains_gateway_lifecycle(script, depth=depth + 1):
            return True
    return False


def _candidate_persistence_files(arguments: dict[str, Any]) -> tuple[Path, ...]:
    """Extract local scripts/plists targeted by a file-write tool call."""
    candidates: list[Path] = []
    path = arguments.get("path")
    if isinstance(path, str) and path.strip():
        candidates.append(Path(os.path.expandvars(path)).expanduser())
    patch = arguments.get("patch")
    if isinstance(patch, str):
        for match in re.finditer(
            r"^\*\*\*\s*(?:Update|Add)\s+File:\s*(.+)$", patch, re.MULTILINE
        ):
            candidates.append(
                Path(os.path.expandvars(match.group(1).strip())).expanduser()
            )
    return tuple(candidates)


def _plist_payload(content: str) -> str:
    try:
        parsed = plistlib.loads(content.encode("utf-8"))
    except Exception:
        return content
    if not isinstance(parsed, dict):
        return content
    label = parsed.get("Label", "")
    args = parsed.get("ProgramArguments", [])
    program = parsed.get("Program", "")
    return " ".join([str(label), str(program), *(str(item) for item in args)])


def file_write_creates_gateway_restart_persistence(arguments: dict[str, Any]) -> bool:
    """Detect script/plist writes that can later restart the current gateway.

    Direct file tools bypass terminal approval. This preflight lets the gateway
    reject one-shot launchd wrappers before they are written, including a plist
    that points at an already-existing restart script.
    """
    raw_contents = [
        value
        for key in ("content", "new_string", "patch")
        if isinstance((value := arguments.get(key)), str)
    ]
    candidates = _candidate_persistence_files(arguments)
    for candidate in candidates:
        suffix = candidate.suffix.lower()
        for content in raw_contents:
            payload = _plist_payload(content) if suffix == ".plist" else content
            if _payload_contains_gateway_lifecycle(payload):
                return True
    return False


def serialized_tool_call_creates_gateway_restart_persistence(text: str) -> bool:
    """Best-effort detector for serialized write_file/patch tool calls."""
    if not text or not re.search(r'(?i)"?(?:write_file|patch)"?', text):
        return False
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False

    def _walk(node: Any) -> bool:
        if isinstance(node, list):
            return any(_walk(item) for item in node)
        if not isinstance(node, dict):
            return False
        name = node.get("name")
        arguments = node.get("arguments")
        function = node.get("function")
        if isinstance(function, dict):
            name = function.get("name", name)
            arguments = function.get("arguments", arguments)
        if name in {"write_file", "patch"}:
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    arguments = {}
            if isinstance(arguments, dict) and file_write_creates_gateway_restart_persistence(arguments):
                return True
        return any(_walk(item) for item in node.values())

    return _walk(value)


def contains_gateway_lifecycle_command(text: str) -> bool:
    """Return True if *text* or a submitted launchd script targets the gateway."""
    if not text:
        return False
    return _payload_contains_gateway_lifecycle(text)


def _resolve_script_path(script_path: str) -> Path:
    """Resolve a cron ``script`` value the same way the scheduler does.

    The scheduler (``cron.scheduler``) resolves a bare/relative script path
    under ``<HERMES_HOME>/scripts/`` and only accepts absolute paths as-is.
    We MUST mirror that here so the guard scans the file that will actually
    run — otherwise a job whose script lives at the scheduler's real location
    (``~/.hermes/scripts/restart.sh``) but is passed as the bare name
    ``restart.sh`` would read as a nonexistent relative path and silently
    scan prompt-only content, letting the command through.
    """
    from hermes_constants import get_hermes_home

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        return raw
    return get_hermes_home() / "scripts" / raw


def _read_script_for_scanning(script_path: str) -> str:
    """Read a script file for lifecycle-pattern scanning.

    Decodes with ``errors="replace"`` so binary or non-UTF-8 content does not
    silently bypass the check — a plain text-mode read raises
    ``UnicodeDecodeError`` on such files, and swallowing that error would let
    an attacker hide the command in binary noise.  Returns an empty string
    only when the file cannot be read at all.
    """
    try:
        return _resolve_script_path(script_path).read_bytes().decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def check_gateway_lifecycle(
    prompt: Optional[str],
    script: Optional[str] = None,
) -> None:
    """Raise ``GatewayLifecycleBlocked`` if *prompt* or *script* contains a
    gateway-lifecycle command pattern.

    ``prompt`` is scanned directly.  ``script``, when supplied, is read from
    disk and concatenated for the scan.  Both are considered together so a
    job cannot slip through by splitting the command across the prompt and
    the script.

    Callers should let the exception propagate when they want the create to
    fail with a ``ValueError``-shaped error (the agent's ``cronjob`` tool
    surfaces this as a tool error; the CLI prints it in red and exits 1).
    """
    combined = prompt or ""
    if script:
        script_text = _read_script_for_scanning(script)
        if script_text:
            combined = f"{combined}\n{script_text}"

    if contains_gateway_lifecycle_command(combined):
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command "
            "(restart/stop/kill). This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )
