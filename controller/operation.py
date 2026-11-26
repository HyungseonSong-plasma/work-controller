"""One Controller operation cycle; existing contracts own all decisions and writes."""
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from controller import decision_engine, executor
from validation import authority, issue as issue_contract
from validation.model import REPO, SHA, api

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


COMMAND = "/controller run"


def request_identity(repo, event, sha, actor, run_id, attempt):
    """Recheck a created comment against trusted policy and current GitHub records."""
    if not REPO.fullmatch(repo) or not SHA.fullmatch(sha):
        raise ValueError("Invalid repository or checkout SHA")
    if any(type(n) is not int or n < 1 for n in (run_id, attempt)):
        raise ValueError("Invalid run identity")
    if event["action"] != "created" or event["repository"]["full_name"].casefold() != repo.casefold():
        raise ValueError("Invalid request event")
    original_issue, original = event["issue"], event["comment"]
    work, comment = original_issue["number"], original["id"]
    if any(type(n) is not int or n < 1 for n in (work, comment)):
        raise ValueError("Invalid Work or comment number")
    if ("pull_request" in original_issue or original_issue["state"] != "open"
            or original["body"] != COMMAND or not original["created_at"]
            or original["created_at"] != original["updated_at"]):
        raise ValueError("Expected a new exact command on an open Work")
    root = f"repos/{repo}"
    if api(f"{root}/branches/main")["commit"]["sha"] != sha:
        raise ValueError("Checkout is no longer current main")
    owner = authority.read_policy(repo, sha)["owner"].casefold()
    if any(login.casefold() != owner for login in
           (actor, event["sender"]["login"], original["user"]["login"])):
        raise ValueError("Only the policy owner may request a run")
    current_issue = api(f"{root}/issues/{work}")
    if (current_issue["number"] != work or current_issue["state"] != "open"
            or "pull_request" in current_issue or not issue_contract.validate(current_issue["body"])):
        raise ValueError("Current Work is closed, invalid or not an Issue")
    current = api(f"{root}/issues/comments/{comment}")
    expected_url = f"https://api.github.com/repos/{repo}/issues/{work}"
    if (current["id"] != comment or current["issue_url"].casefold() != expected_url.casefold()
            or original["issue_url"].casefold() != expected_url.casefold()
            or current["body"] != COMMAND or current["user"]["login"].casefold() != owner
            or current["user"]["id"] != original["user"]["id"]
            or current["created_at"] != original["created_at"]
            or current["updated_at"] != original["updated_at"]):
        raise ValueError("Request changed or does not belong to this Work")
    return {"work": work, "comment": comment, "run": run_id, "attempt": attempt, "base": sha}


def comment_request(repo):
    """Actions-only, read-only entrypoint; no plan or write options."""
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_EVENT_NAME") != "issue_comment"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_REPOSITORY", "").casefold() != repo.casefold()):
        raise ValueError("Comment request requires trusted-main GitHub Actions")
    sha = os.environ["GITHUB_SHA"]
    checked_out = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                                 capture_output=True, text=True).stdout.strip()
    if checked_out != sha:
        raise ValueError("Checkout does not match event SHA")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
        event = json.load(stream)
    identity = request_identity(repo, event, sha, os.environ["GITHUB_ACTOR"],
                                int(os.environ["GITHUB_RUN_ID"]),
                                int(os.environ["GITHUB_RUN_ATTEMPT"]))
    result = run(repo)
    result["request"] = identity
    return result


def summary(report):
    """Render only controlled action names and numeric references, not untrusted Issue text."""
    lines = ["## Controller operation", "", "Result: " + (
        report["state"] if report["state"] in {"REPORTED", "READY", "PUBLISHED"} else "Human Review"), ""]
    if "request" in report:
        request = report["request"]
        lines += [f"Request: Work #{request['work']}, comment {request['comment']}, run {request['run']}, attempt {request['attempt']}", ""]
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



