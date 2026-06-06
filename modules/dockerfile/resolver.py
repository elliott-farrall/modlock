"""
Resolver for the dockerfile module.

Locks FROM instructions to their Docker manifest digest, e.g.:

  # python:3.12-slim          ← original tag preserved as a Dockerfile comment
  FROM python@sha256:abc...   ← locked to an immutable digest

A comment line above is used instead of a trailing inline comment because
Dockerfile does not treat '#' as a comment character mid-line.

Uses the Docker registry v2 API (registry-1.docker.io) to fetch the manifest
digest. Official Docker Hub images (e.g. python, ubuntu) are resolved under
the implicit 'library/' namespace.
"""

import json
import re
import urllib.error
import urllib.request

_PATTERN = re.compile(
    r"^(?P<prefix>FROM\s+)(?P<image>[a-zA-Z0-9._/-]+):(?P<ref>[a-zA-Z0-9._-]+)(?P<suffix>.*)$"
)
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Module-level scan / apply (override core defaults)
# ---------------------------------------------------------------------------

def scan(content: str) -> list[dict]:
    results = []
    for i, line in enumerate(content.splitlines(), 1):
        m = _PATTERN.match(line)
        if m:
            results.append({"line": i, **m.groupdict()})
    return results


def apply(content: str, locks: dict[str, str]) -> str:
    out_lines = []
    for line in content.splitlines():
        m = _PATTERN.match(line)
        if m:
            groups = m.groupdict()
            ref = groups["ref"]
            image = groups["image"]
            key = f"{image}@{ref}"
            if key in locks and not _SHA_RE.match(ref):
                sha = locks[key]
                suffix = groups["suffix"].strip()
                new_from = f"{groups['prefix']}{image}@{sha}"
                if suffix and not suffix.startswith("#"):
                    new_from += f" {suffix}"
                out_lines.append(f"# {image}:{ref}")
                out_lines.append(new_from)
                continue
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def _get_token(image_path: str) -> str:
    url = (
        "https://auth.docker.io/token"
        f"?service=registry.docker.io"
        f"&scope=repository:{image_path}:pull"
    )
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())["token"]


class Resolver:
    def __init__(self, config=None, token=None):
        pass  # Docker Hub uses anonymous pull tokens; no user token needed.

    def resolve(self, image: str, ref: str) -> str:
        if _SHA_RE.match(ref):
            return ref

        image_path = f"library/{image}" if "/" not in image else image
        token = _get_token(image_path)

        req = urllib.request.Request(
            f"https://registry-1.docker.io/v2/{image_path}/manifests/{ref}"
        )
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header(
            "Accept",
            (
                "application/vnd.docker.distribution.manifest.list.v2+json,"
                "application/vnd.oci.image.index.v1+json,"
                "application/vnd.docker.distribution.manifest.v2+json"
            ),
        )
        try:
            with urllib.request.urlopen(req) as resp:
                digest = resp.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as e:
            raise ValueError(f"Could not resolve {image}:{ref}: HTTP {e.code}")

        if not digest:
            raise ValueError(f"No digest returned for {image}:{ref}")
        return digest
