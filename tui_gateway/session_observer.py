"""Multi-client observation and bounded replay for TUI gateway sessions.

The desktop owns a session transport, but companion clients must be able to
observe the same turn without rebinding that transport.  This module is the
transport-agnostic broker used by ``server.write_json``: the owner still gets
the original event stream, while subscribed transports receive a copy carrying
one canonical run identity and monotonic sequence number.

Replay is deliberately memory-only and bounded.  Durable conversation state
continues to belong to ``state.db``; the journal only covers reconnect gaps
inside the lifetime of the gateway process.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any


_TERMINAL_EVENTS = {"error": "failed"}


@dataclass
class _Journal:
    sequence: int = 0
    generation: int = 0
    run_id: str | None = None
    source: str = "unknown"
    running: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    terminal_state: str | None = None
    bytes_used: int = 0
    events: deque[tuple[int, int, dict[str, Any]]] = field(default_factory=deque)


class SessionObserverHub:
    """Thread-safe session event fan-out with bounded in-memory replay."""

    def __init__(
        self,
        *,
        max_sessions: int = 64,
        max_events_per_session: int = 512,
        max_bytes_per_session: int = 2 * 1024 * 1024,
        max_replay_event_bytes: int = 64 * 1024,
    ) -> None:
        self._lock = threading.RLock()
        self._max_sessions = max(1, int(max_sessions))
        self._max_events_per_session = max(1, int(max_events_per_session))
        self._max_bytes_per_session = max(1024, int(max_bytes_per_session))
        self._max_replay_event_bytes = max(1024, int(max_replay_event_bytes))
        self._journals: OrderedDict[str, _Journal] = OrderedDict()
        self._observers: dict[str, set[Any]] = {}
        self._sessions_by_transport: dict[Any, set[str]] = {}

    def _journal(self, session_id: str) -> _Journal:
        journal = self._journals.get(session_id)
        if journal is None:
            journal = _Journal()
            self._journals[session_id] = journal
        else:
            self._journals.move_to_end(session_id)
        self._prune_sessions()
        return journal

    def _prune_sessions(self) -> None:
        if len(self._journals) <= self._max_sessions:
            return
        for session_id in list(self._journals):
            if len(self._journals) <= self._max_sessions:
                break
            if self._observers.get(session_id):
                continue
            self._journals.pop(session_id, None)

    @staticmethod
    def _snapshot(session_id: str, journal: _Journal) -> dict[str, Any]:
        earliest = journal.events[0][0] if journal.events else journal.sequence + 1
        return {
            "session_id": session_id,
            "run_id": journal.run_id,
            "generation": journal.generation,
            "source": journal.source,
            "running": journal.running,
            "started_at": journal.started_at,
            "finished_at": journal.finished_at,
            "terminal_state": journal.terminal_state,
            "latest_sequence": journal.sequence,
            "earliest_replay_sequence": earliest,
        }

    def begin_run(self, session_id: str, source: str | None = None) -> dict[str, Any]:
        """Create the canonical identity before a prompt begins emitting."""
        normalized_source = str(source or "unknown").strip().lower() or "unknown"
        with self._lock:
            journal = self._journal(session_id)
            journal.generation += 1
            journal.run_id = uuid.uuid4().hex
            journal.source = normalized_source
            journal.running = True
            journal.started_at = time.time()
            journal.finished_at = None
            journal.terminal_state = None
            return self._snapshot(session_id, journal)

    def settle_run(self, session_id: str, terminal_state: str) -> dict[str, Any]:
        with self._lock:
            journal = self._journal(session_id)
            journal.running = False
            journal.finished_at = time.time()
            journal.terminal_state = str(terminal_state or "unknown")
            return self._snapshot(session_id, journal)

    def subscribe(
        self,
        session_id: str,
        transport: Any,
        *,
        after_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Subscribe ``transport`` and return an atomic snapshot plus replay."""
        after = max(0, int(after_sequence or 0))
        with self._lock:
            journal = self._journal(session_id)
            self._observers.setdefault(session_id, set()).add(transport)
            self._sessions_by_transport.setdefault(transport, set()).add(session_id)
            snapshot = self._snapshot(session_id, journal)
            events = [copy.deepcopy(frame) for seq, _size, frame in journal.events if seq > after]
        return {"run": snapshot, "events": events}

    def unsubscribe(self, session_id: str, transport: Any) -> bool:
        with self._lock:
            observers = self._observers.get(session_id)
            removed = bool(observers and transport in observers)
            if observers:
                observers.discard(transport)
                if not observers:
                    self._observers.pop(session_id, None)
            sessions = self._sessions_by_transport.get(transport)
            if sessions:
                sessions.discard(session_id)
                if not sessions:
                    self._sessions_by_transport.pop(transport, None)
            return removed

    def unregister_transport(self, transport: Any) -> None:
        with self._lock:
            session_ids = list(self._sessions_by_transport.pop(transport, set()))
            for session_id in session_ids:
                observers = self._observers.get(session_id)
                if observers:
                    observers.discard(transport)
                    if not observers:
                        self._observers.pop(session_id, None)

    def observer_transports(self, session_id: str, *, excluding: Any = None) -> list[Any]:
        with self._lock:
            return [
                transport
                for transport in self._observers.get(session_id, set())
                if transport is not excluding
            ]

    def runtime(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._snapshot(session_id, self._journal(session_id))

    def publish(self, frame: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
        """Decorate, journal, and resolve observer targets for one event frame."""
        params = frame.get("params") if isinstance(frame, dict) else None
        session_id = str((params or {}).get("session_id") or "")
        event_type = str((params or {}).get("type") or "")
        if frame.get("method") != "event" or not session_id:
            return frame, []

        with self._lock:
            journal = self._journal(session_id)
            if event_type == "message.start" and not journal.running:
                self.begin_run(session_id, journal.source)
                journal = self._journal(session_id)

            payload = (params or {}).get("payload")
            if event_type == "session.info" and isinstance(payload, dict):
                running = payload.get("running")
                if isinstance(running, bool):
                    journal.running = running
                    if not running and journal.run_id and journal.finished_at is None:
                        journal.finished_at = time.time()
                        journal.terminal_state = journal.terminal_state or "completed"
            terminal = _TERMINAL_EVENTS.get(event_type)
            if event_type == "message.complete":
                terminal = (
                    "failed"
                    if isinstance(payload, dict)
                    and str(payload.get("status") or "").strip().lower() == "error"
                    else "completed"
                )
            if terminal:
                journal.running = False
                journal.finished_at = time.time()
                journal.terminal_state = terminal

            journal.sequence += 1
            decorated = copy.deepcopy(frame)
            decorated_params = decorated.setdefault("params", {})
            decorated_params["run"] = {
                "run_id": journal.run_id,
                "generation": journal.generation,
                "sequence": journal.sequence,
                "source": journal.source,
            }

            try:
                encoded_size = len(
                    json.dumps(decorated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
            except Exception:
                encoded_size = self._max_replay_event_bytes + 1

            if encoded_size <= self._max_replay_event_bytes:
                journal.events.append((journal.sequence, encoded_size, decorated))
                journal.bytes_used += encoded_size
                while journal.events and (
                    len(journal.events) > self._max_events_per_session
                    or journal.bytes_used > self._max_bytes_per_session
                ):
                    _seq, old_size, _old_frame = journal.events.popleft()
                    journal.bytes_used -= old_size

            targets = list(self._observers.get(session_id, set()))
            return decorated, targets
