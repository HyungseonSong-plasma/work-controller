# work-controller
Issue-based work management and PR-based work-unit orchestration

## Repository layout

- `.github/ISSUE_TEMPLATE/`: GitHub-native issue forms (reserved path).
- `.github/workflows/`: GitHub-native event triggers and contract enforcement (reserved path).
- `validation/`: reusable contract modules and shared model, independent of GitHub event handling.
- `app/cli.py`: single CLI entrypoint (`python3 -m app.cli`).
- `controller/decision_engine.py`: read-only Controller decisions.

Run all unit tests using `python3 -m app.cli test`.

Run `python3 -m app.cli test issue` to test the Work issue validator.

## Work Unit pull requests

Use `.github/pull_request_template.md`: `Work` (`Refs #<issue number>`), `Changes`, and `Verification`. The Work section must contain exactly one same-repository `Refs #<issue number>` reference. The required `pr-template` check fetches that Issue, rejects missing or PR targets, requires OPEN state, and reuses the Issue validator. Scope and semantic evidence remain separate contracts. Use `Refs`, not `Closes`, because one Work may have multiple Work Units.

Run `python3 -m app.cli test pr` for validator tests. The `pr-template` Actions check runs on opened, edited, reopened, synchronized, and ready-for-review PRs. To enforce merge blocking, configure a main-branch protection rule or active ruleset requiring this check (GitHub Actions source) and disallow bypasses. A failed check alone does not prevent a privileged merge. Candidate tests run in a separate read-only job. The required check validates metadata using validators checked out at the base SHA, not candidate validators. The workflow definition remains PR-controlled; trusted review of workflow changes and a dedicated independent required check source are needed for adversarial enforcement. PR body edits trigger asynchronous revalidation.

The Work relationship is checked on PR events, not automatically on Issue changes. The check is a point-in-time result; revalidate the current Issue and PR before merge. A reference outside the Work section is not a membership declaration.

## Merge contract

Run from a trusted, up-to-date main checkout with authenticated GitHub CLI (never run the preflight from a candidate branch).

```sh
python3 -m app.cli merge --repo OWNER/REPO --pr NUMBER
```

Review the returned Issue objective, scope, acceptance criteria, PR changes and verification, and the actual code diff at the recorded base/head SHA. Review any contract/workflow changes explicitly. Reject unrelated changes and unsupported verification claims. The digest binds the review to the displayed metadata and code revisions; it is not proof of semantic correctness or reviewer identity.

```sh
python3 -m app.cli merge --repo OWNER/REPO --pr NUMBER --approved-snapshot DIGEST --expected-head SHA
```

Add `--merge` only with explicit merge authorization. The script re-reads metadata, requires a clean mergeable PR and successful required checks, then re-reads before optionally calling the GitHub merge endpoint with the expected head SHA. No bypass or auto-merge. Tests: `python3 -m app.cli test merge`.

Native GitHub UI/API merges can bypass this preflight while satisfying the ruleset. Issue or PR metadata edits are not atomically bound to GitHub's merge API; there is a residual race, and Issue changes do not trigger PR CI. This is the required operational path for the future Controller, not a universal server-side enforcement guarantee. Independent trusted reviewer/check-source hardening and automatic Issue-change invalidation remain follow-up work.

## Work completion contract

A code Work may close as completed when its Issue contract is valid, at least one declared Work Unit has merged into this repository's main with valid PR metadata, and no declared Work Unit remains open. A closed-unmerged PR is discarded, not delivered; its disposition must be considered during acceptance review. No new template fields or Work IDs. Non-code work without merged PRs is not covered by this completion policy.

Membership is read from current canonical `Refs #N` lines in the PR's Work section (including malformed PRs with such lines); ordinary references elsewhere do not establish membership. All PR pages are scanned. Removing or changing the declaration can alter membership; historical linkage, reachability after reverts, and semantic verification are not proved by this check.

From trusted main, review current acceptance criteria and the delivered/discarded Work Units:

```sh
python3 -m app.cli completion --repo OWNER/REPO --work NUMBER
python3 -m app.cli completion --repo OWNER/REPO --work NUMBER --approved-snapshot DIGEST
```

Add `--close --evidence "review evidence or links"` only after explicit acceptance review and authorization. The script records the digest and evidence in an Issue comment, rechecks the snapshot, then closes as completed. The digest is not proof of reviewer identity or semantic completion. Tests: `python3 -m app.cli test completion`.

The Issue workflow also checks completed closures after the event and reopens structural violations with a reason. Not-planned and duplicate closures are unaffected. Direct UI completion is a manual acceptance decision, not a required digest attestation. The guard is asynchronous, cannot guarantee atomic closure, may fail on API/runner errors, and is not continuously triggered by later PR/Issue changes. Controller must revalidate before closure; semantic acceptance remains manual.

## Controller discovery

Run from trusted main with authenticated GitHub CLI:

```sh
python3 -m app.cli controller --repo OWNER/REPO
python3 -m app.cli test controller
```

The Controller reads every page of open Issues and PRs, excludes PRs from the Work list, and uses existing validators for membership and completion. It emits JSON with one or more recommended actions per Work and unassigned open PRs. It does not write, delegate, merge, or close.

- `PLAN_WORK`: no delivered or active Work Unit; plan or replan from the Issue.
- `FIX_WORK_CONTRACT`, `FIX_WORK_REFERENCE`, `FIX_PR_CONTRACT`, `FIX_PR_TARGET`: repair structural problems.
- `CONTINUE_DRAFT`, `FIX_CI`, `WAIT_CI`, `UPDATE_BRANCH`, `WAIT_MERGE_GATE`, `REFRESH_STATE`: resolve or refresh PR state.
- `REVIEW_MERGE`: inspect scope and evidence, then use the existing merge preflight if authorized.
- `REVIEW_COMPLETION`, `REVIEW_COMPLETION_BLOCKERS`: inspect acceptance criteria or resolve completion blockers.

Review digests are suggestions bound to the observed snapshot, not approvals. The controller does not infer semantic completion, and live data can change during discovery. Re-run the relevant preflight immediately before a write. Read-only/non-code Work completion and A/B/C file modification authority require separate policy approval; this version does not change those contracts. API errors fail the run rather than producing a potentially incomplete execution plan.
