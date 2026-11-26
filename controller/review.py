"""Trusted review bridge: read-only verification, manual and opt-in execution."""
import os
import json
import subprocess
import hashlib
import io
import stat
import zipfile
from urllib import request as http_request, error as http_error
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
import re
import time
from controller import operation
from validation import approval, authority, completion, merge
from validation.model import REPO, SHA, api, digest

DIGEST = re.compile(r"[0-9a-f]{64}")
NUMBER = re.compile(r"[1-9][0-9]*")


def number(value):
    if not isinstance(value, str) or not NUMBER.fullmatch(value):
        raise ValueError("Expected positive decimal identifier")
    return int(value)


def parse_command(command):
    if not isinstance(command, str) or len(command) > 256:
        raise ValueError("Invalid review command")
    match = re.fullmatch(r"/review merge snapshot ([1-9][0-9]*)", command)
    if match:
        return ("merge", "snapshot", number(match[1]), None, None)
    match = re.fullmatch(r"/review merge verify ([1-9][0-9]*) ([0-9a-f]{64}) ([0-9a-f]{40})", command)
    if match:
        return ("merge", "verify", number(match[1]), match[2], match[3])
    if command == "/review completion snapshot":
        return ("completion", "snapshot", None, None, None)
    match = re.fullmatch(r"/review completion verify ([0-9a-f]{64})", command)
    if match:
        return ("completion", "verify", None, match[1], None)
    raise ValueError("Unsupported review command")


def current_main(repo, sha):
    if not REPO.fullmatch(repo) or not SHA.fullmatch(sha):
        raise ValueError("Invalid repository or trusted SHA")
    if api(f"repos/{repo}/branches/main")["commit"]["sha"] != sha:
        raise ValueError("Checkout is no longer current main")


def preflight(repo, identity, command):
    kind, mode, pr, approved, head = parse_command(command)
    work = identity["work"]
    if kind == "merge":
        if mode == "snapshot":
            data, _ = merge.read_snapshot(repo, pr)
        else:
            data = merge.verify_full(repo, pr, approved, head, expected_work=work)
        if data["work"] != work or data["base"] != identity["base"]:
            raise ValueError("PR must belong to requesting Work and current main")
        blockers = []
    elif mode == "snapshot":
        data, blockers = completion.read_assessment(repo, work)
        if data["state"] != "open":
            raise ValueError("Work must be OPEN")
    else:
        data = completion.approved_close(repo, work, approved)
        blockers = []
    current_main(repo, identity["base"])
    return {"repository": repo, "state": "BLOCKED" if blockers else
            ("VERIFIED" if mode == "verify" else "SNAPSHOT"),
            "mode": "read-only", "execution": None,
            "request": dict(identity, command=command, pr=pr),
            "snapshot": data, "review_digest": digest(data), "blockers": blockers}


REFRESH_TIMEOUT = 90
REFRESH_INTERVAL = 3
UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def refresh_ready(repo, identity, head, created_at):
    """Observe the latest status on the exact PR head, never accept an old success.

    A status/run/time match is readiness evidence only: GitHub does not expose
    an exact triggering comment ID in the approval run metadata. Concurrent
    issue events can still race; the full verifier remains authoritative.
    """
    combined = api(f"repos/{repo}/commits/{head}/status")
    if combined.get("sha") != head or not isinstance(combined.get("statuses"), list):
        raise ValueError("Missing or mismatched head status")
    statuses = combined["statuses"]
    if any(s.get("state") in {"failure", "error"} for s in statuses
           if s.get("context") in {"grade-a", "grade-b"}):
        raise ValueError("Terminal FileRank status failure")
    matches = [s for s in statuses if s.get("context") == "grade-b"]
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous grade-b status")
    status = matches[0]
    stamp = status.get("created_at", "")
    if not isinstance(stamp, str) or not UTC_TIMESTAMP.fullmatch(stamp):
        raise ValueError("Invalid grade-b status timestamp")
    if status.get("state") not in {"pending", "success"}:
        raise ValueError("Unexpected grade-b status state")
    if stamp < created_at or status["state"] == "pending":
        return None
    url = status.get("target_url", "")
    match = re.fullmatch(r"https://github\.com/" + re.escape(repo)
                         + r"/actions/runs/([1-9][0-9]*)", url, re.IGNORECASE)
    if not match:
        raise ValueError("Missing grade-b workflow run reference")
    run_id = int(match[1])
    run = api(f"repos/{repo}/actions/runs/{run_id}")
    if (run.get("id") != run_id or run.get("path") != ".github/workflows/grade-b.yml"
            or run.get("event") != "issue_comment" or run.get("head_sha") != identity["base"]
            or run.get("head_branch") != "main"):
        return None
    started = run.get("created_at", "")
    if not isinstance(started, str) or not UTC_TIMESTAMP.fullmatch(started):
        raise ValueError("Invalid approval run timestamp")
    if started < created_at:
        return None
    if run.get("status") != "completed":
        return None
    if run.get("conclusion") != "success":
        raise ValueError("Approval refresh workflow failed")
    return {"run": run_id, "status_created_at": stamp,
            "correlation": "head/status/run/time; triggering comment ID unavailable"}


