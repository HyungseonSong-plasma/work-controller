"""Operation scenarios and fixed mocked identity adapter."""
import json
import os
import subprocess
import unittest
from unittest.mock import patch
from controller import decision_engine, executor, operation
from controller.operation import COMMAND, run, summary, request_identity, comment_request
from validation import authority


def identity_case(event=None, current_issue=None, current_comment=None, actor="owner", base=None):
    # The fixture patches the exact read boundaries; JSON cannot select a patch target.
    return CommentRequestTests().identity(event, current_issue, current_comment, actor, base)


class OperationTests(unittest.TestCase):
    repo = "owner/repo"
    plan = {"title": "change", "body": "example", "files": []}

    def report(self, action="PLAN_WORK", kind="Implementation"):
        return {"repository": self.repo, "mode": "read-only", "works": [
            {"work": 4, "work_type": kind, "actions": [{"action": action}]},
            {"work": 5, "work_type": "Implementation", "actions": [{"action": "WAIT_B_APPROVAL"}]},
        ], "unassigned_prs": [{"pr": 8, "references": [], "action": "FIX_WORK_REFERENCE"}]}

    def test_report_only(self):
        with patch.object(decision_engine, "discover", return_value=self.report()) as discover, patch.object(executor, "run") as publish:
            result = run(self.repo)
            self.assertEqual(result["state"], "REPORTED")
            self.assertIsNone(result["execution"])
            self.assertIn("Work #4", summary(result))
            self.assertIn("WAIT_B_APPROVAL", summary(result))
            self.assertIn("PR #8", summary(result))
            discover.assert_called_once_with(self.repo)
            publish.assert_not_called()

    def test_dispatch_and_dry_run(self):
        for execute, digest, state in [(False, None, "READY"), (True, "reviewed", "PUBLISHED")]:
            with patch.object(decision_engine, "discover", return_value=self.report()) as discover, patch.object(executor, "run", return_value={"state": state}) as publish:
                result = run(self.repo, 4, self.plan, execute, digest)
                self.assertEqual(result["state"], state)
                self.assertEqual(len(result["discovery"]["works"]), 2)
                discover.assert_called_once_with(self.repo)
                publish.assert_called_once_with(self.repo, 4, self.plan, execute, digest)

    def test_blocked_and_ambiguous(self):
        duplicate = self.report()
        duplicate["works"].append(duplicate["works"][0])
        multiple = self.report()
        multiple["works"][0]["actions"].append({"action": "HUMAN_REVIEW"})
        orphan = self.report()
        orphan["unassigned_prs"][0]["references"] = [4, 7]
        reports = [duplicate, multiple, orphan, self.report("WAIT_B_APPROVAL"),
                   self.report("STOP_B_WORK"), self.report("REVIEW_MERGE"),
                   self.report("PLAN_WORK", "Read-only")]
        for report in reports:
            with patch.object(decision_engine, "discover", return_value=report), patch.object(executor, "run") as publish:
                result = run(self.repo, 4, self.plan, True, "reviewed")
                self.assertEqual(result["state"], "HUMAN_REVIEW")
                publish.assert_not_called()
        with patch.object(decision_engine, "discover", return_value=self.report()), patch.object(executor, "run") as publish:
            self.assertEqual(run(self.repo, 99, self.plan)["state"], "HUMAN_REVIEW")
            publish.assert_not_called()

    def test_failures_and_explicit_arguments(self):
        with patch.object(decision_engine, "discover", side_effect=RuntimeError("API unavailable")), patch.object(executor, "run") as publish:
            self.assertEqual(run(self.repo, 4, self.plan)["state"], "HUMAN_REVIEW")
            publish.assert_not_called()
        with patch.object(decision_engine, "discover", return_value=self.report()), patch.object(executor, "run", return_value={"state": "HUMAN_REVIEW", "phase": "create_pr"}):
            self.assertEqual(run(self.repo, 4, self.plan, True, "reviewed")["execution"]["phase"], "create_pr")
        with patch.object(decision_engine, "discover") as discover:
            for args in [(4, None), (None, self.plan), (4, self.plan, True), (None, None, True, "reviewed")]:
                with self.assertRaises(ValueError):
                    run(self.repo, *args)
            discover.assert_not_called()



