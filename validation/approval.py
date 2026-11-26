"""B-rank approval: request scope + owner-operated GitHub labels and timeline."""
import json
import re
import subprocess
import time
import unittest
from unittest.mock import patch

from . import authority
from .model import api, pages, digest
from .pr import work_number
from .completion import declared_works

HEADER = "### B-rank Approval Request"
PENDING = "B-rank: pending"
APPROVED = "B-rank: approved"
REVOKED = "B-rank: revoked"
LABELS = {PENDING, APPROVED, REVOKED}
DIGEST = re.compile(r"[0-9a-f]{64}")
ERRORS = (ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError)


def b_paths(classification):
    return sorted(p for p, grade in classification["paths"].items() if grade == "B")


def format_request(paths, scope_digest):
    if (not isinstance(paths, list) or not paths or any(not authority.path_valid(p) for p in paths)
            or paths != sorted(set(paths)) or not isinstance(scope_digest, str)
            or not DIGEST.fullmatch(scope_digest)):
        raise ValueError("Expected sorted unique B paths and scope digest")
    return HEADER + "\n\n```json\n" + json.dumps(
        {"paths": paths, "digest": scope_digest}, ensure_ascii=False, indent=2) + "\n```"


def parse_request(comment):
    if not comment.get("created_at") or comment["created_at"] != comment.get("updated_at"):
        raise ValueError("Approval request was edited or has missing timestamps")
    body = (comment.get("body") or "").replace("\r\n", "\n").strip()
    match = re.fullmatch(re.escape(HEADER) + r"\n\n```json\n([\s\S]+)\n```", body)
    if not match:
        raise ValueError("Malformed approval request")
    data = json.loads(match.group(1))
    if not isinstance(data, dict) or set(data) != {"paths", "digest"}:
        raise ValueError("Invalid approval request fields")
    format_request(data["paths"], data["digest"])
    return data


def labels(issue):
    return {x["name"] for x in issue.get("labels", [])} & LABELS


def label_events(events):
    return sorted((e for e in events if e.get("event") in {"labeled", "unlabeled"}
                   and (e.get("label") or {}).get("name") in LABELS), key=lambda e: e["id"])


def result(state, reason, **extra):
    return dict(state=state, allowed=state in {"APPROVED", "NOT_REQUIRED"}, reason=reason, **extra)


def evaluate(repo, work, issue, policy, comments, events, expected_paths=None):
    current = labels(issue)
    history = label_events(events)
    if len(current) > 1:
        return result("HUMAN_REVIEW", "Conflicting approval labels")
    if current == {REVOKED}:
        return result("REVOKED", "Owner approval revoked")
    if not current:
        if history and history[-1]["event"] == "unlabeled" and history[-1]["label"]["name"] == APPROVED:
            return result("REVOKED", "Approved label removed")
        if not history and not any((c.get("body") or "").startswith(HEADER) for c in comments) and expected_paths is None:
            return result("NOT_REQUESTED", "No B approval requested")
        return result("HUMAN_REVIEW", "No unambiguous current approval label")
    pending = [e for e in history if e["event"] == "labeled" and e["label"]["name"] == PENDING]
    if not pending:
        return result("HUMAN_REVIEW", "Missing pending approval epoch")
    epoch = pending[-1]["created_at"]
    requests = [c for c in comments if (c.get("body") or "").startswith(HEADER)
                and c.get("created_at", "") >= epoch]
    if len(requests) != 1 or requests[0]["created_at"] <= epoch:
        return result("HUMAN_REVIEW", "Missing, duplicate or ambiguously timed request in current pending epoch")
    request = requests[0]
    try:
        record = parse_request(request)
        classification = authority.classify(policy, record["paths"])
        if classification["grade"] != "B" or b_paths(classification) != record["paths"]:
            raise ValueError("Request no longer consists exclusively of B paths")
        required = authority.approval_scope(repo, work, issue, policy, classification)
        if record["digest"] != required or (expected_paths is not None and expected_paths != record["paths"]):
            raise ValueError("Work, policy or B paths differ from request")
    except ERRORS as exc:
        return result("HUMAN_REVIEW", str(exc))
    details = {"approval_digest": required, "comment_id": request["id"], "paths": record["paths"]}
    if current == {PENDING}:
        return result("PENDING", "Waiting for owner approval label", **details)
    # Current approved label must have been applied after this request by the policy owner.
    approvals = [e for e in history if e["label"]["name"] == APPROVED]
    last = approvals[-1] if approvals else {}
    revocations = [e for e in history if e["event"] == "labeled" and e["label"]["name"] == REVOKED]
    if (last.get("event") != "labeled" or last.get("created_at", "") <= request["created_at"]
            or (last.get("actor") or {}).get("login", "").casefold() != policy["owner"].casefold()
            or (revocations and revocations[-1]["id"] > pending[-1]["id"])):
        return result("HUMAN_REVIEW", "Approval event missing, stale, ambiguous or not attributed to owner", **details)
    return result("APPROVED", "Matching request and owner approval label", label_event_id=last["id"], **details)


