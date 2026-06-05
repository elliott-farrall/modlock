# Contributing

## Adding a module

Each module lives in its own directory under `modules/` and consists of
three required files:

```
modules/
  your-module/
    schema.toml   — file patterns, regex scanner, output template
    resolver.py   — Resolver class (and optionally scan/apply overrides)
    tests.py      — module tests
```

An `examples/` subdirectory with representative config files is
encouraged but not required.

### 1. schema.toml

Defines the file patterns to scan, how to match version references, and
how to format the locked output.

```toml
name = "your-module"
file_patterns = [
  "**/path/to/*.yml",
]

[scanner]
# Named groups `prefix`, `action`, `ref`, and `suffix` are expected.
# `action` is the thing being versioned (e.g. a repo name).
# `ref`    is the version reference to lock (e.g. a tag or branch).
pattern = '^(?P<prefix>...)(?P<action>...)@(?P<ref>...)(?P<suffix>.*)$'
sha_pattern = '^[0-9a-f]{40}$'

[output]
# {prefix}, {action}, {ref}, {suffix} come from the scanner groups.
# {sha} is the resolved commit hash.
template = "{prefix}{action}@{sha} # {ref}"

[resolver]
action_field = "action"
ref_field    = "ref"
```

For formats where a reference spans multiple lines (e.g. Azure Pipelines
`name:`/`ref:` pairs), see [Multi-line formats](#multi-line-formats) below.

### 2. resolver.py

Must export a `Resolver` class with a `resolve(action, ref) -> str` method
that returns the commit SHA for the given reference.

```python
class Resolver:
    def __init__(self, config: dict | None = None, token: str | None = None):
        self.token = token

    def resolve(self, action: str, ref: str) -> str:
        # Return a 40-character commit SHA.
        ...
```

`token` is passed from `--token` on the CLI or the relevant environment
variable (e.g. `GITHUB_TOKEN`). `config` contains the `[resolver]` table
from `schema.toml` in case you need module-specific settings.

### 3. tests.py

Inherit from `SchemaTestCase` (from `core.testing`) for free contract
tests that every module must satisfy, then add module-specific tests.

```python
import unittest
from pathlib import Path
from core.testing import SchemaTestCase, load_module_resolver

Resolver = load_module_resolver(Path(__file__).parent)

_MODULE = "your-module"
_EXAMPLE_LINE = "...a single line or block containing one version ref...\n"
_EXAMPLE_KEY  = "owner/repo@v1.0"


class TestScan(SchemaTestCase, unittest.TestCase):
    module_name  = _MODULE
    example_line = _EXAMPLE_LINE
    example_key  = _EXAMPLE_KEY

    # Add module-specific scan tests here.


class TestApply(SchemaTestCase, unittest.TestCase):
    module_name  = _MODULE
    example_line = _EXAMPLE_LINE
    example_key  = _EXAMPLE_KEY

    # Add module-specific apply tests here.


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = Resolver(token="fake-token")

    # Add resolver tests here (mock HTTP calls where needed).
```

`SchemaTestCase` runs five contract tests automatically:

| Test | What it checks |
|------|---------------|
| `test_scan_returns_line_number` | `scan()` returns at least one result with a `line` field |
| `test_apply_inserts_sha` | `apply()` writes the SHA into the content |
| `test_apply_trailing_newline` | `apply()` always ends with a newline |
| `test_apply_unknown_key_unchanged` | `apply()` leaves content alone when the key isn't in locks |
| `test_apply_preserves_unmatched_lines` | `apply()` doesn't touch lines it doesn't recognise |

`example_line` can be a multi-line string if your format requires it
(e.g. a YAML block containing both `name:` and `ref:`).

## Multi-line formats

When a version reference spans multiple lines you can override the
default regex-based scanner and applier by defining module-level
`scan()` and/or `apply()` functions in `resolver.py`.

```python
def scan(content: str) -> list[dict]:
    """
    Return one dict per reference found.
    Each dict must include at least:
      line:   int  — line number (1-indexed) of the ref
      action: str  — the thing being versioned
      ref:    str  — the version reference
    Additional keys (prefix, suffix, ...) are passed straight to the
    output template during apply().
    """
    ...


def apply(content: str, locks: dict[str, str]) -> str:
    """
    Return a copy of content with locked refs replaced by their SHAs.
    locks maps "action@ref" -> sha.
    Must end with a newline.
    """
    ...


class Resolver:
    ...
```

When either function is present, the core uses it instead of the
regex/template from `schema.toml`. The `azure-pipelines` module is a
worked example of this pattern.

## Running tests

```sh
pip install -e .
pytest
```

Tests are discovered automatically from `modules/*/tests.py`.
