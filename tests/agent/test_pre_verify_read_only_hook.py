"""Regression coverage for read-only pre-finalization verification hooks."""

from __future__ import annotations

import inspect

from agent import conversation_loop


def test_pre_verify_is_not_limited_to_file_editing_turns() -> None:
    """Live-fact recovery needs the stop hook even when no file was edited."""
    source = inspect.getsource(conversation_loop.run_conversation)

    assert 'if has_hook("pre_verify") and _attempt < max_verify_nudges()' in source
    assert 'if _edited and has_hook("pre_verify")' not in source
    assert "changed_paths=_edited" in source
