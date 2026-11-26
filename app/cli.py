"""Unified CLI. Invoke from repository root: python3 -m app.cli."""
import argparse
import json
import os
import subprocess
import sys
import unittest

from validation import issue, pr, merge, completion, authority, approval
from validation.model import REPO, SHA, api, digest
from validation.merge import read_snapshot, verify
from validation.completion import read_assessment, approved_close, guard_closed
from controller import decision_engine, executor, operation
from controller.decision_engine import discover


def issue_command(argv):
    parser = argparse.ArgumentParser(prog="issue")
    parser.parse_args(argv)
    return int(not issue.validate(sys.stdin.read()))


def pr_command(argv):
    parser = argparse.ArgumentParser(prog="pr")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--work-number", action="store_true")
    mode.add_argument("--issue-json", type=int)
    args = parser.parse_args(argv)
    if args.work_number:
        print(pr.work_number(sys.stdin.read()))
        return 0
    problems = (pr.work_errors(args.issue_json, json.load(sys.stdin))
                if args.issue_json is not None else pr.errors(sys.stdin.read()))
    for problem in problems:
        print(problem, file=sys.stderr)
    return int(bool(problems))


def merge_command(argv):
    parser = argparse.ArgumentParser(description=None)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--approved-snapshot", help="Digest of metadata and code revision reviewed for scope and evidence")
    parser.add_argument("--expected-head")
    parser.add_argument("--approved-change", help="B-grade scope digest, only after explicit user approval")
    parser.add_argument("--merge", action="store_true", help="Explicitly request a squash merge after verification")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo) or args.pr < 1:
        parser.error("Expected owner/repo and a positive PR number")
    if not args.approved_snapshot:
        if args.merge or args.expected_head:
            parser.error("Merge/verification requires an approved snapshot and expected head")
        data, _ = read_snapshot(args.repo, args.pr)
        print(json.dumps({"snapshot": data, "review_digest": digest(data)}, indent=2, ensure_ascii=False))
        return
    if not args.expected_head or not SHA.fullmatch(args.expected_head):
        parser.error("Verification requires --expected-head")
    data = verify(args.repo, args.pr, args.approved_snapshot, args.expected_head)
    if args.merge:
        evidence = approval.inspect_pr(args.repo, args.pr, data["head"], data["base"])
        if not evidence["allowed"]:
            raise ValueError(evidence["reason"])
        approved_change = evidence.get("approval_digest")
        if args.approved_change and args.approved_change != approved_change:
            raise ValueError("Supplied approval digest does not match recorded evidence")
        permission = authority.inspect_pr(args.repo, args.pr, data["head"], data["base"], approved_change)
        if not permission["allowed"]:
            raise ValueError(permission["reason"])
        data = verify(args.repo, args.pr, args.approved_snapshot, args.expected_head)
        result = api(f"repos/{args.repo}/pulls/{args.pr}/merge", "-X", "PUT",
                     "-f", f"sha={data['head']}", "-f", "merge_method=squash")
        if result.get("merged") is not True:
            raise ValueError("GitHub rejected merge")
        print(json.dumps(result))
    else:
        print("Merge preflight passed for reviewed snapshot " + digest(data))



def completion_command(argv):
    parser = argparse.ArgumentParser(description=None)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--work", required=True, type=int)
    parser.add_argument("--approved-snapshot")
    parser.add_argument("--evidence")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--close", action="store_true")
    mode.add_argument("--guard-closed", action="store_true")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo) or args.work < 1:
        parser.error("Expected owner/repo and positive issue number")
    if args.guard_closed:
        if args.approved_snapshot or args.evidence:
            parser.error("Guard mode does not accept review arguments")
        blockers = guard_closed(args.repo, args.work)
        if blockers:
            raise ValueError("; ".join(blockers))
        print("Structural completion guard passed or not applicable")
    elif args.approved_snapshot:
        approved_close(args.repo, args.work, args.approved_snapshot, args.evidence, args.close)
        print("Work completion preflight passed" + ("; closed as completed" if args.close else ""))
    else:
        if args.close or args.evidence:
            parser.error("Closure requires --approved-snapshot and --evidence")
        data, blockers = read_assessment(args.repo, args.work)
        print(json.dumps({"snapshot": data, "review_digest": digest(data), "blockers": blockers},
                         indent=2, ensure_ascii=False))
        sys.exit(bool(blockers) or data["state"] != "open")



def controller_command(argv):
    parser = argparse.ArgumentParser(description=None)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo):
        parser.error("Expected owner/repo")
    print(json.dumps(discover(args.repo), indent=2, ensure_ascii=False))



def authority_command(argv):
    parser = argparse.ArgumentParser(prog="authority")
    parser.add_argument("--repo", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pr", type=int)
    mode.add_argument("--path", nargs="+")
    parser.add_argument("--grade-a", action="store_true", help="Check A protection only; B approval is separate")
    parser.add_argument("--expected-head")
    parser.add_argument("--expected-base")
    parser.add_argument("--work", type=int, help="Work number for planned path assessment")
    parser.add_argument("--approved-change", help="B-grade scope digest, only after explicit user approval")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo) or (args.pr is not None and args.pr < 1):
        parser.error("Expected owner/repo and positive PR number")
    if args.path is not None and (args.work is None or args.work < 1 or args.approved_change):
        parser.error("Path planning requires --work; approval is supplied only for PR checks")
    if args.pr is not None and args.work is not None:
        parser.error("PR inspection derives the Work from its body")
    if args.grade_a and (args.pr is None or args.approved_change or not args.expected_head or not args.expected_base):
        parser.error("Grade-A check requires PR, expected head and base, and no approval override")
    if not args.grade_a and (args.expected_head or args.expected_base):
        parser.error("Expected revisions are only used by grade-a")
    result = (authority.inspect_pr(args.repo, args.pr, expected_head=args.expected_head, expected_base=args.expected_base, approved_change=args.approved_change, grade_a_only=args.grade_a) if args.pr is not None
              else authority.plan_paths(args.repo, args.work, args.path))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(not result["allowed"]) if args.pr is not None else 0


