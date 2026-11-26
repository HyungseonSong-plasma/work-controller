"""One Controller operation cycle; existing contracts own all decisions and writes."""
import subprocess
import unittest
from unittest.mock import patch

from controller import decision_engine, executor
from validation.model import REPO

WAIT = {"WAIT_CI", "WAIT_MERGE_GATE", "WAIT_B_APPROVAL", "REFRESH_STATE"}
ACTION = {"PLAN_WORK", "PERFORM_READ_ONLY", "CONTINUE_DRAFT", "FIX_CI", "UPDATE_BRANCH"}
ERRORS = (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.CalledProcessError)


def category(action):
    if action in WAIT:
        return "Wait"
    if action in ACTION:
        return "Action"
    return "Human Review"


def run(repo, work=None, plan=None, execute=False, reviewed_digest=None):
    """Report one discovery cycle; optionally delegate one explicit plan to Executor."""
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise ValueError("Expected owner/repo")
    if (work is None) != (plan is None):
        raise ValueError("Work and plan must be supplied together")
    if work is not None and (type(work) is not int or work < 1 or not isinstance(plan, dict)):
        raise ValueError("Expected positive Work number and JSON object plan")
    if execute != bool(reviewed_digest) or (execute and plan is None):
        raise ValueError("Execution requires a plan and reviewed digest")
    result = {"repository": repo, "state": "REPORTED", "discovery": None, "execution": None}
    try:
        report = decision_engine.discover(repo)
        if (report["repository"].casefold() != repo.casefold()
                or not isinstance(report["works"], list) or not isinstance(report["unassigned_prs"], list)):
            raise ValueError("Invalid discovery report")
        result["discovery"] = report
        if plan is None:
            return result
        targets = [item for item in report["works"] if item["work"] == work]
        if (len(targets) != 1 or targets[0].get("work_type") != "Implementation"
                or [a["action"] for a in targets[0]["actions"]] != ["PLAN_WORK"]
                or any(work in p.get("references", []) for p in report["unassigned_prs"])):
            result.update(state="HUMAN_REVIEW", reason="Target must have one unambiguous Implementation PLAN_WORK decision")
            return result
    except ERRORS as exc:
        result.update(state="HUMAN_REVIEW", reason=str(exc))
        return result
    try:
        execution = executor.run(repo, work, plan, execute, reviewed_digest)
        result["execution"] = execution
        result["state"] = execution["state"]
    except ERRORS as exc:
        result.update(state="HUMAN_REVIEW", reason=str(exc),
                      partial_write_possible=execute)
    return result


def summary(report):
    """Render only controlled action names and numeric references, not untrusted Issue text."""
    lines = ["## Controller operation", "", "Result: " + (
        report["state"] if report["state"] in {"REPORTED", "READY", "PUBLISHED"} else "Human Review"), ""]
    discovery = report.get("discovery")
    if discovery is None:
        return "\n".join(lines + ["Discovery failed; inspect the run log.\n"])
    repo = report["repository"]
    groups = {"Action": [], "Wait": [], "Human Review": []}
    for work in discovery["works"]:
        number = work["work"]
        if type(number) is not int:
            raise ValueError("Invalid Work reference")
        for item in work["actions"]:
            action = item["action"]
            group = category(action)
            safe = action if isinstance(action, str) and action.isascii() and action.replace("_", "").isalnum() and action.isupper() else "UNKNOWN_ACTION"
            reference = f"[Work #{number}](https://github.com/{repo}/issues/{number})"
            if type(item.get("pr")) is int:
                pr = item["pr"]
                reference += f" / [PR #{pr}](https://github.com/{repo}/pull/{pr})"
            groups[group].append(f"- {reference}: `{safe}`")
    for item in discovery["unassigned_prs"]:
        number = item["pr"]
        if type(number) is not int:
            raise ValueError("Invalid PR reference")
        groups["Human Review"].append(f"- [PR #{number}](https://github.com/{repo}/pull/{number}): `FIX_WORK_REFERENCE`")
    for group, entries in groups.items():
        lines += [f"### {group} ({len(entries)})", "", *(entries or ["None"]), ""]
    return "\n".join(lines)


