#!/usr/bin/env python3
"""make_assets.py -- the launcher's asset pipeline (v2 / M0).

Keeps ``overlay/v2/assets/`` honest and small.  It does three things:

  * writes ``assets/manifest.json`` -- name, bytes and sha256 of every asset, so
    a build can tell what shipped and detect silent changes;
  * ``--icons`` renders the sizes the app actually asks for from the source art
    (``logo.png``) into ``assets/icons/`` -- no hand-cut PNGs in the repo;
  * fails if any single asset is large enough to bloat the EXE.

    python tools/make_assets.py            # refresh the manifest
    python tools/make_assets.py --icons    # also (re)render derived icons
    python tools/make_assets.py --check    # verify, change nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "overlay" / "v2" / "assets"
SOURCE_LOGO = ROOT / "logo.png"
ICON_SIZES = (16, 32, 48, 64, 128, 256)
MAX_ASSET_BYTES = 2 * 1024 * 1024


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_assets():
    if not ASSETS.is_dir():
        return
    for p in sorted(ASSETS.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            yield p


def build_manifest() -> dict:
    entries = {}
    for p in iter_assets():
        rel = p.relative_to(ASSETS).as_posix()
        entries[rel] = {"bytes": p.stat().st_size, "sha256": sha256(p)}
    return {"version": 1, "count": len(entries), "files": entries}


def render_icons() -> list[str]:
    if not SOURCE_LOGO.is_file():
        print(f"! source art missing: {SOURCE_LOGO}", file=sys.stderr)
        return []
    try:
        from PIL import Image
    except Exception as e:
        print(f"! needs Pillow to render icons ({e})", file=sys.stderr)
        return []
    out_dir = ASSETS / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    with Image.open(SOURCE_LOGO) as img:
        rgba = img.convert("RGBA")
        for size in ICON_SIZES:
            dest = out_dir / f"icon_{size}.png"
            rgba.resize((size, size), Image.LANCZOS).save(dest)
            made.append(dest.relative_to(ASSETS).as_posix())
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--icons", action="store_true", help="render derived icon sizes from logo.png")
    ap.add_argument("--check", action="store_true", help="verify only; never write")
    args = ap.parse_args()

    ASSETS.mkdir(parents=True, exist_ok=True)

    made = render_icons() if args.icons and not args.check else []
    if made:
        print("rendered: " + ", ".join(made))

    manifest = build_manifest()
    if manifest["count"] == 0:
        print("assets/: empty (the launcher falls back to no assets)")

    oversized = [f for f, m in manifest["files"].items() if m["bytes"] > MAX_ASSET_BYTES]
    for f in oversized:
        print(f"! {f}: {manifest['files'][f]['bytes'] / 1048576:.1f} MB exceeds the "
              f"{MAX_ASSET_BYTES // 1048576} MB asset budget", file=sys.stderr)

    target = ASSETS / "manifest.json"
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.check:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current != text:
            print("manifest out of date (run without --check)", file=sys.stderr)
            return 1
        print(f"manifest OK ({manifest['count']} asset(s))")
    else:
        target.write_text(text, encoding="utf-8")
        total = sum(m["bytes"] for m in manifest["files"].values())
        print(f"manifest written: {manifest['count']} asset(s), "
              f"{total / 1024:.1f} KB total")

    return 1 if oversized else 0


if __name__ == "__main__":
    raise SystemExit(main())