def read_state(repo, work, policy, expected_paths=None):
    issue = api(f"repos/{repo}/issues/{work}")
    comments = pages(f"repos/{repo}/issues/{work}/comments?per_page=100")
    events = pages(f"repos/{repo}/issues/{work}/events?per_page=100")
    answer = evaluate(repo, work, issue, policy, comments, events, expected_paths)
    fresh = api(f"repos/{repo}/issues/{work}")
    if any(fresh.get(k) != issue.get(k) for k in ("title", "body", "state", "updated_at")) or labels(fresh) != labels(issue):
        raise ValueError("Work changed during approval check")
    return answer


def inspect_work(repo, work, paths=None):
    base = api(f"repos/{repo}/branches/main")["commit"]["sha"]
    policy = authority.read_policy(repo, base)
    if paths is not None:
        classification = authority.classify(policy, paths)
        if classification["grade"] == "A":
            return result("HUMAN_REVIEW", "A-grade paths require owner-only change")
        if classification["grade"] == "C":
            return result("NOT_REQUIRED", "No B-grade paths changed")
        paths = b_paths(classification)
    return read_state(repo, work, policy, paths)


def request(repo, work, paths):
    """Create a new pending epoch and scope record; never add/remove approved or revoked."""
    plan = authority.plan_paths(repo, work, paths)
    if plan["grade"] != "B":
        raise ValueError("Approval request requires B paths and no A paths")
    issue = api(f"repos/{repo}/issues/{work}")
    current = labels(issue)
    if current:
        raise ValueError("Owner must clear prior approval state before starting a new request")
    api(f"repos/{repo}/issues/{work}/labels", "-X", "POST", "-f", "labels[]=" + PENDING)
    time.sleep(1.1)  # GitHub event timestamps have second-level resolution.
    comment = api(f"repos/{repo}/issues/{work}/comments", "-X", "POST", "-f",
                  "body=" + format_request(b_paths(plan), plan["approval_digest"]))
    answer = inspect_work(repo, work, paths)
    if answer["state"] != "PENDING" or answer.get("comment_id") != comment["id"]:
        raise ValueError("Request needs human review: " + answer["reason"])
    return answer


def inspect_pr(repo, number, expected_head=None, expected_base=None):
    pr = api(f"repos/{repo}/pulls/{number}")
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    if (expected_head and head != expected_head) or (expected_base and base != expected_base):
        raise ValueError("PR revision changed before B approval check")
    classification = authority.inspect_pr(repo, number, head, base, grade_a_only=True)
    paths = b_paths(classification)
    answer = result("NOT_REQUIRED", "No B-grade paths changed")
    if paths:
        work = work_number(pr.get("body"))
        answer = read_state(repo, work, authority.read_policy(repo, base), paths)
        answer["work"] = work
    fresh = api(f"repos/{repo}/pulls/{number}")
    if (fresh["state"] != "open" or fresh["head"]["sha"] != head or fresh["base"]["sha"] != base
            or fresh.get("body") != pr.get("body") or fresh["base"]["ref"] != "main"):
        raise ValueError("PR changed during B approval check")
    return dict(answer, grade=classification["grade"], paths=paths, head=head, base=base)


