"""Structural Work Unit contract; no external dependencies."""
import re
import unittest
from .issue import validate as validate_issue, work_type
from .model import PLACEHOLDERS, normalize, sections, without_comments

FIELDS = ("Work", "Changes", "Verification")
REFERENCE = re.compile(r"Refs #[1-9][0-9]*")


def errors(body):
    fields = sections(without_comments(normalize(body)))
    if tuple(name for name, _ in fields) != FIELDS:
        return ["Expected exactly these sections in order: " + ", ".join(FIELDS)]
    problems = []
    for field, value in fields:
        value = value.strip()
        if field == "Work":
            if not REFERENCE.fullmatch(value):
                problems.append("Work must be one reference: Refs #<issue number>")
        elif not value or value.casefold() in PLACEHOLDERS:
            problems.append(field + " requires content")
    return problems


def work_number(body):
    problems = errors(body)
    if problems:
        raise ValueError("; ".join(problems))
    return int(sections(without_comments(normalize(body)))[0][1].strip().split("#")[1])


def work_errors(number, issue):
    if not isinstance(issue, dict) or issue.get("number") != number:
        return ["Referenced Work does not exist or number does not match"]
    if "pull_request" in issue:
        return ["Work reference must point to an Issue, not a PR"]
    if issue.get("state") != "open":
        return ["Referenced Work must be OPEN"]
    if not validate_issue(issue.get("body")):
        return ["Referenced Work violates the Issue contract"]
    if work_type(issue.get("body")) == "Read-only":
        return ["Read-only Work must not have a PR"]
    return []


class ContractTests(unittest.TestCase):
    good = "### Work\n\nRefs #12\n\n### Changes\n\nAdd X\n\n### Verification\n\nTests passed"

    def test_valid(self):
        self.assertEqual(errors(self.good), [])
        self.assertEqual(errors(self.good.replace("\n", "\r\n")), [])

    def test_empty_or_missing(self):
        for body in (None, "", "free text", self.good.replace("Add X", ""), self.good.replace("### Changes", "### Other")):
            self.assertTrue(errors(body))

    def test_work_reference(self):
        for value in ("Refs #", "Refs #0", "Refs #1 #2", "Closes #12", "Refs other/repo#12"):
            self.assertTrue(errors(self.good.replace("Refs #12", value)))

    def test_placeholders(self):
        for value in ("TBD", "<!-- instructions -->", "   "):
            self.assertTrue(errors(self.good.replace("Tests passed", value)))

    def test_duplicate_or_reordered(self):
        self.assertTrue(errors(self.good + "\n### Work\nRefs #13"))
        self.assertTrue(errors(self.good.replace("### Work", "### Changes", 1)))


    def test_work_number(self):
        self.assertEqual(work_number(self.good), 12)
        self.assertEqual(work_number(self.good.replace("Refs #12", "<!-- ignored -->Refs #12")), 12)
        for body in ("", self.good.replace("Refs #12", "Refs #12, #13")):
            with self.assertRaises(ValueError):
                work_number(body)

    def test_work_target(self):
        issue = {"number": 12, "state": "open", "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n### Acceptance criteria\n\nX passes"}
        self.assertEqual(work_errors(12, issue), [])
        for bad in (None, {}, dict(issue, number=13), dict(issue, state="closed"),
                    dict(issue, pull_request={}), dict(issue, body=""), dict(issue, body=None)):
            self.assertTrue(work_errors(12, bad))

    def test_read_only_target(self):
        issue = {"number": 12, "state": "open", "body": "### Work type\n\nRead-only\n\n### Objective\n\nX\n\n### Scope\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n### Acceptance criteria\n\nReport"}
        self.assertEqual(work_errors(12, issue), ["Read-only Work must not have a PR"])
