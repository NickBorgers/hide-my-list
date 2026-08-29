# Bug #669: LLM calls carried no timeout or retry cap — one wedged model host stalled the app for 90 minutes

`app.models.llm()` built its `ChatOpenAI` instance with `model`, `temperature`, `base_url`,
`api_key`, and (for the cheap tier) `max_tokens` and `extra_body` — and nothing else. It passed
neither `timeout` nor `max_retries`, so both fell back to the OpenAI SDK defaults. Every other
outbound client in the app is bounded: `app/tools/signal_client.py` sets a 60s read timeout,
`app/tools/notion.py` a 30s one. The LLM path was the only unbounded one.

The reverse proxy in front of the LiteLLM proxy gives up on an unresponsive backend at 600s and
returns a 504. With the SDK's default of 2 retries that is three attempts, so a single wedged
model host cost **1800s per call** — three observed calls failed at 1,801,430ms, 1,801,425ms, and
1,801,184ms, all after the model backend stopped responding entirely and logged nothing further.

The model backend holds one model in RAM and serves one request at a time, so the cost is not
confined to the turn that hit it. Three messages sent within 50 seconds took 90 minutes to answer:
the first message's reply landed normally, the second waited 30 minutes for `intake` to exhaust
its retries, and the third was not even read off the WebSocket until the second finished — then
spent 30 minutes failing `classify` and another 30 failing the `chat` fallback it routes to on
classification error. The app never crashed; it went deaf.

The fix passes an explicit `timeout` (default 120s) and `max_retries` (default 1) on every
`ChatOpenAI` instance, overridable via `LLM_REQUEST_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES`. The
worst case drops from 1800s to 240s per call, and lands under the gateway's 600s so the app fails
on its own clock and can classify its own error rather than reading an opaque 504.

Successful calls observed on this backend run 0.6s to 8.2s, so the 120s default leaves room for a
slow reasoning turn plus queue wait behind another tenant on the shared single-threaded backend.

The two remaining amplifiers — the receive loop awaiting the graph inline, and a failed
`classify` falling through to a second full-length `chat` call — are tracked separately; this fix
addresses only the unbounded call itself.

## Coverage

`test_llm_call_unbounded.py` proves `llm()` passes both `timeout` and `max_retries` to
`ChatOpenAI` for every tier, that both are honoured from `LLM_REQUEST_TIMEOUT_SECONDS` /
`LLM_MAX_RETRIES`, that unparseable and non-positive overrides fall back to the defaults rather
than reintroducing an unbounded call, and that the default retry budget multiplied by the default
timeout stays under the 600s gateway ceiling that produced the original 1800s hang.
