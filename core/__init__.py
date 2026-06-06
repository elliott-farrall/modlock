"""
modlock — modular version locking for plain-text config files.

Config-file mode (reads modlock.toml/yaml/json, no file arguments needed):
  modlock lock    Resolve refs, write lock file, and apply to files.
  modlock apply   Apply an existing lock file without re-resolving.
  modlock update  Re-resolve all refs, update lock file, and apply.

Explicit mode (single module, files supplied on the command line):
  modlock lock   --schema SCHEMA FILE [FILE ...]
  modlock apply  --schema SCHEMA FILE [FILE ...]
  modlock update --schema SCHEMA FILE [FILE ...]

Options:
  --schema   Module name (explicit mode only).
  --token    API token for the resolver (falls back to env var).
  --lockfile Path to the lock file (default: modlock.lock).
  --config   Path to the project config (auto-discovered if omitted).
"""

import argparse
import glob as _glob
import importlib.util
import json
import os
import re
import sys
import tomllib
from pathlib import Path

import yaml

LOCKFILE_DEFAULT = "modlock.lock"
CONFIG_NAMES = ["modlock.toml", "modlock.yaml", "modlock.yml", "modlock.json"]
MODULES_DIR = Path(__file__).parent.parent / "modules"


# ---------------------------------------------------------------------------
# Schema — drives scan / apply from a TOML config dict
# ---------------------------------------------------------------------------

class Schema:
    def __init__(self, config: dict, module_dir: Path, token: str | None = None):
        self.name = config["name"]
        self.file_patterns = config.get("file_patterns", [])

        scanner = config["scanner"]
        self._pattern = re.compile(scanner["pattern"])
        self._sha_pattern = re.compile(scanner["sha_pattern"])

        resolver_cfg = config["resolver"]
        self.action_field = resolver_cfg.get("action_field", "action")
        self.ref_field = resolver_cfg.get("ref_field", "ref")

        self._template = config["output"]["template"]

        mod = _load_module(module_dir)
        self._resolver = mod.Resolver(config=resolver_cfg, token=token)
        # Modules may override the default regex-based scanner/applier for
        # formats that require multi-line context (e.g. Azure Pipelines).
        self._custom_scanner = getattr(mod, "scan", None)
        self._custom_applier = getattr(mod, "apply", None)

    def lock_key(self, groups: dict) -> str:
        return f"{groups[self.action_field]}@{groups[self.ref_field]}"

    def _canonical_groups(self, groups: dict) -> dict:
        """
        If the matched ref is already a SHA and the suffix contains '# original-ref',
        substitute the original ref so the lock key stays stable across lock/apply cycles.
        This is what lets modlock lock and modlock update work correctly on already-applied
        files, and prevents sha-keyed noise entries accumulating in the lock file.
        """
        ref = groups.get(self.ref_field, "")
        suffix = groups.get("suffix", "")
        if self._sha_pattern.match(ref):
            m = re.match(r"\s*#\s*(\S+)", suffix)
            if m:
                return {**groups, self.ref_field: m.group(1)}
        return groups

    def scan(self, content: str) -> list[dict]:
        """Return one dict per matched reference, including all named groups and line number."""
        if self._custom_scanner:
            return self._custom_scanner(content)
        refs = []
        for i, line in enumerate(content.splitlines(), 1):
            m = self._pattern.match(line)
            if m:
                refs.append({"line": i, **self._canonical_groups(m.groupdict())})
        return refs

    def resolve(self, action: str, ref: str) -> str:
        """Return the commit SHA for action@ref."""
        return self._resolver.resolve(action, ref)

    def apply(self, content: str, locks: dict[str, str]) -> str:
        """
        Return content with every matched ref replaced by its locked SHA.
        The original ref is preserved as a trailing comment via the output template.
        Already-applied lines (SHA + comment) are also updated when the lock changes,
        enabling modlock update to work on files that have already been applied.
        """
        if self._custom_applier:
            return self._custom_applier(content, locks)
        out_lines = []
        for line in content.splitlines():
            m = self._pattern.match(line)
            if m:
                groups = self._canonical_groups(m.groupdict())
                ref = groups[self.ref_field]
                key = self.lock_key(groups)
                if key in locks and not self._sha_pattern.match(ref):
                    sha = locks[key]
                    new_line = self._template.format(**groups, sha=sha)
                    suffix = groups.get("suffix", "").strip()
                    if suffix and not suffix.startswith("#"):
                        new_line += f" {suffix}"
                    out_lines.append(new_line)
                    continue
            out_lines.append(line)
        return "\n".join(out_lines) + "\n"


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

