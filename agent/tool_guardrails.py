"""Pure tool-call loop guardrail primitives.

The controller in this module is intentionally side-effect free: it tracks
per-turn tool-call observations and returns decisions. Runtime code owns whether
those decisions become warning guidance, synthetic tool results, or controlled
turn halts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import file_mutation_result_landed


IDEMPOTENT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "web_search",
        "web_extract",
        "session_search",
        "browser_snapshot",
        "browser_console",
        "browser_get_images",
        "mcp_filesystem_read_file",
        "mcp_filesystem_read_text_file",
        "mcp_filesystem_read_multiple_files",
        "mcp_filesystem_list_directory",
        "mcp_filesystem_list_directory_with_sizes",
        "mcp_filesystem_directory_tree",
        "mcp_filesystem_get_file_info",
        "mcp_filesystem_search_files",
    }
)

MUTATING_TOOL_NAMES = frozenset(
    {
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "todo",
        "memory",
        "skill_manage",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_navigate",
        "send_message",
        "cronjob",
        "delegate_task",
        "process",
    }
)


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings are enabled by default and never prevent tool execution. Repeated
    failures are always steered back into the model/tool loop; the legacy
    hard-stop settings remain readable for configuration compatibility but do
    not terminate a user turn.
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 2
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ToolCallGuardrailConfig":
        """Build config from the `tool_loop_guardrails` config.yaml section."""
        if not isinstance(data, Mapping):
            return cls()

        warn_after = data.get("warn_after")
        if not isinstance(warn_after, Mapping):
            warn_after = {}
        hard_stop_after = data.get("hard_stop_after")
        if not isinstance(hard_stop_after, Mapping):
            hard_stop_after = {}

        defaults = cls()
        return cls(
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            hard_stop_enabled=_as_bool(data.get("hard_stop_enabled"), defaults.hard_stop_enabled),
            exact_failure_warn_after=_positive_int(
                warn_after.get("exact_failure", data.get("exact_failure_warn_after")),
                defaults.exact_failure_warn_after,
            ),
            same_tool_failure_warn_after=_positive_int(
                warn_after.get("same_tool_failure", data.get("same_tool_failure_warn_after")),
                defaults.same_tool_failure_warn_after,
            ),
            no_progress_warn_after=_positive_int(
                warn_after.get("idempotent_no_progress", data.get("no_progress_warn_after")),
                defaults.no_progress_warn_after,
            ),
            exact_failure_block_after=_positive_int(
                hard_stop_after.get("exact_failure", data.get("exact_failure_block_after")),
                defaults.exact_failure_block_after,
            ),
            same_tool_failure_halt_after=_positive_int(
                hard_stop_after.get("same_tool_failure", data.get("same_tool_failure_halt_after")),
                defaults.same_tool_failure_halt_after,
            ),
            no_progress_block_after=_positive_int(
                hard_stop_after.get("idempotent_no_progress", data.get("no_progress_block_after")),
                defaults.no_progress_block_after,
            ),
        )


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        canonical = canonical_tool_args(args or {})
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))

    def to_metadata(self) -> dict[str, str]:
        """Return public metadata without raw argument values."""
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the tool-call guardrail controller."""

    action: str = "allow"  # allow | warn | steer
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        """Whether the current call may reach the real tool implementation."""
        return self.action != "steer"

    @property
    def should_inject_steering(self) -> bool:
        return self.action == "steer"

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        if self.signature is not None:
            data["signature"] = self.signature.to_metadata()
        return data


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Safety-fallback classifier used only when callers don't pass ``failed``.

    Mirrors ``agent.display._detect_tool_failure`` exactly so the guardrail
    never disagrees with the CLI's user-visible ``[error]`` tag. Production
    callers in ``run_agent.py`` always pass an explicit ``failed=`` derived
    from ``_detect_tool_failure``; this function exists so standalone callers
    (tests, tooling) still get consistent behavior.
    """
    if result is None:
        return False, ""
    if file_mutation_result_landed(tool_name, result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
        return False, ""

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            if data.get("success") is False and "exceed the limit" in data.get("error", ""):
                return True, " [full]"

    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


class ToolCallGuardrailController:
    """Per-turn controller for repeated failed/non-progressing tool calls."""

    def __init__(self, config: ToolCallGuardrailConfig | None = None):
        self.config = config or ToolCallGuardrailConfig()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._steering_injected: set[ToolCallSignature] = set()  # Track which signatures got steering

    def prior_failure_count(
        self, tool_name: str, args: Mapping[str, Any] | None
    ) -> int:
        """Return failures for this exact call before a new execution begins."""
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        return self._exact_failure_counts.get(signature, 0)

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            self._steering_injected.add(signature)
            return ToolGuardrailDecision(
                action="steer",
                code="repeated_exact_failure_steering",
                message="",
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )

        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    self._steering_injected.add(signature)
                    return ToolGuardrailDecision(
                        action="steer",
                        code="idempotent_no_progress_steering",
                        message="",
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool | None = None,
        prior_failures: int | None = None,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)

        if failed:
            failure_count = (
                prior_failures
                if prior_failures is not None
                else self._exact_failure_counts.get(signature, 0)
            )
            exact_count = failure_count + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} has failed {exact_count} times with identical arguments. "
                        "This looks like a loop; inspect the error and change strategy "
                        "instead of retrying it unchanged."
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                )

            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times. "
                    "Use the result already provided or change the query instead of "
                    "repeating it unchanged."
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a synthetic role=tool content string for a rejected tool call."""
    return json.dumps(
        {
            "error": "tool_guardrail_rejected",
            "code": decision.code,
            "message": (
                "This repeated tool call was not executed. Inspect the prior error "
                "and choose a materially different valid action."
            ),
            "tool_name": decision.tool_name,
        },
        ensure_ascii=False,
    )


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content."""
    if decision.action != "warn" or not decision.message:
        return result
    label = "Tool loop warning"
    suffix = (
        f"\n\n[{label}: "
        f"{decision.code}; count={decision.count}; {decision.message}]"
    )
    return (result or "") + suffix


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """Return private guidance that redirects a repeated failed call."""
    del count
    common = (
        f"`{tool_name}` has failed repeatedly. Do not call it again until you can "
        "correct the specific error. Inspect the latest error/output, then choose a "
        "different valid tool call or report the concrete blocker. "
    )
    if tool_name == "terminal":
        return common + (
            "For terminal failures, use a small diagnostic such as `pwd && ls -la` only "
            "when it addresses the error; otherwise use a complete read_file/write_file/"
            "patch call with every required field. If the blocker is external, report it "
            "instead of repeating the failed command."
        )
    return common + (
        "Use complete required arguments, a narrower query/path, an absolute path when "
        "relevant, or a different tool that can make progress. If the blocker is external, "
        "report it instead of repeating the same failing path."
    )


def _tool_failure_steering(tool_name: str) -> tuple[str, str]:
    """Return tool-specific diagnostic and alternative steps for a repeated call."""
    if tool_name == "terminal":
        return (
            "Run `pwd && ls -la` in the same tool to verify cwd and permissions.",
            "- Use absolute paths instead of relative paths.\n"
            "- Try a simpler command.\n"
            "- Use a different working directory.\n"
            "- Use read_file, write_file, or patch when shell access is not needed.",
        )
    if tool_name in {"read_file", "write_file", "patch"}:
        return (
            "Verify the absolute path and that its parent directory exists and is accessible.",
            "- Use an absolute path instead of a relative path.\n"
            "- Read the parent directory before writing or patching.\n"
            "- Try a smaller read or a simpler patch.\n"
            "- Use terminal to inspect permissions or a different file tool.",
        )
    if tool_name in {"web_search", "web_extract"}:
        return (
            "Check network connectivity and inspect the service error before retrying.",
            "- Try a narrower query or a different URL.\n"
            "- Use a simpler query with fewer filters.\n"
            "- Use web_extract for a known URL or web_search to find one.\n"
            "- Report a service outage after the diagnostic attempt.",
        )
    if tool_name in {"skill_view", "skill_manage"}:
        return (
            "Check the skill name spelling and list available skills first.",
            "- Use the exact listed skill name.\n"
            "- Try skill_view before skill_manage.\n"
            "- Request a smaller, valid skill operation.\n"
            "- Use another tool if the task does not require a skill.",
        )
    if tool_name == "delegate_task":
        return (
            "Verify the task description and check subagent availability.",
            "- Make the task description shorter and more specific.\n"
            "- Split the work into a smaller task.\n"
            "- Use an available subagent or perform the task directly.\n"
            "- Report unavailable delegation infrastructure after the diagnostic attempt.",
        )
    if tool_name == "cronjob":
        return (
            "Verify the script path exists, check cron syntax, and test the script manually first.",
            "- Use an absolute script path.\n"
            "- Try a simpler cron schedule.\n"
            "- Run the script manually with terminal before scheduling it.\n"
            "- Use a different scheduler or report unavailable cron infrastructure.",
        )
    return (
        "Inspect the latest error/output and verify the tool's required arguments.",
        "- Try different arguments.\n"
        "- Use a narrower query or path.\n"
        "- Use an absolute path when relevant.\n"
        "- Use a different tool that can make progress.",
    )


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: str | None) -> str:
    parsed = safe_json_loads(result or "")
    if parsed is not None:
        try:
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            canonical = str(parsed)
    else:
        canonical = result or ""
    return _sha256(canonical)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_system_reminder(tool_name: str, count: int, action: str) -> dict[str, str]:
    """Build a private, actionable recovery directive for a blocked call."""
    del count, action
    diagnostic, alternatives = _tool_failure_steering(tool_name)
    message = (
        f"TOOL RECOVERY REQUIRED: the repeated `{tool_name}` call was blocked before "
        "execution because it would repeat the same failing route. Continue the task; "
        "do not retry that call unchanged and do not fabricate a result.\n\n"
        f"1. {diagnostic}\n"
        f"2. Take ONE materially different valid action:\n{alternatives}\n\n"
        "Use the new tool result to continue. If an external dependency truly blocks "
        "progress, perform one focused diagnostic, then report the concrete blocker."
    )

    return {
        "role": "user",
        "content": f"<system-reminder>\n{message}\n</system-reminder>",
    }
