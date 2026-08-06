# Hermes/Bob Capability Observability: Stable IDs & Decision/Outcome Event Schema v1

**Status:** design proposal  
**Schema ID:** `hermes.capability-observability`  
**Schema version:** `1.0.0`  
**Scope:** local-first, opt-in event ledger for Tool · Skill · Plugin · Gate · model routing · invocation · outcome attribution. This is **not** outbound analytics by default.

## 1. Goals and non-goals

### Goals

1. Answer, per session/turn/run: *which capabilities were candidates, which were selected/invoked, what concise policy reason applied, and what outcome occurred?*
2. Give every catalog object a durable identity independent of display labels, locations, and execution instances.
3. Measure **correct use** rather than only call volume: eligibility, selection, invocation, result, verification/evidence, and whether a loaded skill was followed by a relevant action.
4. Preserve existing prompt-cache behavior: collection must not mutate system prompts, tool schemas, messages, or toolset composition mid-turn.
5. Make model/fallback/delegation/verification decisions explainable with an allowlisted reason code, **never chain-of-thought or hidden reasoning**.
6. Make event export safe by default through local persistence, redaction, bounded fields, hashes, sampling, and cardinality controls.

### Non-goals

- Persisting raw user/assistant messages, tool arguments/results, secrets, raw provider responses, chain-of-thought, or opaque plugin payloads.
- Replacing `messages`, `sessions`, `session_model_usage`, `.usage.json`, approval records, or logs in v1.
- Adding a vendor analytics backend. Any export must remain disabled until a generic user-facing opt-in gate/setup toggle exists (consistent with repository policy).

## 2. Canonical IDs: catalog identity != runtime invocation identity

All IDs are lowercase ASCII and use `:` as namespace separators; arbitrary names are normalized with `[a-z0-9][a-z0-9._/-]{0,127}`. Reject/escape invalid input rather than silently changing identity. IDs must not contain paths, user IDs, secrets, or argument values.

| Entity | Stable catalog ID (durable semantic identity) | Runtime instance ID (unique event occurrence) | Notes |
|---|---|---|---|
| Tool | `tool:core:<name>`; `tool:plugin:<plugin-key>:<name>`; `tool:mcp:<server-key>:<name>`; `tool:memory:<provider-key>:<name>`; `tool:context-engine:<engine-key>:<name>` | `inv:tool:<ULID>` | A tool registration has an immutable catalog ID; every emitted provider tool call receives a new invocation ID. Tool aliases/bridge calls also retain `provider_tool_call_id`. |
| Toolset | `toolset:<name>` | n/a | Snapshot records requested/resolved/available/deferred toolsets. |
| Skill | `skill:core:<canonical-name>`; `skill:user:<canonical-name>`; `skill:external:<source-key>:<canonical-name>`; `skill:plugin:<plugin-key>:<canonical-name>` | `inv:skill-load:<ULID>` | Canonical name is resolved frontmatter name + provenance, not just the caller's alias. A supporting file is represented by `artifact:skill-file:<skill-id>:<sha256-prefix>`. |
| Plugin | `plugin:<source>:<manifest-key>` | `run:plugin-hook:<ULID>` | `source ∈ {bundled,user,project,entrypoint}`. Use manifest `key` if present, else manifest `name`; never Python module name as canonical public ID. |
| Gate | `gate:<domain>:<name>` | `decision:gate:<ULID>` | Examples: `gate:tool-scope:allowed`, `gate:plugin-pre-tool:block`, `gate:guardrail:tool-loop`, `gate:approval:dangerous-command`, `gate:verify:pre-verify`, `gate:toolset:availability`. |
| Model target | `model:<provider-key>:<model-slug>` | `decision:model:<ULID>` | `provider-key` comes from resolved provider config; base URL is represented only by a salted endpoint fingerprint, never raw endpoint if it contains tenant/private host data. |
| Capability decision | n/a | `decision:<kind>:<ULID>` | `kind ∈ {capability,model,gate,verification,delegation}`. A decision links candidates to selected/rejected catalog IDs. |
| Session | existing `sessions.id`, normalized as `session:<id>` in event payloads | n/a | Do not replace existing session IDs. |
| Turn | n/a | existing `agent._current_turn_id`, normalized `turn:<id>` | `TurnContext.build_turn_context` already generates a unique ID. |
| Agent run | n/a | `run:agent:<ULID>` | Generated at `AIAgent.run_conversation` entry; child/delegated runs have `parent_run_id`. |
| Evidence | content-addressed `evidence:<kind>:sha256:<digest>` | n/a | Metadata-only evidence such as test exit class, file-diff summary, provider usage bucket. Never hash raw secret-bearing payload without redaction/canonicalization. |

