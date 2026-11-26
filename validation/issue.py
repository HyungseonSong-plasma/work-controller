"""Canonical structural Work contract. No legacy format fallback."""
import re
from types import ModuleType
from unittest import TestLoader, TestResult
from .model import TestClass, ISSUE_HEADING, PLACEHOLDERS, normalize, sections, without_comments

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


TestClass = TestClass.for_module(__name__)
TestClass.scope = "#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X"
TestClass.good = ("### Work type\n\nImplementation\n\n### Objective\n\nImplement X\n\n### Scope\n\n"
        + TestClass.scope + "\n\n### Acceptance criteria\n\n- X passes")


@TestClass.add("valid")
def _(self):
    self.assertTrue(validate(self.good))
    self.assertTrue(validate(self.good.replace("\n", "\r\n")))

@TestClass.add("empty_or_missing")
def _(self):
    for body in (None, "", "free text", self.good.replace("X only", ""),
                 self.good.replace("### Scope", "### Other"),
                 self.good.replace("### Work type\n\nImplementation\n\n", ""),
                 self.good.replace(self.scope, "X only")):
        self.assertFalse(validate(body))

@TestClass.add("placeholders")
def _(self):
    for placeholder in ("_No response_", "TBD", "TODO", "N/A", "<!-- instructions -->", "   ", "[ ]"):
        self.assertFalse(validate(self.good.replace("X only", placeholder)))
        self.assertFalse(validate(self.good.replace("- Implement X", "- " + placeholder)))

@TestClass.add("duplicate_or_reordered")
def _(self):
    self.assertFalse(validate(self.good + "\n### Scope\nmore"))
    self.assertFalse(validate(self.good.replace("### Objective", "### Scope", 1)))

@TestClass.add("work_type")
def _(self):
    for kind in TYPES:
        body = self.good.replace("Implementation", kind, 1)
        self.assertTrue(validate(body))
        self.assertEqual(work_type(body), kind)
    self.assertIsNone(work_type(self.good.replace("### Work type\n\nImplementation\n\n", "")))
    self.assertFalse(validate(self.good.replace("Implementation", "Unknown", 1)))

@TestClass.add("malformed_scope")
def _(self):
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



@TestClass.add("registration_isolation")
def _(self):
    left = TestClass.for_module("test.left")
    right = TestClass.for_module("test.right")

    @left.add("same")
    def _(case):
        case.assertTrue(True)

    @right.add("same")
    def _(case):
        case.assertFalse(False)

    @left.add("left_only")
    def _(case):
        case.assertEqual(1, 1)

    self.assertIsNot(left, right)
    self.assertEqual(left.__module__, "test.left")
    self.assertEqual(right.__module__, "test.right")
    self.assertFalse(hasattr(right, "test_left_only"))
    self.assertFalse(hasattr(TestClass, "test_left_only"))
    self.assertIsNot(left.test_same, right.test_same)


@TestClass.add("registration_invalid_names")
def _(self):
    local = TestClass.for_module("test.local")
    for name in (None, 42, "", "with space", "with-dash", "1first"):
        with self.subTest(name=name), self.assertRaises(ValueError):
            local.add(name)
    with self.assertRaises(TypeError):
        local.add("not_function")(True)
    self.assertFalse(hasattr(local, "test_not_function"))


@TestClass.add("registration_duplicate")
def _(self):
    local = TestClass.for_module("test.local")

    @local.add("same")
    def original(case):
        case.assertTrue(True)

    with self.assertRaises(ValueError):
        local.add("same")(lambda case: None)
    self.assertIs(local.test_same, original)


@TestClass.add("registration_discovery_and_deferred_execution")
def _(self):
    module = ModuleType("test.discovery")
    module.TestClass = TestClass.for_module(module.__name__)
    seen = []

    @module.TestClass.add("deferred")
    def _(case):
        seen.append(case.id())
        case.assertTrue(True)

    self.assertEqual(seen, [])
    suite = TestLoader().loadTestsFromModule(module)
    self.assertEqual(suite.countTestCases(), 1)
    result = TestResult()
    suite.run(result)
    self.assertTrue(result.wasSuccessful())
    self.assertEqual(result.testsRun, 1)
    self.assertEqual(seen, ["test.discovery.TestClass.test_deferred"])
