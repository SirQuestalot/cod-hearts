#!/usr/bin/env python3
"""
Import pre-automation Modrinth versions as entry 0 (CHANGELOG + GitHub Release).

Commands:
  prepare  — fetch, filter, download, write artefact dir (no durable git/release writes)
  commit   — append CHANGELOG section, emit release notes path (release created by workflow)

Env (prepare):
  PROJECT_SLUG, PROJECT_TYPE (default resourcepack)
  MODRINTH_API (default https://api.modrinth.com/v2)
  CHANGELOG_FILE (default CHANGELOG.md)
  OUT_DIR (artefact output)
  FORCE (true/false)
  GITHUB_REPOSITORY (owner/repo) — optional check for existing release via gh not required here

Env (commit):
  OUT_DIR, CHANGELOG_FILE, CHANGELOG_URL, GITHUB_REPOSITORY
  IMPORT_DATE (YYYY-MM-DD)
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from update_changelog import (  # noqa: E402
    append_entry0_to_changelog,
    extract_modrinth_ids_from_changelog,
    format_entry0_release_notes,
    format_entry0_section,
    format_entry0_version_lines,
    has_entry_0,
    release_tag,
    release_url,
)

MAX_ATTEMPTS = 5
BACKOFF_CAP_SEC = 60
USER_AGENT = "cod-hearts-import/1.0 (github.com; resourcepack history import)"


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def http_json(url: str, timeout: int = 60):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} GET {url}:\n{body}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error GET {url}: {e.reason}") from e


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429, 500, 502, 503, 504)
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return True
    return False


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            if not data:
                raise OSError("empty download body")
            dest.write_bytes(data)
            return
        except Exception as e:
            last_error = e
            if attempt < MAX_ATTEMPTS and is_transient(e):
                wait = min(BACKOFF_CAP_SEC, 2**attempt)
                print(
                    f"  transient error attempt {attempt}/{MAX_ATTEMPTS}: {e}; "
                    f"retry in {wait}s"
                )
                time.sleep(wait)
                continue
            if is_transient(e):
                raise SystemExit(
                    f"Download failed after {MAX_ATTEMPTS} attempts: {url}\n{e}"
                ) from e
            # permanent
            raise
    if last_error:
        raise last_error


def derived_zip_prefix(project_slug: str) -> str:
    return "".join(word.capitalize() for word in project_slug.split("-"))


def primary_file(version: dict) -> dict | None:
    files = version.get("files") or []
    if not isinstance(files, list) or not files:
        return None
    for f in files:
        if isinstance(f, dict) and f.get("primary") is True:
            return f
    # no primary flag — treat as missing primary (skip with warning)
    return None


def fetch_all_versions(api: str, slug: str) -> list[dict]:
    """Modrinth returns the full list for project versions (no pagination required on v2)."""
    data = http_json(f"{api}/project/{slug}/version")
    if data is None:
        return []
    if not isinstance(data, list):
        raise SystemExit("Unexpected versions payload (not a list)")
    return [v for v in data if isinstance(v, dict)]


def cmd_prepare() -> None:
    api = env("MODRINTH_API", "https://api.modrinth.com/v2").rstrip("/")
    slug = env("PROJECT_SLUG")
    project_type = env("PROJECT_TYPE", "resourcepack").strip() or "resourcepack"
    changelog_path = Path(env("CHANGELOG_FILE", "CHANGELOG.md"))
    out_dir = Path(env("OUT_DIR"))
    force = os.environ.get("FORCE", "false").strip().lower() in ("1", "true", "yes")

    out_dir.mkdir(parents=True, exist_ok=True)
    zips_dir = out_dir / "zips"
    zips_dir.mkdir(parents=True, exist_ok=True)

    if not changelog_path.is_file():
        raise SystemExit(f"CHANGELOG not found: {changelog_path}")
    changelog_text = changelog_path.read_text(encoding="utf-8")

    if has_entry_0(changelog_text) and not force:
        raise SystemExit(
            "CHANGELOG already contains ## [0]. Re-run with force=true to replace."
        )

    excluded = extract_modrinth_ids_from_changelog(changelog_text)
    print(f"Excluded Modrinth IDs from existing entries: {len(excluded)}")

    project = http_json(f"{api}/project/{slug}")
    if not isinstance(project, dict):
        raise SystemExit(f"Project not found: {slug}")
    project_id = project.get("id") or ""
    project_title = project.get("title") or slug
    print(f"Project {slug!r} id={project_id} title={project_title!r}")

    versions = fetch_all_versions(api, slug)
    # explicit newest-first
    versions.sort(key=lambda v: v.get("date_published") or "", reverse=True)
    print(f"Fetched {len(versions)} version(s) from Modrinth")

    prefix = derived_zip_prefix(slug)
    warnings: list[str] = []
    entries: list[dict] = []

    for ver in versions:
        vid = str(ver.get("id") or "").strip()
        vnum = str(ver.get("version_number") or "").strip()
        if not vid:
            warnings.append(f"skip version with empty id (number={vnum!r})")
            continue
        if vid in excluded:
            print(f"  skip already-recorded id={vid} number={vnum}")
            continue

        pf = primary_file(ver)
        if pf is None:
            warnings.append(
                f"skip {vnum or vid}: no primary file "
                f"(external, empty, or multi-file without primary)"
            )
            continue
        file_url = str(pf.get("url") or "").strip()
        if not file_url:
            warnings.append(f"skip {vnum or vid}: primary file has empty url")
            continue

        file_name = f"{prefix}-v{vnum}.zip"
        dest = zips_dir / file_name
        print(f"  download {vnum} → {file_name}")
        try:
            download_file(file_url, dest)
        except SystemExit:
            raise
        except Exception as e:
            warnings.append(f"skip {vnum or vid}: download error: {e}")
            if dest.is_file():
                dest.unlink(missing_ok=True)
            continue

        game_versions = ver.get("game_versions") or []
        if not isinstance(game_versions, list):
            game_versions = []
        game_versions = [str(g) for g in game_versions]

        changelog = ver.get("changelog")
        if changelog is None:
            changelog = ""
        else:
            changelog = str(changelog)

        entries.append(
            {
                "title": str(ver.get("name") or ver.get("title") or "").strip(),
                "date_published": str(ver.get("date_published") or "").strip(),
                "version_number": vnum,
                "modrinth_id": vid,
                "file": file_name,
                "game_versions": game_versions,
                "changelog": changelog,
            }
        )

    if not entries:
        print("No versions left to import after filtering.")
        # Still write empty metadata so commit can no-op cleanly
        meta = {
            "project_slug": slug,
            "project_id": project_id,
            "project_title": project_title,
            "imported_date": "",
            "entries": [],
        }
        (out_dir / "metadata.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        (out_dir / "warnings.json").write_text(
            json.dumps(warnings, indent=2) + "\n", encoding="utf-8"
        )
        (out_dir / "prepare_summary.txt").write_text(
            "No versions to import.\n", encoding="utf-8"
        )
        _print_warnings(warnings)
        _gh_out("imported_date", "")
        _gh_out("entry_count", "0")
        _gh_out("version_numbers_json", "[]")
        return

    from datetime import datetime, timezone

    imported_date = datetime.now(timezone.utc).date().isoformat()
    meta = {
        "project_slug": slug,
        "project_id": project_id,
        "project_title": project_title,
        "imported_date": imported_date,
        "entries": entries,
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "warnings.json").write_text(
        json.dumps(warnings, indent=2) + "\n", encoding="utf-8"
    )

    summary_lines = [
        f"Prepared import for {slug}",
        f"imported_date={imported_date}",
        f"entries={len(entries)}",
        f"warnings={len(warnings)}",
        "",
        "Versions:",
    ]
    for e in entries:
        summary_lines.append(f"  {e['version_number']}  {e['modrinth_id']}  {e['file']}")
    if warnings:
        summary_lines.append("")
        summary_lines.append("Warnings:")
        for w in warnings:
            summary_lines.append(f"  {w}")
    summary = "\n".join(summary_lines) + "\n"
    (out_dir / "prepare_summary.txt").write_text(summary, encoding="utf-8")
    print(summary)
    _print_warnings(warnings)

    # Job outputs
    _gh_out("imported_date", imported_date)
    _gh_out("entry_count", str(len(entries)))
    _gh_out("version_numbers_json", json.dumps([e["version_number"] for e in entries]))


def _print_warnings(warnings: list[str]) -> None:
    if not warnings:
        print("No warnings.")
        return
    print(f"\n{len(warnings)} warning(s):")
    for w in warnings:
        print(f"  - {w}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write("\n### Import prepare warnings\n\n")
                for w in warnings:
                    fh.write(f"- {w}\n")
        except OSError as e:
            print(f"(Could not write GITHUB_STEP_SUMMARY: {e})", file=sys.stderr)


def _gh_out(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write(f"{name}<<OUTPUT_END\n{value}")
            if not value.endswith("\n"):
                fh.write("\n")
            fh.write("OUTPUT_END\n")
        else:
            fh.write(f"{name}={value}\n")


def cmd_commit() -> None:
    out_dir = Path(env("OUT_DIR"))
    changelog_path = Path(env("CHANGELOG_FILE", "CHANGELOG.md"))
    changelog_url = env("CHANGELOG_URL")
    github_repository = env("GITHUB_REPOSITORY")
    project_type = env("PROJECT_TYPE", "resourcepack").strip() or "resourcepack"
    force = os.environ.get("FORCE", "false").strip().lower() in ("1", "true", "yes")

    meta_path = out_dir / "metadata.json"
    if not meta_path.is_file():
        raise SystemExit(f"missing {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    entries = meta.get("entries") or []
    if not entries:
        print("No entries to commit; nothing to do.")
        _gh_out("skipped", "true")
        return

    day = (meta.get("imported_date") or "").strip()
    if not day:
        day = env("IMPORT_DATE")
    slug = str(meta.get("project_slug") or "").strip()
    project_title = str(meta.get("project_title") or slug).strip()

    if not changelog_path.is_file():
        raise SystemExit(f"CHANGELOG not found: {changelog_path}")
    cl_text = changelog_path.read_text(encoding="utf-8")
    if has_entry_0(cl_text) and not force:
        raise SystemExit(
            "CHANGELOG already contains ## [0]. Re-run with force=true to replace."
        )

    version_lines = format_entry0_version_lines(
        entries,
        project_title=project_title,
        project_type=project_type,
        slug=slug,
    )
    tag = release_tag(0, day)
    rel = release_url(github_repository, 0, day)
    section = format_entry0_section(
        day=day,
        version_lines=version_lines,
        release_url_str=rel,
        tag=tag,
    )
    notes = format_entry0_release_notes(
        day=day,
        version_lines=version_lines,
        changelog_url=changelog_url,
    )

    append_entry0_to_changelog(changelog_path, section)
    notes_path = out_dir / "release-notes.md"
    notes_path.write_text(notes, encoding="utf-8")

    print(f"Wrote ## [0] - {day} to {changelog_path}")
    print(f"Wrote release notes to {notes_path}")
    print(f"tag={tag}")
    print(f"release_url={rel}")

    _gh_out("skipped", "false")
    _gh_out("import_date", day)
    _gh_out("release_tag", tag)
    _gh_out("release_url", rel)
    _gh_out(
        "version_numbers_json",
        json.dumps([str(e.get("version_number") or "") for e in entries]),
    )
    # durable actions so far (CHANGELOG written; release not yet)
    durable = [f"CHANGELOG.md ## [0] - {day}"]
    _gh_out("durable_json", json.dumps(durable))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Import Modrinth history as entry 0")
    parser.add_argument("command", choices=["prepare", "commit"])
    args = parser.parse_args(argv)
    if args.command == "prepare":
        cmd_prepare()
    elif args.command == "commit":
        cmd_commit()
    else:
        raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
