# Bug 0632: A blank task title silently suppressed the reward image

**Issue:** #632

## Bug Story

A completion delivered the emoji celebration plus a real-life fallback
suggestion instead of the generated image. Production logs showed
`generate_reward_image.empty_descriptions` immediately before a manifest row
with `reward_kind = image_fallback`, and that row's `task_title` had length 0.

The title was empty the whole way down. `intake_node` accepted whatever the
model returned for `title` — including `""` — because
`str(parsed.get("title", incoming[:200]))` only substitutes the default when
the key is *missing*, not when it is present and blank. That empty string was
written to the Notion page, read back by `selection_node` into
`active_task["title"]`, and read again by `complete_node`, which had the same
`.get(key, default)` bug and so passed `""` straight into `maybe_reward()`.
`generate_reward_image()` filtered the blank description out, found nothing
left, and returned `None`.

The guard it tripped over protected nothing. `_build_image_prompt()` never
embeds `task_descriptions` — by design, per the private-data discipline in
`docs/reward-system.md`. The prompt is assembled from intensity,
theme/style/palette, streak count, and user preferences alone. So the empty
list could not have degraded the prompt; it only prevented the image from ever
being requested.

**Bug class:** a validation guard on an input the guarded code does not use,
combined with `dict.get(key, default)` treating empty and missing as different
at three separate boundaries. Same visible symptom as bug 0620 ("we only get
emoji rewards"), different cause — there the API call was rejected, here it was
never made.

## Fix

- `app/tools/rewards.py`: blank descriptions no longer abort image generation.
  Intensity remains the only gate.
- `app/graph/nodes/intake.py`: a blank model-supplied title falls back to the
  user's own words; a save that cannot be named at all asks the user for a name
  instead of writing a nameless page.
- `app/graph/nodes/complete.py`: reads `active_task["title"]` with `or`, and
  passes the empty string through rather than fabricating a placeholder that
  would be stored in the manifest as if it were the user's words.
- `app/graph/nodes/{check_in,need_help,cannot_finish,rejection}.py`: the same
  `.get(key, default)` hole in their user-facing display fallbacks.

## Regression Tests

`test_blank_title_still_generates_image.py` — the direct guard. Verified to
fail against the pre-fix code.

- `test_blank_description_still_calls_images_generate` — the load-bearing
  assertion: a blank title still results in an awaited `images.generate` call
  and a returned image, not `None`.
- `test_all_blank_descriptions_still_generate` — whitespace-only and
  empty-string descriptions, since the old filter used `.strip()`.
- `test_prompt_never_contains_task_text` — pins the reason the guard was
  unnecessary. If a future change starts embedding task descriptions in the
  prompt, this fails and the tolerance above has to be revisited.
- `test_complete_node_does_not_fabricate_a_title` — asserts `complete_node`
  forwards the empty string to `maybe_reward` rather than the old `"task"`
  placeholder, which would have been written to the private manifest column.
- `test_intake_blank_title_does_not_create_a_page` — asserts a blank model
  title with a blank incoming message asks for a name instead of calling
  `notion.create_task`.

## Lesson

A guard that rejects input the downstream code never reads is not defensive —
it is an outage waiting for the input to go blank. When adding a validation
check, confirm the value actually reaches something that can be harmed by it.

Corollary: `dict.get(key, default)` is not a blank check. Where a value flows
into user-visible text or private storage, normalize with `or` at the
boundary — and prefer failing loudly over substituting a placeholder that later
reads as the user's own words.
