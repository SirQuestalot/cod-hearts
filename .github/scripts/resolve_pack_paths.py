#!/usr/bin/env python3
"""
Resolve version spans, pack folders, and SemVer triples.

Shared by build_artifact and validate_artifact.

Typical call sequence for a lookup row whose game_versions list is
["1.20.2", "1.20.3", "1.20.4"]:

  low, high   = min/max by version_key
  content_dir = find_range_dir_for_span(CONTENT_ROOT, low, high)
  meta_dir    = find_range_dir_for_span(META_ROOT, low, high)

Range directory names may be single-endpoint ("26.3") or hyphenated
("1.20.2-26.3"). Overlapping or missing coverage is a hard error so that
a misconfigured core/ tree cannot silently produce an incomplete pack.
"""

from __future__ import annotations

import re
from pathlib import Path


def version_key(v: str) -> tuple:
    """
    Turn "1.20.2" / "26.3" into a tuple of ints for sorting/comparison.

    Non-digit segments become 0 (defensive). The resulting tuples can be
    compared directly with ordinary Python ordering.
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
    The search is deterministic: directories are examined in sorted name order.
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


def parse_semver3(s: str):
    """
    Parse strict major.minor.patch → (major, minor, patch) or None.

    Returns None for any string that is not exactly three non-negative
    integer components separated by dots (no pre-release or build metadata).
    """
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", s.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def game_overlap(a, b) -> float:
    """
    Jaccard overlap of two game version lists.

    Used to match an existing Modrinth version to this lookup entry's lineage
    (same pack-format major is not enough after merges/splits). Returns 0.0
    when either list is empty; otherwise |intersection| / |union|.
    """
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
