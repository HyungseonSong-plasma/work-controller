"""Merge scenarios requiring multiple assertions or mock state."""
import subprocess
import unittest
from unittest.mock import patch
from validation import merge
from validation.merge import snapshot, verify
from validation.model import digest


class MergeTests(unittest.TestCase):
    repo = "owner/repo"
    pr = {"number": 4, "state": "open", "draft": False, "title": "Change",
          "body": "### Work\n\nRefs #3\n\n### Changes\n\nX\n\n### Verification\n\nTests pass",
          "base": {"ref": "main", "sha": "a"*40, "repo": {"full_name": repo}},
          "head": {"sha": "b"*40}}
    issue = {"number": 3, "state": "open", "title": "Work",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n### Acceptance criteria\n\nX passes"}

    def test_valid_snapshot(self):
        data = snapshot(self.pr, self.issue, self.repo, 4)
        self.assertEqual(data["work"], 3)
        self.assertEqual(data["head"], "b"*40)
        self.assertEqual(digest(data), digest(dict(reversed(list(data.items())))))

    def test_review_snapshot_changes(self):
        original = digest(snapshot(self.pr, self.issue, self.repo, 4))
        for pr, issue in ((dict(self.pr, title="Changed"), self.issue),
                          (dict(self.pr, head={"sha": "c"*40}), self.issue),
                          (self.pr, dict(self.issue, body=self.issue["body"]+" again"))):
            self.assertNotEqual(original, digest(snapshot(pr, issue, self.repo, 4)))

    def test_verify_rejects_stale_or_failed(self):
        data = snapshot(self.pr, self.issue, self.repo, 4)
        pr = dict(self.pr, mergeable=True, mergeable_state="clean")
        checks = subprocess.CompletedProcess([], 0, '[{"name":"pr-template","state":"SUCCESS"}]')
        with patch.object(merge, "read_snapshot", return_value=(data, pr)), patch("subprocess.run", return_value=checks):
            self.assertEqual(verify(self.repo, 4, digest(data), data["head"]), data)
            with self.assertRaises(ValueError):
                verify(self.repo, 4, "wrong", data["head"])
            with self.assertRaises(ValueError):
                verify(self.repo, 4, digest(data), "c"*40)
        with patch.object(merge, "read_snapshot", return_value=(data, pr)), patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, '[]')):
            with self.assertRaises(ValueError):
                verify(self.repo, 4, digest(data), data["head"])
        with patch.object(merge, "read_snapshot", side_effect=[(data, pr), (dict(data, work_title="new"), pr)]), patch("subprocess.run", return_value=checks):
            with self.assertRaises(ValueError):
                verify(self.repo, 4, digest(data), data["head"])


    def test_full_preflight_and_merge_share_authority(self):
        from validation import approval, authority
        data = snapshot(self.pr, self.issue, self.repo, 4)
        args = (self.repo, 4, digest(data), data["head"])
        with patch.object(merge, "verify", return_value=data) as check, patch.object(approval, "inspect_pr", return_value={"allowed": True, "approval_digest": "B"}) as approve, patch.object(authority, "inspect_pr", return_value={"allowed": True}) as rank, patch.object(merge, "api", return_value={"merged": True}) as write:
            self.assertEqual(merge.verify_full(*args, expected_work=3), data)
            self.assertEqual(check.call_count, 2)
            rank.assert_called_once_with(self.repo, 4, data["head"], data["base"], "B")
            write.assert_not_called()
            self.assertEqual(merge.merge_approved(*args), {"merged": True})
            write.assert_called_once()
            write.reset_mock()
            for kwargs in ({"expected_work": 99}, {"approved_change": "wrong"}):
                with self.assertRaises(ValueError):
                    merge.merge_approved(*args, **kwargs)
            approve.return_value = {"allowed": False, "reason": "revoked"}
            with self.assertRaises(ValueError):
                merge.verify_full(*args)
            approve.return_value = {"allowed": True}
            rank.return_value = {"allowed": False, "reason": "A path"}
            with self.assertRaises(ValueError):
                merge.merge_approved(*args)
            write.assert_not_called()
