"""
Resolver for the github-actions module.

Resolves owner/repo@ref to an immutable commit SHA via the GitHub REST API.
Handles lightweight tags, annotated tags (two-step dereference), and branches.
"""

import json
import os
import re
import urllib.error
import urllib.request

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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
        repo = repo.split("/")[0]  # strip subdirectory paths

        for kind in ("tags", "heads"):
            try:
                data = _gh_api(f"/repos/{owner}/{repo}/git/ref/{kind}/{ref}", self.token)
                obj = data["object"]
                if obj["type"] == "commit":
                    return obj["sha"]
                # Annotated tag — dereference to the underlying commit
                tag_data = _gh_api(f"/repos/{owner}/{repo}/git/tags/{obj['sha']}", self.token)
                return tag_data["object"]["sha"]
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise

        raise ValueError(f"Could not resolve ref '{ref}' in {owner}/{repo}")
