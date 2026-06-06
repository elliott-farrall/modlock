"""
Resolver for the docker-compose module.

Locks `image:` fields to their Docker manifest digest, e.g.:
  image: postgres:16  →  image: postgres@sha256:abc... # 16

Already-applied lines are also handled so that modlock lock is idempotent
and modlock update can re-resolve the original tag on an already-applied file.

Uses the Docker registry v2 API (registry-1.docker.io) to fetch the manifest
digest. Official Docker Hub images (e.g. postgres, redis) are resolved under
the implicit 'library/' namespace.
"""

import json
import re
import urllib.error
import urllib.request

_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Matches unlocked:  image: postgres:16
_UNLOCKED = re.compile(
    r"^(?P<prefix>[ \t]+image:[ \t]+)(?P<image>[a-zA-Z0-9._/-]+):(?P<ref>[a-zA-Z0-9._-]+)(?P<suffix>.*)$"
)
# Matches already-applied:  image: postgres@sha256:abc... # 16
# Captures the original tag from the comment so the lock key stays stable.
_LOCKED = re.compile(
    r"^(?P<prefix>[ \t]+image:[ \t]+)(?P<image>[a-zA-Z0-9._/-]+)@sha256:[0-9a-f]{64}\s+#\s+(?P<ref>[a-zA-Z0-9._-]+)(?P<suffix>.*)$"
)


# ---------------------------------------------------------------------------
# Module-level scan / apply (override core defaults)
# ---------------------------------------------------------------------------

def scan(content: str) -> list[dict]:
    """
    Return one entry per image: line (both unlocked and already-applied formats).
    For already-applied lines, the original tag is extracted from the comment so
    that the lock key is the same as before applying.
    """
    results = []
    for i, line in enumerate(content.splitlines(), 1):
        m = _UNLOCKED.match(line) or _LOCKED.match(line)
        if m:
            results.append({"line": i, **m.groupdict()})
    return results


def apply(content: str, locks: dict[str, str]) -> str:
    """
    Rewrite image: lines using locks. Handles both unlocked (image:tag) and
    already-applied (image@sha # tag) formats, so modlock update works on
    files that have already been applied.
    """
    out_lines = []
    for line in content.splitlines():
        m = _UNLOCKED.match(line) or _LOCKED.match(line)
        if m:
            groups = m.groupdict()
            key = f"{groups['image']}@{groups['ref']}"
            if key in locks:
                sha = locks[key]
                out_lines.append(f"{groups['prefix']}{groups['image']}@{sha} # {groups['ref']}")
                continue
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


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
