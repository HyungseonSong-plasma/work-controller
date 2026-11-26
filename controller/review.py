"""Narrow Actions bridge for read-only review and owner manual execution."""
import os
import re
import time
from controller import operation
from validation import approval, authority, completion, merge
from validation.model import REPO, SHA, api, digest

DIGEST = re.compile(r"[0-9a-f]{64}")
NUMBER = re.compile(r"[1-9][0-9]*")


def number(value):
    if not isinstance(value, str) or not NUMBER.fullmatch(value):
        raise ValueError("Expected positive decimal identifier")
    return int(value)


def parse_command(command):
    if not isinstance(command, str) or len(command) > 256:
        raise ValueError("Invalid review command")
    match = re.fullmatch(r"/review merge snapshot ([1-9][0-9]*)", command)
    if match:
        return ("merge", "snapshot", number(match[1]), None, None)
    match = re.fullmatch(r"/review merge verify ([1-9][0-9]*) ([0-9a-f]{64}) ([0-9a-f]{40})", command)
    if match:
        return ("merge", "verify", number(match[1]), match[2], match[3])
    if command == "/review completion snapshot":
        return ("completion", "snapshot", None, None, None)
    match = re.fullmatch(r"/review completion verify ([0-9a-f]{64})", command)
    if match:
        return ("completion", "verify", None, match[1], None)
    raise ValueError("Unsupported review command")


def current_main(repo, sha):
    if not REPO.fullmatch(repo) or not SHA.fullmatch(sha):
        raise ValueError("Invalid repository or trusted SHA")
    if api(f"repos/{repo}/branches/main")["commit"]["sha"] != sha:
        raise ValueError("Checkout is no longer current main")


def preflight(repo, identity, command):
    kind, mode, pr, approved, head = parse_command(command)
    work = identity["work"]
    if kind == "merge":
        if mode == "snapshot":
            data, _ = merge.read_snapshot(repo, pr)
        else:
            data = merge.verify_full(repo, pr, approved, head, expected_work=work)
        if data["work"] != work or data["base"] != identity["base"]:
            raise ValueError("PR must belong to requesting Work and current main")
        blockers = []
    elif mode == "snapshot":
        data, blockers = completion.read_assessment(repo, work)
        if data["state"] != "open":
            raise ValueError("Work must be OPEN")
    else:
        data = completion.approved_close(repo, work, approved)
        blockers = []
    current_main(repo, identity["base"])
    return {"repository": repo, "state": "BLOCKED" if blockers else
            ("VERIFIED" if mode == "verify" else "SNAPSHOT"),
            "mode": "read-only", "execution": None,
            "request": dict(identity, command=command, pr=pr),
            "snapshot": data, "review_digest": digest(data), "blockers": blockers}


REFRESH_TIMEOUT = 90
REFRESH_INTERVAL = 3
UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def refresh_ready(repo, identity, head, created_at):
    """Observe the latest status on the exact PR head, never accept an old success.

    A status/run/time match is readiness evidence only: GitHub does not expose
    an exact triggering comment ID in the approval run metadata. Concurrent
    issue events can still race; the full verifier remains authoritative.
    """
    combined = api(f"repos/{repo}/commits/{head}/status")
    if combined.get("sha") != head or not isinstance(combined.get("statuses"), list):
        raise ValueError("Missing or mismatched head status")
    statuses = combined["statuses"]
    if any(s.get("state") in {"failure", "error"} for s in statuses
           if s.get("context") in {"grade-a", "grade-b"}):
        raise ValueError("Terminal FileRank status failure")
    matches = [s for s in statuses if s.get("context") == "grade-b"]
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous grade-b status")
    status = matches[0]
    stamp = status.get("created_at", "")
    if not isinstance(stamp, str) or not UTC_TIMESTAMP.fullmatch(stamp):
        raise ValueError("Invalid grade-b status timestamp")
    if status.get("state") not in {"pending", "success"}:
        raise ValueError("Unexpected grade-b status state")
    if stamp < created_at or status["state"] == "pending":
        return None
    url = status.get("target_url", "")
    match = re.fullmatch(r"https://github\.com/" + re.escape(repo)
                         + r"/actions/runs/([1-9][0-9]*)", url, re.IGNORECASE)
    if not match:
        raise ValueError("Missing grade-b workflow run reference")
    run_id = int(match[1])
    run = api(f"repos/{repo}/actions/runs/{run_id}")
    if (run.get("id") != run_id or run.get("path") != ".github/workflows/grade-b.yml"
            or run.get("event") != "issue_comment" or run.get("head_sha") != identity["base"]
            or run.get("head_branch") != "main"):
        return None
    started = run.get("created_at", "")
    if not isinstance(started, str) or not UTC_TIMESTAMP.fullmatch(started):
        raise ValueError("Invalid approval run timestamp")
    if started < created_at:
        return None
    if run.get("status") != "completed":
        return None
    if run.get("conclusion") != "success":
        raise ValueError("Approval refresh workflow failed")
    return {"run": run_id, "status_created_at": stamp,
            "correlation": "head/status/run/time; triggering comment ID unavailable"}


