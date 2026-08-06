"""Regression tests for interim-commentary suppression (opus-5 narration spam).

Context: after the opus-5 upgrade (2026-08-05) the share of tool-call turns that
also emitted a short procedural narration jumped from ~20% to 45-56%, flooding
Slack with "확인하겠습니다" / "이제 검증하겠습니다" play-by-play. The filter drops
those interim segments for opus-5 only; every other model keeps legacy behavior.
"""

import pytest

from gateway.commentary_filters import is_procedural_narration


OPUS5 = "claude-opus-5"


# --- narration that SHOULD be suppressed on opus-5 ------------------------
@pytest.mark.parametrize(
    "text",
    [
        "확인하겠습니다.",
        "이제 검증하겠습니다.",
        "먼저 세션 DB를 확인하겠습니다.",
        "수정하겠습니다.",
        "진행하겠습니다.",
        "찾아볼게요.",
        "한번 볼게요",
        "바로 고칠게요.",
        "Let me check the config.",
        "I'll verify that now.",
        "Now let's look at the gateway.",
    ],
)
def test_procedural_narration_suppressed_on_opus5(text):
    assert is_procedural_narration(text, OPUS5) is True


# --- substance that MUST always survive -----------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "183라인이 범인이네요! day shift 키워드 오매칭입니다.",
        "형님 이거 롤백할까요?",
        "8/5부터 나레이션 비율이 24%에서 56%로 뛰었어요.",
        "테스트 228개 전부 통과했습니다.",
        "안 되네요. 모델명이 스코프에 없어서 빈 문자열로 들어갑니다.",
    ],
)
def test_substance_never_suppressed(text):
    assert is_procedural_narration(text, OPUS5) is False


# --- model gating ---------------------------------------------------------
@pytest.mark.parametrize("model", ["claude-opus-4-6", "gpt-5.6-luna", "", None])
def test_other_models_keep_legacy_behavior(model):
    """Only opus-5 is filtered; everything else (incl. the proxy path that has
    no resolved model name) passes narration through untouched."""
    assert is_procedural_narration("확인하겠습니다.", model) is False


def test_opus5_variants_all_match():
    for m in ["claude-opus-5", "anthropic/claude-opus-5", "claude-opus-5-20260801"]:
        assert is_procedural_narration("확인하겠습니다.", m) is True


def test_empty_and_whitespace_are_not_narration():
    for t in ["", "   ", "\n"]:
        assert is_procedural_narration(t, OPUS5) is False
