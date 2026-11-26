"""Read-only controller: discover Work and recommend the next action."""
import json
import subprocess

from validation.issue import validate as valid_issue, work_type
from validation.completion import assessment, declared_works
from validation.merge import snapshot
from validation.model import api, digest, pages
from validation.pr import errors as pr_errors
from validation import approval


def required_checks(repo, number):
    result = subprocess.run(["gh", "pr", "checks", str(number), "--repo", repo,
                             "--required", "--json", "name,state"], capture_output=True, text=True)
    if result.returncode not in (0, 1, 8):
        raise RuntimeError(f"Cannot read required checks for PR #{number}")
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot read required checks for PR #{number}") from exc
    if not isinstance(checks, list):
        raise RuntimeError(f"Invalid required checks response for PR #{number}")
    return checks


def pr_action(repo, pr, issue, check_reader, detail_reader):
    number = pr["number"]
    problems = pr_errors(pr.get("body"))
    if problems:
        return {"pr": number, "action": "FIX_PR_CONTRACT", "reasons": problems}
    if pr["base"]["ref"] != "main" or pr["base"]["repo"]["full_name"].casefold() != repo.casefold():
        return {"pr": number, "action": "FIX_PR_TARGET"}
    if pr.get("draft"):
        return {"pr": number, "action": "CONTINUE_DRAFT"}
    fresh = detail_reader(repo, number)
    if (fresh.get("body") != pr.get("body") or fresh.get("state") != "open" or
            fresh.get("head", {}).get("sha") != pr["head"]["sha"] or fresh.get("draft") or
            any(fresh.get("base", {}).get(k) != pr.get("base", {}).get(k)
                for k in ("ref", "sha")) or
            fresh["base"]["repo"]["full_name"].casefold() != pr["base"]["repo"]["full_name"].casefold()):
        return {"pr": number, "action": "REFRESH_STATE"}
    data = snapshot(fresh, issue, repo, number)
    checks = check_reader(repo, number)
    states = [c["state"] for c in checks]
    if any(s in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"} for s in states):
        action = "FIX_CI"
    elif not any(c["name"] == "pr-template" for c in checks) or any(s != "SUCCESS" for s in states):
        action = "WAIT_CI"
    elif fresh.get("mergeable") is False or fresh.get("mergeable_state") in {"dirty", "behind"}:
        action = "UPDATE_BRANCH"
    elif fresh.get("mergeable_state") != "clean" or fresh.get("mergeable") is not True:
        action = "WAIT_MERGE_GATE"
    else:
        action = "REVIEW_MERGE"
    result = {"pr": number, "action": action, "checks": checks}
    if action == "REVIEW_MERGE":
        result.update({"head": data["head"], "review_digest": digest(data)})
    return result


def decide(repo, issues, pulls, check_reader, detail_reader, comment_reader=None, approval_reader=None):
    works = {i["number"]: i for i in issues if "pull_request" not in i and i["state"] == "open"}
    report = {"repository": repo, "mode": "read-only", "works": [], "unassigned_prs": []}
    for pr in sorted(pulls, key=lambda p: p["number"]):
        if pr["state"] != "open":
            continue
        refs = declared_works(pr.get("body"))
        if len(refs) != 1 or not refs.issubset(works):
            report["unassigned_prs"].append({"pr": pr["number"], "references": sorted(refs),
                "action": "FIX_WORK_REFERENCE", "reasons": ["Expected exactly one existing OPEN Work"]})
    for number, issue in sorted(works.items()):
        item = {"work": number, "title": issue["title"], "actions": []}
        report["works"].append(item)
        if not valid_issue(issue.get("body")):
            item["actions"].append({"action": "FIX_WORK_CONTRACT"})
            continue
        kind = work_type(issue.get("body"))
        item["work_type"] = kind
        comments = comment_reader(repo, number) if kind == "Read-only" and comment_reader else []
        data, blockers = assessment(repo, number, issue, pulls, comments)
        item["units"] = [{"pr": u["number"], "status": u["status"]} for u in data["units"]]
        active = [p for p in pulls if p["state"] == "open" and number in declared_works(p.get("body"))]
        if kind == "Read-only":
            if data["units"]:
                item["actions"].append({"action": "REVIEW_COMPLETION_BLOCKERS", "reasons": blockers})
            elif blockers:
                item["actions"].append({"action": "PERFORM_READ_ONLY", "reasons": blockers})
            else:
                item["actions"].append({"action": "REVIEW_COMPLETION", "review_digest": digest(data)})
        elif active:
            actions = []
            for pr in sorted(active, key=lambda p: p["number"]):
                try:
                    if approval_reader:
                        evidence = approval_reader(repo, number, pr["number"])
                        if not evidence["allowed"]:
                            actions.append({"pr": pr["number"], "action": {"PENDING": "WAIT_B_APPROVAL",
                                            "REVOKED": "STOP_B_WORK"}.get(evidence["state"], "HUMAN_REVIEW"),
                                            "reasons": [evidence["reason"]]})
                            continue
                    actions.append(pr_action(repo, pr, issue, check_reader, detail_reader))
                except approval.ERRORS as exc:
                    actions.append({"pr": pr["number"], "action": "HUMAN_REVIEW", "reasons": [str(exc)]})
            blockers = [a for a in actions if a["action"] in {"HUMAN_REVIEW", "WAIT_B_APPROVAL", "STOP_B_WORK"}]
            item["actions"] = blockers or actions
        elif approval_reader:
            try:
                evidence = approval_reader(repo, number, None)
                if evidence["state"] not in {"APPROVED", "NOT_REQUESTED", "NOT_REQUIRED"}:
                    item["actions"].append({"action": {"PENDING": "WAIT_B_APPROVAL", "REVOKED": "STOP_B_WORK"}.get(evidence["state"], "HUMAN_REVIEW"), "reasons": [evidence["reason"]]})
                elif not blockers:
                    item["actions"].append({"action": "REVIEW_COMPLETION", "review_digest": digest(data)})
                elif any(u["status"] == "merged" for u in data["units"]):
                    item["actions"].append({"action": "REVIEW_COMPLETION_BLOCKERS", "reasons": blockers})
                else:
                    item["actions"].append({"action": "PLAN_WORK", "reasons": blockers})
            except approval.ERRORS as exc:
                item["actions"].append({"action": "HUMAN_REVIEW", "reasons": [str(exc)]})
        elif not blockers:
            item["actions"].append({"action": "REVIEW_COMPLETION", "review_digest": digest(data)})
        elif any(u["status"] == "merged" for u in data["units"]):
            item["actions"].append({"action": "REVIEW_COMPLETION_BLOCKERS", "reasons": blockers})
        else:
            item["actions"].append({"action": "PLAN_WORK", "reasons": blockers})
    return report


def discover(repo):
    issues = pages(f"repos/{repo}/issues?state=open&per_page=100")
    pulls = pages(f"repos/{repo}/pulls?state=all&per_page=100")
    return decide(repo, issues, pulls, required_checks,
                  lambda r, n: api(f"repos/{r}/pulls/{n}"),
                  lambda r, n: pages(f"repos/{r}/issues/{n}/comments?per_page=100"),
                  lambda r, w, p: approval.inspect_pr(r, p) if p else approval.inspect_work(r, w))