def refresh_work(repo, work, trusted_base, target_url):
    """Revalidate linked open PRs after Issue events. Fail closed per PR."""
    output = []
    for pr in pages(f"repos/{repo}/pulls?state=open&per_page=100"):
        if pr["base"]["ref"] != "main" or work not in declared_works(pr.get("body")):
            continue
        number, head = pr["number"], pr["head"]["sha"]
        endpoint = f"repos/{repo}/statuses/{head}"
        def publish(state, description):
            api(endpoint, "-X", "POST", "-f", "context=grade-b", "-f", "state="+state,
                "-f", "description="+description[:140], "-f", "target_url="+target_url)
        try:
            publish("pending", "Refreshing B-rank approval after Work event")
            answer = inspect_pr(repo, number, head, trusted_base)
            publish("success" if answer["allowed"] else "failure", answer["reason"])
        except ERRORS as exc:
            answer = result("HUMAN_REVIEW", str(exc))
            try:
                publish("failure", "B-rank verification error; human review required")
            except ERRORS as publish_error:
                answer["publish_error"] = str(publish_error)
        output.append(dict(answer, pr=number))
    return output


class ApprovalTests(unittest.TestCase):
    repo = "owner/repo"
    policy = {"owner": "owner", "A": [], "B": ["app/", "validation/"], "default": "C"}
    issue = {"number": 3, "title": "Work", "state": "open",
             "body": "### Work type\n\nImplementation\n\n### Objective\n\nX\n\n### Scope\n\n#### Constraints\n\n- X only\n\n#### Actions\n\n- Implement X\n\n### Acceptance criteria\n\nX passes"}
    paths = ["app/cli.py"]

    def fixture(self):
        scope = authority.approval_scope(self.repo, 3, self.issue, self.policy,
                                         authority.classify(self.policy, self.paths))
        comment = {"id": 10, "created_at": "2026-01-01T00:00:02Z", "updated_at": "2026-01-01T00:00:02Z",
                   "body": format_request(self.paths, scope)}
        def event(i, label, kind="labeled", actor="owner"):
            return {"id": i, "event": kind, "label": {"name": label}, "actor": {"login": actor},
                    "created_at": f"2026-01-01T00:00:{i:02d}Z"}
        return comment, [event(1, PENDING), event(3, PENDING, "unlabeled"), event(4, APPROVED)]

    def state(self, current, comments, events, paths=None, **issue_changes):
        issue = dict(self.issue, labels=[{"name": x} for x in current], **issue_changes)
        return evaluate(self.repo, 3, issue, self.policy, comments, events, paths)["state"]

    def test_approval_states(self):
        c, events = self.fixture()
        self.assertEqual(self.state([APPROVED], [c], events, self.paths), "APPROVED")
        self.assertEqual(self.state([PENDING], [c], events[:1], self.paths), "PENDING")
        self.assertEqual(self.state([REVOKED], [c], events), "REVOKED")
        removed = dict(events[-1], id=5, event="unlabeled")
        self.assertEqual(self.state([], [c], events+[removed]), "REVOKED")
        self.assertEqual(self.state([], [], []), "NOT_REQUESTED")
        self.assertEqual(self.state([], [], [], self.paths), "HUMAN_REVIEW")

    def test_abnormal(self):
        c, events = self.fixture()
        for comments in ([], [dict(c, updated_at="later")], [c,c], [dict(c, body=HEADER+" bad")]):
            self.assertEqual(self.state([APPROVED], comments, events, self.paths), "HUMAN_REVIEW")
        self.assertEqual(self.state([APPROVED, PENDING], [c], events), "HUMAN_REVIEW")
        self.assertEqual(self.state([APPROVED], [c], events, ["validation/pr.py"]), "HUMAN_REVIEW")
        self.assertEqual(self.state([APPROVED], [c], events, self.paths, title="Changed"), "HUMAN_REVIEW")
        self.assertEqual(self.state([APPROVED], [c], events[:-1]+[dict(events[-1], actor={"login":"other"})]), "HUMAN_REVIEW")
        new_epoch = dict(events[0], id=5, created_at="2026-01-01T00:00:05Z")
        self.assertEqual(self.state([APPROVED], [c], events+[new_epoch]), "HUMAN_REVIEW")
        self.assertEqual(self.state([APPROVED], [c], events[:-1]+[dict(events[-1], created_at=c["created_at"])]), "HUMAN_REVIEW")

    def test_c_only(self):
        pr = {"state":"open", "head":{"sha":"b"*40}, "base":{"sha":"c"*40,"ref":"main"}, "body":""}
        with patch(__name__+".api", return_value=pr), patch.object(authority,"inspect_pr",return_value=authority.classify(self.policy,["README.md"])), patch(__name__+".read_state") as read:
            self.assertTrue(inspect_pr(self.repo, 1)["allowed"])
            read.assert_not_called()
