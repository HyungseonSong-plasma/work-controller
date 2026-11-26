"""Cleanup I/O boundaries. Migrate these mocks to the #90 model."""
import unittest
from unittest.mock import patch

from controller import cleanup
from validation.model import digest


class CleanupTests(unittest.TestCase):
    def test_counted_incomplete(self):
        with patch.object(cleanup, "api", return_value=[{"total_count": 2, "jobs": [{"id": 1}]}]):
            with self.assertRaisesRegex(ValueError, "truncated"):
                cleanup.counted("example", "jobs")

    def test_counted_duplicate(self):
        with patch.object(cleanup, "api", return_value=[{"total_count": 2, "jobs": [{"id": 1}, {"id": 1}]}]):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                cleanup.counted("example", "jobs")

    def test_dry_run_no_deletion(self):
        ctx = {"root": "repos/example/test"}
        report = {"state": "PLANNED"}
        with patch.object(cleanup, "context", return_value=ctx), \
                patch.object(cleanup, "manifest", return_value=report), \
                patch.object(cleanup, "apply") as apply:
            self.assertEqual(cleanup.run("example/test", "branches"), report)
            apply.assert_not_called()

    def test_changed_digest_no_write(self):
        plan = {"targets": [{"branch": "test/one"}]}
        report = {"manifest": plan, "digest": digest(plan)}
        with patch.object(cleanup, "delete_branch") as delete, patch.object(cleanup, "catalog") as read:
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                cleanup.apply({}, "branches", report, "0" * 64)
            read.assert_not_called()
            delete.assert_not_called()

    def test_changed_ref_no_write(self):
        target = {"branch": "test/one", "head": "a" * 40}
        plan = {"targets": [target]}
        report = {"manifest": plan, "digest": digest(plan)}
        ctx = {"root": "repos/example/test", "base": "b" * 40, "automatic": False}
        with patch.object(cleanup, "current_main", return_value=ctx["base"]), \
                patch.object(cleanup, "catalog", return_value={"branches": {"test/one": {"name": "test/one"}}}), \
                patch.object(cleanup, "active_refs", return_value=set()), \
                patch.object(cleanup, "branch_target", return_value=(None, "Branch head changed")), \
                patch.object(cleanup, "delete_branch") as delete:
            with self.assertRaisesRegex(ValueError, "target changed"):
                cleanup.apply(ctx, "branches", report, report["digest"])
            delete.assert_not_called()

    def test_changed_run_no_write(self):
        target = {"run": 123}
        plan = {"targets": [target]}
        report = {"manifest": plan, "digest": digest(plan)}
        ctx = {"root": "repos/example/test", "base": "b" * 40, "automatic": False}
        with patch.object(cleanup, "current_main", return_value=ctx["base"]), \
                patch.object(cleanup, "catalog", return_value={}), \
                patch.object(cleanup, "api", return_value={"id": 123, "status": "in_progress"}), \
                patch.object(cleanup, "run_target", return_value=(None, "Not disposable")), \
                patch.object(cleanup, "delete_run") as delete:
            with self.assertRaisesRegex(ValueError, "target changed"):
                cleanup.apply(ctx, "runs", report, report["digest"])
            delete.assert_not_called()

    def test_exact_lease_and_readback(self):
        target = {"branch": "test/one", "head": "a" * 40}
        ctx = {"repo": "example/test", "default": "main"}
        from types import SimpleNamespace
        with patch.object(cleanup, "git_command", return_value=SimpleNamespace(stdout="")) as git:
            cleanup.delete_branch(ctx, target)
            calls = git.call_args_list
            self.assertEqual(calls[1].args, ("example/test", "push",
                             "--force-with-lease=refs/heads/test/one:" + "a" * 40,
                             "https://github.com/example/test.git", ":refs/heads/test/one"))
            self.assertEqual(calls[2].args[-1], "refs/heads/test/one")

    def test_failed_auto_history_blocks(self):
        ctx = {"root": "repos/example/test", "automatic": True, "since": "2026-01-01T00:00:00Z", "run": 2}
        runs = [{"id": 1, "created_at": "2026-01-01T00:01:00Z", "status": "completed", "conclusion": "failure"},
                {"id": 2, "created_at": "2026-01-01T00:02:00Z", "status": "in_progress"}]
        with patch.object(cleanup, "counted", return_value=runs):
            with self.assertRaisesRegex(ValueError, "owner review"):
                cleanup.auto_history(ctx)

    def test_non_actions_context_no_io(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(cleanup, "api") as api:
            with self.assertRaisesRegex(ValueError, "trusted-main"):
                cleanup.context("example/test", "runs", None)
            api.assert_not_called()
