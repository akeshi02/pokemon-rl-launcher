#!/usr/bin/env python3
"""dashboard_check.py -- CI-able gate for the live dashboard (v2 / M3).

The M3 promise is narrow and testable: **the 2 s status path never redraws a
matplotlib figure, and pushing a status sample to the dashboard is cheap enough
to sit on that path** (< 1 % of one core).  matplotlib stays bundled, but only
for the explicit "Expanded chart" dialog.

Three checks, all of them about that promise:

1. **Static reachability.**  Starting from ``LauncherApp.poll_training_status``
   the call graph is walked *inside the GUI module*; no reachable function may
   mention matplotlib.  ``open_chart`` is deliberately not reachable, so it may.
   ``dashboard.py`` may not mention matplotlib at all.
2. **Runtime isolation.**  Importing ``theme``/``anim``/``dashboard`` must not
   pull matplotlib into ``sys.modules`` -- checked in a fresh interpreter.
3. **Cost + fallback.**  On a real Tk root the dashboard is updated the way the
   2 s poll does; its CPU is measured against the 2 s cadence, and the
   reduced-motion fallback (one-line labels) is confirmed to keep updating.

    python tools/dashboard_check.py
    python tools/dashboard_check.py --json
    python tools/dashboard_check.py --samples 80 --budget 1.0

Exit codes: 0 clean, 1 a check failed, 2 the tool could not run.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V2 = ROOT / "overlay" / "v2"
GUI = V2 / "pokemon_launcher_gui.py"
DASHBOARD = V2 / "dashboard.py"

# Identifiers that mean "matplotlib is on this code path" (AST only, so prose
# in docstrings/comments does not count).
MPL_IDENTS = {"matplotlib", "pyplot", "plt", "Figure", "FigureCanvas",
              "FigureCanvasTkAgg", "draw_idle"}
MPL_MODULES = ("matplotlib", "pylab")
HOT_PATH_ROOT = "poll_training_status"


# ---------------------------------------------------------------------------
# 1. static reachability
# ---------------------------------------------------------------------------

def _mpl_refs(node: ast.AST) -> set[str]:
    """Matplotlib imports/identifiers used anywhere inside ``node``."""
    refs: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Import):
            for alias in sub.names:
                if alias.name.split(".")[0] in MPL_MODULES:
                    refs.add(alias.name)
        elif isinstance(sub, ast.ImportFrom):
            if (sub.module or "").split(".")[0] in MPL_MODULES:
                refs.add(sub.module or "")
        elif isinstance(sub, ast.Name):
            if sub.id in MPL_IDENTS:
                refs.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            if sub.attr in MPL_IDENTS:
                refs.add(sub.attr)
    return refs


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls_in(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                names.add(fn.attr)
            elif isinstance(fn, ast.Name):
                names.add(fn.id)
    return names


def _reachable(functions: dict[str, ast.AST], root: str) -> set[str]:
    seen = {root}
    frontier = [root]
    while frontier:
        node = functions.get(frontier.pop())
        if node is None:
            continue
        for name in _calls_in(node):
            if name in functions and name not in seen:
                seen.add(name)
                frontier.append(name)
    return seen


def check_static() -> list[str]:
    problems: list[str] = []

    # dashboard.py: must be pure tk -- no matplotlib, ever.
    dash_tree = ast.parse(DASHBOARD.read_text(encoding="utf-8"), filename=str(DASHBOARD))
    for ref in sorted(_mpl_refs(dash_tree)):
        problems.append(f"dashboard.py uses matplotlib identifier {ref!r}")

    # pokemon_launcher_gui.py: no matplotlib reachable from the poll root.
    gui_src = GUI.read_text(encoding="utf-8")
    gui_tree = ast.parse(gui_src, filename=str(GUI))
    functions = _functions(gui_tree)
    if HOT_PATH_ROOT not in functions:
        problems.append(f"{HOT_PATH_ROOT}() not found in pokemon_launcher_gui.py")
        return problems
    for name in sorted(_reachable(functions, HOT_PATH_ROOT)):
        refs = _mpl_refs(functions[name])
        if refs:
            problems.append(f"hot path reaches {name}() using matplotlib "
                            f"identifier(s) {', '.join(sorted(refs))}")
    return problems


# ---------------------------------------------------------------------------
# 2. runtime isolation (fresh interpreter)
# ---------------------------------------------------------------------------

def check_import_isolation() -> list[str]:
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{V2}')\n"
        "import theme, anim, dashboard\n"
        "print('matplotlib' in sys.modules)\n"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return [f"import-isolation check could not run: {type(e).__name__}: {e}"]
    if out.returncode != 0:
        return [f"import-isolation subprocess failed: {out.stderr.strip()[:200]}"]
    loaded = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "?"
    if loaded != "False":
        return ["importing theme/anim/dashboard pulled matplotlib into sys.modules"]
    return []


# ---------------------------------------------------------------------------
# 3. cost + reduced-motion fallback (real Tk)
# ---------------------------------------------------------------------------

def _pump(app, seconds: float) -> None:
    app.after(max(1, int(seconds * 1000)), app.quit)
    app.mainloop()


class _Silent:
    """Swallow dialog calls the GUI would otherwise make."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


