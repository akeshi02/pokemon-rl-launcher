#!/usr/bin/env python3
"""perf_budget.py -- CI-able CPU gate for the launcher's UI (v2 / M0).

Rule from the roadmap: while a training run is active nothing animates, and the
UI must cost < 2 % of one core.  This script measures that by running
``overlay/v2/ui_selftest.py`` twice and sampling the process tree with psutil.

    python tools/perf_budget.py                 # perf (<2%) + animated report
    python tools/perf_budget.py --seconds 6
    python tools/perf_budget.py --python D:\\envs\\x\\python.exe
    python tools/perf_budget.py --json          # machine-readable summary

Exit codes: 0 pass, 1 budget exceeded, 2 the harness itself could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELFTEST = ROOT / "overlay" / "v2" / "ui_selftest.py"

PERF_BUDGET_PCT = 2.0        # hard gate: animation disabled (the shipped default)
ANIMATED_CAP_PCT = 25.0      # sanity cap for the demo mode
MIN_FPS = 20.0               # 30 fps target, tolerate 33 % scheduling slack


def _tree_cpu_seconds(p) -> float:
    """Total CPU seconds used by a process and its children (best-effort)."""
    total = 0.0
    try:
        t = p.cpu_times()
        total += t.user + t.system
    except Exception:
        return 0.0
    try:
        for child in p.children(recursive=True):
            try:
                ct = child.cpu_times()
                total += ct.user + ct.system
            except Exception:
                pass
    except Exception:
        pass
    return total


def measure(python: str, mode: str, seconds: float, widgets: int):
    """Run the selftest and sample its CPU use (percent of one core)."""
    try:
        import psutil
    except Exception as e:
        raise SystemExit(f"perf_budget needs psutil ({e}). Install it in the test env.")

    samples: list[float] = []
    cpu_seconds, wall = 0.0, 0.0
    ready = Path(tempfile.gettempdir()) / f"ui_selftest_{os.getpid()}_{mode}.ready"
    try:
        ready.unlink()
    except OSError:
        pass
    cmd = [python, str(SELFTEST), "--seconds", str(seconds), "--mode", mode,
           "--widgets", str(widgets), "--ready-file", str(ready)]

    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        p = psutil.Process(proc.pid)
        # Wait until the window is actually built and the loops are running:
        # importing customtkinter/matplotlib is seconds of one-off cost that has
        # nothing to do with the animation budget.
        deadline = time.time() + 120
        while not ready.exists() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        # Let the first paint / toast layout finish, then measure the steady state.
        time.sleep(1.5)
        p.cpu_percent(None)  # prime the counter (first call is always 0.0)
        cpu_start = _tree_cpu_seconds(p)
        wall_start = time.perf_counter()
        deadline = time.time() + seconds + 60
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.25)
            try:
                cpu = p.cpu_percent(None)
                for child in p.children(recursive=True):
                    try:
                        cpu += child.cpu_percent(None)
                    except psutil.Error:
                        pass
            except psutil.Error:
                break
            samples.append(cpu)
            # Read cumulative CPU while the child is still alive: the loop ends
            # when it exits, and a dead pid reads as 0.
            cpu_now = _tree_cpu_seconds(p)
            if cpu_now >= cpu_start:
                cpu_seconds = cpu_now - cpu_start
                wall = max(0.001, time.perf_counter() - wall_start)
    finally:
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        try:
            ready.unlink()
        except OSError:
            pass
    if len(samples) > 4:
        # Drop the first interval (spans the settle gap) and the last one (the
        # shutdown/teardown burst): both are one-off, not ongoing UI cost.
        samples = samples[1:-1]

    summary = None
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                summary = json.loads(line)
                break
            except ValueError:
                continue

    # Prefer the process's own steady-state reading (in-process time.process_time
    # delta from "ready"), which excludes one-off import cost and is not
    # perturbed by the sampler.  Fall back to the external measurement.
    self_pct = (summary or {}).get("cpu_pct")
    external = round(cpu_seconds / wall * 100.0, 3) if wall > 0 else None
    if self_pct is not None:
        avg_cpu, source = float(self_pct), "self"
    elif external is not None:
        avg_cpu, source = external, "external"
    elif samples:
        avg_cpu, source = round(statistics.fmean(samples), 3), "sampled"
    else:
        avg_cpu, source = None, "none"
    return {
        "mode": mode,
        "rc": proc.returncode,
        "avg_cpu": avg_cpu,
        "avg_cpu_source": source,
        "external_cpu": external,
        "peak_cpu": round(max(samples), 3) if samples else None,
        "cpu_seconds": round(cpu_seconds, 3),
        "window_s": round(wall, 2),
        "samples": len(samples),
        "summary": summary,
        "output": (out or "").strip(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default=sys.executable, help="interpreter that can import customtkinter")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--widgets", type=int, default=12)
    ap.add_argument("--threshold", type=float, default=PERF_BUDGET_PCT,
                    help="hard CPU budget for perf mode (default 2.0%%)")
    ap.add_argument("--json", action="store_true", help="print a JSON summary only")
    ap.add_argument("--skip-animated", action="store_true",
                    help="only run the gated perf-mode measurement")
    args = ap.parse_args()

    if not SELFTEST.is_file():
        print(f"selftest not found: {SELFTEST}", file=sys.stderr)
        return 2
    if not Path(args.python).is_file() and os.sep in args.python:
        print(f"interpreter not found: {args.python}", file=sys.stderr)
        return 2

    runs = [measure(args.python, "perf", args.seconds, args.widgets)]
    if not args.skip_animated:
        runs.append(measure(args.python, "animated", args.seconds, args.widgets))

    failures: list[str] = []
    for r in runs:
        if r["rc"] != 0:
            failures.append(f"{r['mode']}: selftest exited {r['rc']} "
                            f"({r['output'].splitlines()[-1] if r['output'] else 'no output'})")
            continue
        if r["mode"] == "perf":
            if r["avg_cpu"] is None:
                failures.append("perf: no CPU samples collected")
            elif r["avg_cpu"] > args.threshold:
                failures.append(f"perf: avg {r['avg_cpu']}% > {args.threshold}% budget")
        else:
            if r["avg_cpu"] is not None and r["avg_cpu"] > ANIMATED_CAP_PCT:
                failures.append(f"animated: avg {r['avg_cpu']}% > {ANIMATED_CAP_PCT}% sanity cap")
            fps = (r["summary"] or {}).get("fps")
            if fps is not None and fps < MIN_FPS:
                failures.append(f"animated: {fps} fps < {MIN_FPS} fps floor")

    if args.json:
        print(json.dumps({"ok": not failures, "budget_pct": args.threshold,
                          "runs": runs, "failures": failures}, indent=2))
        return 1 if failures else 0

    print("=" * 66)
    print("UI performance budget")
    print("=" * 66)
    print(f"{'mode':<10} {'avg %cpu':>9} {'src':>9} {'peak %cpu':>10} {'frames':>8} {'fps':>7}")
    for r in runs:
        s = r["summary"] or {}
        avg = "-" if r["avg_cpu"] is None else f"{r['avg_cpu']:.2f}"
        peak = "-" if r["peak_cpu"] is None else f"{r['peak_cpu']:.2f}"
        print(f"{r['mode']:<10} {avg:>9} {r.get('avg_cpu_source', '-'):>9} {peak:>10} "
              f"{str(s.get('frames', '-')):>8} {str(s.get('fps', '-')):>7}")
    print("-" * 66)
    print("src=self  -> CPU measured inside the app from 'ready' (excludes import);")
    print("src=external -> sampled from outside; peak is the external sampler's max.")
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print(f"PASS  perf mode within {args.threshold}% CPU; "
          f"animated demo >= {MIN_FPS} fps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
