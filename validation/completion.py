"""Work completion contract. Run from a trusted main checkout."""
import re

from .issue import validate as validate_issue, work_type
from .pr import errors as pr_errors, work_number
from .model import api, digest, normalize, sections, without_comments, pages, PLACEHOLDERS


def declared_works(body):
    """Find canonical reference lines in Work sections even if other sections are invalid."""
    result = set()
    for name, value in sections(without_comments(normalize(body))):
        if name == "Work":
            result.update(int(n) for n in re.findall(r"^Refs #([1-9][0-9]*)[ \t]*$", value, re.M))
    return result


def result_comments(comments):
    results = []
    for comment in sorted(comments, key=lambda c: c["id"]):
        body = without_comments(normalize(comment.get("body"))).strip()
        match = re.match(r"^### Work result[ \t]*\n([\s\S]+)$", body)
        if match and match.group(1).strip() and match.group(1).strip().casefold() not in PLACEHOLDERS:
            results.append({"id": comment["id"], "body": comment["body"],
                            "author": (comment.get("user") or {}).get("login")})
    return results


def assessment(repo, number, issue, pulls, comments=()):
    """Return review snapshot and structural blockers; does not judge meaning."""
    blockers = []
    if issue.get("number") != number or "pull_request" in issue:
        blockers.append("Target must be the requested Issue, not a PR")
    if not validate_issue(issue.get("body")):
        blockers.append("Work violates Issue contract")
    units = []
    merged = 0
    for pr in sorted(pulls, key=lambda p: p["number"]):
        if number not in declared_works(pr.get("body")):
            continue
        n = pr["number"]
        status = "open" if pr["state"] == "open" else ("merged" if pr.get("merged_at") else "discarded")
        if status == "open":
            blockers.append(f"Work Unit #{n} is still OPEN")
        if status == "merged":
            if (pr["base"]["ref"] != "main" or
                    pr["base"]["repo"]["full_name"].casefold() != repo.casefold()):
                blockers.append(f"Work Unit #{n} did not merge to this repository's main")
            elif pr_errors(pr.get("body")) or work_number(pr["body"]) != number:
                blockers.append(f"Merged Work Unit #{n} violates PR contract")
            elif not pr.get("merge_commit_sha"):
                blockers.append(f"Merged Work Unit #{n} lacks merge commit SHA")
            else:
                merged += 1
        units.append({"number": n, "status": status, "title": pr["title"], "body": pr.get("body"),
                      "head": pr["head"]["sha"], "base": pr["base"]["ref"],
                      "merge_commit": pr.get("merge_commit_sha") if status == "merged" else None})
    kind = work_type(issue.get("body"))
    results = result_comments(comments) if kind == "Read-only" else []
    if kind == "Read-only":
        if units:
            blockers.append("Read-only Work must not have linked PRs")
        if not results:
            blockers.append("Read-only Work requires a non-empty Work result comment")
    elif not merged:
        blockers.append("Work requires at least one valid merged Work Unit")
    data = {"repository": repo.casefold(), "work": number, "title": issue["title"],
            "body": issue.get("body"), "state": issue["state"],
            "state_reason": issue.get("state_reason"), "units": units, "work_type": kind, "results": results}
    return data, blockers


def read_assessment(repo, number):
    issue = api(f"repos/{repo}/issues/{number}")
    comments = pages(f"repos/{repo}/issues/{number}/comments?per_page=100") if work_type(issue.get("body")) == "Read-only" else []
    return assessment(repo, number, issue, pages(f"repos/{repo}/pulls?state=all&per_page=100"), comments)


def approved_close(repo, number, approved, evidence=None, close=False):
    current, blockers = read_assessment(repo, number)
    if current["state"] != "open":
        raise ValueError("Work must be OPEN before completion")
    if blockers:
        raise ValueError("; ".join(blockers))
    if digest(current) != approved:
        raise ValueError("Reviewed Work snapshot changed; review again")
    if close and not (evidence or "").strip():
        raise ValueError("Explicit acceptance review evidence is required to close")
    latest, blockers = read_assessment(repo, number)
    if latest != current or blockers:
        raise ValueError("Work or Work Units changed during completion preflight")
    if close:
        api(f"repos/{repo}/issues/{number}/comments", "-X", "POST", "-f",
            "body=Work completion review\n\nSnapshot: " + approved + "\n\nEvidence: " + evidence.strip())
        latest, blockers = read_assessment(repo, number)
        if latest != current or blockers:
            raise ValueError("Work changed after recording evidence; review again")
        result = api(f"repos/{repo}/issues/{number}", "-X", "PATCH", "-f", "state=closed",
                     "-f", "state_reason=completed")
        if result.get("state") != "closed" or result.get("state_reason") != "completed":
            raise ValueError("GitHub did not report completed closure")
    return current


def guard_closed(repo, number):
    """Post-close structural guard, not semantic approval."""
    data, blockers = read_assessment(repo, number)
    if data["state"] != "closed" or data["state_reason"] != "completed" or not blockers:
        return []
    latest, blockers = read_assessment(repo, number)
    if latest["state"] != "closed" or latest["state_reason"] != "completed" or not blockers:
        return []
    result = api(f"repos/{repo}/issues/{number}", "-X", "PATCH", "-f", "state=open")
    if result.get("state") != "open":
        raise ValueError("GitHub did not reopen the invalid completion")
    api(f"repos/{repo}/issues/{number}/comments", "-X", "POST", "-f",
        "body=Work completion contract failed; reopened.\n\n" + "\n".join("- " + b for b in blockers))
    return blockers
