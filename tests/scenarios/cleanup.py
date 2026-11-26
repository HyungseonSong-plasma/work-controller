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
        pages = [{"total_count": 2, "jobs": [{"id": 1}]},
                 {"total_count": 2, "jobs": [{"id": 1}]}]
        with patch.object(cleanup, "api", return_value=pages) as api, \
                patch.object(cleanup.time, "sleep") as sleep, patch("builtins.print") as log:
            with self.assertRaisesRegex(ValueError, r"Duplicate.*duplicate_ids=\[1\].*attempt=3/3"):
                cleanup.counted("example", "jobs")
            self.assertEqual(api.call_count, 3)
            self.assertEqual([c.args for c in sleep.call_args_list], [(1,), (2,)])
            self.assertIn("endpoint=example, field=jobs", log.call_args.args[0])

    def test_counted_duplicate_recovers_without_old_pages(self):
        old = [{"total_count": 2, "jobs": [{"id": 99}, {"id": 99}]}]
        new = [{"total_count": 1, "jobs": [{"id": 2}]}]
        with patch.object(cleanup, "api", side_effect=[old, new]) as api, \
                patch.object(cleanup.time, "sleep") as sleep, patch("builtins.print"):
            self.assertEqual(cleanup.counted("example", "jobs"), [{"id": 2}])
            self.assertEqual(api.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_listed_duplicate_recovers_and_exhausts(self):
        old = [[{"name": "old"}], [{"name": "old"}]]
        new = [[{"name": "fresh"}]]
        with patch.object(cleanup, "api", side_effect=[old, new]) as api, \
                patch.object(cleanup.time, "sleep") as sleep, patch("builtins.print") as log:
            self.assertEqual(cleanup.listed("branches", "name"), [{"name": "fresh"}])
            self.assertEqual(api.call_count, 2)
            sleep.assert_called_once_with(1)
            self.assertIn("page_counts=[1, 1]", log.call_args.args[0])
        with patch.object(cleanup, "api", return_value=old) as api, \
                patch.object(cleanup.time, "sleep"), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "Duplicate.*endpoint=branches.*attempt=3/3"):
                cleanup.listed("branches", "name")
            self.assertEqual(api.call_count, 3)

    def test_malformed_identities_do_not_retry(self):
        for value in (True, 0, -1, None, "1", [], {}):
            with self.subTest(value=value), patch.object(cleanup, "api", return_value=[
                    {"total_count": 2, "jobs": [{"id": 1}, {"id": value}]}]) as api, \
                    patch.object(cleanup.time, "sleep") as sleep:
                with self.assertRaisesRegex(ValueError, "Invalid enumeration identity"):
                    cleanup.counted("example", "jobs")
                api.assert_called_once()
                sleep.assert_not_called()
        for value in ("", None, 1, []):
            with self.subTest(name=value), patch.object(cleanup, "api", return_value=[[{"name": value}]]) as api, \
                    patch.object(cleanup.time, "sleep") as sleep:
                with self.assertRaisesRegex(ValueError, "Invalid enumeration identity"):
                    cleanup.listed("branches", "name")
                api.assert_called_once()
                sleep.assert_not_called()

    def test_terminal_status_overlap_restarts_all_queries(self):
        ctx = {"root": "repos/example/test"}
        with patch.object(cleanup, "counted", side_effect=[
                [{"id": 99}], [{"id": 99}], [{"id": 2}], [{"id": 1}]]) as counted, \
                patch.object(cleanup.time, "sleep") as sleep, patch("builtins.print") as log:
            self.assertEqual(cleanup.terminal_runs(ctx), [{"id": 1}, {"id": 2}])
            self.assertEqual([c.args[0] for c in counted.call_args_list], [
                ctx["root"] + "/actions/runs?status=cancelled&per_page=100",
                ctx["root"] + "/actions/runs?status=skipped&per_page=100"] * 2)
            sleep.assert_called_once_with(1)
            self.assertIn("terminal status counts=", log.call_args.args[0])

    def test_duplicate_status_exhaustion_blocks_apply(self):
        ctx = {"root": "repos/example/test"}
        with patch.object(cleanup, "context", return_value=ctx), \
                patch.object(cleanup, "catalog", return_value={}), \
                patch.object(cleanup, "counted", return_value=[{"id": 1}]) as counted, \
                patch.object(cleanup.time, "sleep"), patch("builtins.print"), \
                patch.object(cleanup, "apply") as apply, \
                patch.object(cleanup, "delete_run") as delete:
            with self.assertRaisesRegex(ValueError, "Duplicate.*terminal status counts=.*attempt=3/3"):
                cleanup.run("example/test", "runs", "a" * 64)
            self.assertEqual(counted.call_count, 6)
            apply.assert_not_called()
            delete.assert_not_called()

    def test_duplicate_listed_exhaustion_blocks_apply(self):
        ctx = {"root": "repos/example/test"}
        with patch.object(cleanup, "context", return_value=ctx), \
                patch.object(cleanup, "api", return_value=[[{"number": 1}, {"number": 1}]]) as api, \
                patch.object(cleanup.time, "sleep"), patch("builtins.print"), \
                patch.object(cleanup, "apply") as apply, \
                patch.object(cleanup, "delete_branch") as delete:
            with self.assertRaisesRegex(ValueError, "Duplicate.*pulls.*attempt=3/3"):
                cleanup.run("example/test", "branches", "a" * 64)
            self.assertEqual(api.call_count, 3)
            apply.assert_not_called()
            delete.assert_not_called()

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


    def test_delegation_command_exact(self):
        command = "/execute-cleanup 123 456 branches " + "a" * 64 + " " + "b" * 40
        self.assertEqual(cleanup.parse_delegation(command), (123, 456, "branches", "a" * 64, "b" * 40))
        for value in (command + "\n", command.replace("branches", "tags"), command.replace("123", "0123"),
                      command.replace("a" * 64, "A" * 64), command + " extra"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cleanup.parse_delegation(value)

    def test_delegation_disabled_no_io(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(cleanup.operation, "event_context") as event, \
                patch.object(cleanup, "apply") as apply:
            with self.assertRaisesRegex(ValueError, "disabled"):
                cleanup.delegated_execute("example/test", "branches")
            event.assert_not_called()
            apply.assert_not_called()

    def test_comment_job_and_rerun_rejected(self):
        with patch.dict("os.environ", {"GITHUB_JOB": "wrong"}, clear=True), \
                patch.object(cleanup.operation, "event_context") as event:
            with self.assertRaisesRegex(ValueError, "job mismatch"):
                cleanup.comment_context("example/test", cleanup.PLAN_COMMAND, "delegated-plan")
            event.assert_not_called()
        with patch.dict("os.environ", {"GITHUB_JOB": "delegated-plan", "GITHUB_TRIGGERING_ACTOR": "owner"}), \
                patch.object(cleanup.operation, "event_context", return_value=({}, "a"*40, "owner", 1, 2)), \
                patch.object(cleanup.operation, "request_identity") as identity:
            with self.assertRaisesRegex(ValueError, "initial"):
                cleanup.comment_context("example/test", cleanup.PLAN_COMMAND, "delegated-plan")
            identity.assert_not_called()

    def test_delegation_history_replay(self):
        ctx = {"root": "repos/example/test", "run": 2}
        earlier = {"id": 1, "run_attempt": 1, "created_at": "2026-01-01T00:01:00Z", "status": "completed"}
        current = {"id": 2, "run_attempt": 1, "created_at": "2026-01-01T00:02:00Z", "status": "in_progress"}
        for conclusion in ("success", "failure", "cancelled"):
            with self.subTest(conclusion=conclusion), patch.object(cleanup, "counted", side_effect=[
                    [earlier, current], [{"name": "delegated-branches", "conclusion": conclusion}]]):
                with self.assertRaisesRegex(ValueError, "consumed"):
                    cleanup.guard_delegated_history(ctx, "2026-01-01T00:00:00Z")
        with patch.object(cleanup, "counted", side_effect=[
                [earlier, current], [{"name": "delegated-plan", "conclusion": "success"}]]):
            cleanup.guard_delegated_history(ctx, "2026-01-01T00:00:00Z")
        with patch.object(cleanup, "counted", return_value=[earlier]):
            with self.assertRaisesRegex(ValueError, "missing"):
                cleanup.guard_delegated_history(ctx, "2026-01-01T00:00:00Z")

    def test_delegated_execute_exact_kind_and_manifest(self):
        base = "b"*40
        ctx = {"base": base, "run": 22, "automatic": False, "since": ""}
        identity = {"base": base, "run": 22}
        for kind in ("branches", "runs"):
            reviewed = {"targets": [{"id": 1}], "kind": kind}
            checksum = digest(reviewed)
            command = f"/execute-cleanup 10 11 {kind} {checksum} {base}"
            event = {"comment": {"body": command}}
            fresh = {"manifest": reviewed, "digest": checksum}
            with self.subTest(kind=kind), patch.dict("os.environ", {"AGENT_CLEANUP_DELEGATION": "v1"}), \
                    patch.object(cleanup.operation, "event_context", return_value=(event, base, "owner", 22, 1)), \
                    patch.object(cleanup, "comment_context", return_value=identity), \
                    patch.object(cleanup, "context", return_value=ctx), \
                    patch.object(cleanup, "verify_plan", return_value=(reviewed, "2026-01-01T00:00:00Z")) as verify, \
                    patch.object(cleanup, "guard_delegated_history"), \
                    patch.object(cleanup, "manifest", return_value=fresh) as manifest, \
                    patch.object(cleanup, "apply", return_value={"state": "CLEANED", "deleted": []}) as apply, \
                    patch("builtins.print"):
                result = cleanup.delegated_execute("example/test", kind)
                self.assertEqual(result["kind"], kind)
                self.assertEqual(result["plan_job"], 11)
                self.assertEqual(verify.call_count, 2)
                apply.assert_called_once()
                apply.reset_mock()
                manifest.return_value = {"manifest": {"targets": []}, "digest": checksum}
                with self.assertRaisesRegex(ValueError, "manifest changed"):
                    cleanup.delegated_execute("example/test", kind)
                apply.assert_not_called()
                with self.assertRaisesRegex(ValueError, "kind mismatch"):
                    cleanup.delegated_execute("example/test", "runs" if kind == "branches" else "branches")
                apply.assert_not_called()
                verify.side_effect = ValueError("artifact integrity")
                with self.assertRaisesRegex(ValueError, "artifact integrity"):
                    cleanup.delegated_execute("example/test", kind)
                apply.assert_not_called()

    def test_verify_plan_evidence_bindings(self):
        now = cleanup.dt.datetime.now(cleanup.dt.timezone.utc).replace(microsecond=0)
        at = lambda seconds: (now - cleanup.dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = "b"*40
        ctx = {"root": "repos/example/test", "repo": "example/test", "owner": "owner", "id": 1, "base": base}
        identity = {"work": 102, "comment": 21, "created_at": at(5)}
        run = {"id": 10, "path": cleanup.WORKFLOW, "event": "issue_comment", "status": "completed",
               "conclusion": "success", "run_attempt": 1, "head_branch": "main", "head_sha": base,
               "repository": {"id": 1}, "head_repository": {"id": 1}, "actor": {"login": "owner"},
               "triggering_actor": {"login": "owner"}, "created_at": at(60)}
        job = {"id": 11, "name": "delegated-plan", "run_id": 10, "run_attempt": 1, "head_sha": base,
               "status": "completed", "conclusion": "success", "started_at": at(50), "completed_at": at(10)}
        source = {"id": 20, "issue_url": "https://api.github.com/repos/example/test/issues/102",
                  "body": cleanup.PLAN_COMMAND, "user": {"login": "owner"}, "created_at": at(65), "updated_at": at(65)}
        reports = {}
        for kind in ("branches", "runs"):
            plan = {"repository": "example/test", "base": base, "kind": kind, "since": "",
                    "targets": [{"id": 1}] if kind == "branches" else []}
            reports[kind] = {"state": "PLANNED", "mode": "read-only", "automatic": False,
                             "manifest": plan, "digest": digest(plan)}
        report = {"repository": "example/test", "state": "PLANNED", "mode": "read-only", "execution": None,
                  "request": {"work": 102, "run": 10, "attempt": 1, "base": base, "comment": 20,
                              "command": cleanup.PLAN_COMMAND, "actor": "owner", "created_at": at(65)},
                  "generated_at": at(15), "reports": reports}
        checksum = reports["branches"]["digest"]
        import copy
        cases = [("valid", None, None), ("actor", ("run", "actor"), {"login": "other"}),
                 ("ref", ("run", "head_branch"), "other"), ("job", ("job", "name"), "branches"),
                 ("job id", ("job", "id"), 12), ("run id", ("run", "id"), 12),
                 ("attempt", ("run", "run_attempt"), 2), ("stale", ("job", "completed_at"), at(1900)),
                 ("work", ("request", "work"), 103), ("edited", ("source", "updated_at"), at(4)),
                 ("digest", ("branch_report", "digest"), "a"*64),
                 ("mode", ("report", "mode"), "write"), ("base", ("request", "base"), "c"*40)]
        for label, key, value in cases:
            rr, jj, ss, evidence = copy.deepcopy((run, job, source, report))
            parts = {"run": rr, "job": jj, "source": ss, "report": evidence,
                     "request": evidence["request"], "branch_report": evidence["reports"]["branches"]}
            if key:
                parts[key[0]][key[1]] = value
            with self.subTest(label=label), patch.object(cleanup, "api", side_effect=[rr, ss]), \
                    patch.object(cleanup, "counted", return_value=[jj]), \
                    patch.object(cleanup.review, "read_review_artifact", return_value=evidence):
                if key is None:
                    self.assertEqual(cleanup.verify_plan(ctx, identity, 10, 11, "branches", checksum),
                                     (reports["branches"]["manifest"], at(10)))
                else:
                    with self.assertRaises(ValueError):
                        cleanup.verify_plan(ctx, identity, 10, 11, "branches", checksum)
        with patch.object(cleanup, "api", side_effect=[run, source]), \
                patch.object(cleanup, "counted", return_value=[job]), \
                patch.object(cleanup.review, "read_review_artifact", return_value=report):
            with self.assertRaisesRegex(ValueError, "empty targets"):
                cleanup.verify_plan(ctx, identity, 10, 11, "runs", reports["runs"]["digest"])


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
