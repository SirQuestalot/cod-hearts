#!/usr/bin/env python3
"""
CHANGELOG.md helpers (Keep a Changelog Unreleased + sequential publish sections).

Used by:
  - build_artifact.py       → extract_unreleased()
  - publish_to_modrinth.py  → next_sequence(), format_modrinth_changelog()
  - workflow job changelog  → promote (rewrite file only; git is in the workflow)
  - workflow job release    → release-notes (stdout body for GitHub Release)
  - import_modrinth_history → entry-0 section helpers, ID extraction

Version-list item shape:
  **label**&ensp;[version](url)<!--modrinthid:ID-->

Items joined with " - ".

CHANGELOG section footer:
  [GitHub Release](url) · changelog{n}-YYYY-MM-DD

Release notes footer:
  [Full project changelog](url) · entry n · YYYY-MM-DD

Modrinth version changelog footer (both lines):
  [Full project changelog](url) · entry n · YYYY-MM-DD
  [GitHub Release](url) · changelog{n}-YYYY-MM-DD
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

UNRELEASED_RE = re.compile(r"^##\s+\[Unreleased\]\s*$", re.IGNORECASE)
HEADING_RE = re.compile(r"^##\s+\[.+\]")
SEQUENCE_RE = re.compile(r"^##\s+\[(\d+)\]", re.MULTILINE)
MODRINTH_ID_COMMENT_RE = re.compile(r"<!--modrinthid:([^>]+)-->")
ENTRY_0_RE = re.compile(r"^##\s+\[0\]\s*-\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)

DEFAULT_FOOTER_LABEL = "Full project changelog"
ENSP = "\u2002"  # en-space


def extract_unreleased(changelog_path: Path, *, require_nonempty: bool = True) -> str:
    """Return the body under ## [Unreleased] until the next ## [ ...] heading."""
    if not changelog_path.is_file():
        raise SystemExit(f"CHANGELOG file not found: {changelog_path}")

    lines = changelog_path.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if UNRELEASED_RE.match(line):
            start = i + 1
            break

    if start is None:
        raise SystemExit(f"No ## [Unreleased] section found in {changelog_path}")

    body_lines: list[str] = []
    for line in lines[start:]:
        if HEADING_RE.match(line) and not UNRELEASED_RE.match(line):
            break
        body_lines.append(line)

    body = "\n".join(body_lines).strip()
    if require_nonempty and not body:
        raise SystemExit(
            f"## [Unreleased] in {changelog_path} is empty — "
            "add change notes before building"
        )
    return body


def next_sequence(changelog_text: str) -> int:
    """Max ## [n] in the file + 1; starts at 1 if none exist. Ignores [0]."""
    nums = [int(m) for m in SEQUENCE_RE.findall(changelog_text)]
    positive = [n for n in nums if n >= 1]
    return (max(positive) if positive else 0) + 1


def release_tag(entry_n: int, day: str) -> str:
    return f"changelog{entry_n}-{day.strip()}"


def release_url(github_repository: str, entry_n: int, day: str) -> str:
    """Deterministic release URL (P1) — valid once the release job creates the tag."""
    repo = github_repository.strip()
    if not repo:
        raise SystemExit("github_repository is empty")
    return f"https://github.com/{repo}/releases/tag/{release_tag(entry_n, day)}"


def format_version_item(
    *,
    version_number: str,
    url: str | None = None,
    label: str | None = None,
    modrinth_id: str | None = None,
) -> str:
    """
    Single version-list item:
      **label**&ensp;[ver](url)<!--modrinthid:ID-->
    Label omitted when empty. ID comment omitted when id missing.
    """
    ver = version_number.strip()
    if not ver:
        raise SystemExit("format_version_item: version_number is empty")

    if url and url.strip():
        core = f"[{ver}]({url.strip()})"
    else:
        core = ver

    mid = (modrinth_id or "").strip()
    if mid:
        core = f"{core}<!--modrinthid:{mid}-->"

    lab = (label or "").strip()
    if lab:
        return f"**{lab}**{ENSP}{core}"
    return core


def format_version_list_line(items: list[str]) -> str:
    """Join pre-formatted items with ' - '."""
    if not items:
        raise SystemExit("format_version_list_line: items is empty")
    return " - ".join(items)


