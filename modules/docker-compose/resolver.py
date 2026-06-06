"""
Resolver for the docker-compose module.

Locks `image:` fields to their Docker manifest digest, e.g.:
  image: postgres:16  →  image: postgres@sha256:abc... # 16

Uses the Docker registry v2 API (registry-1.docker.io) to fetch the manifest
digest. Official Docker Hub images (e.g. postgres, redis) are resolved under
the implicit 'library/' namespace.
"""

import json
import re
import urllib.error
import urllib.request

_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


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
