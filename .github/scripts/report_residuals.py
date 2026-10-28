#!/usr/bin/env python3
"""
Print factual inventory after publish-chain or import-commit success/failure.

Env (shared):
  RESIDUAL_STAGE   - publish | changelog | release | import
  RESIDUAL_OUTCOME - failure | success

Publish-chain env:
  CHANGELOG_VERSIONS_JSON - [{version_number, id, key}, ...]
  CHANGELOG_ENTRY  CHANGELOG_ENTRY  - sequence n
  CHANGELOG_DATE   - YYYY-MM-DD
  PROJECT_SLUG     - Modrinth slug
  PROJECT_TYPE     - default resourcepack
  RELEASE_TAG      - optional; tag name when known to exist
  RELEASE_URL      - optional; release HTML URL when known to exist
  
Import env:
  IMPORT_VERSIONS_JSON - ["1.2.3", ...] plain version numbers (success)
  IMPORT_DURABLE_JSON  - ["CHANGELOG.md ## [0] ...", "tag ...", ...] (failure)
  CHANGELOG_DATE, RELEASE_TAG, RELEASE_URL (optional)
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

    # publish or changelog failure: Modrinth list only
    lines.append("Modrinth versions created in this run:")
    if ver_block:
        lines.extend(ver_block)
    else:
        lines.append("  (none)")
    lines.append("")
    return "\n".join(lines)


def format_import_residuals(
    *,
    outcome: str,
    version_numbers: list[str] | None = None,
    durable_actions: list[str] | None = None,
    day: str | None = None,
    release_tag: str | None = None,
    release_url: str | None = None,
) -> str:
    """
    Import commit residuals.

    success: plain version numbers + GitHub pointers
    failure: only durable actions that already occurred and need revert
    """
    outcome = (outcome or "failure").strip().lower()
    if outcome not in ("failure", "success"):
        outcome = "failure"

    lines: list[str] = []
    lines.append("")
    if outcome == "success":
        lines.append("SUCCESS")
        lines.append("")
        lines.append("Imported versions")
        nums = version_numbers or []
        if nums:
            for v in nums:
                lines.append(f"  {v}")
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("GitHub")
        if day:
            lines.append(f"  CHANGELOG.md ## [0] - {day}")
        if release_tag:
            lines.append(f"  tag {release_tag}")
        if release_url:
            lines.append(f"  {release_url}")
        lines.append("")
        return "\n".join(lines)

    lines.append("FAILURE - import")
    lines.append("")
    lines.append("Durable changes that may need revert before re-run:")
    actions = durable_actions or []
    if actions:
        for a in actions:
            lines.append(f"  {a}")
    else:
        lines.append("  (none — no durable writes completed)")
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


def _json_list_env(name: str) -> list[Any]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"Warning: {name} is not valid JSON; using empty", file=sys.stderr)
        return []
    if not isinstance(data, list):
        return []
    return data


def main(argv: list[str] | None = None) -> None:
    stage = os.environ.get("RESIDUAL_STAGE", "unknown").strip() or "unknown"
    outcome = os.environ.get("RESIDUAL_OUTCOME", "failure").strip().lower() or "failure"
    if outcome not in ("failure", "success"):
        outcome = "failure"

    if stage == "import":
        version_numbers = [
            str(x).strip()
            for x in _json_list_env("IMPORT_VERSIONS_JSON")
            if str(x).strip()
        ]
        durable = [
            str(x).strip()
            for x in _json_list_env("IMPORT_DURABLE_JSON")
            if str(x).strip()
        ]
        text = format_import_residuals(
            outcome=outcome,
            version_numbers=version_numbers,
            durable_actions=durable,
            day=os.environ.get("CHANGELOG_DATE", "").strip() or None,
            release_tag=os.environ.get("RELEASE_TAG", "").strip() or None,
            release_url=os.environ.get("RELEASE_URL", "").strip() or None,
        )
        emit_residuals(text, outcome=outcome)
        return

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
