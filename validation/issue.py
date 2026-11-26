"""Structural Work contract. No external dependencies."""
import unittest
from .model import ISSUE_HEADING, PLACEHOLDERS, normalize, sections, without_comments

FIELDS = ("Objective", "Scope", "Acceptance criteria")
TYPES = ("Implementation", "Read-only")


def work_type(body):
    fields = sections(normalize(body), ISSUE_HEADING)
    names = tuple(name for name, _ in fields)
    if names == FIELDS:
        return "Implementation"
    if names == ("Work type", *FIELDS):
        value = without_comments(fields[0][1]).strip()
        if value in TYPES:
            return value
    return None


def validate(body, require_type=False):
    fields = sections(normalize(body), ISSUE_HEADING)
    if work_type(body) is None or (require_type and tuple(name for name, _ in fields) == FIELDS):
        return False
    for _, value in fields:
        value = without_comments(value).strip()
        if not value or value.casefold() in PLACEHOLDERS:
            return False
    return True


class ContractTests(unittest.TestCase):
    good = "### Objective\n\nImplement X\n\n### Scope\n\nX only\n\n### Acceptance criteria\n\n- X passes"

    def test_valid(self):
        self.assertTrue(validate(self.good))
        self.assertTrue(validate(self.good.replace("\n", "\r\n")))

    def test_empty_or_missing(self):
        for body in (None, "", "free text", self.good.replace("X only", ""), self.good.replace("### Scope", "### Other")):
            self.assertFalse(validate(body))

    def test_placeholders(self):
        for placeholder in ("_No response_", "TBD", "<!-- instructions -->", "   "):
            self.assertFalse(validate(self.good.replace("X only", placeholder)))

    def test_duplicate_or_reordered(self):
        self.assertFalse(validate(self.good + "\n### Scope\nmore"))
        self.assertFalse(validate(self.good.replace("### Objective", "### Scope", 1)))

    def test_work_type(self):
        for kind in TYPES:
            body = "### Work type\n\n" + kind + "\n\n" + self.good
            self.assertTrue(validate(body, require_type=True))
            self.assertEqual(work_type(body), kind)
        self.assertEqual(work_type(self.good), "Implementation")
        self.assertFalse(validate(self.good, require_type=True))
        self.assertFalse(validate("### Work type\n\nUnknown\n\n" + self.good))
