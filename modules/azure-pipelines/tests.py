"""Tests for the azure-pipelines module."""

import json
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.testing import SchemaTestCase, load_module_resolver

Resolver = load_module_resolver(Path(__file__).parent)

_MODULE = "azure-pipelines"
_EXAMPLE_LINE = textwrap.dedent("""\
        - repository: templates
          type: github
          name: myorg/pipeline-templates
          ref: refs/tags/v1.2.0
""")
_EXAMPLE_KEY = "myorg/pipeline-templates@refs/tags/v1.2.0"


class TestScan(SchemaTestCase, unittest.TestCase):
    module_name = _MODULE
    example_line = _EXAMPLE_LINE
    example_key = _EXAMPLE_KEY

    def test_multiple_repos(self):
        content = textwrap.dedent("""\
            resources:
              repositories:
                - repository: templates
                  type: github
                  name: myorg/pipeline-templates
                  ref: refs/tags/v1.2.0
                - repository: tools
                  type: github
                  name: myorg/build-tools
                  ref: refs/heads/stable
        """)
        refs = self.schema.scan(content)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["action"], "myorg/pipeline-templates")
        self.assertEqual(refs[0]["ref"], "refs/tags/v1.2.0")
        self.assertEqual(refs[1]["action"], "myorg/build-tools")
        self.assertEqual(refs[1]["ref"], "refs/heads/stable")

    def test_bare_ref_without_name_is_ignored(self):
        content = "          ref: refs/tags/v1.0\n"
        self.assertEqual(self.schema.scan(content), [])

    def test_already_pinned_sha(self):
        sha = "a" * 40
        content = textwrap.dedent(f"""\
                - repository: templates
                  type: github
                  name: myorg/pipeline-templates
                  ref: {sha}
        """)
        refs = self.schema.scan(content)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["ref"], sha)

    def test_scan_extracts_original_ref_from_locked_line(self):
        sha = "a" * 40
        content = textwrap.dedent(f"""\
                - repository: templates
                  type: github
                  name: myorg/pipeline-templates
                  ref: {sha} # refs/tags/v1.2.0
        """)
        refs = self.schema.scan(content)
        self.assertEqual(refs[0]["ref"], "refs/tags/v1.2.0")

    def test_task_steps_not_scanned(self):
        content = textwrap.dedent("""\
            steps:
              - task: PublishTestResults@2
                inputs:
                  testResultsFormat: JUnit
        """)
        self.assertEqual(self.schema.scan(content), [])


class TestApply(SchemaTestCase, unittest.TestCase):
    module_name = _MODULE
    example_line = _EXAMPLE_LINE
    example_key = _EXAMPLE_KEY

    def setUp(self):
        super().setUp()
        self.sha = "a" * 40

    def test_original_ref_preserved_as_comment(self):
        result = self.schema.apply(self.example_line, {self.example_key: self.sha})
        self.assertIn(f"# refs/tags/v1.2.0", result)

    def test_does_not_double_lock_sha(self):
        # SHA with no comment — intentional commit pin, never updated.
        sha = "b" * 40
        content = textwrap.dedent(f"""\
                - repository: templates
                  type: github
                  name: myorg/pipeline-templates
                  ref: {sha}
        """)
        result = self.schema.apply(content, {f"myorg/pipeline-templates@{sha}": "c" * 40})
        self.assertIn(sha, result)
        self.assertNotIn("c" * 40, result)

    def test_update_locked_ref(self):
        old_sha, new_sha = "a" * 40, "b" * 40
        content = textwrap.dedent(f"""\
                - repository: templates
                  type: github
                  name: myorg/pipeline-templates
                  ref: {old_sha} # refs/tags/v1.2.0
        """)
        result = self.schema.apply(content, {"myorg/pipeline-templates@refs/tags/v1.2.0": new_sha})
        self.assertIn(new_sha, result)
        self.assertIn("# refs/tags/v1.2.0", result)
        self.assertNotIn(old_sha, result)

    def test_multiple_repos_locked(self):
        content = textwrap.dedent("""\
            resources:
              repositories:
                - repository: templates
                  type: github
                  name: myorg/pipeline-templates
                  ref: refs/tags/v1.2.0
                - repository: tools
                  type: github
                  name: myorg/build-tools
                  ref: refs/heads/stable
        """)
        sha1, sha2 = "1" * 40, "2" * 40
        locks = {
            "myorg/pipeline-templates@refs/tags/v1.2.0": sha1,
            "myorg/build-tools@refs/heads/stable": sha2,
        }
        result = self.schema.apply(content, locks)
        self.assertIn(sha1, result)
        self.assertIn(sha2, result)
        self.assertIn("# refs/tags/v1.2.0", result)
        self.assertIn("# refs/heads/stable", result)

    def test_task_steps_unchanged(self):
        content = textwrap.dedent("""\
            steps:
              - task: PublishTestResults@2
                inputs:
                  testResultsFormat: JUnit
        """)
        result = self.schema.apply(content, {"anything@v1": "a" * 40})
        self.assertIn("PublishTestResults@2", result)


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = Resolver(token="fake-token")

    def _mock_ref_response(self, sha: str, obj_type: str = "commit"):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"object": {"type": obj_type, "sha": sha}}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_resolve_refs_tags(self):
        sha = "a" * 40
        with patch("urllib.request.urlopen", return_value=self._mock_ref_response(sha)) as mock_open:
            result = self.resolver.resolve("myorg/pipeline-templates", "refs/tags/v1.2.0")
        self.assertEqual(result, sha)
        self.assertIn("/git/ref/tags/v1.2.0", mock_open.call_args[0][0].full_url)

    def test_resolve_refs_heads(self):
        sha = "b" * 40
        with patch("urllib.request.urlopen", return_value=self._mock_ref_response(sha)) as mock_open:
            result = self.resolver.resolve("myorg/build-tools", "refs/heads/stable")
        self.assertEqual(result, sha)
        self.assertIn("/git/ref/heads/stable", mock_open.call_args[0][0].full_url)

    def test_resolve_sha_passthrough(self):
        sha = "c" * 40
        with patch("urllib.request.urlopen") as mock_open:
            result = self.resolver.resolve("myorg/repo", sha)
        mock_open.assert_not_called()
        self.assertEqual(result, sha)


if __name__ == "__main__":
    unittest.main()