def wait_merge_refresh(repo, identity, pr, approved, head, created_at):
    """Bounded read-only readiness wait, not an approval or a retry of a write."""
    if not isinstance(created_at, str) or not UTC_TIMESTAMP.fullmatch(created_at):
        raise ValueError("Invalid review comment timestamp")
    current_main(repo, identity["base"])
    initial = approval.inspect_pr(repo, pr, head, identity["base"])
    if not initial["allowed"]:
        raise ValueError(initial["reason"])
    deadline = time.monotonic() + REFRESH_TIMEOUT
    # Let the comment-triggered refresh start before considering status evidence.
    time.sleep(REFRESH_INTERVAL)
    while time.monotonic() < deadline:
        current_main(repo, identity["base"])
        data, state = merge.read_snapshot(repo, pr)
        if (data["work"] != identity["work"] or data["base"] != identity["base"]
                or data["head"] != head or digest(data) != approved):
            raise ValueError("Reviewed Work or PR changed while waiting for approval refresh")
        if state.get("mergeable") is False or state.get("mergeable_state") not in {"clean", "blocked", "unknown"}:
            raise ValueError("PR is not eligible for approval refresh wait")
        ready = refresh_ready(repo, identity, head, created_at)
        if ready is not None and state.get("mergeable") is True and state.get("mergeable_state") == "clean":
            latest = approval.inspect_pr(repo, pr, head, identity["base"])
            if not latest["allowed"] or latest != initial:
                raise ValueError("Approval changed while waiting for refresh")
            if time.monotonic() >= deadline:
                break
            return ready
        time.sleep(min(REFRESH_INTERVAL, max(0, deadline - time.monotonic())))
    raise ValueError("Timed out waiting for current grade-b refresh and clean PR; human review required")


def comment_request(repo):
    event, sha, actor, run, attempt = operation.event_context(repo, "issue_comment")
    command = event["comment"]["body"]
    kind, mode, pr, approved, head = parse_command(command)
    identity = operation.request_identity(repo, event, sha, actor, run, attempt, command=command)
    if kind == "merge" and mode == "verify":
        refresh = wait_merge_refresh(repo, identity, pr, approved, head, event["comment"]["created_at"])
        # The request may have been edited/deleted, or main may have changed while waiting.
        if operation.request_identity(repo, event, sha, actor, run, attempt, command=command) != identity:
            raise ValueError("Review request identity changed")
        result = preflight(repo, identity, command)
        result["refresh"] = refresh
        return result
    return preflight(repo, identity, command)


def manual_identity(repo, event, sha, actor, run, attempt):
    current_main(repo, sha)
    owner = authority.read_policy(repo, sha)["owner"].casefold()
    if (event["repository"]["full_name"].casefold() != repo.casefold()
            or event["ref"] not in {"main", "refs/heads/main"}
            or type(run) is not int or run < 1 or attempt != 1
            or any(x.casefold() != owner for x in
                   (actor, event["sender"]["login"], os.environ["GITHUB_TRIGGERING_ACTOR"]))):
        raise ValueError("Execution requires initial owner manual dispatch on main")
    return {"run": run, "attempt": attempt, "base": sha, "actor": actor}


def normalize_dispatch_inputs(inputs):
    """Accept omitted optional workflow inputs without weakening required fields."""
    required = {"action", "work", "snapshot"}
    optional = {"pr", "head", "evidence"}
    if not isinstance(inputs, dict) or not required <= inputs.keys() or inputs.keys() - (required | optional):
        raise ValueError("Unexpected manual dispatch inputs: require action, work, snapshot; optional pr, head, evidence")
    if any(not isinstance(v, str) for v in inputs.values()):
        raise ValueError("Dispatch inputs must be strings")
    return {**dict.fromkeys(optional, ""), **inputs}


def execute(repo, identity, inputs):
    inputs = normalize_dispatch_inputs(inputs)
    work = number(inputs["work"])
    approved = inputs["snapshot"]
    if not DIGEST.fullmatch(approved):
        raise ValueError("Expected reviewed snapshot digest")
    current_main(repo, identity["base"])
    if inputs["action"] == "merge":
        pr = number(inputs["pr"])
        head = inputs["head"]
        if not SHA.fullmatch(head) or inputs["evidence"]:
            raise ValueError("Merge requires expected head and no closure evidence")
        data, _ = merge.read_snapshot(repo, pr)
        if data["work"] != work or data["base"] != identity["base"]:
            raise ValueError("PR must belong to requested Work and current main")
        result = merge.merge_approved(repo, pr, approved, head, expected_work=work)
        fresh = api(f"repos/{repo}/pulls/{pr}")
        if (fresh.get("merged") is not True or fresh.get("merge_commit_sha") != result.get("sha")
                or fresh["head"]["sha"] != head or fresh["base"]["ref"] != "main"
                or fresh["base"]["repo"]["full_name"].casefold() != repo.casefold()):
            raise ValueError("Merge not confirmed; inspect state before retry")
        return {"repository": repo, "state": "MERGED", "request": identity,
                "work": work, "pr": pr, "head": head, "review_digest": approved, "merge_commit": result["sha"]}
    if inputs["action"] == "close":
        if inputs["pr"] or inputs["head"] or not inputs["evidence"].strip():
            raise ValueError("Close requires acceptance evidence and no PR/head")
        completion.approved_close(repo, work, approved, inputs["evidence"], close=True)
        fresh = api(f"repos/{repo}/issues/{work}")
        if fresh.get("state") != "closed" or fresh.get("state_reason") != "completed":
            raise ValueError("Closure not confirmed; inspect state before retry")
        return {"repository": repo, "state": "CLOSED", "request": identity,
                "work": work, "review_digest": approved}
    raise ValueError("Unsupported execution action")


def execute_request(repo):
    event, sha, actor, run, attempt = operation.event_context(repo, "workflow_dispatch")
    identity = manual_identity(repo, event, sha, actor, run, attempt)
    return execute(repo, identity, event["inputs"])
