# Bug #647: cannot_finish echoed the user's message back

The cannot_finish prompt's output schema included a `user_message` field, and
models fill a field with that name with an echo of the user's own words. The
response parser preferred `user_message` over `progress_question`, so a user
saying "this is too big, I can't finish it" got "this is too big, I can't
finish it" back — at the second-highest shame-risk moment in the product.

The fix removes `user_message` from the output schema (template + spec) and
makes the parser select only the user-facing fields (`progress_question`,
`next_sub_task_message`), falling back to the shame-safe progress question
when neither is present instead of leaking raw JSON or an echo.
