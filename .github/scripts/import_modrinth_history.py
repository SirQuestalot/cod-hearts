#!/usr/bin/env python3
"""
Import pre-automation Modrinth versions as entry 0 (CHANGELOG + GitHub Release).

Commands:
  prepare  — complete version fetch, download zips, write artifact (no durable writes)
  commit   — write ## [0] section and release notes file (git/release done in workflow)

Env (prepare):
  PROJECT_SLUG, PROJECT_TYPE (default resourcepack)
  MODRINTH_API (default https://api.modrinth.com/v2)
  CHANGELOG_FILE (default CHANGELOG.md)
  OUT_DIR (artefact output)
  FORCE (true/false)
  GITHUB_REPOSITORY (owner/repo) — optional check for existing release via gh not required here


Env (commit):
  OUT_DIR, CHANGELOG_FILE, CHANGELOG_URL, GITHUB_REPOSITORY, FORCE
  IMPORT_DATE (fallback if metadata lacks imported_date)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
VERSION_IDS_BATCH = 50


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def force_enabled() -> bool:
    return os.environ.get("FORCE", "false").strip().lower() in ("1", "true", "yes")


def http_json(url: str, timeout: int = 60):
    """GET JSON; raises urllib errors to caller."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))


def http_json_or_exit(url: str, timeout: int = 60):
    try:
        return http_json(url, timeout=timeout)
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
    return None


def fetch_version_by_id(api: str, version_id: str) -> dict | None:
    """Return version dict or None after exhausting retries on transient errors."""
    url = f"{api}/version/{version_id}"
    last_error: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = http_json(url)
            if isinstance(data, dict):
                return data
            return None
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code in (404, 403, 410):
                return None
            if attempt < MAX_ATTEMPTS and is_transient(e):
                wait = min(BACKOFF_CAP_SEC, 2**attempt)
                print(f"  transient HTTP {e.code} for {version_id}; retry in {wait}s")
                time.sleep(wait)
                continue
            if is_transient(e):
                return None
            return None
        except Exception as e:
            last_error = e
            if attempt < MAX_ATTEMPTS and is_transient(e):
                wait = min(BACKOFF_CAP_SEC, 2**attempt)
                print(f"  transient error for {version_id}: {e}; retry in {wait}s")
                time.sleep(wait)
                continue
            return None
    return None


def fetch_versions_by_ids(api: str, ids: list[str]) -> dict[str, dict]:
    """Batch GET /versions?ids=... ; returns id -> version for successful hits."""
    out: dict[str, dict] = {}
    if not ids:
        return out
    # API expects a JSON array in the query string
    encoded = urllib.parse.quote(json.dumps(ids), safe="")
    url = f"{api}/versions?ids={encoded}"
    try:
        data = http_json(url)
    except Exception as e:
        print(f"  batch fetch failed ({e}); falling back to per-id")
        for vid in ids:
            ver = fetch_version_by_id(api, vid)
            if ver and str(ver.get("id") or "") == vid:
                out[vid] = ver
        return out
    if not isinstance(data, list):
        return out
    for ver in data:
        if isinstance(ver, dict):
            vid = str(ver.get("id") or "").strip()
            if vid:
                out[vid] = ver
    return out


def load_all_project_versions(api: str, slug: str, expected_ids: list[str]) -> tuple[dict[str, dict], list[str]]:
    """
    Return (id -> version, warnings for ids that could not be loaded).
    Completeness is defined by expected_ids (from project.versions).
    """
    by_id: dict[str, dict] = {}
    warnings: list[str] = []

    bulk = http_json_or_exit(f"{api}/project/{slug}/version")
    if isinstance(bulk, list):
        for ver in bulk:
            if isinstance(ver, dict):
                vid = str(ver.get("id") or "").strip()
                if vid:
                    by_id[vid] = ver
    print(f"Bulk list returned {len(by_id)} version object(s)")

    missing = [vid for vid in expected_ids if vid not in by_id]
    if missing:
        print(f"Gap-fill required for {len(missing)} id(s)")
        for i in range(0, len(missing), VERSION_IDS_BATCH):
            chunk = missing[i : i + VERSION_IDS_BATCH]
            got = fetch_versions_by_ids(api, chunk)
            by_id.update(got)
            still = [vid for vid in chunk if vid not in by_id]
            for vid in still:
                ver = fetch_version_by_id(api, vid)
                if ver is not None:
                    rid = str(ver.get("id") or "").strip() or vid
                    by_id[rid] = ver
                else:
                    warnings.append(
                        f"skip version id={vid}: could not load version object after retries"
                    )

    return by_id, warnings


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


