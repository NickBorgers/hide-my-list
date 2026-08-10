# bug_0652 — reward images had no relationship to the task that earned them

Fixed in PR #652.

## Symptom

Celebration images looked arbitrary. Auditing the test instance's
`reward_manifests` rows against the themes they drew showed the connection was
absent, not weak: a grocery run drew "ancient temple lit by aurora borealis",
a small errand drew "astronaut planting flag on colorful planet".

## Cause

`_build_image_prompt()` accepted `task_descriptions` and never read it.
`generate_reward_image()` accepted `work_type` and `energy_level` and never used
them. Theme selection was a weighted random draw from five hardcoded entries per
intensity tier, so the prompt was byte-identical whether the user bought
groceries or filed taxes.

The docstring claimed task descriptions were "used to classify task motifs but
NOT copied verbatim into the prompt". Only the second half was ever built — the
classification step did not exist, so nothing task-derived reached the prompt at
all.

## Fix

`classify_task_motif()` labels the completed task with one key from the fixed
`_MOTIFS` vocabulary, on the local cheap LLM tier. The label — never the title —
biases theme selection (`_MOTIF_BONUS`) and adds one scene line to the
prompt. The title still never leaves the tailnet: text inference runs against the
local proxy while image generation calls OpenAI.

## What the test pins

A classified motif must reach the image prompt as its scene phrase, and the
theme chosen for it must be one tagged for that motif. Both halves matter: a
motif line on a scene picked at random would still produce an image about the
wrong thing.

The blank/unclassifiable path is pinned separately in
`tests/regressions/bug_0632_reward_image_blank_title/` — an unresolved motif must
leave the generic prompt untouched rather than cost the user their image.
