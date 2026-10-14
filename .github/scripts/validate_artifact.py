#!/usr/bin/env python3
"""
Local validation of a resource-pack build artifact directory.

Checks metadata.json shape and every referenced zip. Does not call Modrinth
and does not upload anything.

Used by:
  - build_artifact.py  (after writing OUT_DIR, before the workflow uploads)
  - publish_to_modrinth.py  (after downloading the artifact, before creates)

Returns:
  A list of human-readable problem strings. Empty list means the artifact
  passed the local checklist. Callers print the list and exit non-zero when
  the list is non-empty.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

SEMVER3 = re.compile(r"^\d+\.\d+\.\d+$")


def validate_artifact(dist_dir: str | Path) -> list[str]:
    """
    Run the full local checklist on dist_dir.

    Parameters:
      dist_dir: Directory that should contain metadata.json and the zip files
                named in metadata["entries"][*]["file"].

    Returns:
      list[str]: One message per problem found. Empty if everything looks sound.
    """
    errors: list[str] = []
    dist_dir = Path(dist_dir)

    meta_path = dist_dir / "metadata.json"
    if not meta_path.is_file():
        errors.append(f"missing file: {meta_path}")
        return errors

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        errors.append(f"metadata.json is not valid JSON: {e}")
        return errors

    if not isinstance(meta, dict):
        errors.append("metadata.json root must be a JSON object")
        return errors

    project_id = meta.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        errors.append("metadata.project_id must be a non-empty string")

    project_slug = meta.get("project_slug")
    project_title = meta.get("project_title")
    if not (
        (isinstance(project_slug, str) and project_slug.strip())
        or (isinstance(project_title, str) and project_title.strip())
    ):
        errors.append(
            "metadata must include a non-empty project_slug and/or project_title"
        )

    entries = meta.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("metadata.entries must be a non-empty array")
        return errors

    seen_versions: dict[str, str] = {}
    seen_files: dict[str, str] = {}

    for i, entry in enumerate(entries):
        prefix = f"entries[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{prefix}.key must be a non-empty string")
            key_label = f"index {i}"
        else:
            key_label = key

        ver = entry.get("version_number")
        if not isinstance(ver, str) or not ver.strip():
            errors.append(
                f"{prefix} ({key_label}): version_number must be a non-empty string"
            )
        elif not SEMVER3.match(ver.strip()):
            errors.append(
                f"{prefix} ({key_label}): version_number must be major.minor.patch, "
                f"got {ver!r}"
            )
        else:
            ver_s = ver.strip()
            if ver_s in seen_versions:
                errors.append(
                    f"duplicate version_number {ver_s!r} in artifact "
                    f"(keys {seen_versions[ver_s]!r} and {key_label!r})"
                )
            else:
                seen_versions[ver_s] = str(key_label)

        changelog = entry.get("changelog")
        if not isinstance(changelog, str) or not changelog.strip():
            errors.append(
                f"{prefix} ({key_label}): changelog must be a non-empty string "
                "(after trimming leading/trailing whitespace)"
            )

        game_versions = entry.get("game_versions")
        if not isinstance(game_versions, list) or not game_versions:
            errors.append(
                f"{prefix} ({key_label}): game_versions must be a non-empty list"
            )
        elif not all(isinstance(v, str) and v.strip() for v in game_versions):
            errors.append(
                f"{prefix} ({key_label}): game_versions must be a list of "
                "non-empty strings"
            )

        file_name = entry.get("file")
        if not isinstance(file_name, str) or not file_name.strip():
            errors.append(
                f"{prefix} ({key_label}): file must be a non-empty string"
            )
            continue

        file_name = file_name.strip()
        if file_name in seen_files:
            errors.append(
                f"duplicate file {file_name!r} in artifact "
                f"(keys {seen_files[file_name]!r} and {key_label!r})"
            )
        else:
            seen_files[file_name] = str(key_label)

        zip_path = dist_dir / file_name
        if not zip_path.is_file():
            errors.append(f"{prefix} ({key_label}): missing zip: {zip_path}")
            continue
        if zip_path.stat().st_size <= 0:
            errors.append(f"{prefix} ({key_label}): zip is empty: {zip_path}")
            continue
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    errors.append(
                        f"{prefix} ({key_label}): zip failed integrity test "
                        f"(bad entry {bad!r}): {zip_path}"
                    )
        except zipfile.BadZipFile:
            errors.append(
                f"{prefix} ({key_label}): not a valid zip: {zip_path}"
            )

    return errors
