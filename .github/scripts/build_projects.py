#!/usr/bin/env python3
"""
Build resource-pack zips + metadata.json from mcmeta_lookup.json and core/ trees.

Invoked by .github/workflows/build-projects.yml with configuration via env vars:
  LOOKUP_FILE, CONTENT_ROOT, META_ROOT, MODS_ROOT, MODRINTH_API,
  PROJECT_SLUG, BUMP, VERSION_OVERRIDE, INCLUDE_MODS, CHANGELOG,
  DESCRIPTION, LICENSE, TARGETS, OUT_DIR

Does not publish to Modrinth — only writes zips + metadata.json under OUT_DIR.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


# =============================================================================
# Environment helpers
# =============================================================================

def env(name: str, default: str | None = None) -> str:
    """
    Read a required environment variable.
    If default is None and the var is missing, abort the whole build.
    """
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def http_json(url: str, user_agent: str):
    """
    GET a JSON URL (Modrinth API). Returns parsed object, or None if empty body.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


# =============================================================================
# mcmeta_lookup.json loading
# =============================================================================
# Root object keyed by version key:
#   {
#     "26.3": { "pack_format": 97, "game_versions": ["26.3"] },
#     "1.0-1.5.x": { "pack_format": null, "game_versions": ["1.0", ...] }
#   }
# pack_format: int or null (null = pre-pack-format, game_versions<1.6).

