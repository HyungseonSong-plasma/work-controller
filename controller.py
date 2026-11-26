"""Read-only controller: discover Work and recommend the next action."""
import argparse
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Existing validators use sibling imports; import them through the validation path.
sys.path.insert(0, str(Path(__file__).resolve().parent / "validation"))
from validate_issue import validate as valid_issue
from validate_completion import assessment, declared_works
from validate_merge import REPO, api, digest, snapshot
from validate_pr import errors as pr_errors


def pages(path):
    return [item for page in api(path, "--paginate", "--slurp") for item in page]


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
            fresh.get("base") != pr.get("base")):
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


def decide(repo, issues, pulls, check_reader, detail_reader):
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
        data, blockers = assessment(repo, number, issue, pulls)
        item["units"] = [{"pr": u["number"], "status": u["status"]} for u in data["units"]]
        active = [p for p in pulls if p["state"] == "open" and number in declared_works(p.get("body"))]
        if active:
            for pr in sorted(active, key=lambda p: p["number"]):
                item["actions"].append(pr_action(repo, pr, issue, check_reader, detail_reader))
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
                  lambda r, n: api(f"repos/{r}/pulls/{n}"))


class ControllerTests(unittest.TestCase):
    repo = "owner/repo"
    issue = {"number": 1, "title": "Work", "state": "open",
             "body": "### Objective\n\nX\n\n### Scope\n\nX only\n\n### Acceptance criteria\n\nX passes"}
    pr = {"number": 2, "title": "Change", "state": "open", "draft": False,
          "body": "### Work\n\nRefs #1\n\n### Changes\n\nX\n\n### Verification\n\nTests pass",
          "base": {"ref": "main", "sha": "a"*40, "repo": {"full_name": repo}},
          "head": {"sha": "b"*40}, "mergeable": True, "mergeable_state": "clean"}
    checks = [{"name": "pr-template", "state": "SUCCESS"}]

    def report(self, pulls, issues=None, checks=None, detail=None):
        return decide(self.repo, [self.issue] if issues is None else issues, pulls,
                      lambda r, n: self.checks if checks is None else checks,
                      lambda r, n: detail if detail is not None else next(p for p in pulls if p["number"] == n))

    def action(self, pulls, **kwargs):
        return self.report(pulls, **kwargs)["works"][0]["actions"][0]["action"]

    def test_plan_and_completion(self):
        self.assertEqual(self.action([]), "PLAN_WORK")
        merged = dict(self.pr, state="closed", merged_at="2026-01-01", merge_commit_sha="c"*40)
        self.assertEqual(self.action([merged]), "REVIEW_COMPLETION")
        self.assertEqual(self.action([dict(merged, merged_at=None)]), "PLAN_WORK")
        self.assertEqual(self.action([dict(merged, body="### Work\n\nRefs #1")]), "REVIEW_COMPLETION_BLOCKERS")

    def test_pr_states(self):
        self.assertEqual(self.action([self.pr]), "REVIEW_MERGE")
        self.assertEqual(self.action([self.pr], checks=[]), "WAIT_CI")
        self.assertEqual(self.action([self.pr], checks=[{"name":"pr-template","state":"FAILURE"}]), "FIX_CI")
        self.assertEqual(self.action([dict(self.pr, draft=True)]), "CONTINUE_DRAFT")
        self.assertEqual(self.action([dict(self.pr, mergeable=False, mergeable_state="dirty")]), "UPDATE_BRANCH")
        self.assertEqual(self.action([dict(self.pr, mergeable_state="blocked")]), "WAIT_MERGE_GATE")
        self.assertEqual(self.action([self.pr], detail=dict(self.pr, body="changed")), "REFRESH_STATE")

    def test_invalid_and_orphan(self):
        self.assertEqual(self.action([], issues=[dict(self.issue, body="")]), "FIX_WORK_CONTRACT")
        self.assertEqual(self.action([dict(self.pr, body="### Work\n\nRefs #1")]), "FIX_PR_CONTRACT")
        p = dict(self.pr, body=self.pr["body"].replace("#1", "#9"))
        self.assertEqual(self.report([p])["unassigned_prs"][0]["action"], "FIX_WORK_REFERENCE")
        self.assertEqual(self.report([self.pr], issues=[])["unassigned_prs"][0]["pr"], 2)

    def test_api_failure(self):
        with patch(__name__ + ".pages", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                discover(self.repo)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    if not REPO.fullmatch(args.repo):
        parser.error("Expected owner/repo")
    print(json.dumps(discover(args.repo), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        unittest.main(argv=[sys.argv[0]])
    else:
        try:
            main()
        except (ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"Controller discovery failed: {exc}", file=sys.stderr)
            sys.exit(1)