### Catalog registration and revisions

Every catalog object has a **logical ID** plus a non-identity revision fingerprint:

```json
{
  "catalog_id": "tool:core:read_file",
  "catalog_revision": "sha256:8f0c…",
  "schema_hash": "sha256:41d9…",
  "origin": {"kind": "core", "module": "tools.file_tools"}
}
```

- Logical IDs survive code/schema/docs changes; `catalog_revision` changes when normalized relevant metadata changes.
- `tool` revision input: canonical tool ID, toolset, normalized schema after dynamic overrides/sanitization, handler module+qualified name, ownership/plugin ID, availability contract name. Exclude descriptions only if it is deliberately considered non-semantic; recommended: include it in `schema_hash` because the model sees it.
- `skill` revision input: normalized SKILL.md bytes after newline normalization; supporting files receive their own artifact hashes. Do **not** record skill content in events.
- `plugin` revision input: normalized manifest (with credential values omitted) + package/version + optional source bundle digest.
- `gate` revision input: gate ID + policy/config version/fingerprint, never raw policy text or command.
- `prompt_hash`: SHA-256 of canonical system-prompt bytes; `toolset_hash`: SHA-256 of sorted `{catalog_id,schema_hash}` advertised to the model; `skill_index_hash`: sorted `{skill_id,revision,description_hash}`. `plugin_set_hash` and `gate_policy_hash` use the equivalent sorted tuples.

## 3. Event envelope (append-only JSON Lines)

One line is one immutable event. Consumers must tolerate unknown fields and versions. Store events locally in a separate SQLite/event-log sink; no changes to `messages` FTS, and no raw text joins.

```json
{
  "$schema": "https://hermes-agent.nousresearch.com/schemas/capability-observability/1.0.0/event.json",
  "schema_id": "hermes.capability-observability",
  "schema_version": "1.0.0",
  "event_id": "evt:01J…",
  "event_type": "capability.decision",
  "occurred_at": "2026-07-24T12:34:56.789Z",
  "monotonic_ms": 12345,
  "run_id": "run:agent:01J…",
  "session_id": "session:existing-session-id",
  "turn_id": "turn:existing-turn-id",
  "parent_event_id": null,
  "causation_id": "decision:capability:01J…",
  "trace_flags": {"local_only": true, "sampled": true, "redaction_version": "v1"},
  "snapshot": {
    "prompt_hash": "sha256:…",
    "toolset_hash": "sha256:…",
    "skill_index_hash": "sha256:…",
    "plugin_set_hash": "sha256:…",
    "gate_policy_hash": "sha256:…",
    "config_fingerprint": "sha256:…",
    "registry_generation": 42
  },
  "actor": {"kind": "agent", "agent_id": "agent:primary"},
  "payload": {}
}
```

### Required event types and payloads

#### A. `catalog.observed`
Emitted once per catalog object/revision per process boot (and on registry/plugin/MCP refresh); deduplicated by `{catalog_id,catalog_revision}`.

```json
{
  "catalog": {"catalog_id": "tool:plugin:web/brave:brave_search", "kind": "tool", "catalog_revision": "sha256:…"},
  "owner_plugin_id": "plugin:bundled:web/brave",
  "toolset_id": "toolset:web",
  "availability": "available",
  "availability_reason_code": "AVAIL_CONFIGURED"
}
```

#### B. `capability.decision`
Captures candidate/selected capability selection (toolset resolution, runtime tool availability, tool-search deferral/unwrapping, skill matching/loadability). It is intentionally a **structured operational explanation**, not a model rationale.

