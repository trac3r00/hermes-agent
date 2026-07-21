# Qdrant memory provider

Imports/wraps the LAN Qdrant MCP REST bridge (`qdrant_upsert` / `qdrant_search`
/ `qdrant_delete`) as a Hermes `MemoryProvider`. The bridge server itself is
tracked separately at https://github.com/trac3r00/qdrant-mcp-server (its
`fix/scoped-recall-filter-contract` branch adds server-side tenant/profile
scope enforcement — see that repo's `docs/scoped-recall-plan.md`).

## Provenance

Prior to this commit this file existed only as an untracked, unversioned
deployed file at `~/.hermes/plugins/qdrant/__init__.py` with no git root and
no tracked copy anywhere (confirmed via repo-wide search across
`trac3r00/bob`, `trac3r00/hermes-agent`, and all other trac3r00 repos before
import — kanban task `t_b47bd68e`). Imported byte-for-byte as the ownership/
version-control prerequisite for the scoped-recall fix tracked in kanban task
`t_be525c8a`. The live file has NOT been modified as part of this import; the
copy here is the new source of truth for future edits.

## Follow-up (t_be525c8a)

This provider currently ignores the `agent_identity` (profile) and any
tenant context passed via `initialize(session_id, **kwargs)`, writes only a
bare `session_id`, and calls `qdrant_search` with no server-side filter.
Needs updating to pass `tenant`/`profile`/`session_id` on every
upsert/search call per the new bridge contract, once PR #1 on
`qdrant-mcp-server` lands.
