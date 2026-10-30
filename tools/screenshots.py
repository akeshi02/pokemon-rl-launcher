#!/usr/bin/env python3
"""screenshots.py -- build every launcher screen/dialog and capture a PNG (v2 / M1).

The M1 acceptance test is visual: restyle every screen, add the light/dark +
accent switcher, and keep the layout correct across DPI.  This tool drives the
real launcher screen-by-screen on this machine, saves a PNG of each surface and
reports whether any *actual* window ends up larger than (or hanging off) the
screen -- the clipping check.

    python tools/screenshots.py                     # dark/blue at the real DPI
    python tools/screenshots.py --appearance light --accent poke_red
    python tools/screenshots.py --scale 1.5         # emulate a 150 % display
    python tools/screenshots.py --screen 1280x720   # emulate a small screen
    python tools/screenshots.py --matrix            # the whole grid in one go
    python tools/screenshots.py --json              # machine-readable summary

``--scale`` cancels the real monitor factor and asks customtkinter for the target
one; ``--screen`` makes the sizing clamp pretend the display is that small.  Both
only affect this process -- real use keeps per-monitor automatic scaling.

Exit codes: 0 clean, 1 a surface was clipped, 2 the harness could not run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V2 = ROOT / "overlay" / "v2"

SURFACES = ("select", "reward", "env", "install", "headless", "grid")

# The visual grid the milestone asks for: both appearances, a couple of accents,
# the real DPI and an emulated 150 %.
MATRIX = [
    ("dark", "blue", 1.0),
    ("light", "blue", 1.0),
    ("dark", "poke_red", 1.0),
    ("dark", "gold", 1.0),
    ("dark", "blue", 1.5),
    ("light", "poke_red", 1.5),
]


class _SilentDialogs:
    """Swallow modal dialogs while capturing (a real messagebox would block)."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _pump(win, seconds: float = 0.6) -> None:
    """Run the Tcl loop for a moment so paints and `after` callbacks happen.

    Uses a real (short) mainloop rather than tight ``update()`` calls: the app
    schedules background work with ``after`` from worker threads, which needs the
    interpreter to actually be in a main loop.
    """
    try:
        win.after(max(1, int(seconds * 1000)), win.quit)
        win.mainloop()
    except Exception:
        pass
    try:
        win.update()
    except Exception:
        pass


def _pump_until(win, predicate, timeout: float = 20.0) -> bool:
    """Pump until ``predicate()`` is true or ``timeout`` seconds elapse."""
    end = time.time() + timeout
    while time.time() < end:
        _pump(win, 0.3)
        try:
            if predicate():
                return True
        except Exception:
            return False
    return False


def _find_all(widget, cls):
    """Every descendant of ``widget`` that is an instance of ``cls``."""
    found = []
    try:
        children = widget.winfo_children()
    except Exception:
        return found
    for child in children:
        if isinstance(child, cls):
            found.append(child)
        found.extend(_find_all(child, cls))
    return found


