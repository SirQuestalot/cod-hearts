#!/usr/bin/env python3
"""
Build resource-pack zips + metadata.json from format_lookup.json and core/ trees.

Invoked by .github/workflows/build-and_publish.yml with configuration via env vars:
  LOOKUP_FILE, CONTENT_ROOT, META_ROOT, MODS_ROOT, MODRINTH_API,
  PROJECT_SLUG, BUMP, VERSION_OVERRIDE, INCLUDE_MODS, CHANGELOG_FILE,
  DESCRIPTION, LICENSE, TARGETS, OUT_DIR, PACK_ICON, LICENSE_FILE

Does not publish to Modrinth — only writes zips + metadata.json under OUT_DIR.
Reads CHANGELOG.md [Unreleased] into metadata.changelog_body.
Runs local validate_artifact() on OUT_DIR before exiting successfully.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
import sys

# Sibling import: validate_artifact.py lives next to this script in CI checkout
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from validate_artifact import validate_artifact
from update_changelog import extract_unreleased
from read_env import env, write_github_output
from fetch_modrinth import http_json_or_exit as http_json
from resolve_pack_paths import (
    version_key,
    parse_range_dir_name,
    in_range,
    find_range_dir_for_span,
    parse_semver3,
    game_overlap,
)


# =============================================================================
# format_lookup.json loading
# =============================================================================
# Expected root object keyed by version key:
#   {
#     "26.3": { "pack_format": 97, "game_versions": ["26.3"] },
#     "1.0-1.5.x": { "pack_format": null, "game_versions": ["1.0", ...] }
#   }
# pack_format: int or null (null = pre-pack-format, game_versions<1.6).

def resolve_targets_from_env() -> list[str]:
    """
    Resolve TARGETS from workflow inputs (MODE, MC_VERSION_KEY_*, LOOKUP_FILE).

    Mirrors the former plan-step bash: validates mode/bump combinations,
    loads lookup keys in file order, and returns the ordered target list.
    """
    mode = os.environ.get("MODE", "").strip()
    single = os.environ.get("MC_VERSION_KEY_SINGLE", "").strip()
    range_raw = os.environ.get("MC_VERSION_KEY_RANGE", "").strip()
    bump = os.environ.get("BUMP", "").strip()
    version_override = os.environ.get("VERSION_OVERRIDE", "").strip()
    lookup_file = Path(env("LOOKUP_FILE"))

    if single in ("(for mode=single)",):
        single = ""
    if range_raw in ("(for mode=range)",):
        range_raw = ""
    if version_override in ("(for bump=manual)",):
        version_override = ""

    if mode == "single" and not single:
        raise SystemExit("Error: mc_version_key_single is required when mode=single")
    if mode == "range" and not range_raw:
        raise SystemExit("Error: mc_version_key_range is required when mode=range")
    if bump == "manual":
        if mode != "single":
            raise SystemExit("Error: bump=manual is only allowed when mode=single")
        if not version_override:
            raise SystemExit("Error: version_override is required when bump=manual")

    if not lookup_file.is_file():
        raise SystemExit(f"Error: {lookup_file} not found")

    lookup = parse_lookup(lookup_file)
    all_keys = list(lookup.keys())
    if not all_keys:
        raise SystemExit(f"Error: {lookup_file} has no keys")

    def resolve_key(want: str) -> str:
        for key in all_keys:
            if key == want:
                return key
        raise SystemExit(f"Error: key not found in {lookup_file}: {want!r}")

    if mode == "single":
        targets = [resolve_key(single)]
    elif mode == "range":
        endpoints = [p.strip() for p in range_raw.split(",") if p.strip()]
        if len(endpoints) != 2:
            raise SystemExit(
                "Error: mode=range requires exactly two comma-separated keys "
                f"(start, end); got {len(endpoints)} from: {range_raw}"
            )
        start_key = resolve_key(endpoints[0])
        end_key = resolve_key(endpoints[1])
        start_i = all_keys.index(start_key)
        end_i = all_keys.index(end_key)
        if start_i > end_i:
            start_i, end_i = end_i, start_i
        targets = all_keys[start_i : end_i + 1]
    elif mode == "all":
        targets = list(all_keys)
    else:
        raise SystemExit(f"Error: unknown mode {mode!r}")

    print(f"Targets ({len(targets)}): {' '.join(targets)}")
    return targets


def cli_resolve_targets() -> None:
    """Workflow entry: write targets to GITHUB_OUTPUT."""
    targets = resolve_targets_from_env()
    write_github_output("targets", "\n".join(targets) + "\n")


def parse_lookup(path: Path) -> dict:
    """
    Load LOOKUP_FILE JSON and validate shape.
    Returns the dict json.loads produces.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid JSON in {path}: {e}") from e

    if not isinstance(data, dict):
        raise SystemExit(f"Lookup root must be a JSON object: {path}")

    for key, row in data.items():
        if not isinstance(key, str) or not key:
            raise SystemExit(f"Lookup keys must be non-empty strings, got: {key!r}")

        if not isinstance(row, dict):
            raise SystemExit(f"lookup entry {key!r} must be an object")

        if "pack_format" not in row or "game_versions" not in row:
            raise SystemExit(
                f"lookup entry {key!r} must have pack_format and game_versions"
            )

        pack_format = row["pack_format"]
        # type(...) is not int rejects bool (True/False), which isinstance would allow
        if pack_format is not None and type(pack_format) is not int:
            raise SystemExit(
                f"lookup entry {key!r}: pack_format must be an integer or null"
            )

        game_versions = row["game_versions"]
        if not isinstance(game_versions, list) or not game_versions:
            raise SystemExit(
                f"lookup entry {key!r}: game_versions must be a non-empty list"
            )
        if not all(isinstance(v, str) and v for v in game_versions):
            raise SystemExit(
                f"lookup entry {key!r}: game_versions must be a list of non-empty strings"
            )

    return data


