"""Work completion contract. Run from a trusted main checkout."""
import re
import unittest
from unittest.mock import patch

from .issue import validate as validate_issue
from .pr import errors as pr_errors, work_number
from .model import api, digest, normalize, sections, without_comments, pages


def declared_works(body):
    """Find canonical reference lines in Work sections even if other sections are invalid."""
    result = set()
    for name, value in sections(without_comments(normalize(body))):
        if name == "Work":
            result.update(int(n) for n in re.findall(r"^Refs #([1-9][0-9]*)[ \t]*$", value, re.M))
    return result


def assessment(repo, number, issue, pulls):
    """Return review snapshot and structural blockers; does not judge meaning."""
    blockers = []
    if issue.get("number") != number or "pull_request" in issue:
        blockers.append("Target must be the requested Issue, not a PR")
    if not validate_issue(issue.get("body")):
        blockers.append("Work violates Issue contract")
    units = []
    merged = 0
    for pr in sorted(pulls, key=lambda p: p["number"]):
        if number not in declared_works(pr.get("body")):
            continue
        n = pr["number"]
        status = "open" if pr["state"] == "open" else ("merged" if pr.get("merged_at") else "discarded")
        if status == "open":
            blockers.append(f"Work Unit #{n} is still OPEN")
        if status == "merged":
            if (pr["base"]["ref"] != "main" or
                    pr["base"]["repo"]["full_name"].casefold() != repo.casefold()):
                blockers.append(f"Work Unit #{n} did not merge to this repository's main")
            elif pr_errors(pr.get("body")) or work_number(pr["body"]) != number:
                blockers.append(f"Merged Work Unit #{n} violates PR contract")
            elif not pr.get("merge_commit_sha"):
                blockers.append(f"Merged Work Unit #{n} lacks merge commit SHA")
            else:
                merged += 1
        units.append({"number": n, "status": status, "title": pr["title"], "body": pr.get("body"),
                      "head": pr["head"]["sha"], "base": pr["base"]["ref"],
                      "merge_commit": pr.get("merge_commit_sha") if status == "merged" else None})
    if not merged:
        blockers.append("Work requires at least one valid merged Work Unit")
    data = {"repository": repo.casefold(), "work": number, "title": issue["title"],
            "body": issue.get("body"), "state": issue["state"],
            "state_reason": issue.get("state_reason"), "units": units}
    return data, blockers


def read_assessment(repo, number):
    issue = api(f"repos/{repo}/issues/{number}")
    return assessment(repo, number, issue, pages(f"repos/{repo}/pulls?state=all&per_page=100"))


def approved_close(repo, number, approved, evidence=None, close=False):
    current, blockers = read_assessment(repo, number)
    if current["state"] != "open":
        raise ValueError("Work must be OPEN before completion")
    if blockers:
        raise ValueError("; ".join(blockers))
    if digest(current) != approved:
        raise ValueError("Reviewed Work snapshot changed; review again")
    if close and not (evidence or "").strip():
        raise ValueError("Explicit acceptance review evidence is required to close")
    latest, blockers = read_assessment(repo, number)
    if latest != current or blockers:
        raise ValueError("Work or Work Units changed during completion preflight")
    if close:
        api(f"repos/{repo}/issues/{number}/comments", "-X", "POST", "-f",
            "body=Work completion review\n\nSnapshot: " + approved + "\n\nEvidence: " + evidence.strip())
        latest, blockers = read_assessment(repo, number)
        if latest != current or blockers:
            raise ValueError("Work changed after recording evidence; review again")
        result = api(f"repos/{repo}/issues/{number}", "-X", "PATCH", "-f", "state=closed",
                     "-f", "state_reason=completed")
        if result.get("state") != "closed" or result.get("state_reason") != "completed":
            raise ValueError("GitHub did not report completed closure")
    return current


