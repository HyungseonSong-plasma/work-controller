"""Current-state merge preflight. Run from a trusted main checkout."""
import json
import subprocess
import unittest
from unittest.mock import patch
from .pr import work_number, work_errors
from .model import SHA, api, digest

def snapshot(pr, issue, repo, number):
    if pr.get("number") != number or pr.get("state") != "open" or pr.get("draft"):
        raise ValueError("PR must be the requested OPEN non-draft pull request")
    base, head = pr["base"], pr["head"]
    if base["ref"] != "main" or base["repo"]["full_name"].casefold() != repo.casefold():
        raise ValueError("PR must target this repository's main")
    if not all(SHA.fullmatch(x["sha"]) for x in (base, head)):
        raise ValueError("Invalid commit SHA")
    work = work_number(pr.get("body"))
    problems = work_errors(work, issue)
    if problems:
        raise ValueError("; ".join(problems))
    return {
        "repository": repo.casefold(), "pr": number, "head": head["sha"], "base": base["sha"],
        "pr_title": pr["title"], "pr_body": pr["body"],
        "work": work, "work_title": issue["title"], "work_body": issue["body"],
        "work_state": issue["state"],
    }


def read_snapshot(repo, number):
    pr = api(f"repos/{repo}/pulls/{number}")
    work = work_number(pr.get("body"))
    issue = api(f"repos/{repo}/issues/{work}")
    return snapshot(pr, issue, repo, number), pr


def verify(repo, number, approved, expected_head):
    current, pr = read_snapshot(repo, number)
    if current["head"] != expected_head or digest(current) != approved:
        raise ValueError("Reviewed snapshot or expected head changed; review again")
    if pr.get("mergeable_state") != "clean" or pr.get("mergeable") is not True:
        raise ValueError("GitHub does not report a clean mergeable PR")
    result = subprocess.run(["gh", "pr", "checks", str(number), "--repo", repo,
                             "--required", "--json", "name,state"], check=True,
                            capture_output=True, text=True)
    checks = json.loads(result.stdout)
    if not any(c["name"] == "pr-template" for c in checks) or any(c["state"] != "SUCCESS" for c in checks):
        raise ValueError("All required checks, including pr-template, must succeed")
    latest, latest_pr = read_snapshot(repo, number)
    if latest != current or latest_pr.get("mergeable_state") != "clean":
        raise ValueError("PR or Work changed during preflight")
    return latest


class MergeTests(unittest.TestCase):
    repo = "owner/repo"
    pr = {"number": 4, "state": "open", "draft": False, "title": "Change",
          "body": "### Work\n\nRefs #3\n\n### Changes\n\nX\n\n### Verification\n\nTests pass",
          "base": {"ref": "main", "sha": "a"*40, "repo": {"full_name": repo}},
          "head": {"sha": "b"*40}}
    issue = {"number": 3, "state": "open", "title": "Work",
             "body": "### Objective\n\nX\n\n### Scope\n\nX only\n\n### Acceptance criteria\n\nX passes"}

    def test_valid_snapshot(self):
        data = snapshot(self.pr, self.issue, self.repo, 4)
        self.assertEqual(data["work"], 3)
        self.assertEqual(data["head"], "b"*40)
        self.assertEqual(digest(data), digest(dict(reversed(list(data.items())))))

    def test_invalid_metadata(self):
        for pr in (dict(self.pr, draft=True), dict(self.pr, state="closed"),
                   dict(self.pr, number=5), dict(self.pr, base=dict(self.pr["base"], ref="dev"))):
            with self.assertRaises(ValueError):
                snapshot(pr, self.issue, self.repo, 4)
        for issue in (dict(self.issue, state="closed"), dict(self.issue, body="")):
            with self.assertRaises(ValueError):
                snapshot(self.pr, issue, self.repo, 4)

    def test_review_snapshot_changes(self):
        original = digest(snapshot(self.pr, self.issue, self.repo, 4))
        for pr, issue in ((dict(self.pr, title="Changed"), self.issue),
                          (dict(self.pr, head={"sha": "c"*40}), self.issue),
                          (self.pr, dict(self.issue, body=self.issue["body"]+" again"))):
            self.assertNotEqual(original, digest(snapshot(pr, issue, self.repo, 4)))

    def test_verify_rejects_stale_or_failed(self):
        data = snapshot(self.pr, self.issue, self.repo, 4)
        pr = dict(self.pr, mergeable=True, mergeable_state="clean")
        checks = subprocess.CompletedProcess([], 0, '[{"name":"pr-template","state":"SUCCESS"}]')
        with patch(__name__ + ".read_snapshot", return_value=(data, pr)), patch("subprocess.run", return_value=checks):
            self.assertEqual(verify(self.repo, 4, digest(data), data["head"]), data)
            with self.assertRaises(ValueError):
                verify(self.repo, 4, "wrong", data["head"])
            with self.assertRaises(ValueError):
                verify(self.repo, 4, digest(data), "c"*40)
        with patch(__name__ + ".read_snapshot", return_value=(data, pr)), patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, '[]')):
            with self.assertRaises(ValueError):
                verify(self.repo, 4, digest(data), data["head"])
        with patch(__name__ + ".read_snapshot", side_effect=[(data, pr), (dict(data, work_title="new"), pr)]), patch("subprocess.run", return_value=checks):
            with self.assertRaises(ValueError):
                verify(self.repo, 4, digest(data), data["head"])
