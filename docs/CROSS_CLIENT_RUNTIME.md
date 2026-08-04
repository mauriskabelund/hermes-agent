# Cross-client runtime contract

Hermes Desktop, WebUI-backed phone clients, and watch clients share one live
TUI Gateway session. Clients keep their existing renderers; the gateway owns
execution, run identity, ordering, replay, and controls.

## Discovery

`gateway.ready.payload.cross_client_runtime == 1` advertises this contract.
Older gateways remain usable through each client's legacy transport.

`session.active_list` maps a durable `session_key` (the state.db session id) to
the in-process runtime `id`. `session.runtime` reads current state without
changing ownership.

## Observation and replay

`session.observe {session_id, after_sequence}` subscribes the calling WebSocket
without rebinding the session owner. Its atomic result contains:

- `runtime_session_id` and `stored_session_id`
- `run`: canonical identity, source, lifecycle, and replay bounds
- `events`: ordered frames whose sequence is greater than `after_sequence`

Every subsequent session event has `params.run` containing `run_id`,
`generation`, `sequence`, and `source`. Replay is an in-memory reconnect aid,
not a second transcript store; state.db remains durable truth. A client detects
a replay gap when its requested next sequence is below
`earliest_replay_sequence`, then reloads the durable transcript before applying
newer events.

`session.unobserve` removes the subscription. Disconnect removes every
subscription held by that transport. If the owner disconnects during a run, an
observer is promoted so execution and controls remain reachable.

## Submission and controls

`prompt.submit` accepts `client_mode: "companion"` and a stable `client` source
such as `desktop`, `iphone`, or `watch`. Companion submission subscribes the
caller before accepting the turn and never steals the owner transport. The
response includes the canonical `run` snapshot. Queued turns retain their
origin and the existing owner.

Companion clients also pass `client_mode: "companion"` to `session.resume`.
If another client wins a concurrent resume, the gateway atomically subscribes
the companion under the resume lock and preserves the winner as owner.

All clients use the existing controls against the same runtime session:

- stop: `session.interrupt`
- steer: `session.steer`
- redirect: `session.redirect`
- approval/clarification: existing response RPCs

Clients must treat `409`/busy results as shared state, not create another local
agent. Only the gateway persists the model transcript; adapters may journal
presentation events for reconnect, but must not execute or append a parallel
assistant turn.
