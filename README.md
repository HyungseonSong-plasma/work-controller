# work-controller
Issue-based work management and PR-based work-unit orchestration

## Repository layout

- `.github/ISSUE_TEMPLATE/`: GitHub-native issue forms (reserved path).
- `.github/workflows/`: GitHub-native event triggers and contract enforcement (reserved path).
- `validation/`: reusable validation functions and their tests, independent of GitHub event handling.

Run `python3 validation/validate_issue.py --test` to test the Work issue validator.

## Work Unit pull requests

Use `.github/pull_request_template.md`: `Work` (`Refs #<issue number>`), `Changes`, and `Verification`. The Work section must contain exactly one same-repository `Refs #<issue number>` reference. The required `pr-template` check fetches that Issue, rejects missing or PR targets, requires OPEN state, and reuses the Issue validator. Scope and semantic evidence remain separate contracts. Use `Refs`, not `Closes`, because one Work may have multiple Work Units.

Run `python3 validation/validate_pr.py --test` for validator tests. The `pr-template` Actions check runs on opened, edited, reopened, synchronized, and ready-for-review PRs. To enforce merge blocking, configure a main-branch protection rule or active ruleset requiring this check (GitHub Actions source) and disallow bypasses. A failed check alone does not prevent a privileged merge. The check executes PR-controlled code with read-only permissions; changes to the contract itself require trusted review. PR body edits trigger asynchronous revalidation.

The Work relationship is checked on PR events, not automatically on Issue changes. The check is a point-in-time result; revalidate the current Issue and PR before merge. A reference outside the Work section is not a membership declaration.