class OperationTests(unittest.TestCase):
    repo = "owner/repo"
    plan = {"title": "change", "body": "example", "files": []}

    def report(self, action="PLAN_WORK", kind="Implementation"):
        return {"repository": self.repo, "mode": "read-only", "works": [
            {"work": 4, "work_type": kind, "actions": [{"action": action}]},
            {"work": 5, "work_type": "Implementation", "actions": [{"action": "WAIT_B_APPROVAL"}]},
        ], "unassigned_prs": [{"pr": 8, "references": [], "action": "FIX_WORK_REFERENCE"}]}

    def test_report_only(self):
        with patch.object(decision_engine, "discover", return_value=self.report()) as discover, patch.object(executor, "run") as publish:
            result = run(self.repo)
            self.assertEqual(result["state"], "REPORTED")
            self.assertIsNone(result["execution"])
            self.assertIn("Work #4", summary(result))
            self.assertIn("WAIT_B_APPROVAL", summary(result))
            self.assertIn("PR #8", summary(result))
            discover.assert_called_once_with(self.repo)
            publish.assert_not_called()

    def test_dispatch_and_dry_run(self):
        for execute, digest, state in [(False, None, "READY"), (True, "reviewed", "PUBLISHED")]:
            with patch.object(decision_engine, "discover", return_value=self.report()) as discover, patch.object(executor, "run", return_value={"state": state}) as publish:
                result = run(self.repo, 4, self.plan, execute, digest)
                self.assertEqual(result["state"], state)
                self.assertEqual(len(result["discovery"]["works"]), 2)
                discover.assert_called_once_with(self.repo)
                publish.assert_called_once_with(self.repo, 4, self.plan, execute, digest)

    def test_blocked_and_ambiguous(self):
        duplicate = self.report()
        duplicate["works"].append(duplicate["works"][0])
        multiple = self.report()
        multiple["works"][0]["actions"].append({"action": "HUMAN_REVIEW"})
        orphan = self.report()
        orphan["unassigned_prs"][0]["references"] = [4, 7]
        reports = [duplicate, multiple, orphan, self.report("WAIT_B_APPROVAL"),
                   self.report("STOP_B_WORK"), self.report("REVIEW_MERGE"),
                   self.report("PLAN_WORK", "Read-only")]
        for report in reports:
            with patch.object(decision_engine, "discover", return_value=report), patch.object(executor, "run") as publish:
                result = run(self.repo, 4, self.plan, True, "reviewed")
                self.assertEqual(result["state"], "HUMAN_REVIEW")
                publish.assert_not_called()
        with patch.object(decision_engine, "discover", return_value=self.report()), patch.object(executor, "run") as publish:
            self.assertEqual(run(self.repo, 99, self.plan)["state"], "HUMAN_REVIEW")
            publish.assert_not_called()

    def test_failures_and_explicit_arguments(self):
        with patch.object(decision_engine, "discover", side_effect=RuntimeError("API unavailable")), patch.object(executor, "run") as publish:
            self.assertEqual(run(self.repo, 4, self.plan)["state"], "HUMAN_REVIEW")
            publish.assert_not_called()
        with patch.object(decision_engine, "discover", return_value=self.report()), patch.object(executor, "run", return_value={"state": "HUMAN_REVIEW", "phase": "create_pr"}):
            self.assertEqual(run(self.repo, 4, self.plan, True, "reviewed")["execution"]["phase"], "create_pr")
        with patch.object(decision_engine, "discover") as discover:
            for args in [(4, None), (None, self.plan), (4, self.plan, True), (None, None, True, "reviewed")]:
                with self.assertRaises(ValueError):
                    run(self.repo, *args)
            discover.assert_not_called()
