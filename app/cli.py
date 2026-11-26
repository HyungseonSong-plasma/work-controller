"""Unified CLI. Invoke from repository root: python3 -m app.cli."""
import argparse
import json
import subprocess
import sys
import unittest

from validation import issue, pr, merge, completion, authority
from validation.model import REPO, SHA, api, digest
from validation.merge import read_snapshot, verify
from validation.completion import read_assessment, approved_close, guard_closed
from controller import decision_engine
from controller.decision_engine import discover


def issue_command(argv):
    parser = argparse.ArgumentParser(prog="issue")
    parser.add_argument("--require-type", action="store_true")
    args = parser.parse_args(argv)
    return int(not issue.validate(sys.stdin.read(), require_type=args.require_type))


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
        permission = authority.inspect_pr(args.repo, args.pr, data["head"], data["base"], args.approved_change)
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


COMMANDS = {"authority": authority_command, "issue": issue_command, "pr": pr_command, "merge": merge_command,
            "completion": completion_command, "controller": controller_command}
MODULES = {"authority": authority, "issue": issue, "pr": pr, "merge": merge, "completion": completion,
           "controller": decision_engine}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[*COMMANDS, "test"])
    args, rest = parser.parse_known_args(argv)
    if args.command == "test":
        tests = argparse.ArgumentParser(prog="test")
        tests.add_argument("module", nargs="?", choices=[*MODULES, "all"], default="all")
        selection = tests.parse_args(rest).module
        modules = MODULES.values() if selection == "all" else [MODULES[selection]]
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(m) for m in modules)
        return int(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
    return COMMANDS[args.command](rest) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Contract command failed: {exc}", file=sys.stderr)
        sys.exit(1)
