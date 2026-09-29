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


def test_prepare_failure_drops_summary_that_echoes_traceback():
    job = {"id": "j3", "name": "quality-failure", "no_agent": True}
    raw = "Script exited with code 1\nstderr:\nTraceback: RAW_FAILURE_SENTINEL"
    with patch.object(sched, "_cron_delivery_policy", return_value=("ko", True)), patch(
        "agent.oneshot.run_oneshot", return_value="실패\nTraceback: RAW_FAILURE_SENTINEL"
    ):
        out = sched._prepare_cron_delivery_content(job, raw, success=False)
    assert "RAW_FAILURE_SENTINEL" not in out
    assert "Traceback" not in out
    assert "quality-failure 실행에 실패" in out


def test_prepare_failure_delivers_korean_summary():
    job = {"id": "j3b", "name": "quality-failure", "no_agent": True}
    with patch.object(sched, "_cron_delivery_policy", return_value=("ko", True)), patch(
        "agent.oneshot.run_oneshot", return_value="품질 검사가 실패했습니다."
    ):
        out = sched._prepare_cron_delivery_content(job, "boom", success=False)
    assert out == "품질 검사가 실패했습니다."


def test_summarizer_input_is_redacted_even_when_global_redaction_off(monkeypatch):
    import agent.redact as redact

    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    key = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    seen = {}

    def fake_oneshot(**kw):
        seen["input"] = kw["user_input"]
        return "요약"

    with patch("agent.oneshot.run_oneshot", side_effect=fake_oneshot):
        sched._summarize_cron_text_for_delivery(
            {"id": "j10"}, f"OPENAI_API_KEY={key} failed", failure=True
        )
    assert key not in seen["input"]


def test_prepare_low_value_no_agent_is_silent():
    job = {"id": "j4", "name": "watchdog", "no_agent": True}
    with patch.object(sched, "_cron_delivery_policy", return_value=("ko", True)):
        assert sched._prepare_cron_delivery_content(job, "All clear - no action needed", success=True) == ""


def test_prepare_failure_uses_korean_fallback_when_llm_blank():
    job = {"id": "j6", "name": "quality-fail-closed", "no_agent": True}
    with patch("agent.oneshot.run_oneshot", return_value=""):
        out = sched._summarize_cron_text_for_delivery(job, "stdout:\nRAW_FAILURE_SENTINEL", failure=True)
    assert "RAW_FAILURE_SENTINEL" not in out
    assert "실패" in out


def test_english_fallback_unchanged_when_compact_off():
    job = {"id": "j5", "name": "quality-failure"}
    with patch.object(sched, "_cron_delivery_policy", return_value=("", False)):
        out = sched._summarize_cron_failure_for_delivery(job, "429 rate limit exceeded")
    assert "provider rate limit" in out


def test_compact_off_success_is_verbatim_even_when_low_value():
    job = {"id": "j7", "name": "watchdog", "no_agent": True}
    text = "All clear - no action needed\n"
    with patch.object(sched, "_cron_delivery_policy", return_value=("", False)):
        assert sched._prepare_cron_delivery_content(job, text, success=True) == text


def test_compact_off_failure_without_error_keeps_english_one_liner():
    job = {"id": "j8", "name": "quality-failure"}
    with patch.object(sched, "_cron_delivery_policy", return_value=("", False)):
        out = sched._prepare_cron_delivery_content(job, None, success=False)
    assert out == "⚠️ Cron 'quality-failure' failed: unknown error"


def test_compact_on_failure_without_error_skips_llm():
    job = {"id": "j9", "name": "quality-failure", "no_agent": True}
    with patch.object(sched, "_cron_delivery_policy", return_value=("ko", True)), patch(
        "agent.oneshot.run_oneshot"
    ) as oneshot:
        out = sched._prepare_cron_delivery_content(job, None, success=False)
    oneshot.assert_not_called()
    assert "실패" in out


def test_delivery_policy_defaults_off(monkeypatch):
    monkeypatch.setattr(sched, "load_config", lambda: {"cron": {}})
    assert sched._cron_delivery_policy() == ("", False)
    monkeypatch.setattr(sched, "load_config", lambda: {"cron": {"delivery_language": "ko"}})
    assert sched._cron_delivery_policy() == ("ko", True)
