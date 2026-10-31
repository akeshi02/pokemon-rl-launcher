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
import dashboard as D      # noqa: E402

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

    # The real M3 dashboard: KPI cards, Canvas sparkline, component bars and a
    # resource strip.  In perf mode every update snaps -- that static cost is
    # exactly what perf_budget gates.
    dash = _build_dashboard(holder, motion)
    spark = dash["spark"]
    history: list[float] = []

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
        """Sparkline at the *real* update cadence (the 120 ms figure in the spec
        is the morph duration, not how often a new point arrives).  A run writes
        status every 2 s, so the morph runs then -- not eight times a second."""
        if state["stop"]:
            return
        spark.push(state["frames"] % 40 / 40.0)
        root.after(2000, spark_loop)

    def poll():
        if state["stop"]:
            return
        # Emulates the launcher's 2 s status read (a small JSON file) plus the
        # dashboard push that replaced the old matplotlib redraw.  Values move
        # every sample, like a real run, so the tween/redraw cost is real.
        n = int(time.perf_counter() - state["t0"])
        history.append(0.5 + (n % 7) * 0.1)
        dash["update"]({
            "steps": n * 500, "steps_s": 250.0,
            "mean_reward": history[-1], "best_reward": max(history),
            "elapsed": n, "tiles": 4, "expected": 4,
            "history": list(history),
            "components": {"explore": 0.4 + (n % 3) * 0.01,
                           "heal": -0.1 - (n % 2) * 0.01,
                           "caught": 1.2 - (n % 4) * 0.01},
        })
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


def _build_dashboard(parent, motion):
    """Build the same widget group the launcher's compact dashboard uses."""
    import customtkinter as ctk

    cards = ctk.CTkFrame(parent, fg_color="transparent")
    cards.pack(fill="x")
    spec = [("STEPS", 0), ("STEPS/S", 1), ("MEAN REWARD", 2),
            ("BEST REWARD", 2), ("ELAPSED", 0), ("TILES", 0)]
    kpi = []
    for i, (caption, decimals) in enumerate(spec):
        cards.grid_columnconfigure(i, weight=1)
        card = D.KpiCard(cards, caption, width=96, height=54, decimals=decimals,
                         motion=motion)
        card.frame.grid(row=0, column=i, sticky="nsew",
                        padx=(0 if i == 0 else T.SPACE["sm"], 0))
        kpi.append(card)

    mid = ctk.CTkFrame(parent, fg_color="transparent")
    mid.pack(fill="x", pady=(T.SPACE["xs"], 0))
    spark = D.Sparkline(mid, width=420, height=70, motion=motion)
    spark.pack(side="left", fill="both", expand=True)
    bars = D.ComponentBars(mid, motion=motion, bar_width=110)
    bars.pack(side="left", fill="x", padx=(T.SPACE["md"], 0))
    resource = D.ResourceStrip(parent)
    resource.pack(anchor="w", pady=(T.SPACE["xs"], 0))

    def update(data):
        kpi[0].set_number(data["steps"])
        kpi[1].set_number(data.get("steps_s", 0.0))
        kpi[2].set_number(data["mean_reward"])
        kpi[3].set_number(data.get("best_reward", data["mean_reward"]))
        kpi[4].set_text(f"{data.get('elapsed', 0)}s")
        kpi[5].set_text(f"{data.get('tiles', 0)}/{data.get('expected', 0)}")
        spark.set_data(data.get("history", []))
        bars.update(data.get("components", {}) or {})
        resource.refresh()

    return {"kpi": kpi, "spark": spark, "bars": bars,
            "resource": resource, "update": update}


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
