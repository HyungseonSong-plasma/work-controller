"""Bounded Work Unit publisher. No shell tasks, force pushes, merge or completion."""
import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch

from validation import approval, authority
from validation.completion import declared_works
from validation.model import api, pages, digest, REPO, SHA
from validation.pr import work_number, work_errors


def write_json(endpoint, payload):
    completed = subprocess.run(["gh", "api", "--hostname", "github.com", endpoint,
                                "-X", "POST", "--input", "-"],
                               input=json.dumps(payload), text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)


def validate_plan(plan, work):
    if not isinstance(plan, dict) or set(plan) != {"title", "body", "files"}:
        raise ValueError("Plan requires exactly title, body and files")
    title, body, files = plan["title"], plan["body"], plan["files"]
    if not isinstance(title, str) or not title.strip() or len(title) > 256 or "\n" in title:
        raise ValueError("Invalid PR title")
    if not isinstance(body, str) or work_number(body) != work:
        raise ValueError("PR body must use the canonical template and reference this Work")
    if not isinstance(files, list) or not files:
        raise ValueError("Expected at least one file change")
    paths = []
    for f in files:
        if not isinstance(f, dict) or set(f) != {"path", "content"}:
            raise ValueError("Every file requires path and content (null for deletion)")
        path, content = f["path"], f["content"]
        if (not authority.path_valid(path) or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or any(p.casefold() == ".git" for p in path.split("/"))):
            raise ValueError("Invalid repository path")
        if content is not None:
            if not isinstance(content, str) or "\x00" in content:
                raise ValueError("Only UTF-8 text files or null deletion are supported")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate paths")
    if any(a != b and b.startswith(a + "/") for a in paths for b in paths):
        raise ValueError("Overlapping change paths")
    return sorted(paths)


def git_blob_sha(content):
    data = content.encode("utf-8")
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def tree_entries(plan, base_tree):
    if base_tree.get("truncated") is not False or not isinstance(base_tree.get("tree"), list):
        raise ValueError("Complete trusted tree required")
    existing = {f["path"]: f for f in base_tree["tree"]}
    entries = []
    for f in sorted(plan["files"], key=lambda f: f["path"]):
        path, content = f["path"], f["content"]
        old = existing.get(path)
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent = existing.get("/".join(parts[:i]))
            if parent and parent.get("type") != "tree":
                raise ValueError("Non-directory ancestor: " + path)
        if old and (old.get("type") != "blob" or old.get("mode") not in {"100644", "100755"}):
            raise ValueError("Only ordinary file changes allowed: " + path)
        if content is None:
            if not old:
                raise ValueError("Cannot delete missing path: " + path)
            entries.append({"path": path, "type": "blob", "mode": old["mode"], "sha": None})
        else:
            if old and old.get("sha") == git_blob_sha(content):
                raise ValueError("No-op file change: " + path)
            entries.append({"path": path, "type": "blob", "mode": old["mode"] if old else "100644",
                            "content": content})
    return entries


def check_context(repo, work, plan):
    if not REPO.fullmatch(repo) or type(work) is not int or work < 1:
        raise ValueError("Expected owner/repo and positive Work number")
    paths = validate_plan(plan, work)
    main = api(f"repos/{repo}/branches/main")["commit"]
    base, tree = main["sha"], main["commit"]["tree"]["sha"]
    if not SHA.fullmatch(base) or not SHA.fullmatch(tree):
        raise ValueError("Invalid main revision")
    issue = api(f"repos/{repo}/issues/{work}")
    problems = work_errors(work, issue)
    if problems:
        raise ValueError("; ".join(problems))
    for p in pages(f"repos/{repo}/pulls?state=all&per_page=100"):
        if work in declared_works(p.get("body")) and (p["state"] == "open" or p.get("merged_at")):
            raise ValueError("v0.1 requires PLAN_WORK with no active or delivered Work Unit")
    policy = authority.read_policy(repo, base)
    classification = authority.classify(policy, paths)
    grade = classification["grade"]
    evidence = {"state": "NOT_REQUIRED", "allowed": True, "reason": "C-grade paths"}
    if grade == "A":
        evidence = {"state": "USER_ONLY", "allowed": False, "reason": "A-grade paths require owner-only change"}
    elif grade == "B":
        evidence = approval.read_state(repo, work, policy, approval.b_paths(classification))
    current = api(f"repos/{repo}/issues/{work}")
    if any(current.get(k) != issue.get(k) for k in ("title", "body", "state", "updated_at")) or approval.labels(current) != approval.labels(issue):
        raise ValueError("Work changed during planning")
    snapshot = {"repository": repo.casefold(), "work": work, "base": base, "base_tree": tree,
                "title": issue["title"], "body": issue["body"], "policy": policy,
                "plan": plan, "classification": classification, "approval": evidence}
    return {"work": work, "base": base, "base_tree": tree, "grade": grade,
            "paths": classification["paths"], "approval": evidence, "allowed": evidence["allowed"],
            "plan_digest": digest(snapshot)}