class CommentRequestTests(unittest.TestCase):
    repo = "owner/repo"
    sha = "a" * 40
    user = {"login": "owner", "id": 12}
    issue = {"number": 4, "state": "open",
             "body": "### Work type\n\nRead-only\n\n### Objective\n\nCheck X\n\n### Scope\n\n#### Constraints\n\n- Read only\n\n#### Actions\n\n- Check X\n\n### Acceptance criteria\n\n- Report"}
    comment = {"id": 51, "user": user, "body": COMMAND,
               "issue_url": "https://api.github.com/repos/owner/repo/issues/4",
               "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}

    def event(self):
        return {"action": "created", "repository": {"full_name": self.repo},
                "issue": dict(self.issue), "comment": dict(self.comment), "sender": self.user}

    def identity(self, event=None, current_issue=None, current_comment=None, actor="owner", base=None):
        with patch(__name__ + ".api", side_effect=[
            {"commit": {"sha": base or self.sha}},
            self.issue if current_issue is None else current_issue,
            self.comment if current_comment is None else current_comment,
        ]), patch.object(authority, "read_policy", return_value={"owner": "owner"}):
            return request_identity(self.repo, event or self.event(), self.sha, actor, 99, 1)

    def test_valid_identity(self):
        self.assertEqual(self.identity(), {"work": 4, "comment": 51, "run": 99,
                                          "attempt": 1, "base": self.sha})

    def test_invalid_event(self):
        event = self.event()
        for changes in ({"action": "edited"}, {"action": "deleted"},
                        {"repository": {"full_name": "other/repo"}},
                        {"sender": {"login": "other"}},
                        {"issue": dict(self.issue, pull_request={})},
                        {"comment": dict(self.comment, body="/CONTROLLER RUN")},
                        {"comment": dict(self.comment, body="ordinary comment")},
                        {"comment": dict(self.comment, updated_at="later")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.identity(dict(event, **changes))
        with self.assertRaises(ValueError):
            self.identity(actor="other")

    def test_stale_current_state(self):
        for current_issue in (dict(self.issue, state="closed"), dict(self.issue, body="bad"),
                              dict(self.issue, pull_request={})):
            with self.assertRaises(ValueError):
                self.identity(current_issue=current_issue)
        for current_comment in (dict(self.comment, body="changed"),
                                dict(self.comment, updated_at="later"),
                                dict(self.comment, issue_url="https://api.github.com/repos/owner/repo/issues/7"),
                                dict(self.comment, user={"login": "other", "id": 12})):
            with self.assertRaises(ValueError):
                self.identity(current_comment=current_comment)
        with self.assertRaises(ValueError):
            self.identity(base="b" * 40)

    def test_fail_closed_and_read_only_dispatch(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "issue_comment",
               "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": self.repo,
               "GITHUB_SHA": self.sha, "GITHUB_EVENT_PATH": "event.json",
               "GITHUB_ACTOR": "owner", "GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}
        identity = self.identity()
        result = {"repository": self.repo, "state": "REPORTED", "discovery": None, "execution": None}
        from unittest.mock import mock_open
        with patch.dict(os.environ, env, clear=True), patch(__name__ + ".subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, self.sha + "\n")), \
                patch("builtins.open", mock_open(read_data=json.dumps(self.event()))), \
                patch(__name__ + ".request_identity", return_value=identity) as check, \
                patch(__name__ + ".run", return_value=dict(result)) as cycle, \
                patch.object(executor, "run") as publish:
            actual = comment_request(self.repo)
            self.assertEqual(actual["request"], identity)
            self.assertIn("comment 51, run 99, attempt 1", summary(actual))
            cycle.assert_called_once_with(self.repo)
            publish.assert_not_called()
            cycle.reset_mock()
            check.side_effect = RuntimeError("API unavailable")
            with self.assertRaises(RuntimeError):
                comment_request(self.repo)
            cycle.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch(__name__ + ".run") as cycle:
            with self.assertRaises(ValueError):
                comment_request(self.repo)
            cycle.assert_not_called()