```json
{
  "decision_id": "decision:capability:01J…",
  "decision_kind": "tool_advertisement",
  "candidate_summary": {"total": 31, "included": 18, "excluded": 13, "top_k": 50, "overflow_count": 0},
  "candidates": [
    {"catalog_id": "tool:core:read_file", "state": "selected", "reason_code": "SELECTED_TOOLSET_ENABLED"},
    {"catalog_id": "tool:plugin:web/brave:brave_search", "state": "deferred", "reason_code": "DEFERRED_PROGRESSIVE_DISCLOSURE"},
    {"catalog_id": "tool:core:terminal", "state": "excluded", "reason_code": "EXCLUDED_TOOLSET_DISABLED"}
  ],
  "selected_ids": ["tool:core:read_file"],
  "reason_code": "TOOLSET_RESOLVED",
  "reason_detail": null
}
```

`candidates` is bounded (default 50). When overflow occurs, preserve grouped `{state, reason_code, count}` only. No user text, prompt, tool arguments, or free-form model reasoning.

#### C. `model.decision`
Emitted for initial resolved runtime, explicit model switch, automatic fallback candidate evaluation, and selected fallback.

```json
{
  "decision_id": "decision:model:01J…",
  "decision_kind": "fallback",
  "candidate_models": [
    {"catalog_id": "model:custom:codex/gpt-5.6-terra", "state": "rejected", "reason_code": "REJECTED_CURRENT_FAILURE"},
    {"catalog_id": "model:openai:gpt-5", "state": "selected", "reason_code": "SELECTED_FALLBACK_NEXT_ELIGIBLE"}
  ],
  "selected_model_id": "model:openai:gpt-5",
  "reason_code": "FAILOVER_RATE_LIMIT",
  "provider_error_class": "http_429",
  "endpoint_fingerprint": "sha256:…"
}
```

#### D. `gate.decision`
Every enforcement/decision point uses a stable gate ID and records `allow|block|continue|require_approval|skip|defer|error`. For a fan-out hook, emit one aggregate gate decision plus optional per-plugin child decisions (bounded). Gate payloads must never include raw shell commands, user messages, tool arguments, or hook return bodies.

```json
{
  "decision_id": "decision:gate:01J…",
  "gate_id": "gate:guardrail:tool-loop",
  "subject_id": "inv:tool:01J…",
  "decision": "block",
  "reason_code": "BLOCK_GUARDRAIL_POLICY",
  "policy_revision": "sha256:…",
  "enforcer": {"kind": "core", "catalog_id": "gate:guardrail:tool-loop"},
  "evidence_ids": ["evidence:policy:sha256:…"]
}
```

#### E. `tool.invocation` and `tool.outcome`
`tool.invocation` is emitted immediately after argument parsing and bridge unwrap, before middleware/pre-tool gate/dispatch. `tool.outcome` is emitted exactly once in a `finally`-style completion path, including blocks, cancellation, malformed calls, timeout, and dispatch exception.

```json
{
  "invocation_id": "inv:tool:01J…",
  "tool_id": "tool:core:read_file",
  "provider_tool_call_id": "call_abc",
  "original_tool_id": "tool:core:tool_call",
  "toolset_id": "toolset:file",
  "execution_mode": "sequential",
  "argument_shape_hash": "sha256:…",
  "argument_key_set": ["path","offset","limit"],
  "argument_size_bucket": "257-1024",
  "candidate_decision_id": "decision:capability:01J…"
}
```

```json
{
  "invocation_id": "inv:tool:01J…",
  "status": "succeeded",
  "outcome_code": "OUTCOME_OK",
  "duration_ms": 82,
  "result_shape": "json_object",
  "result_size_bucket": "1025-4096",
  "error_class": null,
  "effect_disposition": "read_only",
  "evidence_ids": ["evidence:tool-result:sha256:…"],
  "verification_state": "not_required"
}
```

`argument_shape_hash` is derived from redacted/canonicalized *shape*, e.g. key names, JSON types, lengths/buckets, path class (`workspace_relative`, `home`, `external`, `unknown`); it is not a hash of raw values. Result evidence is a hash of a sanitized, bounded, nonrecoverable result summary plus `{status,error_class}`.

#### F. `skill.load`, `skill.use-assessment`, `plugin.hook`, `verification.outcome`, `run.outcome`

