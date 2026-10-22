#!/usr/bin/env python3
"""
Print factual inventory after publish-chain success or failure.

Env:
  RESIDUAL_STAGE   - publish | changelog | release
  RESIDUAL_OUTCOME - failure | success
  CHANGELOG_VERSIONS_JSON - [{version_number, id, key}, ...]
  CHANGELOG_ENTRY  - sequence n
  CHANGELOG_DATE   - YYYY-MM-DD
  PROJECT_SLUG     - Modrinth slug
  PROJECT_TYPE     - default resourcepack
  RELEASE_TAG      - optional; tag name when known to exist
  RELEASE_URL      - optional; release HTML URL when known to exist
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _modrinth_version_url(project_type: str, slug: str, version_id: str) -> str:
    return f"https://modrinth.com/{project_type}/{slug}/version/{version_id}"


def _version_lines(
    versions: list[dict[str, Any]],
    *,
    slug: str,
    project_type: str,
) -> list[str]:
    lines: list[str] = []
    for item in versions:
        ver = str(item.get("version_number") or "").strip() or "?"
        vid = str(item.get("id") or "").strip()
        key = str(item.get("key") or "").strip()
        bits: list[str] = []
        if key:
            bits.append(key)
        bits.append(ver)
        if vid and vid != "?" and slug:
            bits.append(_modrinth_version_url(project_type, slug, vid))
        lines.append("  " + "  ".join(bits))
    return lines


def format_residuals(
    *,
    stage: str,
    versions: list[dict[str, Any]],
    entry_n: str | int | None = None,
    day: str | None = None,
    slug: str = "",
    project_type: str = "resourcepack",
    outcome: str = "failure",
    release_tag: str | None = None,
    release_url: str | None = None,
) -> str:
    outcome = (outcome or "failure").strip().lower()
    if outcome not in ("failure", "success"):
        outcome = "failure"
    stage = (stage or "unknown").strip() or "unknown"
    project_type = project_type.strip() or "resourcepack"

    lines: list[str] = []
    lines.append("")
    if outcome == "success":
        lines.append("SUCCESS")
    else:
        lines.append(f"FAILURE - {stage}")
    lines.append("")

    ver_block = _version_lines(versions, slug=slug, project_type=project_type)

    if outcome == "success":
        lines.append("Modrinth")
        if ver_block:
            lines.extend(ver_block)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("GitHub")
        if entry_n not in (None, "") and day:
            lines.append(f"  CHANGELOG.md ## [{entry_n}] - {day}")
        if release_tag:
            lines.append(f"  tag {release_tag}")
        if release_url:
            lines.append(f"  {release_url}")
        lines.append("")
        return "\n".join(lines)

    if stage == "release":
        lines.append("Modrinth versions created in this run:")
        if ver_block:
            lines.extend(ver_block)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("GitHub")
        if entry_n not in (None, "") and day:
            lines.append(f"  CHANGELOG.md ## [{entry_n}] - {day}")
        if release_tag:
            lines.append(f"  tag {release_tag}")
        if release_url:
            lines.append(f"  {release_url}")
        lines.append("")
        return "\n".join(lines)

    # publish or changelog failure: Modrinth list only
    lines.append("Modrinth versions created in this run:")
    if ver_block:
        lines.extend(ver_block)
    else:
        lines.append("  (none)")
    lines.append("")
    return "\n".join(lines)


def emit_residuals(text: str, *, outcome: str = "failure") -> None:
    print(text, end="" if text.endswith("\n") else "\n")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        title = "SUCCESS" if outcome == "success" else "FAILURE"
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n### {title}\n\n```\n")
            fh.write(text.strip() + "\n")
            fh.write("```\n")
    except OSError as e:
        print(f"(Could not write GITHUB_STEP_SUMMARY: {e})", file=sys.stderr)


def versions_from_env() -> list[dict[str, Any]]:
    raw = os.environ.get("CHANGELOG_VERSIONS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(
            "Warning: CHANGELOG_VERSIONS_JSON is not valid JSON; listing empty",
            file=sys.stderr,
        )
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def main(argv: list[str] | None = None) -> None:
    stage = os.environ.get("RESIDUAL_STAGE", "unknown").strip() or "unknown"
    outcome = os.environ.get("RESIDUAL_OUTCOME", "failure").strip().lower() or "failure"
    if outcome not in ("failure", "success"):
        outcome = "failure"
    text = format_residuals(
        stage=stage,
        versions=versions_from_env(),
        entry_n=os.environ.get("CHANGELOG_ENTRY", "").strip() or None,
        day=os.environ.get("CHANGELOG_DATE", "").strip() or None,
        slug=os.environ.get("PROJECT_SLUG", "").strip(),
        project_type=os.environ.get("PROJECT_TYPE", "resourcepack").strip()
        or "resourcepack",
        outcome=outcome,
        release_tag=os.environ.get("RELEASE_TAG", "").strip() or None,
        release_url=os.environ.get("RELEASE_URL", "").strip() or None,
    )
    emit_residuals(text, outcome=outcome)


if __name__ == "__main__":
    main()
