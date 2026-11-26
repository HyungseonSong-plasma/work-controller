"""Cleanup I/O boundaries. Migrate these mocks to the #90 model."""
import unittest
from unittest.mock import patch

from controller import cleanup
from validation.model import digest


class CleanupTests(unittest.TestCase):
    def test_counted_incomplete(self):
        with patch.object(cleanup, "api", return_value=[{"total_count": 2, "jobs": [{"id": 1}]}]) as api, \
                patch.object(cleanup.time, "sleep") as sleep, patch("builtins.print") as log:
            with self.assertRaisesRegex(ValueError, r"truncated: example, field=jobs, .*attempt=3/3"):
                cleanup.counted("example", "jobs")
            self.assertEqual(api.call_count, 3)
            self.assertEqual([c.args for c in sleep.call_args_list], [(1,), (2,)])
            self.assertEqual(log.call_count, 2)

    def test_counted_recovers_with_fresh_pages(self):
        first = [{"total_count": 2, "jobs": [{"id": 99}]}]
        second = [{"total_count": 2, "jobs": [{"id": 1}]},
                  {"total_count": 2, "jobs": [{"id": 2}]}]
        with patch.object(cleanup, "api", side_effect=[first, second]) as api, \
                patch.object(cleanup.time, "sleep") as sleep, patch("builtins.print") as log:
            self.assertEqual(cleanup.counted("example", "jobs"), [{"id": 1}, {"id": 2}])
            self.assertEqual([c.args for c in api.call_args_list],
                             [("example", "--paginate", "--slurp")] * 2)
            sleep.assert_called_once_with(1)
            self.assertIn("counts=[(2, 1)]", log.call_args.args[0])

    def test_counted_restarts_inconsistent_page_totals(self):
        first = [{"total_count": 1, "jobs": [{"id": 1}]},
                 {"total_count": 2, "jobs": [{"id": 2}]}]
        second = [{"total_count": 0, "jobs": []}]
        with patch.object(cleanup, "api", side_effect=[first, second]) as api, \
                patch.object(cleanup.time, "sleep"), patch("builtins.print"):
            self.assertEqual(cleanup.counted("example", "jobs"), [])
            self.assertEqual(api.call_count, 2)

    def test_counted_duplicate(self):
        with patch.object(cleanup, "api", return_value=[{"total_count": 2, "jobs": [{"id": 1}, {"id": 1}]}]) as api, \
                patch.object(cleanup.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                cleanup.counted("example", "jobs")
            api.assert_called_once()
            sleep.assert_not_called()

    def test_counted_malformed_no_retry(self):
        for pages in ([], [{"total_count": -1, "jobs": []}],
                      [{"total_count": True, "jobs": []}], [{"total_count": 0}]):
            with self.subTest(pages=pages), patch.object(cleanup, "api", return_value=pages) as api, \
                    patch.object(cleanup.time, "sleep") as sleep:
                with self.assertRaisesRegex(ValueError, "Incomplete counted pagination"):
                    cleanup.counted("example", "jobs")
                api.assert_called_once()
                sleep.assert_not_called()

    def test_counted_exhausted_inventory_no_write(self):
        ctx = {"root": "repos/example/test"}
        for kind in ("branches", "runs"):
            with self.subTest(kind=kind), patch.object(cleanup, "context", return_value=ctx), \
                    patch.object(cleanup, "catalog", return_value={}), \
                    patch.object(cleanup, "api", return_value=[{"total_count": 1, "workflow_runs": []}]) as api, \
                    patch.object(cleanup.time, "sleep"), patch("builtins.print"), \
                    patch.object(cleanup, "apply") as apply, \
                    patch.object(cleanup, "delete_branch") as delete_branch, \
                    patch.object(cleanup, "delete_run") as delete_run:
                with self.assertRaisesRegex(ValueError, "attempt=3/3"):
                    cleanup.run("example/test", kind, "a" * 64)
                self.assertEqual(api.call_count, 3)
                apply.assert_not_called()
                delete_branch.assert_not_called()
                delete_run.assert_not_called()

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


class CleanupLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.ctx = {"repo": "example/test", "root": "repos/example/test", "id": 1,
                    "base": "b" * 40, "default": "main", "owner": "owner",
                    "since": "", "run": 999, "automatic": False}
        self.work = {"number": 10, "title": "Test", "state": "closed", "state_reason": "completed",
                     "closed_at": "2020-01-01T00:30:00Z",
                     "body": "### Work type\n\nImplementation\n\n### Objective\n\nTest\n\n### Scope\n\n#### Constraints\n\n- Local only.\n\n#### Actions\n\n- Verify.\n\n### Acceptance criteria\n\nVerified result."}
        body = "### Work\n\nRefs #10\n\n### Changes\n\nTest change.\n\n### Verification\n\nTest result."
        self.pull = {"number": 11, "id": 101, "title": "Merged unit", "state": "closed",
                     "body": body, "merged_at": "2020-01-01T00:10:00Z",
                     "closed_at": "2020-01-01T00:10:00Z", "merge_commit_sha": "c" * 40,
                     "head": {"repo": {"id": 1}, "ref": "work/test", "sha": "a" * 40},
                     "base": {"repo": {"id": 1, "full_name": "example/test"}, "ref": "main"}}
        self.branch = {"name": "work/test", "protected": False, "commit": {"sha": "a" * 40}}
        self.cat = {"pulls": [self.pull], "issues": {10: self.work},
                    "branches": {"work/test": self.branch}, "open_refs": set(), "open_heads": set()}

    def compare_or_rules(self, path):
        if "/compare/" in path:
            self.assertTrue(path.endswith("c" * 40 + "..." + "b" * 40))
            return {"status": "ahead", "behind_by": 0}
        if "/rules/branches/" in path:
            return []
        self.fail("Unexpected API: " + path)

    def test_squash_merged_branch(self):
        with patch.object(cleanup, "api", side_effect=self.compare_or_rules):
            target, reason = cleanup.branch_target(self.ctx, self.cat, self.branch, set())
        self.assertIsNone(reason)
        self.assertEqual(target["disposition"], "merged")
        self.assertEqual(target["head"], "a" * 40)
        self.assertEqual(target["merge_commit"], "c" * 40)

    def test_discarded_completed_work(self):
        discarded = dict(self.pull, number=12, id=102, merged_at=None, merge_commit_sha=None,
                         head=dict(self.pull["head"], ref="test/discard", sha="d" * 40))
        self.cat["pulls"].append(discarded)
        close = {"id": 1, "event": "closed", "created_at": discarded["closed_at"],
                 "actor": {"login": "owner"}}
        with patch.object(cleanup, "listed", return_value=[close]):
            target, reason = cleanup.terminal(self.ctx, self.cat, discarded)
        self.assertIsNone(reason)
        self.assertEqual(target["disposition"], "discarded")
        self.assertEqual(target["terminal_at"], self.work["closed_at"])
        with patch.object(cleanup, "listed", return_value=[dict(close, actor={"login": "other"})]):
            self.assertIsNone(cleanup.terminal(self.ctx, self.cat, discarded)[0])

    def test_missing_or_invalid_work(self):
        for w in (None, dict(self.work, body=""), dict(self.work, state_reason="not_planned"),
                  dict(self.work, pull_request={})):
            with self.subTest(work=w), patch.object(cleanup, "api") as api:
                self.cat["issues"] = {10: w} if w else {}
                self.assertIsNone(cleanup.terminal(self.ctx, self.cat, self.pull)[0])
                api.assert_not_called()

    def test_unreachable_merge_preserved(self):
        with patch.object(cleanup, "api", return_value={"status": "diverged", "behind_by": 1}):
            self.assertIsNone(cleanup.terminal(self.ctx, self.cat, self.pull)[0])

    def test_busy_and_open_ref_preserved(self):
        with patch.object(cleanup, "api") as api:
            self.assertIsNone(cleanup.branch_target(self.ctx, self.cat, self.branch, {"work/test"})[0])
            self.cat["open_refs"].add("work/test")
            self.assertIsNone(cleanup.branch_target(self.ctx, self.cat, self.branch, set())[0])
            api.assert_not_called()

    def test_reused_branch_preserved(self):
        self.cat["pulls"].append(dict(self.pull, number=12, id=102))
        with patch.object(cleanup, "api") as api:
            self.assertIsNone(cleanup.branch_target(self.ctx, self.cat, self.branch, set())[0])
            api.assert_not_called()

    def test_historical_cutoff(self):
        self.ctx["since"] = "2021-01-01T00:00:00Z"
        with patch.object(cleanup, "api", side_effect=self.compare_or_rules):
            self.assertIsNone(cleanup.terminal(self.ctx, self.cat, self.pull)[0])

    def candidate_run(self):
        return {"id": 12345, "path": ".github/workflows/pr-contract.yml", "event": "pull_request",
                "status": "completed", "conclusion": "cancelled", "run_attempt": 1,
                "repository": {"id": 1}, "head_repository": {"id": 1},
                "head_sha": "a" * 40, "head_branch": "work/test",
                "updated_at": "2020-01-01T00:20:00Z", "check_suite_id": 45,
                "pull_requests": [{"number": 11, "id": 101, "head": self.pull["head"],
                                   "base": self.pull["base"]}]}

    def test_disposable_run_and_cross_work_evidence(self):
        run = self.candidate_run()
        jobs = [{"id": 67890, "status": "completed", "conclusion": "cancelled", "run_attempt": 1}]
        with patch.object(cleanup, "api", side_effect=self.compare_or_rules), \
                patch.object(cleanup, "counted", side_effect=lambda path, field: jobs if field == "jobs" else []), \
                patch.object(cleanup, "listed", return_value=[]):
            target, reason = cleanup.run_target(self.ctx, self.cat, run)
            self.assertIsNone(reason)
            self.assertEqual(target["run"], 12345)
            self.assertEqual(target["jobs"], [67890])
            self.cat["issues"][20] = {"body": "Preserve run 12345 as acceptance evidence."}
            self.assertIsNone(cleanup.run_target(self.ctx, self.cat, run)[0])

    def test_empty_pr_association_preserved(self):
        run = dict(self.candidate_run(), pull_requests=[])
        with patch.object(cleanup, "api", return_value={"id": 45, "head_sha": "a" * 40,
                                                       "head_branch": "work/test", "pull_requests": []}), \
                patch.object(cleanup, "counted") as counted:
            target, reason = cleanup.run_target(self.ctx, self.cat, run)
            self.assertIsNone(target)
            self.assertIn("association", reason)
            counted.assert_not_called()
