"""Interim-commentary suppression for tool-narrating models.

Some models (notably ``claude-opus-5``) emit a short procedural sentence before
almost every tool call — "먼저 확인하겠습니다.", "이제 검증하겠습니다.", "Let me
check the config." — which the gateway delivers as a separate interim chat
bubble.  Individually harmless; at 50%+ of tool turns it reads as the assistant
repeating itself and drowns the actual answer.

This module decides whether a *interim* commentary segment is pure procedural
narration and can be dropped.  It never runs against a final response: only the
``Commentary`` events emitted between tool iterations reach it.

Scope is deliberately narrow:

* Only applies to models listed in ``NARRATING_MODEL_PATTERNS`` (opus-5 family).
* Only drops SHORT segments (<= ``_MAX_NARRATION_CHARS``).
* Only drops segments that are entirely procedural — any segment carrying a
  finding, number, path, code, or question is preserved.
"""

from __future__ import annotations

import re
from typing import Any

# Model substrings whose interim commentary is filtered.  Matched
# case-insensitively against the resolved model name.  Keep this list tight:
# opting a model in changes what the user sees mid-turn.
NARRATING_MODEL_PATTERNS: tuple[str, ...] = (
    "opus-5",
    "opus5",
)

# Commentary longer than this is assumed to carry real content and is always
# delivered, even if it opens with a procedural phrase.
_MAX_NARRATION_CHARS = 200

# Korean procedural verb endings: 하겠습니다 / 확인할게요 / 보겠습니다 / 시작합니다 …
_KO_PROCEDURAL_TAIL = re.compile(
    r"(하|되|드리|보|주|가|오|알아보|살펴보|찾아보|확인해?|점검해?|검증해?|수정해?"
    r"|진행해?|시작해?|열어?|읽어?|적용해?|정리해?)"
    r"\s*(겠|을게|ㄹ게|께|지|시)?\s*"
    r"(습니다|입니다|어요|아요|네요|요|다)\s*[.!…]*$",
    re.IGNORECASE,
)

# ``ㄹ``-contracted volitive endings.  Korean composes the ㄹ into the stem
# syllable (보 + ㄹ게 → 볼게), so the decomposed pattern above cannot match
# "찾아볼게요" / "확인할게요" / "읽어드릴게요".  Anchor on an explicit set of
# contracted stems so a bare noun ending in 게 ("이렇게") never matches.
_KO_CONTRACTED_TAIL = re.compile(
    r"(볼|할|드릴|릴|줄|쓸|열|읽을|찾을|만들|돌릴|넣을|뺄|고칠|짤|맞출|올릴|내릴)"
    r"\s*[게께]\s*(요|습니다)?\s*[.!…]*$"
)

# English procedural openers: "Let me …", "I'll …", "Now I will …", "First, …"
_EN_PROCEDURAL = re.compile(
    r"^(ok(ay)?[,.\s]+)?"
    r"(now\s+)?"
    r"(let me|let's|i'll|i will|i'm going to|i am going to|going to|"
    r"first[,\s]|next[,\s]|then[,\s]|starting|checking|verifying|inspecting|"
    r"looking|reading|running|opening)\b",
    re.IGNORECASE,
)

# Signals that a segment carries substance and must never be dropped, even when
# short.  Order matters only for readability.
_SUBSTANCE_MARKERS = (
    "```",      # code block
    "://",      # URL
    "|",        # table row
    "→", "->",  # cause/effect or mapping
)

# A digit sequence of 2+ chars usually means a count, size, version, or metric —
# i.e. a finding, not narration.  Single digits ("1단계") are still narration.
_NUMERIC_FINDING = re.compile(r"\d{2,}")

# Bullet/enumeration prefixes imply a structured report.
_STRUCTURED_LINE = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.MULTILINE)


def model_narrates_tools(model: Any) -> bool:
    """Return True when *model* is known to prepend narration to tool calls."""
    if not isinstance(model, str) or not model:
        return False
    lowered = model.lower()
    return any(pat in lowered for pat in NARRATING_MODEL_PATTERNS)


def _has_substance(text: str) -> bool:
    """Return True when *text* carries content beyond procedural narration."""
    if any(marker in text for marker in _SUBSTANCE_MARKERS):
        return True
    if _NUMERIC_FINDING.search(text):
        return True
    if _STRUCTURED_LINE.search(text):
        return True
    # A question is an interaction, not narration — always deliver.
    if "?" in text or "？" in text:
        return True
    # Multi-sentence segments generally report something before announcing the
    # next step.  Treat 3+ sentences as substantive.
    if len(re.findall(r"[.!?。！？]+", text)) >= 3:
        return True
    return False


def is_procedural_narration(text: Any, model: Any = None) -> bool:
    """Return True when *text* is a droppable interim narration segment.

    ``model`` gates the check: only known tool-narrating models are filtered.
    Anything else — including a missing/unknown model name, as on the proxy
    path where no model is resolved — returns False and preserves existing
    behaviour verbatim.  Fail-open is deliberate: dropping a user-visible
    message on an unidentified model is worse than letting narration through.
    """
    if not model_narrates_tools(model):
        return False
    if not isinstance(text, str):
        return False

    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > _MAX_NARRATION_CHARS:
        return False
    if _has_substance(stripped):
        return False

    # Evaluate the last sentence for Korean, the first for English: Korean puts
    # the verb at the end, English puts the intent at the front.
    if _KO_PROCEDURAL_TAIL.search(stripped):
        return True
    if _KO_CONTRACTED_TAIL.search(stripped):
        return True
    if _EN_PROCEDURAL.match(stripped):
        return True
    return False
