"""Small unittest registration helper for isolated, ordered JSON cases."""
import unittest


class TestClass(unittest.TestCase):
    @staticmethod
    def for_module(module):
        return type("TestClass", (TestClass,), {"__module__": module})

    @classmethod
    def add(cls, name):
        if cls is TestClass:
            raise ValueError("Use a suite-local TestClass")
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError("Expected a case identifier")

        def register(function):
            method = "test_" + name
            if hasattr(cls, method):
                raise ValueError(f"Duplicate case: {name}")
            if not callable(function):
                raise TypeError("Expected a test function")
            setattr(cls, method, function)
            return function

        return register
