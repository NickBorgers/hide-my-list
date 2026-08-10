# Bug 0613: Signal ingress liveness was not asserted

**Issue:** #613

## Bug Story

Signal messages stopped reaching the app while every component-level health
check still looked green. The receive WebSocket could stay open without
delivering frames, so `receive_messages()` blocked indefinitely and never logged
or reconnected. Separately, the app had no durable inbound-silence detector, so
weeks without processed inbound traffic looked the same as a quiet user.

## Fix

- `app/tools/signal_client.py` applies an env-configurable idle deadline to the
  receive stream and reconnects when no frame arrives in time.
- `app/tools/signal_ingress_health.py` stores the last authorized inbound item in
  Postgres and checks it against an env-configurable silence threshold.
- `app/scheduler/jobs.py` registers a recurring `signal_ingress_silence` job
  that logs a warning when the durable marker is stale. Silence is expected on
  low-traffic instances that do not receive a message every day, so it is
  recorded as a log event rather than a paging ops alert.

## Regression Tests

Tests live in this directory:

- `test_receive_idle_timeout.py` asserts a WebSocket that opens and then goes
  silent is closed and reconnected.
- `test_signal_ingress_health.py` asserts the silence detector logs (and does
  not enqueue an ops alert) past the threshold and stays quiet below it.
