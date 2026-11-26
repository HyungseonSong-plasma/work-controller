"""One Controller operation cycle; existing contracts own all decisions and writes."""
import json
import os
import subprocess

from controller import decision_engine, executor
from validation import authority, issue as issue_contract
from validation.model import REPO, SHA, api

WAIT = {"WAIT_CI", "WAIT_MERGE_GATE", "WAIT_B_APPROVAL", "REFRESH_STATE"}
ACTION = {"PLAN_WORK", "PERFORM_READ_ONLY", "CONTINUE_DRAFT", "FIX_CI", "UPDATE_BRANCH"}
ERRORS = (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.CalledProcessError)


def category(action):
    if action in WAIT:
        return "Wait"
    if action in ACTION:
        return "Action"
    return "Human Review"


def run(repo, work=None, plan=None, execute=False, reviewed_digest=None):
    """Report one discovery cycle; optionally delegate one explicit plan to Executor."""
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise ValueError("Expected owner/repo")
    if (work is None) != (plan is None):
        raise ValueError("Work and plan must be supplied together")
    if work is not None and (type(work) is not int or work < 1 or not isinstance(plan, dict)):
        raise ValueError("Expected positive Work number and JSON object plan")
    if execute != bool(reviewed_digest) or (execute and plan is None):
        raise ValueError("Execution requires a plan and reviewed digest")
    result = {"repository": repo, "state": "REPORTED", "discovery": None, "execution": None}
    try:
        report = decision_engine.discover(repo)
        if (report["repository"].casefold() != repo.casefold()
                or not isinstance(report["works"], list) or not isinstance(report["unassigned_prs"], list)):
            raise ValueError("Invalid discovery report")
        result["discovery"] = report
        if plan is None:
            return result
        targets = [item for item in report["works"] if item["work"] == work]
        if (len(targets) != 1 or targets[0].get("work_type") != "Implementation"
                or [a["action"] for a in targets[0]["actions"]] != ["PLAN_WORK"]
                or any(work in p.get("references", []) for p in report["unassigned_prs"])):
            result.update(state="HUMAN_REVIEW", reason="Target must have one unambiguous Implementation PLAN_WORK decision")
            return result
    except ERRORS as exc:
        result.update(state="HUMAN_REVIEW", reason=str(exc))
        return result
    try:
        execution = executor.run(repo, work, plan, execute, reviewed_digest)
        result["execution"] = execution
        result["state"] = execution["state"]
    except ERRORS as exc:
        result.update(state="HUMAN_REVIEW", reason=str(exc),
                      partial_write_possible=execute)
    return result


COMMAND = "/controller run"


def request_identity(repo, event, sha, actor, run_id, attempt, command=COMMAND):
    """Recheck a created comment against trusted policy and current GitHub records."""
    if not REPO.fullmatch(repo) or not SHA.fullmatch(sha):
        raise ValueError("Invalid repository or checkout SHA")
    if any(type(n) is not int or n < 1 for n in (run_id, attempt)):
        raise ValueError("Invalid run identity")
    if event["action"] != "created" or event["repository"]["full_name"].casefold() != repo.casefold():
        raise ValueError("Invalid request event")
    original_issue, original = event["issue"], event["comment"]
    work, comment = original_issue["number"], original["id"]
    if any(type(n) is not int or n < 1 for n in (work, comment)):
        raise ValueError("Invalid Work or comment number")
    if ("pull_request" in original_issue or original_issue["state"] != "open"
            or original["body"] != command or not original["created_at"]
            or original["created_at"] != original["updated_at"]):
        raise ValueError("Expected a new exact command on an open Work")
    root = f"repos/{repo}"
    if api(f"{root}/branches/main")["commit"]["sha"] != sha:
        raise ValueError("Checkout is no longer current main")
    owner = authority.read_policy(repo, sha)["owner"].casefold()
    if any(login.casefold() != owner for login in
           (actor, event["sender"]["login"], original["user"]["login"])):
        raise ValueError("Only the policy owner may request a run")
    current_issue = api(f"{root}/issues/{work}")
    if (current_issue["number"] != work or current_issue["state"] != "open"
            or "pull_request" in current_issue or not issue_contract.validate(current_issue["body"])):
        raise ValueError("Current Work is closed, invalid or not an Issue")
    current = api(f"{root}/issues/comments/{comment}")
    expected_url = f"https://api.github.com/repos/{repo}/issues/{work}"
    if (current["id"] != comment or current["issue_url"].casefold() != expected_url.casefold()
            or original["issue_url"].casefold() != expected_url.casefold()
            or current["body"] != command or current["user"]["login"].casefold() != owner
            or current["user"]["id"] != original["user"]["id"]
            or current["created_at"] != original["created_at"]
            or current["updated_at"] != original["updated_at"]):
        raise ValueError("Request changed or does not belong to this Work")
    return {"work": work, "comment": comment, "run": run_id, "attempt": attempt, "base": sha}


def event_context(repo, event_name):
    """Read the event from the exact trusted checkout."""
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_EVENT_NAME") != event_name
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_REPOSITORY", "").casefold() != repo.casefold()):
        raise ValueError("Comment request requires trusted-main GitHub Actions")
    sha = os.environ["GITHUB_SHA"]
    checked_out = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                                 capture_output=True, text=True).stdout.strip()
    if checked_out != sha:
        raise ValueError("Checkout does not match event SHA")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
        event = json.load(stream)
    return (event, sha, os.environ["GITHUB_ACTOR"],
            int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"]))


def comment_request(repo):
    """Actions-only, read-only entrypoint; no plan or write options."""
    identity = request_identity(repo, *event_context(repo, "issue_comment"))
    result = run(repo)
    result["request"] = identity
    return result


def summary(report):
    """Render only controlled action names and numeric references, not untrusted Issue text."""
    lines = ["## Controller operation", "", "Result: " + (
        report["state"] if report["state"] in {"REPORTED", "READY", "PUBLISHED"} else "Human Review"), ""]
    if "request" in report:
        request = report["request"]
        lines += [f"Request: Work #{request['work']}, comment {request['comment']}, run {request['run']}, attempt {request['attempt']}", ""]
    discovery = report.get("discovery")
    if discovery is None:
        return "\n".join(lines + ["Discovery failed; inspect the run log.\n"])
    repo = report["repository"]
    groups = {"Action": [], "Wait": [], "Human Review": []}
    for work in discovery["works"]:
        number = work["work"]
        if type(number) is not int:
            raise ValueError("Invalid Work reference")
        for item in work["actions"]:
            action = item["action"]
            group = category(action)
            safe = action if isinstance(action, str) and action.isascii() and action.replace("_", "").isalnum() and action.isupper() else "UNKNOWN_ACTION"
            reference = f"[Work #{number}](https://github.com/{repo}/issues/{number})"
            if type(item.get("pr")) is int:
                pr = item["pr"]
                reference += f" / [PR #{pr}](https://github.com/{repo}/pull/{pr})"
            groups[group].append(f"- {reference}: `{safe}`")
    for item in discovery["unassigned_prs"]:
        number = item["pr"]
        if type(number) is not int:
            raise ValueError("Invalid PR reference")
        groups["Human Review"].append(f"- [PR #{number}](https://github.com/{repo}/pull/{number}): `FIX_WORK_REFERENCE`")
    for group, entries in groups.items():
        lines += [f"### {group} ({len(entries)})", "", *(entries or ["None"]), ""]
    return "\n".join(lines)
