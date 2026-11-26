"""Completion scenarios and pure JSON adapter."""
import unittest
from unittest.mock import patch
from validation import completion
from validation.completion import assessment, declared_works, approved_close, guard_closed
from validation.model import digest


def assessment_blockers(repo, number, issue, pulls, comments=()):
    return completion.assessment(repo, number, issue, pulls, comments)[1]


class CompletionTests(unittest.TestCase):
    repo = "owner/repo"
    issue = {"number": 3, "title": "Work", "state": "open",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n### Acceptance criteria\n\nX passes"}
    pr = {"number": 4, "title": "Change", "state": "closed", "merged_at": "2026-01-01",
          "merge_commit_sha": "a"*40, "body": "### Work\n\nRefs #3\n\n### Changes\n\nX\n\n### Verification\n\nTests pass",
          "base": {"ref": "main", "repo": {"full_name": repo}}, "head": {"sha": "b"*40}}

    def assess(self, pulls, issue=None):
        return assessment(self.repo, 3, issue or self.issue, pulls)

    def test_valid_and_discarded(self):
        discarded = dict(self.pr, number=5, merged_at=None)
        data, blockers = self.assess([discarded, self.pr])
        self.assertEqual(blockers, [])
        self.assertEqual([u["status"] for u in data["units"]], ["merged", "discarded"])

    def test_membership(self):
        self.assertEqual(declared_works(self.pr["body"]), {3})
        self.assertEqual(declared_works("### Changes\n\nRefs #3"), set())
        self.assertEqual(declared_works("<!-- ### Work\n\nRefs #3 -->"), set())
        self.assertEqual(declared_works("### Work\n\nRefs #3\nRefs #7"), {3, 7})

    def test_approval_and_stale(self):
        data, blockers = self.assess([self.pr])
        with patch.object(completion, "read_assessment", return_value=(data, blockers)), patch.object(completion, "api") as call:
            self.assertEqual(approved_close(self.repo, 3, digest(data)), data)
            with self.assertRaises(ValueError):
                approved_close(self.repo, 3, "wrong")
            with self.assertRaises(ValueError):
                approved_close(self.repo, 3, digest(data), close=True)
            call.assert_not_called()
        with patch.object(completion, "read_assessment", side_effect=[(data, []), (dict(data, title="changed"), [])]):
            with self.assertRaises(ValueError):
                approved_close(self.repo, 3, digest(data))

    def test_guard(self):
        data, blockers = self.assess([], dict(self.issue, state="closed", state_reason="completed"))
        with patch.object(completion, "read_assessment", return_value=(data, blockers)), patch.object(completion, "api", side_effect=[{"state": "open"}, {}]) as call:
            self.assertTrue(guard_closed(self.repo, 3))
            self.assertEqual(call.call_count, 2)
        with patch.object(completion, "read_assessment", return_value=(dict(data, state_reason="not_planned"), blockers)), patch.object(completion, "api") as call:
            self.assertEqual(guard_closed(self.repo, 3), [])
            call.assert_not_called()

    def test_read_only_completion(self):
        issue = dict(self.issue, body=self.issue["body"].replace("Implementation", "Read-only", 1))
        comment = {"id": 7, "body": "### Work result\n\nFindings and evidence.", "user": {"login": "reviewer"}}
        data, blockers = assessment(self.repo, 3, issue, [], [comment])
        self.assertEqual(blockers, [])
        self.assertEqual(data["work_type"], "Read-only")
        self.assertEqual(data["results"][0]["id"], 7)
        revised, _ = assessment(self.repo, 3, issue, [], [dict(comment, body=comment["body"]+" revised")])
        self.assertNotEqual(digest(data), digest(revised))
