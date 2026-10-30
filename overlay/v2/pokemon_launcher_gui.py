"""
pokemon_launcher_gui.py

CustomTkinter launch screen + single-window embedded grid of all PyBoy
game windows spawned by train_cli.py.

The interpreter that runs training is whatever is configured in
launcher_config.json (written by the setup wizard) or picked here with
"Detect Environments…". Training runs with that environment's python.exe and
an activated-like PATH, so conda envs work without `conda activate`.

Requirements (installed into the chosen environment):
    pip install -r requirements-launcher.txt

Usage:
    python pokemon_launcher_gui.py

This expects train_cli.py (with --ep-length, --explore-weight,
--reward-scale, --num-cpu CLI args) in your working directory. Checkpoints
are still passed via stdin, matching the script's existing sys.stdin.read()
logic.
"""

import json
import ast
import os
import re
import signal
import sys
import subprocess
import threading
import time
import traceback
import webbrowser
from collections import deque
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import psutil
import win32con
import win32gui
import win32process
import shutil

try:
    import env_utils
except Exception:  # pragma: no cover - env_utils ships next to this file
    env_utils = None


CONFIG_NAME = "launcher_config.json"


def app_dir() -> Path:
    """Folder the launcher lives in (the exe's folder when frozen by PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_path() -> Path:
    """launcher_config.json written by the setup wizard, next to the launcher."""
    return app_dir() / CONFIG_NAME


ERROR_LOG_NAME = "launcher_error.log"


def error_log_path() -> Path:
    return app_dir() / ERROR_LOG_NAME


def log_error(text: str) -> None:
    """Record a traceback where a windowed (console-less) build can show it."""
    try:
        with open(error_log_path(), "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{text}\n")
    except OSError:
        pass


def install_error_hooks(root=None) -> None:
    """Surface errors instead of losing them.

    The shipped launcher is a windowed EXE: sys.stderr is None, so Tk's default
    report_callback_exception (and thread tracebacks) disappear without a trace
    and the window just looks frozen. Log them and show a dialog.
    """
    def hook(exc_type, value, tb):
        text = "".join(traceback.format_exception(exc_type, value, tb))
        log_error(text)
        try:
            messagebox.showerror(
                "Launcher error",
                f"{value}\n\nDetails were written to:\n{error_log_path()}")
        except Exception:
            pass

    if root is not None:
        root.report_callback_exception = hook
    sys.excepthook = hook
    try:
        threading.excepthook = lambda args: hook(
            args.exc_type, args.exc_value or args.exc_type(), args.exc_traceback)
    except Exception:
        pass


def load_launcher_config() -> dict:
    try:
        # utf-8-sig: Notepad and PowerShell's Set-Content add a BOM, which plain
        # utf-8 json.loads rejects -- the launcher then silently used defaults.
        data = json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_launcher_config(updates: dict) -> None:
    """Merge updates into launcher_config.json (keeps fields written by setup)."""
    data = load_launcher_config()
    data.update({k: v for k, v in updates.items() if v is not None})
    try:
        config_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass

def find_training_python(workdir, cfg=None) -> str:
    """Interpreter that runs train_cli.py.

    Priority: the python saved in launcher_config.json, then (for a frozen
    exe, where sys.executable is the launcher itself) POKE_PYTHON, a venv next
    to / above the working folder, the active conda env, then PATH.
    """
    cfg = load_launcher_config() if cfg is None else cfg
    saved = str(cfg.get("python") or "").strip()
    if saved and Path(saved).is_file():
        return saved
    if not getattr(sys, "frozen", False):
        return sys.executable
    override = os.environ.get("POKE_PYTHON", "").strip()
    if override:
        return override
    wd = Path(workdir)
    for base in (wd, wd.parent, app_dir()):
        for venv in ("venv", ".venv"):
            cand = base / venv / "Scripts" / "python.exe"
            if cand.exists():
                return str(cand)
    conda = os.environ.get("CONDA_PREFIX")
    if conda and (Path(conda) / "python.exe").exists():
        return str(Path(conda) / "python.exe")
    return shutil.which("python") or ""


DEFAULT_REWARD_WEIGHTS = {
    "event_multiplier": 4,
    "heal_multiplier": 10,
    "badge_multiplier": 10,
    "explore_multiplier": 0.1,
    "stuck_multiplier": -0.05,
    "level_multiplier": 1,
    "op_lvl_multiplier": 0.2,
    "death_multiplier": -0.1,
    "catch_multiplier": 5,
    "stuck_ramp_start_count": 100,
    "stuck_ramp_end_count": 600,
    "battle_multiplier": -1,
    "battle_grace_steps": 30,
    "battle_penalty_per_step": 0.02,
    "battle_max_penalty": 5.0,
    "battle_damage_multiplier": 2.0,
    "battle_damage_cap": 10.0,
    "evolution_multiplier": 8.0,
    "map_progress_multiplier": 5.0,
}


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Checkpoint names train_cli.py writes: poke_<N>_steps.zip,
# poke_interrupted_<N>_steps.zip, and hand-saved poke_<N>.zip. The old
# pattern only matched the first form, so interrupted/final saves were
# invisible in the launcher.
CHECKPOINT_RE = re.compile(r"^poke_(?:interrupted_)?(\d+)(?:_steps)?\.zip$")


def load_reward_defaults(workdir) -> dict:
    """Read DEFAULT_WEIGHTS out of the project's configurable_reward_env.py.

    The launcher used to keep a second copy of the defaults, which silently
    drifted from the real ones. Parsing the assignment (no import -- that would
    pull in torch) keeps the reward editor honest even if defaults change.
    """
    out = dict(DEFAULT_REWARD_WEIGHTS)
    path = Path(workdir) / "configurable_reward_env.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return out
    for node in tree.body:
        if not (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "DEFAULT_WEIGHTS" for t in node.targets)):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            break
        if isinstance(value, dict):
            out.update({k: v for k, v in value.items() if k in out})
        break
    return out


# ---------------------------------------------------------------------------
# Win32 helpers (unchanged approach: reparent real HWNDs into a plain tk.Frame)
# ---------------------------------------------------------------------------

def get_descendant_pids(root_pid):
    pids = {root_pid}
    try:
        root = psutil.Process(root_pid)
        for child in root.children(recursive=True):
            pids.add(child.pid)
    except psutil.NoSuchProcess:
        pass
    return pids


def enum_visible_windows_for_pids(pids):
    matches = []

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if not win32gui.GetWindowText(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids:
            rect = win32gui.GetWindowRect(hwnd)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if w > 50 and h > 50:
                matches.append(hwnd)
        return True

    win32gui.EnumWindows(_callback, None)
    return matches


def embed_window(hwnd, parent_hwnd, x, y, w, h):
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    style &= ~win32con.WS_POPUP
    style &= ~win32con.WS_CAPTION
    style &= ~win32con.WS_THICKFRAME
    style &= ~win32con.WS_MINIMIZEBOX
    style &= ~win32con.WS_MAXIMIZEBOX
    style &= ~win32con.WS_SYSMENU
    style |= win32con.WS_CHILD
    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
    win32gui.SetParent(hwnd, parent_hwnd)
    win32gui.SetWindowPos(hwnd, 0, x, y, w, h,
                           win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)


def compute_grid(n, cols, area_w, area_h, margin):
    if n == 0:
        return []
    cols = max(1, min(cols, n))
    rows = (n + cols - 1) // cols
    cell_w = max(50, (area_w - margin * (cols + 1)) // cols)
    cell_h = max(50, (area_h - margin * (rows + 1)) // rows)
    positions = []
    for i in range(n):
        r, c = divmod(i, cols)
        x = margin + c * (cell_w + margin)
        y = margin + r * (cell_h + margin)
        positions.append((x, y, cell_w, cell_h))
    return positions


def find_checkpoints(runs_dir: Path):
    results = []
    seen = set()
    if runs_dir.exists():
        for f in runs_dir.rglob("poke_*.zip"):
            m = CHECKPOINT_RE.search(f.name)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                results.append((int(m.group(1)), f))
    results.sort(key=lambda t: t[0], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class LauncherApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Pokemon RL Training Launcher")
        self.geometry("1000x760")
        self.minsize(720, 480)

        self.proc = None
        self.root_pid = None
        self.embedded_hwnds = set()
        self.resolved_sess_path = None
        self.start_time = None
        self.timer_label = None
        self.status_label_live = None
        self.components_label = None
        self.reward_history = deque(maxlen=120)
        self.graph_canvas = None
        self.graph_ax = None
        self._train_log = None
        self.train_log_path = None

        self.cfg = load_launcher_config()

        default_workdir = str(self.cfg.get("workdir") or "")
        if not default_workdir or not Path(default_workdir).is_dir():
            default_workdir = str(app_dir() if getattr(sys, "frozen", False) else Path.cwd())

        self.workdir_var = tk.StringVar(value=default_workdir)
        self.script_var = tk.StringVar(value=self.cfg.get("script") or "train_cli.py")
        self.python_var = tk.StringVar(value=find_training_python(default_workdir, self.cfg))
        self.num_cpu_var = tk.StringVar(value=str(self.cfg.get("num_cpu") or 4))
        self.cols_var = tk.StringVar(value=str(self.cfg.get("cols") or 2))
        self.ep_length_var = tk.StringVar(value=str(self.cfg.get("ep_length") or (2048 * 80)))
        self.explore_weight_var = tk.StringVar(value=str(self.cfg.get("explore_weight") or 0.25))
        self.reward_scale_var = tk.StringVar(value=str(self.cfg.get("reward_scale") or 0.5))
        self.checkpoint_var = tk.StringVar(value="")  # "" == fresh start
        self.headless_var = tk.BooleanVar(value=bool(self.cfg.get("headless", False)))
        self.no_reset_var = tk.BooleanVar(value=bool(self.cfg.get("no_reset", False)))
        self.curriculum_start_var = tk.StringVar(value="")  # "" == very start (init.state)
        self.save_curriculum_var = tk.BooleanVar(value=bool(self.cfg.get("save_curriculum", False)))

        # Resolved details (prefix, version, packages) of the training env.
        self._env_info = None
        self.env_status_var = tk.StringVar(value="")
        self._env_status_label = None

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.select_frame = None
        self.grid_frame = None
        self.build_select_frame()
        self.refresh_env_status()

        # Automation/troubleshooting hook: POKE_AUTOSTART=1 starts the run that is
        # configured in launcher_config.json as soon as the window opens.
        if os.environ.get("POKE_AUTOSTART", "").strip().lower() not in ("", "0", "false", "no"):
            self.after(800, self.launch_training)

    # ------------------------------------------------------------------
    def _clear_root_children(self):
        """Destroy anything currently packed directly under the root window.

        CTkScrollableFrame occasionally leaves a stray scrollbar/canvas
        behind after .destroy() (a known customtkinter quirk where an
        internal <Configure> binding on the *toplevel* outlives the
        widget). That leftover widget then shares pack() space with the
        next screen, squeezing it into a fraction of the window and
        leaving a floating scrollbar behind. Explicitly wiping every
        direct child of the root -- not just the one reference we kept --
        guarantees a clean slate before building the next screen.
        """
        for child in list(self.winfo_children()):
            child.destroy()
        self.update_idletasks()

    def build_select_frame(self):
        self._clear_root_children()
        self.grid_frame = None

        # The whole settings screen lives inside a CTkScrollableFrame now, so
        # it never gets clipped when the window is small or the checkpoint /
        # curriculum lists grow long -- the user can just scroll the page.
        f = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color="#3b8ed0",
            scrollbar_button_hover_color="#1f6aa5",
        )
        f.pack(fill="both", expand=True, padx=24, pady=20)
        self.select_frame = f

        header = ctk.CTkFrame(f, fg_color="transparent")
        header.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(header, text="🎮 Pokemon RL Training Launcher",
                     font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(header, text="Configure a run, pick a checkpoint (or start fresh), and launch.",
                     text_color="gray70", font=ctk.CTkFont(size=13)).pack(anchor="w", pady=(2, 0))
        ctk.CTkFrame(f, height=2, fg_color="#3b8ed0").pack(fill="x", pady=(12, 16))

        def section_label(parent, text):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.grid(row=0, column=0, sticky="w", padx=14, pady=(12, 6), columnspan=4)
            ctk.CTkFrame(row, width=4, height=18, fg_color="#3b8ed0", corner_radius=2).pack(side="left", padx=(0, 8))
            ctk.CTkLabel(row, text=text, font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")

        # --- Paths section ---
        paths_card = ctk.CTkFrame(f, corner_radius=14, border_width=1, border_color="gray25")
        paths_card.pack(fill="x", pady=(0, 14))
        section_label(paths_card, "📁 Paths")

        ctk.CTkLabel(paths_card, text="Working directory").grid(row=1, column=0, sticky="w", padx=14, pady=6)
        ctk.CTkEntry(paths_card, textvariable=self.workdir_var, width=420).grid(row=1, column=1, padx=6, pady=6)
        ctk.CTkButton(paths_card, text="Browse", width=80, command=self.browse_workdir).grid(row=1, column=2, padx=(6, 14))

        ctk.CTkLabel(paths_card, text="Training script").grid(row=2, column=0, sticky="w", padx=14, pady=(6, 14))
        ctk.CTkEntry(paths_card, textvariable=self.script_var, width=420).grid(row=2, column=1, padx=6, pady=(6, 14))

        ctk.CTkLabel(paths_card, text="Python (training)").grid(row=3, column=0, sticky="w", padx=14, pady=6)
        ctk.CTkEntry(paths_card, textvariable=self.python_var, width=420).grid(row=3, column=1, padx=6, pady=6)
        ctk.CTkButton(paths_card, text="Browse", width=80, command=self.browse_python).grid(
            row=3, column=2, padx=(6, 14), pady=6)

        env_row = ctk.CTkFrame(paths_card, fg_color="transparent")
        env_row.grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 4))
        ctk.CTkButton(env_row, text="🔍 Detect Environments…", width=170,
                      command=self.detect_environments).pack(side="left")
        ctk.CTkButton(env_row, text="Install Packages…", width=150,
                      command=self.open_install_dialog).pack(side="left", padx=8)
        ctk.CTkButton(env_row, text="Refresh status", width=120,
                      command=self.refresh_env_status).pack(side="left")

        self._env_status_label = ctk.CTkLabel(
            paths_card, textvariable=self.env_status_var, text_color="gray60",
            font=ctk.CTkFont(size=11), justify="left", wraplength=680)
        self._env_status_label.grid(row=5, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 12))

        # --- Run config section ---
        cfg_card = ctk.CTkFrame(f, corner_radius=14, border_width=1, border_color="gray25")
        cfg_card.pack(fill="x", pady=(0, 14))
        section_label(cfg_card, "⚙️ Run Configuration")

        def labeled_entry(parent, row, col, label, var, width=100):
            ctk.CTkLabel(parent, text=label).grid(row=row, column=col, sticky="w", padx=(14, 6), pady=6)
            ctk.CTkEntry(parent, textvariable=var, width=width).grid(row=row, column=col + 1, sticky="w", pady=6)

        def stepper(parent, row, col, label, var, minv, maxv, step=1):
            """A '- value +' control for quickly increasing/decreasing an integer setting."""
            ctk.CTkLabel(parent, text=label).grid(row=row, column=col, sticky="w", padx=(14, 6), pady=6)

            box = ctk.CTkFrame(parent, fg_color="transparent")
            box.grid(row=row, column=col + 1, sticky="w", pady=6)

            def clamp_set(new_val):
                new_val = max(minv, min(maxv, new_val))
                var.set(str(new_val))

            def get_val():
                try:
                    return int(var.get())
                except ValueError:
                    return minv

            ctk.CTkButton(box, text="−", width=28, corner_radius=8,
                          command=lambda: clamp_set(get_val() - step)).pack(side="left")
            ctk.CTkEntry(box, textvariable=var, width=48, justify="center").pack(side="left", padx=4)
            ctk.CTkButton(box, text="+", width=28, corner_radius=8,
                          command=lambda: clamp_set(get_val() + step)).pack(side="left")

        stepper(cfg_card, 1, 0, "num_cpu (# of GUIs)", self.num_cpu_var, minv=1, maxv=16)
        stepper(cfg_card, 1, 2, "Grid columns", self.cols_var, minv=1, maxv=8)
        labeled_entry(cfg_card, 2, 0, "ep_length", self.ep_length_var, width=140)
        labeled_entry(cfg_card, 2, 2, "explore_weight", self.explore_weight_var)
        labeled_entry(cfg_card, 3, 0, "reward_scale", self.reward_scale_var)
        ctk.CTkCheckBox(cfg_card, text="Headless (no game windows)",
                         variable=self.headless_var).grid(row=3, column=2, columnspan=2, sticky="w", pady=(6, 14), padx=(14, 0))
        ctk.CTkCheckBox(cfg_card, text="Don't reset episode (let current run continue past ep_length)",
                         variable=self.no_reset_var).grid(row=4, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 6))
        ctk.CTkCheckBox(cfg_card, text="Save curriculum snapshots (capture a state file the first time each quest completes)",
                         variable=self.save_curriculum_var).grid(row=5, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 6))

        ctk.CTkLabel(cfg_card, text="ep_length/explore_weight/reward_scale apply to both fresh runs and resumed checkpoints.",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=6, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 6))

        quick_row = ctk.CTkFrame(cfg_card, fg_color="transparent")
        quick_row.grid(row=7, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 14))
        ctk.CTkButton(quick_row, text="Reward Weights...", width=140,
                      command=self.open_reward_editor).pack(side="left")
        ctk.CTkButton(quick_row, text="Open TensorBoard", width=140,
                      command=self.launch_tensorboard).pack(side="left", padx=8)
        ctk.CTkButton(quick_row, text="Open Runs Folder", width=140,
                      command=self.open_runs_folder).pack(side="left")

        # --- Checkpoint section ---
        ckpt_card = ctk.CTkFrame(f, corner_radius=14, border_width=1, border_color="gray25")
        ckpt_card.pack(fill="x", pady=(0, 14))
        header_row = ctk.CTkFrame(ckpt_card, fg_color="transparent")
        header_row.pack(fill="x", padx=14, pady=(12, 4))
        left = ctk.CTkFrame(header_row, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkFrame(left, width=4, height=18, fg_color="#3b8ed0", corner_radius=2).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(left, text="💾 Checkpoint", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        ctk.CTkButton(header_row, text="Refresh", width=80, command=self.refresh_checkpoints).pack(side="right")

        # Fixed height: the outer settings screen is itself scrollable now,
        # so this inner list just needs a sane, bounded viewport rather than
        # trying to expand to fill an infinitely-scrollable parent.
        self.ckpt_scroll = ctk.CTkScrollableFrame(ckpt_card, fg_color="transparent", height=170)
        self.ckpt_scroll.pack(fill="x", padx=8, pady=(0, 12))

        # --- Curriculum start point section ---
        # Independent of the Checkpoint section above: checkpoint picks
        # which PPO model WEIGHTS to resume from; this picks which in-game
        # STATE the episode starts from (e.g. "already past Brock"). The
        # two combine freely -- e.g. resume a checkpoint's weights while
        # still starting each fresh episode from a curriculum snapshot.
        curr_card = ctk.CTkFrame(f, corner_radius=14, border_width=1, border_color="gray25")
        curr_card.pack(fill="x", pady=(0, 14))
        curr_header_row = ctk.CTkFrame(curr_card, fg_color="transparent")
        curr_header_row.pack(fill="x", padx=14, pady=(12, 4))
        curr_left = ctk.CTkFrame(curr_header_row, fg_color="transparent")
        curr_left.pack(side="left")
        ctk.CTkFrame(curr_left, width=4, height=18, fg_color="#3b8ed0", corner_radius=2).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(curr_left, text="🧭 Curriculum Start Point", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        ctk.CTkButton(curr_header_row, text="Refresh", width=80, command=self.refresh_curriculum_points).pack(side="right")
        ctk.CTkLabel(curr_card, text="Requires \"Save curriculum snapshots\" to have been enabled on a "
                                      "previous run to have anything besides \"very start\" to pick from.",
                     text_color="gray60", font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14, pady=(0, 6))

        self.curr_scroll = ctk.CTkScrollableFrame(curr_card, fg_color="transparent", height=140)
        self.curr_scroll.pack(fill="x", padx=8, pady=(0, 12))

        # --- Launch ---
        bottom = ctk.CTkFrame(f, fg_color="transparent")
        bottom.pack(fill="x", pady=(4, 4))
        self.status_label = ctk.CTkLabel(bottom, text="", text_color="gray70")
        self.status_label.pack(side="left")
        ctk.CTkButton(bottom, text="🚀 Launch Training", height=44, width=180, corner_radius=10,
                      font=ctk.CTkFont(size=14, weight="bold"), command=self.launch_training).pack(side="right")

        self._checkpoint_paths = []
        self.refresh_checkpoints()
        self._curriculum_manifest = {}
        self.refresh_curriculum_points()

    def open_reward_editor(self):
        workdir = Path(self.workdir_var.get())
        config_path = workdir / "reward_config.json"
        defaults = load_reward_defaults(workdir)

        current = dict(defaults)
        if config_path.exists():
            try:
                with open(config_path) as f:
                    loaded = json.load(f)
                current.update({k: v for k, v in loaded.items() if k in defaults})
            except (json.JSONDecodeError, OSError):
                pass

        win = ctk.CTkToplevel(self)
        win.title("Reward Weights")
        win.geometry("440x1020")
        win.attributes("-topmost", True)

        ctk.CTkLabel(win, text="Reward Weights", font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=20, pady=(16, 4))
        ctk.CTkLabel(win, text="Saves to reward_config.json in the working directory.\n"
                                "Running training picks up changes automatically (no restart needed).",
                     text_color="gray60", font=ctk.CTkFont(size=11), justify="left").pack(anchor="w", padx=20, pady=(0, 12))

        vars_map = {}
        labels = {
            "event_multiplier": "Event reward",
            "heal_multiplier": "Heal reward",
            "badge_multiplier": "Badge reward",
            "explore_multiplier": "Explore reward",
            "stuck_multiplier": "Stuck penalty",
            "level_multiplier": "Level reward",
            "op_lvl_multiplier": "Opponent level reward",
            "death_multiplier": "Death penalty",
            "catch_multiplier": "Catch reward",
            "stuck_ramp_start_count": "Stuck ramp start (visits)",
            "stuck_ramp_end_count": "Stuck ramp end (visits)",
            "battle_multiplier": "Battle penalty sign",
            "battle_grace_steps": "Battle grace (steps)",
            "battle_penalty_per_step": "Battle penalty / step",
            "battle_max_penalty": "Battle penalty cap",
            "battle_damage_multiplier": "Battle damage reward",
            "battle_damage_cap": "Battle damage cap (refills on quest completion)",
            "evolution_multiplier": "Evolution reward",
            "map_progress_multiplier": "New city/area reward",
        }

        for key, label in labels.items():
            if key not in defaults:
                continue
            row = ctk.CTkFrame(win, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=6)
            ctk.CTkLabel(row, text=label, width=130, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(current[key]))
            vars_map[key] = var
            ctk.CTkEntry(row, textvariable=var, width=100).pack(side="left")

        status_lbl = ctk.CTkLabel(win, text="", text_color="gray60")
        status_lbl.pack(pady=(4, 0))

        def save():
            try:
                new_weights = {k: float(v.get()) for k, v in vars_map.items()}
            except ValueError:
                status_lbl.configure(text="All weights must be numbers.", text_color="#e05a5a")
                return
            try:
                with open(config_path, "w") as f:
                    json.dump(new_weights, f, indent=2)
                status_lbl.configure(text=f"Saved to {config_path.name}", text_color="#5ac47a")
            except OSError as e:
                status_lbl.configure(text=f"Failed to save: {e}", text_color="#e05a5a")

        def reset_defaults():
            for key, var in vars_map.items():
                var.set(str(defaults[key]))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(pady=16)
        ctk.CTkButton(btn_row, text="Reset to defaults", command=reset_defaults, width=140).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Save", command=save, width=100).pack(side="left", padx=6)

    def launch_tensorboard(self):
        runs_dir = Path(self.workdir_var.get()) / "runs"
        tb = shutil.which("tensorboard") or "tensorboard"
        child_env = env_utils.clean_env() if env_utils is not None else os.environ.copy()
        if env_utils is not None and self._env_info:
            prefix = self._env_info.get("prefix") or ""
            cand = Path(prefix) / "Scripts" / "tensorboard.exe"
            if prefix and cand.is_file():
                tb = str(cand)
            child_env = env_utils.activation_env(prefix, base_env=child_env)
        try:
            subprocess.Popen(
                [tb, "--logdir", str(runs_dir)],
                env=child_env,
                creationflags=subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0,
            )
            threading.Timer(2.5, lambda: webbrowser.open("http://localhost:6006")).start()
        except FileNotFoundError:
            messagebox.showerror("TensorBoard not found",
                                  "Couldn't find the 'tensorboard' command. Make sure it's installed "
                                  "in the selected environment (pip install tensorboard).")

    def open_runs_folder(self):
        runs_dir = Path(self.workdir_var.get()) / "runs"
        runs_dir.mkdir(exist_ok=True)
        try:
            os.startfile(str(runs_dir))  # Windows-only, matches this project's target platform
        except AttributeError:
            messagebox.showinfo("Runs folder", str(runs_dir))

    def open_train_log(self):
        """Open the log of the current/last training run (a windowed EXE has no console)."""
        path = self.train_log_path
        if not path or not Path(path).exists():
            runs = Path(self.workdir_var.get()) / "runs"
            logs = sorted(runs.glob("train_*.log"), reverse=True) if runs.exists() else []
            path = logs[0] if logs else None
        if not path:
            messagebox.showinfo("No log yet", "Logs appear here after the first training launch.")
            return
        try:
            os.startfile(str(path))
        except AttributeError:
            messagebox.showinfo("Training log", str(path))

    def browse_workdir(self):
        d = filedialog.askdirectory(initialdir=self.workdir_var.get())
        if d:
            self.workdir_var.set(d)
            self.refresh_checkpoints()
            self.refresh_curriculum_points()

    # ------------------------------------------------------------------
    # training environment (conda / venv / system python)
    # ------------------------------------------------------------------
    def browse_python(self):
        f = filedialog.askopenfilename(title="Select the environment's python.exe",
                                       filetypes=[("python.exe", "python.exe"), ("All files", "*.*")])
        if not f:
            return
        info = None
        if env_utils is not None:
            info = env_utils.python_info(f)
            if not info:
                messagebox.showwarning(
                    "Not a Python interpreter",
                    f"Couldn't run:\n{f}\n\nPick the environment's python.exe "
                    "(for conda: <envs>\\<name>\\python.exe).")
                return
            info.setdefault("name", f"Python {info['version']}")
            info.setdefault("source", "path")
        self.python_var.set(f)
        self.refresh_env_status(info)

    def refresh_env_status(self, info=None):
        """Resolve the configured interpreter and report its packages."""
        if env_utils is None:
            self.env_status_var.set(
                "env_utils.py is missing next to the launcher — environment detection "
                "and package checks are unavailable.")
            return
        py = self.python_var.get().strip()
        if not py:
            self._env_info = None
            self.env_status_var.set("No environment selected. Click 'Detect Environments…'.")
            return
        if info is None or os.path.normcase(info.get("python", "")) != os.path.normcase(py):
            info = env_utils.python_info(py)
        if not info:
            self._env_info = None
            self.env_status_var.set(f"Not a usable Python interpreter: {py}")
            return
        self._env_info = info
        self.env_status_var.set(f"{env_utils.describe(info)}\nChecking installed packages…")

        def work():
            missing = env_utils.missing_packages(info["python"])
            self.after(0, lambda: self._set_env_status(info, missing))
        threading.Thread(target=work, daemon=True).start()

    def _set_env_status(self, info, missing):
        text = ("✔ " if not missing else "⚠ ") + env_utils.describe(info)
        if missing:
            text += "\nMissing: " + ", ".join(missing) + "  —  use 'Install Packages…'."
        else:
            text += "\nAll required packages are installed."
        if not info.get("compatible", True):
            text += (f"\n⚠ Python {info['version']} is outside the supported range "
                     f"({env_utils.MIN_PY[0]}.{env_utils.MIN_PY[1]}–"
                     f"{env_utils.MAX_PY[0]}.{env_utils.MAX_PY[1]}); the pinned training "
                     f"packages may not install.")
        self.env_status_var.set(text)

    def detect_environments(self):
        """Pick a conda env / venv / system python for training."""
        if env_utils is None:
            messagebox.showerror("Not available",
                                 "env_utils.py is missing next to the launcher, so environments "
                                 "can't be detected. Type the python.exe path instead.")
            return

        win = ctk.CTkToplevel(self)
        win.title("Detect Python Environments")
        win.geometry("840x580")
        win.attributes("-topmost", True)
        win.transient(self)

        ctk.CTkLabel(win, text="Which environment should run training?",
                     font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(win, text="Conda environments, virtualenvs and system Pythons are listed. "
                               "The training libraries must be installed in the one you pick.",
                     text_color="gray60", font=ctk.CTkFont(size=11), justify="left").pack(
            anchor="w", padx=20, pady=(0, 10))

        status = ctk.CTkLabel(win, text="Scanning for Python environments…", text_color="gray70")
        status.pack(anchor="w", padx=20)

        listbox = ctk.CTkScrollableFrame(win, fg_color="transparent", height=340)
        listbox.pack(fill="both", expand=True, padx=16, pady=8)

        choice = tk.StringVar(value="")
        envs = {}

        def populate(found):
            for w in listbox.winfo_children():
                w.destroy()
            envs.clear()
            labels = []
            for env in found:
                label = env_utils.describe(env)
                while label in envs:
                    label += " *"
                envs[label] = env
                labels.append(label)
                ctk.CTkRadioButton(listbox, text=label, variable=choice,
                                   value=label).pack(anchor="w", padx=8, pady=5)
            if not found:
                ctk.CTkLabel(listbox, text="No environments found. Use 'Browse python.exe…' below.",
                             text_color="gray60").pack(anchor="w", padx=8, pady=8)
            status.configure(text=f"{len(found)} environment(s) found.")
            if labels:
                current = self.python_var.get().strip()
                pick = next((l for l, e in envs.items()
                             if os.path.normcase(e["python"]) == os.path.normcase(current)), None)
                pick = pick or next((l for l, e in envs.items()
                                     if e.get("compatible") and e.get("source") == "conda"), None)
                pick = pick or next((l for l, e in envs.items() if e.get("compatible")), labels[0])
                choice.set(pick)

        def scan():
            status.configure(text="Scanning for Python environments…")

            def work():
                try:
                    found = env_utils.discover_environments(extra_roots=[Path(self.workdir_var.get())])
                except Exception:
                    found = []
                try:
                    win.after(0, lambda: populate(found))
                except Exception:
                    pass  # dialog closed while scanning
            threading.Thread(target=work, daemon=True).start()

        def choose():
            env = envs.get(choice.get())
            if not env:
                messagebox.showwarning("Pick an environment",
                                       "Select one of the listed environments first.", parent=win)
                return
            self.python_var.set(env["python"])
            win.destroy()
            self.refresh_env_status(env)

        def browse():
            f = filedialog.askopenfilename(parent=win, title="Select the environment's python.exe",
                                           filetypes=[("python.exe", "python.exe"), ("All files", "*.*")])
            if not f:
                return
            info = env_utils.python_info(f)
            if not info:
                messagebox.showwarning("Not a Python interpreter",
                                       f"Couldn't run:\n{f}", parent=win)
                return
            info.setdefault("name", f"Python {info['version']}")
            info.setdefault("source", "path")
            self.python_var.set(f)
            win.destroy()
            self.refresh_env_status(info)

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(btns, text="Rescan", width=100, command=scan).pack(side="left")
        ctk.CTkButton(btns, text="Browse python.exe…", width=160, command=browse).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Cancel", width=90, command=win.destroy).pack(side="right")
        ctk.CTkButton(btns, text="Use this environment", width=170,
                      command=choose).pack(side="right", padx=8)

        scan()

    def open_install_dialog(self):
        """Install/repair the training packages in the configured environment."""
        if env_utils is None:
            messagebox.showerror("Not available", "env_utils.py is missing next to the launcher.")
            return
        py = self.python_var.get().strip()
        info = env_utils.python_info(py)
        if not info:
            messagebox.showerror(
                "Python not found",
                "Set 'Python (training)' to the python.exe of the environment where the "
                "training libraries should live.")
            return
        v2 = Path(self.workdir_var.get())
        if not (v2 / "requirements.txt").exists():
            messagebox.showerror(
                "Project files not found",
                f"Expected requirements.txt in:\n{v2}\n\n"
                "Set 'Working directory' to the project's v2 folder first.")
            return

        missing = env_utils.missing_packages(info["python"])

        win = ctk.CTkToplevel(self)
        win.title("Install Training Packages")
        win.geometry("680x430")
        win.attributes("-topmost", True)
        win.transient(self)

        ctk.CTkLabel(win, text="Install training packages",
                     font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w", padx=20, pady=(16, 4))
        ctk.CTkLabel(win, text=env_utils.describe(info), text_color="gray70",
                     font=ctk.CTkFont(size=11), justify="left", wraplength=630).pack(
            anchor="w", padx=20, pady=(0, 10))
        ctk.CTkLabel(win, text=("Already installed: " + ", ".join(sorted(set(env_utils.REQUIRED_PACKAGES) - set(missing)))
                                if missing else "Everything is already installed."),
                     text_color="gray60", font=ctk.CTkFont(size=11)).pack(anchor="w", padx=20)
        if missing:
            ctk.CTkLabel(win, text="Missing: " + ", ".join(missing), text_color="#e0a15a",
                         font=ctk.CTkFont(size=11)).pack(anchor="w", padx=20, pady=(2, 8))

        cpu_only = tk.BooleanVar(value=False)
        box = ctk.CTkFrame(win, corner_radius=12, border_width=1, border_color="gray25")
        box.pack(fill="x", padx=20, pady=(12, 8))
        ctk.CTkLabel(box, text="PyTorch build", font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=14, pady=(10, 4))
        ctk.CTkRadioButton(box, text="CUDA 12.4 — NVIDIA GPU (big download, recommended if you have one)",
                           variable=cpu_only, value=False).pack(anchor="w", padx=14, pady=3)
        ctk.CTkRadioButton(box, text="CPU only — smaller download, works on any PC",
                           variable=cpu_only, value=True).pack(anchor="w", padx=14, pady=(3, 12))

        ctk.CTkLabel(win, text="Runs in a new console window: torch, upstream requirements "
                               "(req-win.txt), then requirements-launcher.txt.",
                     text_color="gray60", font=ctk.CTkFont(size=11), justify="left",
                     wraplength=630).pack(anchor="w", padx=20)

        def start():
            win.destroy()
            self.start_package_install(info, v2, cpu_only.get())

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(btns, text="Install now", width=140, command=start).pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=100,
                      command=win.destroy).pack(side="right", padx=8)

    def start_package_install(self, info, v2, cpu_only):
        req = env_utils.write_req_win(v2)
        if req is None:
            messagebox.showerror("requirements.txt missing",
                                 f"Can't find requirements.txt in:\n{v2}")
            return
        bat = env_utils.write_install_bat(v2, info["python"], cpu_only=cpu_only)
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            proc = subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(v2),
                                    env=env_utils.clean_env(), creationflags=flags)
        except OSError as e:
            messagebox.showerror("Could not start the installer", str(e))
            return

        self.env_status_var.set("Installing packages in a console window… "
                                "(status refreshes when it closes)")
        messagebox.showinfo(
            "Installing packages",
            "A console window is installing the training packages into:\n"
            f"{env_utils.describe(info)}\n\n"
            "This can take a while (~3 GB for the CUDA build). Leave it running; "
            "the launcher re-checks the environment when that window closes.")

        def wait():
            proc.wait()
            self.after(0, self.refresh_env_status)
        threading.Thread(target=wait, daemon=True).start()

    def refresh_checkpoints(self):
        for w in self.ckpt_scroll.winfo_children():
            w.destroy()

        runs_dir = Path(self.workdir_var.get()) / "runs"
        checkpoints = find_checkpoints(runs_dir)
        self._checkpoint_paths = checkpoints

        ctk.CTkRadioButton(self.ckpt_scroll, text="Start fresh (no checkpoint)",
                            variable=self.checkpoint_var, value="").pack(anchor="w", pady=4, padx=8)

        if not checkpoints:
            ctk.CTkLabel(self.ckpt_scroll, text=f"(no checkpoints found in {runs_dir})",
                         text_color="gray60").pack(anchor="w", padx=8, pady=4)
        for steps, path in checkpoints:
            label = f"{steps:,} steps  —  {path.parent.name}/{path.name}"
            if path.name == "poke_26214400.zip":
                # Ships with the upstream project (the run_pretrained_interactive
                # demo model), so it isn't the user's own progress.
                label += "   (upstream demo model)"
            ctk.CTkRadioButton(self.ckpt_scroll, text=label,
                                variable=self.checkpoint_var, value=str(path)).pack(anchor="w", pady=4, padx=8)

        self.checkpoint_var.set("")

    def refresh_curriculum_points(self):
        for w in self.curr_scroll.winfo_children():
            w.destroy()

        curriculum_dir = Path(self.workdir_var.get()) / "curriculum_snapshots"
        manifest_path = curriculum_dir / "manifest.json"
        manifest = {}
        if manifest_path.exists():
            try:
                with open(manifest_path, "r") as mf:
                    manifest = json.load(mf)
            except (json.JSONDecodeError, OSError):
                manifest = {}
        self._curriculum_manifest = manifest

        ctk.CTkRadioButton(self.curr_scroll, text="Start from the very beginning (init.state)",
                            variable=self.curriculum_start_var, value="").pack(anchor="w", pady=4, padx=8)

        # Order entries by the quests' actual story progression, not
        # whatever order they happen to sit in the JSON file -- fall back to
        # raw manifest order for any quest_id that isn't recognized (e.g.
        # quest_definitions.py changed since a snapshot was captured).
        ordered_ids = []
        try:
            wd = str(Path(self.workdir_var.get()))
            if wd not in sys.path:
                sys.path.insert(0, wd)  # quest_system/ and global_map.py live in v2/
            from quest_system.quest_definitions import QUESTS
            known_order = [wp.quest_id for wp in QUESTS]
            ordered_ids = [qid for qid in known_order if qid in manifest]
            ordered_ids += [qid for qid in manifest if qid not in ordered_ids]
        except Exception:
            ordered_ids = list(manifest.keys())

        if not ordered_ids:
            ctk.CTkLabel(self.curr_scroll,
                         text=f"(no curriculum snapshots found in {curriculum_dir})",
                         text_color="gray60").pack(anchor="w", padx=8, pady=4)
        for qid in ordered_ids:
            entry = manifest[qid]
            label = f"After: {entry.get('description', qid)}"
            ctk.CTkRadioButton(self.curr_scroll, text=label,
                                variable=self.curriculum_start_var, value=qid).pack(anchor="w", pady=4, padx=8)

        self.curriculum_start_var.set("")

    def launch_training(self):
        workdir = Path(self.workdir_var.get())
        script = workdir / self.script_var.get()
        if not script.exists():
            messagebox.showerror("Script not found", f"Could not find:\n{script}")
            return

        try:
            num_cpu = int(self.num_cpu_var.get())
            cols = int(self.cols_var.get())
            ep_length = int(self.ep_length_var.get())
            explore_weight = float(self.explore_weight_var.get())
            reward_scale = float(self.reward_scale_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "num_cpu, columns, and ep_length must be integers; "
                                                    "explore_weight/reward_scale must be numbers.")
            return

        checkpoint_path = self.checkpoint_var.get()
        checkpoint_arg = ""
        if checkpoint_path:
            checkpoint_arg = str(Path(checkpoint_path).relative_to(workdir)).rsplit(".zip", 1)[0]

        python_exe = self.python_var.get().strip()
        if not python_exe or not Path(python_exe).exists():
            messagebox.showerror(
                "Python environment not found",
                "This launcher doesn't know which Python runs the training code.\n\n"
                "Fix it one of these ways:\n"
                "  • Click 'Detect Environments…' and pick the conda/venv environment\n"
                "    that has torch, stable-baselines3 and pyboy installed.\n"
                "  • Or re-run the setup wizard (PokemonRLSetup.exe) and choose that\n"
                f"    environment there; it writes {config_path()}.\n\n"
                f"Currently configured: {python_exe or '(empty)'}")
            return

        if env_utils is not None:
            info = env_utils.python_info(python_exe)
            if info:
                self._env_info = info
            missing = env_utils.missing_packages(python_exe)
            if missing:
                install = messagebox.askyesno(
                    "Packages missing",
                    "These training packages are missing from:\n"
                    f"{python_exe}\n\n  " + ", ".join(missing) +
                    "\n\nInstall them now? (choose No to launch anyway)")
                if install:
                    self.open_install_dialog()
                    return

        cmd = [
            python_exe, self.script_var.get(),
            "--ep-length", str(ep_length),
            "--explore-weight", str(explore_weight),
            "--reward-scale", str(reward_scale),
            "--num-cpu", str(num_cpu),
        ]
        if self.headless_var.get():
            cmd.append("--headless")

        self.status_label.configure(text=f"Launching (num_cpu={num_cpu})...")
        self.update_idletasks()

        # POKE_NO_RESET is read directly by quest_reward_env.py -- it's not
        # a CLI flag, so train_cli.py needs no changes for this to
        # work. Only set in the child's environment, not this process's own,
        # so toggling it in the GUI doesn't leak into anything else.
        base_env = env_utils.clean_env() if env_utils is not None else os.environ.copy()
        if env_utils is not None and self._env_info and self._env_info.get("prefix"):
            # Activates conda-like PATH (Library\bin etc.) for the child without
            # requiring `conda activate` in a real terminal. clean_env() strips
            # PyInstaller's _MEIxxxx paths when this launcher is a frozen EXE.
            child_env = env_utils.activation_env(self._env_info["prefix"], base_env=base_env)
        else:
            child_env = base_env
        child_env["POKE_NO_RESET"] = "1" if self.no_reset_var.get() else "0"
        child_env["POKE_CURRICULUM_SNAPSHOTS"] = "1" if self.save_curriculum_var.get() else "0"
        # Without this, stdout is block-buffered into runs\train_*.log and the
        # file stays empty for minutes -- the GUI's Log button would look broken.
        child_env["PYTHONUNBUFFERED"] = "1"

        curriculum_quest_id = self.curriculum_start_var.get()
        if curriculum_quest_id:
            entry = self._curriculum_manifest.get(curriculum_quest_id)
            if entry and Path(entry["path"]).exists():
                child_env["POKE_INIT_STATE_OVERRIDE"] = entry["path"]
            else:
                messagebox.showwarning(
                    "Curriculum snapshot missing",
                    f"The selected snapshot for '{curriculum_quest_id}' no longer exists on disk. "
                    "Falling back to the very start. Click Refresh to update the list.",
                )
                child_env.pop("POKE_INIT_STATE_OVERRIDE", None)
        else:
            child_env.pop("POKE_INIT_STATE_OVERRIDE", None)

        runs_dir = workdir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        log_path = runs_dir / f"train_{time.strftime('%Y%m%d_%H%M%S')}.log"
        self._train_log = None
        try:
            self._train_log = open(log_path, "w", encoding="utf-8", errors="replace")
        except OSError:
            log_path = None
        self.train_log_path = log_path

        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            cmd, cwd=str(workdir), stdin=subprocess.PIPE,
            stdout=self._train_log if self._train_log is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            env=child_env,
        )
        self.root_pid = self.proc.pid
        self.start_time = time.time()
        self.resolved_sess_path = None
        self.save_settings()

        try:
            self.proc.stdin.write((checkpoint_arg + "\n").encode())
            self.proc.stdin.close()
        except Exception as e:
            print(f"Warning: couldn't write checkpoint selection to stdin: {e}")

        if self.headless_var.get():
            self.build_headless_status_frame()
        else:
            self.build_grid_frame(num_cpu, cols)
            threading.Thread(target=self.watch_for_windows, args=(num_cpu, cols), daemon=True).start()

    # ------------------------------------------------------------------
    def build_headless_status_frame(self):
        self._clear_root_children()
        self.select_frame = None
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=24, pady=20)
        self.grid_frame = f
        self.timer_label = ctk.CTkLabel(f, text="00:00:00", font=ctk.CTkFont(size=28, weight="bold"))
        self.timer_label.pack(pady=(10, 0))
        ctk.CTkLabel(f, text="Training running headless (no windows to display).",
                     font=ctk.CTkFont(size=16)).pack(pady=(4, 20))

        self.status_label_live = ctk.CTkLabel(f, text="steps: -- | mean reward: --",
                                               font=ctk.CTkFont(size=13))
        self.status_label_live.pack(pady=(0, 6))

        self.components_label = ctk.CTkLabel(f, text="", text_color="gray60",
                                              font=ctk.CTkFont(size=11), justify="left")
        self.components_label.pack(pady=(0, 10))

        graph_holder = ctk.CTkFrame(f, fg_color="transparent")
        graph_holder.pack(fill="x", padx=40)
        self._build_mini_graph(graph_holder)

        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.pack(pady=20)
        ctk.CTkButton(btn_row, text="Open TensorBoard", width=140, command=self.launch_tensorboard).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Open Runs Folder", width=140, command=self.open_runs_folder).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Open Log", width=110, command=self.open_train_log).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Stop Training & Close", command=self.on_close).pack(side="left", padx=6)

        self.update_timer()
        self.poll_training_status()

    def build_grid_frame(self, num_cpu, cols):
        self._clear_root_children()
        self.select_frame = None

        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True)
        self.grid_frame = f

        toolbar = ctk.CTkFrame(f, fg_color="transparent")
        toolbar.pack(fill="x", padx=12, pady=10)

        self.timer_label = ctk.CTkLabel(toolbar, text="00:00:00", font=ctk.CTkFont(size=20, weight="bold"))
        self.timer_label.pack(side="left", padx=(0, 16))

        self.grid_status_label = ctk.CTkLabel(toolbar, text="Waiting for game windows to appear...")
        self.grid_status_label.pack(side="left")
        ctk.CTkButton(toolbar, text="Stop Training & Close", command=self.on_close).pack(side="right")
        ctk.CTkButton(toolbar, text="Open TensorBoard", width=130, command=self.launch_tensorboard).pack(side="right", padx=8)
        ctk.CTkButton(toolbar, text="Runs Folder", width=100, command=self.open_runs_folder).pack(side="right", padx=8)
        ctk.CTkButton(toolbar, text="Log", width=80, command=self.open_train_log).pack(side="right", padx=8)
        ctk.CTkButton(toolbar, text="Re-tile now", width=100, command=self.retile_now).pack(side="right", padx=8)
        self.update_timer()

        stats_row = ctk.CTkFrame(f, fg_color="transparent")
        stats_row.pack(fill="x", padx=12, pady=(0, 4))
        self.status_label_live = ctk.CTkLabel(stats_row, text="steps: -- | mean reward: --",
                                               font=ctk.CTkFont(size=13))
        self.status_label_live.pack(side="left")

        self._build_mini_graph(stats_row)

        self.components_label = ctk.CTkLabel(f, text="", text_color="gray60",
                                              font=ctk.CTkFont(size=11), justify="left")
        self.components_label.pack(anchor="w", padx=12, pady=(0, 8))

        self.poll_training_status()

        # Plain tk.Frame (not CTkFrame) for the embed target -- keeps a real, simple HWND to reparent into.
        self.game_area = tk.Frame(f, bg="black", highlightthickness=0, bd=0)
        self.game_area.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.game_area.bind("<Configure>", lambda e: self.retile_now())

        self.grid_cols = cols
        self.expected_windows = num_cpu

    def update_timer(self):
        if self.start_time is None or self.timer_label is None:
            return
        elapsed = int(time.time() - self.start_time)
        hrs, rem = divmod(elapsed, 3600)
        mins, secs = divmod(rem, 60)
        suffix = "" if (self.proc is not None and self.proc.poll() is None) else "  (stopped)"
        try:
            self.timer_label.configure(text=f"{hrs:02d}:{mins:02d}:{secs:02d}{suffix}")
        except Exception:
            return  # widget may have been destroyed
        self.after(1000, self.update_timer)

    def _build_mini_graph(self, parent):
        fig = Figure(figsize=(4.2, 1.1), dpi=100)
        fig.patch.set_facecolor("#2b2b2b")
        ax = fig.add_subplot(111)
        ax.set_facecolor("#2b2b2b")
        ax.tick_params(colors="gray", labelsize=7)
        for spine in ax.spines.values():
            spine.set_color("#555")
        fig.subplots_adjust(left=0.08, right=0.98, top=0.92, bottom=0.2)

        canvas = FigureCanvasTkAgg(fig, master=parent)
        canvas.get_tk_widget().pack(side="right", fill="x", expand=True, padx=(20, 0))
        self.graph_canvas = canvas
        self.graph_ax = ax

    def poll_training_status(self):
        if self.proc is None:
            return

        workdir = Path(self.workdir_var.get())

        if self.resolved_sess_path is None:
            pointer_path = workdir / "runs" / "current_run.txt"
            if pointer_path.exists():
                try:
                    resolved = pointer_path.read_text().strip()
                    if resolved:
                        self.resolved_sess_path = Path(resolved)
                        if not self.resolved_sess_path.is_absolute():
                            self.resolved_sess_path = workdir / self.resolved_sess_path
                except OSError:
                    pass

        status_path = (self.resolved_sess_path / "training_status.json") if self.resolved_sess_path else None

        if status_path and status_path.exists():
            try:
                with open(status_path) as fjson:
                    data = json.load(fjson)
                steps = data.get("steps", 0)
                mean_reward = data.get("mean_reward", 0.0)
                self.status_label_live.configure(
                    text=f"steps: {steps:,} | mean reward: {mean_reward:.2f}")
                self.reward_history.append(mean_reward)
                self._redraw_graph()

                components = data.get("components", {})
                if components and self.components_label is not None:
                    parts = [f"{k}: {v:.2f}" for k, v in components.items()]
                    self.components_label.configure(text="  ·  ".join(parts))
            except (json.JSONDecodeError, OSError):
                pass

        if self.proc.poll() is None:
            self.after(2000, self.poll_training_status)

    def _redraw_graph(self):
        if self.graph_ax is None or len(self.reward_history) < 2:
            return
        self.graph_ax.clear()
        self.graph_ax.set_facecolor("#2b2b2b")
        self.graph_ax.tick_params(colors="gray", labelsize=7)
        for spine in self.graph_ax.spines.values():
            spine.set_color("#555")
        ys = list(self.reward_history)
        self.graph_ax.plot(ys, color="#3b8ed0", linewidth=1.5)
        self.graph_ax.set_title("mean reward", color="gray", fontsize=8, loc="left")
        try:
            self.graph_canvas.draw_idle()
        except Exception:
            pass

    def retile_now(self):
        if not self.embedded_hwnds:
            return
        self.game_area.update_idletasks()
        area_w = self.game_area.winfo_width()
        area_h = self.game_area.winfo_height()
        if area_w < 10 or area_h < 10:
            return
        positions = compute_grid(len(self.embedded_hwnds), self.grid_cols, area_w, area_h, margin=4)
        for hwnd, (x, y, w, h) in zip(sorted(self.embedded_hwnds), positions):
            try:
                win32gui.SetWindowPos(hwnd, 0, x, y, w, h,
                                       win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
            except Exception as e:
                print(f"Warning: couldn't reposition window {hwnd}: {e}")

    def watch_for_windows(self, num_cpu, cols, timeout=90.0):
        start = time.time()
        found = []
        while time.time() - start < timeout:
            if self.proc.poll() is not None:
                self.after(0, lambda: self.grid_status_label.configure(
                    text="Training process exited before windows were detected."))
                return
            pids = get_descendant_pids(self.root_pid)
            found = enum_visible_windows_for_pids(pids)
            if len(found) >= num_cpu:
                break
            time.sleep(1.0)

        if not found:
            self.after(0, lambda: self.grid_status_label.configure(
                text="No game windows detected."))
            return

        self.after(0, lambda: self.embed_found_windows(found))

        while self.proc.poll() is None:
            time.sleep(3.0)
            pids = get_descendant_pids(self.root_pid)
            current = set(enum_visible_windows_for_pids(pids))
            new_ones = current - self.embedded_hwnds
            if new_ones:
                self.after(0, lambda w=list(new_ones): self.embed_found_windows(w))

    def embed_found_windows(self, hwnds):
        self.game_area.update_idletasks()
        parent_hwnd = self.game_area.winfo_id()
        for hwnd in hwnds:
            try:
                embed_window(hwnd, parent_hwnd, 0, 0, 100, 100)
                self.embedded_hwnds.add(hwnd)
            except Exception as e:
                print(f"Warning: failed to embed window {hwnd}, leaving it floating. ({e})")

        self.grid_status_label.configure(text=f"{len(self.embedded_hwnds)} game window(s) embedded.")
        self.retile_now()

    # ------------------------------------------------------------------
    def save_settings(self):
        """Persist the launcher screen so the next start matches this one."""
        def as_int(var):
            try:
                return int(var.get())
            except (ValueError, TypeError):
                return None

        def as_float(var):
            try:
                return float(var.get())
            except (ValueError, TypeError):
                return None

        env = self._env_info or {}
        save_launcher_config({
            "workdir": self.workdir_var.get().strip(),
            "script": self.script_var.get().strip(),
            "python": self.python_var.get().strip(),
            "env_name": env.get("name", ""),
            "env_prefix": env.get("prefix", ""),
            "env_source": env.get("source", ""),
            "num_cpu": as_int(self.num_cpu_var),
            "cols": as_int(self.cols_var),
            "ep_length": as_int(self.ep_length_var),
            "explore_weight": as_float(self.explore_weight_var),
            "reward_scale": as_float(self.reward_scale_var),
            "headless": bool(self.headless_var.get()),
            "no_reset": bool(self.no_reset_var.get()),
            "save_curriculum": bool(self.save_curriculum_var.get()),
        })

    def on_close(self):
        self.save_settings()
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno(
                "Stop training?",
                "This will stop training. It will try to save first (may take a few seconds). Continue?"
            ):
                return
            try:
                # CTRL_BREAK_EVENT -> mapped to SIGBREAK in train_cli.py,
                # which raises KeyboardInterrupt there and triggers its safety-save,
                # rather than being killed outright.
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self._hard_kill()
            except Exception:
                self._hard_kill()
        if self._train_log is not None:
            try:
                self._train_log.close()
            except OSError:
                pass
            self._train_log = None
        self.destroy()

    def _hard_kill(self):
        try:
            parent = psutil.Process(self.root_pid)
            for child in parent.children(recursive=True):
                child.terminate()
            parent.terminate()
        except psutil.NoSuchProcess:
            pass


if __name__ == "__main__":
    install_error_hooks()
    try:
        app = LauncherApp()
    except BaseException:
        detail = traceback.format_exc()
        log_error(detail)
        try:
            messagebox.showerror(
                "Launcher error",
                "The launcher could not start.\n\n"
                f"Details were written to:\n{error_log_path()}")
        except Exception:
            if os.name == "nt":
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        f"The launcher could not start.\n\nDetails: {error_log_path()}",
                        "Pokemon RL Launcher", 0x10)
                except Exception:
                    pass
        raise SystemExit(1)
    install_error_hooks(app)
    app.mainloop()
