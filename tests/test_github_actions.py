"""Tests for the github-actions schema and GitHub resolver."""

import json
import os
import sys
import textwrap
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modlock import Schema, load_schema
from resolvers.github import GitHubResolver


def _make_schema() -> Schema:
    return load_schema("github-actions", token=None)


class TestScan(unittest.TestCase):
    def setUp(self):
        self.schema = _make_schema()

    def test_simple_tag(self):
        content = textwrap.dedent("""\
            steps:
              - uses: actions/checkout@v4
        """)
        refs = self.schema.scan(content)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["action"], "actions/checkout")
        self.assertEqual(refs[0]["ref"], "v4")
        self.assertEqual(refs[0]["line"], 2)

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
        content = f"      - uses: actions/checkout@{sha}\n"
        refs = self.schema.scan(content)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["ref"], sha)

    def test_branch_ref(self):
        content = "      - uses: actions/checkout@main\n"
        refs = self.schema.scan(content)
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
        content = "      - uses: org/repo/.github/actions/foo@v1\n"
        refs = self.schema.scan(content)
        self.assertEqual(refs[0]["action"], "org/repo/.github/actions/foo")
        self.assertEqual(refs[0]["ref"], "v1")


class TestApply(unittest.TestCase):
    def setUp(self):
        self.schema = _make_schema()
        self.sha = "a" * 40

    def test_replaces_tag_with_sha(self):
        content = "      - uses: actions/checkout@v4\n"
        locks = {"actions/checkout@v4": self.sha}
        result = self.schema.apply(content, locks)
        self.assertIn(f"actions/checkout@{self.sha}", result)
        self.assertIn("# v4", result)

    def test_preserves_unlocked_lines(self):
        content = textwrap.dedent("""\
              - uses: actions/checkout@v4
              - run: echo hello
        """)
        locks = {"actions/checkout@v4": self.sha}
        result = self.schema.apply(content, locks)
        self.assertIn("run: echo hello", result)

    def test_does_not_double_lock_sha(self):
        sha = "b" * 40
        content = f"      - uses: actions/checkout@{sha}\n"
        locks = {f"actions/checkout@{sha}": "c" * 40}
        result = self.schema.apply(content, locks)
        self.assertIn(sha, result)
        self.assertNotIn("c" * 40, result)

    def test_unknown_key_unchanged(self):
        content = "      - uses: actions/checkout@v4\n"
        result = self.schema.apply(content, {})
        self.assertIn("actions/checkout@v4", result)
        self.assertNotIn("#", result)

    def test_trailing_newline_preserved(self):
        content = "      - uses: actions/checkout@v4\n"
        locks = {"actions/checkout@v4": self.sha}
        result = self.schema.apply(content, locks)
        self.assertTrue(result.endswith("\n"))

    def test_multiple_locks_applied(self):
        content = textwrap.dedent("""\
              - uses: actions/checkout@v4
              - uses: actions/setup-python@v5
        """)
        sha1 = "1" * 40
        sha2 = "2" * 40
        locks = {
            "actions/checkout@v4": sha1,
            "actions/setup-python@v5": sha2,
        }
        result = self.schema.apply(content, locks)
        self.assertIn(sha1, result)
        self.assertIn(sha2, result)
        self.assertIn("# v4", result)
        self.assertIn("# v5", result)


class TestGitHubResolver(unittest.TestCase):
    """Tests for GitHubResolver — uses mocked HTTP calls."""

    def setUp(self):
        self.resolver = GitHubResolver(token="fake-token")

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
            result = self.resolver.resolve("actions/checkout", "v4")
        self.assertEqual(result, sha)

    def test_resolve_annotated_tag(self):
        tag_sha = "b" * 40
        commit_sha = "c" * 40
        with patch("urllib.request.urlopen", side_effect=self._mock_annotated_responses(tag_sha, commit_sha)):
            result = self.resolver.resolve("actions/checkout", "v4")
        self.assertEqual(result, commit_sha)

    def test_resolve_sha_passthrough(self):
        sha = "d" * 40
        with patch("urllib.request.urlopen") as mock_open:
            result = self.resolver.resolve("actions/checkout", sha)
        mock_open.assert_not_called()
        self.assertEqual(result, sha)

    def test_resolve_subdirectory_action(self):
        sha = "e" * 40
        with patch("urllib.request.urlopen", return_value=self._mock_ref_response(sha)) as mock_open:
            result = self.resolver.resolve("org/repo/.github/actions/foo", "v1")
        call_url = mock_open.call_args[0][0].full_url
        self.assertIn("/repos/org/repo/", call_url)
        self.assertEqual(result, sha)


if __name__ == "__main__":
    unittest.main()