def approval_command(argv):
    parser = argparse.ArgumentParser(prog="approval")
    parser.add_argument("--repo", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pr", type=int)
    mode.add_argument("--work", type=int)
    mode.add_argument("--refresh-work", type=int)
    parser.add_argument("--path", nargs="+")
    parser.add_argument("--request", action="store_true")
    parser.add_argument("--expected-head")
    parser.add_argument("--expected-base")
    parser.add_argument("--target-url")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo):
        parser.error("Expected owner/repo")
    if args.refresh_work is not None:
        if args.refresh_work < 1 or not args.expected_base or not SHA.fullmatch(args.expected_base) or not args.target_url or args.expected_head or args.path or args.request:
            parser.error("Refresh requires Work, trusted base SHA and target URL")
        output = approval.refresh_work(args.repo, args.refresh_work, args.expected_base, args.target_url)
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return int(any(x.get("publish_error") for x in output))
    if args.work is not None:
        if args.work < 1 or args.expected_head or args.expected_base or args.target_url or (args.request and not args.path):
            parser.error("Work inspection or --request with --path")
        result = approval.request(args.repo, args.work, args.path) if args.request else approval.inspect_work(args.repo, args.work, args.path)
    else:
        if args.pr < 1 or args.path or args.request or args.target_url or bool(args.expected_head) != bool(args.expected_base):
            parser.error("PR validation requires --pr and optionally both expected revisions")
        result = approval.inspect_pr(args.repo, args.pr, args.expected_head, args.expected_base)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(not result["allowed"]) if args.pr is not None else 0


def executor_command(argv):
    parser = argparse.ArgumentParser(prog="executor")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--work", required=True, type=int)
    parser.add_argument("--plan", required=True, help="JSON file containing title, body and files; - for stdin")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--reviewed-plan", help="Digest returned by a reviewed dry-run")
    args = parser.parse_args(argv)
    if args.execute != bool(args.reviewed_plan):
        parser.error("Execution requires --execute and --reviewed-plan; dry-run uses neither")
    if args.plan == "-":
        plan = json.load(sys.stdin)
    else:
        with open(args.plan, encoding="utf-8") as stream:
            plan = json.load(stream)
    result = executor.run(args.repo, args.work, plan, args.execute, args.reviewed_plan)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(result["state"] not in {"READY", "PUBLISHED"})


def executor_live_ci_command(argv):
    parser = argparse.ArgumentParser(prog="executor-live-ci")
    parser.parse_args(argv)
    executor.live_ci()
    return 0


def operation_command(argv):
    parser = argparse.ArgumentParser(prog="operation")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--work", type=int)
    parser.add_argument("--plan", help="Prepared JSON plan; - for stdin")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--reviewed-plan")
    parser.add_argument("--summary", action="store_true", help="Write GitHub Actions step summary")
    parser.add_argument("--comment-request", action="store_true", help="Validate a read-only Actions issue_comment request")
    args = parser.parse_args(argv)
    if bool(args.plan) != (args.work is not None) or args.execute != bool(args.reviewed_plan) or (args.execute and not args.plan):
        parser.error("Work and plan are paired; execute requires a reviewed plan digest")
    if args.comment_request and (args.plan or args.work is not None or args.execute or args.reviewed_plan):
        parser.error("Comment requests are read-only and cannot supply a plan")
    plan = None
    if args.plan == "-":
        plan = json.load(sys.stdin)
    elif args.plan:
        with open(args.plan, encoding="utf-8") as stream:
            plan = json.load(stream)
    if args.plan and not isinstance(plan, dict):
        parser.error("Plan must be a JSON object")
    if args.comment_request:
        result = operation.comment_request(args.repo)
    else:
        result = operation.run(args.repo, args.work, plan, args.execute, args.reviewed_plan)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.summary:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(operation.summary(result))
    return int(result["state"] not in {"REPORTED", "READY", "PUBLISHED"})


COMMANDS = {"operation": operation_command, "executor-live-ci": executor_live_ci_command, "executor": executor_command, "approval": approval_command, "authority": authority_command, "issue": issue_command, "pr": pr_command, "merge": merge_command,
            "completion": completion_command, "controller": controller_command}
MODULES = {"operation": operation, "executor": executor, "approval": approval, "authority": authority, "issue": issue, "pr": pr, "merge": merge, "completion": completion,
           "controller": decision_engine}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[*COMMANDS, "test"])
    args, rest = parser.parse_known_args(argv)
    if args.command == "test":
        tests = argparse.ArgumentParser(prog="test")
        tests.add_argument("module", nargs="?", choices=[*MODULES, "all"], default="all")
        selection = tests.parse_args(rest).module
        from tests import runner
        names = list(MODULES) if selection == "all" else [selection]
        suite = unittest.TestSuite()
        for name in names:
            if name in runner.FILES:
                # The shared runner loads JSON and any registered Python scenarios.
                suite.addTests(runner.load_suite(name))
            else:
                suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(MODULES[name]))
        if selection in ("all", "issue"):
            suite.addTests(runner.self_suite())
        return int(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
    return COMMANDS[args.command](rest) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Contract command failed: {exc}", file=sys.stderr)
        sys.exit(1)
