"""Narrow Actions bridge for read-only review and owner manual execution."""
import os
import re
from controller import operation
from validation import authority, completion, merge
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


def comment_request(repo):
    event, sha, actor, run, attempt = operation.event_context(repo, "issue_comment")
    command = event["comment"]["body"]
    parse_command(command)
    identity = operation.request_identity(repo, event, sha, actor, run, attempt, command=command)
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


def execute(repo, identity, inputs):
    if set(inputs) != {"action", "work", "pr", "snapshot", "head", "evidence"}:
        raise ValueError("Unexpected manual dispatch inputs")
    if any(not isinstance(v, str) for v in inputs.values()):
        raise ValueError("Dispatch inputs must be strings")
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
