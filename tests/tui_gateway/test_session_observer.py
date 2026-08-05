from __future__ import annotations

import threading

from tui_gateway import server
from tui_gateway.session_observer import SessionObserverHub


class RecordingTransport:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.frames: list[dict] = []
        self.succeeds = succeeds

    def write(self, frame: dict) -> bool:
        self.frames.append(frame)
        return self.succeeds

    def close(self) -> None:
        pass


def _event(session_id: str, event_type: str, payload: dict | None = None) -> dict:
    params = {"session_id": session_id, "type": event_type}
    if payload is not None:
        params["payload"] = payload
    return {"jsonrpc": "2.0", "method": "event", "params": params}


def test_observer_receives_owner_stream_with_canonical_replay(monkeypatch):
    hub = SessionObserverHub()
    monkeypatch.setattr(server, "_session_event_hub", hub)
    owner = RecordingTransport()
    observer = RecordingTransport()
    server._sessions["runtime-1"] = {
        "transport": owner,
        "session_key": "stored-1",
        "running": True,
    }
    try:
        run = hub.begin_run("runtime-1", "desktop")
        hub.subscribe("runtime-1", observer)

        assert server.write_json(
            _event("runtime-1", "message.delta", {"text": "hello"})
        )

        assert owner.frames == observer.frames
        metadata = owner.frames[0]["params"]["run"]
        assert metadata == {
            "run_id": run["run_id"],
            "generation": 1,
            "sequence": 1,
            "source": "desktop",
        }
        replay = hub.subscribe("runtime-1", observer, after_sequence=0)
        assert replay["events"] == owner.frames
    finally:
        server._sessions.pop("runtime-1", None)


def test_owner_is_not_sent_duplicate_when_also_subscribed(monkeypatch):
    hub = SessionObserverHub()
    monkeypatch.setattr(server, "_session_event_hub", hub)
    owner = RecordingTransport()
    server._sessions["runtime-1"] = {
        "transport": owner,
        "session_key": "stored-1",
        "running": True,
    }
    try:
        hub.begin_run("runtime-1", "webui")
        hub.subscribe("runtime-1", owner)
        server.write_json(_event("runtime-1", "reasoning.delta", {"text": "x"}))
        assert len(owner.frames) == 1
    finally:
        server._sessions.pop("runtime-1", None)


def test_replay_is_bounded_and_reports_gap():
    hub = SessionObserverHub(max_events_per_session=2, max_bytes_per_session=1024 * 1024)
    hub.begin_run("runtime-1", "desktop")
    for text in ("one", "two", "three"):
        hub.publish(_event("runtime-1", "message.delta", {"text": text}))

    replay = hub.subscribe("runtime-1", RecordingTransport(), after_sequence=0)

    assert [frame["params"]["payload"]["text"] for frame in replay["events"]] == [
        "two",
        "three",
    ]
    assert replay["run"]["earliest_replay_sequence"] == 2
    assert replay["run"]["latest_sequence"] == 3


def test_recent_runtime_exposes_short_completed_run_for_bounded_window(monkeypatch):
    from tui_gateway import session_observer

    now = 1000.0
    monkeypatch.setattr(session_observer.time, "time", lambda: now)
    hub = SessionObserverHub()
    run = hub.begin_run("runtime-1", "desktop")
    hub.publish(_event("runtime-1", "message.complete", {"status": "complete"}))

    recent = hub.recent_runtime("runtime-1", max_age_seconds=30)
    assert recent is not None
    assert recent["run_id"] == run["run_id"]
    assert recent["running"] is False
    assert recent["latest_sequence"] == 1

    now = 1031.0
    assert hub.recent_runtime("runtime-1", max_age_seconds=30) is None


