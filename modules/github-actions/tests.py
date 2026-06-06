"""Tests for the github-actions module."""

import json
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.testing import SchemaTestCase, load_module_resolver

Resolver = load_module_resolver(Path(__file__).parent)

_MODULE = "github-actions"
_EXAMPLE_LINE = "      - uses: actions/checkout@v4\n"
_EXAMPLE_KEY = "actions/checkout@v4"


class TestScan(SchemaTestCase, unittest.TestCase):
    module_name = _MODULE
    example_line = _EXAMPLE_LINE
    example_key = _EXAMPLE_KEY

    def test_simple_tag_fields(self):
        refs = self.schema.scan(self.example_line)
        self.assertEqual(refs[0]["action"], "actions/checkout")
        self.assertEqual(refs[0]["ref"], "v4")

    def test_multiple_actions(self):
        content = textwrap.dedent("""\
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-python@v5
              - uses: astral-sh/setup-uv@v5
        """)
        refs = self.schema.scan(content)
        self.assertEqual(len(refs), 3)
        self.assertEqual(refs[0]["action"], "actions/checkout")
        self.assertEqual(refs[1]["action"], "actions/setup-python")
        self.assertEqual(refs[2]["action"], "astral-sh/setup-uv")

    def test_already_pinned_sha(self):
        sha = "a" * 40
        refs = self.schema.scan(f"      - uses: actions/checkout@{sha}\n")
        self.assertEqual(refs[0]["ref"], sha)

    def test_scan_extracts_original_ref_from_locked_line(self):
        sha = "a" * 40
        refs = self.schema.scan(f"      - uses: actions/checkout@{sha} # v4\n")
        self.assertEqual(refs[0]["ref"], "v4")

    def test_branch_ref(self):
        refs = self.schema.scan("      - uses: actions/checkout@main\n")
        self.assertEqual(refs[0]["ref"], "main")

    def test_no_uses_lines(self):
        content = textwrap.dedent("""\
            name: CI
            on: push
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: echo hello
        """)
        self.assertEqual(self.schema.scan(content), [])

    def test_action_with_subdirectory(self):
        refs = self.schema.scan("      - uses: org/repo/.github/actions/foo@v1\n")
        self.assertEqual(refs[0]["action"], "org/repo/.github/actions/foo")
        self.assertEqual(refs[0]["ref"], "v1")


class TestApply(SchemaTestCase, unittest.TestCase):
    module_name = _MODULE
    example_line = _EXAMPLE_LINE
    example_key = _EXAMPLE_KEY

    def setUp(self):
        super().setUp()
        self.sha = "a" * 40

    def test_original_ref_preserved_as_comment(self):
        result = self.schema.apply(self.example_line, {self.example_key: self.sha})
        self.assertIn("# v4", result)

    def test_does_not_double_lock_sha(self):
        # SHA with no comment — treated as an intentional commit pin, never updated.
        sha = "b" * 40
        content = f"      - uses: actions/checkout@{sha}\n"
        result = self.schema.apply(content, {f"actions/checkout@{sha}": "c" * 40})
        self.assertIn(sha, result)
        self.assertNotIn("c" * 40, result)

    def test_update_locked_line(self):
        old_sha, new_sha = "a" * 40, "b" * 40
        content = f"      - uses: actions/checkout@{old_sha} # v4\n"
        result = self.schema.apply(content, {"actions/checkout@v4": new_sha})
        self.assertIn(new_sha, result)
        self.assertIn("# v4", result)
        self.assertNotIn(old_sha, result)

    def test_multiple_locks_applied(self):
        content = textwrap.dedent("""\
              - uses: actions/checkout@v4
              - uses: actions/setup-python@v5
        """)
        sha1, sha2 = "1" * 40, "2" * 40
        locks = {"actions/checkout@v4": sha1, "actions/setup-python@v5": sha2}
        result = self.schema.apply(content, locks)
        self.assertIn(sha1, result)
        self.assertIn(sha2, result)
        self.assertIn("# v4", result)
        self.assertIn("# v5", result)


class TestResolver(unittest.TestCase):
    """Tests for the GitHub resolver — uses mocked HTTP calls."""

    def setUp(self):
        self.resolver = Resolver(token="fake-token")

    def _mock_ref_response(self, sha: str, obj_type: str = "commit"):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"object": {"type": obj_type, "sha": sha}}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _mock_annotated_responses(self, tag_sha: str, commit_sha: str):
        ref_resp = MagicMock()
        ref_resp.read.return_value = json.dumps({"object": {"type": "tag", "sha": tag_sha}}).encode()
        ref_resp.__enter__ = lambda s: s
        ref_resp.__exit__ = MagicMock(return_value=False)

        tag_resp = MagicMock()
        tag_resp.read.return_value = json.dumps({"object": {"type": "commit", "sha": commit_sha}}).encode()
        tag_resp.__enter__ = lambda s: s
        tag_resp.__exit__ = MagicMock(return_value=False)

        return [ref_resp, tag_resp]

    def test_resolve_tag(self):
        sha = "a" * 40
        with patch("urllib.request.urlopen", return_value=self._mock_ref_response(sha)):
            self.assertEqual(self.resolver.resolve("actions/checkout", "v4"), sha)

    def test_resolve_annotated_tag(self):
        tag_sha, commit_sha = "b" * 40, "c" * 40
        with patch("urllib.request.urlopen", side_effect=self._mock_annotated_responses(tag_sha, commit_sha)):
            self.assertEqual(self.resolver.resolve("actions/checkout", "v4"), commit_sha)

    def test_resolve_sha_passthrough(self):
        sha = "d" * 40
        with patch("urllib.request.urlopen") as mock_open:
            self.assertEqual(self.resolver.resolve("actions/checkout", sha), sha)
        mock_open.assert_not_called()

    def test_resolve_subdirectory_action(self):
        sha = "e" * 40
        with patch("urllib.request.urlopen", return_value=self._mock_ref_response(sha)) as mock_open:
            self.resolver.resolve("org/repo/.github/actions/foo", "v1")
        self.assertIn("/repos/org/repo/", mock_open.call_args[0][0].full_url)


if __name__ == "__main__":
    unittest.main()
