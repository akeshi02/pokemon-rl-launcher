"""ui_selftest.py -- drive the motion engine without a training run (v2 / M0).

This is what ``tools/perf_budget.py`` launches.  It builds a window holding the
same *number* of animated widgets the roadmap allows at once (<= 12), runs the
engine in one of two modes, and reports the achieved frame rate as JSON on the
last stdout line.

    python ui_selftest.py --seconds 5 --mode animated
    python ui_selftest.py --seconds 5 --mode perf      # animation disabled

--mode perf mirrors the shipped default: nothing animates while training runs.
The CPU cost of both modes is measured by tools/perf_budget.py, not here.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import theme as T          # noqa: E402
import anim as A           # noqa: E402

MAX_ANIMATED_WIDGETS = 12


def build(root, widgets: int, motion):
    """Create the widget set the perf test animates, and the loops to drive it."""
    import customtkinter as ctk

    holder = ctk.CTkFrame(root, fg_color="transparent")
    holder.pack(fill="both", expand=True, padx=16, pady=16)

    header = ctk.CTkLabel(holder, text="ui_selftest", font=T.f("h1", "bold"))
    header.pack(anchor="w")
    ctk.CTkFrame(holder, height=2, fg_color=T.c("accent")).pack(fill="x", pady=(8, 12))

    cards = []
    grid = ctk.CTkFrame(holder, fg_color="transparent")
    grid.pack(fill="x")
    for i in range(widgets):
        card = ctk.CTkFrame(grid, corner_radius=T.RADIUS["card"], border_width=1,
                            border_color=T.c("border"), width=120, height=54)
        card.grid(row=i // 4, column=i % 4, padx=6, pady=6)
        card.grid_propagate(False)
        ctk.CTkLabel(card, text=f"card {i}", font=T.f("small")).pack(expand=True)
        cards.append(card)

    counter = ctk.CTkLabel(holder, text="0", font=T.f("timer", "bold"),
                           text_color=T.c("accent"))
    counter.pack(pady=(10, 4))

    # A pure-tk Canvas sparkline: M3 replaces matplotlib with something like this.
    spark = _make_sparkline(holder)

    toasts = A.Toasts(root, motion=motion)
    number = A.TweenNumber(counter, duration=300, motion=motion)

    state = {"frames": 0, "t0": time.perf_counter(), "stop": False}

    def animate_cards(i=0):
        if state["stop"] or not cards:
            return
        card = cards[i % len(cards)]
        start = T.col("card")
        end = T.col("accent_soft")
        A.Animator(card, motion).tween_props(
            {"fg_color": end}, duration=200, easing="easeInOutQuad",
            to_from={"fg_color": start},
            tag=f"card:{i % len(cards)}")
        root.after(240, animate_cards, i + 1)

    def tick():
        """The one loop that runs every frame (30 fps): a cheap counter tween."""
        if state["stop"]:
            return
        began = time.perf_counter()
        state["frames"] += 1
        number.set(state["frames"] * 7)
        work_ms = (time.perf_counter() - began) * 1000.0
        root.after(max(1, motion.frame_ms() - int(work_ms)), tick)

    def spark_loop():
        """Sparkline at the spec'd ~120 ms cadence, not per frame."""
        if state["stop"]:
            return
        spark.append(state["frames"] % 40 / 40.0)
        root.after(120, spark_loop)

    def poll():
        if state["stop"]:
            return
        # Emulates the launcher's 2 s status read (a small JSON file).
        try:
            json.loads('{"steps": 1, "mean_reward": 0.0, "components": {"explore": 0.1}}')
        except Exception:
            pass
        root.after(2000, poll)

    def timer_loop():
        """The 1 Hz clock the real launcher keeps even with motion disabled."""
        if state["stop"]:
            return
        try:
            elapsed = int(time.perf_counter() - state["t0"])
            header.configure(text=f"ui_selftest  {elapsed // 60:02d}:{elapsed % 60:02d}")
        except Exception:
            return
        root.after(1000, timer_loop)

    toasts.show("Motion engine online", kind="ok", timeout=0)
    toasts.show("Second toast, stacked", kind="info", timeout=0)
    toasts.show("Third: unread incoming message", kind="warn", timeout=0)

    A.breathe(header, period=1400, motion=motion)
    # The "running" indicator ships *static* while a run is active (the <2 % CPU
    # gate forbids a per-frame colour pulse during perf mode), so model it that
    # way: it pulses only in the animated demo, exactly like the real launcher.
    run_dot = ctk.CTkLabel(holder, text="●", font=T.f("body_bold"),
                           text_color=T.c("accent"))
    run_dot.pack()
    A.breathe(run_dot, motion=motion)
    # A busy progress bar: animates when motion is on, parks itself when off.
    progress = A.Progress(holder, motion=motion, width=200, height=6)
    progress.pack(pady=(6, 0))
    return state, animate_cards, tick, spark_loop, poll, timer_loop