def test_disconnect_promotes_observer_instead_of_orphaning(monkeypatch):
    hub = SessionObserverHub()
    monkeypatch.setattr(server, "_session_event_hub", hub)
    owner = RecordingTransport()
    observer = RecordingTransport()
    server._sessions["runtime-1"] = {
        "transport": owner,
        "session_key": "stored-1",
        "running": True,
        "close_on_disconnect": False,
    }
    try:
        hub.subscribe("runtime-1", observer)
        reaped, detached = server._close_sessions_for_transport(owner)
        assert (reaped, detached) == (0, 0)
        assert server._sessions["runtime-1"]["transport"] is observer
    finally:
        server._sessions.pop("runtime-1", None)


def test_session_runtime_does_not_rebind_owner(monkeypatch):
    hub = SessionObserverHub()
    monkeypatch.setattr(server, "_session_event_hub", hub)
    owner = RecordingTransport()
    caller = RecordingTransport()
    server._sessions["runtime-1"] = {
        "transport": owner,
        "session_key": "stored-1",
        "running": True,
    }
    try:
        hub.begin_run("runtime-1", "desktop")
        response = server.dispatch(
            {
                "id": "runtime",
                "method": "session.runtime",
                "params": {"session_id": "runtime-1"},
            },
            caller,
        )
        assert response["result"]["run"]["running"] is True
        assert response["result"]["run"]["stored_session_id"] == "stored-1"
        assert server._sessions["runtime-1"]["transport"] is owner
    finally:
        server._sessions.pop("runtime-1", None)


def test_companion_resume_reuses_live_session_without_rebinding_owner(monkeypatch):
    class FakeDB:
        def get_session(self, session_id):
            return {"id": session_id, "cwd": "/tmp"}

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, session_id):
            return session_id

        def get_messages_as_conversation(self, *_args, **_kwargs):
            return []

    hub = SessionObserverHub()
    monkeypatch.setattr(server, "_session_event_hub", hub)
    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())
    owner = RecordingTransport()
    companion = RecordingTransport()
    server._sessions["runtime-1"] = {
        "transport": owner,
        "session_key": "stored-1",
        "running": True,
        "history": [],
        "history_lock": threading.RLock(),
        "created_at": 1.0,
        "cwd": "/tmp",
    }
    try:
        token = server.bind_transport(companion)
        try:
            response = server.handle_request(
                {
                    "id": "resume",
                    "method": "session.resume",
                    "params": {
                        "session_id": "stored-1",
                        "client_mode": "companion",
                    },
                }
            )
        finally:
            server.reset_transport(token)
        assert response["result"]["session_id"] == "runtime-1"
        assert server._sessions["runtime-1"]["transport"] is owner
        server.write_json(_event("runtime-1", "message.delta", {"text": "shared"}))
        assert owner.frames == companion.frames
    finally:
        server._sessions.pop("runtime-1", None)


def test_companion_submit_observes_without_rebinding_owner(monkeypatch):
    hub = SessionObserverHub()
    monkeypatch.setattr(server, "_session_event_hub", hub)
    owner = RecordingTransport()
    companion = RecordingTransport()
    session = {
        "transport": owner,
        "session_key": "stored-1",
        "running": False,
        "history": [],
        "history_lock": __import__("threading").RLock(),
        "agent": object(),
        "lazy": False,
        "created_at": 1.0,
    }
    server._sessions["runtime-1"] = session
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: True)
    monkeypatch.setattr(
        server,
        "_submit_prompt_to_compute_host",
        lambda rid, sid, current, text: server._ok(
            rid, {"status": "streaming", "turn_isolation": True}
        ),
    )
    try:
        response = server.dispatch(
            {
                "id": "submit",
                "method": "prompt.submit",
                "params": {
                    "session_id": "runtime-1",
                    "text": "continue",
                    "client": "iphone",
                    "client_mode": "companion",
                },
            },
            companion,
        )
        assert response["result"]["run"]["source"] == "iphone"
        assert server._sessions["runtime-1"]["transport"] is owner
        server.write_json(_event("runtime-1", "message.delta", {"text": "shared"}))
        assert owner.frames == companion.frames
    finally:
        server._sessions.pop("runtime-1", None)