def format_modrinth_changelog(
    body: str,
    entry_n: int,
    day: str,
    changelog_url: str,
    *,
    footer_label: str | None = None,
    github_repository: str | None = None,
    release_url_override: str | None = None,
) -> str:
    """
    Body + dual footer for Modrinth version pages (n >= 1).

    Footer:
      [Full project changelog](url) · entry n · YYYY-MM-DD
      [GitHub Release](url) · changelog{n}-YYYY-MM-DD
    """
    body = body.strip()
    if not body:
        raise SystemExit("format_modrinth_changelog: body is empty")
    if entry_n < 1:
        raise SystemExit(f"format_modrinth_changelog: invalid entry_n {entry_n}")
    if not day.strip():
        raise SystemExit("format_modrinth_changelog: day is empty")
    if not changelog_url.strip():
        raise SystemExit("format_modrinth_changelog: changelog_url is empty")

    label = (footer_label if footer_label is not None else "").strip()
    if not label:
        label = os.environ.get("CHANGELOG_FOOTER_LABEL", DEFAULT_FOOTER_LABEL).strip()
        if not label:
            label = DEFAULT_FOOTER_LABEL

    day_s = day.strip()
    line1 = f"[{label}]({changelog_url.strip()}) · entry {entry_n} · {day_s}"

    rel = (release_url_override or "").strip()
    if not rel:
        repo = (github_repository or os.environ.get("GITHUB_REPOSITORY", "")).strip()
        if not repo:
            raise SystemExit(
                "format_modrinth_changelog: github_repository or GITHUB_REPOSITORY "
                "required to build GitHub Release footer"
            )
        rel = release_url(repo, entry_n, day_s)
    line2 = f"[GitHub Release]({rel}) · {release_tag(entry_n, day_s)}"

    return body + "\n\n" + line1 + "\n" + line2


def format_github_section(
    body: str,
    version_items: list[str],
    day: str,
    seq: int,
    *,
    include_header: bool = True,
    github_repository: str | None = None,
    changelog_url: str | None = None,
    release_url_override: str | None = None,
) -> str:
    """
    GitHub changelog section or release-notes body.

    include_header=True (CHANGELOG.md promote):
      ## [n] - YYYY-MM-DD

      <version list>

      <body>

      [GitHub Release](url) · changelog{n}-YYYY-MM-DD

    include_header=False (Release notes):
      <version list>

      <body>

      [Full project changelog](url) · entry n · YYYY-MM-DD
    """
    body = body.strip()
    if not body:
        raise SystemExit("format_github_section: body is empty")
    if not version_items:
        raise SystemExit("format_github_section: versions list is empty")
    if seq < 1:
        raise SystemExit(f"format_github_section: invalid sequence number {seq}")
    if not day.strip():
        raise SystemExit("format_github_section: day is empty")

    day_s = day.strip()
    versions_line = format_version_list_line(version_items)
    parts: list[str] = []
    if include_header:
        parts.append(f"## [{seq}] - {day_s}")
        parts.append("")
    parts.append(versions_line)
    parts.append("")
    parts.extend(body.splitlines())
    parts.append("")

    if include_header:
        rel = (release_url_override or "").strip()
        if not rel:
            repo = (github_repository or os.environ.get("GITHUB_REPOSITORY", "")).strip()
            if not repo:
                raise SystemExit(
                    "format_github_section: github_repository or GITHUB_REPOSITORY "
                    "required for CHANGELOG release footer"
                )
            rel = release_url(repo, seq, day_s)
        parts.append(f"[GitHub Release]({rel}) · {release_tag(seq, day_s)}")
    else:
        cl_url = (changelog_url or os.environ.get("CHANGELOG_URL", "")).strip()
        if not cl_url:
            raise SystemExit(
                "format_github_section: changelog_url or CHANGELOG_URL required "
                "for release-notes footer"
            )
        label = os.environ.get("CHANGELOG_FOOTER_LABEL", DEFAULT_FOOTER_LABEL).strip()
        if not label:
            label = DEFAULT_FOOTER_LABEL
        parts.append(f"[{label}]({cl_url}) · entry {seq} · {day_s}")

    out = "\n".join(parts)
    if not out.endswith("\n"):
        out += "\n"
    return out


def extract_modrinth_ids_from_changelog(changelog_text: str) -> set[str]:
    """All modrinthid comment values under ## [n] sections with n >= 1."""
    ids: set[str] = set()
    lines = changelog_text.splitlines()
    in_numbered = False
    for line in lines:
        m = re.match(r"^##\s+\[(\d+)\]", line)
        if m:
            in_numbered = int(m.group(1)) >= 1
            continue
        if re.match(r"^##\s+", line):
            in_numbered = False
            continue
        if in_numbered:
            for mid in MODRINTH_ID_COMMENT_RE.findall(line):
                mid = mid.strip()
                if mid:
                    ids.add(mid)
    return ids


def has_entry_0(changelog_text: str) -> bool:
    return ENTRY_0_RE.search(changelog_text) is not None


