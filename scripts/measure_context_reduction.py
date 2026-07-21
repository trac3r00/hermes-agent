#!/usr/bin/env python3
"""Deterministically report prompt and tool-history character reductions.

Run from the repository root.  The skill comparison uses a fixed synthetic
catalog, so changes in a developer's installed skills never affect the result.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
from tools.budget_config import BudgetConfig
from tools.tool_result_storage import maybe_persist_tool_result


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        home = Path(temp)
        import os
        previous_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            for category, name, description in (
                ("automation", "alpha", "A" * 2_000),
                ("automation", "beta", "B" * 2_000),
                ("coding", "gamma", "C" * 2_000),
            ):
                skill = home / "skills" / category / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {description}\n---\n",
                    encoding="utf-8",
                )
            clear_skills_system_prompt_cache(clear_snapshot=True)
            compact_prompt = build_skills_system_prompt()
        finally:
            if previous_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous_home
            clear_skills_system_prompt_cache(clear_snapshot=True)

    legacy_catalog_chars = sum(len(description) for description in ("A" * 2_000, "B" * 2_000, "C" * 2_000))
    result = "x" * 60_000
    with tempfile.TemporaryDirectory() as output_dir:
        import tools.tool_result_storage as storage
        old_dir = storage.STORAGE_DIR
        storage.STORAGE_DIR = output_dir
        try:
            compact_result = maybe_persist_tool_result(
                result, "terminal", "measure", env=None,
                config=BudgetConfig(default_result_size=30_000),
            )
        finally:
            storage.STORAGE_DIR = old_dir

    from model_tools import get_tool_definitions
    tool_schema_chars = len(json.dumps(get_tool_definitions(), sort_keys=True))
    print(f"tool_schema_chars_before={tool_schema_chars}")
    print(f"tool_schema_chars_after={tool_schema_chars}")  # no schema expansion
    print(f"skill_description_chars_before={legacy_catalog_chars}")
    print(f"skill_prompt_chars_after={len(compact_prompt)}")
    print(f"tool_history_chars_before={len(result)}")
    print(f"tool_history_chars_after={len(compact_result)}")


if __name__ == "__main__":
    main()
