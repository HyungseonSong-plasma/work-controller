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

## Work Issue format

Use the native Work form or the identical Markdown body when creating Issues with a connector. Keep four top-level sections and two ordered subsections under Scope. Write concise restrictions under Constraints and intended work under Actions, without repeating Objective or Acceptance criteria.

```markdown
### Work type

Implementation

### Objective

Describe the intended outcome.

### Scope

#### Constraints

- State restrictions and exclusions.

#### Actions

- State the work to perform.

### Acceptance criteria

- State observable completion evidence.
```

Both Scope subsections must be non-empty, unique and in the order shown. Replace the form's placeholder bullets. The single validator (`python3 -m app.cli issue`) enforces this format for Issue events and downstream Work validation. No compatibility flags, alternate format, or historical migration.

## Work Unit pull requests

Use `.github/pull_request_template.md`: `Work` (`Refs #<issue number>`), `Changes`, and `Verification`. The Work section must contain exactly one same-repository `Refs #<issue number>` reference. The required `pr-template` check fetches that Issue, rejects missing or PR targets, requires OPEN state, and reuses the Issue validator. Scope and semantic evidence remain separate contracts. Use `Refs`, not `Closes`, because one Work may have multiple Work Units.

Run `python3 -m app.cli test pr` for validator tests. The `pr-template` Actions check runs on opened, edited, reopened, synchronized, and ready-for-review PRs. To enforce merge blocking, configure a main-branch protection rule or active ruleset requiring this check (GitHub Actions source) and disallow bypasses. A failed check alone does not prevent a privileged merge. Candidate tests run in a separate read-only job. The required check validates metadata using validators checked out at the base SHA, not candidate validators. The workflow definition remains PR-controlled; trusted review of workflow changes and a dedicated independent required check source are needed for adversarial enforcement. PR body edits trigger asynchronous revalidation.

The Work relationship is checked on PR events, not automatically on Issue changes. The check is a point-in-time result; revalidate the current Issue and PR before merge. A reference outside the Work section is not a membership declaration. Read-only Work cannot have a PR.

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

A code Work may close as completed when its Issue contract is valid, at least one declared Work Unit has merged into this repository's main with valid PR metadata, and no declared Work Unit remains open. A closed-unmerged PR is discarded, not delivered; its disposition must be considered during acceptance review. No new Work IDs. Every Work must use the canonical Issue format, including explicit Work type (Implementation or Read-only) and structured Scope. There is no legacy validation fallback. Closed historical records remain unchanged; correct a legacy Issue before reopening or using it in a new Work Unit.

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

Review digests are suggestions bound to the observed snapshot, not approvals. The controller does not infer semantic completion, and live data can change during discovery. Re-run the relevant preflight immediately before a write. Read-only Work with no result suggests PERFORM_READ_ONLY; with a result it suggests REVIEW_COMPLETION. A/B/C file modification authority remains a separate policy. API errors fail the run rather than producing a potentially incomplete execution plan.

### Read-only Work

No PR is required or allowed. Post a non-empty Issue comment beginning with `### Work result`, containing findings and verification evidence mapped to the acceptance criteria. The completion validator requires at least one such result, no associated PRs, and valid Issue metadata. Result comments are included in the reviewed snapshot. The Controller recommends `PERFORM_READ_ONLY` until results exist. Semantic acceptance is still a manual decision; a valid comment format is not proof of correctness. Historical untyped Issues must be migrated before editing or reopening. The event-based enforcement remains asynchronous.

## Change authority (A/B/C)

Owner-controlled policy lives in `.github/change-authority.json`. **The owner must create and maintain this file personally; the bootstrap PR deliberately does not create it.** Example for this personal repository:

```json
{
  "owner": "HyungseonSong-plasma",
  "A": [".github/change-authority.json", "validation/authority.py", ".github/workflows/grade-a.yml"],
  "B": [".github/", "validation/", "app/", "controller/"],
  "default": "C"
}
```

Exact paths and directory prefixes ending in / are supported. Highest grade wins, including both sides of a rename. Policy, authority evaluator and grade-a workflow are always A. Promotion/demotion is owner-only.

- A: ChatGPT may propose changes, but the user modifies the files personally. Automated merge is denied.
- B: ChatGPT must present the Work, intended change and affected B paths and obtain the user's approved Label **before modifying files**. After approval, normal implementation and merge procedures apply, just as for C. No separate GitHub reviewer or agent account is required.
- C: autonomous modification within the approved Work, subject to ordinary contracts.

