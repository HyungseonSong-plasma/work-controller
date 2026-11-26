"""Structural Work Unit contract; no external dependencies."""
import re
from .issue import validate as validate_issue, work_type
from .model import PLACEHOLDERS, normalize, sections, without_comments

FIELDS = ("Work", "Changes", "Verification")
REFERENCE = re.compile(r"Refs #[1-9][0-9]*")


def errors(body):
    fields = sections(without_comments(normalize(body)))
    if tuple(name for name, _ in fields) != FIELDS:
        return ["Expected exactly these sections in order: " + ", ".join(FIELDS)]
    problems = []
    for field, value in fields:
        value = value.strip()
        if field == "Work":
            if not REFERENCE.fullmatch(value):
                problems.append("Work must be one reference: Refs #<issue number>")
        elif not value or value.casefold() in PLACEHOLDERS:
            problems.append(field + " requires content")
    return problems


def work_number(body):
    problems = errors(body)
    if problems:
        raise ValueError("; ".join(problems))
    return int(sections(without_comments(normalize(body)))[0][1].strip().split("#")[1])


def work_errors(number, issue):
    if not isinstance(issue, dict) or issue.get("number") != number:
        return ["Referenced Work does not exist or number does not match"]
    if "pull_request" in issue:
        return ["Work reference must point to an Issue, not a PR"]
    if issue.get("state") != "open":
        return ["Referenced Work must be OPEN"]
    if not validate_issue(issue.get("body")):
        return ["Referenced Work violates the Issue contract"]
    if work_type(issue.get("body")) == "Read-only":
        return ["Read-only Work must not have a PR"]
    return []
