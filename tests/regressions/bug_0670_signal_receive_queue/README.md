# Bug #670: Signal receive loop blocked behind graph turns

`SignalListener.run()` awaited each graph turn inside the WebSocket receive
loop. A slow turn stopped the app from reading later Signal envelopes, so read
receipts and queued messages waited behind model latency.

The listener owns a bounded text-message buffer and a single serial graph
worker. The receive loop authenticates messages, records ingress, schedules read
receipts, enqueues text, and returns to the socket. The worker debounces
same-peer backlog briefly and invokes the graph with one combined turn.

Tests live in `tests/unit/test_signal_listener_auth.py` and
`tests/e2e/scenarios/test_signal_receive_queue.py`.
