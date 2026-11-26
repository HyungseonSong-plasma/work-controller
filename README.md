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

Keep existing workflow filenames. Workflow YAML under `.github/workflows/` defines triggers, permissions, runner configuration and module invocation only for Controller workflows. Executor live CI is implemented in `tests/live_executor.py` and explicitly invoked by `python3 -m app.cli executor-live-ci` on trusted main via `executor-ci.yml` (workflow_dispatch). It creates and verifies one disposable C-only Work Unit and cleans up without merging; partial failures require human review. The repository must allow Actions to create pull requests. The test verifies publication, not downstream checks, which may be suppressed or require approval for token-created PRs. Ordinary `test executor` does not invoke live CI. `controller-run.yml` invokes `controller/operation.py` for read-only discovery.

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

## JSON-driven contract tests (schema v1)

Issue and PR structural tests live in `tests/cases/issue.json` and `tests/cases/pr.json`. Each case is registered as a separate unittest method in list order by the shared wrapper. Add a case without editing Python when it uses an existing target and assertion:

```json
{
  "version": 1,
  "target": "validation.issue",
  "cases": [
    {
      "name": "empty_body",
      "function": "validate",
      "args": [""],
      "assert": {"op": "false"}
    }
  ]
}
```

Each case requires name, function, args, assert; kwargs is optional. Assertions are exactly one of equal (value), true, false or raises (exception: ValueError). True/false retain unittest truthiness semantics. Only the explicit pure-function allowlist is callable; no arbitrary import, expression, mock, command, or network/write operations in this schema. Unknown fields, duplicate keys/names, empty suites, invalid inputs to the schema and unknown targets fail before execution. Individual assertion failures fail the CLI. The wrapper itself is tested by the issue and full suites.

Migration mapping: Issue (38 individual assertions from six existing methods): valid 2, empty_or_missing 7, placeholders 14, duplicate_or_reordered 2, work_type 6, malformed_scope 7. PR (30 individual assertions from eight existing methods): valid 2, empty_or_missing 5, work_reference 5, placeholders 3, duplicate_or_reordered 2, work_number 4, work_target 8, read_only_target 1. Original truthiness, equality, None and exception assertions and literal input variations are preserved. At that migration stage, other validation and Controller unit tests remained in their existing modules; the write-capable Executor Live CI remains explicitly invoked and is never loaded by the JSON runner.

```sh
python3 -m app.cli test issue
python3 -m app.cli test pr
python3 -m app.cli test
```

### Hybrid suites: JSON + Python scenarios

Approval, Completion and Merge use the same schema v1 and static allowlist. The runner combines each JSON suite with its explicitly registered unittest class in `tests/scenarios/`. Mock targets, call sequencing, subprocess fixtures, digest comparisons and multi-assertion interactions stay in Python. JSON carries literal inputs and expected state or truthiness; the narrow adapters return the real evaluate state or assessment blockers without changing their meaning. Approval request fixtures contain an independently computed fixed scope digest for the original inputs. No new mock language or dynamically selected patch targets.

| Original test group | JSON cases | Retained Python methods |
|---|---:|---|
| Approval: approval_states, abnormal, c_only | 6 + 10 | c_only (including no read_state call) |
| Completion: valid_and_discarded, blockers, membership, approval_and_stale, guard, read_only_completion | 7 blockers + 4 read-only negatives | valid_and_discarded, membership (exact set equality), approval_and_stale, guard, read_only_completion (3 positive assertions and changed digest inequality) |
| Merge: valid_snapshot, invalid_metadata, review_snapshot_changes, verify_rejects_stale_or_failed | 6 invalid metadata | valid_snapshot, review_snapshot_changes, verify_rejects_stale_or_failed |

All original test variations are retained; complex Python tests keep original assertions and patch actual production module attributes. At that stage, Authority and Controller tests remained in their original modules. Targeted commands: `python3 -m app.cli test approval`, `python3 -m app.cli test completion`, `python3 -m app.cli test merge`. Full CI also exercises these selectors through wrapper regression tests.

### Controller unit tests

The controller, executor and operation selectors now load both literal JSON inputs and explicit Python scenarios using the same schema v1. The fixed Decision Engine adapters provide in-memory check/detail readers; the Operation identity adapter always mocks the production API and policy reader. JSON cannot choose mocks, call sequences, or write-capable targets. Executor JSON only calls pure validate_plan and tree_entries; the unchanged-blob fixture uses the independently calculated Git SHA-1 for the string "same".

