# Bug #671: classifier backend failures must not spend a second LLM call

When `classify_intent` caught every exception by returning `intent=CHAT`, the
graph routed into `chat_node`. If the model backend was unhealthy, that meant a
failed cheap-tier classifier call was immediately followed by a failed
medium-tier chat call, only to produce the same constant fallback string.

The fix distinguishes backend call failure from unusable classifier output.
Raised classifier calls draft the LLM-unavailable fallback directly and route
straight to `send`; present-but-invalid model output still resolves to `CHAT`.

## Coverage

`test_classify_error_short_circuit.py` runs the compiled graph with fake LLM and
Signal clients. It proves a classifier exception sends exactly one fallback
draft with only the classifier LLM call recorded, and that an unusable classifier
label still routes through `chat_node`.
