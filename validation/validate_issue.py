"""Structural Work contract. No external dependencies."""
import re
import sys
import unittest

FIELDS = ("Objective", "Scope", "Acceptance criteria")
HEADING = re.compile(r"^### (.+?)\s*$", re.MULTILINE)


def validate(body):
    body = (body or "").replace("\r\n", "\n")
    headings = list(HEADING.finditer(body))
    if tuple(h.group(1) for h in headings) != FIELDS:
        return False
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        value = body[heading.end():end]
        value = re.sub(r"<!--.*?-->", "", value, flags=re.S).strip()
        if not value or value.casefold() in {"_no response_", "n/a", "tbd", "todo", "- [ ]"}:
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


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        unittest.main(argv=[sys.argv[0]])
    else:
        sys.exit(0 if validate(sys.stdin.read()) else 1)
