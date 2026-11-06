#!/usr/bin/env python3
"""
Local validation of a resource-pack build artifact directory.

Checks metadata.json shape (including shared changelog_body) and every
referenced zip. For each zip also verifies:

  - required root entries (pack.mcmeta or pack.txt, pack.png, LICENSE.txt)
  - pack_format inside the rendered meta matches the lookup row
  - every file from the content folder selected for the entry's game_versions
    is present in the zip (folder fidelity + correct-folder resolution)

Does not call Modrinth and does not upload anything.

Used by:
  - build_artifact.py  (after writing OUT_DIR, before the workflow uploads)
  - publish_to_modrinth.py  (after downloading the artifact, before creates)

Environment (required for the richer checks):
  LOOKUP_FILE   - path to format_lookup.json
  CONTENT_ROOT  - path to core/content

Returns:
  A list of human-readable problem strings. Empty list means the artifact
  passed the local checklist. Callers print the list and exit non-zero when
  the list is non-empty.
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from resolve_pack_paths import (  # noqa: E402
    find_range_dir_for_span,
    parse_semver3,
    version_key,
)

ROOT_ICON = "pack.png"
ROOT_LICENSE = "LICENSE.txt"


def _load_lookup(path: Path) -> dict | None:
    """Load format_lookup.json; return None on any read or JSON error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _expected_pack_format(lookup: dict, key: str, game_versions: list[str]):
    """
    Resolve the pack_format that the rendered meta for this entry must contain.

    Preference order:
      1. The row keyed by the entry's lookup key (normal path).
      2. Any row whose game_versions list exactly matches the entry's list
         (fallback when the key string has drifted).
    Returns a sentinel object() when no matching row is found so that the
    caller can distinguish "unknown" from a legitimate null pack_format.
    """
    row = lookup.get(key)
    if isinstance(row, dict) and "pack_format" in row:
        return row["pack_format"]
    for row in lookup.values():
        if not isinstance(row, dict):
            continue
        if list(row.get("game_versions") or []) == list(game_versions):
            return row.get("pack_format")
    return object()  # unknown


