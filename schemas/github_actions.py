"""
GitHub Actions schema for modlock.

Handles scanning workflow YAML files for `uses: owner/repo@ref` action
references and resolving them to immutable commit SHAs via the GitHub API.
"""

import re
import urllib.request
import urllib.error
import json
import os

# Matches `uses: owner/repo@ref` with optional leading whitespace/dash
_USES_RE = re.compile(
    r'^(?P<prefix>[ \t]*-?[ \t]*uses:[ \t]*)(?P<action>[a-zA-Z0-9_.-]+/[a-zA-Z0-9_./.-]+)@(?P<ref>[a-zA-Z0-9_./:-]+)(?P<suffix>.*)$'
)

# A full 40-char hex SHA — already pinned, nothing to resolve
_SHA_RE = re.compile(r'^[0-9a-f]{40}$')

FILE_PATTERNS = ["**/.github/workflows/*.yml", "**/.github/workflows/*.yaml"]


def _gh_api(path: str, token: str | None = None) -> dict:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _resolve_ref(owner: str, repo: str, ref: str, token: str | None) -> str:
    """Return the commit SHA that `ref` points to in owner/repo."""
    if _SHA_RE.match(ref):
        return ref

    # Try as a tag first, then as a branch
    for kind in ("tags", "heads"):
        try:
            data = _gh_api(f"/repos/{owner}/{repo}/git/ref/{kind}/{ref}", token)
            obj = data["object"]
            if obj["type"] == "commit":
                return obj["sha"]
            # Annotated tag — dereference to the commit
            tag_data = _gh_api(f"/repos/{owner}/{repo}/git/tags/{obj['sha']}", token)
            return tag_data["object"]["sha"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise

    raise ValueError(f"Could not resolve ref '{ref}' in {owner}/{repo}")


class GitHubActionsSchema:
    name = "github-actions"

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan(self, content: str) -> list[dict]:
        """
        Return a list of references found in the file content.

        Each entry: {"action": "owner/repo", "ref": "v4", "line": 3}
        """
        refs = []
        for i, line in enumerate(content.splitlines(), 1):
            m = _USES_RE.match(line)
            if m:
                refs.append({
                    "action": m.group("action"),
                    "ref": m.group("ref"),
                    "line": i,
                })
        return refs

    # ------------------------------------------------------------------
    # Resolving
    # ------------------------------------------------------------------

    def resolve(self, action: str, ref: str) -> str:
        """Return the commit SHA for action@ref."""
        owner, repo = action.split("/", 1)
        # Strip subdirectory paths (e.g. org/repo/.github/actions/foo → org/repo)
        repo = repo.split("/")[0]
        return _resolve_ref(owner, repo, ref, self.token)

    # ------------------------------------------------------------------
    # Generating locked content
    # ------------------------------------------------------------------

    def apply(self, content: str, locks: dict[str, str]) -> str:
        """
        Return a copy of `content` with every `uses: action@ref` replaced by
        the locked SHA, with the original ref preserved in a trailing comment.

        `locks` maps "owner/repo@ref" → "sha".
        """
        out_lines = []
        for line in content.splitlines():
            m = _USES_RE.match(line)
            if m:
                action = m.group("action")
                ref = m.group("ref")
                key = f"{action}@{ref}"
                if key in locks and not _SHA_RE.match(ref):
                    sha = locks[key]
                    locked_line = f"{m.group('prefix')}{action}@{sha} # {ref}"
                    suffix = m.group("suffix").strip()
                    # Preserve anything after the ref (e.g. inline comments already there)
                    if suffix and not suffix.startswith("#"):
                        locked_line += f" {suffix}"
                    out_lines.append(locked_line)
                    continue
            out_lines.append(line)
        return "\n".join(out_lines) + "\n"