def guard_closed(repo, number):
    """Post-close structural guard, not semantic approval."""
    data, blockers = read_assessment(repo, number)
    if data["state"] != "closed" or data["state_reason"] != "completed" or not blockers:
        return []
    latest, blockers = read_assessment(repo, number)
    if latest["state"] != "closed" or latest["state_reason"] != "completed" or not blockers:
        return []
    result = api(f"repos/{repo}/issues/{number}", "-X", "PATCH", "-f", "state=open")
    if result.get("state") != "open":
        raise ValueError("GitHub did not reopen the invalid completion")
    api(f"repos/{repo}/issues/{number}/comments", "-X", "POST", "-f",
        "body=Work completion contract failed; reopened.\n\n" + "\n".join("- " + b for b in blockers))
    return blockers


class CompletionTests(unittest.TestCase):
    repo = "owner/repo"
    issue = {"number": 3, "title": "Work", "state": "open",
             "body": "### Objective\n\nX\n\n### Scope\n\nX only\n\n### Acceptance criteria\n\nX passes"}
    pr = {"number": 4, "title": "Change", "state": "closed", "merged_at": "2026-01-01",
          "merge_commit_sha": "a"*40, "body": "### Work\n\nRefs #3\n\n### Changes\n\nX\n\n### Verification\n\nTests pass",
          "base": {"ref": "main", "repo": {"full_name": repo}}, "head": {"sha": "b"*40}}

    def assess(self, pulls, issue=None):
        return assessment(self.repo, 3, issue or self.issue, pulls)

    def test_valid_and_discarded(self):
        discarded = dict(self.pr, number=5, merged_at=None)
        data, blockers = self.assess([discarded, self.pr])
        self.assertEqual(blockers, [])
        self.assertEqual([u["status"] for u in data["units"]], ["merged", "discarded"])

    def test_blockers(self):
        for pulls in ([], [dict(self.pr, merged_at=None)], [dict(self.pr, state="open")],
                      [self.pr, dict(self.pr, number=5, state="open", body="### Work\n\nRefs #3")],
                      [dict(self.pr, body="### Work\n\nRefs #3")],
                      [dict(self.pr, base=dict(self.pr["base"], ref="dev"))]):
            self.assertTrue(self.assess(pulls)[1])
        self.assertTrue(self.assess([self.pr], dict(self.issue, body=""))[1])

    def test_membership(self):
        self.assertEqual(declared_works(self.pr["body"]), {3})
        self.assertEqual(declared_works("### Changes\n\nRefs #3"), set())
        self.assertEqual(declared_works("<!-- ### Work\n\nRefs #3 -->"), set())
        self.assertEqual(declared_works("### Work\n\nRefs #3\nRefs #7"), {3, 7})

    def test_approval_and_stale(self):
        data, blockers = self.assess([self.pr])
        with patch(__name__ + ".read_assessment", return_value=(data, blockers)), patch(__name__ + ".api") as call:
            self.assertEqual(approved_close(self.repo, 3, digest(data)), data)
            with self.assertRaises(ValueError):
                approved_close(self.repo, 3, "wrong")
            with self.assertRaises(ValueError):
                approved_close(self.repo, 3, digest(data), close=True)
            call.assert_not_called()
        with patch(__name__ + ".read_assessment", side_effect=[(data, []), (dict(data, title="changed"), [])]):
            with self.assertRaises(ValueError):
                approved_close(self.repo, 3, digest(data))

    def test_guard(self):
        data, blockers = self.assess([], dict(self.issue, state="closed", state_reason="completed"))
        with patch(__name__ + ".read_assessment", return_value=(data, blockers)), patch(__name__ + ".api", side_effect=[{"state": "open"}, {}]) as call:
            self.assertTrue(guard_closed(self.repo, 3))
            self.assertEqual(call.call_count, 2)
        with patch(__name__ + ".read_assessment", return_value=(dict(data, state_reason="not_planned"), blockers)), patch(__name__ + ".api") as call:
            self.assertEqual(guard_closed(self.repo, 3), [])
            call.assert_not_called()
