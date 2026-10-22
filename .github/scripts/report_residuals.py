#!/usr/bin/env python3
"""
Print what this run created (for undo after tests or after a failure).

Used by:
  - publish_to_modrinth.py (partial failure; optional success list)
  - workflow jobs changelog / release (failure and success), via CLI

Env (CLI / workflow):
  RESIDUAL_STAGE          - publish | changelog | release | complete
  RESIDUAL_OUTCOME        - failure | success  (default: failure)
  CHANGELOG_VERSIONS_JSON - JSON array of {version_number, id, key}
  CHANGELOG_ENTRY         - sequence n (optional)
  CHANGELOG_DATE          - YYYY-MM-DD (optional)
  PROJECT_SLUG            - Modrinth slug
  PROJECT_TYPE            - default resourcepack
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _modrinth_version_url(project_type: str, slug: str, version_id: str) -> str:
    return f"https://modrinth.com/{project_type}/{slug}/version/{version_id}"


def format_residuals(
    *,
    stage: str,
    versions: list[dict[str, Any]],
    entry_n: str | int | None = None,
    day: str | None = None,
    slug: str = "",
    project_type: str = "resourcepack",
    outcome: str = "failure",
) -> str:
    """
    Multi-line residual report (no auto-delete).

    outcome=failure — error path; cleanup may be required to retry cleanly.
    outcome=success — run finished OK; same inventory for optional test undo.
    """
    outcome = (outcome or "failure").strip().lower()
    if outcome not in ("failure", "success"):
        outcome = "failure"

    lines: list[str] = []
    lines.append("")
    lines.append("=" * 60)
    if outcome == "success":
        lines.append("PIPELINE SUCCESS — RUN INVENTORY (optional undo guide)")
        lines.append("=" * 60)
        lines.append("Status: success (workflow did not report a failure at this point)")
    else:
        lines.append("PIPELINE FAILURE — MANUAL CLEANUP MAY BE REQUIRED")
        lines.append("=" * 60)
        lines.append("Status: failure")
    lines.append(f"Stage: {stage}")
    if entry_n not in (None, ""):
        lines.append(f"Changelog entry n: {entry_n}")
    if day:
        lines.append(f"Date: {day}")
    if slug:
        lines.append(
            f"Modrinth project: https://modrinth.com/{project_type}/{slug}"
        )
    lines.append("")

    if outcome == "success":
        if stage in ("complete", "release"):
            lines.append(
                "This run finished successfully (Modrinth publish, CHANGELOG.md, "
                "and GitHub Release as designed). Use the list below only if you "
                "were testing and want to undo what this run created."
            )
        elif stage == "publish":
            lines.append(
                "Modrinth version create(s) succeeded. Changelog and Release jobs "
                "may still be running or may run next. Inventory below is only the "
                "Modrinth side so far."
            )
        else:
            lines.append(
                "This stage completed successfully. Inventory below is what this "
                "run has recorded so far."
            )
    elif stage == "publish":
        lines.append(
            "One or more Modrinth versions were created in this run before the "
            "error. Later versions in the same run were not created. "
            "Changelog promote and GitHub Release did not run."
        )
    elif stage == "changelog":
        lines.append(
            "All Modrinth versions for this run were created successfully. "
            "Updating CHANGELOG.md failed (promote and/or commit/push). "
            "GitHub Release did not run."
        )
    elif stage == "release":
        lines.append(
            "Modrinth versions exist and CHANGELOG.md promote likely succeeded. "
            "Creating the GitHub Release (tag and/or assets) failed. "
            "Check whether tag/release partially exist on GitHub."
        )
    else:
        lines.append(f"Failure reported at stage {stage!r}.")

    lines.append("")
    if outcome == "success":
        lines.append(
            "Modrinth versions from this run "
            "(delete on Modrinth only if you intend to undo this run):"
        )
    else:
        lines.append(
            "Modrinth versions from this run "
            "(delete on Modrinth if you want a clean retry):"
        )
    if not versions:
        lines.append(
            "  (none recorded — if publish failed before the first create, "
            "nothing to delete on Modrinth)"
        )
    else:
        for item in versions:
            ver = str(item.get("version_number") or "").strip() or "?"
            vid = str(item.get("id") or "").strip()
            key = str(item.get("key") or "").strip()
            parts = [f"version_number={ver}"]
            if key:
                parts.append(f"key={key}")
            if vid and vid != "?":
                parts.append(f"id={vid}")
                if slug:
                    parts.append(_modrinth_version_url(project_type, slug, vid))
            lines.append("  - " + " | ".join(parts))

    lines.append("")
    if outcome == "success":
        lines.append("Optional undo checklist (only if you meant this as a test):")
    else:
        lines.append("Suggested manual checks:")
    lines.append("  1. Modrinth: remove or keep the versions listed above as you prefer.")
    if stage in ("changelog", "release", "complete") or outcome == "success":
        lines.append(
            "  2. GitHub CHANGELOG.md: if entry "
            f"## [{entry_n}] is on main, revert that commit or edit the file."
            if entry_n not in (None, "")
            else "  2. GitHub CHANGELOG.md: check main for a new ## [n] section from this run."
        )
    if stage in ("release", "complete") or (
        outcome == "success" and stage in ("release", "complete", "publish")
    ):
        tag_hint = ""
        if entry_n not in (None, "") and day:
            tag_hint = f" (tag changelog{entry_n}-{day})"
        if stage == "publish" and outcome == "success":
            lines.append(
                "  3. GitHub Release/tag: only after those jobs finish"
                f"{tag_hint}."
            )
        else:
            lines.append(
                f"  3. GitHub Releases/tags: remove the release/tag from this run"
                f"{tag_hint} if undoing."
            )
    lines.append(
        "  This workflow does not auto-delete Modrinth versions or force-revert git."
    )
    lines.append("=" * 60)
    lines.append("")
    return "\n".join(lines)


def emit_residuals(text: str, *, outcome: str = "failure") -> None:
    """Print to the job log and append to the GitHub Actions job summary when available."""
    print(text, end="" if text.endswith("\n") else "\n")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            title = (
                "Pipeline success — run inventory"
                if outcome == "success"
                else "Pipeline failure — residual state"
            )
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
    )
    emit_residuals(text, outcome=outcome)


if __name__ == "__main__":
    main()
