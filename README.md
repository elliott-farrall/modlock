# modlock

Modular version locking for plain-text config files.

modlock resolves version references in config files (tags, branches) to
immutable commit SHAs and stores the mapping in a lock file. When a lock
is applied, the original reference is kept as a comment so the file
remains human-readable. Future runs use the locked SHA unless you
explicitly update it.

```yaml
# before
- uses: actions/checkout@v4

# after
- uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4
```

## Installation

Requires Python 3.11+.

```sh
pip install modlock
```

## Usage

### lock

Resolve all version references in one or more files and write a lock file.

```sh
modlock lock --schema github-actions .github/workflows/*.yml
```

On the first run this creates `modlock.lock`. References that are already
in the lock file are left untouched; only new ones are resolved.

### apply

Rewrite files in-place using the locked SHAs.

```sh
modlock apply --schema github-actions .github/workflows/*.yml
```

### update

Re-resolve every reference (ignoring existing locks) then apply.

```sh
modlock update --schema github-actions .github/workflows/*.yml
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--schema` | `github-actions` | Module to use for scanning and resolving |
| `--lockfile` | `modlock.lock` | Path to the lock file |
| `--token` | `$GITHUB_TOKEN` | API token for the resolver |

## Lock file

`modlock.lock` is a plain JSON file you should commit alongside your
config files.

```json
{
  "version": 1,
  "locks": {
    "actions/checkout@v4": "11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/setup-python@v5": "a26ac4671e2c6d8a506ba6fa9b5b07d9afc3cf72"
  }
}
```

Changing or removing an entry and re-running `modlock lock` will
re-resolve that reference. Everything else stays pinned.

## Modules

| Module | File patterns | What it locks |
|--------|--------------|---------------|
| `github-actions` | `.github/workflows/*.yml` | `uses: owner/repo@ref` action references |
| `azure-pipelines` | `azure-pipelines.yml`, `.azure/**/*.yml` | `name:`/`ref:` repository resource pairs |

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a new module.
