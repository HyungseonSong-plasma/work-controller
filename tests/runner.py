"""Strict JSON case loader. Only explicitly listed pure contract functions run."""
import inspect
import json
import math
from pathlib import Path
import unittest

from validation import issue, pr, merge
from controller import cleanup, executor
from .scenarios import approval as approval_scenarios, completion as completion_scenarios, merge as merge_scenarios
from .scenarios import controller as controller_scenarios, executor as executor_scenarios, operation as operation_scenarios
from .scenarios import cleanup as cleanup_scenarios
from .model import TestClass

TARGETS = {
    "controller.cleanup": {"branch_is_unchanged": cleanup.branch_is_unchanged,
                           "run_is_disposable": cleanup.run_is_disposable,
                           "associated": cleanup.associated,
                           "references_evidence": cleanup.references_evidence},
    "validation.issue": {"validate": issue.validate, "work_type": issue.work_type},
    "validation.pr": {"errors": pr.errors, "work_number": pr.work_number,
                      "work_errors": pr.work_errors},
    "validation.approval": {"evaluate_state": approval_scenarios.evaluate_state},
    "validation.completion": {"assessment_blockers": completion_scenarios.assessment_blockers},
    "validation.merge": {"snapshot": merge.snapshot},
    "controller.decision_engine": {"first_action": controller_scenarios.first_action,
                                   "orphan_action": controller_scenarios.orphan_action,
                                   "orphan_number": controller_scenarios.orphan_number},
    "controller.executor": {"validate_plan": executor.validate_plan, "tree_entries": executor.tree_entries},
    "controller.operation": {"identity_case": operation_scenarios.identity_case},
}
FILES = {name: "validation." + name for name in ("issue", "pr", "approval", "completion", "merge")}
FILES.update({"cleanup": "controller.cleanup", "controller": "controller.decision_engine", "executor": "controller.executor",
              "operation": "controller.operation"})
