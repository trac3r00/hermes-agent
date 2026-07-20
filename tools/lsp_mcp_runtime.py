"""Standalone MCP tool schemas and diagnostic helpers for LSP integration.

This module is a **self-contained prototype** that defines:

1. Schemas for four LSP-flavored MCP tools:
   ``lsp.status``, ``diagnostics``, ``goto_definition``, ``rename``.

2. Diagnostic classification helpers used by the post-edit feedback
   loop after ``patch`` / ``write_file`` touch a file — the same loop
   that ``tools/file_operations.py`` already drives via the
   ``agent.lsp`` layer.

The module depends on **no Hermes internals** at import time —
schema dicts and classification helpers are pure-Python.  Handlers
are thin wrappers that delegate to ``agent.lsp.get_service()``
lazily at call time, so importing this module never spawns a
language server.

Lifecycle (documented in ``website/docs/developer-guide/lsp-mcp-runtime.md``):

    schema dicts ──► register_tools() opts into registry.register()
    handlers     ──► agent.lsp.get_service() at dispatch time
    diagnostics  ──► classify after every edit, block on errors
"""
from __future__ import annotations

import enum
import json
import logging
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Diagnostic severity / classification enums
# ---------------------------------------------------------------------------

class Severity(enum.IntEnum):
    """LSP diagnostic severity (1-indexed, matches the wire protocol)."""
    ERROR = 1
    WARNING = 2
    INFO = 3
    HINT = 4


class DiagnosticKind(enum.Enum):
    """Semantic classification of a diagnostic for post-edit feedback."""
    SYNTAX = "syntax"
    TYPE = "type"
    IMPORT = "import"
    NAME = "name"
    UNUSED = "unused"
    LINT = "lint"
    OTHER = "other"


# Heuristic keyword sets for classification.  Keys are lowercase
# substrings that appear in diagnostic messages from common language
# servers (pyright, gopls, rust-analyzer, tsserver, clangd).
_KIND_KEYWORDS: Dict[DiagnosticKind, FrozenSet[str]] = {
    DiagnosticKind.IMPORT: frozenset({
        "import", "module", "cannot be resolved", "no module named",
    }),
    DiagnosticKind.SYNTAX: frozenset({
        "syntax", "unexpected", "expected", "indent", "unterminated",
    }),
    DiagnosticKind.TYPE: frozenset({
        "type", "incompatible", "assignable",
    }),
    DiagnosticKind.NAME: frozenset({
        "undefined", "not defined", "name", "unresolved reference",
    }),
    DiagnosticKind.UNUSED: frozenset({
        "unused", "never", "unreachable", "redeclared",
    }),
}


def classify_diagnostic(diag: Dict[str, Any]) -> DiagnosticKind:
    """Classify a raw LSP diagnostic dict into a ``DiagnosticKind``.

    The classification is heuristic — it inspects ``message`` and
    ``code`` fields for keyword matches.  Returns ``OTHER`` when no
    heuristic fires.
    """
    message = str(diag.get("message", "")).lower()
    code = str(diag.get("code", "")).lower()
    combined = f"{message} {code}"
    for kind, keywords in _KIND_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return kind
    return DiagnosticKind.OTHER


# ---------------------------------------------------------------------------
# Post-edit diagnostic feedback
# ---------------------------------------------------------------------------

def classify_severity(diag: Dict[str, Any]) -> Severity:
    """Return the ``Severity`` enum value for a raw diagnostic dict."""
    return Severity(diag.get("severity", Severity.ERROR))


def is_new_diagnostic(
    diag: Dict[str, Any],
    baseline: Sequence[Dict[str, Any]],
) -> bool:
    """Return True when *diag* is not present in *baseline*.

    Comparison is by ``(severity, code, source, message, range)`` —
    the same key the ``agent.lsp.manager`` delta filter uses.  Line
    shifting is already applied to *baseline* by the caller (via
    ``agent.lsp.range_shift``) before this helper is called.
    """
    key = _diag_key(diag)
    return key not in {_diag_key(b) for b in baseline}


def _diag_key(d: Dict[str, Any]) -> Tuple:
    """Stable hashable key for a diagnostic."""
    rng = d.get("range") or {}
    start = rng.get("start") or {}
    end = rng.get("end") or {}
    return (
        d.get("severity"),
        d.get("code"),
        d.get("source"),
        d.get("message"),
        start.get("line"),
        start.get("character"),
        end.get("line"),
        end.get("character"),
    )


