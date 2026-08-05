from __future__ import annotations

import threading

import pytest

from tools import approval


@pytest.fixture(autouse=True)
def _isolated_gateway_approval_state():
    approval._gateway_queues.clear()
    approval._gateway_notify_cbs.clear()
    yield
    approval._gateway_queues.clear()
    approval._gateway_notify_cbs.clear()


def test_gateway_approval_notification_and_response_share_request_id(monkeypatch):
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 5)
    notified = threading.Event()
    payloads: list[dict] = []
    result: dict = {}

    def notify(payload: dict) -> None:
        payloads.append(payload)
        notified.set()

    def wait_for_decision() -> None:
        result.update(
            approval._await_gateway_decision(
                "session-1",
                notify,
                {
                    "command": "dangerous command",
                    "description": "dangerous operation",
                    "pattern_key": "dangerous",
                    "pattern_keys": ["dangerous"],
                },
            )
        )

    worker = threading.Thread(target=wait_for_decision)
    worker.start()
    assert notified.wait(2)
    request_id = payloads[0]["request_id"]
    assert request_id

    assert approval.resolve_gateway_approval(
        "session-1", "once", request_id=request_id
    ) == 1
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["resolved"] is True
    assert result["choice"] == "once"


def test_stale_request_id_cannot_resolve_a_newer_approval():
    current = approval._ApprovalEntry({"request_id": "current-request"})
    approval._gateway_queues["session-1"] = [current]

    assert approval.resolve_gateway_approval(
        "session-1", "once", request_id="stale-request"
    ) == 0
    assert approval._gateway_queues["session-1"] == [current]
    assert current.event.is_set() is False
    assert current.result is None

    assert approval.resolve_gateway_approval(
        "session-1", "deny", request_id="current-request"
    ) == 1
    assert current.event.is_set() is True
    assert current.result == "deny"
