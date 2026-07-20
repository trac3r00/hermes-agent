from datetime import datetime, timezone

from gateway.session import SessionEntry


def test_session_entry_loads_offset_timestamps_as_naive_local():
    aware = datetime(2026, 7, 5, 20, 12, 22, tzinfo=timezone.utc)
    restored = SessionEntry.from_dict(
        {
            "session_key": "agent:main:telegram:dm:1",
            "session_id": "sid",
            "created_at": aware.isoformat(),
            "updated_at": aware.isoformat(),
            "chat_type": "dm",
            "resume_pending": True,
            "resume_reason": "restart_interrupted",
            "last_resume_marked_at": aware.isoformat(),
        }
    )

    assert restored.created_at.tzinfo is None
    assert restored.updated_at.tzinfo is None
    assert restored.last_resume_marked_at is not None
    assert restored.last_resume_marked_at.tzinfo is None
    age_seconds = (datetime.now() - restored.last_resume_marked_at).total_seconds()
    assert isinstance(age_seconds, float)
