"""Approval scenarios and pure JSON adapter."""
import unittest
from unittest.mock import patch
from validation import approval, authority


def evaluate_state(repo, work, issue, policy, comments, events, paths=None):
    return approval.evaluate(repo, work, issue, policy, comments, events, paths)["state"]


class ApprovalTests(unittest.TestCase):
    repo = "owner/repo"
    policy = {"owner": "owner", "A": [], "B": ["app/", "validation/"], "default": "C"}
    issue = {"number": 3, "title": "Work", "state": "open",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n### Acceptance criteria\n\nX passes"}

    def test_c_only(self):
        pr = {"state":"open", "head":{"sha":"b"*40}, "base":{"sha":"c"*40,"ref":"main"}, "body":""}
        with patch.object(approval, "api", return_value=pr), patch.object(authority,"inspect_pr",return_value=authority.classify(self.policy,["README.md"])), patch.object(approval, "read_state") as read:
            self.assertTrue(approval.inspect_pr(self.repo, 1)["allowed"])
            read.assert_not_called()