def _make_sparkline(parent, width: int = 420, height: int = 90):
    import tkinter as tk

    canvas = tk.Canvas(parent, width=width, height=height, highlightthickness=0,
                       bd=0, bg=T.col("chart_bg"))
    canvas.pack(fill="x", pady=(10, 0))
    values: list[float] = []
    line = {"id": None}

    def redraw():
        canvas.delete("all")
        if len(values) < 2:
            return
        n = len(values)
        pts = []
        for i, v in enumerate(values):
            x = 4 + (width - 8) * i / (n - 1)
            y = height - 6 - (height - 12) * max(0.0, min(1.0, v))
            pts.extend((x, y))
        canvas.create_line(*pts, fill=T.col("chart_line"), width=2, smooth=True)

    def append(v: float):
        values.append(v)
        if len(values) > 120:
            del values[0]
        redraw()

    canvas.append = append  # type: ignore[attr-defined]
    return canvas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--mode", choices=("animated", "perf"), default="animated")
    ap.add_argument("--widgets", type=int, default=MAX_ANIMATED_WIDGETS)
    ap.add_argument("--appearance", choices=("dark", "light"), default="dark")
    ap.add_argument("--ready-file", default=None,
                    help="write this file once the window is up (for perf_budget)")
    args = ap.parse_args()

    try:
        import tkinter as tk
        import customtkinter as ctk  # noqa: F401
    except Exception as e:  # pragma: no cover
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 3

    T.apply(appearance=args.appearance, accent="blue")
    A.set_performance(args.mode == "perf")
    A.MOTION.fps = 30

    try:
        root = tk.Tk()
    except Exception as e:  # no display / no window station
        print(json.dumps({"ok": False, "error": f"Tk unavailable: {e}"}))
        return 3

    root.title("ui_selftest")
    root.geometry("640x520")
    try:
        state, animate_cards, tick, spark_loop, poll, timer_loop = build(
            root, max(1, args.widgets), A.MOTION)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"build failed: {type(e).__name__}: {e}"}))
        return 4

    # What always runs, in both modes (matches the shipped launcher).
    poll()
    timer_loop()
    # Animation-only loops: exactly what performance mode skips while training.
    if A.MOTION.enabled:
        animate_cards()
        tick()
        spark_loop()

    def signal_ready():
        state["cpu0"] = time.process_time()
        state["t_ready"] = time.perf_counter()
        if args.ready_file:
            try:
                Path(args.ready_file).write_text("ready", encoding="utf-8")
            except OSError:
                pass

    root.after(80, signal_ready)

    def finish():
        elapsed = max(0.001, time.perf_counter() - state["t0"])
        state["stop"] = True
        ready_at = state.get("t_ready")
        steady = max(0.001, time.perf_counter() - ready_at) if ready_at else elapsed
        cpu = max(0.0, time.process_time() - state.get("cpu0", 0.0))
        summary = {
            "ok": True,
            "mode": args.mode,
            "seconds": round(elapsed, 2),
            "steady_seconds": round(steady, 2),
            "widgets": args.widgets,
            "frames": state["frames"],
            "fps": round(state["frames"] / elapsed, 1),
            "cpu_seconds": round(cpu, 3),
            "cpu_pct": round(cpu / steady * 100.0, 2),
            "motion_enabled": bool(A.MOTION.enabled),
        }
        print(json.dumps(summary))
        try:
            root.destroy()
        except Exception:
            pass

    root.after(int(args.seconds * 1000), finish)
    try:
        root.mainloop()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"mainloop: {type(e).__name__}: {e}"}))
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
