"""Bounded, trusted-main cleanup of terminal Work Units and disposable CI runs."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote, urlencode

from controller import operation, review
from validation import authority, completion, issue, pr
from validation.model import REPO, SHA, api, digest

WORKFLOW = ".github/workflows/post-work-cleanup.yml"
REVIEW = ".github/workflows/review-execute.yml"
DISPOSABLE = {".github/workflows/pr-contract.yml", ".github/workflows/issue-contract.yml"}
TERMINAL = {"cancelled", "skipped"}
ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")
LIMIT = 20
AGE = dt.timedelta(hours=1)
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
HEX = re.compile(r"[0-9a-f]{64}")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def timestamp(value):
    if not isinstance(value, str) or not TIME.fullmatch(value):
        raise ValueError("Expected UTC timestamp YYYY-MM-DDTHH:MM:SSZ")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def number(value):
    if type(value) is not int or value < 1:
        raise ValueError("Expected positive numeric identity")
    return value


def ref_valid(name):
    return (isinstance(name, str) and REF.fullmatch(name) is not None
            and ".." not in name and "//" not in name and not name.endswith(("/", "."))
            and all(not p.startswith(".") and not p.endswith(".lock") for p in name.split("/")))


def local(side, repo_id):
    return isinstance(side, dict) and (side.get("repo") or {}).get("id") == repo_id


class DuplicateEnumeration(ValueError):
    """A changing read-only collection may be retried, not deduplicated."""


def unique(items, key):
    if not isinstance(items, list) or any(not isinstance(x, dict) or key not in x for x in items):
        raise ValueError("Invalid enumeration")
    values = [x[key] for x in items]
    if key == "name":
        if any(not isinstance(v, str) or not v for v in values):
            raise ValueError("Invalid enumeration identity: name")
    elif key in {"id", "number"}:
        if any(type(v) is not int or v < 1 for v in values):
            raise ValueError(f"Invalid enumeration identity: {key}")
    else:
        raise ValueError("Unsupported enumeration identity key")
    seen, duplicates = set(), set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        sample = [str(v)[:160] if key == "name" else v for v in sorted(duplicates)[:10]]
        raise DuplicateEnumeration(
            f"Duplicate identities in enumeration: key={key}, duplicate_count={len(duplicates)}, "
            f"duplicate_ids={sample!r}, collected={len(items)}")
    return items


def retry_read(reason, attempt):
    message = f"{reason}, attempt={attempt}/3"
    if attempt == 3:
        raise ValueError(message)
    print(message + "; restarting read-only enumeration", file=sys.stderr, flush=True)
    time.sleep(attempt)


def listed(path, key):
    for attempt in range(1, 4):
        pages = api(path, "--paginate", "--slurp")
        if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
            raise ValueError(f"Incomplete array pagination: {path}")
        try:
            return unique([x for page in pages for x in page], key)
        except DuplicateEnumeration as exc:
            retry_read(f"{exc}; endpoint={path}, page_counts={[len(p) for p in pages[:20]]}, "
                       f"pages={len(pages)}", attempt)


def counted(path, field):
    """Restart inconsistent counted GETs; never use partial or combined results."""
    for attempt in range(1, 4):
        pages = api(path, "--paginate", "--slurp")
        if not isinstance(pages, list) or not pages or any(
                not isinstance(p, dict) or type(p.get("total_count")) is not int
                or p["total_count"] < 0 or not isinstance(p.get(field), list)
                for p in pages):
            raise ValueError(f"Incomplete counted pagination: {path}, field={field}")
        counts = [(p["total_count"], len(p[field])) for p in pages[:20]]
        try:
            items = unique([x for p in pages for x in p[field]], "id")
        except DuplicateEnumeration as exc:
            retry_read(f"{exc}; endpoint={path}, field={field}, "
                       f"page (total, fetched) counts={counts}, pages={len(pages)}", attempt)
            continue
        if all(p["total_count"] == len(items) for p in pages):
            return items
        retry_read(f"Counted enumeration changed or truncated: {path}, field={field}, "
                   f"page (total, fetched) counts={counts}, collected={len(items)}, "
                   f"pages={len(pages)}", attempt)


def terminal_runs(ctx):
    """Restart the entire status group if a run changes status between queries."""
    endpoint = ctx["root"] + "/actions/runs"
    for attempt in range(1, 4):
        groups = {state: counted(endpoint + "?" + urlencode({"status": state, "per_page": 100}),
                                 "workflow_runs") for state in sorted(TERMINAL)}
        counts = {state: len(rows) for state, rows in groups.items()}
        try:
            items = unique([x for rows in groups.values() for x in rows], "id")
        except DuplicateEnumeration as exc:
            retry_read(f"{exc}; endpoint={endpoint}, terminal status counts={counts}", attempt)
            continue
        return sorted(items, key=lambda x: x["id"])


def run_is_disposable(run, jobs, artifacts):
    """A cancelled/skipped top-level result is insufficient."""
    return (run.get("path") in DISPOSABLE and run.get("event") == "pull_request"
            and run.get("status") == "completed" and run.get("conclusion") in TERMINAL
            and type(run.get("run_attempt")) is int and run["run_attempt"] == 1
            and isinstance(jobs, list) and bool(jobs)
            and all(j.get("status") == "completed" and j.get("conclusion") in TERMINAL
                    and j.get("run_attempt") == 1 for j in jobs)
            and artifacts == [])


def branch_is_unchanged(branch, expected, default, open_refs, rules):
    name = branch.get("name")
    return (ref_valid(name) and name not in ("main", default) and name not in open_refs
            and branch.get("protected") is False and branch.get("commit", {}).get("sha") == expected
            and isinstance(expected, str) and SHA.fullmatch(expected) is not None
            and rules == [])


def references_evidence(texts, identities):
    pattern = re.compile(r"(?<![0-9])(?:" + "|".join(str(number(i)) for i in identities) + r")(?![0-9])")
    return any(pattern.search(value or "") for value in texts)


def associated(run, relation, pull, repo_id):
    head, base = relation.get("head") or {}, relation.get("base") or {}
    return (relation.get("number") == pull["number"] and relation.get("id") == pull["id"]
            and local(head, repo_id) and local(base, repo_id)
            and head.get("ref") == pull["head"]["ref"] and base.get("ref") == "main"
            and head.get("sha") == pull["head"]["sha"] == run.get("head_sha")
            and run.get("head_branch") == head["ref"])


def current_main(root):
    return api(root + "/branches/main")["commit"]["sha"]


def context(repo, kind, approved, comment_mode=False):
    if (not REPO.fullmatch(repo) or kind not in {"branches", "runs"}
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY") != repo
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_WORKFLOW_REF") != repo + "/" + WORKFLOW + "@refs/heads/main"):
        raise ValueError("Cleanup requires its trusted-main Actions workflow")
    root = "repos/" + repo
    meta = api(root)
    if meta.get("full_name", "").casefold() != repo.casefold() or meta.get("default_branch") != "main":
        raise ValueError("Unexpected repository or default branch")
    repo_id = number(meta["id"])
    sha = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    if not SHA.fullmatch(sha) or current_main(root) != sha:
        raise ValueError("Checkout is not current main")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
        event = json.load(stream)
    if event.get("repository", {}).get("id") != repo_id:
        raise ValueError("Event repository mismatch")
    name = os.environ.get("GITHUB_EVENT_NAME")
    owner = authority.read_policy(repo, sha)["owner"].casefold()
    since = os.environ.get("POST_WORK_CLEANUP_SINCE", "")
    cutoff = timestamp(since) if since else None
    if name == "workflow_dispatch":
        cutoff = None
        since = ""
        if approved:
            inputs = event.get("inputs") or {}
            if (inputs.get("branch_digest" if kind == "branches" else "run_digest") != approved
                    or any(os.environ.get(k, "").casefold() != owner
                           for k in ("GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR"))
                    or event.get("sender", {}).get("login", "").casefold() != owner):
                raise ValueError("Historical deletion requires matching owner dispatch digest")
    elif name == "issue_comment" and comment_mode:
        cutoff = None
        since = ""
    elif name == "pull_request_target":
        p = event.get("pull_request") or {}
        if (event.get("action") != "closed" or p.get("merged") is not True
                or not local(p.get("base"), repo_id) or p["base"].get("ref") != "main"):
            raise ValueError("Not a main-target merged PR")
    elif name == "issues":
        w = event.get("issue") or {}
        if (event.get("action") != "closed" or "pull_request" in w
                or w.get("state_reason") != "completed"):
            raise ValueError("Not a completed Work event")
    elif name == "workflow_run":
        x = event.get("workflow_run") or {}
        live = api(root + "/actions/runs/" + str(number(x["id"])))
        if (event.get("action") != "completed" or live.get("id") != x["id"]
                or live.get("path") != REVIEW or live.get("head_branch") != "main"
                or (live.get("head_repository") or {}).get("id") != repo_id
                or (live.get("repository") or {}).get("id") != repo_id
                or live.get("status") != "completed" or live.get("conclusion") != "success"
                or live.get("event") not in {"workflow_dispatch", "issue_comment"}):
            raise ValueError("Not a successful trusted Review execute run")
    elif name != "schedule":
        raise ValueError("Unsupported cleanup event")
    if approved:
        if not HEX.fullmatch(approved) or os.environ.get("GITHUB_RUN_ATTEMPT") != "1":
            raise ValueError("Apply requires exact digest and initial attempt")
        if name != "workflow_dispatch" and cutoff is None:
            raise ValueError("Automatic cleanup is disabled")
    return {"repo": repo, "root": root, "id": repo_id, "base": sha, "default": "main",
            "owner": owner, "since": since, "automatic": name != "workflow_dispatch" and cutoff is not None,
            "run": number(int(os.environ["GITHUB_RUN_ID"]))}


def catalog(ctx):
    root = ctx["root"]
    pulls = listed(root + "/pulls?state=all&per_page=100", "number")
    issues = listed(root + "/issues?state=all&per_page=100", "number")
    branches = listed(root + "/branches?per_page=100", "name")
    open_pulls = [p for p in pulls if p["state"] == "open"]
    open_refs = {p[side]["ref"] for p in open_pulls for side in ("head", "base")
                 if local(p.get(side), ctx["id"])}
    open_heads = {p["head"]["sha"] for p in open_pulls}
    return {"pulls": pulls, "issues": {w["number"]: w for w in issues},
            "branches": {x["name"]: x for x in branches}, "open_refs": open_refs,
            "open_heads": open_heads}


def terminal(ctx, cat, pull):
    """Return the verified terminal relation, or an explicit exclusion."""
    head, base = pull.get("head") or {}, pull.get("base") or {}
    if (pull.get("state") != "closed" or not local(head, ctx["id"])
            or not local(base, ctx["id"]) or base.get("ref") != "main"
            or not ref_valid(head.get("ref")) or not SHA.fullmatch(head.get("sha", ""))
            or head["ref"] in cat["open_refs"]):
        return None, "Not a closed local main Work Unit, or open ref dependency"
    try:
        work = pr.work_number(pull.get("body"))
    except ValueError:
        return None, "Invalid Work Unit contract"
    w = cat["issues"].get(work)
    if (not w or "pull_request" in w or not issue.validate(w.get("body"))
            or issue.work_type(w.get("body")) != "Implementation"):
        return None, "Missing or invalid Implementation Work"
    merged = bool(pull.get("merged_at"))
    if w.get("state") == "closed":
        if w.get("state_reason") != "completed":
            return None, "Work not completed"
        _, blockers = completion.assessment(ctx["repo"], work, w, cat["pulls"])
        if blockers:
            return None, "Work completion invalid: " + "; ".join(blockers)
    elif w.get("state") != "open" or not merged:
        return None, "Discarded unit requires completed Work"
    if merged:
        merge = pull.get("merge_commit_sha")
        if not isinstance(merge, str) or not SHA.fullmatch(merge):
            return None, "Missing merge commit"
        compare = api(ctx["root"] + "/compare/" + merge + "..." + ctx["base"])
        if compare.get("status") not in {"ahead", "identical"} or compare.get("behind_by") != 0:
            return None, "Merge commit not reachable from trusted main"
        when = pull["merged_at"]
        disposition = "merged"
    else:
        events = listed(ctx["root"] + f"/issues/{pull['number']}/events?per_page=100", "id")
        closes = [e for e in events if e.get("event") == "closed"]
        if (not closes or closes[-1].get("created_at") != pull.get("closed_at")
                or (closes[-1].get("actor") or {}).get("login", "").casefold() != ctx["owner"]):
            return None, "Discard requires owner-attributed PR closure"
        when = max(pull["closed_at"], w["closed_at"], key=timestamp)
        disposition = "discarded"
    if ctx["since"] and timestamp(when) < timestamp(ctx["since"]):
        return None, "Historical target predates automatic activation"
    return {"pr": pull["number"], "work": work, "branch": head["ref"], "head": head["sha"],
            "terminal_at": when, "disposition": disposition, "merge_commit": pull.get("merge_commit_sha") if merged else None,
            "work_state": w["state"], "pr_closed_at": pull["closed_at"]}, None


def active_refs(ctx):
    result = set()
    for status in ACTIVE:
        runs = counted(ctx["root"] + "/actions/runs?" + urlencode({"status": status, "per_page": 100}), "workflow_runs")
        result.update(r.get("head_branch") for r in runs)
    return result


def branch_target(ctx, cat, branch, active):
    name = branch["name"]
    if (name == ctx["default"] or branch.get("protected") is not False or name in active
            or name in cat["open_refs"] or not ref_valid(name)):
        return None, "Default/protected/open/busy/invalid branch"
    matches = [p for p in cat["pulls"] if local(p.get("head"), ctx["id"]) and p["head"].get("ref") == name]
    if len(matches) != 1:
        return None, "Missing or reused PR branch identity"
    p = matches[0]
    if branch.get("commit", {}).get("sha") != p["head"].get("sha"):
        return None, "Branch head changed"
    relation, reason = terminal(ctx, cat, p)
    if reason:
        return None, reason
    rules = api(ctx["root"] + "/rules/branches/" + quote(name, safe=""))
    if not branch_is_unchanged(branch, relation["head"], ctx["default"], cat["open_refs"], rules):
        return None, "Ref changed, protected or subject to active rules"
    return relation, None


def run_target(ctx, cat, run):
    if (run.get("path") not in DISPOSABLE or run.get("event") != "pull_request"
            or run.get("status") != "completed" or run.get("conclusion") not in TERMINAL
            or run.get("run_attempt") != 1 or (run.get("repository") or {}).get("id") != ctx["id"]
            or (run.get("head_repository") or {}).get("id") != ctx["id"]
            or run.get("head_sha") in cat["open_heads"] or run.get("head_branch") in cat["open_refs"]
            or run.get("id") == ctx["run"]):
        return None, "Not disposable candidate CI or has open dependency"
    relations = run.get("pull_requests")
    if relations == []:
        suite = api(ctx["root"] + "/check-suites/" + str(number(run["check_suite_id"])))
        if (suite.get("id") != run["check_suite_id"] or suite.get("head_sha") != run.get("head_sha")
                or suite.get("head_branch") != run.get("head_branch")):
            return None, "Check suite identity mismatch"
        relations = suite.get("pull_requests")
    if not isinstance(relations, list) or len(relations) != 1:
        return None, "No unique positive PR association"
    p = next((p for p in cat["pulls"] if p["number"] == relations[0].get("number")), None)
    if not p or not associated(run, relations[0], p, ctx["id"]):
        return None, "PR association differs from exact local head"
    relation, reason = terminal(ctx, cat, p)
    if reason:
        return None, reason
    branch = cat["branches"].get(relation["branch"])
    if branch and branch.get("commit", {}).get("sha") != relation["head"]:
        return None, "Branch reused"
    if (dt.datetime.now(dt.timezone.utc) - max(timestamp(run["updated_at"]),
                                               timestamp(relation["terminal_at"])) < AGE):
        return None, "Run or terminal PR is within one-hour grace period"
    ident = number(run["id"])
    root = ctx["root"] + f"/actions/runs/{ident}"
    jobs = counted(root + "/jobs?filter=all&per_page=100", "jobs")
    artifacts = counted(root + "/artifacts?per_page=100", "artifacts")
    if not run_is_disposable(run, jobs, artifacts):
        return None, "Significant job/attempt/artifact evidence"
    ids = [ident, *(j["id"] for j in jobs)]
    # Review/acceptance references may be on a different Work. Search repository
    # issue/PR bodies, conversation and inline review comments, plus related PR reviews.
    comments = [*listed(ctx["root"] + "/issues/comments?per_page=100", "id"),
                *listed(ctx["root"] + "/pulls/comments?per_page=100", "id"),
                *listed(ctx["root"] + f"/pulls/{p['number']}/reviews?per_page=100", "id")]
    statuses = listed(ctx["root"] + f"/commits/{relation['head']}/statuses?per_page=100", "id")
    texts = [*(x.get("body") for x in cat["issues"].values()),
             *(x.get("body") for x in cat["pulls"]),
             *(x.get("body") for x in comments), *(x.get("target_url") for x in statuses)]
    if references_evidence(texts, ids):
        return None, "Work/PR review or status references run evidence"
    return dict(relation, run=ident, path=run["path"], updated_at=run["updated_at"],
                attempt=run["run_attempt"], conclusion=run["conclusion"], jobs=ids[1:]), None


def manifest(ctx, kind):
    cat = catalog(ctx)
    if kind == "branches":
        active = active_refs(ctx)
        items = sorted(cat["branches"].values(), key=lambda x: x["name"])
        evaluate = lambda x: branch_target(ctx, cat, x, active)
        key = "name"
    else:
        items = terminal_runs(ctx)
        evaluate = lambda x: run_target(ctx, cat, x)
        key = "id"
    eligible, excluded = [], []
    for item in items:
        value, reason = evaluate(item)
        if value is None:
            excluded.append({"target": item[key], "reason": reason})
        else:
            eligible.append(value)
    if current_main(ctx["root"]) != ctx["base"]:
        raise ValueError("Main changed during inventory")
    plan = {"repository": ctx["repo"], "base": ctx["base"], "kind": kind,
            "since": ctx["since"], "targets": eligible[:LIMIT]}
    return {"state": "PLANNED", "mode": "read-only", "automatic": ctx["automatic"],
            "manifest": plan, "digest": digest(plan), "eligible_count": len(eligible),
            "remaining": eligible[LIMIT:], "excluded": excluded}


def auto_history(ctx):
    """A failed earlier automatic write may be partial; do not silently retry it."""
    if not ctx["automatic"]:
        return
    history = counted(ctx["root"] + "/actions/workflows/post-work-cleanup.yml/runs?" +
                      urlencode({"created": ">=" + ctx["since"], "per_page": 100}), "workflow_runs")
    current = [r for r in history if r["id"] == ctx["run"]]
    if len(current) != 1:
        raise ValueError("Current cleanup run missing from activation history")
    start = timestamp(current[0]["created_at"])
    for r in history:
        if r["id"] == ctx["run"] or timestamp(r["created_at"]) > start:
            continue
        if r.get("status") != "completed" or r.get("conclusion") not in {"success", "skipped"}:
            raise ValueError("Earlier cleanup unresolved or failed; owner review required")


def git_command(repo, *args):
    return subprocess.run(["git", "-c", "credential.helper=", "-c",
                           "credential.helper=!gh auth git-credential", *args],
                          check=True, capture_output=True, text=True)


def delete_branch(ctx, target):
    branch, sha = target["branch"], target["head"]
    if not ref_valid(branch) or branch == ctx["default"] or not SHA.fullmatch(sha):
        raise ValueError("Invalid deletion identity")
    ref = "refs/heads/" + branch
    url = "https://github.com/" + ctx["repo"] + ".git"
    git_command(ctx["repo"], "check-ref-format", ref)
    git_command(ctx["repo"], "push", "--force-with-lease=" + ref + ":" + sha, url, ":" + ref)
    remaining = git_command(ctx["repo"], "ls-remote", "--refs", url, ref).stdout.strip()
    if remaining:
        raise ValueError("Deleted branch still exists or was recreated")


def delete_run(ctx, target):
    path = ctx["root"] + "/actions/runs/" + str(number(target["run"]))
    command = ["gh", "api", "--hostname", "github.com", path]
    subprocess.run([*command, "-X", "DELETE"], check=True, capture_output=True, text=True)
    readback = subprocess.run(command, capture_output=True, text=True)
    if readback.returncode == 0 or "HTTP 404" not in readback.stderr:
        raise ValueError("Run deletion not confirmed by HTTP 404")
    if api(ctx["root"]).get("id") != ctx["id"]:
        raise ValueError("Repository readback failed")


def apply(ctx, kind, report, approved, guard=None):
    if report["digest"] != approved or digest(report["manifest"]) != approved:
        raise ValueError("Reviewed cleanup manifest changed")
    auto_history(ctx)
    deleted = []
    for target in report["manifest"]["targets"]:
        if current_main(ctx["root"]) != ctx["base"]:
            raise ValueError("Main changed; stop cleanup")
        cat = catalog(ctx)
        if kind == "branches":
            branch = cat["branches"].get(target["branch"])
            fresh, reason = branch_target(ctx, cat, branch, active_refs(ctx)) if branch else (None, "Ref absent")
        else:
            run = api(ctx["root"] + "/actions/runs/" + str(number(target["run"])))
            fresh, reason = run_target(ctx, cat, run)
        if fresh != target or reason or current_main(ctx["root"]) != ctx["base"]:
            raise ValueError("Reviewed target changed; stop cleanup: " + str(reason))
        if guard is not None:
            guard()
        print(json.dumps({"state": "DELETING", "target": target}), flush=True)
        if kind == "branches":
            delete_branch(ctx, target)
        else:
            delete_run(ctx, target)
        deleted.append(target)
        print(json.dumps({"state": "DELETED", "target": target}), flush=True)
    return {"state": "CLEANED", "digest": approved, "deleted": deleted}


def run(repo, kind, approved=None):
    ctx = context(repo, kind, approved)
    report = manifest(ctx, kind)
    if not approved:
        return report
    return apply(ctx, kind, report, approved)


PLAN_COMMAND = "/cleanup plan"
EXEC_COMMAND = re.compile(r"/execute-cleanup ([1-9][0-9]*) ([1-9][0-9]*) (branches|runs) ([0-9a-f]{64}) ([0-9a-f]{40})")
DELEGATED_JOB = {"branches": "delegated-branches", "runs": "delegated-runs"}
APPLY_JOBS = {"branches", "runs", *DELEGATED_JOB.values()}


def parse_delegation(command):
    match = EXEC_COMMAND.fullmatch(command) if isinstance(command, str) and len(command) <= 256 else None
    if not match:
        raise ValueError("Invalid cleanup execution command")
    return int(match[1]), int(match[2]), match[3], match[4], match[5]


def comment_context(repo, command, job):
    if os.environ.get("GITHUB_JOB") != job:
        raise ValueError("Cleanup request/job mismatch")
    event, sha, actor, run_id, attempt = operation.event_context(repo, "issue_comment")
    if attempt != 1 or os.environ.get("GITHUB_TRIGGERING_ACTOR", "").casefold() != actor.casefold():
        raise ValueError("Cleanup requires initial owner-attributed request")
    identity = operation.request_identity(repo, event, sha, actor, run_id, attempt, command=command)
    w = api(f"repos/{repo}/issues/{identity['work']}")
    if w.get("state") != "open" or not issue.validate(w.get("body")) or issue.work_type(w["body"]) != "Implementation":
        raise ValueError("Cleanup requires an open Implementation Work")
    identity = dict(identity, actor=actor, command=command, created_at=event["comment"]["created_at"])
    return identity


def delegated_plan(repo):
    identity = comment_context(repo, PLAN_COMMAND, "delegated-plan")
    reports = {}
    for kind in DELEGATED_JOB:
        ctx = context(repo, kind, None, comment_mode=True)
        if ctx["base"] != identity["base"] or ctx["run"] != identity["run"] or ctx["automatic"] or ctx["since"]:
            raise ValueError("Cleanup plan identity mismatch")
        reports[kind] = manifest(ctx, kind)
    if comment_context(repo, PLAN_COMMAND, "delegated-plan") != identity:
        raise ValueError("Cleanup plan request changed")
    report = {"repository": repo, "state": "PLANNED", "mode": "read-only", "execution": None,
              "request": identity, "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "reports": reports}
    if len(json.dumps(report, indent=2, ensure_ascii=False).encode()) + 1 > review.MAX_REVIEW_JSON:
        raise ValueError("Cleanup plan evidence too large")
    return report


def verify_plan(ctx, identity, plan_run, plan_job, kind, approved):
    root, owner = ctx["root"], ctx["owner"]
    run = api(f"{root}/actions/runs/{plan_run}")
    if (run.get("id") != plan_run or run.get("path") != WORKFLOW or run.get("event") != "issue_comment"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("run_attempt") != 1 or run.get("head_branch") != "main" or run.get("head_sha") != ctx["base"]
            or (run.get("repository") or {}).get("id") != ctx["id"]
            or (run.get("head_repository") or {}).get("id") != ctx["id"]
            or any((run.get(k) or {}).get("login", "").casefold() != owner for k in ("actor", "triggering_actor"))):
        raise ValueError("Invalid cleanup evidence run")
    jobs = counted(f"{root}/actions/runs/{plan_run}/jobs?filter=all&per_page=100", "jobs")
    matches = [j for j in jobs if j["id"] == plan_job]
    if (len(matches) != 1 or any(j.get("name") != "delegated-plan" and j.get("conclusion") != "skipped" for j in jobs)):
        raise ValueError("Cleanup evidence is not an exclusively read-only plan")
    job = matches[0]
    if (job.get("name") != "delegated-plan" or job.get("run_id") != plan_run
            or job.get("run_attempt") != 1 or job.get("head_sha") != ctx["base"]
            or job.get("status") != "completed" or job.get("conclusion") != "success"):
        raise ValueError("Invalid cleanup evidence job")
    completed = timestamp(job["completed_at"])
    request_time = timestamp(identity["created_at"])
    now = dt.datetime.now(dt.timezone.utc)
    if not completed < request_time <= now or now - completed > dt.timedelta(minutes=30):
        raise ValueError("Cleanup evidence expired or request predates completed plan")
    # Existing transport authenticates the GitHub origin and archive hash and
    # decodes only a bounded single review.json, without extracting files.
    report = review.read_review_artifact(ctx["repo"], run)
    if (report.get("repository") != ctx["repo"] or report.get("state") != "PLANNED"
            or report.get("mode") != "read-only" or "execution" not in report or report["execution"] is not None):
        raise ValueError("Invalid cleanup evidence report")
    request = report["request"]
    if (request.get("work") != identity["work"] or request.get("run") != plan_run or request.get("attempt") != 1
            or request.get("base") != ctx["base"] or request.get("command") != PLAN_COMMAND
            or request.get("actor", "").casefold() != owner or request.get("comment") == identity["comment"]):
        raise ValueError("Cleanup evidence request mismatch")
    source = api(f"{root}/issues/comments/{number(request['comment'])}")
    expected_url = f"https://api.github.com/{root}/issues/{identity['work']}"
    if (source.get("id") != request["comment"] or source.get("issue_url", "").casefold() != expected_url.casefold()
            or source.get("body") != PLAN_COMMAND or (source.get("user") or {}).get("login", "").casefold() != owner
            or not source.get("created_at") or source["created_at"] != source.get("updated_at")
            or source["created_at"] != request.get("created_at")
            or not timestamp(source["created_at"]) <= timestamp(run["created_at"]) <= timestamp(job["started_at"])
            <= timestamp(report["generated_at"]) <= completed):
        raise ValueError("Cleanup evidence comment changed or unrelated")
    reports = report["reports"]
    if set(reports) != set(DELEGATED_JOB):
        raise ValueError("Missing cleanup evidence manifests")
    for name, item in reports.items():
        plan = item["manifest"]
        targets = plan.get("targets")
        if (item.get("state") != "PLANNED" or item.get("mode") != "read-only" or item.get("automatic") is not False
                or plan.get("repository") != ctx["repo"] or plan.get("base") != ctx["base"]
                or plan.get("kind") != name or plan.get("since") != ""
                or not isinstance(targets, list) or len(targets) > LIMIT
                or digest(plan) != item.get("digest")):
            raise ValueError("Invalid cleanup evidence manifest")
    selected = reports[kind]
    if selected["digest"] != approved or not selected["manifest"]["targets"]:
        raise ValueError("Cleanup evidence digest mismatch or empty targets")
    return selected["manifest"], job["completed_at"]


def guard_delegated_history(ctx, completed):
    history = counted(ctx["root"] + "/actions/workflows/post-work-cleanup.yml/runs?" +
                      urlencode({"created": ">=" + completed, "per_page": 100}), "workflow_runs")
    current = [r for r in history if r["id"] == ctx["run"]]
    if len(current) != 1 or current[0].get("run_attempt") != 1:
        raise ValueError("Current delegated cleanup missing from history")
    start = timestamp(current[0]["created_at"])
    for run in history:
        if run["id"] == ctx["run"] or timestamp(run["created_at"]) > start:
            continue
        if run.get("status") != "completed":
            raise ValueError("Earlier cleanup unresolved; obtain fresh plan")
        jobs = counted(ctx["root"] + f"/actions/runs/{number(run['id'])}/jobs?filter=all&per_page=100", "jobs")
        if not jobs or any(j.get("name") in APPLY_JOBS and j.get("conclusion") != "skipped" for j in jobs):
            raise ValueError("Earlier cleanup apply attempt consumed evidence; obtain fresh plan")


def delegated_execute(repo, kind):
    if os.environ.get("AGENT_CLEANUP_DELEGATION") != "v1":
        raise ValueError("Agent cleanup delegation is disabled")
    event, _, _, _, _ = operation.event_context(repo, "issue_comment")
    command = event["comment"]["body"]
    plan_run, plan_job, expected_kind, approved, base = parse_delegation(command)
    if kind != expected_kind or kind not in DELEGATED_JOB:
        raise ValueError("Cleanup kind mismatch")
    identity = comment_context(repo, command, DELEGATED_JOB[kind])
    ctx = context(repo, kind, None, comment_mode=True)
    if base != ctx["base"] or base != identity["base"] or ctx["run"] != identity["run"] or ctx["automatic"] or ctx["since"]:
        raise ValueError("Cleanup execution base or context mismatch")
    reviewed, completed = verify_plan(ctx, identity, plan_run, plan_job, kind, approved)
    guard_delegated_history(ctx, completed)
    fresh = manifest(ctx, kind)
    if fresh["manifest"] != reviewed or fresh["digest"] != approved:
        raise ValueError("Reviewed cleanup manifest changed")

    def guard():
        if comment_context(repo, command, DELEGATED_JOB[kind]) != identity:
            raise ValueError("Cleanup execution request changed")

    guard()
    if verify_plan(ctx, identity, plan_run, plan_job, kind, approved) != (reviewed, completed):
        raise ValueError("Cleanup evidence changed")
    guard_delegated_history(ctx, completed)
    trace = {"request": identity, "plan_run": plan_run, "plan_job": plan_job,
             "kind": kind, "digest": approved}
    print(json.dumps(dict(trace, state="AUTHORIZED")), flush=True)
    return dict(apply(ctx, kind, fresh, approved, guard=guard), **trace)
