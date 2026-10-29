#!/usr/bin/env python3
"""Build sound pack manifests for GitHub Pages.

For every packs/<id>/pack.yml this script:
  * converts each source audio file in packs/<id>/ to AAC at packs/<id>/sounds/<slug>.m4a
  * rejects any sound longer than MAX_DURATION seconds
  * writes packs/<id>/pack.json, bumping "version" when anything in it changes
and then writes index.json at the repository root.

Requires only the Python standard library and macOS's afconvert/afinfo.
Run with:  python3 build.py
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKS_DIR = ROOT / "packs"
INDEX_PATH = ROOT / "index.json"
# Remembers which source produced each .m4a so unchanged sounds aren't re-encoded
# (re-encoding could produce different bytes and cause a spurious version bump).
CACHE_PATH = ROOT / ".build-cache.json"

FORMAT_VERSION = 1
MAX_DURATION = 60.0
SOURCE_EXTENSIONS = {".wav", ".aif", ".aiff", ".aifc", ".caf", ".mp3", ".m4a", ".aac", ".flac"}
AFCONVERT_ARGS = ["-f", "m4af", "-d", "aac", "-b", "128000"]
PACK_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class BuildError(Exception):
    pass


# --- Minimal YAML subset ----------------------------------------------------
# Supports nested mappings, lists of scalars, lists of mappings, comments and
# single-line plain/quoted scalars -- enough for pack.yml.

def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(text, where):
    text = text.strip()
    if text == "" or text in ("~", "null"):
        return None
    if text.startswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise BuildError(f"{where}: bad double-quoted string {text}")
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise BuildError(f"{where}: bad single-quoted string {text}")
        return text[1:-1].replace("''", "'")
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text[0] in "[{|>&*!":
        raise BuildError(f"{where}: unsupported YAML syntax {text!r}")
    return text


def _split_key(text):
    m = re.match(r"^([A-Za-z0-9_-]+):(?:\s+(.*)|)$", text)
    return (m.group(1), m.group(2) or "") if m else None


def parse_yaml(source, filename):
    lines = []
    for n, raw in enumerate(source.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise BuildError(f"{filename}:{n}: tabs are not allowed for indentation")
        text = _strip_comment(raw).rstrip()
        if text.strip():
            lines.append([len(text) - len(text.lstrip()), text.strip(), n])
    if not lines:
        return {}

    def where(i):
        return f"{filename}:{lines[i][2]}"

    def parse_block(i, indent):
        if lines[i][1].startswith("- ") or lines[i][1] == "-":
            return parse_list(i, indent)
        return parse_map(i, indent)

    def parse_list(i, indent):
        items = []
        while i < len(lines) and lines[i][0] == indent and (lines[i][1] == "-" or lines[i][1].startswith("- ")):
            rest = lines[i][1][1:].lstrip()
            if not rest:
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    value, i = parse_block(i + 1, lines[i + 1][0])
                else:
                    value, i = None, i + 1
            elif _split_key(rest):
                # "- key: value" starts a mapping indented to where "key" begins.
                offset = indent + (len(lines[i][1]) - len(rest))
                lines[i] = [offset, rest, lines[i][2]]
                value, i = parse_map(i, offset)
            else:
                value, i = _scalar(rest, where(i)), i + 1
            items.append(value)
        return items, i

    def parse_map(i, indent):
        result = {}
        while i < len(lines) and lines[i][0] == indent:
            kv = _split_key(lines[i][1])
            if not kv:
                raise BuildError(f"{where(i)}: expected 'key: value', got {lines[i][1]!r}")
            key, rest = kv
            if key in result:
                raise BuildError(f"{where(i)}: duplicate key {key!r}")
            if rest:
                result[key], i = _scalar(rest, where(i)), i + 1
            elif i + 1 < len(lines) and (
                lines[i + 1][0] > indent
                or (lines[i + 1][0] == indent and lines[i + 1][1].startswith("-"))
            ):
                result[key], i = parse_block(i + 1, lines[i + 1][0])
            else:
                result[key], i = None, i + 1
        return result, i

    value, i = parse_block(0, lines[0][0])
    if i != len(lines):
        raise BuildError(f"{where(i)}: unexpected indentation")
    return value


# --- Helpers ----------------------------------------------------------------

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(stem):
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "sound"


def default_name(stem):
    return re.sub(r"[_\-]+", " ", stem).strip().title()


def duration_of(path):
    try:
        out = subprocess.run(["afinfo", str(path)], capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise BuildError(f"{path.relative_to(ROOT)}: afinfo could not read file: {e.stderr.strip()}")
    m = re.search(r"estimated duration:\s*([\d.]+)", out)
    if not m:
        raise BuildError(f"{path.relative_to(ROOT)}: could not determine duration")
    return float(m.group(1))


def convert(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_out = Path(tmp) / "out.m4a"
        result = subprocess.run(["afconvert", *AFCONVERT_ARGS, str(src), str(tmp_out)], capture_output=True, text=True)
        if result.returncode != 0:
            raise BuildError(f"{src.relative_to(ROOT)}: afconvert failed: {result.stderr.strip()}")
        shutil.move(str(tmp_out), dest)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, data):
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


# --- Pack building ----------------------------------------------------------

def read_pack_config(pack_dir):
    yml = pack_dir / "pack.yml"
    config = parse_yaml(yml.read_text(), str(yml.relative_to(ROOT)))
    if not isinstance(config, dict):
        raise BuildError(f"{yml.relative_to(ROOT)}: top level must be a mapping")

    def require_str(obj, key, label):
        value = obj.get(key) if isinstance(obj, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise BuildError(f"{yml.relative_to(ROOT)}: '{label}' is required and must be text")
        return value.strip()

    name = require_str(config, "name", "name")
    description = require_str(config, "description", "description")
    palette = config.get("palette")
    palette_name = require_str(palette, "name", "palette.name")
    colors = palette.get("colors")
    if not isinstance(colors, list) or not colors:
        raise BuildError(f"{yml.relative_to(ROOT)}: 'palette.colors' must be a non-empty list")
    for c in colors:
        if not isinstance(c, str) or not HEX_RE.match(c):
            raise BuildError(
                f"{yml.relative_to(ROOT)}: palette color {c!r} must look like \"#RRGGBB\" "
                "(quote it -- an unquoted # starts a YAML comment)"
            )

    sounds = config.get("sounds") or []
    if not isinstance(sounds, list) or not all(isinstance(s, dict) for s in sounds):
        raise BuildError(f"{yml.relative_to(ROOT)}: 'sounds' must be a list of mappings with a 'file' key")

    return {
        "name": name,
        "description": description,
        "palette": {"name": palette_name, "colorsHex": [c.upper() for c in colors]},
        "sounds": sounds,
    }


def plan_sounds(pack_dir, config):
    """Return [(source_path, display_name, color_index)] in display order."""
    rel = pack_dir.relative_to(ROOT)
    sources = {p.name: p for p in pack_dir.iterdir() if p.is_file() and p.suffix.lower() in SOURCE_EXTENSIONS}
    n_colors = len(config["palette"]["colorsHex"])
    plan, used = [], set()

    for entry in config["sounds"]:
        file = entry.get("file")
        if file not in sources:
            raise BuildError(f"{rel}/pack.yml: sound file {file!r} not found in {rel}/")
        if file in used:
            raise BuildError(f"{rel}/pack.yml: sound file {file!r} listed twice")
        used.add(file)
        plan.append((sources[file], entry.get("name"), entry.get("color")))
    for file in sorted(set(sources) - used, key=str.lower):
        plan.append((sources[file], None, None))

    if not plan:
        raise BuildError(f"{rel}: no audio files found")

    result = []
    for i, (src, name, color) in enumerate(plan):
        if name is None:
            name = default_name(src.stem)
        elif not isinstance(name, str) or not name.strip():
            raise BuildError(f"{rel}/pack.yml: name for {src.name!r} must be text")
        if color is None:
            color = i % n_colors
        elif not isinstance(color, int) or isinstance(color, bool) or not 0 <= color < n_colors:
            raise BuildError(f"{rel}/pack.yml: color for {src.name!r} must be an index 0..{n_colors - 1}")
        result.append((src, str(name).strip(), color))

    slugs = {}
    for src, _, _ in result:
        slug = slugify(src.stem)
        if slug in slugs:
            raise BuildError(f"{rel}: {slugs[slug]!r} and {src.name!r} would both become sounds/{slug}.m4a")
        slugs[slug] = src.name
    return result


def build_pack(pack_dir, cache):
    pack_id = pack_dir.name
    if not PACK_ID_RE.match(pack_id):
        raise BuildError(f"packs/{pack_id}: pack id must be lowercase letters, digits and hyphens")
    config = read_pack_config(pack_dir)
    plan = plan_sounds(pack_dir, config)

    too_long = [(src, d) for src, _, _ in plan if (d := duration_of(src)) > MAX_DURATION]
    if too_long:
        details = "\n".join(f"  {s.relative_to(ROOT)}: {d:.1f} s" for s, d in too_long)
        raise BuildError(f"sounds longer than {MAX_DURATION:.0f} s are not allowed:\n{details}")

    out_dir = pack_dir / "sounds"
    settings = " ".join(AFCONVERT_ARGS)
    sounds, keep = [], set()
    for src, name, color in plan:
        out = out_dir / f"{slugify(src.stem)}.m4a"
        key = str(out.relative_to(ROOT))
        src_hash = sha256_of(src)
        cached = cache.get(key)
        if not (
            out.exists()
            and cached
            and cached.get("source") == src_hash
            and cached.get("settings") == settings
            and cached.get("output") == sha256_of(out)
        ):
            print(f"  converting {src.relative_to(ROOT)}")
            convert(src, out)
        out_hash = sha256_of(out)
        cache[key] = {"source": src_hash, "settings": settings, "output": out_hash}
        keep.add(key)
        sounds.append({
            "name": name,
            "file": f"sounds/{out.name}",
            "bytes": out.stat().st_size,
            "sha256": out_hash,
            "colorIndex": color,
        })

    # Remove encodings whose source files are gone.
    if out_dir.exists():
        for stale in out_dir.glob("*.m4a"):
            if str(stale.relative_to(ROOT)) not in keep:
                print(f"  removing stale {stale.relative_to(ROOT)}")
                stale.unlink()
                cache.pop(str(stale.relative_to(ROOT)), None)

    manifest = {
        "formatVersion": FORMAT_VERSION,
        "id": pack_id,
        "name": config["name"],
        "description": config["description"],
        "version": 1,
        "palette": config["palette"],
        "sounds": sounds,
    }
    previous = load_json(pack_dir / "pack.json", None)
    if previous:
        old_version = previous.get("version", 0)
        unchanged = {k: v for k, v in previous.items() if k != "version"} == {
            k: v for k, v in manifest.items() if k != "version"
        }
        manifest["version"] = old_version if unchanged else old_version + 1
        if not unchanged:
            print(f"  version {old_version} -> {manifest['version']}")
    return manifest


def main():
    cache = load_json(CACHE_PATH, {})
    pack_dirs = sorted(p for p in PACKS_DIR.iterdir() if (p / "pack.yml").is_file()) if PACKS_DIR.exists() else []
    manifests, errors = [], []
    for pack_dir in pack_dirs:
        print(f"{pack_dir.name}:")
        try:
            manifests.append(build_pack(pack_dir, cache))
        except BuildError as e:
            errors.append(f"{pack_dir.name}: {e}")

    if errors:
        print("\nBuild failed:", file=sys.stderr)
        for e in errors:
            print(f"- {e}", file=sys.stderr)
        print("index.json was not updated.", file=sys.stderr)
        sys.exit(1)

    # Only write manifests once every pack has built, so pack.json and index.json stay in sync.
    for m in manifests:
        write_json(PACKS_DIR / m["id"] / "pack.json", m)

    # Drop cache entries for packs that no longer exist.
    cache = {k: v for k, v in cache.items() if (ROOT / k).exists()}
    write_json(CACHE_PATH, dict(sorted(cache.items())))

    index = {
        "formatVersion": FORMAT_VERSION,
        "packs": [
            {
                "id": m["id"],
                "name": m["name"],
                "description": m["description"],
                "version": m["version"],
                "palette": m["palette"],
                "soundCount": len(m["sounds"]),
                "totalBytes": sum(s["bytes"] for s in m["sounds"]),
                "manifest": f"packs/{m['id']}/pack.json",
            }
            for m in manifests
        ],
    }
    write_json(INDEX_PATH, index)
    print(f"\nWrote index.json with {len(manifests)} pack(s).")


if __name__ == "__main__":
    main()
