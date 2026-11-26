"""Structural Work Unit contract; no external dependencies."""
import json
import re
import sys
import unittest
from validate_issue import validate as validate_issue

FIELDS = ("Work", "Changes", "Verification")
HEADING = re.compile(r"^### ([^\n]+?)\s*$", re.MULTILINE)
REFERENCE = re.compile(r"Refs #[1-9][0-9]*")


def errors(body):
    body = (body or "").replace("\r\n", "\n")
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    headings = list(HEADING.finditer(body))
    if tuple(h.group(1) for h in headings) != FIELDS:
        return ["Expected exactly these sections in order: " + ", ".join(FIELDS)]
    problems = []
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        value = body[heading.end():end].strip()
        field = FIELDS[i]
        if field == "Work":
            if not REFERENCE.fullmatch(value):
                problems.append("Work must be one reference: Refs #<issue number>")
        elif not value or value.casefold() in {"_no response_", "n/a", "tbd", "todo", "- [ ]"}:
            problems.append(field + " requires content")
    return problems


def work_number(body):
    problems = errors(body)
    if problems:
        raise ValueError("; ".join(problems))
    body = re.sub(r"<!--.*?-->", "", body.replace("\r\n", "\n"), flags=re.S)
    headings = list(HEADING.finditer(body))
    return int(body[headings[0].end():headings[1].start()].strip().split("#")[1])


def work_errors(number, issue):
    if not isinstance(issue, dict) or issue.get("number") != number:
        return ["Referenced Work does not exist or number does not match"]
    if "pull_request" in issue:
        return ["Work reference must point to an Issue, not a PR"]
    if issue.get("state") != "open":
        return ["Referenced Work must be OPEN"]
    if not validate_issue(issue.get("body")):
        return ["Referenced Work violates the Issue contract"]
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
        issue = {"number": 12, "state": "open", "body": "### Objective\n\nX\n\n### Scope\n\nX only\n\n### Acceptance criteria\n\nX passes"}
        self.assertEqual(work_errors(12, issue), [])
        for bad in (None, {}, dict(issue, number=13), dict(issue, state="closed"),
                    dict(issue, pull_request={}), dict(issue, body=""), dict(issue, body=None)):
            self.assertTrue(work_errors(12, bad))


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        unittest.main(argv=[sys.argv[0]])
    elif sys.argv[1:] == ["--work-number"]:
        try:
            print(work_number(sys.stdin.read()))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) == 3 and sys.argv[1] == "--issue-json":
        problems = work_errors(int(sys.argv[2]), json.load(sys.stdin))
        for problem in problems:
            print(problem, file=sys.stderr)
        sys.exit(bool(problems))
    else:
        for problem in (problems := errors(sys.stdin.read())):
            print(problem, file=sys.stderr)
        sys.exit(bool(problems))
