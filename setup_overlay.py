#!/usr/bin/env python3
"""
setup_overlay.py -- install this project on top of upstream PokemonRedExperiments.

This repo contains the added/changed files (in ./overlay), plus one patched
upstream file (v2/tensorboard_callback.py, see CREDITS.md). The original project
by Peter Whidden (MIT) is fetched from GitHub and the overlay is copied into its
v2/ folder; every other upstream file is left untouched.

    python setup_overlay.py                         # download upstream into ./PokemonRedExperiments
    python setup_overlay.py --dest D:\\pokemon       # choose the folder
    python setup_overlay.py --zip master.zip        # use a zip you already downloaded (no network)
    python setup_overlay.py --ref <commit-sha>      # pin a specific upstream commit/branch/tag

Re-running is safe: upstream files are left alone if already present, overlay
files are refreshed, and your edited reward_config.json is never overwritten.
The ROM is never downloaded -- you must supply your own.
"""

import argparse
import hashlib
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

UPSTREAM = "PWhiddy/PokemonRedExperiments"
ROM_SHA1 = "ea9bcae617fdf159b045185467ae58b2e4a48b9a"  # from the upstream README
HERE = Path(__file__).resolve().parent
OVERLAY = HERE / "overlay"
PRESERVE_IF_EXISTS = {Path("v2/reward_config.json")}  # user-editable, never clobber


def get_zip_bytes(args):
    if args.zip:
        print(f"Using local zip: {args.zip}")
        return Path(args.zip).read_bytes()
    url = f"https://github.com/{UPSTREAM}/archive/{args.ref}.zip"
    print(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "pokemon-rl-launcher-setup"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def extract_upstream(data, dest):
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            parts = Path(member.filename).parts
            if len(parts) < 2:  # skip the zip's top-level folder entry itself
                continue
            target = (dest / Path(*parts[1:])).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                raise RuntimeError(f"Unsafe path in zip: {member.filename}")  # zip-slip guard
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)


def copy_overlay(dest):
    copied, kept = 0, []
    for src in sorted(OVERLAY.rglob("*")):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(OVERLAY)
        target = dest / rel
        if rel in PRESERVE_IF_EXISTS and target.exists():
            kept.append(str(rel))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1
    return copied, kept


def check_rom(dest):
    rom = dest / "PokemonRed.gb"
    if not rom.exists():
        print(f"\n[!] ROM missing: place your own legally obtained Pokemon Red ROM at\n    {rom}")
        return
    sha1 = hashlib.sha1(rom.read_bytes()).hexdigest()
    if sha1 == ROM_SHA1:
        print("\n[ok] ROM found, SHA-1 matches.")
    else:
        print(f"\n[!] ROM found but SHA-1 is {sha1}\n    expected {ROM_SHA1} -- may be a different revision.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default="PokemonRedExperiments", help="Install folder.")
    ap.add_argument("--ref", default="master", help="Upstream branch, tag or commit SHA (default master).")
    ap.add_argument("--zip", help="Use this local upstream zip instead of downloading.")
    args = ap.parse_args()

    if not OVERLAY.is_dir():
        sys.exit(f"overlay/ folder not found next to this script ({OVERLAY}).")

    dest = Path(args.dest)
    if (dest / "v2" / "red_gym_env_v2.py").exists():
        print(f"Upstream already present in {dest} -- reusing it.")
    else:
        dest.mkdir(parents=True, exist_ok=True)
        try:
            extract_upstream(get_zip_bytes(args), dest)
        except Exception as e:
            sys.exit(f"Could not obtain upstream: {e}\n"
                     f"Download it manually from https://github.com/{UPSTREAM} and re-run with --zip.")
        print(f"Upstream extracted to {dest.resolve()}")

    if not (dest / "v2" / "red_gym_env_v2.py").exists():
        sys.exit(f"{dest}/v2/red_gym_env_v2.py missing -- this doesn't look like the upstream repo.")

    copied, kept = copy_overlay(dest)
    print(f"Installed {copied} overlay file(s) into {dest / 'v2'}")
    for k in kept:
        print(f"  kept your existing {k}")

    check_rom(dest)

    v2 = dest / "v2"
    print(f"""
Next steps
  1. Install dependencies (Python 3.10-3.12; ffmpeg on PATH). You can use any
     environment -- including a conda env -- and the launcher will run training
     with it (use "Detect Environments..." there, or the setup wizard):
       cd {v2}
       pip install -r requirements.txt            # upstream's list (see its windows-setup-guide.md on Windows)
       pip install -r requirements-launcher.txt   # GUI extras
  2. Put your ROM at {dest / 'PokemonRed.gb'} (if not already there).
  3. Launch:
       python pokemon_launcher_gui.py             # GUI (Windows)
       python train_cli.py --headless --num-cpu 4 # or straight from the terminal
""")


if __name__ == "__main__":
    main()