def wait_merge_refresh(repo, identity, pr, approved, head, created_at):
    """Bounded read-only readiness wait, not an approval or a retry of a write."""
    if not isinstance(created_at, str) or not UTC_TIMESTAMP.fullmatch(created_at):
        raise ValueError("Invalid review comment timestamp")
    current_main(repo, identity["base"])
    initial = approval.inspect_pr(repo, pr, head, identity["base"])
    if not initial["allowed"]:
        raise ValueError(initial["reason"])
    deadline = time.monotonic() + REFRESH_TIMEOUT
    # Let the comment-triggered refresh start before considering status evidence.
    time.sleep(REFRESH_INTERVAL)
    while time.monotonic() < deadline:
        current_main(repo, identity["base"])
        data, state = merge.read_snapshot(repo, pr)
        if (data["work"] != identity["work"] or data["base"] != identity["base"]
                or data["head"] != head or digest(data) != approved):
            raise ValueError("Reviewed Work or PR changed while waiting for approval refresh")
        if state.get("mergeable") is False or state.get("mergeable_state") not in {"clean", "blocked", "unknown"}:
            raise ValueError("PR is not eligible for approval refresh wait")
        ready = refresh_ready(repo, identity, head, created_at)
        if ready is not None and state.get("mergeable") is True and state.get("mergeable_state") == "clean":
            latest = approval.inspect_pr(repo, pr, head, identity["base"])
            if not latest["allowed"] or latest != initial:
                raise ValueError("Approval changed while waiting for refresh")
            if time.monotonic() >= deadline:
                break
            return ready
        time.sleep(min(REFRESH_INTERVAL, max(0, deadline - time.monotonic())))
    raise ValueError("Timed out waiting for current grade-b refresh and clean PR; human review required")


def comment_request(repo):
    event, sha, actor, run, attempt = operation.event_context(repo, "issue_comment")
    command = event["comment"]["body"]
    kind, mode, pr, approved, head = parse_command(command)
    identity = operation.request_identity(repo, event, sha, actor, run, attempt, command=command)
    if kind == "merge" and mode == "verify":
        refresh = wait_merge_refresh(repo, identity, pr, approved, head, event["comment"]["created_at"])
        # The request may have been edited/deleted, or main may have changed while waiting.
        if operation.request_identity(repo, event, sha, actor, run, attempt, command=command) != identity:
            raise ValueError("Review request identity changed")
        result = preflight(repo, identity, command)
        result["refresh"] = refresh
        return result
    return preflight(repo, identity, command)


def manual_identity(repo, event, sha, actor, run, attempt):
    current_main(repo, sha)
    owner = authority.read_policy(repo, sha)["owner"].casefold()
    if (event["repository"]["full_name"].casefold() != repo.casefold()
            or event["ref"] not in {"main", "refs/heads/main"}
            or type(run) is not int or run < 1 or attempt != 1
            or any(x.casefold() != owner for x in
                   (actor, event["sender"]["login"], os.environ["GITHUB_TRIGGERING_ACTOR"]))):
        raise ValueError("Execution requires initial owner manual dispatch on main")
    return {"run": run, "attempt": attempt, "base": sha, "actor": actor}


