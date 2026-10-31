#!/usr/bin/env python3
"""motion_check.py -- CI-able gate for the launcher's motion engine (v2 / M2).

The motion engine makes one promise above all others: **animation may never
steal emulator throughput** while a training run is active.  That promise is
encoded in two ideas, and this gate pins both down:

1. **The spec is the contract.**  Every duration/easing lives in
   ``anim.MOTION_SPEC`` and must match the table in ``ROADMAP-v2.md`` §3.  A new
   animation needs a new row -- nothing may invent its own numbers.
2. **Skipping is not just "duration 0".**  Under *performance mode* (a run is
   active) or *reduce motion* (accessibility), a tween must call its target once
   with the **end** value and schedule **nothing** on the Tk event loop.  Toasts
   appear already placed; ``TweenNumber`` snaps; the busy progress bar parks.
   The lone exception is the "running" indicator (``breathe(always=True)``),
   which may keep pulsing during a run -- but reduce-motion still vetoes it.

    python tools/motion_check.py
    python tools/motion_check.py --json

Exit codes: 0 clean, 1 a check failed, 2 the tool could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V2 = ROOT / "overlay" / "v2"

# The motion spec, transcribed from ROADMAP-v2.md §3.  Keep it literal so a
# silent edit to anim.py cannot also silently edit the expectation.
SPEC_TABLE = {
    "page": (220, "easeOutCubic"),
    "dialog_open": (140, "easeOutQuad"),
    "dialog_close": (110, "easeOutQuad"),
    "toast_in": (180, "easeOutCubic"),
    "toast_out": (140, "easeOutCubic"),
    "button": (90, "linear"),
    "number": (300, "easeOutCubic"),
    "sparkline": (120, "easeInOutQuad"),
    "component": (200, "easeOutCubic"),
    "tile_border": (90, "linear"),
    "running_dot": (1400, "sine"),
    "retile": (120, "linear"),
    "progress": (900, "linear"),
}
PAGE_SLIDE = 12


class FakeWidget:
    """A Tk-widget stand-in that records ``after``/``configure`` traffic."""

    def __init__(self):
        self.after_calls = []
        self.cancelled = []
        self.configures = []
        self._n = 0

    def winfo_exists(self):
        return True

    def after(self, ms, fn=None):
        self._n += 1
        job = f"job{self._n}"
        self.after_calls.append((ms, fn, job))
        return job

    def after_cancel(self, job):
        self.cancelled.append(job)

    def configure(self, **kw):
        self.configures.append(kw)

    def reset(self):
        self.after_calls = []
        self.cancelled = []
        self.configures = []
        self._n = 0


def _import_anim():
    if str(V2) not in sys.path:
        sys.path.insert(0, str(V2))
    import theme as T   # noqa: F401  (anim imports it)
    import anim as A
    return A


def check_spec(A) -> list[str]:
    problems = []
    spec = A.MOTION_SPEC
    for name, (ms, easing) in SPEC_TABLE.items():
        row = spec.get(name)
        if not isinstance(row, dict):
            problems.append(f"MOTION_SPEC[{name!r}] missing from anim.py")
            continue
        if row.get("ms") != ms:
            problems.append(f"MOTION_SPEC[{name!r}].ms = {row.get('ms')!r}, §3 says {ms!r}")
        if row.get("easing") != easing:
            problems.append(f"MOTION_SPEC[{name!r}].easing = {row.get('easing')!r}, §3 says {easing!r}")
    if spec.get("page_slide") != PAGE_SLIDE:
        problems.append(f"MOTION_SPEC['page_slide'] = {spec.get('page_slide')!r}, §3 says {PAGE_SLIDE!r}")
    # Nothing may name an easing the engine cannot perform.
    for name, row in spec.items():
        if isinstance(row, dict) and row.get("easing") not in A.EASINGS:
            problems.append(f"MOTION_SPEC[{name!r}].easing {row.get('easing')!r} is not in EASINGS")
    return problems


def check_policy(A) -> list[str]:
    problems = []
    A.set_reduce_motion(False)
    A.set_performance(False)
    if not A.MOTION.enabled:
        problems.append("MOTION.enabled is False with both switches off")
    if A.MOTION.duration(220) != 220:
        problems.append("MOTION.duration(220) should be 220 when enabled")
    for flag, label in ((A.set_performance, "performance"), (A.set_reduce_motion, "reduce_motion")):
        flag(True)
        if A.MOTION.enabled:
            problems.append(f"MOTION.enabled is True while {label} is on")
        if A.MOTION.duration(220) != 0:
            problems.append(f"MOTION.duration(220) should be 0 while {label} is on")
        flag(False)
    A.set_reduce_motion(False)
    A.set_performance(False)
    return problems


def check_tween_collapses(A) -> list[str]:
    """A disabled tween applies the end value once and schedules nothing."""
    problems = []
    for flag, label in ((A.set_performance, "performance"), (A.set_reduce_motion, "reduce_motion")):
        w = FakeWidget()
        seen = []
        flag(True)
        animator = A.Animator(w, A.MOTION)
        animator.tween(lambda v: seen.append(v), 0.0, 1.0, duration=300, easing="easeOutCubic")
        flag(False)
        if seen != [1.0]:
            problems.append(f"disabled ({label}) tween called target with {seen!r}, expected [1.0]")
        if w.after_calls:
            problems.append(f"disabled ({label}) tween scheduled {len(w.after_calls)} frame(s)")

    # With motion on, the same call must actually pace itself.
    w = FakeWidget()
    seen = []
    animator = A.Animator(w, A.MOTION)
    tag = animator.tween(lambda v: seen.append(v), 0.0, 1.0, duration=300, easing="easeOutCubic")
    if not w.after_calls:
        problems.append("enabled tween scheduled no frame")
    if seen and seen[-1] == 1.0:
        problems.append("enabled tween jumped straight to the end value")
    animator.stop(tag)
    return problems


def check_breathe(A) -> list[str]:
    """``breathe`` is a no-op in perf mode unless ``always=True``; reduce-motion always wins.

    The launcher ships the running indicator *static* during a run (see
    ``_running_dot``), but ``always`` remains part of the API contract, so the
    engine behaviour is pinned here.
    """
    problems = []

    # Performance mode + always=True -> keeps pulsing (schedules frames).
    w = FakeWidget()
    A.set_reduce_motion(False)
    A.set_performance(True)
    stop = A.breathe(w, period=A.spec("running_dot")["ms"], always=True)
    if not w.after_calls:
        problems.append("breathe(always=True) did not pulse in performance mode")
    if not any("text_color" in c for c in w.configures):
        problems.append("breathe(always=True) never set its colour")
    stop()

    # Reduce-motion wins over always=True -> one colour, no frames.
    w.reset()
    A.set_reduce_motion(True)
    A.set_performance(True)
    stop = A.breathe(w, period=A.spec("running_dot")["ms"], always=True)
    if w.after_calls:
        problems.append("breathe(always=True) pulsed despite reduce-motion")
    stop()

    # The shipped running dot: always=False -> static in performance mode.
    w.reset()
    A.set_reduce_motion(False)
    A.set_performance(True)
    stop = A.breathe(w, period=A.spec("running_dot")["ms"])
    if w.after_calls:
        problems.append("breathe(always=False) pulsed in performance mode")
    if not any("text_color" in c for c in w.configures):
        problems.append("breathe(always=False) did not apply its resting colour")
    stop()

    A.set_reduce_motion(False)
    A.set_performance(False)
    return problems


def check_widgets(A) -> list[str]:
    """Toasts / TweenNumber / Progress park themselves when motion is off."""
    import tkinter as tk
    import customtkinter as ctk
    import theme as T

    T.enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    T.apply(appearance="dark", accent="blue")
    problems = []

    # -- Toasts: instant placement (no tween job) under performance mode.
    A.set_performance(True)
    toasts = A.Toasts(root, A.MOTION)
    toasts.show("hello", kind="ok", timeout=0)
    item = toasts._items[-1]
    if not item.get("shown"):
        problems.append("toast was not marked shown immediately under performance mode")
    if item["animator"]._jobs:
        problems.append("toast scheduled an animation job under performance mode")
    if abs(item.get("x", 0) - (-A.Toasts.MARGIN)) > 0.5:
        problems.append(f"toast x = {item.get('x')!r}, expected resting {-A.Toasts.MARGIN}")
    # And with motion on, it must tween in instead of appearing placed.
    toasts.clear()
    A.set_performance(False)
    toasts.show("hello", kind="ok", timeout=0)
    item = toasts._items[-1]
    if item.get("shown"):
        problems.append("toast snapped in with motion enabled")
    if not item["animator"]._jobs:
        problems.append("toast scheduled no animation with motion enabled")
    toasts.clear()

    # -- TweenNumber: snaps to the exact value when motion is off.
    A.set_performance(True)
    label = ctk.CTkLabel(root, text="")
    tn = A.TweenNumber(label, decimals=2)
    tn.set(1234.5)
    if label.cget("text") != "1,234.50":
        problems.append(f"TweenNumber snapped to {label.cget('text')!r}, expected '1,234.50'")

    # -- Progress: parks (no frames, idles at its resting value) when off.
    A.set_performance(True)
    prog = A.Progress(root, width=120, height=6)
    if prog._held:
        problems.append("Progress held a timer under performance mode")
    prog.destroy()
    label.destroy()

    A.set_performance(False)
    root.destroy()
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not (V2 / "anim.py").is_file():
        print(f"motion engine not found: {V2 / 'anim.py'}", file=sys.stderr)
        return 2

    A = _import_anim()
    failures = {
        "spec_drift": check_spec(A),
        "policy": check_policy(A),
        "tween_collapse": check_tween_collapses(A),
        "breathe": check_breathe(A),
        "widgets": check_widgets(A),
    }
    ok = not any(failures.values())

    if args.json:
        print(json.dumps({"ok": ok, "spec_rows": len(SPEC_TABLE), "failures": failures}, indent=2))
        return 0 if ok else 1

    print("=" * 66)
    print("Launcher motion check")
    print("=" * 66)
    print(f"motion spec rows match sec 3: {len(SPEC_TABLE)} checked, "
          f"{len(failures['spec_drift'])} bad")
    for line in failures["spec_drift"]:
        print(f"  FAIL  {line}")
    print(f"policy (perf/reduce collapse durations): "
          f"{'ok' if not failures['policy'] else str(len(failures['policy'])) + ' bad'}")
    for line in failures["policy"]:
        print(f"  FAIL  {line}")
    print(f"disabled tween collapses to end value: "
          f"{'ok' if not failures['tween_collapse'] else str(len(failures['tween_collapse'])) + ' bad'}")
    for line in failures["tween_collapse"]:
        print(f"  FAIL  {line}")
    print(f"breathe: always-pulse in perf, static dot, reduce-motion veto: "
          f"{'ok' if not failures['breathe'] else str(len(failures['breathe'])) + ' bad'}")
    for line in failures["breathe"]:
        print(f"  FAIL  {line}")
    print(f"toasts / counters / progress park themselves: "
          f"{'ok' if not failures['widgets'] else str(len(failures['widgets'])) + ' bad'}")
    for line in failures["widgets"]:
        print(f"  FAIL  {line}")
    print("-" * 66)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