def _load_module(module_dir: Path):
    """Load a module's resolver.py and return the module object."""
    resolver_path = module_dir / "resolver.py"
    spec = importlib.util.spec_from_file_location("resolver", resolver_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_schema(name: str, token: str | None = None) -> Schema:
    module_dir = MODULES_DIR / name
    toml_path = module_dir / "schema.toml"
    if not toml_path.exists():
        available = [p.name for p in MODULES_DIR.iterdir() if p.is_dir()]
        raise FileNotFoundError(
            f"No module '{name}' found. Available: {', '.join(sorted(available)) or 'none'}"
        )
    with open(toml_path, "rb") as f:
        config = tomllib.load(f)
    return Schema(config, module_dir=module_dir, token=token)


# ---------------------------------------------------------------------------
# Project config
# ---------------------------------------------------------------------------

def _find_config() -> str | None:
    """Return the first config file found in the current directory, or None."""
    for name in CONFIG_NAMES:
        if os.path.exists(name):
            return name
    return None


def load_config(path: str) -> dict[str, dict]:
    """
    Load a modlock config file (TOML, YAML, or JSON).
    Returns {module_name: {"files": [...], ...}}.
    """
    ext = Path(path).suffix.lower()
    if ext == ".toml":
        with open(path, "rb") as f:
            data = tomllib.load(f)
    elif ext in (".yaml", ".yml"):
        with open(path) as f:
            data = yaml.safe_load(f)
    elif ext == ".json":
        with open(path) as f:
            data = json.load(f)
    else:
        raise ValueError(
            f"Unsupported config format '{ext}'. Use .toml, .yaml, .yml, or .json."
        )
    return data.get("modules", {})


# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------

def load_lockfile(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        if data.get("version", 1) < 2:
            print(
                f"Warning: {path} is v1 format and cannot be reused. "
                f"Re-run 'modlock lock' to regenerate.",
                file=sys.stderr,
            )
            return {"version": 2, "modules": {}}
        return data
    return {"version": 2, "modules": {}}


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
# Commands — operate on a shared lockdata dict, caller saves
# ---------------------------------------------------------------------------

def cmd_lock(schema: Schema, files: list[str], lockdata: dict, force: bool = False) -> bool:
    """Resolve refs and update lockdata in-place. Returns True if anything changed."""
    module_locks = lockdata["modules"].setdefault(schema.name, {})
    changed = False

    for filepath in files:
        content = Path(filepath).read_text()
        for ref_info in schema.scan(content):
            action = ref_info[schema.action_field]
            ref = ref_info[schema.ref_field]
            key = schema.lock_key(ref_info)
            if key in module_locks and not force:
                print(f"  already locked: {key} → {module_locks[key][:12]}…")
                continue
            print(f"  resolving: {key}", end="", flush=True)
            try:
                sha = schema.resolve(action, ref)
                module_locks[key] = sha
                changed = True
                print(f" → {sha[:12]}…")
            except Exception as e:
                print(f" FAILED: {e}", file=sys.stderr)

    return changed


def cmd_apply(schema: Schema, files: list[str], lockdata: dict) -> None:
    """Rewrite files in-place using the locks stored in lockdata for this module."""
    module_locks = lockdata["modules"].get(schema.name, {})
    if not module_locks:
        print(f"  no locks for '{schema.name}' — run 'modlock lock' first.", file=sys.stderr)
        return

    for filepath in files:
        original = Path(filepath).read_text()
        locked = schema.apply(original, module_locks)
        if locked != original:
            Path(filepath).write_text(locked)
            print(f"  applied: {filepath}")
        else:
            print(f"  no changes: {filepath}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="modlock",
        description="Modular version locking for plain-text config files.",
    )
    parser.add_argument("command", choices=["lock", "apply", "update"])
    parser.add_argument(
        "files", nargs="*", metavar="FILE",
        help="Files to process (explicit mode). Omit to use modlock.toml.",
    )
    parser.add_argument("--schema", help="Module to use (required in explicit mode).")
    parser.add_argument("--token", help="API token for the resolver.")
    parser.add_argument("--lockfile", default=LOCKFILE_DEFAULT)
    parser.add_argument(
        "--config", default=None,
        help=f"Project config file. Auto-discovered if omitted (tries {', '.join(CONFIG_NAMES)}).",
    )

    args = parser.parse_args()

    # Build list of (schema, files) jobs
    if args.files:
        # Explicit mode — --schema required
        if not args.schema:
            parser.error("--schema is required when specifying files explicitly.")
        try:
            schema = load_schema(args.schema, token=args.token)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        files = expand_files(args.files)
        if not files:
            print("No files matched.", file=sys.stderr)
            sys.exit(1)
        jobs = [(schema, files)]
    else:
        # Config mode — auto-discover or use --config path
        config_path = args.config or _find_config()
        if config_path is None:
            print(
                f"No config file found. Tried: {', '.join(CONFIG_NAMES)}\n"
                f"Create one or use --schema and FILE arguments for explicit mode.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            config = load_config(config_path)
        except (FileNotFoundError, ValueError) as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        jobs = []
        for module_name, module_cfg in config.items():
            try:
                schema = load_schema(module_name, token=args.token)
            except FileNotFoundError as e:
                print(str(e), file=sys.stderr)
                sys.exit(1)
            patterns = module_cfg.get("files") or schema.file_patterns
            files = expand_files(patterns)
            if files:
                jobs.append((schema, files))

    lockdata = load_lockfile(args.lockfile)
    changed = False

    for schema, files in jobs:
        print(f"\n[{schema.name}]")
        if args.command in ("lock", "update"):
            changed |= cmd_lock(schema, files, lockdata, force=args.command == "update")
        if args.command in ("lock", "apply", "update"):
            cmd_apply(schema, files, lockdata)

    if args.command in ("lock", "update"):
        if changed:
            save_lockfile(args.lockfile, lockdata)
            print(f"\nLock file written: {args.lockfile}")
        else:
            print("\nNothing to update.")
