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

# Matches unlocked:  FROM python:3.12-slim [AS builder]
_PATTERN = re.compile(
    r"^(?P<prefix>FROM\s+)(?P<image>[a-zA-Z0-9._/-]+):(?P<ref>[a-zA-Z0-9._-]+)(?P<suffix>.*)$"
)
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Matches the lock comment we write above each FROM:  # python:3.12-slim
_LOCK_COMMENT = re.compile(r"^#\s+(?P<image>[a-zA-Z0-9._/-]+):(?P<ref>[a-zA-Z0-9._-]+)$")
# Matches the locked FROM line:  FROM python@sha256:...  [AS builder]
_LOCKED_FROM = re.compile(
    r"^FROM\s+(?P<image>[a-zA-Z0-9._/-]+)@sha256:[0-9a-f]{64}(?P<suffix>.*)$"
)


# ---------------------------------------------------------------------------
# Module-level scan / apply (override core defaults)
# ---------------------------------------------------------------------------

def scan(content: str) -> list[dict]:
    """
    Return one entry per FROM instruction (both unlocked and already-applied formats).
    Already-applied blocks (# image:tag comment + FROM image@sha256:... line) are
    detected via the state machine so the original tag is returned as the ref,
    keeping the lock key stable across lock/apply cycles.
    """
    results = []
    pending: dict | None = None  # {image, ref} from a lock comment line

    for i, line in enumerate(content.splitlines(), 1):
        cm = _LOCK_COMMENT.match(line)
        if cm:
            pending = {"image": cm.group("image"), "ref": cm.group("ref")}
            continue

        if pending:
            lm = _LOCKED_FROM.match(line)
            if lm and lm.group("image") == pending["image"]:
                results.append({
                    "line": i,
                    "image": pending["image"],
                    "ref": pending["ref"],
                    "prefix": "FROM ",
                    "suffix": lm.group("suffix"),
                })
                pending = None
                continue
            # Comment wasn't followed by a matching locked FROM — treat as regular comment
            pending = None

        m = _PATTERN.match(line)
        if m:
            results.append({"line": i, **m.groupdict()})

    return results


def apply(content: str, locks: dict[str, str]) -> str:
    """
    Lock or update FROM instructions.
    - Unlocked (FROM image:tag): write lock comment above, replace tag with SHA.
    - Already-applied (# image:tag / FROM image@sha256:...): update SHA when lock changes.
    """
    out_lines = []
    pending_comment: tuple | None = None  # (original_line, image, ref)

    for line in content.splitlines():
        cm = _LOCK_COMMENT.match(line)
        if cm:
            pending_comment = (line, cm.group("image"), cm.group("ref"))
            continue

        if pending_comment:
            orig_line, c_image, c_ref = pending_comment
            lm = _LOCKED_FROM.match(line)
            if lm and lm.group("image") == c_image:
                key = f"{c_image}@{c_ref}"
                if key in locks:
                    sha = locks[key]
                    suffix = lm.group("suffix").strip()
                    new_from = f"FROM {c_image}@{sha}"
                    if suffix and not suffix.startswith("#"):
                        new_from += f" {suffix}"
                    out_lines.append(f"# {c_image}:{c_ref}")
                    out_lines.append(new_from)
                else:
                    out_lines.append(orig_line)
                    out_lines.append(line)
                pending_comment = None
                continue
            out_lines.append(orig_line)
            pending_comment = None

        m = _PATTERN.match(line)
        if m:
            groups = m.groupdict()
            key = f"{groups['image']}@{groups['ref']}"
            if key in locks and not _SHA_RE.match(groups["ref"]):
                sha = locks[key]
                suffix = groups["suffix"].strip()
                new_from = f"{groups['prefix']}{groups['image']}@{sha}"
                if suffix and not suffix.startswith("#"):
                    new_from += f" {suffix}"
                out_lines.append(f"# {groups['image']}:{groups['ref']}")
                out_lines.append(new_from)
                continue

        out_lines.append(line)

    if pending_comment:
        out_lines.append(pending_comment[0])

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