def normalize_dispatch_inputs(inputs):
    """Accept omitted optional workflow inputs without weakening required fields."""
    required = {"action", "work", "snapshot"}
    optional = {"pr", "head", "evidence"}
    if not isinstance(inputs, dict) or not required <= inputs.keys() or inputs.keys() - (required | optional):
        raise ValueError("Unexpected manual dispatch inputs: require action, work, snapshot; optional pr, head, evidence")
    if any(not isinstance(v, str) for v in inputs.values()):
        raise ValueError("Dispatch inputs must be strings")
    return {**dict.fromkeys(optional, ""), **inputs}


def execute(repo, identity, inputs):
    inputs = normalize_dispatch_inputs(inputs)
    work = number(inputs["work"])
    approved = inputs["snapshot"]
    if not DIGEST.fullmatch(approved):
        raise ValueError("Expected reviewed snapshot digest")
    current_main(repo, identity["base"])
    if inputs["action"] == "merge":
        pr = number(inputs["pr"])
        head = inputs["head"]
        if not SHA.fullmatch(head) or inputs["evidence"]:
            raise ValueError("Merge requires expected head and no closure evidence")
        data, _ = merge.read_snapshot(repo, pr)
        if data["work"] != work or data["base"] != identity["base"]:
            raise ValueError("PR must belong to requested Work and current main")
        result = merge.merge_approved(repo, pr, approved, head, expected_work=work)
        fresh = api(f"repos/{repo}/pulls/{pr}")
        if (fresh.get("merged") is not True or fresh.get("merge_commit_sha") != result.get("sha")
                or fresh["head"]["sha"] != head or fresh["base"]["ref"] != "main"
                or fresh["base"]["repo"]["full_name"].casefold() != repo.casefold()):
            raise ValueError("Merge not confirmed; inspect state before retry")
        return {"repository": repo, "state": "MERGED", "request": identity,
                "work": work, "pr": pr, "head": head, "review_digest": approved, "merge_commit": result["sha"]}
    if inputs["action"] == "close":
        if inputs["pr"] or inputs["head"] or not inputs["evidence"].strip():
            raise ValueError("Close requires acceptance evidence and no PR/head")
        completion.approved_close(repo, work, approved, inputs["evidence"], close=True)
        fresh = api(f"repos/{repo}/issues/{work}")
        if fresh.get("state") != "closed" or fresh.get("state_reason") != "completed":
            raise ValueError("Closure not confirmed; inspect state before retry")
        return {"repository": repo, "state": "CLOSED", "request": identity,
                "work": work, "review_digest": approved}
    raise ValueError("Unsupported execution action")


DELEGATION_MAX_AGE = 1800
MAX_REVIEW_JSON = 1024 * 1024
MAX_REVIEW_ARCHIVE = MAX_REVIEW_JSON + 65536