def filter_by_severity(
    diagnostics: Sequence[Dict[str, Any]],
    *,
    min_severity: Severity = Severity.ERROR,
) -> List[Dict[str, Any]]:
    """Return diagnostics at or above *min_severity*.

    ``min_severity=Severity.ERROR`` keeps only errors.
    ``min_severity=Severity.WARNING`` keeps errors + warnings.
    """
    return [d for d in diagnostics if classify_severity(d) <= min_severity]


def has_blocking_errors(
    diagnostics: Sequence[Dict[str, Any]],
) -> bool:
    """Return True when any diagnostic is an ERROR (severity 1).

    Used by the post-edit blocking loop: when ``True``, the agent
    should not proceed to the next edit without fixing the error.
    """
    return any(classify_severity(d) == Severity.ERROR for d in diagnostics)


def summarize_diagnostics(
    diagnostics: Sequence[Dict[str, Any]],
    *,
    include_kinds: bool = True,
) -> Dict[str, Any]:
    """Aggregate diagnostics into a summary dict.

    Returns ``{"errors": N, "warnings": N, "new": N, "kinds": {...}}``.
    The ``kinds`` sub-dict maps ``DiagnosticKind.value`` to count when
    *include_kinds* is True.
    """
    errors = 0
    warnings = 0
    kinds: Dict[str, int] = {}
    for d in diagnostics:
        sev = classify_severity(d)
        if sev == Severity.ERROR:
            errors += 1
        elif sev == Severity.WARNING:
            warnings += 1
        if include_kinds:
            kind = classify_diagnostic(d)
            kinds[kind.value] = kinds.get(kind.value, 0) + 1
    result: Dict[str, Any] = {"errors": errors, "warnings": warnings}
    if include_kinds:
        result["kinds"] = kinds
    return result


# ---------------------------------------------------------------------------
# MCP tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

LSP_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "lsp.status",
    "description": (
        "Show which language servers are running for the current workspace, "
        "their health, and the files they are tracking."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "Workspace root path. Defaults to the current working directory.",
            },
        },
        "required": [],
    },
}

DIAGNOSTICS_SCHEMA: Dict[str, Any] = {
    "name": "diagnostics",
    "description": (
        "Get LSP diagnostics (errors, warnings) for a file. Use after "
        "editing a file to check for issues the language server detected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
            "severity": {
                "type": "string",
                "enum": ["error", "warning", "info", "hint"],
                "description": "Minimum severity to return (default: error).",
            },
        },
        "required": ["path"],
    },
}

GOTO_DEFINITION_SCHEMA: Dict[str, Any] = {
    "name": "goto_definition",
    "description": (
        "Find the source location (file + line) where a symbol is defined. "
        "Useful for navigating unfamiliar codebases."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File containing the symbol.",
            },
            "line": {
                "type": "integer",
                "description": "1-indexed line number of the symbol.",
                "minimum": 1,
            },
            "character": {
                "type": "integer",
                "description": "1-indexed column number of the symbol.",
                "minimum": 1,
            },
        },
        "required": ["path", "line", "character"],
    },
}

RENAME_SCHEMA: Dict[str, Any] = {
    "name": "rename",
    "description": (
        "Rename a symbol across all files that reference it. Returns the "
        "list of files and positions that would be modified."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File containing the symbol.",
            },
            "line": {
                "type": "integer",
                "description": "1-indexed line number of the symbol.",
                "minimum": 1,
            },
            "character": {
                "type": "integer",
                "description": "1-indexed column number of the symbol.",
                "minimum": 1,
            },
            "new_name": {
                "type": "string",
                "description": "The new name for the symbol.",
            },
        },
        "required": ["path", "line", "character", "new_name"],
    },
}

ALL_LSP_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "lsp.status": LSP_STATUS_SCHEMA,
    "diagnostics": DIAGNOSTICS_SCHEMA,
    "goto_definition": GOTO_DEFINITION_SCHEMA,
    "rename": RENAME_SCHEMA,
}


def get_all_schemas() -> List[Dict[str, Any]]:
    """Return all LSP tool schemas in OpenAI function-calling wrapper format."""
    return [
        {"type": "function", "function": schema}
        for schema in ALL_LSP_SCHEMAS.values()
    ]


