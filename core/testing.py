"""Shared test utilities for modlock module tests."""

import importlib.util
import unittest
from pathlib import Path

from core import Schema, load_schema


def load_module_resolver(module_dir: Path):
    """Load the Resolver class from a module directory."""
    spec = importlib.util.spec_from_file_location("resolver", module_dir / "resolver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Resolver


class SchemaTestCase:
    """
    Base class for module schema tests.

    Subclasses must define:
        module_name: str   — name of the module directory (e.g. "github-actions")
        example_line: str  — a single file line containing one version ref
        example_key: str   — the lock key for that ref (e.g. "actions/checkout@v4")

    setUp loads self.schema automatically; subclasses can call super().setUp()
    to extend it.
    """

    module_name: str
    example_line: str
    example_key: str

    def setUp(self):
        self.schema = load_schema(self.module_name, token=None)

    # -- Contract tests every schema must satisfy --

    def test_scan_returns_line_number(self):
        refs = self.schema.scan(self.example_line)
        self.assertEqual(len(refs), 1)
        self.assertIn("line", refs[0])

    def test_apply_inserts_sha(self):
        sha = "a" * 40
        result = self.schema.apply(self.example_line, {self.example_key: sha})
        self.assertIn(sha, result)

    def test_apply_trailing_newline(self):
        sha = "a" * 40
        result = self.schema.apply(self.example_line, {self.example_key: sha})
        self.assertTrue(result.endswith("\n"))

    def test_apply_unknown_key_unchanged(self):
        result = self.schema.apply(self.example_line, {})
        self.assertEqual(result.rstrip("\n"), self.example_line.rstrip("\n"))

    def test_apply_preserves_unmatched_lines(self):
        sha = "a" * 40
        content = self.example_line + "unmatched line\n"
        result = self.schema.apply(content, {self.example_key: sha})
        self.assertIn("unmatched line", result)