def test_companion_queued_prompt_keeps_owner_when_drained(monkeypatch):
    hub = SessionObserverHub()
    monkeypatch.setattr(server, "_session_event_hub", hub)
    owner = RecordingTransport()
    companion = RecordingTransport()
    session = {
        "transport": owner,
        "session_key": "stored-1",
        "running": True,
        "history": [],
        "history_lock": __import__("threading").RLock(),
        "agent": None,
        "lazy": False,
    }
    server._sessions["runtime-1"] = session
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    drained: list[str] = []
    monkeypatch.setattr(
        server, "_run_prompt_submit", lambda _rid, _sid, _session, text: drained.append(text)
    )
    try:
        current_run = hub.begin_run("runtime-1", "desktop")
        response = server.dispatch(
            {
                "id": "queue",
                "method": "prompt.submit",
                "params": {
                    "session_id": "runtime-1",
                    "text": "next",
                    "client": "watch",
                    "client_mode": "companion",
                    "queued": True,
                },
            },
            companion,
        )
        assert response["result"]["status"] == "queued"
        queue_ticket = response["result"]["queue_ticket"]
        assert queue_ticket
        assert hub.runtime("runtime-1")["run_id"] == current_run["run_id"]
        assert hub.runtime("runtime-1")["queue_ticket"] is None
        session["running"] = False
        assert server._drain_queued_prompt("queue", "runtime-1", session)
        assert drained == ["next"]
        assert session["transport"] is owner
        successor = hub.runtime("runtime-1")
        assert successor["source"] == "watch"
        assert successor["run_id"] != current_run["run_id"]
        assert successor["queue_ticket"] == queue_ticket

        decorated, _targets = hub.publish(
            _event("runtime-1", "message.delta", {"text": "queued answer"})
        )
        assert decorated["params"]["run"]["queue_ticket"] == queue_ticket
    finally:
        server._sessions.pop("runtime-1", None)


def test_companion_steer_binds_to_the_active_run(monkeypatch):
    class SteeringAgent:
        def steer(self, text):
            assert text == "also check the logs"
            return True

    hub = SessionObserverHub()
    monkeypatch.setattr(server, "_session_event_hub", hub)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    owner = RecordingTransport()
    companion = RecordingTransport()
    session = {
        "transport": owner,
        "session_key": "stored-1",
        "running": True,
        "history": [],
        "history_lock": threading.RLock(),
        "agent": SteeringAgent(),
        "lazy": False,
    }
    server._sessions["runtime-1"] = session
    try:
        current_run = hub.begin_run("runtime-1", "desktop")
        response = server.dispatch(
            {
                "id": "steer",
                "method": "prompt.submit",
                "params": {
                    "session_id": "runtime-1",
                    "text": "also check the logs",
                    "client": "iphone",
                    "client_mode": "companion",
                },
            },
            companion,
        )

        assert response["result"]["status"] == "steered"
        assert response["result"]["accepted_run_id"] == current_run["run_id"]
        assert response["result"]["run"]["run_id"] == current_run["run_id"]
        assert session["transport"] is owner
    finally:
        server._sessions.pop("runtime-1", None)


def test_approval_response_forwards_request_identity(monkeypatch):
    from tools import approval

    captured: dict = {}

    def resolve(session_key, choice, **kwargs):
        captured.update(session_key=session_key, choice=choice, **kwargs)
        return 1

    monkeypatch.setattr(approval, "resolve_gateway_approval", resolve)
    server._sessions["runtime-1"] = {
        "session_key": "stored-1",
        "history_lock": threading.RLock(),
    }
    try:
        response = server.handle_request(
            {
                "id": "approval",
                "method": "approval.respond",
                "params": {
                    "session_id": "runtime-1",
                    "choice": "once",
                    "request_id": "approval-123",
                },
            }
        )
    finally:
        server._sessions.pop("runtime-1", None)

    assert response["result"]["resolved"] == 1
    assert captured == {
        "session_key": "stored-1",
        "choice": "once",
        "resolve_all": False,
        "request_id": "approval-123",
    }
