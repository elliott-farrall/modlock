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
  --token    API token for the schema's resolver (falls back to GITHUB_TOKEN env var).
  --lockfile Path to the lock file (default: modlock.lock).
"""

import argparse
import json
import sys
import glob as _glob
import os
from pathlib import Path


LOCKFILE_DEFAULT = "modlock.lock"


def load_lockfile(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"version": 1, "locks": {}}


def save_lockfile(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


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


def cmd_lock(schema, files: list[str], lockfile_path: str, force: bool = False) -> None:
    data = load_lockfile(lockfile_path)
    locks = data.get("locks", {})

    changed = False
    for filepath in files:
        content = Path(filepath).read_text()
        refs = schema.scan(content)
        for ref_info in refs:
            action = ref_info["action"]
            ref = ref_info["ref"]
            key = f"{action}@{ref}"
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


def cmd_apply(schema, files: list[str], lockfile_path: str) -> None:
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


def cmd_update(schema, files: list[str], lockfile_path: str) -> None:
    cmd_lock(schema, files, lockfile_path, force=True)
    cmd_apply(schema, files, lockfile_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="modlock",
        description="Modular version locking for plain-text config files.",
    )
    parser.add_argument(
        "command", choices=["lock", "apply", "update"],
        help="Action to perform.",
    )
    parser.add_argument(
        "files", nargs="+", metavar="FILE",
        help="Files or glob patterns to process.",
    )
    parser.add_argument(
        "--schema", default="github-actions",
        help="Schema to use (default: github-actions).",
    )
    parser.add_argument(
        "--token",
        help="API token for the resolver (e.g. GitHub PAT).",
    )
    parser.add_argument(
        "--lockfile", default=LOCKFILE_DEFAULT,
        help=f"Path to lock file (default: {LOCKFILE_DEFAULT}).",
    )

    args = parser.parse_args()

    from schemas import SCHEMAS
    if args.schema not in SCHEMAS:
        print(f"Unknown schema '{args.schema}'. Available: {', '.join(SCHEMAS)}", file=sys.stderr)
        sys.exit(1)

    schema = SCHEMAS[args.schema](token=args.token)
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
