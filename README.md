# work-controller
Issue-based work management and PR-based work-unit orchestration

## Repository layout

- `.github/ISSUE_TEMPLATE/`: GitHub-native issue forms (reserved path).
- `.github/workflows/`: GitHub-native event triggers and contract enforcement (reserved path).
- `validation/`: reusable validation functions and their tests, independent of GitHub event handling.

Run `python3 validation/validate_issue.py --test` to test the Work issue validator.

## Work Unit pull requests

Use `.github/pull_request_template.md`: `Work` (`Refs #<issue number>`), `Changes`, and `Verification`. The Work section must contain exactly one same-repository `Refs #<issue number>` reference. The required `pr-template` check fetches that Issue, rejects missing or PR targets, requires OPEN state, and reuses the Issue validator. Scope and semantic evidence remain separate contracts. Use `Refs`, not `Closes`, because one Work may have multiple Work Units.

Run `python3 validation/validate_pr.py --test` for validator tests. The `pr-template` Actions check runs on opened, edited, reopened, synchronized, and ready-for-review PRs. To enforce merge blocking, configure a main-branch protection rule or active ruleset requiring this check (GitHub Actions source) and disallow bypasses. A failed check alone does not prevent a privileged merge. Candidate tests run in a separate read-only job. The required check validates metadata using validators checked out at the base SHA, not candidate validators. The workflow definition remains PR-controlled; trusted review of workflow changes and a dedicated independent required check source are needed for adversarial enforcement. PR body edits trigger asynchronous revalidation.

The Work relationship is checked on PR events, not automatically on Issue changes. The check is a point-in-time result; revalidate the current Issue and PR before merge. A reference outside the Work section is not a membership declaration.

## Merge contract

Run from a trusted, up-to-date main checkout with authenticated GitHub CLI (never run the preflight from a candidate branch).

```sh
python3 validation/validate_merge.py --repo OWNER/REPO --pr NUMBER
```

Review the returned Issue objective, scope, acceptance criteria, PR changes and verification, and the actual code diff at the recorded base/head SHA. Review any contract/workflow changes explicitly. Reject unrelated changes and unsupported verification claims. The digest binds the review to the displayed metadata and code revisions; it is not proof of semantic correctness or reviewer identity.

```sh
python3 validation/validate_merge.py --repo OWNER/REPO --pr NUMBER --approved-snapshot DIGEST --expected-head SHA
```

Add `--merge` only with explicit merge authorization. The script re-reads metadata, requires a clean mergeable PR and successful required checks, then re-reads before optionally calling the GitHub merge endpoint with the expected head SHA. No bypass or auto-merge. Tests: `python3 validation/validate_merge.py --test`.

Native GitHub UI/API merges can bypass this preflight while satisfying the ruleset. Issue or PR metadata edits are not atomically bound to GitHub's merge API; there is a residual race, and Issue changes do not trigger PR CI. This is the required operational path for the future Controller, not a universal server-side enforcement guarantee. Independent trusted reviewer/check-source hardening and automatic Issue-change invalidation remain follow-up work.
