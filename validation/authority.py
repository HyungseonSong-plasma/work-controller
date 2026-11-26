"""User-owned A/B/C policy for the Controller automation path."""
import base64
import json
import re
import unittest
from unittest.mock import patch

from .model import SHA, api, pages, digest
from .pr import work_number, work_errors

POLICY_PATH = ".github/change-authority.json"
ROOT_A = (POLICY_PATH, "validation/authority.py")
GRADE_RANK = {"C": 0, "B": 1, "A": 2}


def path_valid(path, prefix=False):
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    return all(p not in ("", ".", "..") for p in (path.rstrip("/") if prefix else path).split("/"))


def parse_policy(raw):
    if not isinstance(raw, dict) or set(raw) != {"owner", "A", "B", "default"}:
        raise ValueError("Policy requires exactly owner, A, B and default")
    if not isinstance(raw["owner"], str) or not re.fullmatch(r"[A-Za-z0-9-]+", raw["owner"]):
        raise ValueError("Policy owner must be a GitHub login")
    if raw["default"] not in ("B", "C"):
        raise ValueError("Policy default must be B or C")
    for grade in ("A", "B"):
        if not isinstance(raw[grade], list) or any(not path_valid(p, prefix=True) for p in raw[grade]):
            raise ValueError("Policy paths must be exact paths or directory prefixes ending in /")
    return raw


def read_policy(repo, base_sha):
    if not SHA.fullmatch(base_sha):
        raise ValueError("Expected trusted base SHA")
    try:
        obj = api(f"repos/{repo}/contents/{POLICY_PATH}?ref={base_sha}")
    except Exception as exc:
        raise ValueError("Cannot read user-owned authority policy at trusted base") from exc
    if obj.get("type") != "file" or obj.get("encoding") != "base64":
        raise ValueError("Authority policy must be a regular JSON file")
    return parse_policy(json.loads(base64.b64decode(obj["content"]).decode("utf-8")))


def matches(path, rule):
    return path.startswith(rule) if rule.endswith("/") else path == rule


def classify(policy, paths):
    policy = parse_policy(policy)
    if not paths or any(not path_valid(p) for p in paths):
        raise ValueError("Complete non-empty changed path list required")
    result = {}
    for path in sorted(set(paths)):
        if any(matches(path, rule) for rule in (*ROOT_A, *policy["A"])):
            grade = "A"
        elif any(matches(path, rule) for rule in policy["B"]):
            grade = "B"
        else:
            grade = policy["default"]
        result[path] = grade
    return {"grade": max(result.values(), key=GRADE_RANK.get), "paths": result}


def changed_paths(files, count):
    if not isinstance(count, int) or count < 1 or len(files) != count:
        raise ValueError("Changed-file enumeration incomplete")
    paths = []
    for f in files:
        paths.append(f["filename"])
        if f.get("status") == "renamed":
            paths.append(f["previous_filename"])
        elif f.get("previous_filename"):
            paths.append(f["previous_filename"])
    return paths


def approval_scope(repo, work, issue, policy, classification):
    """Bind approval to the Work, policy, and exact set of B paths, not unrelated C changes."""
    problems = work_errors(work, issue)
    if problems:
        raise ValueError("; ".join(problems))
    return digest({"repository": repo.casefold(), "work": work,
                   "title": issue["title"], "body": issue["body"], "policy": policy,
                   "paths": sorted(p for p, grade in classification["paths"].items() if grade == "B")})


def decision(classification, required_approval=None, approved_change=None):
    grade = classification["grade"]
    if grade == "A":
        return dict(classification, allowed=False, reason="A: owner must modify and merge manually")
    if grade == "C":
        return dict(classification, allowed=True, reason="C: ordinary contracts still apply")
    allowed = bool(required_approval and approved_change == required_approval)
    return dict(classification, allowed=allowed, approval_digest=required_approval,
                reason="B: caller attests explicit user approval of this change scope" if allowed
                else "B: request explicit user approval before modification")


