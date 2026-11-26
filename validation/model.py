"""Shared contract parsing and GitHub data helpers."""
import hashlib
import json
import re
import subprocess
import unittest
from types import FunctionType

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



class TestClass(unittest.TestCase):
    """Register ordinary unittest methods on an isolated module-local class."""

    @staticmethod
    def for_module(module):
        return type("TestClass", (TestClass,), {"__module__": module})

    @classmethod
    def add(cls, name):
        if cls is TestClass:
            raise ValueError("Create a module-local TestClass before registration")
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError("Expected a test case identifier")
        method = "test_" + name

        def register(function):
            if hasattr(cls, method):
                raise ValueError(f"Duplicate test case: {name}")
            if not isinstance(function, FunctionType):
                raise TypeError("Expected a test function")
            setattr(cls, method, function)
            return function

        return register
