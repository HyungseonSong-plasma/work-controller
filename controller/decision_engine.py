"""Read-only controller: discover Work and recommend the next action."""
import json
import subprocess
import unittest
from unittest.mock import patch

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

    def test_read_only_decisions(self):
        issue = dict(self.issue, body="### Work type\n\nRead-only\n\n" + self.issue["body"])
        def run(pulls, comments):
            data = decide(self.repo, [issue], pulls, lambda r, n: self.checks, lambda r, n: self.pr,
                          lambda r, n: comments)
            return data["works"][0]["actions"][0]["action"]
        self.assertEqual(run([], []), "PERFORM_READ_ONLY")
        result = {"id": 7, "body": "### Work result\n\nEvidence"}
        self.assertEqual(run([], [result]), "REVIEW_COMPLETION")
        self.assertEqual(run([self.pr], [result]), "REVIEW_COMPLETION_BLOCKERS")

    def test_approval_isolation(self):
        other = dict(self.issue, number=3)
        other_pr = dict(self.pr, number=4, body=self.pr["body"].replace("#1", "#3"))
        for state, expected in (("HUMAN_REVIEW","HUMAN_REVIEW"),("PENDING","WAIT_B_APPROVAL"),("REVOKED","STOP_B_WORK")):
            report = decide(self.repo, [self.issue,other], [self.pr,other_pr],
                            lambda r,n:self.checks, lambda r,n:self.pr if n==2 else other_pr,
                            approval_reader=lambda r,w,p: {"allowed":w==3,"state":state,"reason":"test"})
            self.assertEqual(report["works"][0]["actions"][0]["action"], expected)
            self.assertEqual(report["works"][1]["actions"][0]["action"], "REVIEW_MERGE")
        def broken(r,w,p):
            if w==1:
                raise RuntimeError("approval API unavailable")
            return {"allowed":True}
        report = decide(self.repo,[self.issue,other],[self.pr,other_pr],lambda r,n:self.checks,
                        lambda r,n:self.pr if n==2 else other_pr,approval_reader=broken)
        self.assertEqual(report["works"][0]["actions"][0]["action"],"HUMAN_REVIEW")
        self.assertEqual(report["works"][1]["actions"][0]["action"],"REVIEW_MERGE")

    def test_removed_approval_without_active_pr(self):
        report = decide(self.repo,[self.issue],[],lambda r,n:self.checks,lambda r,n:self.pr,
                        approval_reader=lambda r,w,p: {"state":"REVOKED","allowed":False,"reason":"label removed"})
        self.assertEqual(report["works"][0]["actions"][0]["action"], "STOP_B_WORK")
        report = decide(self.repo,[self.issue],[],lambda r,n:self.checks,lambda r,n:self.pr,
                        approval_reader=lambda r,w,p: {"state":"NOT_REQUESTED","allowed":False,"reason":"none"})
        self.assertEqual(report["works"][0]["actions"][0]["action"], "PLAN_WORK")
