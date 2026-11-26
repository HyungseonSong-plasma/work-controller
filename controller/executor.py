"""Bounded Work Unit publisher. No shell tasks, force pushes, merge or completion."""
import hashlib
import json
import subprocess

from validation import approval, authority
from validation.completion import declared_works
from validation.model import api, pages, digest, REPO, SHA
from validation.pr import work_number, work_errors


def write_json(endpoint, payload):
    completed = subprocess.run(["gh", "api", "--hostname", "github.com", endpoint,
                                "-X", "POST", "--input", "-"],
                               input=json.dumps(payload), text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)


def validate_plan(plan, work):
    if not isinstance(plan, dict) or set(plan) != {"title", "body", "files"}:
        raise ValueError("Plan requires exactly title, body and files")
    title, body, files = plan["title"], plan["body"], plan["files"]
    if not isinstance(title, str) or not title.strip() or len(title) > 256 or "\n" in title:
        raise ValueError("Invalid PR title")
    if not isinstance(body, str) or work_number(body) != work:
        raise ValueError("PR body must use the canonical template and reference this Work")
    if not isinstance(files, list) or not files:
        raise ValueError("Expected at least one file change")
    paths = []
    for f in files:
        if not isinstance(f, dict) or set(f) != {"path", "content"}:
            raise ValueError("Every file requires path and content (null for deletion)")
        path, content = f["path"], f["content"]
        if (not authority.path_valid(path) or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or any(p.casefold() == ".git" for p in path.split("/"))):
            raise ValueError("Invalid repository path")
        if content is not None:
            if not isinstance(content, str) or "\x00" in content:
                raise ValueError("Only UTF-8 text files or null deletion are supported")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate paths")
    if any(a != b and b.startswith(a + "/") for a in paths for b in paths):
        raise ValueError("Overlapping change paths")
    return sorted(paths)


def git_blob_sha(content):
    data = content.encode("utf-8")
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def tree_entries(plan, base_tree):
    if base_tree.get("truncated") is not False or not isinstance(base_tree.get("tree"), list):
        raise ValueError("Complete trusted tree required")
    existing = {f["path"]: f for f in base_tree["tree"]}
    entries = []
    for f in sorted(plan["files"], key=lambda f: f["path"]):
        path, content = f["path"], f["content"]
        old = existing.get(path)
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent = existing.get("/".join(parts[:i]))
            if parent and parent.get("type") != "tree":
                raise ValueError("Non-directory ancestor: " + path)
        if old and (old.get("type") != "blob" or old.get("mode") not in {"100644", "100755"}):
            raise ValueError("Only ordinary file changes allowed: " + path)
        if content is None:
            if not old:
                raise ValueError("Cannot delete missing path: " + path)
            entries.append({"path": path, "type": "blob", "mode": old["mode"], "sha": None})
        else:
            if old and old.get("sha") == git_blob_sha(content):
                raise ValueError("No-op file change: " + path)
            entries.append({"path": path, "type": "blob", "mode": old["mode"] if old else "100644",
                            "content": content})
    return entries


def check_context(repo, work, plan):
    if not REPO.fullmatch(repo) or type(work) is not int or work < 1:
        raise ValueError("Expected owner/repo and positive Work number")
    paths = validate_plan(plan, work)
    main = api(f"repos/{repo}/branches/main")["commit"]
    base, tree = main["sha"], main["commit"]["tree"]["sha"]
    if not SHA.fullmatch(base) or not SHA.fullmatch(tree):
        raise ValueError("Invalid main revision")
    issue = api(f"repos/{repo}/issues/{work}")
    problems = work_errors(work, issue)
    if problems:
        raise ValueError("; ".join(problems))
    for p in pages(f"repos/{repo}/pulls?state=all&per_page=100"):
        if work in declared_works(p.get("body")) and (p["state"] == "open" or p.get("merged_at")):
            raise ValueError("v0.1 requires PLAN_WORK with no active or delivered Work Unit")
    policy = authority.read_policy(repo, base)
    classification = authority.classify(policy, paths)
    grade = classification["grade"]
    evidence = {"state": "NOT_REQUIRED", "allowed": True, "reason": "C-grade paths"}
    if grade == "A":
        evidence = {"state": "USER_ONLY", "allowed": False, "reason": "A-grade paths require owner-only change"}
    elif grade == "B":
        evidence = approval.read_state(repo, work, policy, approval.b_paths(classification))
    current = api(f"repos/{repo}/issues/{work}")
    if any(current.get(k) != issue.get(k) for k in ("title", "body", "state", "updated_at")) or approval.labels(current) != approval.labels(issue):
        raise ValueError("Work changed during planning")
    snapshot = {"repository": repo.casefold(), "work": work, "base": base, "base_tree": tree,
                "title": issue["title"], "body": issue["body"], "policy": policy,
                "plan": plan, "classification": classification, "approval": evidence}
    return {"work": work, "base": base, "base_tree": tree, "grade": grade,
            "paths": classification["paths"], "approval": evidence, "allowed": evidence["allowed"],
            "plan_digest": digest(snapshot)}


