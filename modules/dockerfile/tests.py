"""Tests for the dockerfile module."""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.testing import SchemaTestCase, load_module_resolver

Resolver = load_module_resolver(Path(__file__).parent)

_MODULE = "dockerfile"
_EXAMPLE_LINE = "FROM python:3.12-slim\n"
_EXAMPLE_KEY = "python@3.12-slim"
_DIGEST = "sha256:" + "a" * 64


class TestScan(SchemaTestCase, unittest.TestCase):
    module_name = _MODULE
    example_line = _EXAMPLE_LINE
    example_key = _EXAMPLE_KEY

    def test_image_and_tag_fields(self):
        refs = self.schema.scan(self.example_line)
        self.assertEqual(refs[0]["image"], "python")
        self.assertEqual(refs[0]["ref"], "3.12-slim")

    def test_multi_stage_build(self):
        content = "FROM node:20-alpine AS builder\nFROM nginx:1.27-alpine\n"
        refs = self.schema.scan(content)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["image"], "node")
        self.assertEqual(refs[0]["ref"], "20-alpine")
        self.assertEqual(refs[1]["image"], "nginx")

    def test_alias_captured_in_suffix(self):
        refs = self.schema.scan("FROM python:3.12-slim AS builder\n")
        self.assertIn("AS builder", refs[0]["suffix"])

    def test_from_scratch_not_matched(self):
        self.assertEqual(self.schema.scan("FROM scratch\n"), [])

    def test_already_locked_without_comment_not_matched(self):
        # A sha FROM line with no preceding lock comment is not managed by modlock.
        self.assertEqual(self.schema.scan(f"FROM python@{_DIGEST}\n"), [])

    def test_scan_locked_format(self):
        content = f"# python:3.12-slim\nFROM python@{_DIGEST}\n"
        refs = self.schema.scan(content)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["image"], "python")
        self.assertEqual(refs[0]["ref"], "3.12-slim")

    def test_namespaced_image(self):
        refs = self.schema.scan("FROM bitnami/postgresql:16\n")
        self.assertEqual(refs[0]["image"], "bitnami/postgresql")
        self.assertEqual(refs[0]["ref"], "16")


class TestApply(SchemaTestCase, unittest.TestCase):
    module_name = _MODULE
    example_line = _EXAMPLE_LINE
    example_key = _EXAMPLE_KEY

    def test_comment_line_inserted_above_from(self):
        result = self.schema.apply(self.example_line, {self.example_key: _DIGEST})
        lines = result.splitlines()
        self.assertEqual(lines[0], "# python:3.12-slim")
        self.assertIn(_DIGEST, lines[1])

    def test_alias_preserved_on_from_line(self):
        line = "FROM python:3.12-slim AS builder\n"
        result = self.schema.apply(line, {"python@3.12-slim": _DIGEST})
        self.assertIn("AS builder", result)
        self.assertIn("# python:3.12-slim", result)

    def test_locked_block_without_entry_unchanged(self):
        content = f"# python:3.12-slim\nFROM python@{_DIGEST}\n"
        result = self.schema.apply(content, {})
        self.assertIn(_DIGEST, result)

    def test_update_already_locked(self):
        new_digest = "sha256:" + "b" * 64
        content = f"# python:3.12-slim\nFROM python@{_DIGEST}\n"
        result = self.schema.apply(content, {"python@3.12-slim": new_digest})
        self.assertIn(new_digest, result)
        self.assertIn("# python:3.12-slim", result)
        self.assertNotIn("a" * 64, result)


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = Resolver()

    def _mock_token(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"token": "fake-token"}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _mock_manifest(self, digest: str):
        resp = MagicMock()
        resp.read.return_value = b"{}"
        resp.headers = {"Docker-Content-Digest": digest}
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_resolve_official_image(self):
        with patch("urllib.request.urlopen", side_effect=[self._mock_token(), self._mock_manifest(_DIGEST)]):
            self.assertEqual(self.resolver.resolve("python", "3.12-slim"), _DIGEST)

    def test_official_image_uses_library_namespace(self):
        with patch("urllib.request.urlopen", side_effect=[self._mock_token(), self._mock_manifest(_DIGEST)]) as mock_open:
            self.resolver.resolve("ubuntu", "22.04")
        self.assertIn("library/ubuntu", mock_open.call_args_list[0][0][0])

    def test_namespaced_image_no_library_prefix(self):
        with patch("urllib.request.urlopen", side_effect=[self._mock_token(), self._mock_manifest(_DIGEST)]) as mock_open:
            self.resolver.resolve("bitnami/postgresql", "16")
        self.assertIn("bitnami/postgresql", mock_open.call_args_list[0][0][0])
        self.assertNotIn("library/bitnami", mock_open.call_args_list[0][0][0])

    def test_resolve_digest_passthrough(self):
        with patch("urllib.request.urlopen") as mock_open:
            self.assertEqual(self.resolver.resolve("python", _DIGEST), _DIGEST)
        mock_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