def _check_zip_contents(
    zf: zipfile.ZipFile,
    *,
    key_label: str,
    prefix: str,
    game_versions: list[str],
    content_root: Path | None,
    expected_format,
) -> list[str]:
    """
    Validate the interior of one pack zip.

    Checks performed:
      - Required root entries exist (pack.mcmeta or pack.txt, pack.png,
        LICENSE.txt).
      - The rendered pack_format (or min/max_format) matches the value
        expected from the lookup row.
      - Every file that lives under the content-range directory selected
        for this entry's game_versions span is present in the zip at the
        corresponding relative path (folder fidelity).

    Returns a list of human-readable problem strings (empty = pass).
    """
    errors: list[str] = []
    names = set(zf.namelist())

    has_meta = "pack.mcmeta" in names or "pack.txt" in names
    if not has_meta:
        errors.append(
            f"{prefix} ({key_label}): zip root missing pack.mcmeta or pack.txt"
        )
    if ROOT_ICON not in names:
        errors.append(f"{prefix} ({key_label}): zip root missing {ROOT_ICON}")
    if ROOT_LICENSE not in names:
        errors.append(f"{prefix} ({key_label}): zip root missing {ROOT_LICENSE}")

    if "pack.mcmeta" in names and expected_format is not object():
        try:
            raw = zf.read("pack.mcmeta").decode("utf-8")
            meta_obj = json.loads(raw)
            pack = meta_obj.get("pack") if isinstance(meta_obj, dict) else None
            if not isinstance(pack, dict):
                errors.append(
                    f"{prefix} ({key_label}): pack.mcmeta missing 'pack' object"
                )
            else:
                if "min_format" in pack or "max_format" in pack:
                    mn = pack.get("min_format")
                    mx = pack.get("max_format")
                    if expected_format is None:
                        errors.append(
                            f"{prefix} ({key_label}): pack.mcmeta has numeric "
                            f"format but lookup pack_format is null"
                        )
                    elif mn != expected_format or mx != expected_format:
                        errors.append(
                            f"{prefix} ({key_label}): pack.mcmeta "
                            f"min/max_format ({mn}/{mx}) != lookup "
                            f"pack_format ({expected_format})"
                        )
                elif "pack_format" in pack:
                    pf = pack.get("pack_format")
                    if pf != expected_format:
                        errors.append(
                            f"{prefix} ({key_label}): pack.mcmeta "
                            f"pack_format ({pf}) != lookup "
                            f"pack_format ({expected_format})"
                        )
                else:
                    errors.append(
                        f"{prefix} ({key_label}): pack.mcmeta has no "
                        f"pack_format / min_format / max_format"
                    )
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            errors.append(
                f"{prefix} ({key_label}): pack.mcmeta is not valid JSON: {e}"
            )
    elif "pack.txt" in names and expected_format is not object():
        try:
            text = zf.read("pack.txt").decode("utf-8").strip()
            if not text:
                errors.append(f"{prefix} ({key_label}): pack.txt is empty")
            if expected_format is not None:
                errors.append(
                    f"{prefix} ({key_label}): pack.txt present but lookup "
                    f"pack_format is {expected_format!r} (expected null)"
                )
        except UnicodeDecodeError as e:
            errors.append(
                f"{prefix} ({key_label}): pack.txt is not valid UTF-8: {e}"
            )

    if content_root is not None and game_versions:
        try:
            low = min(game_versions, key=version_key)
            high = max(game_versions, key=version_key)
            content_dir = find_range_dir_for_span(content_root, low, high)
        except SystemExit:
            errors.append(
                f"{prefix} ({key_label}): cannot resolve unique content "
                f"folder for game_versions {game_versions}"
            )
            content_dir = None
        if content_dir is not None:
            expected_files = [
                f.relative_to(content_dir).as_posix()
                for f in content_dir.rglob("*")
                if f.is_file()
            ]
            missing = [rel for rel in expected_files if rel not in names]
            if missing:
                shown = missing[:10]
                extra = len(missing) - len(shown)
                detail = ", ".join(shown)
                if extra > 0:
                    detail += f" (+{extra} more)"
                errors.append(
                    f"{prefix} ({key_label}): zip missing {len(missing)} "
                    f"file(s) from content folder {content_dir.name!r}: {detail}"
                )

    return errors


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

    changelog_body = meta.get("changelog_body")
    if not isinstance(changelog_body, str) or not changelog_body.strip():
        errors.append(
            "metadata.changelog_body must be a non-empty string "
            "(Unreleased section from CHANGELOG.md)"
        )

    entries = meta.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("metadata.entries must be a non-empty array")
        return errors

    lookup_path = Path(os.environ.get("LOOKUP_FILE", ".github/format_lookup.json"))
    content_root_env = os.environ.get("CONTENT_ROOT", "core/content")
    content_root = Path(content_root_env) if content_root_env else None
    lookup = _load_lookup(lookup_path) if lookup_path.is_file() else None
    if lookup is None:
        errors.append(f"LOOKUP_FILE not readable or invalid JSON: {lookup_path}")
    if content_root is not None and not content_root.is_dir():
        errors.append(f"CONTENT_ROOT is not a directory: {content_root}")
        content_root = None

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
        elif parse_semver3(ver.strip()) is None:
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

        game_versions = entry.get("game_versions")
        if not isinstance(game_versions, list) or not game_versions:
            errors.append(
                f"{prefix} ({key_label}): game_versions must be a non-empty list"
            )
            game_versions = []
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

        expected_format = (
            _expected_pack_format(lookup, str(key_label), game_versions)
            if lookup is not None
            else object()
        )

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    errors.append(
                        f"{prefix} ({key_label}): zip failed integrity test "
                        f"(bad entry {bad!r}): {zip_path}"
                    )
                    continue
                errors.extend(
                    _check_zip_contents(
                        zf,
                        key_label=str(key_label),
                        prefix=prefix,
                        game_versions=game_versions,
                        content_root=content_root,
                        expected_format=expected_format,
                    )
                )
        except zipfile.BadZipFile:
            errors.append(
                f"{prefix} ({key_label}): not a valid zip: {zip_path}"
            )

    return errors
