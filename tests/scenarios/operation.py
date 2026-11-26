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


    def test_dispatch_optional_inputs_and_action_specific_requirements(self):
        from controller import review
        from validation import merge, completion
        identity = {"run": 99, "attempt": 1, "base": self.sha, "actor": "owner"}
        close = {"action": "close", "work": "4", "snapshot": "a"*64, "evidence": "Reviewed"}
        merge_args = {"action": "merge", "work": "4", "snapshot": "a"*64, "pr": "5", "head": "b"*40}
        merged = {"merged": True, "merge_commit_sha": "c"*40,
                  "head": {"sha": "b"*40}, "base": {"ref": "main", "repo": {"full_name": self.repo}}}
        with patch.object(review, "current_main"), patch.object(review, "api") as api, \
                patch.object(completion, "approved_close") as close_write, \
                patch.object(merge, "read_snapshot", return_value=({"work": 4, "base": self.sha}, {})), \
                patch.object(merge, "merge_approved", return_value={"sha": "c"*40}) as merge_write:
            api.return_value = {"state": "closed", "state_reason": "completed"}
            for args in (close, dict(close, pr=""), dict(close, head=""), dict(close, pr="", head="")):
                with self.subTest(close=args):
                    close_write.reset_mock()
                    self.assertEqual(review.execute(self.repo, identity, args)["state"], "CLOSED")
                    close_write.assert_called_once_with(self.repo, 4, "a"*64, "Reviewed", close=True)
            merge_write.assert_not_called()
            close_write.reset_mock()
            api.return_value = merged
            for args in (merge_args, dict(merge_args, evidence="")):
                with self.subTest(merge=args):
                    merge_write.reset_mock()
                    self.assertEqual(review.execute(self.repo, identity, args)["state"], "MERGED")
                    merge_write.assert_called_once_with(self.repo, 5, "a"*64, "b"*40, expected_work=4)
            close_write.assert_not_called()
            merge_write.reset_mock()
            close_write.reset_mock()
            api.reset_mock()
            invalid = [None, [], "", dict(close, unknown=""), dict(close, evidence=None),
                       dict(close, work=4), dict(close, pr="5"), dict(close, head="b"*40),
                       dict(close, evidence=" "), dict(merge_args, evidence="not allowed"),
                       dict(merge_args, pr="0"), dict(merge_args, head=""), dict(close, action="other"),
                       dict(close, snapshot="bad")]
            invalid.extend({k: v for k, v in close.items() if k != missing}
                           for missing in ("action", "work", "snapshot", "evidence"))
            invalid.extend({k: v for k, v in merge_args.items() if k != missing}
                           for missing in ("pr", "head"))
            for args in invalid:
                with self.subTest(invalid=args), self.assertRaises(ValueError):
                    review.execute(self.repo, identity, args)
            close_write.assert_not_called()
            merge_write.assert_not_called()
            api.assert_not_called()

    def test_execute_request_accepts_omitted_optional_inputs(self):
        from controller import review
        from validation import completion
        inputs = {"action": "close", "work": "4", "snapshot": "a"*64, "evidence": "Reviewed"}
        event = {"repository": {"full_name": self.repo}, "ref": "main", "sender": self.user, "inputs": inputs}
        with patch.object(operation, "event_context", return_value=(event, self.sha, "owner", 99, 1)) as context, \
                patch.object(review, "current_main"), patch.object(authority, "read_policy", return_value={"owner": "owner"}), \
                patch.dict(os.environ, {"GITHUB_TRIGGERING_ACTOR": "owner"}), \
                patch.object(completion, "approved_close") as close, \
                patch.object(review, "api", return_value={"state": "closed", "state_reason": "completed"}):
            result = review.execute_request(self.repo)
            context.assert_called_once_with(self.repo, "workflow_dispatch")
            close.assert_called_once_with(self.repo, 4, "a"*64, "Reviewed", close=True)
            self.assertEqual(result["state"], "CLOSED")
            self.assertEqual(result["request"], {"run": 99, "attempt": 1, "base": self.sha, "actor": "owner"})
            self.assertNotIn("pr", inputs)
            self.assertNotIn("head", inputs)


    def test_refresh_readiness_uses_current_head_and_fresh_run(self):
        from controller import review
        identity = self.identity()
        head = "b"*40
        created = "2026-01-01T00:00:00Z"
        stamp = "2026-01-01T00:00:05Z"
        status = {"context": "grade-b", "state": "success", "created_at": stamp,
                  "target_url": "https://github.com/owner/repo/actions/runs/100"}
        run = {"id": 100, "path": ".github/workflows/grade-b.yml", "event": "issue_comment",
               "head_sha": self.sha, "head_branch": "main", "created_at": created,
               "status": "completed", "conclusion": "success"}
        combined = {"sha": head, "statuses": [status]}
        with patch.object(review, "api", side_effect=[combined, run]) as read:
            result = review.refresh_ready(self.repo, identity, head, created)
            self.assertEqual(result["run"], 100)
            self.assertEqual(read.call_count, 2)
            read.assert_any_call("repos/owner/repo/commits/" + head + "/status")
            read.assert_any_call("repos/owner/repo/actions/runs/100")
        for entry in (dict(status, state="pending"), dict(status, created_at="2025-12-31T23:59:59Z")):
            with patch.object(review, "api", return_value=dict(combined, statuses=[entry])):
                self.assertIsNone(review.refresh_ready(self.repo, identity, head, created))
        for other in (dict(run, event="issues"), dict(run, head_sha="c"*40),
                      dict(run, created_at="2025-12-31T23:59:59Z"), dict(run, status="in_progress")):
            with patch.object(review, "api", side_effect=[combined, other]):
                self.assertIsNone(review.refresh_ready(self.repo, identity, head, created))
        for invalid in (dict(combined, sha="c"*40), dict(combined, statuses=[]),
                        dict(combined, statuses=[status, status]), dict(combined, statuses=[dict(status, state="failure")]),
                        dict(combined, statuses=[dict(status, target_url="https://example.com")])):
            with patch.object(review, "api", return_value=invalid), self.assertRaises(ValueError):
                review.refresh_ready(self.repo, identity, head, created)
        with patch.object(review, "api", side_effect=[combined, dict(run, conclusion="failure")]):
            with self.assertRaises(ValueError):
                review.refresh_ready(self.repo, identity, head, created)
        with patch.object(review, "api", side_effect=RuntimeError("API unavailable")):
            with self.assertRaises(RuntimeError):
                review.refresh_ready(self.repo, identity, head, created)

    def test_wait_merge_refresh_bounded_and_fail_closed(self):
        from controller import review
        from validation import approval, merge
        from validation.model import digest
        identity = self.identity()
        head = "b"*40
        data = {"work": 4, "base": self.sha, "head": head}
        args = (self.repo, identity, 5, digest(data), head, self.comment["created_at"])
        clean = {"mergeable": True, "mergeable_state": "clean"}
        blocked = dict(clean, mergeable_state="blocked")
        allowed = {"allowed": True, "state": "APPROVED", "label_event_id": 7}
        ready = {"run": 100}
        with patch.object(review, "current_main"), patch.object(review.time, "sleep") as sleep, \
                patch.object(review.time, "monotonic", side_effect=[0, 3, 4, 6, 7]) as clock, \
                patch.object(approval, "inspect_pr", return_value=allowed), \
                patch.object(merge, "read_snapshot", side_effect=[(data, blocked), (data, clean)]), \
                patch.object(review, "refresh_ready", side_effect=[None, ready]) as status, \
                patch.object(merge, "merge_approved") as write:
            self.assertEqual(review.wait_merge_refresh(*args), ready)
            self.assertEqual(status.call_count, 2)
            self.assertEqual(sleep.call_count, 2)
            write.assert_not_called()
        with patch.object(review, "current_main"), patch.object(review.time, "sleep"), \
                patch.object(review.time, "monotonic", side_effect=[0, 91]), \
                patch.object(approval, "inspect_pr", return_value=allowed), \
                patch.object(merge, "read_snapshot") as snapshot:
            with self.assertRaisesRegex(ValueError, "Timed out"):
                review.wait_merge_refresh(*args)
            snapshot.assert_not_called()
        for bad in (dict(data, head="c"*40), dict(data, base="c"*40), dict(data, work=99)):
            with patch.object(review, "current_main"), patch.object(review.time, "sleep"), \
                    patch.object(approval, "inspect_pr", return_value=allowed), \
                    patch.object(merge, "read_snapshot", return_value=(bad, clean)), \
                    patch.object(review, "refresh_ready") as status:
                with self.assertRaises(ValueError):
                    review.wait_merge_refresh(*args)
                status.assert_not_called()
        with patch.object(review, "current_main", side_effect=ValueError("stale main")), \
                patch.object(approval, "inspect_pr") as approve:
            with self.assertRaises(ValueError):
                review.wait_merge_refresh(*args)
            approve.assert_not_called()
        for changed in ({"allowed": False, "reason": "revoked"}, dict(allowed, label_event_id=8)):
            with patch.object(review, "current_main"), patch.object(review.time, "sleep"), \
                    patch.object(approval, "inspect_pr", side_effect=[allowed, changed]), \
                    patch.object(merge, "read_snapshot", return_value=(data, clean)), \
                    patch.object(review, "refresh_ready", return_value=ready):
                with self.assertRaisesRegex(ValueError, "Approval changed"):
                    review.wait_merge_refresh(*args)

    def test_merge_comment_wait_then_full_verifier(self):
        from controller import review
        from validation import merge
        from validation.model import digest
        identity = self.identity()
        data = {"work": 4, "base": self.sha, "head": "b"*40}
        command = "/review merge verify 5 " + digest(data) + " " + data["head"]
        event = self.event()
        event["comment"]["body"] = command
        with patch.object(operation, "event_context", return_value=(event, self.sha, "owner", 99, 1)), \
                patch.object(operation, "request_identity", return_value=identity) as request, \
                patch.object(review, "wait_merge_refresh", return_value={"run": 100}) as wait, \
                patch.object(review, "current_main"), \
                patch.object(merge, "verify_full", return_value=data) as full, \
                patch.object(merge, "merge_approved") as write:
            result = review.comment_request(self.repo)
            self.assertEqual(result["state"], "VERIFIED")
            self.assertEqual(result["refresh"], {"run": 100})
            self.assertIsNone(result["execution"])
            wait.assert_called_once_with(self.repo, identity, 5, digest(data), data["head"], self.comment["created_at"])
            self.assertEqual(request.call_count, 2)
            full.assert_called_once_with(self.repo, 5, digest(data), data["head"], expected_work=4)
            write.assert_not_called()
            full.reset_mock()
            wait.side_effect = ValueError("pending timeout")
            with self.assertRaises(ValueError):
                review.comment_request(self.repo)
            full.assert_not_called()
            wait.side_effect = None
            request.side_effect = [identity, ValueError("request edited")]
            with self.assertRaises(ValueError):
                review.comment_request(self.repo)
            full.assert_not_called()
            request.side_effect = None
            full.side_effect = ValueError("required check failed")
            with self.assertRaises(ValueError):
                review.comment_request(self.repo)
            write.assert_not_called()
