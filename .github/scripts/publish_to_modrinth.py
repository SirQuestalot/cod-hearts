#!/usr/bin/env python3
"""
Publish resource-pack zips from a build artifact's metadata.json to Modrinth.

Env:
  MODRINTH_API   - API base (default https://api.modrinth.com/v2)
  MODRINTH_TOKEN - PAT with VERSION_CREATE
  DIST_DIR       - folder containing metadata.json and the zip files
                   (default: dist)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def encode_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----ModrinthBoundary7MA4YWxkTrZu0gW"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(value if isinstance(value, bytes) else str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for name, path in files.items():
        data = path.read_bytes()
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{path.name}"\r\n'
                f"Content-Type: application/zip\r\n\r\n"
            ).encode()
        )
        body.extend(data)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())
    content_type = f"multipart/form-data; boundary={boundary}"
    return bytes(body), content_type


def main() -> None:
    api = env("MODRINTH_API", "https://api.modrinth.com/v2").rstrip("/")
    token = env("MODRINTH_TOKEN")
    dist_dir = Path(os.environ.get("DIST_DIR", "dist"))

    meta_path = dist_dir / "metadata.json"
    if not meta_path.is_file():
        raise SystemExit(f"metadata.json not found: {meta_path}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    project_id = meta["project_id"]
    title = meta.get("project_title") or meta.get("project_slug") or project_id
    slug = meta.get("project_slug") or "resourcepack"
    entries = meta.get("entries") or []

    if not entries:
        raise SystemExit("metadata.json has no entries to publish")

    for entry in entries:
        key = entry["key"]
        ver_num = entry["version_number"]
        changelog = entry.get("changelog") or ""
        game_versions = entry["game_versions"]
        file_name = entry["file"]
        zip_path = dist_dir / file_name

        if not zip_path.is_file():
            raise SystemExit(f"Missing zip for key {key!r}: {zip_path}")

        payload = {
            "name": f"{title} {key}",
            "version_number": ver_num,
            "changelog": changelog,
            "dependencies": [],
            "game_versions": game_versions,
            "version_type": "release",
            "loaders": ["minecraft"],
            "featured": False,
            "project_id": project_id,
            "file_parts": ["file"],
            "primary_file": "file",
        }

        body, content_type = encode_multipart(
            {"data": json.dumps(payload)},
            {"file": zip_path},
        )

        req = urllib.request.Request(
            f"{api}/version",
            data=body,
            method="POST",
            headers={
                "Authorization": token,
                "User-Agent": f"{slug}-publish/1.0",
                "Content-Type": content_type,
            },
        )

        print(f"Publishing {key!r} → {ver_num} ({file_name}) ...")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise SystemExit(
                f"Modrinth HTTP {e.code} publishing {key!r}:\n{err_body}"
            ) from e

        print(
            f"  OK version id={result.get('id', '?')} "
            f"number={result.get('version_number')}"
        )

    print(f"\nPublished {len(entries)} version(s).")


if __name__ == "__main__":
    main()