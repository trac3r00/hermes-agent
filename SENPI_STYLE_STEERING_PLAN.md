# Senpi-Style Tool Guardrail Steering — Implementation Plan

## Problem
Current implementation provides **better text** but still **halts the turn**:
```
⚠️ Tool guardrail halted cronjob: repeated_exact_failure_block
cronjob failed 5 times... [steering guidance]
```

User still has to manually continue ("크론잡 삭제해 ㄱㄱ") because the turn ended.

## Senpi's Approach (The Right Way)

### 1. **No Turn Halt** — Inject Steering Mid-Turn
```typescript
// senpi/dist/core/extensions/builtin/loop-guard/index.js
pi.on("tool_execution_start", (event) => {
    tracker.record(event.toolName, event.args);
    const detection = detectLoop(tracker.records, gate);
    if (detection === undefined) return;
    
    // Inject steering WITHOUT halting
    pi.sendMessage({
        customType: LOOP_GUARD_NOTICE_CUSTOM_TYPE,
        content: buildLoopGuardReminder(detection),
        display: true,
        details: detection,
    }, { 
        triggerTurn: false,      // Don't start new turn
        deliverAs: "steer"       // Inject into current turn
    });
});
```

### 2. **System Reminder Format**
```xml
<system-reminder>
LOOP GUARD - IDENTICAL TOOL CALLS: you called `cronjob` 5 times in a row with the EXACT same arguments.

Snap out of it:
- if nothing is actually changing, stop calling this tool, state what is blocking you, and try a different tool or ask the user.
- try an absolute path, simpler schedule, test manually with terminal first
- use a different scheduler or report unavailable infrastructure

Do not call `cronjob` again with identical arguments.
</system-reminder>
```

### 3. **Message Injection Flow**
- Tool execution starts
- Loop detector triggers
- **Before tool executes**, inject `<system-reminder>` into conversation
- Tool execution continues (or blocks if hard_stop_enabled)
- Agent sees reminder in next API call
- Agent adjusts strategy **in the same turn**

## Implementation for Bob/Hermes

### Option A: Inject System Message (Simplest)

Modify `agent/tool_guardrails.py` `before_call()`:

```python
def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
    signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
    
    exact_count = self._exact_failure_counts.get(signature, 0)
    if exact_count >= self.config.exact_failure_block_after:
        if self.config.hard_stop_enabled:
            # OLD: Block and halt turn
            # NEW: Inject steering and ALLOW execution (let agent recover)
            return ToolGuardrailDecision(
                action="warn_strong",  # NEW: warning that agent MUST heed
                code="repeated_exact_failure_steering",
                message=_build_system_reminder(tool_name, exact_count),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )
        else:
            # Soft warning mode: already warns, no change needed
            pass
    
    return ToolGuardrailDecision(tool_name=tool_name, signature=signature)
```

### Option B: Message Injection Hook (More Invasive)

Add a message injection hook in `run_agent.py` that:
1. Detects guardrail trigger
2. Injects `<system-reminder>` into messages list
3. Continues tool execution (or blocks tool but continues turn)
4. Next API call includes the reminder

```python
# In _execute_tool_calls_sequential or _before_tool_execution
def _inject_guardrail_steering(self, decision: ToolGuardrailDecision, messages: list):
    """Inject steering reminder into conversation without halting turn."""
    steering_message = {
        "role": "user",  # Or "system" if model supports mid-turn system messages
        "content": _build_system_reminder(decision.tool_name, decision.count)
    }
    messages.append(steering_message)
    # Do NOT set _tool_guardrail_halt_decision
    # Turn continues after this tool result
```

### NEW: `_build_system_reminder()` Function

```python
def _build_system_reminder(tool_name: str, count: int) -> str:
    diagnostic, alternatives = _tool_failure_steering(tool_name)
    return (
        "<system-reminder>\n"
        f"LOOP GUARD - REPEATED FAILURE: you called `{tool_name}` {count} times "
        "with identical arguments and it failed every time.\n\n"
        f"Snap out of it:\n"
        f"- {diagnostic}\n"
        f"- Then: {alternatives.replace('- ', '', 1).replace(chr(10) + '- ', chr(10)  + '  • ')}\n"
        f"- If the blocker is external, report it instead of retrying\n\n"
        f"Do not call `{tool_name}` again with identical arguments.\n"
        "</system-reminder>"
    )
```

## Decision Tree

### Scenario 1: Hard Stop Enabled + Exact Failure Threshold Hit
**Current**: Block tool, halt turn, return steering as final response  
**New (Senpi Style)**: Inject `<system-reminder>`, allow ONE more attempt with different args, or let agent switch tools

### Scenario 2: Hard Stop Disabled (Warnings Only)
**Current**: Append warning to tool result, continue  
**New**: Same (already works)

### Scenario 3: Agent Ignores Steering
**Current**: N/A (turn already halted)  
**New**: If agent calls same tool with same args AGAIN after reminder → **then** block and halt

## Implementation Steps

1. **Add `_build_system_reminder()` to `agent/tool_guardrails.py`**
   - Takes tool_name, count, returns XML-wrapped steering

2. **Modify `before_call()` to inject steering instead of blocking**
   - When hard_stop + threshold hit: inject reminder, allow execution
   - Track "reminder injected" state

3. **Add post-injection enforcement**
   - If tool called AGAIN with same args after reminder → THEN block and halt
   - This gives agent one chance to course-correct

4. **Update `_toolguard_controlled_halt_response()` for fallback**
   - Only called if agent ignores reminder AND retries same call
   - Message: "You were warned about this exact call; halting to prevent runaway loop"

5. **Test**
   - Trigger repeated cronjob failure
   - Verify steering injected mid-turn
   - Verify agent switches strategy without manual re-prompt

## Files to Modify

1. `agent/tool_guardrails.py`:
   - Add `_build_system_reminder()`
   - Modify `before_call()` logic
   - Add reminder-injection state tracking

2. `run_agent.py`:
   - Update tool execution to inject steering messages
   - Handle "warn_strong" action differently from "block"

3. `tests/agent/test_tool_guardrails.py`:
   - Test steering injection
   - Test agent recovery (mock multi-turn scenario)

## Success Criteria

✅ When `cronjob` fails 5 times with identical args:
- Agent receives `<system-reminder>` mid-turn
- Agent switches to different tool/args WITHOUT manual re-prompt
- Turn does NOT halt (unless agent ignores reminder)

✅ If agent ignores reminder and retries same call:
- THEN block and halt (safety preserved)

✅ Warning-only mode unchanged (soft nudges still work)

## Open Questions

1. **Message role**: `"user"` or `"system"`? (Some models don't support mid-turn system messages)
2. **Display in UI**: Should reminder be visible in chat or hidden like tool results?
3. **Interaction with context compression**: Will reminder survive compression?

## Next Actions

1. Prototype `_build_system_reminder()` function
2. Test message injection point (before/after tool execution)
3. Implement reminder-then-block two-strike logic
4. Manual test with live cronjob scenario