class CommentRequestTests(unittest.TestCase):
    repo = "owner/repo"
    sha = "a" * 40
    user = {"login": "owner", "id": 12}
    issue = {"number": 4, "state": "open",
             "body": "### Work type\n\nRead-only\n\n### Objective\n\nCheck X\n\n### Scope\n\n#### Constraints\n\n- Read only\n\n#### Actions\n\n- Check X\n\n### Acceptance criteria\n\n- Report"}
    comment = {"id": 51, "user": user, "body": COMMAND,
               "issue_url": "https://api.github.com/repos/owner/repo/issues/4",
               "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}

    def event(self):
        return {"action": "created", "repository": {"full_name": self.repo},
                "issue": dict(self.issue), "comment": dict(self.comment), "sender": self.user}

    def identity(self, event=None, current_issue=None, current_comment=None, actor="owner", base=None):
        with patch.object(operation, "api", side_effect=[
            {"commit": {"sha": base or self.sha}},
            self.issue if current_issue is None else current_issue,
            self.comment if current_comment is None else current_comment,
        ]), patch.object(authority, "read_policy", return_value={"owner": "owner"}):
            return request_identity(self.repo, event or self.event(), self.sha, actor, 99, 1)

    def test_fail_closed_and_read_only_dispatch(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "issue_comment",
               "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": self.repo,
               "GITHUB_SHA": self.sha, "GITHUB_EVENT_PATH": "event.json",
               "GITHUB_ACTOR": "owner", "GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}
        identity = self.identity()
        result = {"repository": self.repo, "state": "REPORTED", "discovery": None, "execution": None}
        from unittest.mock import mock_open
        with patch.dict(os.environ, env, clear=True), patch.object(operation.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, self.sha + "\n")), \
                patch("builtins.open", mock_open(read_data=json.dumps(self.event()))), \
                patch.object(operation, "request_identity", return_value=identity) as check, \
                patch.object(operation, "run", return_value=dict(result)) as cycle, \
                patch.object(executor, "run") as publish:
            actual = comment_request(self.repo)
            self.assertEqual(actual["request"], identity)
            self.assertIn("comment 51, run 99, attempt 1", summary(actual))
            cycle.assert_called_once_with(self.repo)
            publish.assert_not_called()
            cycle.reset_mock()
            check.side_effect = RuntimeError("API unavailable")
            with self.assertRaises(RuntimeError):
                comment_request(self.repo)
            cycle.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch.object(operation, "run") as cycle:
            with self.assertRaises(ValueError):
                comment_request(self.repo)
            cycle.assert_not_called()


    def test_review_commands_and_no_write(self):
        from controller import review
        from validation import merge, completion
        from validation.model import digest
        for command in ("/review merge snapshot 0", "/review completion snapshot ", "/review merge snapshot 4; echo x", "/review execute 4", "/review completion snapshot\n"):
            with self.assertRaises(ValueError):
                review.parse_command(command)
        identity = self.identity()
        data = {"work": 4, "base": self.sha, "head": "b"*40}
        with patch.object(review, "current_main"), patch.object(merge, "read_snapshot", return_value=(data, {})), patch.object(merge, "verify_full", return_value=data) as full, patch.object(merge, "merge_approved") as write, patch.object(completion, "approved_close") as close:
            result = review.preflight(self.repo, identity, "/review merge snapshot 5")
            self.assertEqual(result["request"], dict(identity, command="/review merge snapshot 5", pr=5))
            self.assertEqual(result["review_digest"], digest(data))
            self.assertIsNone(result["execution"])
            result = review.preflight(self.repo, identity, "/review merge verify 5 " + digest(data) + " " + data["head"])
            self.assertEqual(result["state"], "VERIFIED")
            full.assert_called_once_with(self.repo, 5, digest(data), data["head"], expected_work=4)
            with self.assertRaises(ValueError):
                review.preflight(self.repo, dict(identity, work=99), "/review merge snapshot 5")
            with self.assertRaises(ValueError):
                review.preflight(self.repo, dict(identity, base="c"*40), "/review merge snapshot 5")
            write.assert_not_called()
            close.assert_not_called()

    def test_review_request_and_manual_boundary(self):
        from controller import review
        from validation import merge, completion
        command = "/review completion snapshot"
        event = self.event()
        event["comment"]["body"] = command
        identity = self.identity()
        with patch.object(operation, "event_context", return_value=(event, self.sha, "owner", 99, 1)), patch.object(operation, "request_identity", return_value=identity) as check, patch.object(review, "preflight", return_value={"state": "SNAPSHOT"}) as preflight:
            self.assertEqual(review.comment_request(self.repo), {"state": "SNAPSHOT"})
            check.assert_called_once_with(self.repo, event, self.sha, "owner", 99, 1, command=command)
            check.side_effect = ValueError("edited or unauthorized")
            preflight.reset_mock()
            with self.assertRaises(ValueError):
                review.comment_request(self.repo)
            preflight.assert_not_called()
        manual = {"repository": {"full_name": self.repo}, "ref": "main", "sender": self.user}
        with patch.object(review, "current_main"), patch.object(authority, "read_policy", return_value={"owner": "owner"}), patch.dict(os.environ, {"GITHUB_TRIGGERING_ACTOR": "owner"}), patch.object(merge, "merge_approved") as write, patch.object(completion, "approved_close") as close:
            verified = review.manual_identity(self.repo, manual, self.sha, "owner", 99, 1)
            for actor, attempt in (("other", 1), ("owner", 2)):
                with self.assertRaises(ValueError):
                    review.manual_identity(self.repo, manual, self.sha, actor, 99, attempt)
            args = {"action": "close", "work": "4", "pr": "", "snapshot": "a"*64, "head": "", "evidence": ""}
            with self.assertRaises(ValueError):
                review.execute(self.repo, verified, args)
            with self.assertRaises(ValueError):
                review.execute(self.repo, verified, dict(args, action="merge", pr="5", head="bad"))
            write.assert_not_called()
            close.assert_not_called()
