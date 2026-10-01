#!usrbinenv python3
Build resource-pack zips + manifest.json from meta_lookup and core trees.

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

def env(name str, default str  None = None) - str
    value = os.environ.get(name, default)
    if value is None
        raise SystemExit(fMissing required environment variable {name})
    return value

def http_json(url str, user_agent str)
    req = urllib.request.Request(
        url,
        headers={User-Agent user_agent, Accept applicationjson},
    )
    with urllib.request.urlopen(req) as resp
        raw = resp.read()
        return json.loads(raw) if raw else None

def parse_lookup(path Path)
    rows = {}
    for line in path.read_text().splitlines()
        line = line.split(#, 1)[0].strip()
        if not line
            continue
        m = re.match(r^([^,]+),s([^,]+),s[(.)]s$, line)
        if not m
            raise SystemExit(fBad lookup line {line})
        label = m.group(1).strip()
        fmt_s = m.group(2).strip()
        versions = re.findall(r[0-9]+(.[0-9]+), m.group(3))
        pack_format = None if fmt_s.lower() == nan else int(fmt_s)
        rows[label] = {
            label label,
            pack_format pack_format,
            game_versions versions,
        }
    return rows

def version_key(v str)
    return tuple(int(p) if p.isdigit() else 0 for p in v.split(.))

def parse_range_dir_name(name str)
    if - in name
        left, right = name.split(-, 1)
        return left, right
    return name, name

def in_range(version str, min_v str, max_v str) - bool
    k = version_key(version)
    return version_key(min_v) = k = version_key(max_v)

def find_range_dir_for_span(root Path, low str, high str) - Path
    if not root.is_dir()
        raise SystemExit(fMissing directory {root})
    matches = []
    for path in sorted(p for p in root.iterdir() if p.is_dir())
        min_v, max_v = parse_range_dir_name(path.name)
        if in_range(low, min_v, max_v) and in_range(high, min_v, max_v)
            matches.append(path)
    folders = [p.name for p in root.iterdir() if p.is_dir()]
    if not matches
        raise SystemExit(
            fNo folder under {root} contains full span {low}–{high}. 
            fFolders {folders}
        )
    if len(matches)  1
        raise SystemExit(
            fAmbiguous folders under {root} for span {low}–{high} 
            f{[p.name for p in matches]}
        )
    return matches[0]

def template_file_in(meta_dir Path) - Path
    files = sorted(
        p for p in meta_dir.iterdir() if p.is_file() and not p.name.startswith(.)
    )
    if not files
        raise SystemExit(fNo template file in {meta_dir})
    if len(files)  1
        raise SystemExit(
            fExpected one template file in {meta_dir}, found {[p.name for p in files]}
        )
    return files[0]

def select_paths(content_root Path, meta_root Path, game_versions list[str])
    if not game_versions
        raise SystemExit(Lookup row has empty game_versions list)
    low = min(game_versions, key=version_key)
    high = max(game_versions, key=version_key)
    content = find_range_dir_for_span(content_root, low, high)
    meta_dir = find_range_dir_for_span(meta_root, low, high)
    meta_template = template_file_in(meta_dir)
    return content, meta_template, meta_template.name

def render_meta(
    template Path,
    out_name str,
    pack_format,
    dest Path,
    description str,
    license_text str,
)
    text = template.read_text()
    text = text.replace(
        {{PACK_FORMAT}}, str(pack_format if pack_format is not None else )
    )
    text = text.replace({{DESCRIPTION}}, description)
    text = text.replace({{LICENSE}}, license_text)
    (dest  out_name).write_text(text)

def parse_semver3(s str)
    m = re.fullmatch(r(d+).(d+).(d+), s.strip())
    return tuple(int(x) for x in m.groups()) if m else None

def game_overlap(a, b) - float
    sa, sb = set(a), set(b)
    if not sa or not sb
        return 0.0
    return len(sa & sb)  len(sa  sb)

def resolve_version_number(row, project_versions, bump str, version_override str)
    if bump == manual
        if not parse_semver3(version_override)
            raise SystemExit(
                fversion_override must be major.minor.patch, got {version_override}
            )
        return version_override, None

    major = 0 if row[pack_format] is None else row[pack_format]
    candidates = []
    for ver in project_versions
        num = ver.get(version_number) or 
        parsed = parse_semver3(num)
        if not parsed or parsed[0] != major
            continue
        overlap = game_overlap(ver.get(game_versions) or [], row[game_versions])
        if overlap = 0
            continue
        candidates.append((ver.get(date_published) or , parsed, num, overlap))

    candidates.sort(key=lambda x (x[3], x[0]), reverse=True)
    if not candidates
        return f{major}.0.0, None

    _, (mj, mi, pa), prev_num, _ = candidates[0]
    if bump == feature
        return f{mj}.{mi + 1}.0, prev_num
    return f{mj}.{mi}.{pa + 1}, prev_num

def build_changelog(new_num str, prev_num, bump str, changelog_body str) - str
    if bump == manual
        header = fManual releasen{new_num}
    elif prev_num is None
        header = fInitial releasen{new_num}
    elif bump == feature
        header = fFeature releasen{prev_num} → {new_num}
    else
        header = fPatch releasen{prev_num} → {new_num}
    return f{header}nn{changelog_body}

def zip_pack(
    content_dir Path,
    meta_template Path,
    meta_out_name str,
    pack_format,
    include_mods bool,
    mods_root Path,
    description str,
    license_text str,
    zip_path Path,
)
    with tempfile.TemporaryDirectory() as tmp
        root = Path(tmp)  pack
        root.mkdir()
        for item in content_dir.iterdir()
            dest = root  item.name
            if item.is_dir()
                shutil.copytree(item, dest)
            else
                shutil.copy2(item, dest)
        render_meta(
            meta_template, meta_out_name, pack_format, root, description, license_text
        )
        if include_mods and mods_root.is_dir() and pack_format is not None
            for item in mods_root.iterdir()
                dest = root  item.name
                if item.is_dir()
                    if dest.exists()
                        for sub in item.rglob()
                            rel = sub.relative_to(item)
                            target = dest  rel
                            if sub.is_dir()
                                target.mkdir(parents=True, exist_ok=True)
                            else
                                target.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(sub, target)
                    else
                        shutil.copytree(item, dest)
                else
                    shutil.copy2(item, dest)
        with zipfile.ZipFile(zip_path, w, zipfile.ZIP_DEFLATED) as zf
            for file in root.rglob()
                if file.is_file()
                    zf.write(file, file.relative_to(root).as_posix())

def main() - None
    lookup_file = Path(env(LOOKUP_FILE))
    content_root = Path(env(CONTENT_ROOT))
    meta_root = Path(env(META_ROOT))
    mods_root = Path(env(MODS_ROOT))
    modrinth_api = env(MODRINTH_API).rstrip()
    project_slug = env(PROJECT_SLUG)
    bump = env(BUMP)
    version_override = os.environ.get(VERSION_OVERRIDE, ).strip()
    include_mods = os.environ.get(INCLUDE_MODS, true).lower() == true
    changelog_body = env(CHANGELOG).strip()
    description = os.environ.get(DESCRIPTION, )
    license_text = os.environ.get(LICENSE, See LICENSE.txt)
    targets = [l.strip() for l in env(TARGETS).splitlines() if l.strip()]
    out_dir = Path(os.environ.get(OUT_DIR, builddist))

    if not changelog_body
        raise SystemExit(CHANGELOG is empty)

    lookup = parse_lookup(lookup_file)
    for t in targets
        if t not in lookup
            raise SystemExit(fLabel not in lookup {t})

    ua = f{project_slug}-build1.0
    project = http_json(f{modrinth_api}project{project_slug}, ua)
    project_versions = http_json(f{modrinth_api}project{project_slug}version, ua) or []
    title = project.get(title) or project_slug

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        project_slug project_slug,
        project_id project[id],
        project_title title,
        entries [],
    }

    for label in targets
        row = lookup[label]
        print(fn=== {label} ===)
        content, meta_template, meta_name = select_paths(
            content_root, meta_root, row[game_versions]
        )
        print(fContent {content})
        print(fMeta    {meta_template})

        ver_num, prev = resolve_version_number(
            row, project_versions, bump, version_override
        )
        changelog = build_changelog(ver_num, prev, bump, changelog_body)
        safe_label = label.replace(, -)
        file_name = f{project_slug}-{safe_label}-{ver_num}.zip
        zip_path = out_dir  file_name
        use_mods = include_mods and row[pack_format] is not None
        zip_pack(
            content,
            meta_template,
            meta_name,
            row[pack_format],
            use_mods,
            mods_root,
            description,
            license_text,
            zip_path,
        )

        manifest[entries].append(
            {
                label label,
                version_number ver_num,
                previous_version prev,
                name f{title} {label},
                game_versions row[game_versions],
                changelog changelog,
                file file_name,
            }
        )
        print(fBuilt {zip_path})
        print(changelog)

    (out_dir  manifest.json).write_text(json.dumps(manifest, indent=2) + n)
    print(nWrote manifest.json)

if __name__ == __main__
    main()