def parse_lookup(path: Path) -> dict:
    """
    Load LOOKUP_FILE JSON into:
      { key: { "pack_format": int|None, "game_versions": [str, ...] } }
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid JSON in {path}: {e}") from e

    if not isinstance(data, dict):
        raise SystemExit(f"Lookup root must be a JSON object: {path}")

    rows = {}
    for key, row in data.items():
        if not isinstance(key, str) or not key:
            raise SystemExit(f"lookup keys must be non-empty strings, got: {key!r}")
        if not isinstance(row, dict):
            raise SystemExit(f"lookup entry {key!r} must be an object")

        if "pack_format" not in row or "game_versions" not in row:
            raise SystemExit(
                f"lookup entry {key!r} must have pack_format and game_versions"
            )

        pack_format = row["pack_format"]
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

        rows[key] = {
            "pack_format": pack_format,
            "game_versions": list(game_versions),
        }

    return rows


# =============================================================================
# Minecraft-ish version ordering and content/meta folder selection
# =============================================================================
# Folder names under core/content and core/meta are ranges, e.g. "1.20.2-26.3".
# We place a lookup entry by checking that its lowest and highest game versions
# both fall inside the same folder range (inclusive).

def version_key(v: str) -> tuple:
    """
    Turn "1.20.2" / "26.3" into a tuple of ints for sorting/comparison.
    Non-digit segments become 0 (defensive).
    """
    return tuple(int(p) if p.isdigit() else 0 for p in v.split("."))


def parse_range_dir_name(name: str) -> tuple[str, str]:
    """
    '1.20.2-26.3' -> ('1.20.2', '26.3')
    '26.3'        -> ('26.3', '26.3')   # single endpoint folder
    Only splits on the first '-' so odd names still parse predictably.
    """
    if "-" in name:
        left, right = name.split("-", 1)
        return left, right
    return name, name


def in_range(version: str, min_v: str, max_v: str) -> bool:
    """True if version_key(min) <= version_key(version) <= version_key(max)."""
    k = version_key(version)
    return version_key(min_v) <= k <= version_key(max_v)


def find_range_dir_for_span(root: Path, low: str, high: str) -> Path:
    """
    Find exactly one subdirectory of root whose name-range contains both low and high.
    0 matches or 2+ matches → hard error (misconfigured folders or overlapping ranges).
    """
    if not root.is_dir():
        raise SystemExit(f"Missing directory: {root}")

    matches = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        min_v, max_v = parse_range_dir_name(path.name)
        if in_range(low, min_v, max_v) and in_range(high, min_v, max_v):
            matches.append(path)

    folders = [p.name for p in root.iterdir() if p.is_dir()]
    if not matches:
        raise SystemExit(
            f"No folder under {root} contains full span {low}–{high}. "
            f"Folders: {folders}"
        )
    if len(matches) > 1:
        raise SystemExit(
            f"Ambiguous folders under {root} for span {low}–{high}: "
            f"{[p.name for p in matches]}"
        )
    return matches[0]


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

def parse_semver3(s: str):
    """
    Parse strict major.minor.patch → (major, minor, patch) or None.
    """
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", s.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def game_overlap(a, b) -> float:
    """
    Jaccard overlap of two game version lists.
    Used to match an existing Modrinth version to this lookup entry's lineage
    (same pack-format major is not enough after merges/splits).
    """
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def resolve_version_number(row, project_versions, bump: str, version_override: str):
    """
    Returns (new_version_string, previous_version_string_or_None).

    patch/feature: prior must have the same pack-format major and 
    an exact game_versions set match (overlap == 1). 

    If no such prior exists, exit and tell the user to 
    use bump=manual (e.g. first release or list changed).
    """
    if bump == "manual":
        if not parse_semver3(version_override):
            raise SystemExit(
                f"version_override must be major.minor.patch, got: {version_override}"
            )
        return version_override, None

    major = 0 if row["pack_format"] is None else row["pack_format"]
    row_games = row["game_versions"]

    candidates = []
    for ver in project_versions:
        num = ver.get("version_number") or ""
        parsed = parse_semver3(num)
        if not parsed or parsed[0] != major:
            continue
        overlap = game_overlap(ver.get("game_versions") or [], row_games)
        # Exact set match only (Jaccard == 1)
        if overlap != 1.0:
            continue
        candidates.append(
            (ver.get("date_published") or "", parsed, num, overlap)
        )

    candidates.sort(key=lambda x: (x[3], x[0]), reverse=True)

    if not candidates:
        raise SystemExit(
            "No Modrinth version found with the same pack_format major and an "
            "exact game_versions match for this lookup key.\n"
            "Use bump=manual with version_override (e.g. "
            f"{major}.0.0) for a first release or after changing game_versions."
        )

    _, (mj, mi, pa), prev_num, _ = candidates[0]
    if bump == "feature":
        return f"{mj}.{mi + 1}.0", prev_num
    return f"{mj}.{mi}.{pa + 1}", prev_num


def build_changelog(new_num: str, prev_num, bump: str, changelog_body: str) -> str:
    """
    Auto header + blank line + user body (Markdown-friendly on Modrinth).

      Manual release
      1.0.0

      Initial release
      1.0.0

      Feature release
      1.0.0 → 1.1.0

      Patch release
      1.0.0 → 1.0.1
    """
    if bump == "manual":
        header = f"Manual release\n{new_num}"
    elif prev_num is None:
        header = f"Initial release\n{new_num}"
    elif bump == "feature":
        header = f"Feature release\n{prev_num} → {new_num}"
    else:
        header = f"Patch release\n{prev_num} → {new_num}"
    return f"{header}\n\n{changelog_body}"


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
) -> None:
    """
    Build one resource-pack zip:
      1. Copy content_dir tree into a temp pack root
      2. Render meta template into pack root under meta_out_name
      3. Optionally merge mods_root (only when pack_format is numeric, game_versions 1.6+)
      4. Zip the pack root to zip_path (deflated)
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

        # --- pack metadata ---------------------------------------------------
        render_meta(
            meta_template,
            meta_out_name,
            pack_format,
            root,
            description,
            license_text,
        )

        # --- optional mod overlay (game_versions 1.6+ / numeric pack_format only) ----------
        if include_mods and mods_root.is_dir() and pack_format is not None:
            for item in mods_root.iterdir():
                dest = root / item.name
                if item.is_dir():
                    if dest.exists():
                        # Merge into existing namespace (e.g. assets/)
                        for sub in item.rglob("*"):
                            rel = sub.relative_to(item)
                            target = dest / rel
                            if sub.is_dir():
                                target.mkdir(parents=True, exist_ok=True)
                            else:
                                target.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(sub, target)
                    else:
                        shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

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
    changelog_body = env("CHANGELOG").strip()
    description = os.environ.get("DESCRIPTION", "")
    license_text = os.environ.get("LICENSE", "See LICENSE.txt")
    # Newline-separated lookup keys from the workflow plan step
    targets = [t.strip() for t in env("TARGETS").splitlines() if t.strip()]
    out_dir = Path(os.environ.get("OUT_DIR", "build/dist"))

    if not changelog_body:
        raise SystemExit("CHANGELOG is empty")

    # --- load lookup; ensure every requested key exists ----------------------
    lookup = parse_lookup(lookup_file)
    for t in targets:
        if t not in lookup:
            raise SystemExit(f"Key not in lookup: {t}")

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
        "entries": [],
    }

    # --- build one zip (+ metadata entry) per target key ---------------------
    for key in targets:
        row = lookup[key]
        print(f"\n=== {key} ===")

        content, meta_template, meta_name = select_paths(
            content_root, meta_root, row["game_versions"]
        )
        print(f"Content: {content}")
        print(f"Meta:    {meta_template}")

        ver_num, prev = resolve_version_number(
            row, project_versions, bump, version_override
        )
        changelog = build_changelog(ver_num, prev, bump, changelog_body)

        safe_key = key.replace("/", "-")
        file_name = f"{project_slug}-{safe_key}-{ver_num}.zip"
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
        )

        metadata["entries"].append(
            {
                "key": key,
                "version_number": ver_num,
                "previous_version": prev,
                "name": f"{title} {key}",
                "game_versions": row["game_versions"],
                "changelog": changelog,
                "file": file_name,
            }
        )
        print(f"Built {zip_path}")
        print(changelog)

    # --- write metadata.json for the publish workflow -----------------------------
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    print("\nWrote metadata.json")


if __name__ == "__main__":
    main()