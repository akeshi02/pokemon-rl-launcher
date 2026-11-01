#!/usr/bin/env python3
"""theme_check.py -- CI-able gate for the launcher's visual system (v2 / M0+M1).

Three invariants the UI has to keep, and that are easy to break by accident:

1. **No hardcoded visual literals** in the launcher GUI -- every colour, font and
   radius goes through ``theme.py`` so restyling stays a one-file change.
2. **Every token is a real colour** for both Tk and matplotlib, across both
   appearances and all accent presets.
3. **The classic "blue" accent is byte-identical to 1.0.0** -- the dark+blue
   tokens resolve to the old literals and customtkinter's own widget theme is
   left untouched, so shipping M1 does not silently restyle existing users.

    python tools/theme_check.py
    python tools/theme_check.py --json

Exit codes: 0 clean, 1 a check failed, 2 the tool could not run.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V2 = ROOT / "overlay" / "v2"
GUI = V2 / "pokemon_launcher_gui.py"
DASHBOARD = V2 / "dashboard.py"
TILES = V2 / "tiles.py"

# Every UI module whose literals must go through theme.py.
SCANNED = (GUI, DASHBOARD, TILES)

# (regex, why it is allowed) -- layout numbers that are not on a shared scale.
ALLOWED_LITERALS = [
    (r"corner_radius=2\b", "half of a 4 px accent bar => fully rounded pill"),
    (r"corner_radius=3\b", "half of a 6 px progress bar => fully rounded pill"),
    (r"padx=40\b", "one-off inset for the mini chart"),
    (r"pady=1\b", "1 px gap between stacked reward-component rows"),
    (r"wraplength=\d+", "wraps relative to its dialog's width"),
]

LITERAL_PATTERNS = [
    (r"T\.f\(\s*\d", "font literal T.f(number)"),
    (r"font=\s*\(\s*[\"']", "raw font tuple"),
    (r"text_color=[\"'](?!transparent)", "raw text_color"),
    (r"fg_color=[\"'](?!transparent)", "raw fg_color"),
    (r"corner_radius=\d", "corner_radius number"),
    (r"pad[xy]=\d", "padx/pady number"),
    (r"wraplength=\d", "wraplength number"),
    (r"#(?:[0-9a-fA-F]{3}){1,2}\b", "hex colour"),
]

ACCENTS = ("blue", "poke_red", "gold", "green", "dmg")
APPEARANCES = ("dark", "light")

# Tokens whose dark value must still equal the 1.0.0 literal (M0 guarantee).
ONE_DOT_OHO = {
    "accent": "#3b8ed0",
    "accent_hover": "#1f6aa5",
    "text_dim": "#b3b3b3",   # gray70
    "text_faint": "#999999",  # gray60
    "grid_bg": "#000000",
    "chart_bg": "#2b2b2b",
    "chart_grid": "#555555",
    "ok": "#5ac47a",
    "warn": "#e0a15a",
    "danger": "#e05a5a",
}


def check_literals() -> list[str]:
    problems = []
    for path in SCANNED:
        lines = path.read_text(encoding="utf-8").splitlines()
        for pattern, label in LITERAL_PATTERNS:
            rx = re.compile(pattern)
            for i, line in enumerate(lines, 1):
                if not rx.search(line):
                    continue
                if any(re.search(allow, line) for allow, _ in ALLOWED_LITERALS):
                    continue
                problems.append(f"{path.name}:{i}: {label}: {line.strip()[:80]}")
    return problems


def _import_gui_stack():
    import tkinter as tk
    import customtkinter as ctk
    from matplotlib import colors as mcolors
    if str(V2) not in sys.path:
        sys.path.insert(0, str(V2))
    import theme as T
    return tk, ctk, mcolors, T


def check_tokens() -> tuple[list[str], int]:
    tk, ctk, mcolors, T = _import_gui_stack()
    root = tk.Tk()
    root.withdraw()
    problems = []
    names = list(T.TOKENS) + ["accent", "accent_hover", "accent_soft", "on_accent"]
    for appearance in APPEARANCES:
        for accent in ACCENTS:
            T.apply(appearance=appearance, accent=accent)
            for name in names:
                try:
                    value = T.col(name)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{appearance}/{accent} {name}: not resolvable ({e})")
                    continue
                try:
                    mcolors.to_rgba(value)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"matplotlib rejects {appearance}/{accent} {name}={value!r}: {e}")
                try:
                    root.winfo_rgb(value)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"Tk rejects {appearance}/{accent} {name}={value!r}: {e}")
    root.destroy()
    return problems, len(names) * len(APPEARANCES) * len(ACCENTS)


def check_blue_unchanged() -> list[str]:
    tk, ctk, mcolors, T = _import_gui_stack()
    ctk.set_default_color_theme("blue")
    stock = copy.deepcopy(ctk.ThemeManager.theme)
    problems = []

    T.apply(appearance="dark", accent="blue")
    if ctk.ThemeManager.theme != stock:
        for key, node in stock.items():
            now = ctk.ThemeManager.theme.get(key)
            if now != node:
                problems.append(f"blue preset changed customtkinter's {key}: {node} -> {now}")

    for name, expected in ONE_DOT_OHO.items():
        got = T.col(name)
        want = expected.lower()
        if got.lower() != want:
            problems.append(f"dark/blue {name} = {got!r}, 1.0.0 used {expected!r}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    for path in SCANNED:
        if not path.is_file():
            print(f"UI module not found: {path}", file=sys.stderr)
            return 2

    literals = check_literals()
    tokens, token_count = check_tokens()
    blue = check_blue_unchanged()
    failures = {"hardcoded_literals": literals, "invalid_tokens": tokens, "blue_changed": blue}
    ok = not any(failures.values())

    if args.json:
        print(json.dumps({"ok": ok, "tokens_checked": token_count, "failures": failures}, indent=2))
        return 0 if ok else 1

    print("=" * 66)
    print("Launcher theme check")
    print("=" * 66)
    print(f"hardcoded literals in {', '.join(p.name for p in SCANNED)}: {len(literals)}")
    for line in literals:
        print(f"  FAIL  {line}")
    print(f"colour tokens valid for Tk + matplotlib: {token_count} checked, {len(tokens)} bad")
    for line in tokens:
        print(f"  FAIL  {line}")
    print("dark/blue == 1.0.0 (tokens + customtkinter theme):",
          "ok" if not blue else f"{len(blue)} drift(s)")
    for line in blue:
        print(f"  FAIL  {line}")
    print("-" * 66)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