- `skill.load`: `invocation_id`, resolved `skill_id`, main/supporting artifact hash, `load_status`, `reason_code`, readiness bucket. Generated after resolution in `skill_view`; alias and ambiguous/not-found results are represented without reading content.
- `skill.use-assessment`: emitted at turn end only for skills loaded in the turn. Uses deterministic, explainable indicators below; it must report `unknown` rather than invent compliance.
- `plugin.hook`: hook ID `plugin-hook:<hook-name>`; plugin ID; status; duration; return **shape** only (`none`, `string`, `directive`, `error`); sanitized/bounded decision code. Never hook output text.
- `verification.outcome`: linked to tool invocation/evidence; `verification_kind` (`test`, `syntax`, `readback`, `user_confirmation`, `gate`, `none`), `status`, evidence IDs.
- `run.outcome`: one terminal outcome per agent run: `completed|failed|cancelled|budget_exhausted|handoff|interrupted`; aggregate counts and hashes only.

## 4. Reason code taxonomy

Codes are versioned, finite, and machine-stable. Human-visible localized text is a separate lookup table. Do not emit arbitrary reason prose; optional `reason_detail` is an allowlisted short token (max 96 chars) only.

| Family | Required codes (v1 examples) |
|---|---|
| Catalog/availability | `AVAIL_CONFIGURED`, `AVAIL_CHECK_PASSED`, `UNAVAIL_CHECK_FAILED`, `UNAVAIL_MISSING_REQUIREMENT`, `UNAVAIL_PLUGIN_DISABLED`, `UNAVAIL_PLATFORM_UNSUPPORTED`, `UNAVAIL_REGISTRY_MISSING`, `CATALOG_REVISION_CHANGED` |
| Capability selection | `TOOLSET_RESOLVED`, `SELECTED_TOOLSET_ENABLED`, `EXCLUDED_TOOLSET_DISABLED`, `EXCLUDED_SCOPE`, `EXCLUDED_AVAILABILITY`, `DEFERRED_PROGRESSIVE_DISCLOSURE`, `SELECTED_TOOL_SEARCH_UNWRAP`, `SKILL_EXACT_MATCH`, `SKILL_QUALIFIED_PLUGIN`, `SKILL_AMBIGUOUS`, `SKILL_NOT_FOUND`, `SKILL_DISABLED`, `SKILL_UNSUPPORTED`, `SKILL_SETUP_NEEDED` |
| Model decision | `MODEL_INITIAL_RESOLUTION`, `MODEL_EXPLICIT_SWITCH`, `FAILOVER_RATE_LIMIT`, `FAILOVER_BILLING`, `FAILOVER_UPSTREAM_ERROR`, `FAILOVER_CONTEXT_OVERFLOW`, `SELECTED_FALLBACK_NEXT_ELIGIBLE`, `REJECTED_CURRENT_FAILURE`, `REJECTED_FALLBACK_DUPLICATE`, `REJECTED_FALLBACK_UNCONFIGURED`, `REJECTED_FALLBACK_COOLDOWN`, `FALLBACK_CHAIN_EXHAUSTED` |
| Gate decision | `ALLOW`, `BLOCK_PLUGIN_POLICY`, `BLOCK_GUARDRAIL_POLICY`, `BLOCK_TOOL_SCOPE`, `BLOCK_MALFORMED_ARGUMENTS`, `BLOCK_APPROVAL_DENIED`, `REQUIRE_APPROVAL`, `APPROVAL_TIMEOUT`, `SKIP_INTERRUPT`, `SKIP_WAKE_GATE`, `CONTINUE_PRE_VERIFY`, `ALLOW_PRE_VERIFY`, `GATE_ERROR_FAIL_OPEN`, `GATE_ERROR_FAIL_CLOSED` |
| Invocation/outcome | `OUTCOME_OK`, `OUTCOME_ERROR`, `OUTCOME_TIMEOUT`, `OUTCOME_CANCELLED`, `OUTCOME_INTERRUPTED`, `OUTCOME_BLOCKED`, `OUTCOME_MALFORMED`, `OUTCOME_UNAVAILABLE`, `OUTCOME_PARTIAL`, `OUTCOME_CONTRACT_ERROR`, `OUTCOME_RETRIED` |
| Verification/evidence | `VERIFY_NOT_REQUIRED`, `VERIFY_PENDING`, `VERIFY_PASSED`, `VERIFY_FAILED`, `VERIFY_SKIPPED_NO_EVIDENCE`, `VERIFY_USER_CONFIRMED`, `VERIFY_BUDGET_EXHAUSTED` |
| Telemetry health | `TELEMETRY_DISABLED`, `TELEMETRY_SAMPLED_OUT`, `TELEMETRY_REDACTED`, `TELEMETRY_DROPPED_QUEUE_FULL`, `TELEMETRY_WRITE_ERROR`, `BACKFILL_INFERRED`, `BACKFILL_UNAVAILABLE` |

