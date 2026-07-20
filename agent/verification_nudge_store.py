from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


SESSION_NUDGE_CAP = 6


def session_nudge_count(session_id: str, root: str, scope_key: str) -> int:
    try:
        raw = _session_nudge_file(session_id, root, scope_key).read_text("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return 0
        return int(data.get("count", 0))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def record_session_nudge(session_id: str, root: str, scope_key: str) -> int:
    count = session_nudge_count(session_id, root, scope_key) + 1
    path = _session_nudge_file(session_id, root, scope_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"count": count, "root": root, "scope_key": scope_key}),
            encoding="utf-8",
        )
    except OSError:
        return count
    return count


def nudge_scope_key(status: dict[str, Any], paths: list[str]) -> str:
    payload = {
        "changed_paths": sorted({str(path) for path in paths}),
        "last_edit_at": str(status.get("last_edit_at") or ""),
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]


def _verify_nudge_dir() -> Path:
    home = os.environ.get("HERMES_HOME")
    base = Path(home).expanduser() if home else (Path.home() / ".hermes")
    return base / "verify_nudges"


def _session_nudge_file(session_id: str, root: str, scope_key: str) -> Path:
    key = hashlib.sha1(
        f"{session_id}\x00{root}\x00{scope_key}".encode("utf-8")
    ).hexdigest()[:20]
    return _verify_nudge_dir() / f"{key}.json"