def prepare(repo, work, plan):
    context = check_context(repo, work, plan)
    # A Git tree is immutable: validate the payload once against the pinned base tree.
    context["entries"] = tree_entries(plan, api(f"repos/{repo}/git/trees/{context['base_tree']}?recursive=1")) if context["allowed"] else []
    return context


def public_summary(prepared):
    return {k: v for k, v in prepared.items() if k != "entries"}


def run(repo, work, plan, execute=False, reviewed_digest=None):
    """Dry-run by default. Writes are limited to Git objects, new ref and canonical PR."""
    phase, branch, commit = "preflight", None, None
    try:
        prepared = prepare(repo, work, plan)
        summary = public_summary(prepared)
        if not prepared["allowed"] or not execute:
            return dict(summary, state=prepared["approval"]["state"] if not prepared["allowed"] else "READY",
                        executed=False)
        if not reviewed_digest or reviewed_digest != prepared["plan_digest"]:
            raise ValueError("Reviewed plan digest missing or stale")
        branch = f"work/{work}-{reviewed_digest[:16]}"
        if pages(f"repos/{repo}/git/matching-refs/heads/{branch}"):
            raise ValueError("Execution branch already exists; human review before retry")

        def recheck():
            fresh = check_context(repo, work, plan)
            if not fresh["allowed"] or fresh["plan_digest"] != reviewed_digest:
                raise ValueError("Work, plan, base or permission changed; stop execution")

        phase = "create_tree"
        tree = write_json(f"repos/{repo}/git/trees", {"base_tree": prepared["base_tree"],
                                                       "tree": prepared["entries"]})["sha"]
        if not SHA.fullmatch(tree):
            raise ValueError("Invalid tree response")
        phase = "create_commit"
        commit = write_json(f"repos/{repo}/git/commits", {"message": f"Work #{work}: {plan['title']}",
                                                           "tree": tree, "parents": [prepared["base"]]})["sha"]
        if not SHA.fullmatch(commit):
            raise ValueError("Invalid commit response")
        phase = "pre_ref_recheck"
        recheck()
        phase = "publish_branch"
        ref = write_json(f"repos/{repo}/git/refs", {"ref": "refs/heads/"+branch, "sha": commit})
        if ref.get("ref") != "refs/heads/"+branch or ref.get("object", {}).get("sha") != commit:
            raise ValueError("Unexpected branch response")
        phase = "pre_pr_recheck"
        recheck()
        phase = "create_pr"
        pr = write_json(f"repos/{repo}/pulls", {"title": plan["title"], "body": plan["body"],
                                                  "head": branch, "base": "main"})
        if (type(pr.get("number")) is not int or pr.get("head", {}).get("sha") != commit
                or pr.get("base", {}).get("ref") != "main"):
            raise ValueError("Unexpected PR response; inspect repository before retry")
        return dict(summary, state="PUBLISHED", executed=True, branch=branch, commit=commit,
                    pr=pr["number"], url=pr.get("html_url"))
    except (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        return {"work": work, "state": "HUMAN_REVIEW", "allowed": False, "executed": False,
                "phase": phase, "branch": branch, "commit": commit, "reason": str(exc),
                "partial_write_possible": phase != "preflight",
                "retry": "Inspect repository and obtain fresh review before any retry"}
