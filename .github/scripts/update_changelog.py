#!/usr/bin/env python3
"""
CHANGELOG.md helpers (Keep a Changelog Unreleased + sequential publish sections).

Used by:
  - build_artifact.py       → extract_unreleased()
  - publish_to_modrinth.py  → next_sequence(), format_modrinth_changelog()
  - workflow job changelog  → promote (rewrite file only; git is in the workflow)

GitHub promote section shape:
  ## [n] - YYYY-MM-DD

  - [[1.2.3](modrinth-url), [1.2.4](modrinth-url)]

  <body>

Modrinth changelog shape (no versions list):
  <body>

  [Full project changelog](url) · entry n · YYYY-MM-DD
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

# Default footer link label (override with CHANGELOG_FOOTER_LABEL env)
DEFAULT_FOOTER_LABEL = "Full project changelog"


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
    """Max ## [n] in the file + 1; starts at 1 if none exist."""
    nums = [int(m) for m in SEQUENCE_RE.findall(changelog_text)]
    return (max(nums) if nums else 0) + 1


def format_modrinth_changelog(
    body: str,
    entry_n: int,
    day: str,
    changelog_url: str,
    *,
    footer_label: str | None = None,
) -> str:
    """
    Body + footer for Modrinth version pages.

    Footer shape:
      [Full project changelog](url) · entry n · YYYY-MM-DD
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

    footer = f"[{label}]({changelog_url.strip()}) · entry {entry_n} · {day.strip()}"
    return body + "\n\n" + footer


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
) -> int:
    """
    Clear Unreleased and insert ## [seq] - day with versions bullet + body.

    version_items: markdown link segments or plain version numbers.
    seq must match the entry number used on Modrinth for this publish.
    """
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

    versions_line = "- [" + ", ".join(version_items) + "]"
    section_lines = [
        f"## [{seq}] - {day}",
        "",
        versions_line,
        "",
    ]
    section_lines.extend(body.splitlines())
    while section_lines and section_lines[-1] == "":
        section_lines.pop()
    section_lines.append("")

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
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    body = meta.get("changelog_body")
    if not isinstance(body, str) or not body.strip():
        raise SystemExit(f"metadata.changelog_body missing or empty in {meta_path}")
    return body.strip()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Update CHANGELOG.md after publish")
    parser.add_argument(
        "command",
        choices=["promote"],
        help="promote: rewrite CHANGELOG.md from artifact metadata + publish outputs",
    )
    args = parser.parse_args(argv)

    if args.command != "promote":
        raise SystemExit(f"unknown command: {args.command}")

    changelog_path = Path(os.environ.get("CHANGELOG_FILE", "CHANGELOG.md"))
    dist_dir = Path(os.environ.get("DIST_DIR", "dist"))
    meta_path = Path(os.environ.get("METADATA_JSON", str(dist_dir / "metadata.json")))

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

    body = _body_from_metadata(meta_path)

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
    project_type = os.environ.get("PROJECT_TYPE", "resourcepack").strip() or "resourcepack"
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
        if not isinstance(ver, str) or not ver.strip():
            raise SystemExit("version_number missing in versions_json item")
        ver = ver.strip()
        if isinstance(vid, str) and vid.strip() and vid.strip() != "?":
            url = f"https://modrinth.com/{project_type}/{slug}/version/{vid.strip()}"
            version_items.append(f"[{ver}]({url})")
        else:
            version_items.append(ver)

    used = promote_changelog(changelog_path, body, version_items, day, seq)
    print(f"Promoted CHANGELOG.md section ## [{used}] - {day}")
    print(f"Versions: {version_items}")


if __name__ == "__main__":
    main()
