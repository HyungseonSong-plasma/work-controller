"""Shared contract parsing and GitHub data helpers."""
import hashlib
import json
import re
import subprocess

SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
HEADING = re.compile(r"^### ([^\n]+?)\s*$", re.MULTILINE)
ISSUE_HEADING = re.compile(r"^### (.+?)\s*$", re.MULTILINE)
PLACEHOLDERS = {"_no response_", "n/a", "tbd", "todo", "- [ ]"}


def normalize(body):
    return (body or "").replace("\r\n", "\n")


def without_comments(body):
    return re.sub(r"<!--.*?-->", "", body, flags=re.S)


def sections(body, heading=HEADING):
    matches = list(heading.finditer(body))
    return [(h.group(1), body[h.end():matches[k + 1].start() if k + 1 < len(matches) else len(body)])
            for k, h in enumerate(matches)]


def api(path, *args):
    result = subprocess.run(["gh", "api", "--hostname", "github.com", path, *args],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def pages(path):
    return [item for page in api(path, "--paginate", "--slurp") for item in page]
