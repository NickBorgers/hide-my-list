You are responding to a request in ${REPO} issue #${ISSUE_NUMBER}.

The user said: ${COMMENT_BODY}

WORKFLOW:
1. Read the user's request carefully
2. Fetch the full issue for context: gh api repos/${REPO}/issues/${ISSUE_NUMBER}
3. If needed, fetch issue comments: gh api repos/${REPO}/issues/${ISSUE_NUMBER}/comments
4. Implement the requested changes
5. Run `shellcheck scripts/*.sh` and `yamllint .github/workflows/*.yml` to verify your changes
6. Create a branch, commit your changes, and open a PR

IMPORTANT RULES FOR THIS REPOSITORY:
- This is a Python + LangGraph app (`app/`, `migrations/`, `tests/`, `docker/`, `pyproject.toml`)
- Use `shellcheck scripts/*.sh` for shell script linting
- Use `yamllint .github/workflows/*.yml` for workflow validation
- Check documentation links are not broken
- Reference docs/architecture.md for system design context
- Never use `git push --no-verify`

Complete the user's request. If you make changes, create a branch, commit them, and open a PR.
BEFORE creating a new PR, check if one already exists for this issue:
  ./scripts/issue-pr-claims.sh duplicates ${ISSUE_NUMBER} --repo ${REPO}
If a PR already exists, push changes to that PR's branch instead of creating a new one.
Reply to the user using: gh issue comment ${ISSUE_NUMBER} --body 'YOUR_RESPONSE'