SCENARIOS = {
    "cleanup": (cleanup_scenarios.CleanupTests, cleanup_scenarios.CleanupLifecycleTests),
    "approval": (approval_scenarios.ApprovalTests,),
    "completion": (completion_scenarios.CompletionTests,),
    "merge": (merge_scenarios.MergeTests,),
    "controller": (controller_scenarios.ControllerTests,),
    "executor": (executor_scenarios.ExecutorTests,),
    "operation": (operation_scenarios.OperationTests, operation_scenarios.CommentRequestTests),
}
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
    suite = suite_from_data(data, target, name)
    if name in SCENARIOS:
        classes = SCENARIOS[name]
        if not classes:
            raise ValueError(f"Empty Python scenario registration: {name}")
        for cls in classes:
            scenarios = unittest.defaultTestLoader.loadTestsFromTestCase(cls)
            if not scenarios.countTestCases():
                raise ValueError(f"Empty Python scenario suite: {name}/{cls.__name__}")
            suite.addTests(scenarios)
    return suite


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

    def test_hybrid_selectors(self):
        import contextlib
        import io
        from app.cli import main
        for name, minimum in (("approval", 17), ("completion", 16), ("merge", 9)):
            with self.subTest(name=name):
                suite = load_suite(name)
                tests = list(suite)
                self.assertTrue(any(type(t).__module__ == "tests." + name for t in tests))
                self.assertTrue(any(isinstance(t, unittest.TestSuite) and
                                    any(type(m).__module__ == "tests.scenarios." + name for m in t)
                                    for t in tests) or
                                any(type(t).__module__ == "tests.scenarios." + name for t in tests))
                self.assertGreaterEqual(suite.countTestCases(), minimum)
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(main(["test", name]), 0, output.getvalue())
                self.assertIn("Ran " + str(suite.countTestCases()) + " tests", output.getvalue())

    def test_empty_scenario_rejected(self):
        from unittest.mock import patch
        with patch.dict(SCENARIOS, {"approval": (unittest.TestCase,)}):
            with self.assertRaises(ValueError):
                load_suite("approval")

    def test_hybrid_invalid_data_rejected(self):
        from unittest.mock import patch
        with patch("pathlib.Path.read_text", return_value='{"version":1,"target":"validation.merge","cases":[]}'):
            with self.assertRaises(ValueError):
                load_suite("merge")

    def test_controller_selectors(self):
        import contextlib
        import io
        from app.cli import main
        for name, json_count, scenario_count in (("controller", 15, 4),
                                                  ("executor", 10, 4),
                                                  ("operation", 18, 5)):
            with self.subTest(name=name):
                suite = load_suite(name)
                cases = list(suite)
                actual_json = sum(type(t).__module__ == "tests." + name for t in cases)
                self.assertGreaterEqual(actual_json, json_count)
                scenarios = [t for t in cases if type(t).__module__ == "tests.scenarios." + name]
                actual_scenarios = len(scenarios)
                self.assertGreaterEqual(actual_scenarios, scenario_count)
                self.assertEqual(suite.countTestCases(), actual_json + actual_scenarios)
                if name == "operation":
                    self.assertEqual({type(t).__name__ for t in scenarios},
                                     {"OperationTests", "CommentRequestTests"})
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(main(["test", name]), 0, output.getvalue())
                self.assertIn(f"Ran {actual_json + actual_scenarios} tests", output.getvalue())

    def test_controller_failure_propagation(self):
        import contextlib
        import io
        from unittest.mock import patch
        from app.cli import main
        bad = {"version": 1, "target": "controller.executor", "cases": [
            {"name": "wrong_expected", "function": "validate_plan",
             "args": [{"title": "Change", "body": "### Work\n\nRefs #4\n\n### Changes\n\nX\n\n### Verification\n\nTests pass",
                       "files": [{"path": "README.md", "content": "new"}]}, 4],
             "assert": {"op": "equal", "value": ["different.txt"]}}]}
        with patch("pathlib.Path.read_text", return_value=json.dumps(bad)), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(["test", "executor"]), 1)
        self.assertIn("FAIL: test_wrong_expected", output.getvalue())

    def test_live_explicit_dispatch(self):
        from unittest.mock import patch
        from app.cli import main
        from . import live_executor
        with patch.object(live_executor, "live_ci") as live:
            self.assertEqual(main(["executor-live-ci"]), 0)
            live.assert_called_once_with()

    def test_live_excluded_from_unit_suites(self):
        import contextlib
        import io
        from unittest.mock import patch
        from app.cli import main, MODULES
        from . import runner, live_executor
        self.assertNotIn("live_executor", FILES)
        self.assertNotIn("tests.live_executor", TARGETS)
        self.assertFalse(any("live_ci" in functions for functions in TARGETS.values()))
        # Avoid recursive execution of this wrapper test in the nested full suite.
        with patch.object(live_executor, "live_ci", side_effect=AssertionError("live execution in unit suite")) as live, \
                patch.object(runner, "self_suite", return_value=unittest.TestSuite()), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            for name in (*MODULES, "all"):
                with self.subTest(name=name):
                    self.assertEqual(main(["test", name]), 0, output.getvalue())
            live.assert_not_called()
        data = {"version": 1, "target": "controller.executor", "cases": [
            {"name": "live", "function": "live_ci", "args": [], "assert": {"op": "true"}}]}
        with self.assertRaises(ValueError):
            prepare(data, "controller.executor")

    def test_live_rejects_wrong_context(self):
        import os
        from unittest.mock import patch
        from . import live_executor
        valid = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_ACTIONS": "true",
                 "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"}
        for changes in ({"GITHUB_ACTIONS": "false"}, {"GITHUB_EVENT_NAME": "pull_request"},
                        {"GITHUB_EVENT_NAME": "issue_comment"}, {"GITHUB_REF": "refs/heads/topic"}):
            with self.subTest(changes=changes), patch.dict(os.environ, dict(valid, **changes), clear=True), \
                    patch.object(live_executor.subprocess, "run") as command, patch.object(live_executor, "api") as api:
                with self.assertRaisesRegex(ValueError, "manual GitHub Actions run on main"):
                    live_executor.live_ci()
                command.assert_not_called()
                api.assert_not_called()

    def test_live_rejects_stale_checkout(self):
        import os
        import subprocess
        from unittest.mock import patch
        from . import live_executor
        sha = "a" * 40
        env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_ACTIONS": "true",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
               "GITHUB_SHA": sha}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(live_executor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "b" * 40)) as command, \
                patch.object(live_executor, "api") as api:
            with self.assertRaisesRegex(ValueError, "not current trusted main"):
                live_executor.live_ci()
            command.assert_called_once_with(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
            api.assert_not_called()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(live_executor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, sha)) as command, \
                patch.object(live_executor, "api", return_value={"commit": {"sha": "b" * 40}}) as api:
            with self.assertRaisesRegex(ValueError, "not current trusted main"):
                live_executor.live_ci()
            command.assert_called_once_with(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
            api.assert_called_once_with("repos/owner/repo/branches/main")