def format_entry0_version_lines(
    entries: list[dict],
    *,
    project_title: str,
    project_type: str,
    slug: str,
) -> str:
    """
    Form C: day-grouped, newest day first, newest-within-day first.
    Label = Modrinth title with project title prefix stripped; omit if empty.
    """

    def parse_day(iso: str) -> str:
        s = (iso or "").strip()
        if not s:
            return "1970-01-01"
        return s[:10]

    def sort_key(e: dict) -> str:
        return (e.get("date_published") or "") + (e.get("version_number") or "")

    title_prefix = (project_title or "").strip()

    def label_from_title(title: str) -> str:
        t = (title or "").strip()
        if not t:
            return ""
        if title_prefix and t.lower().startswith(title_prefix.lower()):
            return t[len(title_prefix) :].strip()
        return t

    sorted_entries = sorted(entries, key=sort_key, reverse=True)
    by_day: dict[str, list[dict]] = {}
    day_order: list[str] = []
    for e in sorted_entries:
        d = parse_day(str(e.get("date_published") or ""))
        if d not in by_day:
            by_day[d] = []
            day_order.append(d)
        by_day[d].append(e)

    lines_out: list[str] = []
    for d in day_order:
        items: list[str] = []
        for e in by_day[d]:
            ver = str(e.get("version_number") or "").strip()
            mid = str(e.get("modrinth_id") or e.get("id") or "").strip()
            title = str(e.get("title") or "").strip()
            lab = label_from_title(title)
            url = None
            if mid and slug:
                url = f"https://modrinth.com/{project_type}/{slug}/version/{mid}"
            items.append(
                format_version_item(
                    version_number=ver or "?",
                    url=url,
                    label=lab or None,
                    modrinth_id=mid or None,
                )
            )
        lines_out.append(f"{d} {format_version_list_line(items)}")
    return "\n".join(lines_out)


def format_entry0_section(
    *,
    day: str,
    version_lines: str,
    release_url_str: str,
    tag: str,
) -> str:
    """Full ## [0] section text."""
    parts = [
        f"## [0] - {day.strip()}",
        "",
        version_lines.strip(),
        "",
        "### Added",
        "- Imported pre-automation versions from Modrinth.",
        "- Per-version (pre-KaC) changelogs from Modrinth are stored in "
        "`metadata.json` (GitHub Release asset).",
        "",
        f"[GitHub Release]({release_url_str}) · {tag}",
        "",
    ]
    return "\n".join(parts)


def format_entry0_release_notes(
    *,
    day: str,
    version_lines: str,
    changelog_url: str,
) -> str:
    parts = [
        version_lines.strip(),
        "",
        "### Added",
        "- Imported pre-automation versions from Modrinth.",
        "- Per-version (pre-KaC) changelogs from Modrinth are stored in "
        "`metadata.json` (GitHub Release asset).",
        "",
        f"[{DEFAULT_FOOTER_LABEL}]({changelog_url.strip()}) · entry 0 · {day.strip()}",
        "",
    ]
    return "\n".join(parts)


def append_entry0_to_changelog(changelog_path: Path, section_text: str) -> None:
    """Append ## [0] at end of file (below all other entries). Replace if present."""
    text = changelog_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if re.match(r"^##\s+\[0\]", lines[i]):
            i += 1
            while i < len(lines) and not re.match(r"^##\s+", lines[i]):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    body = "\n".join(out).rstrip() + "\n\n" + section_text.strip() + "\n"
    changelog_path.write_text(body, encoding="utf-8")


def _split_changelog(lines: list[str]) -> tuple[list[str], int, int]:
    start = None
    for i, line in enumerate(lines):
        if UNRELEASED_RE.match(line):
            start = i
            break
    if start is None:
        raise SystemExit("No ## [Unreleased] section found")

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if HEADING_RE.match(lines[j]) and not UNRELEASED_RE.match(lines[j]):
            end = j
            break
    return lines, start, end