def cmd_prepare() -> None:
    api = env("MODRINTH_API", "https://api.modrinth.com/v2").rstrip("/")
    slug = env("PROJECT_SLUG")
    project_type = env("PROJECT_TYPE", "resourcepack").strip() or "resourcepack"
    changelog_path = Path(env("CHANGELOG_FILE", "CHANGELOG.md"))
    out_dir = Path(env("OUT_DIR"))
    force = force_enabled()

    out_dir.mkdir(parents=True, exist_ok=True)
    zips_dir = out_dir / "zips"
    zips_dir.mkdir(parents=True, exist_ok=True)

    if not changelog_path.is_file():
        raise SystemExit(f"CHANGELOG not found: {changelog_path}")
    changelog_text = changelog_path.read_text(encoding="utf-8")

    # Defence in depth (workflow gate should already have enforced this)
    if has_entry_0(changelog_text) and not force:
        raise SystemExit(
            "CHANGELOG already contains ## [0]. Re-run with force=true to replace."
        )

    excluded = extract_modrinth_ids_from_changelog(changelog_text)
    print(f"Excluded Modrinth IDs from existing entries (n>=1): {len(excluded)}")

    project = http_json_or_exit(f"{api}/project/{slug}")
    if not isinstance(project, dict):
        raise SystemExit(f"Project not found: {slug}")
    project_id = str(project.get("id") or "").strip()
    project_title = str(project.get("title") or slug).strip()
    expected_raw = project.get("versions") or []
    if not isinstance(expected_raw, list):
        raise SystemExit("project.versions is not a list")
    expected_ids = [str(x).strip() for x in expected_raw if str(x).strip()]
    print(
        f"Project {slug!r} id={project_id} title={project_title!r} "
        f"expected_versions={len(expected_ids)}"
    )

    by_id, load_warnings = load_all_project_versions(api, slug, expected_ids)
    warnings: list[str] = list(load_warnings)

    # Import candidates: expected ids not already in changelog n>=1
    candidate_ids = [vid for vid in expected_ids if vid not in excluded]
    print(
        f"Import candidates after exclusion: {len(candidate_ids)} "
        f"(skipped already-recorded: {len(expected_ids) - len(candidate_ids)})"
    )

    # Only warn about load failures for in-scope ids
    scoped_load_warnings = [
        w for w in load_warnings
        if any(vid in w for vid in candidate_ids)
    ]
    # Re-build warnings: keep load warnings only for candidates; drop noise for excluded
    warnings = []
    for vid in candidate_ids:
        if vid not in by_id:
            warnings.append(
                f"skip version id={vid}: could not load version object after retries"
            )

    prefix = derived_zip_prefix(slug)
    entries: list[dict] = []
    used_names: set[str] = set()

    # Process candidates in newest-first order when date is known
    ordered = []
    for vid in candidate_ids:
        ver = by_id.get(vid)
        if ver is None:
            continue
        ordered.append(ver)
    ordered.sort(key=lambda v: v.get("date_published") or "", reverse=True)

    for ver in ordered:
        vid = str(ver.get("id") or "").strip()
        vnum = str(ver.get("version_number") or "").strip()
        if not vid:
            warnings.append(f"skip version with empty id (number={vnum!r})")
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

        if not vnum:
            warnings.append(f"skip id={vid}: empty version_number")
            continue

        file_name = f"{prefix}-v{vnum}.zip"
        if file_name in used_names:
            warnings.append(
                f"skip {vnum} (id={vid}): zip name {file_name!r} already used "
                f"by another version in this import"
            )
            continue

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

        used_names.add(file_name)
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

    imported_date = datetime.now(timezone.utc).date().isoformat()
    meta = {
        "project_slug": slug,
        "project_id": project_id,
        "project_title": project_title,
        "imported_date": imported_date if entries else "",
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
        f"imported_date={imported_date if entries else '(none)'}",
        f"entries={len(entries)}",
        f"warnings={len(warnings)}",
        "",
        "Versions:",
    ]
    for e in entries:
        summary_lines.append(
            f"  {e['version_number']}  {e['modrinth_id']}  {e['file']}"
        )
    if warnings:
        summary_lines.append("")
        summary_lines.append("Warnings:")
        for w in warnings:
            summary_lines.append(f"  {w}")
    summary = "\n".join(summary_lines) + "\n"
    (out_dir / "prepare_summary.txt").write_text(summary, encoding="utf-8")
    print(summary)
    _print_warnings(warnings)

    _gh_out("imported_date", imported_date if entries else "")
    _gh_out("entry_count", str(len(entries)))
    _gh_out(
        "version_numbers_json",
        json.dumps([e["version_number"] for e in entries]),
    )


def cmd_commit() -> None:
    out_dir = Path(env("OUT_DIR"))
    changelog_path = Path(env("CHANGELOG_FILE", "CHANGELOG.md"))
    changelog_url = env("CHANGELOG_URL")
    github_repository = env("GITHUB_REPOSITORY")
    project_type = env("PROJECT_TYPE", "resourcepack").strip() or "resourcepack"
    force = force_enabled()

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
