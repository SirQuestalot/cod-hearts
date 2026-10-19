#!/usr/bin/env python3
"""
CHANGELOG.md helpers for Keep a Changelog (Unreleased + sequential publish sections).

Used by:
  - build_artifact.py  → extract_unreleased()
  - workflow job "changelog" → promote via __main__ (rewrite file only; git is in the workflow)

Promote section shape:
  ## [n] - YYYY-MM-DD

  - [1.2.3, 1.2.4]

  <body>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

UNRELEASED_RE = re.compile(r"^##\s+\[Unreleased\]\s*$", re.IGNORECASE)
# Version-style heading: ## [anything] (KaC)
HEADING_RE = re.compile(r"^##\s+\[.+\]")
# Sequential publish sections only: ## [123]
SEQUENCE_RE = re.compile(r"^##\s+\[(\d+)\]", re.MULTILINE)


def extract_unreleased(changelog_path: Path, *, require_nonempty: bool = True) -> str:
    """
    Return the body under ## [Unreleased].

    Body runs until the next ## [ ...] heading or end of file.
    """
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


def _split_changelog(lines: list[str]) -> tuple[list[str], int, int]:
    """
    Return (all lines, unreleased_heading_index, index_after_unreleased_body).

    index_after_unreleased_body is the first line of the rest of the file
    (next ## [ heading) or len(lines).
    """
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
    versions: list[str],
    day: str,
) -> int:
    """
    Clear Unreleased and insert ## [n] - day with versions bullet + body.
    Returns the sequence number used.
    """
    body = body.strip()
    if not body:
        raise SystemExit("promote: changelog body is empty")
    if not versions:
        raise SystemExit("promote: versions list is empty")

    text = changelog_path.read_text(encoding="utf-8")
    seq = next_sequence(text)
    lines = text.splitlines()
    lines, urel_i, after_i = _split_changelog(lines)

    preamble = lines[:urel_i]  # up to but not including ## [Unreleased]
    rest = lines[after_i:]  # older history

    versions_line = "- [" + ", ".join(versions) + "]"
    section_lines = [
        f"## [{seq}] - {day}",
        "",
        versions_line,
        "",
    ]
    section_lines.extend(body.splitlines())
    # Ensure trailing blank line before older history when rest is non-empty
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


def _versions_from_metadata(meta_path: Path) -> tuple[str, list[str]]:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    body = meta.get("changelog_body")
    if not isinstance(body, str) or not body.strip():
        raise SystemExit(f"metadata.changelog_body missing or empty in {meta_path}")
    entries = meta.get("entries") or []
    if not entries:
        raise SystemExit(f"metadata.entries empty in {meta_path}")
    versions: list[str] = []
    for e in entries:
        v = e.get("version_number")
        if not isinstance(v, str) or not v.strip():
            raise SystemExit("entry missing version_number")
        versions.append(v.strip())
    return body.strip(), versions


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Update CHANGELOG.md after publish")
    parser.add_argument(
        "command",
        choices=["promote"],
        help="promote: rewrite CHANGELOG.md from artifact metadata",
    )
    args = parser.parse_args(argv)

    if args.command != "promote":
        raise SystemExit(f"unknown command: {args.command}")

    changelog_path = Path(os.environ.get("CHANGELOG_FILE", "CHANGELOG.md"))
    dist_dir = Path(os.environ.get("DIST_DIR", "dist"))
    meta_path = Path(os.environ.get("METADATA_JSON", str(dist_dir / "metadata.json")))
    day = os.environ.get("CHANGELOG_DATE", "").strip()
    if not day:
        day = datetime.now(timezone.utc).date().isoformat()

    body, versions = _versions_from_metadata(meta_path)
    seq = promote_changelog(changelog_path, body, versions, day)
    print(f"Promoted CHANGELOG.md section ## [{seq}] - {day}")
    print(f"Versions: {versions}")


if __name__ == "__main__":
    main()
