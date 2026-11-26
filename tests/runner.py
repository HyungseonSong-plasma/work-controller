"""Strict JSON case loader. Only explicitly listed pure contract functions run."""
import inspect
import json
import math
from pathlib import Path
import unittest

from validation import issue, pr
from .model import TestClass

TARGETS = {
    "validation.issue": {"validate": issue.validate, "work_type": issue.work_type},
    "validation.pr": {"errors": pr.errors, "work_number": pr.work_number,
                      "work_errors": pr.work_errors},
}
FILES = {"issue": "validation.issue", "pr": "validation.pr"}
EXCEPTIONS = {"ValueError": ValueError}
CASE_DIR = Path(__file__).resolve().parent / "cases"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def decode(text):
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_float=finite_float, parse_constant=invalid_constant)


def fields(obj, required, optional=()):
    if (not isinstance(obj, dict) or not set(required) <= obj.keys()
            or obj.keys() - set(required) - set(optional)):
        raise ValueError(f"Expected fields {required}, optional {optional}")


def prepare(data, target):
    """Validate the entire suite before any case can run."""
    fields(data, ("version", "target", "cases"))
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Unsupported case schema version")
    if target not in TARGETS or data["target"] != target:
        raise ValueError("Unknown or mismatched test target")
    cases = data["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("Expected a nonempty case list")
    names, prepared = set(), []
    for case in cases:
        fields(case, ("name", "function", "args", "assert"), ("kwargs",))
        name, fn, args = case["name"], case["function"], case["args"]
        kwargs, assertion = case.get("kwargs", {}), case["assert"]
        if not isinstance(name, str) or not name.isidentifier() or name in names:
            raise ValueError(f"Invalid or duplicate case name: {name!r}")
        names.add(name)
        if not isinstance(fn, str) or fn not in TARGETS[target]:
            raise ValueError(f"Unknown function in case {name}")
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError(f"Invalid arguments in case {name}")
        function = TARGETS[target][fn]
        inspect.signature(function).bind(*args, **kwargs)
        if not isinstance(assertion, dict):
            raise ValueError(f"Invalid assertion in case {name}")
        op = assertion.get("op")
        if op == "equal":
            fields(assertion, ("op", "value"))
        elif op in ("true", "false"):
            fields(assertion, ("op",))
        elif op == "raises":
            fields(assertion, ("op", "exception"))
            if not isinstance(assertion["exception"], str) or assertion["exception"] not in EXCEPTIONS:
                raise ValueError(f"Unknown exception in case {name}")
        else:
            raise ValueError(f"Unknown assertion in case {name}")
        prepared.append((name, function, args, kwargs, assertion))
    return prepared


def make_test(function, args, kwargs, assertion):
    def run(case):
        op = assertion["op"]
        if op == "raises":
            with case.assertRaises(EXCEPTIONS[assertion["exception"]]):
                function(*args, **kwargs)
            return
        actual = function(*args, **kwargs)
        if op == "equal":
            case.assertEqual(actual, assertion["value"])
        elif op == "true":
            case.assertTrue(actual)
        elif op == "false":
            case.assertFalse(actual)
    return run


def suite_from_data(data, target, name):
    prepared = prepare(data, target)
    cls = TestClass.for_module("tests." + name)
    for case_name, function, args, kwargs, assertion in prepared:
        cls.add(case_name)(make_test(function, args, kwargs, assertion))
    return unittest.TestSuite(cls("test_" + item[0]) for item in prepared)


def load_suite(name):
    target = FILES[name]
    data = decode((CASE_DIR / (name + ".json")).read_text(encoding="utf-8"))
    return suite_from_data(data, target, name)


def self_suite():
    return unittest.defaultTestLoader.loadTestsFromTestCase(WrapperTests)


class WrapperTests(unittest.TestCase):
    target = "validation.issue"

    def case(self, **changes):
        return dict({"name": "sample", "function": "validate", "args": [""],
                     "assert": {"op": "false"}}, **changes)

    def data(self, *cases, **changes):
        return dict({"version": 1, "target": self.target,
                     "cases": list(cases)}, **changes)

    def test_order_and_isolation(self):
        from unittest.mock import patch
        called = []

        def fake(body):
            called.append(body)
            return False

        with patch.dict(TARGETS[self.target], {"validate": fake}):
            left = suite_from_data(self.data(self.case(name="z", args=["z"]),
                                             self.case(name="a", args=["a"])), self.target, "left")
            right = suite_from_data(self.data(self.case(name="z", args=["right"])), self.target, "right")
            self.assertEqual(called, [])
            self.assertEqual(left.countTestCases(), 2)
            self.assertEqual(right.countTestCases(), 1)
            result = unittest.TestResult()
            left.run(result)
            right.run(result)
            self.assertTrue(result.wasSuccessful())
            self.assertEqual(result.testsRun, 3)
            self.assertEqual(called, ["z", "a", "right"])

    def test_duplicate_and_bad_json(self):
        for text in ('{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '{"a":NaN}',
                     '{"a":Infinity}', '{"a":1e999}', '{'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode(text)

    def test_bad_suite(self):
        for data in (self.data(), self.data(self.case(), version=True),
                     self.data(self.case(), version=2), self.data(self.case(), extra=True),
                     self.data(self.case(), target="os"),
                     self.data(self.case(), self.case())):
            with self.subTest(data=data), self.assertRaises(ValueError):
                prepare(data, self.target)

    def test_bad_case(self):
        for case in (self.case(name=""), self.case(name="bad name"),
                     self.case(function="__import__"), self.case(function=[]),
                     self.case(args={}), self.case(kwargs=[]),
                     self.case(extra=True), self.case(**{"assert": {"op": "skip"}}),
                     self.case(**{"assert": {"op": "false", "value": False}}),
                     self.case(**{"assert": {"op": "equal"}}),
                     self.case(**{"assert": {"op": "raises", "exception": "Exception"}})):
            with self.subTest(case=case), self.assertRaises(ValueError):
                prepare(self.data(case), self.target)
        with self.assertRaises(TypeError):
            prepare(self.data(self.case(args=[])), self.target)

    def test_failure_not_skipped(self):
        suite = suite_from_data(self.data(self.case(**{"assert": {"op": "true"}})),
                                self.target, "failure")
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertFalse(result.wasSuccessful())

    def test_full_validation_before_execution(self):
        from unittest.mock import patch
        def forbidden(body):
            self.fail("Case ran before full schema validation")
        with patch.dict(TARGETS[self.target], {"validate": forbidden}):
            with self.assertRaises(ValueError):
                suite_from_data(self.data(self.case(), self.case(name="bad", function="unknown")),
                                self.target, "invalid")

    def test_registration_guards(self):
        local = TestClass.for_module("tests.local")
        local.add("same")(lambda case: None)
        with self.assertRaises(ValueError):
            local.add("same")(lambda case: None)
        with self.assertRaises(ValueError):
            local.add("bad name")
        with self.assertRaises(ValueError):
            TestClass.add("shared")
