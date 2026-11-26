"""Canonical structural Work contract. No legacy format fallback."""
import re
from .model import ISSUE_HEADING, PLACEHOLDERS, normalize, sections, without_comments

FIELDS = ("Work type", "Objective", "Scope", "Acceptance criteria")
TYPES = ("Implementation", "Read-only")
SCOPE_FIELDS = ("Constraints", "Actions")
SCOPE_HEADING = re.compile(r"^####[ \t]+([^\n]+?)[ \t]*$", re.MULTILINE)
BULLET_PREFIX = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)(?:\[[ xX]\]\s*)?")


def work_type(body):
    fields = sections(without_comments(normalize(body)), ISSUE_HEADING)
    if tuple(name for name, _ in fields) == FIELDS:
        value = fields[0][1].strip()
        if value in TYPES:
            return value
    return None


def scope_valid(value):
    value = value.strip()
    matches = list(SCOPE_HEADING.finditer(value))
    if tuple(h.group(1) for h in matches) != SCOPE_FIELDS or value[:matches[0].start()].strip():
        return False
    for _, content in sections(value, SCOPE_HEADING):
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        entries = [BULLET_PREFIX.sub("", line).strip() for line in lines]
        if not entries or any(not entry or entry.casefold() in PLACEHOLDERS
                              or entry in {"-", "*", "+", "[ ]", "[x]", "[X]"}
                              or entry.startswith("#") for entry in entries):
            return False
    return True


def validate(body):
    fields = sections(without_comments(normalize(body)), ISSUE_HEADING)
    if work_type(body) is None:
        return False
    for name, value in fields:
        value = value.strip()
        if not value or value.casefold() in PLACEHOLDERS:
            return False
        if name == "Scope" and not scope_valid(value):
            return False
    return True
