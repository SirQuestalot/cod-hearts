#!/usr/bin/env python3
"""
Build a single aggregate release zip from per-version pack zips + metadata entries order.

Layouts:
  by-date  — entry 0: YYYY-MM-DD/CodHearts-v….zip inside the archive
  flat     — n >= 1: CodHearts-v….zip at archive root

Usage:
  python3 build_release_bundle.py \\
    --metadata path/to/metadata.json \\
    --zips-dir path/to/zips_or_dist \\
    --out path/to/changelogN-YYYY-MM-DD.zip \\
    --layout by-date|flat
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path


def build_bundle(
    *,
    metadata_path: Path,
    zips_dir: Path,
    out_path: Path,
    layout: str,
) -> list[str]:
    if layout not in ("by-date", "flat"):
        raise SystemExit(f"invalid layout {layout!r}; use by-date or flat")

    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    entries = meta.get("entries") or []
    if not isinstance(entries, list) or not entries:
        raise SystemExit("metadata.json has no entries")

    members: list[tuple[str, Path]] = []  # arcname, source
    for e in entries:
        if not isinstance(e, dict):
            continue
        file_name = str(e.get("file") or "").strip()
        if not file_name:
            raise SystemExit(f"entry missing file name: {e!r}")
        src = zips_dir / file_name
        if not src.is_file():
            raise SystemExit(f"missing pack zip: {src}")

        if layout == "by-date":
            day = str(e.get("date_published") or "").strip()[:10]
            if not day:
                raise SystemExit(f"entry {file_name!r} missing date_published for by-date layout")
            arcname = f"{day}/{file_name}"
        else:
            arcname = file_name
        members.append((arcname, src))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    # ZIP_STORED: pack zips are already compressed
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for arcname, src in members:
            zf.write(src, arcname)

    print(f"Wrote {out_path} ({len(members)} pack(s), layout={layout})")
    for arcname, _ in members:
        print(f"  {arcname}")
    return [a for a, _ in members]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Build aggregate GitHub release zip")
    p.add_argument("--metadata", required=True, type=Path)
    p.add_argument("--zips-dir", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--layout", required=True, choices=("by-date", "flat"))
    args = p.parse_args(argv)
    build_bundle(
        metadata_path=args.metadata,
        zips_dir=args.zips_dir,
        out_path=args.out,
        layout=args.layout,
    )


if __name__ == "__main__":
    main()