## 5. Linkage graph and lifecycle

```text
session ──contains──> run ──contains──> turn
                           │              │
                           │              ├─produces──> capability.decision ──selects──> catalog(tool/skill/plugin/gate)
                           │              ├─produces──> model.decision ──selects──> model catalog
                           │              ├─causes──> tool.invocation ──targets──> tool catalog
                           │              │                    └─has──> gate.decision(s) ──enforced-by──> gate catalog/plugin hook
                           │              ├─causes──> tool.outcome ──supports──> evidence
                           │              ├─contains──> skill.load ──assessed-by──> skill.use-assessment
                           │              └─contains──> verification.outcome ──uses──> evidence
                           └─ends-with──> run.outcome
```

Rules:

1. `event_id` is unique and sortable (ULID); `invocation_id`, `decision_id`, and `run_id` are never reused.
2. Every tool outcome references exactly one invocation; blocked/malformed/cancelled calls still receive an invocation + outcome so denominators remain honest.
3. An invocation may reference zero or more gate decisions; a gate decision references exactly one subject (`invocation_id`, `decision_id`, or run/turn).
4. Event causal order is logical (`parent_event_id`, `causation_id`), not inferred solely from wall clock; concurrent tool workers can finish out of order.
5. Child/delegate agents receive a new `run_id`, a child `session_id` when applicable, and `parent_run_id`/`parent_invocation_id` linking to `delegate_task`.
6. The provider's `tool_call.id` is transport correlation only, not a canonical tool/invocation ID.

## 6. Correct-use metrics (measurable without mind-reading)

### Tool

| Metric | Numerator / denominator | Interpretation |
|---|---|---|
| Advertisement coverage | selected advertised tools / available tools | Toolset resolution health. |
| Invocation success | `OUTCOME_OK` / all outcomes | Reliability, not quality. |
| Prevented unsafe invocation | blocked outcomes / all attempted invocations | Policy/gate visibility; not a failure by itself. |
| Verification closure | invocations requiring verification with `VERIFY_PASSED|USER_CONFIRMED` / invocations requiring verification | Evidence-backed completion. |
| Rework rate | same tool ID + same argument-shape hash retried after error / invocations | Detects brittle tool use without retaining args. |
| Tool-search precision | bridge resolution invokes an actually advertised deferred target / tool-search bridge calls | Ensures progressive disclosure resolves real catalog IDs. |

### Skill

A `skill_view` count alone is **load**, not proof of correct application. For each `skill.load`, calculate only deterministic indicators and retain them separately:

- `loaded`: successful `skill.load`.
- `prerequisites_known`: readiness status captured.
- `followup_action`: at least one subsequent invocation whose tool ID is in an optional declared `expected_tool_ids` / `verification_tool_ids` skill metadata list.
- `verification_observed`: relevant verification outcome exists after a mutation task; a skill can declare `expects_verification: true`.
- `contradicted_by_gate`: a loaded skill's required action was blocked/failed (not necessarily incorrect use).
- `use_assessment`: `supported | insufficient_evidence | not_applicable | contradicted`; **never** `followed` based on latent reasoning.

Initial v1 keeps existing `.usage.json` untouched; it may be enriched later from `skill.load` counts only after an explicit migration.

### Plugin and gate

- Hook reliability: successful hook completion / hook invocation, by `plugin_id + hook_name + revision`.
- Enforcement effectiveness: gate blocks followed by no attempted dispatch / blocks; gate latency p50/p95.
- Fail-open/fail-closed counts and reasons, separately surfaced as safety signals.
- Never rank a plugin by raw number of blocks alone; aggregate policy context is required.

