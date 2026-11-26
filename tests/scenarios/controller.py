"""Controller decisions: literal callback fixtures and multi-assertion scenarios."""
import unittest
from unittest.mock import patch
from controller import decision_engine
from controller.decision_engine import decide, discover


def report(repo, issues, pulls, checks, detail=None):
    return decide(repo, issues, pulls, lambda r, n: checks,
                  lambda r, n: detail if detail is not None else next(p for p in pulls if p["number"] == n))


def first_action(repo, issues, pulls, checks, detail=None):
    return report(repo, issues, pulls, checks, detail)["works"][0]["actions"][0]["action"]


def orphan_action(repo, issues, pulls, checks):
    return report(repo, issues, pulls, checks)["unassigned_prs"][0]["action"]


def orphan_number(repo, issues, pulls, checks):
    return report(repo, issues, pulls, checks)["unassigned_prs"][0]["pr"]


class ControllerTests(unittest.TestCase):
    repo = "owner/repo"
    issue = {"number": 1, "title": "Work", "state": "open",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n### Acceptance criteria\n\nX passes"}
    pr = {"number": 2, "title": "Change", "state": "open", "draft": False,
          "body": "### Work\n\nRefs #1\n\n### Changes\n\nX\n\n### Verification\n\nTests pass",
          "base": {"ref": "main", "sha": "a"*40, "repo": {"full_name": repo}},
          "head": {"sha": "b"*40}, "mergeable": True, "mergeable_state": "clean"}
    checks = [{"name": "pr-template", "state": "SUCCESS"}]

    def test_api_failure(self):
        with patch.object(decision_engine, "pages", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                discover(self.repo)

    def test_read_only_decisions(self):
        issue = dict(self.issue, body=self.issue["body"].replace("Implementation", "Read-only", 1))
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