| Original group | JSON cases | Python scenarios retained |
|---|---:|---|
| Controller: plan_and_completion, pr_states, invalid_and_orphan | 4 + 7 + 4 | api_failure, read_only_decisions, approval_isolation, removed_approval_without_active_pr |
| Executor: validation | 5 invalid paths + 5 tree/reference variants | authority_and_no_writes, publish_and_stale, recheck_revocation_and_api_failure, tree_is_read_once_and_context_rechecked |
| CommentRequest: valid_identity, invalid_event, stale_current_state | 1 + 9 + 8 | fail_closed_and_read_only_dispatch |
| Operation | 0 | report_only, dispatch_and_dry_run, blocked_and_ambiguous, failures_and_explicit_arguments |

Every original assertion and negative variant is retained, including mock counts, arguments, no-write checks, set/identity/digest assertions in unchanged suites, and both Operation and CommentRequest classes. Targeted selectors: `python3 -m app.cli test controller`, `python3 -m app.cli test executor`, `python3 -m app.cli test operation`. Full CI exercises them via wrapper regressions. At that migration stage, Executor live CI remained in its original location and was not invoked by the unit runner. Authority tests remain owner-protected in validation/authority.py.

### Explicit live integration test

The original `live_ci()` function now resides in `tests/live_executor.py`, with its body unchanged. `python3 -m app.cli executor-live-ci` lazily imports this entrypoint; the existing `executor-ci.yml` remains unchanged. It runs only via manual workflow_dispatch on current trusted main, with the original C-grade disposable Work Unit, reviewed publication and cleanup safeguards. It is absent from the JSON allowlist and normal unit suite discovery. Unit regressions mock dispatch, reject invalid event/ref and stale checkout, and verify all unit selectors exclude live execution. These tests are not evidence of a real publication; perform and verify a separate manual main run after merge before claiming live integration success.


## Agent review bridge

The trusted-main `review-preflight.yml` accepts new owner-account comments on canonical open Work Issues with read-only permissions. Exactly one line (replace uppercase placeholders):

- `/review merge snapshot PR`
- `/review merge verify PR DIGEST HEAD`
- `/review completion snapshot`
- `/review completion verify DIGEST`

Merge PRs must declare the requesting Work and use the trusted checkout as base. Snapshot is not verification. Verification reuses the full Merge CLI authority and required-check path, or the existing Completion contract. Match successful job JSON to repository, Work, PR where applicable, command, comment ID, run ID, attempt, base SHA, digest and expected head; require VERIFIED, execution null and no blockers. Missing, unmatched, blocked or failed reports cannot authorize execution.

The owner manually dispatches `review-execute.yml` on main: action merge or close, Work number, reviewed digest, PR and head for merge only, explicit acceptance evidence for close only. Separate least-privilege jobs revalidate current trusted main, actor, scope and snapshot and use the existing merge/closure contracts. Reruns are refused; inspect actual state before retry. Agent direct connector merge/close fallback and manual dispatch on behalf of the owner are prohibited. The owner retains direct manual authority. The optional delegated route below is separate from manual dispatch. No autonomous merge, close or approval.

GITHUB_TOKEN may suppress downstream events; repository settings or permissions may reject merges, particularly workflow changes. Live write execution needs a separately authorized disposable test. Same-account identity is not independent human attestation; native writers are not intercepted, and metadata or approval can change in the residual race before a write.


### Opt-in interactive Agent execution

Default off. An owner who accepts the same-account trust model may create the repository Actions variable `AGENT_REVIEW_DELEGATION` with value `v1` in Settings → Secrets and variables → Actions → Variables. Remove or change it to disable future delegated requests; cancel already running jobs separately. Agents must never change the variable. This setting is not proof of individual chat approval: an owner-account connector can post the same comment as a human. It does not prevent other privileged GitHub writers from modifying history or settings.

After the interactive owner approves the exact action, Work, PR/head and reviewed digest or closure evidence in chat, the Agent may post exactly one new command on the requesting open Work, referencing the successful VERIFIED preflight run and job from current main:

- `/execute-review merge RUN JOB PR DIGEST HEAD`
- For closure, exactly two lines: `/execute-review close RUN JOB DIGEST` followed by `Evidence: ACCEPTANCE EVIDENCE` (one printable nonblank line, maximum 500 characters).

