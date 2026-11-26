"""Structural Work Unit contract; no external dependencies."""
import re
import sys
import unittest

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


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        unittest.main(argv=[sys.argv[0]])
    else:
        for problem in (problems := errors(sys.stdin.read())):
            print(problem, file=sys.stderr)
        sys.exit(bool(problems))