def inspect_pr(repo, number, expected_head=None, expected_base=None, approved_change=None):
    pr = api(f"repos/{repo}/pulls/{number}")
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    if pr["state"] != "open" or pr["base"]["ref"] != "main" or pr["base"]["repo"]["full_name"].casefold() != repo.casefold():
        raise ValueError("Authority check requires an OPEN PR targeting this main")
    if (expected_head and head != expected_head) or (expected_base and base != expected_base):
        raise ValueError("PR revision changed before authority check")
    policy = read_policy(repo, base)
    files = pages(f"repos/{repo}/pulls/{number}/files?per_page=100")
    classification = classify(policy, changed_paths(files, pr["changed_files"]))
    required = None
    if classification["grade"] == "B":
        work = work_number(pr.get("body"))
        issue = api(f"repos/{repo}/issues/{work}")
        required = approval_scope(repo, work, issue, policy, classification)
    result = decision(classification, required, approved_change)
    fresh = api(f"repos/{repo}/pulls/{number}")
    if (fresh["state"] != "open" or fresh["head"]["sha"] != head or fresh["base"]["sha"] != base
            or fresh["base"]["ref"] != "main" or fresh["changed_files"] != pr["changed_files"]
            or fresh.get("body") != pr.get("body")):
        raise ValueError("PR changed during authority check")
    return dict(result, head=head, base=base)


def plan_paths(repo, work, paths):
    base = api(f"repos/{repo}/branches/main")["commit"]["sha"]
    policy = read_policy(repo, base)
    result = classify(policy, paths)
    required = None
    if result["grade"] == "B":
        issue = api(f"repos/{repo}/issues/{work}")
        required = approval_scope(repo, work, issue, policy, result)
    return dict(result, work=work, base=base, approval_digest=required,
                action={"A": "USER_ONLY", "B": "REQUEST_USER_APPROVAL", "C": "PLAN_IMPLEMENTATION"}[result["grade"]])


class AuthorityTests(unittest.TestCase):
    policy = {"owner": "owner", "A": ["policy/"], "B": [".github/", "validation/", "app/"], "default": "C"}
    issue = {"number": 3, "title": "Work", "state": "open",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\nX only\n\n### Acceptance criteria\n\nX passes"}

    def test_classification(self):
        self.assertEqual(classify(self.policy, ["README.md"])["grade"], "C")
        self.assertEqual(classify(self.policy, ["README.md", "validation/pr.py"])["grade"], "B")
        for path in ROOT_A:
            self.assertEqual(classify(self.policy, [path])["grade"], "A")
        self.assertEqual(classify(self.policy, ["policy/rules.json", "validation/pr.py"])["grade"], "A")
        with self.assertRaises(ValueError):
            classify(self.policy, ["../bad"])

    def test_rename_and_incomplete(self):
        files = [{"filename": "README.md", "previous_filename": POLICY_PATH, "status": "renamed"}]
        self.assertEqual(classify(self.policy, changed_paths(files, 1))["grade"], "A")
        with self.assertRaises(ValueError):
            changed_paths(files, 2)

    def test_approval(self):
        b = classify(self.policy, ["validation/pr.py"])
        required = approval_scope("owner/repo", 3, self.issue, self.policy, b)
        self.assertFalse(decision(b, required)["allowed"])
        self.assertTrue(decision(b, required, required)["allowed"])
        self.assertFalse(decision(b, required, "wrong")["allowed"])
        self.assertFalse(decision(b, None, None)["allowed"])
        self.assertFalse(decision(classify(self.policy, [POLICY_PATH]), required, required)["allowed"])
        self.assertTrue(decision(classify(self.policy, ["README.md"]))["allowed"])
        expanded = classify(self.policy, ["validation/pr.py", "validation/issue.py"])
        self.assertNotEqual(required, approval_scope("owner/repo", 3, self.issue, self.policy, expanded))
        extra_c = classify(self.policy, ["validation/pr.py", "README.md"])
        self.assertEqual(required, approval_scope("owner/repo", 3, self.issue, self.policy, extra_c))
        self.assertNotEqual(required, approval_scope("owner/repo", 3, dict(self.issue, title="changed"), self.policy, b))
        self.assertNotEqual(required, approval_scope("owner/repo", 3, self.issue, dict(self.policy, default="B"), b))

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            parse_policy({})
        with patch(__name__ + ".api", side_effect=RuntimeError("not available")):
            with self.assertRaises(ValueError):
                read_policy("owner/repo", "a"*40)
