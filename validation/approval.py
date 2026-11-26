"""Recorded B-rank approval evidence. Consent is attested, not independently authenticated."""
import json
import re
import unittest
from unittest.mock import patch

from . import authority
from .model import api, pages, PLACEHOLDERS
from .pr import work_number, work_errors

HEADER = "### B-rank Approval"
DIGEST = re.compile(r"[0-9a-f]{64}")


def format_record(paths, scope_digest, user_response):
    if (not isinstance(paths, list) or not paths or any(not authority.path_valid(p) for p in paths)
            or paths != sorted(set(paths)) or not DIGEST.fullmatch(scope_digest)
            or not isinstance(user_response, str) or not user_response.strip()
            or user_response.strip().casefold() in PLACEHOLDERS):
        raise ValueError("Expected sorted unique B paths, valid digest and explicit user response")
    return HEADER + "\n\n```json\n" + json.dumps(
        {"paths": paths, "digest": scope_digest, "user_response": user_response.strip()},
        ensure_ascii=False, indent=2) + "\n```"


def parse_record(comment, owner):
    if ((comment.get("user") or {}).get("login", "").casefold() != owner.casefold()
            or not comment.get("created_at") or comment["created_at"] != comment.get("updated_at")):
        return None
    body = (comment.get("body") or "").replace("\r\n", "\n").strip()
    match = re.fullmatch(re.escape(HEADER) + r"\n\n```json\n([\s\S]+)\n```", body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        if set(data) != {"paths", "digest", "user_response"}:
            return None
        format_record(data["paths"], data["digest"], data["user_response"])
    except (ValueError, TypeError, KeyError):
        return None
    return data


def matching_evidence(comments, owner, paths, scope_digest):
    for comment in sorted(comments, key=lambda c: c["id"], reverse=True):
        record = parse_record(comment, owner)
        if record and record["paths"] == paths and record["digest"] == scope_digest:
            return comment["id"]
    return None


def b_paths(classification):
    return sorted(p for p, grade in classification["paths"].items() if grade == "B")


def record(repo, work, paths, approved_digest, user_response):
    """Caller must have obtained real user consent before calling; never infer approval."""
    plan = authority.plan_paths(repo, work, paths)
    if plan["grade"] != "B" or plan["approval_digest"] != approved_digest:
        raise ValueError("B approval scope changed or is not B; request approval again")
    policy = authority.read_policy(repo, plan["base"])
    body = format_record(b_paths(plan), approved_digest, user_response)
    comment = api(f"repos/{repo}/issues/{work}/comments", "-X", "POST", "-f", "body=" + body)
    if parse_record(comment, policy["owner"]) is None:
        raise ValueError("Recorded comment does not meet approval evidence contract")
    return {"comment_id": comment["id"], "approval_digest": approved_digest}


def inspect_pr(repo, number, expected_head=None, expected_base=None):
    """Verify B evidence only. A protection remains the separate grade-a gate."""
    pr = api(f"repos/{repo}/pulls/{number}")
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    if (expected_head and head != expected_head) or (expected_base and base != expected_base):
        raise ValueError("PR revision changed before B approval check")
    classification = authority.inspect_pr(repo, number, head, base, grade_a_only=True)
    paths = b_paths(classification)
    result = {"grade": classification["grade"], "paths": paths, "head": head, "base": base,
              "allowed": True, "reason": "No B-grade paths changed"}
    if paths:
        work = work_number(pr.get("body"))
        issue = api(f"repos/{repo}/issues/{work}")
        policy = authority.read_policy(repo, base)
        scope = authority.approval_scope(repo, work, issue, policy, classification)
        comments = pages(f"repos/{repo}/issues/{work}/comments?per_page=100")
        evidence = matching_evidence(comments, policy["owner"], paths, scope)
        result.update(work=work, approval_digest=scope, comment_id=evidence, allowed=evidence is not None,
                      reason="Matching recorded B approval" if evidence is not None else "Missing matching B approval evidence")
        fresh_issue = api(f"repos/{repo}/issues/{work}")
        if authority.approval_scope(repo, work, fresh_issue, policy, classification) != scope:
            raise ValueError("Work changed during B approval check")
    fresh = api(f"repos/{repo}/pulls/{number}")
    if (fresh["state"] != "open" or fresh["head"]["sha"] != head or fresh["base"]["sha"] != base
            or fresh.get("body") != pr.get("body") or fresh["base"]["ref"] != "main"):
        raise ValueError("PR changed during B approval check")
    return result


class ApprovalTests(unittest.TestCase):
    paths = ["app/cli.py"]
    scope = "a"*64
    owner = "owner"
    issue = {"number": 3, "title": "Work", "state": "open",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\nX only\n\n### Acceptance criteria\n\nX passes"}
    policy = {"owner": "owner", "A": [], "B": ["app/", "validation/"], "default": "C"}
    pr = {"state": "open", "head": {"sha": "b"*40}, "base": {"sha": "c"*40, "ref": "main"},
          "body": "### Work\n\nRefs #3\n\n### Changes\n\nX\n\n### Verification\n\nTests pass"}

    def comment(self, paths=None, scope=None):
        return {"id": 9, "user": {"login": self.owner}, "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "body": format_record(paths or self.paths, scope or self.scope, "Approved")}

    def test_evidence(self):
        c = self.comment()
        self.assertEqual(matching_evidence([c], self.owner, self.paths, self.scope), 9)
        for bad in (dict(c, user={"login": "other"}), dict(c, updated_at="later"),
                    dict(c, body="ordinary"), dict(c, body=c["body"]+" extra"),
                    self.comment(scope="b"*64), self.comment(paths=["app/cli.py", "validation/pr.py"])):
            self.assertIsNone(matching_evidence([bad], self.owner, self.paths, self.scope))
        self.assertIsNone(matching_evidence([], self.owner, self.paths, self.scope))
        for paths in ([], ["../bad"], ["app/cli.py", "app/cli.py"]):
            with self.assertRaises(ValueError):
                format_record(paths, self.scope, "Approved")
        with self.assertRaises(ValueError):
            format_record(self.paths, self.scope, " ")

    def test_scope_changes(self):
        classification = authority.classify(self.policy, self.paths)
        required = authority.approval_scope("owner/repo", 3, self.issue, self.policy, classification)
        c = self.comment(scope=required)
        for changed in (dict(self.issue, title="changed"), dict(self.issue, body=self.issue["body"]+" revised")):
            other = authority.approval_scope("owner/repo", 3, changed, self.policy, classification)
            self.assertIsNone(matching_evidence([c], self.owner, self.paths, other))
        other = authority.approval_scope("owner/repo", 3, self.issue, dict(self.policy, default="B"), classification)
        self.assertIsNone(matching_evidence([c], self.owner, self.paths, other))

    def test_inspect(self):
        for changed in (["README.md"], [".github/change-authority.json"], self.paths,
                        ["app/cli.py", "validation/pr.py"]):
            classification = authority.classify(self.policy, changed)
            scope = authority.approval_scope("owner/repo", 3, self.issue, self.policy, classification)
            for evidence in ([], [self.comment(scope=scope)]):
                with patch(__name__ + ".api", side_effect=[self.pr, self.issue, self.issue, self.pr] if b_paths(classification) else [self.pr, self.pr]), patch.object(authority, "inspect_pr", return_value=classification), patch.object(authority, "read_policy", return_value=self.policy), patch(__name__ + ".pages", return_value=evidence):
                    result = inspect_pr("owner/repo", 4)
                    self.assertEqual(result["allowed"], not b_paths(classification) or (bool(evidence) and changed == self.paths))

    def test_record(self):
        plan = {"grade": "B", "paths": {"app/cli.py": "B"}, "approval_digest": self.scope, "base": "c"*40}
        with patch.object(authority, "plan_paths", return_value=plan), patch.object(authority, "read_policy", return_value=self.policy), patch(__name__ + ".api", return_value=self.comment()) as call:
            with self.assertRaises(ValueError):
                record("owner/repo", 3, self.paths, "b"*64, "Approved")
            call.assert_not_called()
            self.assertEqual(record("owner/repo", 3, self.paths, self.scope, "Approved")["comment_id"], 9)
