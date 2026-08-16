from unittest.mock import patch

import cron.scheduler as sched


def test_prepare_success_passes_compact_korean_through():
    job = {"id": "j1", "name": "infra-monitor", "no_agent": True}
    text = "⚠️ pfSense가 응답하지 않습니다.\n\n집 네트워크 감시기입니다."
    with patch.object(sched, "_cron_delivery_policy", return_value=("ko", True)):
        assert sched._prepare_cron_delivery_content(job, text, success=True) == text


def test_prepare_success_compacts_raw_english():
    job = {"id": "j2", "name": "x-account-watch", "no_agent": True}
    raw = "X watch: Anthropic Claude posted something long and promotional about a new model."
    with patch.object(sched, "_cron_delivery_policy", return_value=("ko", True)), patch.object(
        sched,
        "_summarize_cron_text_for_delivery",
        return_value="[X watch] Anthropic Claude\n• 새 모델 발표",
    ) as summarize:
        out = sched._prepare_cron_delivery_content(job, raw, success=True)
    summarize.assert_called_once()
    assert out.startswith("[X watch]")
    assert "promotional" not in out


def test_prepare_failure_never_returns_traceback():
    job = {"id": "j3", "name": "quality-failure", "no_agent": True}
    raw = "Script exited with code 1\nstderr:\nTraceback: RAW_FAILURE_SENTINEL"
    with patch.object(sched, "_cron_delivery_policy", return_value=("ko", True)), patch.object(
        sched,
        "_summarize_cron_text_for_delivery",
        return_value="⚠️ quality-failure 실행에 실패했습니다.\n\n주기적으로 돌아가는 감시기입니다.",
    ):
        out = sched._prepare_cron_delivery_content(job, raw, success=False)
    assert "RAW_FAILURE_SENTINEL" not in out
    assert "Traceback" not in out
    assert "실패" in out


def test_prepare_low_value_no_agent_is_silent():
    job = {"id": "j4", "name": "watchdog", "no_agent": True}
    with patch.object(sched, "_cron_delivery_policy", return_value=("ko", True)):
        assert sched._prepare_cron_delivery_content(job, "All clear - no action needed", success=True) == ""


def test_english_fallback_unchanged_when_compact_off():
    job = {"id": "j5", "name": "quality-failure"}
    with patch.object(sched, "_cron_delivery_policy", return_value=("", False)):
        out = sched._summarize_cron_failure_for_delivery(job, "429 rate limit exceeded")
    assert "provider rate limit" in out
