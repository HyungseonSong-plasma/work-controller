"""Current-state merge preflight. Run from a trusted main checkout."""
import json
import subprocess
from .pr import work_number, work_errors
from .model import SHA, api, digest

def snapshot(pr, issue, repo, number):
    if pr.get("number") != number or pr.get("state") != "open" or pr.get("draft"):
        raise ValueError("PR must be the requested OPEN non-draft pull request")
    base, head = pr["base"], pr["head"]
    if base["ref"] != "main" or base["repo"]["full_name"].casefold() != repo.casefold():
        raise ValueError("PR must target this repository's main")
    if not all(SHA.fullmatch(x["sha"]) for x in (base, head)):
        raise ValueError("Invalid commit SHA")
    work = work_number(pr.get("body"))
    problems = work_errors(work, issue)
    if problems:
        raise ValueError("; ".join(problems))
    return {
        "repository": repo.casefold(), "pr": number, "head": head["sha"], "base": base["sha"],
        "pr_title": pr["title"], "pr_body": pr["body"],
        "work": work, "work_title": issue["title"], "work_body": issue["body"],
        "work_state": issue["state"],
    }


def read_snapshot(repo, number):
    pr = api(f"repos/{repo}/pulls/{number}")
    work = work_number(pr.get("body"))
    issue = api(f"repos/{repo}/issues/{work}")
    return snapshot(pr, issue, repo, number), pr


def verify(repo, number, approved, expected_head):
    current, pr = read_snapshot(repo, number)
    if current["head"] != expected_head or digest(current) != approved:
        raise ValueError("Reviewed snapshot or expected head changed; review again")
    if pr.get("mergeable_state") != "clean" or pr.get("mergeable") is not True:
        raise ValueError("GitHub does not report a clean mergeable PR")
    result = subprocess.run(["gh", "pr", "checks", str(number), "--repo", repo,
                             "--required", "--json", "name,state"], check=True,
                            capture_output=True, text=True)
    checks = json.loads(result.stdout)
    if not any(c["name"] == "pr-template" for c in checks) or any(c["state"] != "SUCCESS" for c in checks):
        raise ValueError("All required checks, including pr-template, must succeed")
    latest, latest_pr = read_snapshot(repo, number)
    if latest != current or latest_pr.get("mergeable_state") != "clean":
        raise ValueError("PR or Work changed during preflight")
    return latest


def verify_full(repo, number, approved, expected_head, approved_change=None, expected_work=None):
    """Read-only authority and snapshot checks shared by both execution paths."""
    from . import approval, authority
    data = verify(repo, number, approved, expected_head)
    if expected_work is not None and data["work"] != expected_work:
        raise ValueError("PR is not a Work Unit of the requesting Work")
    evidence = approval.inspect_pr(repo, number, data["head"], data["base"])
    if not evidence["allowed"]:
        raise ValueError(evidence["reason"])
    recorded = evidence.get("approval_digest")
    if approved_change and approved_change != recorded:
        raise ValueError("Supplied approval digest does not match recorded evidence")
    permission = authority.inspect_pr(repo, number, data["head"], data["base"], recorded)
    if not permission["allowed"]:
        raise ValueError(permission["reason"])
    return verify(repo, number, approved, expected_head)


def merge_approved(repo, number, approved, expected_head, approved_change=None, expected_work=None):
    data = verify_full(repo, number, approved, expected_head, approved_change, expected_work)
    result = api(f"repos/{repo}/pulls/{number}/merge", "-X", "PUT",
                 "-f", f"sha={data['head']}", "-f", "merge_method=squash")
    if result.get("merged") is not True:
        raise ValueError("GitHub rejected merge")
    return result