def check_runtime(samples: int, budget: float) -> list[str]:
    import time

    sys.path.insert(0, str(V2))
    try:
        import theme as T
        import anim as A
        T.enable_dpi_awareness()
        import pokemon_launcher_gui as gui
    except Exception as e:  # noqa: BLE001
        return [f"runtime check could not import the stack: {type(e).__name__}: {e}"]

    problems: list[str] = []
    findings: dict = {}
    gui.messagebox = _Silent()
    gui.filedialog = _Silent()
    if getattr(gui, "env_utils", None) is not None:
        try:
            gui.env_utils.missing_packages = lambda *a, **k: []
        except Exception:
            pass

    cfg = gui.config_path()
    backup = cfg.read_text(encoding="utf-8") if cfg.exists() else None
    app = None
    try:
        app = gui.LauncherApp()
        _pump(app, 0.5)

        # -- cost on the 2 s cadence (performance mode: a run is active) ----
        A.set_reduce_motion(False)
        A.set_performance(True)
        app.start_time = time.time() - 100
        app.expected_windows = 4
        app.build_headless_status_frame()
        _pump(app, 0.3)
        if app.dash is None:
            problems.append("dashboard was not built by build_headless_status_frame()")
        else:
            comps = {"explore": 0.4, "heal": -0.1, "caught": 1.2}
            app._update_dashboard({"steps": 0, "mean_reward": 0.0, "components": comps})
            _pump(app, 0.1)
            cpu0 = time.process_time()
            t0 = time.perf_counter()
            for i in range(samples):
                app._update_dashboard({
                    "steps": (i + 1) * 100,
                    "mean_reward": 0.5 + (i % 7) * 0.1,
                    "components": {k: v + (i % 3) * 0.01 for k, v in comps.items()},
                })
                _pump(app, 0.01)
            cpu = max(0.0, time.process_time() - cpu0)
            wall = max(0.001, time.perf_counter() - t0)
            # CPU% of one core if these updates arrive every 2 s.
            cadence_pct = cpu / (samples * 2.0) * 100.0
            if cadence_pct > budget:
                problems.append(f"dashboard update {cadence_pct:.3f}% of a core "
                                f"on the 2 s cadence > {budget}% budget")
            findings = {
                "cpu_seconds": round(cpu, 4),
                "wall_seconds": round(wall, 3),
                "cadence_pct": round(cadence_pct, 4),
                "samples": samples,
            }
            if len(app.dash["spark"].canvas.find_all()) < 2:
                problems.append("sparkline drew no curve after many samples")

        # -- reduced-motion fallback still updates --------------------------
        A.set_reduce_motion(True)
        A.set_performance(True)
        app.build_grid_frame(4, 2)
        _pump(app, 0.3)
        if app.dash is not None:
            problems.append("reduced motion still built the animated dashboard")
        app._set_stats(500, 0.5)
        app._update_dashboard({"steps": 500, "mean_reward": 0.5,
                               "components": {"explore": 0.3}})
        _pump(app, 0.2)
        label = ""
        comps_label = ""
        try:
            label = app.status_label_live.cget("text")
            comps_label = app.components_label.cget("text")
        except Exception as e:  # noqa: BLE001
            problems.append(f"fallback labels unreadable: {e}")
        if "500" not in str(label):
            problems.append(f"fallback status label did not update: {label!r}")
        if "explore" not in str(comps_label):
            problems.append(f"fallback components label did not update: {comps_label!r}")
        findings["fallback_status"] = str(label)
        findings["fallback_components"] = str(comps_label)
    except Exception as e:  # noqa: BLE001
        return [f"runtime check failed: {type(e).__name__}: {e}"]
    finally:
        if app is not None:
            try:
                app.destroy()
            except Exception:
                pass
        if backup is None:
            try:
                cfg.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            cfg.write_text(backup, encoding="utf-8")

    return problems, findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--samples", type=int, default=60)
    ap.add_argument("--budget", type=float, default=1.0,
                    help="dashboard CPU budget in %% of one core (default 1.0)")
    ap.add_argument("--skip-runtime", action="store_true",
                    help="only run the static + import checks")
    args = ap.parse_args()

    for path in (GUI, DASHBOARD):
        if not path.is_file():
            print(f"module not found: {path}", file=sys.stderr)
            return 2

    static = check_static()
    iso = check_import_isolation()
    findings: dict = {}
    runtime: list[str] = []
    if not args.skip_runtime:
        result = check_runtime(args.samples, args.budget)
        if isinstance(result, tuple):
            runtime, findings = result
        else:
            runtime = result

    failures = {"matplotlib_on_hot_path": static, "import_isolation": iso,
                "runtime_cost_or_fallback": runtime}
    ok = not any(failures.values())

    if args.json:
        print(json.dumps({"ok": ok, "findings": findings, "failures": failures}, indent=2))
        return 0 if ok else 1

    print("=" * 66)
    print("Launcher dashboard check (M3)")
    print("=" * 66)
    print(f"matplotlib reachable from {HOT_PATH_ROOT}():",
          "no" if not static else f"{len(static)} problem(s)")
    for line in static:
        print(f"  FAIL  {line}")
    print("importing dashboard pulls matplotlib:", "no" if not iso else "YES")
    for line in iso:
        print(f"  FAIL  {line}")
    if args.skip_runtime:
        print("runtime cost / fallback: skipped")
    else:
        cpct = findings.get("cadence_pct")
        print(f"dashboard update cost: {cpct if cpct is not None else '-'}% of a core "
              f"on the 2 s cadence (budget {args.budget}%)")
        print("reduced-motion fallback:",
              "ok" if not runtime else f"{len(runtime)} problem(s)")
        for line in runtime:
            print(f"  FAIL  {line}")
    print("-" * 66)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