# ---------------------------------------------------------------------------
# Tool handlers (thin wrappers around agent.lsp)
# ---------------------------------------------------------------------------

def _handle_lsp_status(args: Dict[str, Any], **kwargs: Any) -> str:
    """Return LSP service status as JSON."""
    try:
        from agent.lsp import get_service
        svc = get_service()
        if svc is None:
            return json.dumps({"active": False, "message": "LSP service not available"})
        workspace = args.get("workspace")
        return json.dumps({
            "active": svc.is_active(),
            "workspace": workspace or "default",
        })
    except Exception as exc:
        return json.dumps({"error": f"lsp_status failed: {exc}"})


def _handle_diagnostics(args: Dict[str, Any], **kwargs: Any) -> str:
    """Return diagnostics for a file as JSON."""
    path = args.get("path", "")
    if not path:
        return json.dumps({"error": "path is required"})
    severity_str = args.get("severity", "error")
    severity_map = {
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.INFO,
        "hint": Severity.HINT,
    }
    min_sev = severity_map.get(severity_str, Severity.ERROR)
    try:
        from agent.lsp import get_service
        svc = get_service()
        if svc is None:
            return json.dumps({"file": path, "diagnostics": [], "note": "LSP not available"})
        raw = svc.diagnostics_for(path)
        filtered = filter_by_severity(raw, min_severity=min_sev)
        summary = summarize_diagnostics(filtered)
        return json.dumps({
            "file": path,
            "diagnostics": filtered,
            "summary": summary,
        })
    except Exception as exc:
        return json.dumps({"error": f"diagnostics failed: {exc}"})


def _handle_goto_definition(args: Dict[str, Any], **kwargs: Any) -> str:
    """Return definition location for a symbol as JSON."""
    path = args.get("path", "")
    line = args.get("line")
    character = args.get("character")
    if not path or line is None or character is None:
        return json.dumps({"error": "path, line, and character are required"})
    try:
        from agent.lsp import get_service
        svc = get_service()
        if svc is None:
            return json.dumps({"error": "LSP service not available"})
        return json.dumps({
            "file": path,
            "line": line,
            "character": character,
            "note": "goto_definition requires async LSP request — not yet wired in prototype",
        })
    except Exception as exc:
        return json.dumps({"error": f"goto_definition failed: {exc}"})


def _handle_rename(args: Dict[str, Any], **kwargs: Any) -> str:
    """Return rename edits as JSON."""
    path = args.get("path", "")
    line = args.get("line")
    character = args.get("character")
    new_name = args.get("new_name", "")
    if not path or line is None or character is None or not new_name:
        return json.dumps({"error": "path, line, character, and new_name are required"})
    try:
        from agent.lsp import get_service
        svc = get_service()
        if svc is None:
            return json.dumps({"error": "LSP service not available"})
        return json.dumps({
            "file": path,
            "line": line,
            "character": character,
            "new_name": new_name,
            "note": "rename requires async LSP request — not yet wired in prototype",
        })
    except Exception as exc:
        return json.dumps({"error": f"rename failed: {exc}"})


# ---------------------------------------------------------------------------
# Self-registration (wired into toolsets on import)
# ---------------------------------------------------------------------------
# Import-time registration follows the pattern in tools/file_tools.py.
# The toolset "lsp" is a new core toolset gated on the LSP service
# being active (check_fn probes get_service()).

def _check_lsp_available() -> bool:
    """Return True when the LSP service is active and usable."""
    try:
        from agent.lsp import get_service
        svc = get_service()
        return svc is not None and svc.is_active()
    except Exception:
        return False


def register_tools() -> None:
    """Register all LSP MCP tools with the central registry.

    Called explicitly rather than at module level to avoid side effects
    on import — the caller decides when tool registration should happen.
    """
    from tools.registry import registry

    _TOOLS = [
        ("lsp.status", LSP_STATUS_SCHEMA, _handle_lsp_status),
        ("diagnostics", DIAGNOSTICS_SCHEMA, _handle_diagnostics),
        ("goto_definition", GOTO_DEFINITION_SCHEMA, _handle_goto_definition),
        ("rename", RENAME_SCHEMA, _handle_rename),
    ]

    for name, schema, handler in _TOOLS:
        registry.register(
            name=name,
            toolset="lsp",
            schema=schema,
            handler=handler,
            check_fn=_check_lsp_available,
            emoji="🔍",
            max_result_size_chars=10_000,
        )
