#!/usr/bin/env python3
"""
Publish resource-pack zips from a build artifact's metadata.json to Modrinth.

Flow:
  1. Local validate_artifact(DIST_DIR)
  2. Read-only Modrinth identity checks (token + project id vs slug)
  3. Create each version (oldest first) with shared changelog_body; retries; fail-fast

Env:
  MODRINTH_API   - API base (default https://api.modrinth.com/v2)
  MODRINTH_TOKEN - PAT with VERSION_CREATE
  DIST_DIR       - folder containing metadata.json and the zip files (default: dist)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from validate_artifact import validate_artifact  # noqa: E402

MAX_ATTEMPTS = 5
REQUEST_TIMEOUT_SEC = 240
BACKOFF_CAP_SEC = 60


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


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (408, 429):
            return True
        if 500 <= exc.code <= 599:
            return True
        return False
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    return False


def retry_after_seconds(exc: BaseException, attempt: int) -> float:
    if isinstance(exc, urllib.error.HTTPError):
        ra = exc.headers.get("Retry-After") if exc.headers else None
        if ra:
            try:
                return min(float(ra), float(BACKOFF_CAP_SEC))
            except ValueError:
                pass
    base = min(2**attempt, BACKOFF_CAP_SEC)
    return float(min(base, BACKOFF_CAP_SEC))


def create_version(
    api: str,
    token: str,
    ua: str,
    payload: dict,
    zip_path: Path,
) -> dict:
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
            "User-Agent": ua,
            "Content-Type": content_type,
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def verify_project_identity(
    api: str, token: str, project_id: str, slug: str, ua: str
) -> None:
    """Read-only: token works and metadata.project_id matches API for slug."""
    url = f"{api}/project/{slug}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": token,
            "User-Agent": ua,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            project = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"Modrinth HTTP {e.code} during identity check GET {url}:\n{body}"
        ) from e
    except urllib.error.URLError as e:
        raise SystemExit(
            f"Network error during identity check GET {url}: {e.reason}"
        ) from e

    remote_id = project.get("id")
    if remote_id != project_id:
        raise SystemExit(
            f"metadata.project_id {project_id!r} does not match "
            f"API project id {remote_id!r} for slug {slug!r}"
        )


def main() -> None:
    api = env("MODRINTH_API", "https://api.modrinth.com/v2").rstrip("/")
    token = env("MODRINTH_TOKEN")
    dist_dir = Path(os.environ.get("DIST_DIR", "dist"))

    errors = validate_artifact(dist_dir)
    if errors:
        print(f"Artifact validation failed with {len(errors)} problem(s):")
        for i, msg in enumerate(errors, 1):
            print(f"  {i}. {msg}")
        raise SystemExit(1)
    print("Artifact validation passed.")

    meta_path = dist_dir / "metadata.json"
    if not meta_path.is_file():
        raise SystemExit(f"missing {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    body = meta.get("changelog_body")
    if not isinstance(body, str) or not body.strip():
        raise SystemExit("metadata.changelog_body is missing or empty")
    changelog = body.strip()

    entries = meta.get("entries") or []
    if not entries:
        raise SystemExit("metadata.json has no entries to publish")

    project_id = meta["project_id"]
    title = meta.get("project_title") or meta.get("project_slug") or project_id
    slug = meta.get("project_slug") or "resourcepack"

    ua = f"{slug}-publish/1.0"

    verify_project_identity(api, token, project_id, slug, ua)
    print(f"Modrinth identity OK for project {slug!r} ({project_id}).")

    succeeded: list[str] = []

    for entry in reversed(entries):
        key = entry["key"]
        ver_num = entry["version_number"]
        game_versions = entry["game_versions"]
        file_name = entry["file"]
        zip_path = dist_dir / file_name

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

        print(f"Publishing {key!r} → {ver_num} ({file_name}) ...")
        last_error: BaseException | None = None
        last_err_body = ""
        result = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = create_version(api, token, ua, payload, zip_path)
                last_error = None
                last_err_body = ""
                break
            except Exception as e:
                last_error = e
                # Drain HTTP body once (can only be read once) for logs and final exit
                last_err_body = ""
                if isinstance(e, urllib.error.HTTPError):
                    try:
                        last_err_body = e.read().decode("utf-8", errors="replace")
                    except Exception:
                        last_err_body = ""
                if not is_transient(e) or attempt >= MAX_ATTEMPTS:
                    break
                wait = retry_after_seconds(e, attempt)
                detail = ""
                if isinstance(e, urllib.error.HTTPError):
                    detail = f" HTTP {e.code}"
                print(
                    f"  transient error on attempt {attempt}/{MAX_ATTEMPTS}"
                    f"{detail}; retrying in {wait:.0f}s ..."
                )
                time.sleep(wait)

        if last_error is not None or result is None:
            if succeeded:
                print(
                    "Versions already created in this run "
                    "(manual cleanup on Modrinth may be needed):"
                )
                for line in succeeded:
                    print(f"  - {line}")
            if isinstance(last_error, urllib.error.HTTPError):
                raise SystemExit(
                    f"Modrinth HTTP {last_error.code} publishing {key!r}:\n{last_err_body}"
                ) from last_error
            raise SystemExit(
                f"Failed publishing {key!r}: {last_error}"
            ) from last_error

        vid = result.get("id", "?")
        print(f"  OK version id={vid} number={result.get('version_number')}")
        succeeded.append(f"{key} → {ver_num} (id={vid})")

    print(f"\nPublished {len(succeeded)} version(s).")


if __name__ == "__main__":
    main()