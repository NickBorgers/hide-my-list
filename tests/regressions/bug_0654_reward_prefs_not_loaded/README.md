# Bug #654: stored reward preferences never reached image generation

`maybe_reward` never loaded reward preferences from Postgres. The `user_prefs`
table (migration 0004) existed and the `load_reward_prefs` helper was added in
this PR, but the call site in `complete.py` passed no `user_prefs` argument, so
the preference-loading branch never ran and `_select_theme` always used the
hardcoded default triples regardless of stored profile.

## Regression coverage

The test lives in `tests/integration/test_reward_prefs_load.py`, as
`test_maybe_reward_uses_stored_prefs_without_mock`.

Writes a preference profile to Postgres, invokes `maybe_reward` with the real
`load_reward_prefs` path (not patched), and asserts that `generate_reward_image`
receives the stored `user_prefs`. Fails on any regression that breaks the
DB-read → `maybe_reward` → image-generation wiring.
