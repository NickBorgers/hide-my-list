# Bug 0620: Every reward image silently degraded to the emoji fallback

**PR:** #621

No issue was filed: the bug was found and fixed in the same pass, and opening
an issue would have triggered the auto-resolve pipeline to write a competing
implementation. The catalog number is a local id.

## Bug Story

Reward images never reached the user. The feature looked unbuilt — the observed
behavior was "we only get emoji rewards" — but the whole path existed and was
wired end to end: `maybe_reward()` → `complete_node` sets `attachment_path` →
`send_node` → `signal_client` base64 attachment.

The cause was one argument. `generate_reward_image()` called
`client.images.generate(..., response_format="b64_json")`. Per the installed
`openai==2.37.0` SDK docstring for that method:

> response_format: ... This parameter isn't supported for the GPT image models,
> which always return base64-encoded images.

`gpt-image-1` rejects it with a 400. Two things then conspired to hide it:

1. **The call is wrapped in a bare `except Exception: return None`.** By design
   — `docs/reward-system.md` requires that image failure never surfaces an error
   to the user, because a visible "reward failed" message is exactly the
   "expected reward didn't arrive" anti-pattern the ADHD design priorities warn
   about. So a hard API rejection was indistinguishable from "no API key set".
2. **`None` is a legitimate, well-handled return value.** It appends a real-life
   fallback suggestion and delivers a normal-looking emoji reward. Nothing looked
   broken from the outside, and the manifest row still recorded a delivery.

The existing unit test mocked `images.generate` wholesale with a `MagicMock`,
which accepts any keyword argument. It asserted the happy path returned a path
and logged `image_gen.start` / `image_gen.end` — all of which passed while the
real API call could never have succeeded.

**Bug class:** silent degradation behind an intentionally-swallowing exception
handler, masked by a mock that cannot reject invalid arguments. Related to bug
class 3 (image orphaned from delivery) but distinct: there the image existed and
was not delivered; here it was never generated.

## Fix

- `app/tools/rewards.py`: drop `response_format` from the `images.generate`
  call. A comment marks it as load-bearing so it is not reintroduced.

## Regression Tests

The tests live in `tests/unit/test_rewards.py` — no DB or network is needed, so
they belong with the rest of the reward unit tests rather than in this directory.

`TestImageGenerationCallContract` — asserts on the
kwargs actually passed to `images.generate`, rather than on the mocked return
value:

- `test_response_format_is_not_sent` — the direct guard. Verified to fail when
  the parameter is reintroduced.
- `test_only_sends_parameters_the_sdk_accepts` — introspects
  `inspect.signature(AsyncImages.generate)` and asserts every kwarg is a real
  parameter of the *installed* SDK. Catches typos and parameters removed by a
  future SDK upgrade, which would fail the same silent way.
- `test_sends_expected_model_and_size` — pins model/size/quality/n to the
  technical table in `docs/reward-system.md`.

## Lesson

When a code path deliberately swallows failures to protect the user experience,
its tests cannot rely on the return value — the return value is the same whether
the call was valid or not. Assert on the outbound call, and validate arguments
against the real dependency's signature rather than a permissive mock.