## 7. Privacy, redaction, retention, and cardinality

### Data tiers

1. **Default local ledger (recommended):** metadata, stable IDs, reason/outcome codes, timestamps, durations, buckets, and cryptographic fingerprints only.
2. **Local debug evidence (off by default, short TTL):** sanitized error class/stack category and strictly bounded result *shape*; still no messages/arguments/results.
3. **Export:** disabled by default. Requires `observability.export.enabled: true` plus an explicit setup/UI confirmation. Export applies a second redaction pass and per-event allowlist.

### Never persist in this schema

- Prompt/system prompt/user/assistant content; hidden reasoning; tool arguments or tool results; raw commands; paths beyond classification; HTTP headers; API keys/tokens/cookies; raw plugin hook payload; raw provider error bodies; email/phone/contact IDs; source IP; base URL unless a separately-approved privacy policy permits it.

### Redaction design

- Reuse `agent.redact.redact_sensitive_text(..., force=True)` principles; create a dedicated `observability.redact_event_v1()` that redacts before serialization and validates against a deny-key list (`api_key`, `authorization`, `cookie`, `token`, `password`, `secret`, etc.).
- Use HMAC-SHA-256 with a per-profile local salt for correlation fingerprints. Plain SHA only for non-sensitive immutable bundled artifacts/schemas. Rotating the salt makes old/new sensitive fingerprints intentionally unlinkable.
- Normalize then hash. Never log a raw input merely because it is hashed; dictionary attacks are practical for commands, paths, and short messages.
- Emit `redaction_version`, `redacted_field_count`, and `TELEMETRY_REDACTED` without naming secret fields.

### Cardinality/volume controls

- Stable IDs are catalog-governed. Plugin/MCP IDs are capped at 128 chars and normalized; unknown plugin/MCP IDs collapse to `*:other` plus local bounded fingerprint.
- Candidate lists: max 50 items/event; overflow is reason-coded counts.
- Argument keys: max 32; result/evidence metadata: max 16 keys; optional detail: max 96 chars; no arbitrary labels/tags.
- Durations bucketed for aggregate export (`0-10`, `11-100`, `101-1000`, `1001-10000`, `10001+`) while local ledger may retain integer ms.
- Per-run event budget default 2,000; reserve terminal `run.outcome` and telemetry-drop aggregate event. Coalesce repeated availability/catalog observations and identical retries.
- Retention defaults: local 30 days or 100 MiB per profile, whichever comes first; compact into daily aggregate rows before deletion. Debug evidence 24h. Export queue 7d max.

## 8. Historical backfill rules

Backfill is **inferred**, append-only, and must never fabricate unavailable relationships.

| Source | Can infer | Must mark | Cannot infer |
|---|---|---|---|
| `sessions` | session IDs, source, existing model, aggregate counts, start/end | `BACKFILL_INFERRED`, confidence `high` for direct columns | run/turn IDs for legacy sessions unless a turn ID persisted elsewhere |
| `messages.tool_calls` | provider tool call name, order, raw provider tool-call ID where stored | `invocation_id` newly generated; confidence `medium` | canonical plugin/MCP ownership if registry snapshot is missing; whether call executed |
| `messages` role=`tool`, `tool_name`, `tool_call_id`, `effect_disposition` | possible tool outcome and matching by `tool_call_id` | `outcome_inferred=true`, `confidence=medium|low` | duration, gate decisions, argument shape, verification, exact selected candidates |
| `session_model_usage` / session model columns | model usage aggregates | `MODEL_INITIAL_RESOLUTION` only when direct | fallback chain/reason/model choice at each API call |
| `.usage.json` | historical skill view/use counters/timestamps | `BACKFILL_INFERRED`, aggregated event only | per-turn skill load, correct-use assessment |
| `async_delegations` | delegation IDs/status/times/parent session | `confidence=high` for stored links | child tool invocations unless child session data exists |
| `tool_calls.log` | optional operational aid if enabled | `confidence=low`, redaction mandatory | use as authoritative source; it may contain sensitive arguments |

Backfill algorithm:

