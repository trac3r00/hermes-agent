---
sidebar_position: 19
title: "LSP MCP Runtime"
description: "Prototype: MCP tool schemas and diagnostic classification for LSP-powered feedback"
---

# LSP MCP Runtime

A standalone prototype that wraps Hermes's existing `agent.lsp` infrastructure
into four MCP tool schemas and provides diagnostic classification helpers for
the post-edit feedback loop.

Primary file:

- `tools/lsp_mcp_runtime.py`

Related:

- `agent/lsp/` — language-server client, manager, server registry
- `agent/lsp/range_shift.py` — cross-edit delta filtering
- `tools/file_operations.py` — existing LSP integration in write/patch
- `tests/tools/test_lsp_mcp_runtime.py`

## Runtime boundary

`lsp_mcp_runtime.py` is a **pure-Python module** with zero import-time
side effects:

- **Schema dicts** are plain data — no registry registration on import.
- **Classification helpers** (`classify_diagnostic`, `filter_by_severity`,
  `has_blocking_errors`, `summarize_diagnostics`) are pure functions over
  diagnostic dicts. They never touch the network or filesystem.
- **Handlers** (`_handle_lsp_status`, `_handle_diagnostics`, ...) delegate
  to `agent.lsp.get_service()` at call time. Importing the module does NOT
  spawn a language server.
- **Tool registration** is opt-in via `register_tools()` — the caller
  decides when schemas enter the registry.

This separation means the classification helpers can be tested and used
independently of the LSP service lifecycle.

## Lifecycle

```
import lsp_mcp_runtime
     │
     ├─ schemas defined (module-level dicts)
     │
register_tools()           ◄── called by tool discovery or explicit setup
     │
     ├─ registry.register() for each of the four tools
     │  (check_fn probes agent.lsp.get_service())
     │
dispatch(lsp_status, args) ◄── model calls the tool
     │
     └─ handler calls agent.lsp.get_service()
        └─ LSPService manages per-language subprocesses
```

At no point does importing the module start a language server, create
a thread, or open a socket.

## Workspace mapping

Language servers are scoped to **git workspaces** — the same boundary
the existing `agent.lsp.workspace` module uses. The `lsp.status` tool
accepts an optional `workspace` path; when omitted, the server resolves
from the process cwd.

Each `(server_id, workspace_root)` pair gets one `LSPClient` instance,
lazily spawned on first use.

## Language-server startup and cache

The `agent.lsp.manager.LSPService` owns:

- **One asyncio event loop** in a daemon thread. All client I/O
  happens there; synchronous tool handlers block on the loop via
  `run(coro)`.
- **Lazy spawn**: the first request for a `(server_id, workspace_root)`
  key spawns the child process. Subsequent requests reuse it.
- **Broken-set**: pairs that fail to spawn or initialize are never
  retried for the life of the service.
- **Idle reap**: servers idle for >10 minutes are terminated.

The `lsp_mcp_runtime` module does not add its own caching — it
delegates entirely to the service singleton.

## Failure isolation

Every handler wraps its body in `try/except Exception` and returns a
JSON `{"error": "..."}` payload on failure. This matches the
`tools.registry.dispatch()` convention and ensures:

1. A broken language server never crashes the agent loop.
2. The model sees a clear error message and can retry or skip.
3. `check_fn` failures (service unavailable, missing binary) cause
   the tools to be excluded from the schema list rather than
   returning errors at call time.

The `agent.lsp.manager` broken-set ensures a server that fails
during initialization is permanently blacklisted for the session,
preventing repeated spawn-and-die cycles.

## Tool schemas

Four tools in the `"lsp"` toolset:

| Tool | Purpose |
|------|---------|
| `lsp.status` | Show running servers, health, tracked files |
| `diagnostics` | Get errors/warnings for a file (with severity filter) |
| `goto_definition` | Find where a symbol is defined (file + line) |
| `rename` | Rename a symbol across all references |

Schemas follow the OpenAI function-calling format:
`{"type": "function", "function": {...}}`.

All parameters are JSON-typed with descriptions. Required parameters
are listed explicitly; optional parameters have sensible defaults.

## Edit-after diagnostic blocking loop

After `patch` or `write_file` modifies a file, the agent should check
for new diagnostics before proceeding. The classification helpers
support this loop:

```
1. snapshot baseline diagnostics  (agent.lsp.range_shift applied)
2. apply edit via patch/write_file
3. wait for fresh diagnostics from the language server
4. filter: new = [d for d in fresh if is_new_diagnostic(d, baseline)]
5. block: if has_blocking_errors(new) → stop, report to model
6. classify: summarize_diagnostics(new) → "3 errors, 1 warning"
7. continue or retry
```

The helpers classify each diagnostic by kind (syntax, type, import,
name, unused, lint) using keyword heuristics on the message and code
fields. This gives the model actionable feedback: "import error at
line 42" is more useful than "1 error".

## Safety gates

1. **check_fn gating**: Tools only appear in the schema list when
   `agent.lsp.get_service()` returns an active service. No service →
   no tools → model never attempts LSP calls.

2. **Error isolation**: Every handler catches all exceptions and
   returns JSON errors. No handler can crash the agent loop.

3. **Severity filtering**: `filter_by_severity` defaults to
   `min_severity=ERROR` — the agent only sees errors unless it
   explicitly asks for warnings/info/hints.

4. **Diagnostic key deduplication**: `is_new_diagnostic` compares
   `(severity, code, source, message, range)` — the same tuple
   the `agent.lsp.manager` delta filter uses after line-shifting.
   This prevents shifted-but-identical diagnostics from appearing
   as new errors.

5. **Message sanitization**: All diagnostic content flowing through
   `agent.lsp.reporter` is HTML-escaped, field-capped, and
   control-character-stripped before reaching the model. This is
   handled by the existing reporter — `lsp_mcp_runtime` inherits
   it for free.

## Prototype status

This module is a design prototype. The `goto_definition` and
`rename` handlers return stub responses noting the async-LSP
request is not yet wired. The full implementation requires:

1. Async request support in `agent.lsp.client.LSPClient`
   (`textDocument/definition`, `textDocument/rename`).
2. Workspace edit application for `rename` (multi-file apply).
3. Integration with `tools/file_operations.py` post-edit loop.
4. Toolset wiring in `toolsets.py`.
