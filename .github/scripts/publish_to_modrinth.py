#!/usr/bin/env python3
"""
Publish resource-pack zips from a build artifact's metadata.json to Modrinth.

Flow:
  1. Local validate_artifact(DIST_DIR)
  2. Read-only Modrinth identity checks (token + project id vs slug)
  3. Changelog helpers: entry n, UTC date, Modrinth body + footer
  4. Create each version (oldest first); collect ids; write GITHUB_OUTPUT

Env:
  MODRINTH_API      - API base (default https://api.modrinth.com/v2)
  MODRINTH_TOKEN    - PAT with VERSION_CREATE
  DIST_DIR          - folder with metadata.json and zips (default: dist)
  CHANGELOG_FILE    - path to CHANGELOG.md (default: CHANGELOG.md)
  CHANGELOG_URL     - full URL for footer link to CHANGELOG.md
  CHANGELOG_FOOTER_LABEL - optional link text (default in update_changelog)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from report_residuals import emit_residuals, format_residuals  # noqa: E402
from validate_artifact import validate_artifact  # noqa: E402
from update_changelog import format_modrinth_changelog, next_sequence  # noqa: E402

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



def write_github_output(name: str, value: str) -> None:
    """Append a workflow step output (supports multiline via heredoc)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"GITHUB_OUTPUT not set; skip output {name}")
        return
    with open(path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write(f"{name}<<OUTPUT_END\n")
            fh.write(value)
            if not value.endswith("\n"):
                fh.write("\n")
            fh.write("OUTPUT_END\n")
        else:
            fh.write(f"{name}={value}\n")


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
    body = body.strip()

    entries = meta.get("entries") or []
    if not entries:
        raise SystemExit("metadata.json has no entries to publish")

    changelog_file = Path(os.environ.get("CHANGELOG_FILE", "CHANGELOG.md"))
    changelog_url = os.environ.get("CHANGELOG_URL", "").strip()
    if not changelog_url:
        raise SystemExit(
            "CHANGELOG_URL is not set "
            "(workflow should set the full GitHub URL to CHANGELOG.md)"
        )
    if not changelog_file.is_file():
        raise SystemExit(f"CHANGELOG file not found: {changelog_file}")

    entry_n = next_sequence(changelog_file.read_text(encoding="utf-8"))
    day = datetime.now(timezone.utc).date().isoformat()
    changelog = format_modrinth_changelog(body, entry_n, day, changelog_url)
    print(f"Changelog entry n={entry_n} date={day}")

    project_id = meta["project_id"]
    title = meta.get("project_title") or meta.get("project_slug") or project_id
    slug = meta.get("project_slug") or "resourcepack"

    ua = f"{slug}-publish/1.0"

    verify_project_identity(api, token, project_id, slug, ua)
    print(f"Modrinth identity OK for project {slug!r} ({project_id}).")

    succeeded: list[str] = []
    succeeded_items: list[dict] = []
    published_by_number: dict[str, str] = {}

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
            if succeeded_items:
                emit_residuals(
                    format_residuals(
                        stage="publish",
                        versions=succeeded_items,
                        entry_n=entry_n,
                        day=day,
                        slug=slug,
                        project_type=os.environ.get("PROJECT_TYPE", "resourcepack")
                        .strip()
                        or "resourcepack",
                        outcome="failure",
                    ),
                    outcome="failure",
                )
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
        item = {
            "key": str(key).strip(),
            "version_number": str(ver_num).strip(),
            "id": vid if isinstance(vid, str) else "",
        }
        succeeded_items.append(item)
        if isinstance(vid, str) and vid.strip() and vid != "?":
            published_by_number[str(ver_num).strip()] = vid.strip()

    print(f"\nPublished {len(succeeded)} version(s).")

    versions_out = []
    for entry in entries:
        ver_num = str(entry["version_number"]).strip()
        key = str(entry.get("key", "")).strip()
        versions_out.append(
            {
                "version_number": ver_num,
                "id": published_by_number.get(ver_num, ""),
                "key": key,
            }
        )
    versions_json = json.dumps(versions_out, separators=(",", ":"))
    write_github_output("entry_n", str(entry_n))
    write_github_output("changelog_date", day)
    write_github_output("versions_json", versions_json)
    write_github_output("project_slug", slug)
    print(f"Wrote job outputs entry_n={entry_n} date={day} versions={len(versions_out)}")

    # Inventory after successful Modrinth creates (changelog/release may still follow)
    emit_residuals(
        format_residuals(
            stage="publish",
            versions=versions_out,
            entry_n=entry_n,
            day=day,
            slug=slug,
            project_type=os.environ.get("PROJECT_TYPE", "resourcepack").strip()
            or "resourcepack",
            outcome="success",
        ),
        outcome="success",
    )


if __name__ == "__main__":
    main()
