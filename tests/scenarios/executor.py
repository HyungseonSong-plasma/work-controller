"""Executor scenarios: all publication calls are mocked."""
import unittest
from unittest.mock import patch
from controller import executor
from controller.executor import run, prepare
from validation import authority, approval


class ExecutorTests(unittest.TestCase):
    repo = "owner/repo"
    work = 4
    base = "a"*40
    tree = "b"*40
    commit = "c"*40
    issue = {"number": 4, "state": "open", "title": "Work",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n### Acceptance criteria\n\nX passes", "labels": []}
    policy = {"owner": "owner", "A": [], "B": ["app/"], "default": "C"}

    def plan(self, path="README.md", content="new"):
        return {"title": "Change", "body": "### Work\n\nRefs #4\n\n### Changes\n\nX\n\n### Verification\n\nTests pass",
                "files": [{"path": path, "content": content}]}

    def read(self, path):
        if path.endswith("/branches/main"):
            return {"commit": {"sha": self.base, "commit": {"tree": {"sha": self.tree}}}}
        if path.endswith("/issues/4"):
            return self.issue
        if "/git/trees/" in path:
            return {"truncated": False, "tree": []}
        raise AssertionError(path)

    def test_authority_and_no_writes(self):
        for path, state, allowed in [("README.md","NOT_REQUIRED",True), (authority.POLICY_PATH,"USER_ONLY",False),
                                     ("app/cli.py","PENDING",False),("app/cli.py","APPROVED",True),
                                     ("app/cli.py","REVOKED",False),("app/cli.py","HUMAN_REVIEW",False)]:
            with patch.object(executor, "api", side_effect=self.read), patch.object(executor, "pages",return_value=[]), patch.object(authority,"read_policy",return_value=self.policy), patch.object(approval,"read_state",return_value={"state":state,"allowed":allowed,"reason":"test"}), patch.object(executor, "write_json") as write:
                actual = run(self.repo,self.work,self.plan(path))
                self.assertEqual(actual["allowed"],allowed)
                self.assertEqual(actual["state"],"READY" if allowed else state)
                write.assert_not_called()
                if not allowed:
                    self.assertFalse(run(self.repo,self.work,self.plan(path),True,"x")["allowed"])
                    write.assert_not_called()

    def test_publish_and_stale(self):
        plan = self.plan()
        with patch.object(executor, "api",side_effect=self.read), patch.object(executor, "pages",return_value=[]), patch.object(authority,"read_policy",return_value=self.policy):
            prepared = prepare(self.repo,self.work,plan)
            d = prepared["plan_digest"]
            branch = f"work/4-{d[:16]}"
            responses = [{"sha":self.tree},{"sha":self.commit},{"ref":"refs/heads/"+branch,"object":{"sha":self.commit}},
                         {"number":5,"head":{"sha":self.commit},"base":{"ref":"main"}}]
            with patch.object(executor, "write_json",side_effect=responses) as write:
                self.assertEqual(run(self.repo,self.work,plan,True,d)["state"],"PUBLISHED")
                self.assertEqual([c.args[0] for c in write.call_args_list],
                                 [f"repos/{self.repo}/git/trees",f"repos/{self.repo}/git/commits",f"repos/{self.repo}/git/refs",f"repos/{self.repo}/pulls"])
            with patch.object(executor, "write_json") as write:
                self.assertEqual(run(self.repo,self.work,self.plan(content="changed"),True,d)["state"],"HUMAN_REVIEW")
                write.assert_not_called()
            with patch.object(executor, "write_json",side_effect=[{"sha":self.tree},RuntimeError("failed")]) as write:
                result=run(self.repo,self.work,plan,True,d)
                self.assertEqual(result["phase"],"create_commit")
                self.assertTrue(result["partial_write_possible"])
                self.assertEqual(write.call_count,2)

    def test_recheck_revocation_and_api_failure(self):
        plan=self.plan("app/cli.py")
        with patch.object(executor, "api",side_effect=self.read),patch.object(executor, "pages",return_value=[]),patch.object(authority,"read_policy",return_value=self.policy):
            with patch.object(approval,"read_state",return_value={"state":"APPROVED","allowed":True,"reason":"approved"}):
                d=prepare(self.repo,self.work,plan)["plan_digest"]
            yes={"state":"APPROVED","allowed":True,"reason":"approved"}
            no={"state":"REVOKED","allowed":False,"reason":"revoked"}
            with patch.object(approval,"read_state",side_effect=[yes,no]),patch.object(executor, "write_json",side_effect=[{"sha":self.tree},{"sha":self.commit}]) as write:
                result=run(self.repo,self.work,plan,True,d)
                self.assertEqual(result["phase"],"pre_ref_recheck")
                self.assertEqual(write.call_count,2)
        with patch.object(executor, "api",side_effect=RuntimeError("unavailable")),patch.object(executor, "write_json") as write:
            self.assertEqual(run(self.repo,self.work,plan)["state"],"HUMAN_REVIEW")
            write.assert_not_called()

    def test_tree_is_read_once_and_context_rechecked(self):
        plan = self.plan()
        with patch.object(executor, "api",side_effect=self.read) as read, patch.object(executor, "pages",return_value=[]), patch.object(authority,"read_policy",return_value=self.policy):
            d = prepare(self.repo,self.work,plan)["plan_digest"]
            read.reset_mock()
            branch = f"work/4-{d[:16]}"
            with patch.object(executor, "write_json",side_effect=[{"sha":self.tree},{"sha":self.commit},
                        {"ref":"refs/heads/"+branch,"object":{"sha":self.commit}},
                        {"number":5,"head":{"sha":self.commit},"base":{"ref":"main"}}]):
                self.assertEqual(run(self.repo,self.work,plan,True,d)["state"],"PUBLISHED")
            self.assertEqual(sum("/git/trees/" in c.args[0] for c in read.call_args_list),1)
            self.assertEqual(sum(c.args[0].endswith("/branches/main") for c in read.call_args_list),3)