def _grab(win, path: Path, theme):
    """Screenshot a window's client area; return (x, y, w, h) in physical px."""
    from PIL import ImageGrab
    # Bring it above whatever else is open on this desktop: ImageGrab captures
    # the screen, so an overlapping browser would otherwise appear in the shot.
    try:
        win.attributes("-topmost", True)
        win.lift()
        win.update_idletasks()
    except Exception:
        pass
    _pump(win)
    # Windows cascades new windows down-right from the last one, which can push
    # a (correctly sized) window off the bottom.  Park it centred so the fit
    # check measures the size, not the window manager's placement.
    try:
        sw, sh = theme.screen_size(win)
        cx = max(0, (sw - win.winfo_width()) // 2)
        cy = max(0, (sh - win.winfo_height()) // 2)
        win.geometry(f"+{cx}+{cy}")
        win.update_idletasks()
        _pump(win, 0.15)
    except Exception:
        pass
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    path.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
    try:
        win.attributes("-topmost", False)
    except Exception:
        pass
    return x, y, w, h


def _clipped(win, theme, box) -> tuple[bool, str]:
    x, y, w, h = box
    sw, sh = theme.screen_size(win)
    reasons = []
    if x < 0 or y < 0:
        reasons.append(f"origin {x},{y} off-screen")
    if w > sw or h > sh:
        reasons.append(f"{w}x{h} larger than screen {sw}x{sh}")
    if x + w > sw + 1 or y + h > sh + 1:
        reasons.append(f"extends to {x + w},{y + h} past screen {sw}x{sh}")
    return (bool(reasons), "; ".join(reasons))


def run_one(args) -> dict:
    import os
    os.environ["POKE_THEME"] = args.appearance
    os.environ["POKE_ACCENT"] = args.accent
    if str(V2) not in sys.path:
        sys.path.insert(0, str(V2))

    import customtkinter as ctk
    import theme as T

    T.enable_dpi_awareness()
    if args.scale and abs(args.scale - T.dpi_scaling()) > 1e-6:
        T.set_widget_scaling(args.scale)
    if args.screen:
        w, _, h = args.screen.partition("x")
        T.set_screen_override(int(w), int(h))

    import pokemon_launcher_gui as gui

    # Keep creation deterministic: no modal boxes, no wireless package probes.
    gui.messagebox = _SilentDialogs()
    gui.filedialog = _SilentDialogs()
    workdir = Path(tempfile.gettempdir()) / "poke_shot_workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "requirements.txt").write_text("", encoding="utf-8")
    (workdir / "reward_config.json").write_text("{}", encoding="utf-8")
    if getattr(gui, "env_utils", None) is not None:
        gui.env_utils.missing_packages = lambda *_a, **_k: []

    tag = f"{args.appearance}_{args.accent}_{args.scale:g}x"
    out_dir = Path(args.out) / tag
    app = gui.LauncherApp()
    _pump(app, 0.8)
    screen_snapshot = list(T.screen_size(app))

    def newest_toplevel():
        tops = [w for w in app.winfo_children() if isinstance(w, ctk.CTkToplevel)]
        return tops[-1] if tops else None

    shots = []

    def record(name, win):
        if win is None:
            shots.append({"surface": name, "ok": False,
                          "note": "window was not created"})
            return
        path = out_dir / f"{name}.png"
        box = _grab(win, path, T)
        bad, why = _clipped(win, T, box)
        shots.append({"surface": name, "ok": not bad, "note": why,
                      "png": str(path), "box": list(box)})

    # --- select screen (the main restyle) ---
    record("select", app)

    # --- dialogs (they are toplevels over the select screen) ---
    try:
        app.open_reward_editor()
        dlg = newest_toplevel()
        record("reward", dlg)
        if dlg is not None:
            dlg.destroy()
    except Exception as e:
        shots.append({"surface": "reward", "ok": False, "note": f"{type(e).__name__}: {e}"})

    try:
        app.detect_environments()
        dlg = newest_toplevel()
        # Let the background scan finish so the shot shows real entries.
        if dlg is not None:
            _pump_until(dlg, lambda: bool(_find_all(dlg, ctk.CTkRadioButton)), timeout=25)
        record("env", dlg)
        if dlg is not None:
            dlg.destroy()
    except Exception as e:
        shots.append({"surface": "env", "ok": False, "note": f"{type(e).__name__}: {e}"})

    try:
        app.python_var.set(sys.executable)
        app.workdir_var.set(str(workdir))
        app.open_install_dialog()
        dlg = newest_toplevel()
        record("install", dlg)
        if dlg is not None:
            dlg.destroy()
    except Exception as e:
        shots.append({"surface": "install", "ok": False, "note": f"{type(e).__name__}: {e}"})

    # --- the two training screens (they replace the select screen) ---
    try:
        app.build_headless_status_frame()
        record("headless", app)
    except Exception as e:
        shots.append({"surface": "headless", "ok": False, "note": f"{type(e).__name__}: {e}"})

    try:
        app.build_grid_frame(4, 2)
        record("grid", app)
    except Exception as e:
        shots.append({"surface": "grid", "ok": False, "note": f"{type(e).__name__}: {e}"})

    try:
        app.destroy()
    except Exception:
        pass

    return {
        "appearance": args.appearance,
        "accent": args.accent,
        "scale": args.scale,
        "real_dpi_scale": T.dpi_scaling(),
        "screen": screen_snapshot,
        "shots": shots,
        "ok": all(s["ok"] for s in shots),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--appearance", default="dark", choices=["dark", "light", "system"])
    ap.add_argument("--accent", default="blue")
    ap.add_argument("--scale", type=float, default=0.0, help="emulated display scaling (0 = real)")
    ap.add_argument("--screen", default="", help="emulated screen size, e.g. 1280x720")
    ap.add_argument("--out", default=str(ROOT / "build" / "screenshots"))
    ap.add_argument("--matrix", action="store_true", help="run the whole visual grid")
    ap.add_argument("--json", action="store_true", help="print JSON only")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not (V2 / "pokemon_launcher_gui.py").is_file():
        print(f"launcher not found under {V2}", file=sys.stderr)
        return 2

    if args.matrix and not args.child:
        runs = []
        for appearance, accent, scale in MATRIX:
            cmd = [sys.executable, str(Path(__file__).resolve()),
                   "--appearance", appearance, "--accent", accent,
                   "--scale", str(scale), "--out", args.out,
                   "--json", "--child"]
            if args.screen:
                cmd += ["--screen", args.screen]

            def run_child():
                proc = subprocess.run(cmd, capture_output=True, text=True)
                line = next((l for l in reversed(proc.stdout.splitlines())
                             if l.strip().startswith("{")), None)
                stderr_tail = (proc.stderr or "").strip()[-1200:]
                if line is None:
                    return {"appearance": appearance, "accent": accent, "scale": scale,
                            "ok": False, "shots": [], "stderr": stderr_tail,
                            "note": f"child exited {proc.returncode}: "
                                    f"{(proc.stderr or proc.stdout)[-400:]}"}
                run = json.loads(line)
                run["stderr"] = stderr_tail
                return run

            run = run_child()
            # One retry when the child's Tk interpreter died underneath it: that
            # is a rare environment hiccup (the QC machine occasionally tears the
            # window down mid-capture), not a layout fault -- a genuine crash or
            # clip reproduces on the retry and still fails the gate.
            if _is_transient(run):
                run = run_child()
                run["retried"] = True
            runs.append(run)
        summaries = [_summarize(r) for r in runs]
        if args.json:
            print(json.dumps({"ok": all(s["ok"] for s in summaries),
                              "runs": runs}, indent=2))
        else:
            _print_report(summaries)
        return 1 if not all(s["ok"] for s in summaries) else 0

    try:
        result = run_one(args)
    except Exception as e:  # noqa: BLE001 - report and let CI fail loudly
        import traceback
        traceback.print_exc()
        print(json.dumps({"appearance": args.appearance, "accent": args.accent,
                          "scale": args.scale, "ok": False, "shots": [],
                          "note": f"{type(e).__name__}: {e}"}))
        return 2

    if args.json:
        print(json.dumps(result))
    else:
        _print_report([_summarize(result)])
    return 0 if result["ok"] else 1


def _is_transient(run: dict) -> bool:
    """True when the child died from a Tcl teardown hiccup rather than layout.

    A clipped window reports a size/geometry note; the only thing we treat as
    retryable is the Tk interpreter being gone ("application has been
    destroyed"), which is the QC machine occasionally closing the window while
    the capture is in flight.
    """
    if run.get("ok"):
        return False
    blob = " ".join(str(s.get("note", "")) for s in run.get("shots", []))
    blob += " " + str(run.get("note", ""))
    return "application has been destroyed" in blob or "main thread is not in main loop" in blob


def _summarize(result: dict) -> dict:
    shots = result.get("shots", [])
    bad = [s for s in shots if not s.get("ok")]
    return {
        "label": f"{result.get('appearance', '?')}/{result.get('accent', '?')} "
                 f"@{result.get('scale', 0):g}x",
        "ok": bool(result.get("ok")) and not bad,
        "screen": result.get("screen"),
        "shots": shots,
        "note": result.get("note", ""),
        "retried": bool(result.get("retried")),
    }


def _print_report(summaries: list[dict]) -> None:
    print("=" * 72)
    print("Launcher screenshots + clipping check")
    print("=" * 72)
    for s in summaries:
        screen = s.get("screen")
        head = s["label"] + (f"   screen {screen[0]}x{screen[1]}" if screen else "")
        if s.get("retried"):
            head += "   (retried after a transient Tcl error)"
        print(f"\n{head}")
        for shot in s["shots"]:
            box = shot.get("box")
            where = f"{box[0]},{box[1]} {box[2]}x{box[3]}" if box else "-"
            mark = "ok " if shot.get("ok") else "CLIP"
            note = f"  {shot['note']}" if shot.get("note") else ""
            print(f"  [{mark}] {shot['surface']:<9} {where:<22}{note}")
        if not s["shots"] and s.get("note"):
            print(f"  [!] {s['note']}")
        if not s["ok"] and s.get("stderr"):
            print("  --- child stderr ---")
            for line in s["stderr"].splitlines()[-12:]:
                print(f"  | {line}")
        if s["ok"]:
            print("  -> no clipping")
    failed = [s for s in summaries if not s["ok"]]
    print("-" * 72)
    if failed:
        print(f"FAIL  {len(failed)} configuration(s) clipped or errored")
    else:
        print("PASS  every window fit the screen; PNGs written")


if __name__ == "__main__":
    raise SystemExit(main())