def template_file_in(meta_dir: Path) -> Path:
    """
    Each meta range folder must contain exactly one non-hidden template file
    (any name: pack.mcmeta, pack.txt, etc.). That file is copied into the zip root
    after placeholder substitution.
    """
    files = sorted(
        p for p in meta_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    if not files:
        raise SystemExit(f"No template file in {meta_dir}")
    if len(files) > 1:
        raise SystemExit(
            f"Expected one template file in {meta_dir}, found: {[p.name for p in files]}"
        )
    return files[0]


def select_paths(content_root: Path, meta_root: Path, game_versions: list[str]):
    """
    From a row's game_versions list:
      - low/high = min/max by version_key
      - content dir = unique range folder covering that span
      - meta dir   = unique range folder covering that span
      - template   = the single file in that meta dir
    Returns (content_dir, template_path, template_basename).
    """
    if not game_versions:
        raise SystemExit("Lookup entry has empty game_versions list")

    low = min(game_versions, key=version_key)
    high = max(game_versions, key=version_key)

    content = find_range_dir_for_span(content_root, low, high)
    meta_dir = find_range_dir_for_span(meta_root, low, high)
    meta_template = template_file_in(meta_dir)
    return content, meta_template, meta_template.name


def check_coverage(lookup: dict, content_root: Path, meta_root: Path) -> None:
    """
    Verify that every lookup key is covered by exactly one content folder and
    exactly one meta folder, and that no range folders are left unreferenced.
    Prints every problem then raises SystemExit if any exist.
    """
    errors: list[str] = []

    content_dirs = sorted(p for p in content_root.iterdir() if p.is_dir()) if content_root.is_dir() else []
    meta_dirs = sorted(p for p in meta_root.iterdir() if p.is_dir()) if meta_root.is_dir() else []
    content_names = [p.name for p in content_dirs]
    meta_names = [p.name for p in meta_dirs]

    referenced_content: set[str] = set()
    referenced_meta: set[str] = set()

    for key, row in lookup.items():
        games = row["game_versions"]
        if not games:
            continue
        low = min(games, key=version_key)
        high = max(games, key=version_key)

        # Content coverage
        c_matches = [
            p for p in content_dirs
            if in_range(low, *parse_range_dir_name(p.name))
            and in_range(high, *parse_range_dir_name(p.name))
        ]
        if not c_matches:
            errors.append(
                f"No content folder covers key {key!r} ({low}–{high}): {content_names}"
            )
        elif len(c_matches) > 1:
            errors.append(
                f"Multiple content folders cover key {key!r} ({low}–{high}): "
                f"{[p.name for p in c_matches]}"
            )
        else:
            referenced_content.add(c_matches[0].name)

        # Meta coverage
        m_matches = [
            p for p in meta_dirs
            if in_range(low, *parse_range_dir_name(p.name))
            and in_range(high, *parse_range_dir_name(p.name))
        ]
        if not m_matches:
            errors.append(
                f"No meta folder covers key {key!r} ({low}–{high}): {meta_names}"
            )
        elif len(m_matches) > 1:
            errors.append(
                f"Multiple meta folders cover key {key!r} ({low}–{high}): "
                f"{[p.name for p in m_matches]}"
            )
        else:
            referenced_meta.add(m_matches[0].name)

    for name in content_names:
        if name not in referenced_content:
            errors.append(
                f"Orphan content folder {name!r} is not referenced by any lookup key"
            )
    for name in meta_names:
        if name not in referenced_meta:
            errors.append(
                f"Orphan meta folder {name!r} is not referenced by any lookup key"
            )

    if errors:
        for msg in errors:
            print(msg)
        raise SystemExit("Static coverage check failed")


# =============================================================================
# Template rendering (pack.mcmeta / pack.txt placeholders)
# =============================================================================

def render_meta(
    template: Path,
    out_name: str,
    pack_format,
    dest: Path,
    description: str,
    license_text: str,
) -> None:
    """
    Read template, replace placeholders, write dest/out_name.
      {{PACK_FORMAT}}  → numeric format or "" if null
      {{DESCRIPTION}}  → GitHub About (ASCII-filtered in the workflow)
      {{LICENSE}}      → license string from workflow
    """
    text = template.read_text(encoding="utf-8")
    text = text.replace(
        "{{PACK_FORMAT}}",
        str(pack_format if pack_format is not None else ""),
    )
    text = text.replace("{{DESCRIPTION}}", description)
    text = text.replace("{{LICENSE}}", license_text)
    (dest / out_name).write_text(text, encoding="utf-8")


# =============================================================================
# Version number resolution (Modrinth-aware bump)
# =============================================================================

def resolve_version_number(key: str, row, project_versions, bump: str, version_override: str):
    """
    Returns (new_version_string, previous_version_string_or_None).

    Automatic bumps require an exact game_versions set match (Jaccard == 1.0)
    with a prior version whose major equals the current pack_format (or 0).
    The major component of every version number is defined as that pack_format.

    Before returning or aborting, prints a one-line diagnostic:
      exact:   "{key}: exact predecessor {prev_num}"
      mismatch: "{key}: no exact predecessor (expected [...], received [...] from {cand}, ...)"
      manual:  "{key}: manual override {version_override}"
    At most two near-misses are shown (empty list appears as "received []").
    """
    if bump == "manual":
        if not parse_semver3(version_override):
            raise SystemExit(
                f"version_override must be major.minor.patch, got: {version_override}"
            )
        print(f"{key}: manual override {version_override}")
        return version_override, None

    major = 0 if row["pack_format"] is None else row["pack_format"]
    row_games = row["game_versions"]

    # Collect every remote version that shares the pack_format major.
    # Each entry: (date_published, parsed_semver, version_number, overlap, game_versions)
    candidates = []
    for ver in project_versions:
        num = ver.get("version_number") or ""
        parsed = parse_semver3(num)
        if not parsed or parsed[0] != major:
            continue
        remote_games = ver.get("game_versions") or []
        overlap = game_overlap(remote_games, row_games)
        candidates.append(
            (ver.get("date_published") or "", parsed, num, overlap, remote_games)
        )

    # Highest overlap first, then most recently published.
    candidates.sort(key=lambda x: (x[3], x[0]), reverse=True)

    exact = [c for c in candidates if c[3] == 1.0]
    near = [c for c in candidates if c[3] < 1.0][:2]

    if exact:
        prev_num = exact[0][2]
        print(f"{key}: exact predecessor {prev_num}")
        _, (mj, mi, pa), _, _, _ = exact[0]
        if bump == "feature":
            return f"{mj}.{mi + 1}.0", prev_num
        return f"{mj}.{mi}.{pa + 1}", prev_num

    # No exact match — emit diagnostic then abort.
    if near:
        parts = [f"received {c[4]!r} from {c[2]}" for c in near]
        received_text = ", ".join(parts)
    else:
        received_text = "received []"
    print(
        f"{key}: no exact predecessor "
        f"(expected {row_games!r}, {received_text})"
    )
    raise SystemExit(
        f"No exact game_versions match for {key!r}. Use bump=manual."
    )


# =============================================================================
# Zip assembly
# =============================================================================

def zip_pack(
    content_dir: Path,
    meta_template: Path,
    meta_out_name: str,
    pack_format,
    include_mods: bool,
    mods_root: Path,
    description: str,
    license_text: str,
    zip_path: Path,
    pack_icon: Path,
    license_file: Path,
) -> None:
    """
    Build one resource-pack zip:
      1. Copy content_dir tree into a temp pack root
      2. Render meta template into pack root under meta_out_name
      3. Copy pack icon + license file to pack root (required)
      4. Optionally merge each mods/<name>/assets/ into pack assets/
         (only when pack_format is numeric)
      5. Zip the pack root to zip_path
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pack"
        root.mkdir()

        # --- content assets --------------------------------------------------
        for item in content_dir.iterdir():
            dest = root / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

        # --- pack.mcmeta / pack.txt ------------------------------------------
        render_meta(
            meta_template,
            meta_out_name,
            pack_format,
            root,
            description,
            license_text,
        )

        # --- pack icon + license at zip root ---------------------------------
        shutil.copy2(pack_icon, root / pack_icon.name)
        shutil.copy2(license_file, root / license_file.name)

        # --- mod overlays: mods/<mod>/assets → pack/assets -------------------
        if include_mods and mods_root.is_dir() and pack_format is not None:
            for mod_dir in sorted(p for p in mods_root.iterdir() if p.is_dir()):
                assets_src = mod_dir / "assets"
                if not assets_src.is_dir():
                    print(f"Skipping mod without assets/: {mod_dir.name}")
                    continue
                assets_dest = root / "assets"
                assets_dest.mkdir(exist_ok=True)
                for sub in assets_src.rglob("*"):
                    rel = sub.relative_to(assets_src)
                    target = assets_dest / rel
                    if sub.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(sub, target)

        # --- write zip -------------------------------------------------------
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in root.rglob("*"):
                if file.is_file():
                    # Archive path relative to pack root, POSIX separators
                    zf.write(file, file.relative_to(root).as_posix())


# =============================================================================
# main
# =============================================================================

def main() -> None:
    """
    Build resource-pack zips and metadata.json for the targets selected by
    the workflow.

    High-level steps:
      1. Load and validate format_lookup.json; resolve the list of target keys.
      2. Run check_coverage so that every key maps to exactly one content and
         one meta range directory (and no orphan folders remain).
      3. Fetch the current project versions from Modrinth (needed for automatic
         version-number calculation).
      4. For each target key: resolve content/meta paths, decide the next
         version number (exact game-version match required unless bump=manual),
         assemble the zip, and record an entry.
      5. Write metadata.json (including the shared Unreleased changelog body).
      6. Run local validate_artifact on OUT_DIR and abort if any problems are
         reported.

    Environment variables are listed in the module docstring. This function
    never publishes; publishing is performed by a later job that consumes the
    uploaded artifact.
    """
    # --- config from environment (set by the workflow) -----------------------
    lookup_file = Path(env("LOOKUP_FILE"))
    content_root = Path(env("CONTENT_ROOT"))
    meta_root = Path(env("META_ROOT"))
    mods_root = Path(env("MODS_ROOT"))
    modrinth_api = env("MODRINTH_API").rstrip("/")
    project_slug = env("PROJECT_SLUG")
    bump = env("BUMP")
    version_override = os.environ.get("VERSION_OVERRIDE", "").strip()
    include_mods = os.environ.get("INCLUDE_MODS", "true").lower() == "true"
    description = os.environ.get("DESCRIPTION", "")
    license_text = os.environ.get("LICENSE", "See LICENSE.txt")
    # Newline-separated lookup keys from the workflow plan step
    targets = [t.strip() for t in env("TARGETS").splitlines() if t.strip()]
    out_dir = Path(os.environ.get("OUT_DIR", "build/dist"))

    # Required pack icon + license file (paths from workflow env)
    pack_icon = Path(env("PACK_ICON"))
    license_file = Path(env("LICENSE_FILE"))
    if not pack_icon.is_file():
        raise SystemExit(f"PACK_ICON not found: {pack_icon}")
    if not license_file.is_file():
        raise SystemExit(f"LICENSE_FILE not found: {license_file}")

    changelog_file = Path(os.environ.get("CHANGELOG_FILE", "CHANGELOG.md"))
    changelog_body = extract_unreleased(changelog_file)
    print(f"Unreleased changelog body from {changelog_file} ({len(changelog_body)} chars)")

    # --- load lookup; ensure every requested key exists ----------------------
    lookup = parse_lookup(lookup_file)
    for t in targets:
        if t not in lookup:
            raise SystemExit(f"Key not in lookup: {t}")

    # --- static coverage: every key covered, no orphan folders ---------------
    check_coverage(lookup, content_root, meta_root)

    # --- Modrinth project + existing versions (for auto-bump) ----------------
    ua = f"{project_slug}-build/1.0"
    project = http_json(f"{modrinth_api}/project/{project_slug}", ua)
    project_versions = (
        http_json(f"{modrinth_api}/project/{project_slug}/version", ua) or []
    )
    title = project.get("title") or project_slug

    out_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "project_slug": project_slug,
        "project_id": project["id"],
        "project_title": title,
        "changelog_body": changelog_body,
        "entries": [],
    }

    # --- build one zip (+ metadata entry) per target key ---------------------
    for key in targets:
        row = lookup[key]

        content, meta_template, meta_name = select_paths(
            content_root, meta_root, row["game_versions"]
        )
        # print(f"Content: {content}")
        # print(f"Meta:    {meta_template}")

        ver_num, prev = resolve_version_number(
            key, row, project_versions, bump, version_override
        )

        zip_name = "".join(word.capitalize() for word in project_slug.split("-"))
        file_name = f"{zip_name}-v{ver_num}.zip"
        zip_path = out_dir / file_name

        use_mods = include_mods and row["pack_format"] is not None
        zip_pack(
            content,
            meta_template,
            meta_name,
            row["pack_format"],
            use_mods,
            mods_root,
            description,
            license_text,
            zip_path,
            pack_icon,
            license_file,
        )

        metadata["entries"].append(
            {
                "key": key,
                "version_number": ver_num,
                "previous_version": prev,
                "file": file_name,
                "game_versions": row["game_versions"],
            }
        )
        # print(f"{zip_path}")
        # print(f"Changelog:\n{changelog}")
        # print("=============")

    # --- write metadata.json for the artifact -----------------------------
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    print("\nWrote metadata.json")


    # --- local artifact checklist before workflow uploads -------------------
    errors = validate_artifact(out_dir)
    if errors:
        print(f"Artifact validation failed with {len(errors)} problem(s):")
        for i, msg in enumerate(errors, 1):
            print(f"  {i}. {msg}")
        raise SystemExit(1)
    print("Artifact validation passed.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "resolve-targets":
        cli_resolve_targets()
    else:
        main()