```sh
python3 -m app.cli authority --repo OWNER/REPO --work NUMBER --path validation/pr.py README.md
python3 -m app.cli authority --repo OWNER/REPO --pr NUMBER --approved-change DIGEST
```

The path plan returns an approval digest for B. It binds repository, Work number/title/body, policy and the exact B path set. It is a scope identifier, **not proof of approval**; ChatGPT must not pass it as approved without a real user decision. If B paths, Work description, policy or the approved intent changes, request approval again. Unrelated C paths do not change the digest, but still must remain within Work scope.

The unified `merge --merge` path requires the trusted-base policy and complete file enumeration. For B, a matching request and owner approval label are required; the optional `--approved-change DIGEST` must match it. A is denied; C needs no B approval. Existing merge preflight and explicit merge authorization still apply. Missing/invalid policy fails closed. Pre-write enforcement remains an operating rule until an executor integrates the gate; raw connector writes and native GitHub UI/API are not intercepted. The user retains direct manual merge authority. The CLI verifies scope consistency, not user identity or semantic consent. The owner must install the policy before automated merge. Test: `python3 -m app.cli test authority`.

### GitHub file-rank ruleset

The `grade-a` commit status is published by the trusted-base `pull_request_target` workflow. It checks actual changed paths, including previous paths of renames, against the base policy. A or unavailable policy/data fails; B and C pass this particular gate. The workflow never executes candidate code. The status is posted explicitly to the PR head SHA because the target workflow itself runs against the base.

Bootstrap: leave file-rank Disabled, manually merge this bootstrap PR, personally create the policy file in another Work Unit PR, and manually merge that PR. After successful and failing disposable PR tests, require `grade-a` from GitHub Actions on main (strict up-to-date, no bypass), then activate file-rank. For future A edits, the owner temporarily disables file-rank, personally edits and merges, then restores it. Keep pr_merge_restriction active throughout.

This blocks main merges, not feature branch edits. Commit status source pinning reduces spoofing by other integrations but does not establish an isolated security principal for all GitHub Actions workflows. Same-repository writers may modify other workflows to publish statuses, and trusted helper changes still require review. No claim of adversarial-proof access control is made.

### Label-based B-rank approval

The former comment-only approval format is historical evidence, not a valid authorization under this contract. Configure these three Issue labels: `B-rank: pending`, `B-rank: approved`, `B-rank: revoked`. They must be mutually exclusive. Before B writes, create a request from trusted main:

```sh
python3 -m app.cli approval --repo OWNER/REPO --work NUMBER --path PATH1 PATH2 --request
python3 -m app.cli approval --repo OWNER/REPO --work NUMBER --path PATH1 PATH2
python3 -m app.cli approval --repo OWNER/REPO --pr NUMBER
```

A request starts a new pending label epoch and then posts a `### B-rank Approval Request` comment with exact sorted B paths and digest. Owner clears the prior state before a new request; the agent never adds/removes approved or revoked. The owner replaces pending with approved after inspecting the request, or removes approved and adds revoked to cancel. GitHub Issue events supply actor and timestamp. Only a single unedited request strictly after the latest pending-label addition and a subsequent owner-attributed approved-label addition are valid. Ambiguous timestamps, conflicting labels, missing/edited requests, scope or digest mismatch and errors fail closed as HUMAN_REVIEW. No fallback to older requests. C-only PRs need no B approval. Reapproval requires a fresh pending epoch after clearing previous labels.

The trusted-base grade-b check runs on PR events; Issue label, edit, close/reopen and comment events refresh linked open PRs. The merge CLI rechecks current evidence, independent of status. GitHub events are asynchronous and status refresh can race, be delayed or fail; no atomic merge invalidation or immutable audit log is claimed. Same-account agent access cannot cryptographically distinguish owner activity, so agent approval writes remain prohibited by operating contract. The controller isolates B approval exceptions to the affected Work (WAIT_B_APPROVAL, STOP_B_WORK, HUMAN_REVIEW); global discovery failure still stops discovery. The bounded Executor v0.1 enforces pre-write approval on its own publisher path; other writers are not intercepted.

Require grade-b in a main ruleset only after owner label transition tests. Tests: `python3 -m app.cli test approval` and `python3 -m app.cli test controller`.

## Controller Executor v0.1

The Decision Engine recommends actions; a caller prepares a JSON file-change plan for a new Work Unit (PLAN_WORK only). Review semantic scope before execution. Example:

```json
{"title":"Update docs","body":"### Work\n\nRefs #123\n\n### Changes\n\nUpdate docs\n\n### Verification\n\nReview text","files":[{"path":"README.md","content":"New full text\n"}]}
```