def prepare(repo, work, plan):
    context = check_context(repo, work, plan)
    # A Git tree is immutable: validate the payload once against the pinned base tree.
    context["entries"] = tree_entries(plan, api(f"repos/{repo}/git/trees/{context['base_tree']}?recursive=1")) if context["allowed"] else []
    return context


def public_summary(prepared):
    return {k: v for k, v in prepared.items() if k != "entries"}


def run(repo, work, plan, execute=False, reviewed_digest=None):
    """Dry-run by default. Writes are limited to Git objects, new ref and canonical PR."""
    phase, branch, commit = "preflight", None, None
    try:
        prepared = prepare(repo, work, plan)
        summary = public_summary(prepared)
        if not prepared["allowed"] or not execute:
            return dict(summary, state=prepared["approval"]["state"] if not prepared["allowed"] else "READY",
                        executed=False)
        if not reviewed_digest or reviewed_digest != prepared["plan_digest"]:
            raise ValueError("Reviewed plan digest missing or stale")
        branch = f"work/{work}-{reviewed_digest[:16]}"
        if pages(f"repos/{repo}/git/matching-refs/heads/{branch}"):
            raise ValueError("Execution branch already exists; human review before retry")

        def recheck():
            fresh = check_context(repo, work, plan)
            if not fresh["allowed"] or fresh["plan_digest"] != reviewed_digest:
                raise ValueError("Work, plan, base or permission changed; stop execution")

        phase = "create_tree"
        tree = write_json(f"repos/{repo}/git/trees", {"base_tree": prepared["base_tree"],
                                                       "tree": prepared["entries"]})["sha"]
        if not SHA.fullmatch(tree):
            raise ValueError("Invalid tree response")
        phase = "create_commit"
        commit = write_json(f"repos/{repo}/git/commits", {"message": f"Work #{work}: {plan['title']}",
                                                           "tree": tree, "parents": [prepared["base"]]})["sha"]
        if not SHA.fullmatch(commit):
            raise ValueError("Invalid commit response")
        phase = "pre_ref_recheck"
        recheck()
        phase = "publish_branch"
        ref = write_json(f"repos/{repo}/git/refs", {"ref": "refs/heads/"+branch, "sha": commit})
        if ref.get("ref") != "refs/heads/"+branch or ref.get("object", {}).get("sha") != commit:
            raise ValueError("Unexpected branch response")
        phase = "pre_pr_recheck"
        recheck()
        phase = "create_pr"
        pr = write_json(f"repos/{repo}/pulls", {"title": plan["title"], "body": plan["body"],
                                                  "head": branch, "base": "main"})
        if (type(pr.get("number")) is not int or pr.get("head", {}).get("sha") != commit
                or pr.get("base", {}).get("ref") != "main"):
            raise ValueError("Unexpected PR response; inspect repository before retry")
        return dict(summary, state="PUBLISHED", executed=True, branch=branch, commit=commit,
                    pr=pr["number"], url=pr.get("html_url"))
    except (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        return {"work": work, "state": "HUMAN_REVIEW", "allowed": False, "executed": False,
                "phase": phase, "branch": branch, "commit": commit, "reason": str(exc),
                "partial_write_possible": phase != "preflight",
                "retry": "Inspect repository and obtain fresh review before any retry"}


class ExecutorTests(unittest.TestCase):
    repo = "owner/repo"
    work = 4
    base = "a"*40
    tree = "b"*40
    commit = "c"*40
    issue = {"number": 4, "state": "open", "title": "Work",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\nX\n\n### Acceptance criteria\n\nX passes", "labels": []}
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
            with patch(__name__+".api", side_effect=self.read), patch(__name__+".pages",return_value=[]), patch.object(authority,"read_policy",return_value=self.policy), patch.object(approval,"read_state",return_value={"state":state,"allowed":allowed,"reason":"test"}), patch(__name__+".write_json") as write:
                actual = run(self.repo,self.work,self.plan(path))
                self.assertEqual(actual["allowed"],allowed)
                self.assertEqual(actual["state"],"READY" if allowed else state)
                write.assert_not_called()
                if not allowed:
                    self.assertFalse(run(self.repo,self.work,self.plan(path),True,"x")["allowed"])
                    write.assert_not_called()

    def test_validation(self):
        for path in ("../x", ".git/config", "a//b", "/file", "x\ny"):
            with self.assertRaises(ValueError):
                validate_plan(self.plan(path), self.work)
        for content in (None, "same"):
            p = self.plan(content=content)
            if content is None:
                with self.assertRaises(ValueError):
                    tree_entries(p,{"truncated":False,"tree":[]})
            else:
                with self.assertRaises(ValueError):
                    tree_entries(p,{"truncated":False,"tree":[{"path":"README.md","mode":"100644","type":"blob","sha":git_blob_sha(content)}]})
        with self.assertRaises(ValueError):
            tree_entries(self.plan(), {"truncated":True,"tree":[]})
        with self.assertRaises(ValueError):
            tree_entries(self.plan("link/a"),{"truncated":False,"tree":[{"path":"link","type":"blob","mode":"120000"}]})
        with self.assertRaises(ValueError):
            validate_plan(dict(self.plan(),body=self.plan()["body"].replace("#4","#5")),self.work)

    def test_publish_and_stale(self):
        plan = self.plan()
        with patch(__name__+".api",side_effect=self.read), patch(__name__+".pages",return_value=[]), patch.object(authority,"read_policy",return_value=self.policy):
            prepared = prepare(self.repo,self.work,plan)
            d = prepared["plan_digest"]
            branch = f"work/4-{d[:16]}"
            responses = [{"sha":self.tree},{"sha":self.commit},{"ref":"refs/heads/"+branch,"object":{"sha":self.commit}},
                         {"number":5,"head":{"sha":self.commit},"base":{"ref":"main"}}]
            with patch(__name__+".write_json",side_effect=responses) as write:
                self.assertEqual(run(self.repo,self.work,plan,True,d)["state"],"PUBLISHED")
                self.assertEqual([c.args[0] for c in write.call_args_list],
                                 [f"repos/{self.repo}/git/trees",f"repos/{self.repo}/git/commits",f"repos/{self.repo}/git/refs",f"repos/{self.repo}/pulls"])
            with patch(__name__+".write_json") as write:
                self.assertEqual(run(self.repo,self.work,self.plan(content="changed"),True,d)["state"],"HUMAN_REVIEW")
                write.assert_not_called()
            with patch(__name__+".write_json",side_effect=[{"sha":self.tree},RuntimeError("failed")]) as write:
                result=run(self.repo,self.work,plan,True,d)
                self.assertEqual(result["phase"],"create_commit")
                self.assertTrue(result["partial_write_possible"])
                self.assertEqual(write.call_count,2)

    def test_recheck_revocation_and_api_failure(self):
        plan=self.plan("app/cli.py")
        with patch(__name__+".api",side_effect=self.read),patch(__name__+".pages",return_value=[]),patch.object(authority,"read_policy",return_value=self.policy):
            with patch.object(approval,"read_state",return_value={"state":"APPROVED","allowed":True,"reason":"approved"}):
                d=prepare(self.repo,self.work,plan)["plan_digest"]
            yes={"state":"APPROVED","allowed":True,"reason":"approved"}
            no={"state":"REVOKED","allowed":False,"reason":"revoked"}
            with patch.object(approval,"read_state",side_effect=[yes,no]),patch(__name__+".write_json",side_effect=[{"sha":self.tree},{"sha":self.commit}]) as write:
                result=run(self.repo,self.work,plan,True,d)
                self.assertEqual(result["phase"],"pre_ref_recheck")
                self.assertEqual(write.call_count,2)
        with patch(__name__+".api",side_effect=RuntimeError("unavailable")),patch(__name__+".write_json") as write:
            self.assertEqual(run(self.repo,self.work,plan)["state"],"HUMAN_REVIEW")
            write.assert_not_called()

    def test_tree_is_read_once_and_context_rechecked(self):
        plan = self.plan()
        with patch(__name__+".api",side_effect=self.read) as read, patch(__name__+".pages",return_value=[]), patch.object(authority,"read_policy",return_value=self.policy):
            d = prepare(self.repo,self.work,plan)["plan_digest"]
            read.reset_mock()
            branch = f"work/4-{d[:16]}"
            with patch(__name__+".write_json",side_effect=[{"sha":self.tree},{"sha":self.commit},
                        {"ref":"refs/heads/"+branch,"object":{"sha":self.commit}},
                        {"number":5,"head":{"sha":self.commit},"base":{"ref":"main"}}]):
                self.assertEqual(run(self.repo,self.work,plan,True,d)["state"],"PUBLISHED")
            self.assertEqual(sum("/git/trees/" in c.args[0] for c in read.call_args_list),1)
            self.assertEqual(sum(c.args[0].endswith("/branches/main") for c in read.call_args_list),3)