def timestamp(value):
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        raise ValueError("Invalid review timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_execution(command, work):
    """Exact, data-only request. The Work is taken from the Issue, never the text."""
    if not isinstance(command, str) or len(command) > 1000:
        raise ValueError("Invalid delegated execution command")
    m = re.fullmatch(r"/execute-review merge ([1-9][0-9]*) ([1-9][0-9]*) ([1-9][0-9]*) ([0-9a-f]{64}) ([0-9a-f]{40})", command)
    if m:
        return number(m[1]), number(m[2]), {"action": "merge", "work": str(work), "pr": m[3],
                                              "snapshot": m[4], "head": m[5], "evidence": ""}
    m = re.fullmatch(r"/execute-review close ([1-9][0-9]*) ([1-9][0-9]*) ([0-9a-f]{64})\nEvidence: ([^\r\n]{1,500})", command)
    if m and m[4].isprintable() and m[4].strip():
        return number(m[1]), number(m[2]), {"action": "close", "work": str(work), "pr": "",
                                              "snapshot": m[3], "head": "", "evidence": m[4]}
    raise ValueError("Unsupported delegated execution command")


class NoRedirect(http_request.HTTPRedirectHandler):
    """Do not forward the GitHub token to an artifact storage endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_review_archive(repo, artifact_id):
    if not REPO.fullmatch(repo) or type(artifact_id) is not int or artifact_id < 1:
        raise ValueError("Invalid review artifact reference")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("Review artifact download requires GH_TOKEN")
    opener = http_request.build_opener(NoRedirect())
    url = f"https://api.github.com/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    req = http_request.Request(url, headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28",
                               "User-Agent": "work-controller"})
    try:
        with opener.open(req, timeout=20):
            raise ValueError("Review artifact API did not return the expected redirect")
    except http_error.HTTPError as exc:
        code, location = exc.code, exc.headers.get("Location")
        exc.close()
        if code != 302:
            raise ValueError(f"Review artifact API failed (HTTP {code})") from None
    except (OSError, TimeoutError):
        raise ValueError("Review artifact API transport failed") from None
    try:
        target = urlsplit(location or "")
        host = target.hostname or ""
        if (target.scheme != "https" or target.username or target.password or target.port not in (None, 443)
                or not host.endswith((".blob.core.windows.net", ".actions.githubusercontent.com",
                                      ".githubusercontent.com")) or target.fragment):
            raise ValueError("Invalid review artifact storage redirect")
    except ValueError:
        raise ValueError("Invalid review artifact storage redirect") from None
    # The signed storage URL comes from GitHub, not a comment or artifact.
    # No Authorization header and no further redirects; never log the URL.
    try:
        with opener.open(http_request.Request(location, headers={"User-Agent": "work-controller"}),
                         timeout=20) as response:
            if response.status != 200:
                raise ValueError("Unexpected review artifact storage response")
            raw = response.read(MAX_REVIEW_ARCHIVE + 1)
    except http_error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise ValueError(f"Review artifact storage failed (HTTP {code})") from None
    except (OSError, TimeoutError):
        raise ValueError("Review artifact storage transport failed") from None
    if not raw or len(raw) > MAX_REVIEW_ARCHIVE:
        raise ValueError("Review artifact archive size invalid")
    return raw


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate review JSON key")
        result[key] = value
    return result


def reject_json_constant(value):
    raise ValueError("Invalid review JSON constant")


def decode_review_archive(raw, expected_digest):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REVIEW_ARCHIVE:
        raise ValueError("Review artifact archive size invalid")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("Review artifact archive digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != 1:
                raise ValueError("Review artifact must contain exactly one file")
            entry = entries[0]
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.filename != "review.json" or entry.is_dir() or mode not in (0, stat.S_IFREG)
                    or entry.flag_bits & 1 or not 0 < entry.file_size <= MAX_REVIEW_JSON):
                raise ValueError("Invalid review artifact file layout or size")
            with archive.open(entry) as handle:
                content = handle.read(MAX_REVIEW_JSON + 1)
            if len(content) != entry.file_size or len(content) > MAX_REVIEW_JSON:
                raise ValueError("Review artifact file size mismatch")
        report = json.loads(content.decode("utf-8"), object_pairs_hook=unique_json_object,
                            parse_constant=reject_json_constant)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, UnicodeError,
            json.JSONDecodeError, RecursionError):
        raise ValueError("Malformed review artifact archive or JSON") from None
    if not isinstance(report, dict):
        raise ValueError("Review artifact JSON must be an object")
    return report


def read_review_artifact(repo, run):
    """Select the exact successful preflight attempt's artifact, then verify its bytes."""
    run_id, attempt = run["id"], run["run_attempt"]
    if (not REPO.fullmatch(repo) or type(run_id) is not int or run_id < 1
            or type(attempt) is not int or attempt < 1):
        raise ValueError("Invalid review artifact run reference")
    name = f"review-evidence-{run_id}-{attempt}"
    try:
        listing = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
    except (subprocess.CalledProcessError, OSError, ValueError):
        raise ValueError("Review artifact listing failed") from None
    entries = listing.get("artifacts")
    if not isinstance(entries, list) or listing.get("total_count") != len(entries) or len(entries) > 100:
        raise ValueError("Incomplete review artifact listing")
    matches = [item for item in entries if item.get("name") == name]
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous review attempt artifact")
    item = matches[0]
    artifact_id, size, checksum = item.get("id"), item.get("size_in_bytes"), item.get("digest")
    origin = item.get("workflow_run") or {}
    if (type(artifact_id) is not int or artifact_id < 1 or type(size) is not int
            or not 0 < size <= MAX_REVIEW_ARCHIVE or item.get("expired") is not False
            or not isinstance(checksum, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum)
            or origin.get("id") != run_id or origin.get("head_sha") != run["head_sha"]
            or origin.get("head_branch") != "main"
            or origin.get("repository_id") != run["repository"]["id"]
            or origin.get("head_repository_id") != run["head_repository"]["id"]
            or timestamp(item["expires_at"]) <= datetime.now(timezone.utc)):
        raise ValueError("Review artifact metadata invalid, expired or unrelated")
    return decode_review_archive(download_review_archive(repo, artifact_id), checksum)


def verified_reference(repo, identity, review_run, job_id, inputs, requested_at, owner):
    """Require actual successful, current-base, exactly matched read-only evidence."""
    root = f"repos/{repo}"
    run = api(f"{root}/actions/runs/{review_run}")
    if (run.get("id") != review_run or run.get("path") != ".github/workflows/review-preflight.yml"
            or run.get("event") != "issue_comment" or run.get("head_sha") != identity["base"]
            or run.get("head_branch") != "main" or run.get("status") != "completed"
            or run.get("conclusion") != "success" or run["actor"]["login"].casefold() != owner
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1):
        raise ValueError("Invalid verified review run")
    jobs = api(f"{root}/actions/runs/{review_run}/jobs?per_page=100")
    if jobs.get("total_count") != 1 or len(jobs.get("jobs", [])) != 1:
        raise ValueError("Ambiguous review job list")
    job = jobs["jobs"][0]
    if (job.get("id") != job_id or job.get("run_id") != review_run
            or job.get("run_attempt") != run["run_attempt"] or job.get("name") != "preflight"
            or job.get("head_sha") != identity["base"] or job.get("status") != "completed"
            or job.get("conclusion") != "success"):
        raise ValueError("Invalid verified review job")
    done = timestamp(job["completed_at"])
    requested = timestamp(requested_at)
    now = datetime.now(timezone.utc)
    if not timestamp(run["created_at"]) <= done <= requested <= now or (now - done).total_seconds() > DELEGATION_MAX_AGE:
        raise ValueError("Verified review is expired or not earlier than execution")
    report = read_review_artifact(repo, run)
    request = report["request"]
    snap = report["snapshot"]
    pr = number(inputs["pr"]) if inputs["action"] == "merge" else None
    command = (f"/review merge verify {pr} {inputs['snapshot']} {inputs['head']}" if pr is not None
               else f"/review completion verify {inputs['snapshot']}")
    if (report.get("repository", "").casefold() != repo.casefold()
            or report.get("state") != "VERIFIED" or report.get("mode") != "read-only"
            or "execution" not in report or report["execution"] is not None or report.get("blockers") != []
            or report.get("review_digest") != inputs["snapshot"] or digest(snap) != inputs["snapshot"]
            or request.get("work") != identity["work"] or request.get("pr") != pr
            or request.get("run") != review_run or request.get("attempt") != run["run_attempt"]
            or request.get("base") != identity["base"] or request.get("command") != command
            or type(request.get("comment")) is not int or not 0 < request["comment"] < identity["comment"]
            or snap.get("repository") != repo.casefold() or snap.get("work") != identity["work"]
            or (pr is not None and (snap.get("pr") != pr or snap.get("head") != inputs["head"]
                                    or snap.get("base") != identity["base"]))
            or (pr is None and snap.get("state") != "open")):
        raise ValueError("Verified review evidence does not match execution")
    original = api(f"{root}/issues/comments/{request['comment']}")
    if (original.get("id") != request["comment"] or original.get("body") != command
            or original["user"]["login"].casefold() != owner
            or original.get("issue_url", "").casefold() != f"https://api.github.com/{root}/issues/{identity['work']}".casefold()
            or original.get("created_at") != original.get("updated_at")
            or not timestamp(original["created_at"]) <= timestamp(run["created_at"])):
        raise ValueError("Verified review comment changed or is unrelated")
    return job["completed_at"]


def guard_prior_dispatches(repo, identity, completed_at):
    """Conservative replay guard using existing Actions history, not an approval store.

    Any earlier non-skipped execution run since verification consumes the slot,
    even on a different Work. Missing, ambiguous, or deleted history is not
    durable proof of uniqueness against privileged GitHub writers.
    """
    root = f"repos/{repo}"
    query = urlencode({"created": ">=" + completed_at, "per_page": 100})
    history = api(f"{root}/actions/workflows/review-execute.yml/runs?{query}")
    runs = history.get("workflow_runs", [])
    if history.get("total_count") != len(runs) or len(runs) > 100:
        raise ValueError("Delegation history is incomplete")
    current = [r for r in runs if r.get("id") == identity["run"]]
    if len(current) != 1 or current[0].get("run_attempt") != 1:
        raise ValueError("Current delegated run missing from history")
    start = timestamp(current[0]["created_at"])
    for run in runs:
        if run["id"] == identity["run"] or timestamp(run["created_at"]) > start:
            continue
        if run.get("status") != "completed":
            raise ValueError("Earlier or concurrent delegated run is unresolved; obtain fresh review")
        jobs = api(f"{root}/actions/runs/{run['id']}/jobs?per_page=100")
        entries = jobs.get("jobs", [])
        if jobs.get("total_count") != len(entries) or not entries or any(
                j.get("conclusion") != "skipped" for j in entries):
            raise ValueError("An execution attempt followed the verified review; obtain fresh review")


def delegated_request(repo):
    if os.environ.get("AGENT_REVIEW_DELEGATION") != "v1":
        raise ValueError("Agent review delegation is disabled")
    event, sha, actor, run, attempt = operation.event_context(repo, "issue_comment")
    if attempt != 1 or os.environ.get("GITHUB_TRIGGERING_ACTOR", "").casefold() != actor.casefold():
        raise ValueError("Delegated execution requires initial owner-attributed request")
    command = event["comment"]["body"]
    review_run, job_id, inputs = parse_execution(command, event["issue"]["number"])
    if os.environ.get("REVIEW_ACTION") != inputs["action"]:
        raise ValueError("Execution job/action mismatch")
    identity = operation.request_identity(repo, event, sha, actor, run, attempt, command=command)
    owner = authority.read_policy(repo, sha)["owner"].casefold()
    completed = verified_reference(repo, identity, review_run, job_id, inputs,
                                   event["comment"]["created_at"], owner)
    if inputs["action"] == "merge":
        wait_merge_refresh(repo, identity, number(inputs["pr"]), inputs["snapshot"],
                           inputs["head"], event["comment"]["created_at"])
    if operation.request_identity(repo, event, sha, actor, run, attempt, command=command) != identity:
        raise ValueError("Delegated request changed")
    # Repeat evidence and replay checks after waiting, before the shared write path.
    if verified_reference(repo, identity, review_run, job_id, inputs,
                          event["comment"]["created_at"], owner) != completed:
        raise ValueError("Verified reference changed")
    guard_prior_dispatches(repo, identity, completed)
    result = execute(repo, dict(identity, actor=actor, delegation="v1",
                                review_run=review_run, review_job=job_id), inputs)
    return result


def execute_request(repo):
    if os.environ.get("GITHUB_EVENT_NAME") == "issue_comment":
        return delegated_request(repo)
    event, sha, actor, run, attempt = operation.event_context(repo, "workflow_dispatch")
    identity = manual_identity(repo, event, sha, actor, run, attempt)
    return execute(repo, identity, event["inputs"])