1. Write `backfill.manifest` containing source DB fingerprint, schema version, started/completed timestamp, code version, and no raw content.
2. Derive `legacy_catalog_id` only where unambiguous (`tool:core:<name>` for known historic core tools); otherwise use `tool:legacy:unknown` + HMAC name fingerprint and `BACKFILL_UNAVAILABLE` reason.
3. Match assistant tool calls to tool rows by `tool_call_id`; if missing, do **not** positional-match across retries/compaction—emit unlinked aggregate counts.
4. Never recompute a historical toolset/prompt/plugin/gate hash from today’s registry and claim it was historical. Set field `null` and `snapshot_availability: "unavailable"`.
5. Idempotency key: `backfill:<source-db-fingerprint>:<source-table>:<primary-key>:v1`; reruns upsert/deduplicate by this key.

## 9. Exact source instrumentation points

The following are **proposed insertion points**, intentionally after correlation IDs exist and before/after relevant side effects. Implementation should provide one non-throwing local `ObservabilityRecorder` façade with no-op behavior when disabled; telemetry errors must never alter agent behavior.

| Source location | Existing behavior observed | Instrumentation |
|---|---|---|
| `agent/turn_context.py::build_turn_context` lines 217–221 | Creates `effective_task_id`, `turn_id`, sets `_current_turn_id` | Create/reuse `run_id`; emit `turn.started` with prompt/tool/skill/plugin/gate snapshot hashes after tool list/prompt are final. Preserve current prompt cache. |
| `agent/turn_context.py` lines 524–575 | `pre_llm_call` plugin fan-out and context injection | Emit bounded `plugin.hook` children + aggregate gate/decision; record hook status/return shape only, no injected text. |
| `model_tools.py::get_tool_definitions` / `_compute_tool_definitions` lines 279–567 | Resolves enabled/disabled toolsets, availability, schema sanitization, progressive disclosure | Emit deduplicated `catalog.observed`; emit `capability.decision` after final assembly with selected/excluded/deferred counts, `toolset_hash`, registry generation. Do not call recorder on memo cache hit unless its snapshot has not already been observed for the run. |
| `tools/registry.py::register` lines 365–457 | Registers tool metadata and increments generation | Register catalog revision in memory; do not emit per-import event until a run/session context exists. Capture owner plugin through explicit registration provenance, not handler name only. |
| `tools/registry.py::get_definitions` lines 530–577 | `check_fn` filters availability | Feed availability state/reason to capability decision; avoid per-tool repeated events by snapshot dedupe. |
| `hermes_cli/plugins.py::PluginManager` manifest/load/register and `invoke_hook` | Owns plugin identity, registration and hook dispatch | Catalog plugin observation at successful manifest/load; hook events around callback invocation. Existing `VALID_HOOKS` is the hook catalog input. |
| `tools/skills_tool.py::skill_view` lines 961–1629 and `_skill_view_with_bump` lines 1728–1750 | Resolves qualified/local skills, collision/readiness, returns content, bumps sidecar usage | Emit `skill.load` on every terminal path (success/failure), using resolved canonical skill ID/revision; at success link main/supporting artifact hash. Do not emit content/path. |
| `agent/tool_executor.py::execute_tool_calls_sequential` lines 1060–1246 and concurrent lines 361–524 | Parses calls, unwraps tool-search bridge, applies middleware, plugin pre-block and guardrail | Allocate `inv:tool` after parse/unwrap, emit invocation and `gate.decision` for scope/plugin/guardrail; set invocation ID in context for approval hooks. Malformed args gets invocation+blocked outcome. |
| `agent/tool_executor.py::_run_tool` concurrent lines 573–643 and sequential common post-result path | Dispatches and captures duration/error status | Emit exactly one `tool.outcome` in `finally`, preserving concurrent causal links. Include cancelled/timeout/blocked as outcomes. |
| `model_tools.py` existing `_emit_post_tool_call_hook` path / `agent/tool_executor.py::_emit_terminal_post_tool_call` | Existing post-tool lifecycle flow | Attach outcome event ID to post-hook correlation and emit `plugin.hook` result shape; never serialize tool result into telemetry. |
| `tools/approval.py::set_current_observability_context` lines 181–199 and `_fire_approval_hook` lines 96–120 | Carries turn/tool call IDs to approval hooks | Extend context with **Hermes invocation ID**, emit `gate:approval:dangerous-command` decisions for requested/once/session/always/deny/timeout/smart outcomes with redacted command classification only. |
| `agent/conversation_loop.py` lines 5492–5547 | `pre_verify` may continue model/tool loop | Emit `gate:verify:pre-verify` decision and `verification.outcome` (`continue` versus allowed final) with changed-path **count/class**, not paths. |
| `agent/conversation_loop.py` final success/error/interrupt exits and `agent/turn_finalizer.py` | Ends turn and invokes output/session hooks | Emit `turn.outcome` and once-per-run `run.outcome`; evaluate `skill.use-assessment` from event links only. |
| `agent/chat_completion_helpers.py::try_activate_fallback` lines 1408+ and `agent/agent_runtime_helpers.py::switch_model` | Evaluates fallback entries and switches model | Emit `model.decision` for each candidate skip/select and explicit user switch. Use classified `FailoverReason` / HTTP class, not raw exception/provider body. |
| `tools/delegate_tool.py` and async delegation persistence | Creates child work/delegation state | Emit `delegation.started/outcome`, with parent invocation/run and child session/run links. |
| `hermes_state.py` migration layer | Current session/message DB persists raw transcripts/tool-call blobs | Add new isolated observability tables or append-only local JSONL sink; **do not** add raw telemetry fields to `messages` or its FTS triggers. |

