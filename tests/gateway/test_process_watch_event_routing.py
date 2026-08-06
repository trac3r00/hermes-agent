from queue import Queue

from gateway.run import _drain_gateway_watch_events


def test_watch_disabled_event_is_not_reinjected_as_an_agent_turn() -> None:
    completion_queue: Queue[dict] = Queue()
    completion_queue.put(
        {
            "type": "watch_disabled",
            "session_id": "session-1",
            "message": "Process exited",
        }
    )

    assert _drain_gateway_watch_events(completion_queue) == []


def test_explicit_watch_match_remains_available_for_notification() -> None:
    completion_queue: Queue[dict] = Queue()
    event = {"type": "watch_match", "session_id": "session-1"}
    completion_queue.put(event)

    assert _drain_gateway_watch_events(completion_queue) == [event]
