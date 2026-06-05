#!/usr/bin/env python3
"""
modlock — modular version locking for plain-text config files.

Usage:
  modlock lock   [--schema SCHEMA] [--token TOKEN] FILE [FILE ...]
  modlock apply  [--schema SCHEMA] [--lockfile PATH] FILE [FILE ...]
  modlock update [--schema SCHEMA] [--token TOKEN] [--lockfile PATH] FILE [FILE ...]

Commands:
  lock    Resolve all refs in FILE(s) to commit SHAs and write/update the lock file.
  apply   Rewrite FILE(s) in-place, replacing refs with the locked SHAs.
  update  Re-resolve all refs (ignoring existing locks) and re-apply.

Options:
  --schema   Schema name to use (default: github-actions).
  --token    API token for the schema's resolver (falls back to env var).
  --lockfile Path to the lock file (default: modlock.lock).
"""

import argparse
import glob as _glob
import json
import os
import re
import sys
import tomllib
from pathlib import Path

LOCKFILE_DEFAULT = "modlock.lock"
SCHEMAS_DIR = Path(__file__).parent / "schemas"


# ---------------------------------------------------------------------------
# Schema — drives scan / apply from a TOML config dict
# ---------------------------------------------------------------------------

class Schema:
    def __init__(self, config: dict, token: str | None = None):
        self.name = config["name"]
        self.file_patterns = config.get("file_patterns", [])

        scanner = config["scanner"]
        self._pattern = re.compile(scanner["pattern"])
        self._sha_pattern = re.compile(scanner["sha_pattern"])

        resolver_cfg = config["resolver"]
        self.action_field = resolver_cfg.get("action_field", "action")
        self.ref_field = resolver_cfg.get("ref_field", "ref")

        self._template = config["output"]["template"]

        from resolvers import RESOLVERS
        resolver_type = resolver_cfg["type"]
        if resolver_type not in RESOLVERS:
            raise ValueError(f"Unknown resolver type '{resolver_type}'. Available: {', '.join(RESOLVERS)}")
        self._resolver = RESOLVERS[resolver_type](config=resolver_cfg, token=token)

    def lock_key(self, groups: dict) -> str:
        return f"{groups[self.action_field]}@{groups[self.ref_field]}"

    def scan(self, content: str) -> list[dict]:
        """Return one dict per matched reference, including all named groups and line number."""
        refs = []
        for i, line in enumerate(content.splitlines(), 1):
            m = self._pattern.match(line)
            if m:
                refs.append({"line": i, **m.groupdict()})
        return refs

    def resolve(self, action: str, ref: str) -> str:
        """Return the commit SHA for action@ref."""
        return self._resolver.resolve(action, ref)

    def apply(self, content: str, locks: dict[str, str]) -> str:
        """
        Return content with every matched ref replaced by its locked SHA.
        The original ref is preserved as a trailing comment via the output template.
        Lines whose ref is already a SHA, or whose key is not in locks, are unchanged.
        """
        out_lines = []
        for line in content.splitlines():
            m = self._pattern.match(line)
            if m:
                groups = m.groupdict()
                ref = groups[self.ref_field]
                key = self.lock_key(groups)
                if key in locks and not self._sha_pattern.match(ref):
                    sha = locks[key]
                    new_line = self._template.format(**groups, sha=sha)
                    # Preserve any non-comment suffix (unusual but possible)
                    suffix = groups.get("suffix", "").strip()
                    if suffix and not suffix.startswith("#"):
                        new_line += f" {suffix}"
                    out_lines.append(new_line)
                    continue
            out_lines.append(line)
        return "\n".join(out_lines) + "\n"


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

def load_schema(name: str, token: str | None = None) -> Schema:
    toml_path = SCHEMAS_DIR / f"{name}.toml"
    if not toml_path.exists():
        available = [p.stem for p in SCHEMAS_DIR.glob("*.toml")]
        raise FileNotFoundError(
            f"No schema '{name}' found. Available: {', '.join(sorted(available)) or 'none'}"
        )
    with open(toml_path, "rb") as f:
        config = tomllib.load(f)
    return Schema(config, token=token)


# ---------------------------------------------------------------------------
# Lock file helpers
# ---------------------------------------------------------------------------

def load_lockfile(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"version": 1, "locks": {}}


def save_lockfile(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# File expansion
# ---------------------------------------------------------------------------

def expand_files(patterns: list[str]) -> list[str]:
    files = []
    for pattern in patterns:
        expanded = _glob.glob(pattern, recursive=True)
        if expanded:
            files.extend(sorted(expanded))
        elif os.path.exists(pattern):
            files.append(pattern)
        else:
            print(f"Warning: no files matched '{pattern}'", file=sys.stderr)
    return files


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_lock(schema: Schema, files: list[str], lockfile_path: str, force: bool = False) -> None:
    data = load_lockfile(lockfile_path)
    locks = data.get("locks", {})
    changed = False

    for filepath in files:
        content = Path(filepath).read_text()
        for ref_info in schema.scan(content):
            action = ref_info[schema.action_field]
            ref = ref_info[schema.ref_field]
            key = schema.lock_key(ref_info)
            if key in locks and not force:
                print(f"  already locked: {key} → {locks[key][:12]}…")
                continue
            print(f"  resolving: {key}", end="", flush=True)
            try:
                sha = schema.resolve(action, ref)
                locks[key] = sha
                changed = True
                print(f" → {sha[:12]}…")
            except Exception as e:
                print(f" FAILED: {e}", file=sys.stderr)

    if changed:
        data["locks"] = locks
        save_lockfile(lockfile_path, data)
        print(f"Lock file written: {lockfile_path}")
    else:
        print("Nothing to update.")


def cmd_apply(schema: Schema, files: list[str], lockfile_path: str) -> None:
    data = load_lockfile(lockfile_path)
    locks = data.get("locks", {})

    if not locks:
        print("Lock file is empty — run 'modlock lock' first.", file=sys.stderr)
        sys.exit(1)

    for filepath in files:
        original = Path(filepath).read_text()
        locked = schema.apply(original, locks)
        if locked != original:
            Path(filepath).write_text(locked)
            print(f"  applied locks: {filepath}")
        else:
            print(f"  no changes:    {filepath}")


def cmd_update(schema: Schema, files: list[str], lockfile_path: str) -> None:
    cmd_lock(schema, files, lockfile_path, force=True)
    cmd_apply(schema, files, lockfile_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="modlock",
        description="Modular version locking for plain-text config files.",
    )
    parser.add_argument("command", choices=["lock", "apply", "update"])
    parser.add_argument("files", nargs="+", metavar="FILE")
    parser.add_argument("--schema", default="github-actions")
    parser.add_argument("--token")
    parser.add_argument("--lockfile", default=LOCKFILE_DEFAULT)

    args = parser.parse_args()

    try:
        schema = load_schema(args.schema, token=args.token)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    files = expand_files(args.files)
    if not files:
        print("No files to process.", file=sys.stderr)
        sys.exit(1)

    print(f"Schema: {args.schema}")
    print(f"Files:  {', '.join(files)}")
    print(f"Lock:   {args.lockfile}")
    print()

    if args.command == "lock":
        cmd_lock(schema, files, args.lockfile)
    elif args.command == "apply":
        cmd_apply(schema, files, args.lockfile)
    elif args.command == "update":
        cmd_update(schema, files, args.lockfile)


if __name__ == "__main__":
    main()
