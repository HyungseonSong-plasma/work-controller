#!/usr/bin/env bash
set -euo pipefail
gh api "repos/$GITHUB_REPOSITORY/issues/$ISSUE_NUMBER" > "$RUNNER_TEMP/work-issue.json"
jq -r '.body // ""' "$RUNNER_TEMP/work-issue.json" > "$RUNNER_TEMP/work-body.md"
if python3 validation/validate_issue.py < "$RUNNER_TEMP/work-body.md"; then
  echo "Work contract passed."
else
  if [[ "$(jq -r .state "$RUNNER_TEMP/work-issue.json")" == "open" ]]; then
    gh issue close "$ISSUE_NUMBER" --repo "$GITHUB_REPOSITORY" --reason "not planned" --comment "Work contract failed. Supply non-empty Objective, Scope, and Acceptance criteria sections using the Work template, then reopen the issue."
  fi
  echo "::error::Work contract failed."
  exit 1
fi