No comment is itself independent human attestation. The workflow validates the owner-attributed current comment, initial run attempt, action/job match, same trusted main and exact verified read-only run/job/artifact/Work/PR/snapshot/current review comment. Evidence expires 30 minutes after the preflight job completed. Merge waits for fresh grade-b refresh readiness; both actions then invoke the existing execution contract. The two write jobs retain separate permissions (close additionally reads Actions evidence) and share execution serialization with manual dispatch. A prior eligible delegated run since verification, ambiguous run history, stale or missing evidence, edited request, policy failure or failed write blocks reuse; obtain fresh verification and explicit approval after inspecting actual state. The history guard is intentionally conservative across Works; deleted or altered GitHub history is not a durable anti-replay boundary against privileged writers. No scheduled delegated execution, automatic write retry or raw connector merge/close.

The Agent correlates final MERGED/CLOSED output with the execution comment, run/attempt, main, owner attribution, referenced review run/job and target digest, then reads back actual repository state. Live artifact retrieval and execution require verification after merge and separate owner activation; passing tests alone does not demonstrate them.

#### Structured review evidence

Successful preflight writes its JSON to the fixed `review.json` file while retaining the human-readable log; failed CLI runs do not publish an artifact. The pinned upload action stores `review-evidence-RUN-ATTEMPT` for one day. Delegated execution still requires VERIFIED, a successful current-main run and job, and the 30-minute evidence deadline. It selects exactly one matching, unexpired artifact from that run, checks origin and SHA-256 archive digest, then reads only a bounded single JSON file from the ZIP without extracting paths. It independently rechecks every prior report and comment binding. The GitHub token is sent only to the fixed GitHub API endpoint; storage download uses the GitHub-provided HTTPS redirect without that token. Missing or malformed evidence, unexpected redirects, download errors or digest mismatches stop before writes. No log or caller-supplied report fallback. Artifact retention is not an approval store. The original first delegated close (run 45897462387) failed at gh job-log retrieval, with exit status 1 and no captured HTTP detail; the underlying HTTP cause remains unknown. Stage-specific errors omit signed URLs, token values and raw responses.

#### Live delegated execution evidence (2026-11-26)

After owner activation, the artifact-based Close path completed the following individually approved requests on trusted main `8c27dc92714f6dfd4427b5046f3659be7d6de264`. Each initial attempt returned matching `CLOSED` JSON and the Issue was independently read back as closed/completed.

| Work | VERIFIED preflight run | Execution job |
|---|---|---|
| #92 | [45899819592](https://github.com/HyungseonSong-plasma/work-controller/actions/runs/45899819592) | [140182859934](https://github.com/HyungseonSong-plasma/work-controller/actions/runs/45899905898/job/140182859934) |
| #98 | [45900102397](https://github.com/HyungseonSong-plasma/work-controller/actions/runs/45900102397) | [140183957241](https://github.com/HyungseonSong-plasma/work-controller/actions/runs/45900238107/job/140183957241) |
| #94 | [45900407544](https://github.com/HyungseonSong-plasma/work-controller/actions/runs/45900407544) | [140184922044](https://github.com/HyungseonSong-plasma/work-controller/actions/runs/45900533083/job/140184922044) |
| #89 | [45900875023](https://github.com/HyungseonSong-plasma/work-controller/actions/runs/45900875023) | [140186454243](https://github.com/HyungseonSong-plasma/work-controller/actions/runs/45900999016/job/140186454243) |

These executions exercised the structured evidence transport, current-run and artifact validation, and the existing Completion contract. They supersede the failed log-retrieval attempt as positive evidence, not as a retry of that request. They do not prove independent human identity, server-side enforcement against other writers, or automatic downstream event delivery. Delegated Merge requires its own matching live execution evidence; a successful C-rank Merge would not establish live delegated B-rank Merge. Use Work #100 and #96 for the separately reviewed Merge evidence rather than assuming success from this document or CI.


### Interactive cleanup delegation

On an open Implementation Work, request `/cleanup plan` and review the successful `delegated-plan` job's exact current-main manifest. Owner opt-in: set the Actions variable `AGENT_CLEANUP_DELEGATION=v1` (default off). After explicit interactive approval of the exact kind, digest, base and evidence, post one `/execute-cleanup RUN JOB KIND DIGEST BASE` comment. KIND is branches or runs. Match the execution trace and actual readback. No scheduled delegation or raw deletion; inspect partial failures before fresh review. Same-account attribution is not independent human approval. See the trusted cleanup workflow and controller implementation for validation; automatic lifecycle activation remains separate.