```sh
python3 -m app.cli executor --repo OWNER/REPO --work 123 --plan plan.json
python3 -m app.cli executor --repo OWNER/REPO --work 123 --plan plan.json --execute --reviewed-plan DIGEST
python3 -m app.cli test executor
```

Dry-run performs no writes. Explicit execution requires a fresh reviewed plan digest binding the Work, current base, policy, actual payload and approval evidence. It uses existing Work/PR, FileRank and approval validators; checks the immutable base tree once per invocation, rejects non-regular files, overlapping paths, no-op edits, invalid data, A paths and unapproved B scopes. UTF-8 text upserts and explicit null deletions only; GitHub API limits apply. Creates a commit against main, then a new non-force branch and canonical PR, rechecking only mutable context before branch and PR publication. No independent approval, storage or extra execution lock is introduced; the plan digest is a stale-plan identifier, not approval evidence. An existing active or delivered Work Unit is outside v0.1. No arbitrary shell command, main update, automatic merge, closure or schedule. Errors return HUMAN_REVIEW with phase and possible partial objects/ref; inspect state rather than retry blindly. The caller still judges semantic scope; raw connector writes, concurrent approval changes and external agent execution are not intercepted or atomic. Existing merge and completion contracts are unchanged.

## Controller workflow convention

Keep existing workflow filenames. Workflow YAML under `.github/workflows/` defines triggers, permissions, runner configuration and module invocation only for Controller workflows. Executor live CI is implemented in `controller/executor.py` and explicitly invoked by `python3 -m app.cli executor-live-ci` on trusted main via `executor-ci.yml` (workflow_dispatch). It creates and verifies one disposable C-only Work Unit and cleans up without merging; partial failures require human review. The repository must allow Actions to create pull requests. The test verifies publication, not downstream checks, which may be suppressed or require approval for token-created PRs. Ordinary `test executor` does not invoke live CI. `controller-run.yml` invokes `controller/operation.py` for read-only discovery.

## Controller Operation v0.1

`controller/operation.py` connects one Decision Engine discovery cycle to the existing Executor, with no new approval or persistent state. The default is reporting only. The trusted-main `controller-run.yml` supports manual dispatch and validated Issue comment requests, and is read-only; its job summary groups existing decisions by Action, Wait, and Human Review and links to Work or PR. A blocked Work does not suppress unrelated decisions. Global discovery failure stops dispatch. The summary is a view of the existing decision report, not another task database.

```sh
python3 -m app.cli operation --repo OWNER/REPO
python3 -m app.cli operation --repo OWNER/REPO --work NUMBER --plan plan.json
python3 -m app.cli operation --repo OWNER/REPO --work NUMBER --plan plan.json --execute --reviewed-plan DIGEST
python3 -m app.cli test operation
```

Explicit plan dispatch requires exactly one current Implementation PLAN_WORK decision for the target Work and no ambiguous PR reference to it. Executor rechecks its existing authority and reviewed-plan contract; no plan is generated automatically. The workflow does not accept a plan and cannot publish changes. Automatic plan generation, scheduled execution integration, existing PR modifications, automatic merge and closure are not implemented. No automatic retry after failure. A future Work may enable an hourly schedule or a separately authorized execution integration after the reporting cycle is verified.

### Scheduled Agent read-only request

On an open canonical Work Issue, the FileRank policy owner (including the connected Agent acting as that account) can create a comment consisting exactly of:

```text
/controller run
```

The trusted-main workflow listens only to created issue comments; non-command comments and PR comments skip the operation job. The CLI rechecks the immutable checkout against current main, owner identity, current Issue validity and current comment body, author, membership and timestamps before calling the existing reporting cycle. The comment cannot supply a plan, command arguments or a shell command. Job permissions are read-only, and the workflow does not write back to Issues.

Find Controller run workflow runs with event `issue_comment`, then inspect the operation job JSON or summary. Match the `request` fields (Work, comment ID, run ID, attempt and base SHA) to the submitted comment, and require a successful job with REPORTED and execution null before using its discovery. The report still covers all Works; the Agent processes only its own Work. Do not use historical or unmatched results. Non-command events may appear as skipped workflow runs.

Owner login does not distinguish a human from the Agent using the same account. This is not a write authorization or an Executor interface. A queued run can fail if main advances, a request is changed or an Issue closes; a concurrent update after rechecking remains possible. GitHub concurrency serializes running jobs but can replace pending jobs, and reruns can repeat a read-only request. No exactly-once delivery, persisted request registry or automatic retry is provided. Inspect missing or failed runs rather than blindly reposting.
