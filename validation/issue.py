"""Canonical structural Work contract. No legacy format fallback."""
import re
import unittest
from .model import ISSUE_HEADING, PLACEHOLDERS, normalize, sections, without_comments

FIELDS = ("Work type", "Objective", "Scope", "Acceptance criteria")
TYPES = ("Implementation", "Read-only")
SCOPE_FIELDS = ("Constraints", "Actions")
SCOPE_HEADING = re.compile(r"^####[ \t]+([^\n]+?)[ \t]*$", re.MULTILINE)
BULLET_PREFIX = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)(?:\[[ xX]\]\s*)?")


def work_type(body):
    fields = sections(without_comments(normalize(body)), ISSUE_HEADING)
    if tuple(name for name, _ in fields) == FIELDS:
        value = fields[0][1].strip()
        if value in TYPES:
            return value
    return None


def scope_valid(value):
    value = value.strip()
    matches = list(SCOPE_HEADING.finditer(value))
    if tuple(h.group(1) for h in matches) != SCOPE_FIELDS or value[:matches[0].start()].strip():
        return False
    for _, content in sections(value, SCOPE_HEADING):
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        entries = [BULLET_PREFIX.sub("", line).strip() for line in lines]
        if not entries or any(not entry or entry.casefold() in PLACEHOLDERS
                              or entry in {"-", "*", "+", "[ ]", "[x]", "[X]"}
                              or entry.startswith("#") for entry in entries):
            return False
    return True


def validate(body):
    fields = sections(without_comments(normalize(body)), ISSUE_HEADING)
    if work_type(body) is None:
        return False
    for name, value in fields:
        value = value.strip()
        if not value or value.casefold() in PLACEHOLDERS:
            return False
        if name == "Scope" and not scope_valid(value):
            return False
    return True


class ContractTests(unittest.TestCase):
    scope = "#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X"
    good = ("### Work type\n\nImplementation\n\n### Objective\n\nImplement X\n\n### Scope\n\n"
            + scope + "\n\n### Acceptance criteria\n\n- X passes")

    def test_valid(self):
        self.assertTrue(validate(self.good))
        self.assertTrue(validate(self.good.replace("\n", "\r\n")))

    def test_empty_or_missing(self):
        for body in (None, "", "free text", self.good.replace("X only", ""),
                     self.good.replace("### Scope", "### Other"),
                     self.good.replace("### Work type\n\nImplementation\n\n", ""),
                     self.good.replace(self.scope, "X only")):
            self.assertFalse(validate(body))

    def test_placeholders(self):
        for placeholder in ("_No response_", "TBD", "TODO", "N/A", "<!-- instructions -->", "   ", "[ ]"):
            self.assertFalse(validate(self.good.replace("X only", placeholder)))
            self.assertFalse(validate(self.good.replace("- Implement X", "- " + placeholder)))

    def test_duplicate_or_reordered(self):
        self.assertFalse(validate(self.good + "\n### Scope\nmore"))
        self.assertFalse(validate(self.good.replace("### Objective", "### Scope", 1)))

    def test_work_type(self):
        for kind in TYPES:
            body = self.good.replace("Implementation", kind, 1)
            self.assertTrue(validate(body))
            self.assertEqual(work_type(body), kind)
        self.assertIsNone(work_type(self.good.replace("### Work type\n\nImplementation\n\n", "")))
        self.assertFalse(validate(self.good.replace("Implementation", "Unknown", 1)))

    def test_malformed_scope(self):
        for scope in (
            "#### Constraints\n\n- X only",
            "#### Actions\n\n- Implement X",
            "#### Actions\n\n- Implement X\n\n#### Constraints\n\n- X only",
            "#### Constraints\n\n- X only\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X",
            "#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n#### Extra\n\n- X",
            "Unexpected scope text\n\n" + self.scope,
            self.scope.replace("#### Actions", "##### Actions"),
        ):
            with self.subTest(scope=scope):
                self.assertFalse(validate(self.good.replace(self.scope, scope)))
