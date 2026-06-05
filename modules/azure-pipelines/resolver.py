"""
Resolver for the azure-pipelines module.

Locks repository resource refs in Azure Pipelines YAML files. Because
the repo name (name:) and its ref (ref:) are on separate lines, this
module provides custom scan() and apply() functions rather than relying
on the single-line regex scanner in the core.

Supported format:

  resources:
    repositories:
      - repository: alias
        type: github
        name: org/repo          ← paired with the ref: below
        ref: refs/tags/v1.2.3  ← locked to a commit SHA
"""

import json
import os
import re
import urllib.error
import urllib.request

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^(?P<indent>[ \t]+)name:[ \t]+(?P<action>[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+)[ \t]*$")
_REF_RE = re.compile(r"^(?P<prefix>[ \t]+ref:[ \t]+)(?P<ref>[^\s#]+)(?P<suffix>.*)$")


# ---------------------------------------------------------------------------
# Module-level scan / apply (override core defaults)
# ---------------------------------------------------------------------------

def scan(content: str) -> list[dict]:
    """
    Return one entry per repository resource ref found in content.
    Pairs each ref: line with the name: line that precedes it in the
    same resource block.
    """
    results = []
    pending_name: str | None = None
    pending_indent: int | None = None

    for i, line in enumerate(content.splitlines(), 1):
        name_m = _NAME_RE.match(line)
        if name_m:
            pending_name = name_m.group("action")
            pending_indent = len(name_m.group("indent"))
            continue

        ref_m = _REF_RE.match(line)
        if ref_m and pending_name is not None:
            ref_indent = len(ref_m.group("prefix")) - len(ref_m.group("prefix").lstrip())
            if ref_indent == pending_indent:
                results.append({
                    "line": i,
                    "action": pending_name,
                    "ref": ref_m.group("ref"),
                    "prefix": ref_m.group("prefix"),
                    "suffix": ref_m.group("suffix"),
                })
                pending_name = None
                pending_indent = None
                continue

        # A new repository block resets state
        if line.strip().startswith("- repository:"):
            pending_name = None
            pending_indent = None

    return results


def apply(content: str, locks: dict[str, str]) -> str:
    """
    Rewrite ref: lines whose paired name: has an entry in locks,
    replacing the ref with the locked SHA and preserving the original
    ref as a comment.
    """
    out_lines = []
    pending_name: str | None = None
    pending_indent: int | None = None

    for line in content.splitlines():
        name_m = _NAME_RE.match(line)
        if name_m:
            pending_name = name_m.group("action")
            pending_indent = len(name_m.group("indent"))
            out_lines.append(line)
            continue

        ref_m = _REF_RE.match(line)
        if ref_m and pending_name is not None:
            ref_indent = len(ref_m.group("prefix")) - len(ref_m.group("prefix").lstrip())
            if ref_indent == pending_indent:
                ref = ref_m.group("ref")
                key = f"{pending_name}@{ref}"
                if key in locks and not _SHA_RE.match(ref):
                    sha = locks[key]
                    out_lines.append(f"{ref_m.group('prefix')}{sha} # {ref}")
                    pending_name = None
                    pending_indent = None
                    continue
                pending_name = None
                pending_indent = None

        if line.strip().startswith("- repository:"):
            pending_name = None
            pending_indent = None

        out_lines.append(line)

    return "\n".join(out_lines) + "\n"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def _gh_api(path: str, token: str | None) -> dict:
    req = urllib.request.Request(f"https://api.github.com{path}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


class Resolver:
    def __init__(self, config: dict | None = None, token: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")

    def resolve(self, action: str, ref: str) -> str:
        if _SHA_RE.match(ref):
            return ref

        owner, repo = action.split("/", 1)

        # Normalise refs/tags/v1.0 → (tags, v1.0) and refs/heads/main → (heads, main)
        if ref.startswith("refs/tags/"):
            candidates = [("tags", ref[len("refs/tags/"):])]
        elif ref.startswith("refs/heads/"):
            candidates = [("heads", ref[len("refs/heads/"):])]
        else:
            candidates = [("tags", ref), ("heads", ref)]

        for kind, short_ref in candidates:
            try:
                data = _gh_api(f"/repos/{owner}/{repo}/git/ref/{kind}/{short_ref}", self.token)
                obj = data["object"]
                if obj["type"] == "commit":
                    return obj["sha"]
                tag_data = _gh_api(f"/repos/{owner}/{repo}/git/tags/{obj['sha']}", self.token)
                return tag_data["object"]["sha"]
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise

        raise ValueError(f"Could not resolve ref '{ref}' in {owner}/{repo}")