def promote_changelog(
    changelog_path: Path,
    body: str,
    version_items: list[str],
    day: str,
    seq: int,
    *,
    github_repository: str | None = None,
) -> int:
    """Clear Unreleased and insert ## [seq] - day with versions + body + release footer."""
    body = body.strip()
    if not body:
        raise SystemExit("promote: changelog body is empty")
    if not version_items:
        raise SystemExit("promote: versions list is empty")
    if seq < 1:
        raise SystemExit(f"promote: invalid sequence number {seq}")

    lines = changelog_path.read_text(encoding="utf-8").splitlines()
    lines, urel_i, after_i = _split_changelog(lines)

    preamble = lines[:urel_i]
    rest = lines[after_i:]

    section_lines = format_github_section(
        body,
        version_items,
        day,
        seq,
        include_header=True,
        github_repository=github_repository,
    ).splitlines()

    out_lines: list[str] = []
    out_lines.extend(preamble)
    if out_lines and out_lines[-1].strip() != "":
        out_lines.append("")
    out_lines.append("## [Unreleased]")
    out_lines.append("")
    out_lines.extend(section_lines)
    if rest:
        if out_lines and out_lines[-1] != "":
            out_lines.append("")
        out_lines.extend(rest)

    new_text = "\n".join(out_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    changelog_path.write_text(new_text, encoding="utf-8")
    return seq


def _body_from_metadata(meta_path: Path) -> str:
    if not meta_path.is_file():
        raise SystemExit(f"metadata.json not found: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    body = meta.get("changelog_body")
    if not isinstance(body, str) or not body.strip():
        raise SystemExit("metadata.changelog_body is missing or empty")
    return body.strip()


def _version_items_from_env(meta_path: Path) -> list[str]:
    versions_raw = os.environ.get("CHANGELOG_VERSIONS_JSON", "").strip()
    if not versions_raw:
        raise SystemExit("CHANGELOG_VERSIONS_JSON is required")
    try:
        versions_data = json.loads(versions_raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"CHANGELOG_VERSIONS_JSON is not valid JSON: {e}") from e
    if not isinstance(versions_data, list) or not versions_data:
        raise SystemExit("CHANGELOG_VERSIONS_JSON must be a non-empty array")

    slug = os.environ.get("PROJECT_SLUG", "").strip()
    project_type = (
        os.environ.get("PROJECT_TYPE", "resourcepack").strip() or "resourcepack"
    )
    if not slug:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        slug = (meta.get("project_slug") or "").strip()
    if not slug:
        raise SystemExit("PROJECT_SLUG missing (needed for Modrinth version links)")

    version_items: list[str] = []
    for item in versions_data:
        if not isinstance(item, dict):
            raise SystemExit("each versions_json item must be an object")
        ver = item.get("version_number")
        vid = item.get("id")
        key = item.get("key")
        if not isinstance(ver, str) or not ver.strip():
            raise SystemExit("version_number missing in versions_json item")
        ver = ver.strip()
        key_s = key.strip() if isinstance(key, str) else ""
        mid = vid.strip() if isinstance(vid, str) else ""
        url = None
        if mid and mid != "?":
            url = f"https://modrinth.com/{project_type}/{slug}/version/{mid}"
        version_items.append(
            format_version_item(
                version_number=ver,
                url=url,
                label=key_s or None,
                modrinth_id=mid if mid and mid != "?" else None,
            )
        )
    return version_items


def _entry_and_day() -> tuple[int, str]:
    day = os.environ.get("CHANGELOG_DATE", "").strip()
    if not day:
        raise SystemExit("CHANGELOG_DATE is required (UTC YYYY-MM-DD from publish job)")
    entry_raw = os.environ.get("CHANGELOG_ENTRY", "").strip()
    if not entry_raw:
        raise SystemExit("CHANGELOG_ENTRY is required (sequence n from publish job)")
    try:
        seq = int(entry_raw)
    except ValueError as e:
        raise SystemExit(f"CHANGELOG_ENTRY must be an integer, got {entry_raw!r}") from e
    return seq, day


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="CHANGELOG.md / release notes helpers")
    parser.add_argument(
        "command",
        choices=["promote", "release-notes"],
        help=(
            "promote: rewrite CHANGELOG.md; "
            "release-notes: print GitHub section body to stdout"
        ),
    )
    args = parser.parse_args(argv)

    dist_dir = Path(os.environ.get("DIST_DIR", "dist"))
    meta_path = Path(os.environ.get("METADATA_JSON", str(dist_dir / "metadata.json")))
    seq, day = _entry_and_day()
    body = _body_from_metadata(meta_path)
    version_items = _version_items_from_env(meta_path)
    github_repository = os.environ.get("GITHUB_REPOSITORY", "").strip() or None
    changelog_url = os.environ.get("CHANGELOG_URL", "").strip() or None

    if args.command == "release-notes":
        text_out = format_github_section(
            body,
            version_items,
            day,
            seq,
            include_header=False,
            changelog_url=changelog_url,
        )
        print(text_out, end="" if text_out.endswith("\n") else "\n")
        return

    if args.command == "promote":
        changelog_path = Path(os.environ.get("CHANGELOG_FILE", "CHANGELOG.md"))
        used = promote_changelog(
            changelog_path,
            body,
            version_items,
            day,
            seq,
            github_repository=github_repository,
        )
        print(f"Promoted CHANGELOG.md section ## [{used}] - {day}")
        print(f"Versions: {version_items}")
        return

    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
