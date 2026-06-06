"""Tests for the docker-compose module."""

import json
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.testing import SchemaTestCase, load_module_resolver

Resolver = load_module_resolver(Path(__file__).parent)

_MODULE = "docker-compose"
_EXAMPLE_LINE = "    image: postgres:16\n"
_EXAMPLE_KEY = "postgres@16"
_DIGEST = "sha256:" + "a" * 64


class TestScan(SchemaTestCase, unittest.TestCase):
    module_name = _MODULE
    example_line = _EXAMPLE_LINE
    example_key = _EXAMPLE_KEY

    def test_image_and_tag_fields(self):
        refs = self.schema.scan(self.example_line)
        self.assertEqual(refs[0]["image"], "postgres")
        self.assertEqual(refs[0]["ref"], "16")

    def test_multiple_services(self):
        content = textwrap.dedent("""\
            services:
              db:
                image: postgres:16
              cache:
                image: redis:7-alpine
        """)
        refs = self.schema.scan(content)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["image"], "postgres")
        self.assertEqual(refs[1]["image"], "redis")
        self.assertEqual(refs[1]["ref"], "7-alpine")

    def test_build_only_service_not_matched(self):
        content = textwrap.dedent("""\
            services:
              app:
                build: .
        """)
        self.assertEqual(self.schema.scan(content), [])

    def test_image_without_tag_not_matched(self):
        self.assertEqual(self.schema.scan("    image: postgres\n"), [])

    def test_scan_locked_format(self):
        refs = self.schema.scan(f"    image: postgres@{_DIGEST} # 16\n")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["image"], "postgres")
        self.assertEqual(refs[0]["ref"], "16")

    def test_namespaced_image(self):
        refs = self.schema.scan("    image: bitnami/postgresql:16\n")
        self.assertEqual(refs[0]["image"], "bitnami/postgresql")
        self.assertEqual(refs[0]["ref"], "16")


class TestApply(SchemaTestCase, unittest.TestCase):
    module_name = _MODULE
    example_line = _EXAMPLE_LINE
    example_key = _EXAMPLE_KEY

    def test_original_tag_preserved_as_comment(self):
        result = self.schema.apply(self.example_line, {self.example_key: _DIGEST})
        self.assertIn("# 16", result)

    def test_multiple_services_locked(self):
        content = textwrap.dedent("""\
            services:
              db:
                image: postgres:16
              cache:
                image: redis:7-alpine
        """)
        digest1 = "sha256:" + "1" * 64
        digest2 = "sha256:" + "2" * 64
        locks = {"postgres@16": digest1, "redis@7-alpine": digest2}
        result = self.schema.apply(content, locks)
        self.assertIn(digest1, result)
        self.assertIn(digest2, result)
        self.assertIn("# 16", result)
        self.assertIn("# 7-alpine", result)

    def test_locked_line_without_entry_unchanged(self):
        # Already-applied line whose original tag is absent from locks — preserve as-is.
        line = f"    image: postgres@{_DIGEST} # 16\n"
        result = self.schema.apply(line, {})
        self.assertEqual(result.rstrip("\n"), line.rstrip("\n"))

    def test_update_locked_line(self):
        new_digest = "sha256:" + "b" * 64
        line = f"    image: postgres@{_DIGEST} # 16\n"
        result = self.schema.apply(line, {"postgres@16": new_digest})
        self.assertIn(new_digest, result)
        self.assertIn("# 16", result)
        self.assertNotIn("a" * 64, result)

    def test_build_only_service_unchanged(self):
        content = textwrap.dedent("""\
            services:
              app:
                build: .
        """)
        result = self.schema.apply(content, {"anything@v1": _DIGEST})
        self.assertIn("build: .", result)


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
            self.assertEqual(self.resolver.resolve("postgres", "16"), _DIGEST)

    def test_official_image_uses_library_namespace(self):
        with patch("urllib.request.urlopen", side_effect=[self._mock_token(), self._mock_manifest(_DIGEST)]) as mock_open:
            self.resolver.resolve("redis", "7-alpine")
        self.assertIn("library/redis", mock_open.call_args_list[0][0][0])

    def test_namespaced_image_no_library_prefix(self):
        with patch("urllib.request.urlopen", side_effect=[self._mock_token(), self._mock_manifest(_DIGEST)]) as mock_open:
            self.resolver.resolve("bitnami/postgresql", "16")
        self.assertIn("bitnami/postgresql", mock_open.call_args_list[0][0][0])
        self.assertNotIn("library/bitnami", mock_open.call_args_list[0][0][0])

    def test_resolve_digest_passthrough(self):
        with patch("urllib.request.urlopen") as mock_open:
            self.assertEqual(self.resolver.resolve("postgres", _DIGEST), _DIGEST)
        mock_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
