"""Structural Work contract. No external dependencies."""
import unittest
from .model import ISSUE_HEADING, PLACEHOLDERS, normalize, sections, without_comments

FIELDS = ("Objective", "Scope", "Acceptance criteria")


def validate(body):
    fields = sections(normalize(body), ISSUE_HEADING)
    if tuple(name for name, _ in fields) != FIELDS:
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