### Suggested local storage tables

```sql
CREATE TABLE observability_events (
  event_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  occurred_at REAL NOT NULL,
  session_id TEXT,
  turn_id TEXT,
  run_id TEXT,
  event_type TEXT NOT NULL,
  causation_id TEXT,
  payload_json TEXT NOT NULL,
  backfill_key TEXT UNIQUE,
  expires_at REAL
);
CREATE INDEX idx_obsv_session_turn ON observability_events(session_id, turn_id, occurred_at);
CREATE INDEX idx_obsv_type_time ON observability_events(event_type, occurred_at);
CREATE TABLE observability_catalog (
  catalog_id TEXT NOT NULL,
  catalog_revision TEXT NOT NULL,
  kind TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  PRIMARY KEY(catalog_id, catalog_revision)
);
```

`payload_json` is validated against the schema/redactor before insert. SQLite WAL batching uses a bounded in-process queue; when full, increment a local drop counter and emit one later `telemetry.health` event, never block tool execution.

## 10. Rollout and acceptance criteria

1. **Phase 0 — local disabled/no-op:** ship recorder, schema validation, redactor tests, catalog ID contract tests. No outbound path.
2. **Phase 1 — opt-in local ledger:** tool/skill/plugin/gate/model events for new runs; UI/CLI read-only report. Default `observability.enabled: false` unless product explicitly chooses local-only default after privacy review.
3. **Phase 2 — deterministic correct-use reports:** tool reliability and skill `supported/insufficient_evidence`; no quality score based on hidden reasoning.
4. **Phase 3 — optional backfill:** explicit `hermes observability backfill --local-only`; dry-run default; manifest and counts report.
5. **Phase 4 — export (separate proposal):** generic opt-in config/setup toggle, DPA/privacy review, export-specific redaction tests, and endpoint allowlist.

Acceptance tests:

- Same core tool gets stable `tool:core:<name>` across sessions; two calls get different invocation IDs.
- A `tool_call` bridge invocation records original and underlying IDs, and only the underlying tool is counted as invoked.
- Every attempted tool call has exactly one terminal outcome, including malformed/block/cancel/timeout paths; concurrent ordering remains causally correct.
- Candidate/selected tool(s), concise reason codes, and all five snapshot hashes appear without raw prompt/message/arguments/results.
- A plugin hook/approval/pre-verify decision links to turn + invocation and retains no raw callback payload/command.
- Skill load tracks resolved provenance/revision and a skill-use assessment reports `insufficient_evidence` when no deterministic follow-up exists.
- Existing prompt byte stability and toolset behavior are unchanged when observability is disabled and when recorder writes fail.
- Redaction/property tests prove no deny-listed keys or known test secret values reach `observability_events`; event cardinality remains within budget under synthetic MCP/plugin floods.
