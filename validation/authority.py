"""User-owned A/B/C policy for the Controller automation path."""
import base64
import json
import re
import unittest
from unittest.mock import patch

from .model import SHA, api, pages

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


def decision(policy, classification, pr, reviews):
    grade = classification["grade"]
    if grade == "A":
        return dict(classification, allowed=False, reason="A: owner must modify and merge manually")
    if grade == "C":
        return dict(classification, allowed=True, reason="C: ordinary contracts still apply")
    owner = policy["owner"].casefold()
    if pr["user"]["login"].casefold() == owner:
        return dict(classification, allowed=False, reason="B: PR author and owner share identity; use manual user merge or a separate agent identity")
    decisive = [r for r in reviews if (r.get("user") or {}).get("login", "").casefold() == owner
                and r.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}]
    last = max(decisive, key=lambda r: r["id"], default={})
    allowed = last.get("state") == "APPROVED" and last.get("commit_id") == pr["head"]["sha"]
    return dict(classification, allowed=allowed,
                reason="B: independent owner approved exact head" if allowed else "B: independent owner approval of exact head required")


def inspect_pr(repo, number, expected_head=None, expected_base=None):
    pr = api(f"repos/{repo}/pulls/{number}")
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    if pr["state"] != "open" or pr["base"]["ref"] != "main" or pr["base"]["repo"]["full_name"].casefold() != repo.casefold():
        raise ValueError("Authority check requires an OPEN PR targeting this main")
    if (expected_head and head != expected_head) or (expected_base and base != expected_base):
        raise ValueError("PR revision changed before authority check")
    policy = read_policy(repo, base)
    files = pages(f"repos/{repo}/pulls/{number}/files?per_page=100")
    classification = classify(policy, changed_paths(files, pr["changed_files"]))
    reviews = pages(f"repos/{repo}/pulls/{number}/reviews?per_page=100") if classification["grade"] == "B" else []
    result = decision(policy, classification, pr, reviews)
    fresh = api(f"repos/{repo}/pulls/{number}")
    if (fresh["state"] != "open" or fresh["head"]["sha"] != head or fresh["base"]["sha"] != base
            or fresh["base"]["ref"] != "main" or fresh["changed_files"] != pr["changed_files"]):
        raise ValueError("PR changed during authority check")
    return dict(result, head=head, base=base)


def plan_paths(repo, paths):
    base = api(f"repos/{repo}/branches/main")["commit"]["sha"]
    result = classify(read_policy(repo, base), paths)
    return dict(result, base=base, action={"A": "USER_ONLY", "B": "REQUEST_USER_APPROVAL", "C": "PLAN_IMPLEMENTATION"}[result["grade"]])


class AuthorityTests(unittest.TestCase):
    policy = {"owner": "owner", "A": ["policy/"], "B": [".github/", "validation/", "app/"], "default": "C"}
    pr = {"head": {"sha": "a"*40}, "user": {"login": "agent"}}

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
        review = {"id": 1, "user": {"login": "owner"}, "state": "APPROVED", "commit_id": "a"*40}
        b = classify(self.policy, ["validation/pr.py"])
        self.assertFalse(decision(self.policy, b, self.pr, [])["allowed"])
        self.assertTrue(decision(self.policy, b, self.pr, [review])["allowed"])
        self.assertFalse(decision(self.policy, b, self.pr, [dict(review, commit_id="b"*40)])["allowed"])
        self.assertFalse(decision(self.policy, b, self.pr, [review, dict(review, id=2, state="DISMISSED")])["allowed"])
        self.assertFalse(decision(self.policy, b, dict(self.pr, user={"login":"owner"}), [review])["allowed"])
        self.assertFalse(decision(self.policy, classify(self.policy, [POLICY_PATH]), self.pr, [review])["allowed"])
        self.assertTrue(decision(self.policy, classify(self.policy, ["README.md"]), self.pr, [])["allowed"])

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            parse_policy({})
        with patch(__name__ + ".api", side_effect=RuntimeError("not available")):
            with self.assertRaises(ValueError):
                read_policy("owner/repo", "a"*40)
