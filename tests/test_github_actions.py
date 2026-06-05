"""Tests for the GitHub Actions schema."""

import sys
import os
import textwrap
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from schemas.github_actions import GitHubActionsSchema


class TestScan(unittest.TestCase):
    def setUp(self):
        self.schema = GitHubActionsSchema(token=None)

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
        refs = self.schema.scan(content)
        self.assertEqual(refs, [])

    def test_action_with_subdirectory(self):
        content = "      - uses: org/repo/.github/actions/foo@v1\n"
        refs = self.schema.scan(content)
        self.assertEqual(refs[0]["action"], "org/repo/.github/actions/foo")
        self.assertEqual(refs[0]["ref"], "v1")


class TestApply(unittest.TestCase):
    def setUp(self):
        self.schema = GitHubActionsSchema(token=None)
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
        # An already-SHA ref should not be commented out again
        sha = "b" * 40
        content = f"      - uses: actions/checkout@{sha}\n"
        locks = {f"actions/checkout@{sha}": "c" * 40}
        result = self.schema.apply(content, locks)
        # No replacement because the ref IS a SHA
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


class TestResolve(unittest.TestCase):
    """Tests for resolve() — uses mocked HTTP calls."""

    def setUp(self):
        self.schema = GitHubActionsSchema(token="fake-token")

    def _mock_tag_response(self, sha: str, tag_type: str = "commit"):
        """Return a mock for the /git/ref/tags response."""
        resp = MagicMock()
        resp.read.return_value = json_bytes({
            "object": {"type": tag_type, "sha": sha}
        })
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _mock_annotated_tag_response(self, tag_sha: str, commit_sha: str):
        """Two-step: first the ref (annotated), then the tag object."""
        ref_resp = MagicMock()
        ref_resp.read.return_value = json_bytes({
            "object": {"type": "tag", "sha": tag_sha}
        })
        ref_resp.__enter__ = lambda s: s
        ref_resp.__exit__ = MagicMock(return_value=False)

        tag_resp = MagicMock()
        tag_resp.read.return_value = json_bytes({
            "object": {"type": "commit", "sha": commit_sha}
        })
        tag_resp.__enter__ = lambda s: s
        tag_resp.__exit__ = MagicMock(return_value=False)

        return [ref_resp, tag_resp]

    def test_resolve_tag(self):
        sha = "a" * 40
        with patch("urllib.request.urlopen", return_value=self._mock_tag_response(sha)):
            result = self.schema.resolve("actions/checkout", "v4")
        self.assertEqual(result, sha)

    def test_resolve_annotated_tag(self):
        tag_sha = "b" * 40
        commit_sha = "c" * 40
        responses = self._mock_annotated_tag_response(tag_sha, commit_sha)
        with patch("urllib.request.urlopen", side_effect=responses):
            result = self.schema.resolve("actions/checkout", "v4")
        self.assertEqual(result, commit_sha)

    def test_resolve_sha_passthrough(self):
        sha = "d" * 40
        with patch("urllib.request.urlopen") as mock_open:
            result = self.schema.resolve("actions/checkout", sha)
        mock_open.assert_not_called()
        self.assertEqual(result, sha)

    def test_resolve_subdirectory_action(self):
        sha = "e" * 40
        # org/repo/.github/actions/foo should resolve against org/repo
        with patch("urllib.request.urlopen", return_value=self._mock_tag_response(sha)) as mock_open:
            result = self.schema.resolve("org/repo/.github/actions/foo", "v1")
        call_url = mock_open.call_args[0][0].full_url
        self.assertIn("/repos/org/repo/", call_url)
        self.assertEqual(result, sha)


def json_bytes(obj) -> bytes:
    import json
    return json.dumps(obj).encode()


if __name__ == "__main__":
    unittest.main()
