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

import theme as T
import anim as A
import dashboard as D
import tiles as Tiles

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


# Design tokens, DPI awareness and (optional) bundled fonts.  M0 keeps the
# 1.0.0 look: the "dark" appearance + "blue" accent resolve to exactly the
# literals these tokens replaced.  POKE_THEME/POKE_ACCENT are the M1+ overrides.
T.enable_dpi_awareness()  # must precede the first Tk window
T.load_font_files()
T.apply(appearance=os.environ.get("POKE_THEME", "dark"),
        accent=os.environ.get("POKE_ACCENT", "blue"))
A.set_reduce_motion(A.reduce_motion_from_os())  # honour the OS accessibility setting

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
        # customtkinter multiplies a requested geometry by the monitor scaling
        # (1.25 at 125 %), so a fixed "1000x760" can hang off a 1280x720 screen.
        # Ask for a comfortable size, then clamp it to what actually fits.
        start_w, start_h = T.fit_size(self, 1040, 780)
        self.geometry(f"{start_w}x{start_h}")
        # The minimum must never be *larger* than what fits -- otherwise a small
        # screen at high DPI (e.g. 1280x720 at 150 %) is forced to clip.
        self.minsize(min(720, start_w), min(520, start_h))

        self.proc = None
        self.root_pid = None
        self.embedded_hwnds = set()
        # M4 grid chrome: the tile grid (None until the grid screen is built)
        # and the toolbar's layout-preset variable.
        self.tile_grid = None
        self.preset_var = None
        self.resolved_sess_path = None
        self.start_time = None
        self.timer_label = None
        self.status_label_live = None
        self.components_label = None
        self.reward_history = deque(maxlen=120)
        self._train_log = None
        self.train_log_path = None

        # M2 motion state: a toast layer that survives screen rebuilds, the
        # displayed (tweened) step/reward values, and the running-dot stopper.
        self.toasts = A.Toasts(self)
        self._stats_disp = {"steps": 0.0, "reward": 0.0}
        self._running_dot_stop = None
        self._env_progress = None

        # M3 dashboard state: the built dashboard widget group, the derived
        # metrics (steps/s, best reward) and the resource strip.
        self.dash = None
        self._best_reward = None
        self._prev_steps = None
        self._prev_steps_t = None
        self.resource = None

        self.cfg = load_launcher_config()

        # Appearance + accent (M1).  Precedence: env var (the screenshot/test
        # harnesses set these) -> what the user last chose -> the 1.0.0 default.
        self.appearance = (os.environ.get("POKE_THEME")
                           or str(self.cfg.get("appearance") or "dark")).lower()
        self.accent = (os.environ.get("POKE_ACCENT")
                       or str(self.cfg.get("accent") or "blue")).lower()
        if self.appearance not in ("dark", "light", "system"):
            self.appearance = "dark"
        if self.accent not in T.ACCENTS:
            self.accent = "blue"
        T.apply(appearance=self.appearance, accent=self.accent)

        # Reduce motion (M2): an in-app switch wins over the OS accessibility
        # setting.  anim.MOTION then collapses every animation duration to zero
        # (the state change still applies; only the presentation is skipped).
        rm = self.cfg.get("reduce_motion")
        A.set_reduce_motion(bool(rm) if rm is not None else A.reduce_motion_from_os())
        self.reduce_motion_var = tk.BooleanVar(value=bool(A.MOTION.reduce_motion))

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
            # Toasts belong to the window, not to a screen -- leave them up and
            # let the next screen build raise them again.
            if getattr(child, "_poke_toast", False):
                continue
            child.destroy()
        self.update_idletasks()
        # The running-dot stopper and any busy progress bar belong to the screen
        # we just tore down; stop them explicitly rather than waiting for their
        # widgets to vanish underneath them.
        if self._running_dot_stop is not None:
            try:
                self._running_dot_stop()
            except Exception:
                pass
            self._running_dot_stop = None
        if self._env_progress is not None:
            try:
                self._env_progress.destroy()
            except Exception:
                pass
            self._env_progress = None
        # The dashboard belongs to the screen we just tore down: stop its bar
        # tweens and the GPU probe thread, then drop the references.
        if self.dash is not None:
            for key in ("spark", "bars"):
                widget = self.dash.get(key)
                if widget is not None:
                    try:
                        widget.destroy()
                    except Exception:
                        pass
            self.dash = None
        if self.resource is not None:
            try:
                self.resource.stop()
            except Exception:
                pass
            self.resource = None
        # The tile grid belongs to the grid screen we just tore down.  Its tile
        # frames are already gone with the screen; releasing the animators drops
        # any outstanding timer-resolution lease.
        if self.tile_grid is not None:
            try:
                self.tile_grid.destroy()
            except Exception:
                pass
            self.tile_grid = None

    # ------------------------------------------------------------------
    # Themed chrome helpers (M1: one place defines what a card/header/dialog
    # looks like, so every screen stays consistent and restyling is one file).
    # ------------------------------------------------------------------
    def _card(self, parent, pady=None):
        """A rounded, bordered surface for a group of settings."""
        card = ctk.CTkFrame(parent, corner_radius=T.RADIUS["card"], border_width=1,
                            border_color=T.c("border"))
        card.pack(fill="x", pady=(0, T.SPACE["md"]) if pady is None else pady)
        return card

    def _card_header(self, card, text, on_refresh=None):
        """Accent-bar + bold title row, with an optional Refresh button."""
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=T.SPACE["md"], pady=(T.SPACE["md"], T.SPACE["sm"]))
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkFrame(left, width=4, height=18, fg_color=T.c("accent"),
                     corner_radius=2).pack(side="left", padx=(0, T.SPACE["sm"]))
        ctk.CTkLabel(left, text=text, font=T.f("body_bold", "bold")).pack(side="left")
        if on_refresh is not None:
            ctk.CTkButton(row, text="Refresh", width=80, command=on_refresh).pack(side="right")
        return row

    def _build_appearance_controls(self, parent):
        """Light/dark + accent switcher, top-right of the select screen."""
        block = ctk.CTkFrame(parent, fg_color="transparent")
        block.pack(side="right", anchor="n")
        ctk.CTkLabel(block, text="🎨 Appearance", text_color=T.c("text_faint"),
                     font=T.f("small")).pack(anchor="e")
        row = ctk.CTkFrame(block, fg_color="transparent")
        row.pack(anchor="e", pady=(2, 0))

        self.appearance_menu = ctk.CTkSegmentedButton(
            row, values=["Dark", "Light", "System"], width=210,
            font=T.f("small"),
            selected_color=T.c("accent"), selected_hover_color=T.c("accent_hover"),
            unselected_color=T.c("card"), unselected_hover_color=T.c("accent_soft"),
            text_color=T.c("text"),
            command=self._on_appearance_choice)
        self.appearance_menu.set(self._appearance_label())
        self.appearance_menu.pack(side="left")

        self.accent_menu = ctk.CTkOptionMenu(
            row, values=[T.accent_label(n) for n in T.accent_names()], width=120,
            font=T.f("small"), dropdown_font=T.f("small"),
            fg_color=T.c("accent"), button_color=T.c("accent"),
            button_hover_color=T.c("accent_hover"), text_color=T.c("on_accent"),
            command=self._on_accent_choice)
        self.accent_menu.set(T.accent_label(self.accent))
        self.accent_menu.pack(side="left", padx=(T.SPACE["sm"], 0))

        # Reduce motion (M2): an in-app override of the OS accessibility
        # setting.  Persisted immediately, like the appearance/accent choices.
        self.reduce_motion_switch = ctk.CTkSwitch(
            block, text="Reduce motion", font=T.f("small"),
            variable=self.reduce_motion_var, command=self._on_reduce_motion,
            progress_color=T.c("accent"), button_color=T.c("accent_hover"),
            text_color=T.c("text_dim"))
        self.reduce_motion_switch.pack(anchor="e", pady=(T.SPACE["sm"], 0))

    def _on_reduce_motion(self):
        """Honour (or stop honouring) the accessibility motion switch."""
        A.set_reduce_motion(bool(self.reduce_motion_var.get()))
        save_launcher_config({"reduce_motion": bool(self.reduce_motion_var.get())})

    def _appearance_label(self) -> str:
        return {"light": "Light", "dark": "Dark", "system": "System"}.get(
            self.appearance, "Dark")

    def _on_appearance_choice(self, label):
        """Dark/Light/System: customtkinter recolours live, so no rebuild needed."""
        self.appearance = label.lower()
        T.apply(appearance=self.appearance)
        self._persist_theme()

    def _on_accent_choice(self, label):
        """Accents are baked into widgets at construction -> rebuild the screen."""
        name = T.accent_from_label(label)
        if name == self.accent:
            return
        self.accent = name
        T.apply(accent=name)
        self._persist_theme()
        self.after(1, self.build_select_frame)

    def _persist_theme(self):
        save_launcher_config({"appearance": self.appearance, "accent": self.accent})

    def _dialog(self, title, width, height):
        """A themed, centred Toplevel that fits the screen and closes on Esc."""
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.transient(self)
        win.attributes("-topmost", True)
        cw, ch = T.fit_size(self, width, height, margin_x=T.SPACE["xl"], margin_y=88)
        scale = T.window_scale(self)
        # customtkinter scales the requested WxH itself; +x+y stays physical.
        px_w, px_h = int(cw * scale), int(ch * scale)
        sw, sh = T.screen_size(self)
        x = max(0, (sw - px_w) // 2)
        y = max(0, (sh - px_h) // 3)
        win.geometry(f"{cw}x{ch}+{x}+{y}")
        win.bind("<Escape>", lambda _e: win.destroy())
        self._dialog_intro(win, cw, ch, x, y, px_w, px_h)
        return win

    def _dialog_intro(self, win, cw, ch, x, y, px_w, px_h):
        """Scale the dialog 0.96 -> 1.0 (motion spec §3), staying centred.

        Sizes are requested in customtkinter's logical units (it multiplies them
        by the window scaling); ``+x+y`` is physical, so the centring maths works
        in physical pixels.  A no-op under reduce-motion or while a run is active.
        """
        if not A.MOTION.enabled:
            return
        row = A.spec("dialog_open")

        def apply(f):
            w, h = max(1, int(cw * f)), max(1, int(ch * f))
            ox = x + (px_w - int(px_w * f)) // 2
            oy = y + (px_h - int(px_h * f)) // 2
            try:
                win.geometry(f"{w}x{h}+{ox}+{oy}")
            except Exception:
                pass  # dialog closed mid-animation

        A.Animator(win).tween(apply, 0.96, 1.0, duration=row["ms"],
                              easing=row["easing"], tag="dialog-intro")

    def _enter_screen(self, frame):
        """A 12 px slide-in for a freshly built screen (motion spec §3).

        Only the container's pack padding is animated -- one widget, and it is a
        no-op while a run is active or under reduce-motion, so it can never cost
        the emulator a frame.  The tween's first step runs immediately, so the
        screen is already offset before its children are added.
        """
        if self.toasts is not None:
            self.toasts.reflow()
        if not A.MOTION.enabled:
            return
        try:
            base = frame.pack_info().get("pady", 0)
        except Exception:
            return
        if isinstance(base, str):
            try:
                base = int(base) if base.strip() else 0
            except ValueError:
                return
        if not isinstance(base, int):
            return  # tuple padding: skip rather than guess
        row = A.spec("page")

        def apply(v):
            try:
                frame.pack_configure(pady=int(round(v)))
            except Exception:
                pass  # screen torn down mid-slide

        A.Animator(frame).tween(apply, base + int(A.MOTION_SPEC["page_slide"]), base,
                                duration=row["ms"], easing=row["easing"],
                                tag="page-enter")

    def _running_dot(self, parent, **pack):
        """A solid "running" dot on the training screens.

        It is deliberately **static while a run is active**: the roadmap's < 2 %
        CPU budget is a hard gate, and reconfiguring a CTk widget's colour every
        frame to breathe it cannot fit under it (measured 2.2-4.1 % vs 0.64 %
        static).  ``breathe`` still applies the accent colour, so the dot looks
        identical; it would only pulse on a screen shown with motion enabled.
        """
        dot = ctk.CTkLabel(parent, text="●", font=T.f("body_bold"),
                           text_color=T.c("accent"), width=12)
        dot.pack(**pack)
        self._running_dot_stop = A.breathe(dot, period=A.MOTION_SPEC["running_dot"]["ms"])
        return dot

    def build_select_frame(self):
        self._clear_root_children()
        self.grid_frame = None

        # The whole settings screen lives inside a CTkScrollableFrame now, so
        # it never gets clipped when the window is small or the checkpoint /
        # curriculum lists grow long -- the user can just scroll the page.
        f = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=T.c("accent"),
            scrollbar_button_hover_color=T.c("accent_hover"),
        )
        f.pack(fill="both", expand=True, padx=T.SPACE["xl"], pady=T.SPACE["lg"])
        self.select_frame = f
        self._enter_screen(f)

        header = ctk.CTkFrame(f, fg_color="transparent")
        header.pack(fill="x")
        title_block = ctk.CTkFrame(header, fg_color="transparent")
        title_block.pack(side="left", anchor="n")
        ctk.CTkLabel(title_block, text="🎮 Pokemon RL Training Launcher",
                     font=T.f("h1", "bold")).pack(anchor="w")
        ctk.CTkLabel(title_block, text="Configure a run, pick a checkpoint (or start fresh), and launch.",
                     text_color=T.c("text_dim"), font=T.f("body")).pack(anchor="w", pady=(2, 0))
        self._build_appearance_controls(header)
        ctk.CTkFrame(f, height=2, fg_color=T.c("accent")).pack(
            fill="x", pady=(T.SPACE["md"], T.SPACE["lg"]))

        def section_label(parent, text):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.grid(row=0, column=0, sticky="w", columnspan=4,
                     padx=T.SPACE["md"], pady=(T.SPACE["md"], T.SPACE["sm"]))
            ctk.CTkFrame(row, width=4, height=18, fg_color=T.c("accent"),
                         corner_radius=2).pack(side="left", padx=(0, T.SPACE["sm"]))
            ctk.CTkLabel(row, text=text, font=T.f("body_bold", "bold")).pack(side="left")

        # --- Paths section ---
        sp = T.SPACE
        paths_card = self._card(f)
        section_label(paths_card, "📁 Paths")

        ctk.CTkLabel(paths_card, text="Working directory").grid(
            row=1, column=0, sticky="w", padx=sp["md"], pady=sp["sm"])
        ctk.CTkEntry(paths_card, textvariable=self.workdir_var, width=420).grid(
            row=1, column=1, padx=sp["xs"], pady=sp["sm"])
        ctk.CTkButton(paths_card, text="Browse", width=80, command=self.browse_workdir).grid(
            row=1, column=2, padx=(sp["xs"], sp["md"]))

        ctk.CTkLabel(paths_card, text="Training script").grid(
            row=2, column=0, sticky="w", padx=sp["md"], pady=(sp["sm"], sp["md"]))
        ctk.CTkEntry(paths_card, textvariable=self.script_var, width=420).grid(
            row=2, column=1, padx=sp["xs"], pady=(sp["sm"], sp["md"]))

        ctk.CTkLabel(paths_card, text="Python (training)").grid(
            row=3, column=0, sticky="w", padx=sp["md"], pady=sp["sm"])
        ctk.CTkEntry(paths_card, textvariable=self.python_var, width=420).grid(
            row=3, column=1, padx=sp["xs"], pady=sp["sm"])
        ctk.CTkButton(paths_card, text="Browse", width=80, command=self.browse_python).grid(
            row=3, column=2, padx=(sp["xs"], sp["md"]), pady=sp["sm"])

        env_row = ctk.CTkFrame(paths_card, fg_color="transparent")
        env_row.grid(row=4, column=0, columnspan=3, sticky="w",
                     padx=sp["md"], pady=(2, sp["xs"]))
        ctk.CTkButton(env_row, text="🔍 Detect Environments…", width=170,
                      command=self.detect_environments).pack(side="left")
        ctk.CTkButton(env_row, text="Install Packages…", width=150,
                      command=self.open_install_dialog).pack(side="left", padx=sp["sm"])
        ctk.CTkButton(env_row, text="Refresh status", width=120,
                      command=self.refresh_env_status).pack(side="left")

        self._env_status_label = ctk.CTkLabel(
            paths_card, textvariable=self.env_status_var, text_color=T.c("text_faint"),
            font=T.f("small"), justify="left", wraplength=680)
        self._env_status_label.grid(row=5, column=0, columnspan=3, sticky="w",
                                    padx=sp["md"], pady=(0, sp["md"]))

        # --- Run config section ---
        cfg_card = self._card(f)
        section_label(cfg_card, "⚙️ Run Configuration")

        def labeled_entry(parent, row, col, label, var, width=100):
            ctk.CTkLabel(parent, text=label).grid(
                row=row, column=col, sticky="w", padx=(sp["md"], sp["xs"]), pady=sp["sm"])
            ctk.CTkEntry(parent, textvariable=var, width=width).grid(
                row=row, column=col + 1, sticky="w", pady=sp["sm"])

        def stepper(parent, row, col, label, var, minv, maxv, step=1):
            """A '- value +' control for quickly increasing/decreasing an integer setting."""
            ctk.CTkLabel(parent, text=label).grid(
                row=row, column=col, sticky="w", padx=(sp["md"], sp["xs"]), pady=sp["sm"])

            box = ctk.CTkFrame(parent, fg_color="transparent")
            box.grid(row=row, column=col + 1, sticky="w", pady=sp["sm"])

            def clamp_set(new_val):
                new_val = max(minv, min(maxv, new_val))
                var.set(str(new_val))

            def get_val():
                try:
                    return int(var.get())
                except ValueError:
                    return minv

            ctk.CTkButton(box, text="−", width=30, corner_radius=T.RADIUS["sm"],
                          command=lambda: clamp_set(get_val() - step)).pack(side="left")
            ctk.CTkEntry(box, textvariable=var, width=52, justify="center").pack(
                side="left", padx=sp["xs"])
            ctk.CTkButton(box, text="+", width=30, corner_radius=T.RADIUS["sm"],
                          command=lambda: clamp_set(get_val() + step)).pack(side="left")

        def checkbox(parent, text, var):
            return ctk.CTkCheckBox(parent, text=text, variable=var,
                                   fg_color=T.c("accent"), hover_color=T.c("accent_hover"),
                                   checkmark_color=T.c("on_accent"), text_color=T.c("text"))

        stepper(cfg_card, 1, 0, "num_cpu (# of GUIs)", self.num_cpu_var, minv=1, maxv=16)
        stepper(cfg_card, 1, 2, "Grid columns", self.cols_var, minv=1, maxv=8)
        labeled_entry(cfg_card, 2, 0, "ep_length", self.ep_length_var, width=140)
        labeled_entry(cfg_card, 2, 2, "explore_weight", self.explore_weight_var)
        labeled_entry(cfg_card, 3, 0, "reward_scale", self.reward_scale_var)
        checkbox(cfg_card, "Headless (no game windows)", self.headless_var).grid(
            row=3, column=2, columnspan=2, sticky="w",
            pady=(sp["sm"], sp["md"]), padx=(sp["md"], 0))
        checkbox(cfg_card, "Don't reset episode (let current run continue past ep_length)",
                 self.no_reset_var).grid(
            row=4, column=0, columnspan=4, sticky="w", padx=sp["md"], pady=(0, sp["xs"]))
        checkbox(cfg_card, "Save curriculum snapshots (capture a state file the first time each quest completes)",
                 self.save_curriculum_var).grid(
            row=5, column=0, columnspan=4, sticky="w", padx=sp["md"], pady=(0, sp["xs"]))

        ctk.CTkLabel(cfg_card, text="ep_length/explore_weight/reward_scale apply to both fresh runs and resumed checkpoints.",
                     text_color=T.c("text_faint"), font=T.f("small")).grid(
            row=6, column=0, columnspan=4, sticky="w", padx=sp["md"], pady=(0, sp["xs"]))

        quick_row = ctk.CTkFrame(cfg_card, fg_color="transparent")
        quick_row.grid(row=7, column=0, columnspan=4, sticky="w",
                       padx=sp["md"], pady=(0, sp["md"]))
        ctk.CTkButton(quick_row, text="Reward Weights...", width=140,
                      command=self.open_reward_editor).pack(side="left")
        ctk.CTkButton(quick_row, text="Open TensorBoard", width=140,
                      command=self.launch_tensorboard).pack(side="left", padx=sp["sm"])
        ctk.CTkButton(quick_row, text="Open Runs Folder", width=140,
                      command=self.open_runs_folder).pack(side="left")

        # --- Checkpoint section ---
        ckpt_card = self._card(f)
        self._card_header(ckpt_card, "💾 Checkpoint", on_refresh=self.refresh_checkpoints)

        # Fixed height: the outer settings screen is itself scrollable now,
        # so this inner list just needs a sane, bounded viewport rather than
        # trying to expand to fill an infinitely-scrollable parent.
        self.ckpt_scroll = ctk.CTkScrollableFrame(ckpt_card, fg_color="transparent", height=180)
        self.ckpt_scroll.pack(fill="x", padx=sp["sm"], pady=(0, sp["md"]))

        # --- Curriculum start point section ---
        # Independent of the Checkpoint section above: checkpoint picks
        # which PPO model WEIGHTS to resume from; this picks which in-game
        # STATE the episode starts from (e.g. "already past Brock"). The
        # two combine freely -- e.g. resume a checkpoint's weights while
        # still starting each fresh episode from a curriculum snapshot.
        curr_card = self._card(f)
        self._card_header(curr_card, "🧭 Curriculum Start Point",
                          on_refresh=self.refresh_curriculum_points)
        ctk.CTkLabel(curr_card, text="Requires \"Save curriculum snapshots\" to have been enabled on a "
                                      "previous run to have anything besides \"very start\" to pick from.",
                     text_color=T.c("text_faint"), font=T.f("small")).pack(
            anchor="w", padx=sp["md"], pady=(0, sp["sm"]))

        self.curr_scroll = ctk.CTkScrollableFrame(curr_card, fg_color="transparent", height=140)
        self.curr_scroll.pack(fill="x", padx=sp["sm"], pady=(0, sp["md"]))

        # --- Launch ---
        bottom = ctk.CTkFrame(f, fg_color="transparent")
        bottom.pack(fill="x", pady=(sp["xs"], sp["xs"]))
        self.status_label = ctk.CTkLabel(bottom, text="", text_color=T.c("text_dim"))
        self.status_label.pack(side="left")
        ctk.CTkButton(bottom, text="🚀 Launch Training", height=44, width=180,
                      corner_radius=T.RADIUS["md"], font=T.f("body_bold", "bold"),
                      command=self.launch_training).pack(side="right")

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

        win = self._dialog("Reward Weights", 520, 720)

        ctk.CTkLabel(win, text="Reward Weights", font=T.f("h2", "bold")).pack(
            anchor="w", padx=T.SPACE["lg"], pady=(T.SPACE["lg"], 2))
        ctk.CTkLabel(win, text="Saves to reward_config.json in the working directory. "
                                "Running training picks up changes automatically (no restart needed).",
                     text_color=T.c("text_faint"), font=T.f("small"), justify="left",
                     wraplength=460).pack(anchor="w", padx=T.SPACE["lg"], pady=(0, T.SPACE["sm"]))

        # The weight list scrolls and the buttons/status stay pinned to the
        # bottom: the whole dialog then fits a 720-tall screen instead of asking
        # for a 1020 px window that hangs off the display.
        footer = ctk.CTkFrame(win, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=T.SPACE["lg"], pady=T.SPACE["md"])

        body = ctk.CTkScrollableFrame(
            win, fg_color="transparent",
            scrollbar_button_color=T.c("accent"),
            scrollbar_button_hover_color=T.c("accent_hover"))
        body.pack(fill="both", expand=True, padx=T.SPACE["sm"], pady=(0, T.SPACE["xs"]))

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
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", padx=T.SPACE["sm"], pady=T.SPACE["xs"])
            ctk.CTkLabel(row, text=label, width=210, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(current[key]))
            vars_map[key] = var
            ctk.CTkEntry(row, textvariable=var, width=110).pack(side="left")

        status_lbl = ctk.CTkLabel(footer, text="", text_color=T.c("text_faint"), font=T.f("small"))
        status_lbl.pack(side="left")

        def save():
            try:
                new_weights = {k: float(v.get()) for k, v in vars_map.items()}
            except ValueError:
                status_lbl.configure(text="All weights must be numbers.", text_color=T.col("danger"))
                return
            try:
                with open(config_path, "w") as f:
                    json.dump(new_weights, f, indent=2)
                status_lbl.configure(text=f"Saved to {config_path.name}", text_color=T.col("ok"))
            except OSError as e:
                status_lbl.configure(text=f"Failed to save: {e}", text_color=T.col("danger"))

        def reset_defaults():
            for key, var in vars_map.items():
                var.set(str(defaults[key]))

        ctk.CTkButton(footer, text="Save", command=save, width=110).pack(side="right")
        ctk.CTkButton(footer, text="Reset to defaults", command=reset_defaults,
                      width=160).pack(side="right", padx=T.SPACE["sm"])

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

        win = self._dialog("Detect Python Environments", 840, 600)

        ctk.CTkLabel(win, text="Which environment should run training?",
                     font=T.f("h3", "bold")).pack(anchor="w", padx=T.SPACE["lg"], pady=(T.SPACE["lg"], 2))
        ctk.CTkLabel(win, text="Conda environments, virtualenvs and system Pythons are listed. "
                               "The training libraries must be installed in the one you pick.",
                     text_color=T.c("text_faint"), font=T.f("small"), justify="left").pack(
            anchor="w", padx=T.SPACE["lg"], pady=(0, T.SPACE["sm"]))

        status = ctk.CTkLabel(win, text="Scanning for Python environments…", text_color=T.c("text_dim"))
        status.pack(anchor="w", padx=T.SPACE["lg"])
        self._env_progress = A.Progress(win, width=320, height=6)
        self._env_progress.pack(anchor="w", padx=T.SPACE["lg"], pady=(T.SPACE["xs"], 0))

        listbox = ctk.CTkScrollableFrame(win, fg_color="transparent", height=340)
        listbox.pack(fill="both", expand=True, padx=T.SPACE["md"], pady=T.SPACE["sm"])

        choice = tk.StringVar(value="")
        envs = {}

        def populate(found):
            # The scan runs on a worker thread; the user may have closed the
            # dialog before it finished, in which case its widgets are gone.
            try:
                if not (win.winfo_exists() and listbox.winfo_exists()):
                    return
            except tk.TclError:
                return
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
                                   value=label).pack(anchor="w", padx=T.SPACE["sm"], pady=T.SPACE["xs"])
            if not found:
                ctk.CTkLabel(listbox, text="No environments found. Use 'Browse python.exe…' below.",
                             text_color=T.c("text_faint")).pack(anchor="w", padx=T.SPACE["sm"], pady=T.SPACE["sm"])
            status.configure(text=f"{len(found)} environment(s) found.")
            if self._env_progress is not None:
                self._env_progress.destroy()
                self._env_progress = None
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
        btns.pack(fill="x", padx=T.SPACE["lg"], pady=(0, T.SPACE["md"]))
        ctk.CTkButton(btns, text="Rescan", width=100, command=scan).pack(side="left")
        ctk.CTkButton(btns, text="Browse python.exe…", width=160, command=browse).pack(
            side="left", padx=T.SPACE["sm"])
        ctk.CTkButton(btns, text="Cancel", width=90, command=win.destroy).pack(side="right")
        ctk.CTkButton(btns, text="Use this environment", width=170,
                      command=choose).pack(side="right", padx=T.SPACE["sm"])

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

        win = self._dialog("Install Training Packages", 700, 520)

        ctk.CTkLabel(win, text="Install training packages",
                     font=T.f("h3", "bold")).pack(anchor="w", padx=T.SPACE["lg"], pady=(T.SPACE["lg"], T.SPACE["xs"]))
        ctk.CTkLabel(win, text=env_utils.describe(info), text_color=T.c("text_dim"),
                     font=T.f("small"), justify="left", wraplength=630).pack(
            anchor="w", padx=T.SPACE["lg"], pady=(0, T.SPACE["sm"]))
        ctk.CTkLabel(win, text=("Already installed: " + ", ".join(sorted(set(env_utils.REQUIRED_PACKAGES) - set(missing)))
                                if missing else "Everything is already installed."),
                     text_color=T.c("text_faint"), font=T.f("small")).pack(anchor="w", padx=T.SPACE["lg"])
        if missing:
            ctk.CTkLabel(win, text="Missing: " + ", ".join(missing), text_color=T.col("warn"),
                         font=T.f("small")).pack(anchor="w", padx=T.SPACE["lg"], pady=(2, T.SPACE["sm"]))

        cpu_only = tk.BooleanVar(value=False)
        box = ctk.CTkFrame(win, corner_radius=T.RADIUS["card"], border_width=1,
                           border_color=T.c("border"))
        box.pack(fill="x", padx=T.SPACE["lg"], pady=(T.SPACE["sm"], T.SPACE["sm"]))
        ctk.CTkLabel(box, text="PyTorch build", font=T.f("body_bold", "bold")).pack(
            anchor="w", padx=T.SPACE["md"], pady=(T.SPACE["sm"], T.SPACE["xs"]))
        ctk.CTkRadioButton(box, text="CUDA 12.4 — NVIDIA GPU (big download, recommended if you have one)",
                           variable=cpu_only, value=False).pack(
            anchor="w", padx=T.SPACE["md"], pady=T.SPACE["xs"])
        ctk.CTkRadioButton(box, text="CPU only — smaller download, works on any PC",
                           variable=cpu_only, value=True).pack(
            anchor="w", padx=T.SPACE["md"], pady=(T.SPACE["xs"], T.SPACE["sm"]))

        ctk.CTkLabel(win, text="Runs in a new console window: torch, upstream requirements "
                               "(req-win.txt), then requirements-launcher.txt.",
                     text_color=T.c("text_faint"), font=T.f("small"), justify="left",
                     wraplength=630).pack(anchor="w", padx=T.SPACE["lg"])

        def start():
            win.destroy()
            self.start_package_install(info, v2, cpu_only.get())

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=T.SPACE["lg"], pady=T.SPACE["md"])
        ctk.CTkButton(btns, text="Install now", width=140, command=start).pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=100,
                      command=win.destroy).pack(side="right", padx=T.SPACE["sm"])

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
                            variable=self.checkpoint_var, value="").pack(anchor="w", pady=T.SPACE["xs"], padx=T.SPACE["sm"])

        if not checkpoints:
            ctk.CTkLabel(self.ckpt_scroll, text=f"(no checkpoints found in {runs_dir})",
                         text_color=T.c("text_faint")).pack(anchor="w", padx=T.SPACE["sm"], pady=T.SPACE["xs"])
        for steps, path in checkpoints:
            label = f"{steps:,} steps  —  {path.parent.name}/{path.name}"
            if path.name == "poke_26214400.zip":
                # Ships with the upstream project (the run_pretrained_interactive
                # demo model), so it isn't the user's own progress.
                label += "   (upstream demo model)"
            ctk.CTkRadioButton(self.ckpt_scroll, text=label,
                                variable=self.checkpoint_var, value=str(path)).pack(anchor="w", pady=T.SPACE["xs"], padx=T.SPACE["sm"])

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
                            variable=self.curriculum_start_var, value="").pack(anchor="w", pady=T.SPACE["xs"], padx=T.SPACE["sm"])

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
                         text_color=T.c("text_faint")).pack(anchor="w", padx=T.SPACE["sm"], pady=T.SPACE["xs"])
        for qid in ordered_ids:
            entry = manifest[qid]
            label = f"After: {entry.get('description', qid)}"
            ctk.CTkRadioButton(self.curr_scroll, text=label,
                                variable=self.curriculum_start_var, value=qid).pack(anchor="w", pady=T.SPACE["xs"], padx=T.SPACE["sm"])

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
        # Training saturates the CPU with num_cpu environments: from here until
        # it exits, the UI stops animating entirely (see anim.Motion).
        A.set_performance(True)
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
        self.toasts.show(f"Training started (num_cpu={num_cpu}).",
                         kind="ok", title="Training", timeout=3500)

    # ------------------------------------------------------------------
    def build_headless_status_frame(self):
        self._clear_root_children()
        self.select_frame = None
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=T.SPACE["xl"], pady=T.SPACE["lg"])
        self.grid_frame = f
        self._enter_screen(f)
        self.timer_label = ctk.CTkLabel(f, text="00:00:00", font=T.f("timer", "bold"))
        self.timer_label.pack(pady=(T.SPACE["sm"], 0))
        head_row = ctk.CTkFrame(f, fg_color="transparent")
        head_row.pack(pady=(T.SPACE["xs"], T.SPACE["lg"]))
        self._running_dot(head_row, side="left", padx=(0, T.SPACE["sm"]))
        ctk.CTkLabel(head_row, text="Training running headless (no windows to display).",
                     font=T.f("h4")).pack(side="left")

        self._build_dashboard(f, compact=False)

        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.pack(pady=T.SPACE["lg"])
        ctk.CTkButton(btn_row, text="Expanded chart", width=130, command=self.open_chart).pack(side="left", padx=T.SPACE["xs"])
        ctk.CTkButton(btn_row, text="Open TensorBoard", width=140, command=self.launch_tensorboard).pack(side="left", padx=T.SPACE["xs"])
        ctk.CTkButton(btn_row, text="Open Runs Folder", width=140, command=self.open_runs_folder).pack(side="left", padx=T.SPACE["xs"])
        ctk.CTkButton(btn_row, text="Open Log", width=110, command=self.open_train_log).pack(side="left", padx=T.SPACE["xs"])
        ctk.CTkButton(btn_row, text="Stop Training & Close", command=self.on_close).pack(side="left", padx=T.SPACE["xs"])

        self.update_timer()
        self.poll_training_status()

    def build_grid_frame(self, num_cpu, cols):
        self._clear_root_children()
        self.select_frame = None

        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True)
        self.grid_frame = f
        self._enter_screen(f)

        toolbar = ctk.CTkFrame(f, fg_color="transparent")
        toolbar.pack(fill="x", padx=T.SPACE["md"], pady=T.SPACE["sm"])

        self._running_dot(toolbar, side="left", padx=(0, T.SPACE["sm"]))
        self.timer_label = ctk.CTkLabel(toolbar, text="00:00:00", font=T.f("timer_grid", "bold"))
        self.timer_label.pack(side="left", padx=(0, T.SPACE["lg"]))

        # Pack the buttons first so they always keep their width; the status
        # label is packed last and absorbs whatever space is left (it is the one
        # that may be squeezed, not the controls).
        ctk.CTkButton(toolbar, text="Stop Training & Close", command=self.on_close).pack(side="right")
        ctk.CTkButton(toolbar, text="Open TensorBoard", width=130, command=self.launch_tensorboard).pack(side="right", padx=T.SPACE["sm"])
        ctk.CTkButton(toolbar, text="Runs Folder", width=100, command=self.open_runs_folder).pack(side="right", padx=T.SPACE["sm"])
        ctk.CTkButton(toolbar, text="Log", width=80, command=self.open_train_log).pack(side="right", padx=T.SPACE["sm"])
        ctk.CTkButton(toolbar, text="Re-tile now", width=100,
                      command=lambda: self.retile_now(notify=True)).pack(side="right", padx=T.SPACE["sm"])
        ctk.CTkButton(toolbar, text="Chart", width=70,
                      command=self.open_chart).pack(side="right", padx=T.SPACE["sm"])
        # Layout presets (M4 §2D): how many tiles share the grid at once.  "All"
        # keeps every env visible; a number pages them.
        self.preset_var = tk.StringVar(value=Tiles.PRESETS[0][0])
        ctk.CTkOptionMenu(toolbar, values=[label for label, _ in Tiles.PRESETS],
                          variable=self.preset_var, width=76,
                          command=self._on_preset).pack(side="right", padx=T.SPACE["sm"])
        self.grid_status_label = ctk.CTkLabel(toolbar, text="Waiting for tiles...",
                                              anchor="w")
        self.grid_status_label.pack(side="left", fill="x", expand=True)
        self.update_timer()

        self._build_dashboard(f, compact=True)

        self.poll_training_status()

        # Plain tk.Frame (not CTkFrame) for the embed target -- it gives each
        # tile body a real, simple HWND to reparent into, and lets Tk own the
        # grid layout (see tiles.py).
        self.game_area = tk.Frame(f, bg=T.col("grid_bg"), highlightthickness=0, bd=0)
        self.game_area.pack(fill="both", expand=True, padx=T.SPACE["md"], pady=(0, T.SPACE["md"]))

        self.grid_cols = cols
        self.expected_windows = num_cpu
        # Build the placeholders up front so the grid shows the expected tile
        # count before any emulator window has appeared; unfilled ones stay
        # stale with a Retry (M4 dead-tile watchdog).
        self.tile_grid = Tiles.TileGrid(self.game_area, num_cpu,
                                        embed_fn=embed_window,
                                        on_copy=self._copy_tile,
                                        on_retry=self._retry_tile)
        self.tile_grid.set_preset(self._preset_per_page())
        self.grid_status_label.configure(text=self._tiles_status_text())

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

    # ------------------------------------------------------------------
    # M3 live dashboard (pure tk/Canvas; matplotlib only in open_chart)
    # ------------------------------------------------------------------
    def _build_dashboard(self, parent, compact=False):
        """KPI cards + Canvas sparkline + component bars + resource strip.

        Reduced-motion users get the compact one-line fallback instead of the
        animated cards (ROADMAP §2C).  Nothing here imports matplotlib, so the
        2 s status tick can never redraw a figure.
        """
        # The one-line fallback is always kept and updated by _render_stats; it
        # is only *shown* for reduced-motion users (or before a status arrives).
        self.status_label_live = ctk.CTkLabel(parent, text="steps: -- | mean reward: --",
                                              font=T.f("body"))
        self.components_label = ctk.CTkLabel(parent, text="", text_color=T.c("text_faint"),
                                             font=T.f("small"), justify="left")

        if A.MOTION.reduce_motion:
            self.status_label_live.pack(anchor="w", padx=T.SPACE["md"], pady=(0, T.SPACE["xs"]))
            self.components_label.pack(anchor="w", padx=T.SPACE["md"], pady=(0, T.SPACE["sm"]))
            self.resource = D.ResourceStrip(parent)
            self.resource.pack(anchor="w", padx=T.SPACE["md"], pady=(0, T.SPACE["sm"]))
            self.resource.refresh()
            self.dash = None
            return

        cards = ctk.CTkFrame(parent, fg_color="transparent")
        cards.pack(fill="x", padx=T.SPACE["md"], pady=(0, T.SPACE["sm"]))
        kpi_spec = [
            ("steps", "STEPS", 0),
            ("steps_s", "STEPS/S", 1),
            ("mean_reward", "MEAN REWARD", 2),
            ("best_reward", "BEST REWARD", 2),
            ("elapsed", "ELAPSED", 0),
            ("tiles", "TILES", 0),
        ]
        kpi = {}
        for i, (key, caption, decimals) in enumerate(kpi_spec):
            cards.grid_columnconfigure(i, weight=1)
            card = D.KpiCard(cards, caption, width=96, height=54, decimals=decimals)
            card.frame.grid(row=0, column=i, sticky="nsew",
                            padx=(0 if i == 0 else T.SPACE["sm"], 0))
            kpi[key] = card

        mid = ctk.CTkFrame(parent, fg_color="transparent")
        mid.pack(fill="x", padx=T.SPACE["md"])
        spark = D.Sparkline(mid, width=420, height=(60 if compact else 88))
        spark.pack(side="left", fill="both", expand=True)
        bars = D.ComponentBars(mid, bar_width=(110 if compact else 150))
        bars.pack(side="left", fill="x", padx=(T.SPACE["md"], 0))

        self.resource = D.ResourceStrip(parent)
        self.resource.pack(anchor="w", padx=T.SPACE["md"], pady=(T.SPACE["xs"], T.SPACE["sm"]))
        self.resource.refresh()

        self.dash = {"kpi": kpi, "spark": spark, "bars": bars}

    def _clock(self, seconds: int) -> str:
        hrs, rem = divmod(max(0, int(seconds)), 3600)
        mins, secs = divmod(rem, 60)
        return f"{hrs:d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:d}m {secs:02d}s"

    def _update_dashboard(self, data: dict) -> None:
        """Push one status sample to the dashboard (cheap; no matplotlib)."""
        now = time.time()
        steps = int(data.get("steps", 0) or 0)
        mean_reward = float(data.get("mean_reward", 0.0) or 0.0)

        # steps/s over the wall-clock gap between samples.
        sps = 0.0
        if self._prev_steps is not None and self._prev_steps_t is not None:
            dt = now - self._prev_steps_t
            if dt > 1e-6:
                sps = max(0.0, (steps - self._prev_steps) / dt)
        self._prev_steps, self._prev_steps_t = steps, now

        self._best_reward = mean_reward if self._best_reward is None \
            else max(self._best_reward, mean_reward)
        self.reward_history.append(mean_reward)
        elapsed = int(now - self.start_time) if self.start_time else 0

        if self.dash:
            kpi = self.dash["kpi"]
            kpi["steps"].set_number(steps)
            kpi["steps_s"].set_number(sps)
            kpi["mean_reward"].set_number(mean_reward)
            kpi["best_reward"].set_number(self._best_reward)
            kpi["elapsed"].set_text(self._clock(elapsed))
            kpi["tiles"].set_text(f"{len(self.embedded_hwnds)}/{getattr(self, 'expected_windows', 0)}")
            self.dash["spark"].set_data(list(self.reward_history))
            self.dash["bars"].update(data.get("components", {}) or {})
        elif self.components_label is not None:
            components = data.get("components", {}) or {}
            if components:
                parts = [f"{k}: {v:.2f}" for k, v in components.items()]
                try:
                    self.components_label.configure(text="  ·  ".join(parts))
                except Exception:
                    pass
        if self.resource is not None:
            self.resource.refresh()

    def open_chart(self):
        """The explicit expanded matplotlib view -- deliberately off the hot path."""
        win = self._dialog("Mean reward", 720, 460)
        fig = Figure(figsize=(6.6, 3.5), dpi=100)
        fig.patch.set_facecolor(T.col("chart_bg"))
        ax = fig.add_subplot(111)
        ax.set_facecolor(T.col("chart_bg"))
        ax.tick_params(colors=T.col("chart_axis"), labelsize=T.SIZES["chart_tick"])
        for spine in ax.spines.values():
            spine.set_color(T.col("chart_grid"))
        ys = list(self.reward_history)
        if len(ys) >= 2:
            ax.plot(ys, color=T.col("chart_line"), linewidth=1.5)
        ax.set_title("mean reward", color=T.col("chart_axis"),
                     fontsize=T.SIZES["chart_title"], loc="left")
        fig.subplots_adjust(left=0.08, right=0.98, top=0.9, bottom=0.14)
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.get_tk_widget().pack(fill="both", expand=True,
                                    padx=T.SPACE["md"], pady=T.SPACE["md"])
        try:
            canvas.draw()
        except Exception:
            pass
        return win

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
                # The one-line label stays current as the reduced-motion fallback;
                # the dashboard gets the full sample.  Neither touches matplotlib.
                self._set_stats(steps, mean_reward)
                self._update_dashboard(data)
            except (json.JSONDecodeError, OSError):
                pass

        if self.proc.poll() is None:
            self.after(2000, self.poll_training_status)
        else:
            A.set_performance(False)  # run over: the UI may animate again
            self.toasts.show(f"Training finished after {self._elapsed_text()}.",
                             kind="ok", title="Run finished", timeout=6000)

    def _elapsed_text(self) -> str:
        if not self.start_time:
            return "an unknown time"
        secs = max(0, int(time.time() - self.start_time))
        hrs, rem = divmod(secs, 3600)
        mins, sec = divmod(rem, 60)
        return f"{hrs:d}h {mins:02d}m" if hrs else f"{mins:d}m {sec:02d}s"

    def _set_stats(self, steps, mean_reward):
        """Count the step/reward readout up instead of snapping.

        While a run is active (performance mode) or under reduce-motion this
        renders immediately -- the numbers change, the animation is skipped.
        """
        self._stats = {"steps": float(steps), "reward": float(mean_reward)}
        if self.status_label_live is None:
            return
        if not A.MOTION.enabled:
            self._stats_disp = dict(self._stats)
            self._render_stats()
            return
        row = A.spec("number")
        animator = A.Animator(self.status_label_live)
        animator.tween(lambda v: self._tick_stats("steps", v),
                       self._stats_disp["steps"], self._stats["steps"],
                       duration=row["ms"], easing=row["easing"], tag="stats-steps")
        animator.tween(lambda v: self._tick_stats("reward", v),
                       self._stats_disp["reward"], self._stats["reward"],
                       duration=row["ms"], easing=row["easing"], tag="stats-reward")

    def _tick_stats(self, key, value):
        self._stats_disp[key] = value
        self._render_stats()

    def _render_stats(self):
        try:
            self.status_label_live.configure(
                text=f"steps: {int(round(self._stats_disp['steps'])):,} "
                     f"| mean reward: {self._stats_disp['reward']:.2f}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # M4 grid chrome (tiles.py owns the layout; these wrappers talk to win32
    # and the toolbar/status line)
    # ------------------------------------------------------------------
    def _preset_per_page(self):
        try:
            label = self.preset_var.get() if self.preset_var is not None else "All"
        except Exception:
            label = "All"
        return dict(Tiles.PRESETS).get(label, 0)

    def _on_preset(self, label):
        if self.tile_grid is None:
            return
        self.tile_grid.set_preset(dict(Tiles.PRESETS).get(label, 0))
        self.grid_status_label.configure(text=self._tiles_status_text())

    def _tiles_status_text(self):
        if self.tile_grid is None:
            return "Waiting for tiles..."
        live = self.tile_grid.live_count()
        total = len(self.tile_grid.tiles)
        stale = self.tile_grid.stale_count()
        text = f"{live}/{total} tile(s) live"
        if stale:
            text += f"  ·  {stale} need retry"
        if self.tile_grid.pages() > 1:
            text += f"  ·  page {self.tile_grid.page + 1}/{self.tile_grid.pages()}"
        return text

    def retile_now(self, notify=False):
        """Re-snap every embedded tile to its 10:9 body (the manual Re-tile)."""
        if self.tile_grid is not None:
            try:
                self.game_area.update_idletasks()
                self.tile_grid.refit_all()
            except Exception:
                pass
            self.grid_status_label.configure(text=self._tiles_status_text())
        if notify:
            self.toasts.show(f"Re-tiled {len(self.embedded_hwnds)} tile(s).",
                             kind="info", timeout=2500)

    def watch_for_windows(self, num_cpu, cols, timeout=90.0):
        start = time.time()
        found = []
        while time.time() - start < timeout:
            if self.proc.poll() is not None:
                self.after(0, self._on_watch_gone)
                return
            pids = get_descendant_pids(self.root_pid)
            found = enum_visible_windows_for_pids(pids)
            if len(found) >= num_cpu:
                break
            time.sleep(1.0)

        if found:
            self.after(0, lambda w=list(found): self.embed_found_windows(w))
        else:
            self.after(0, self._on_watch_timeout)

        while self.proc.poll() is None:
            time.sleep(3.0)
            pids = get_descendant_pids(self.root_pid)
            current = set(enum_visible_windows_for_pids(pids))
            new_ones = current - self.embedded_hwnds
            if new_ones:
                self.after(0, lambda w=list(new_ones): self.embed_found_windows(w))
            missing = self.embedded_hwnds - current
            if missing:
                self.after(0, lambda w=list(missing): self.mark_tiles_dead(w))

    def _on_watch_gone(self):
        try:
            self.grid_status_label.configure(
                text="Training process exited before windows were detected.")
        except Exception:
            pass

    def _on_watch_timeout(self):
        # The placeholder tiles are already stale with a Retry, so the watchdog
        # surfaces per tile -- this is just the global nudge.
        try:
            self.grid_status_label.configure(text=self._tiles_status_text())
        except Exception:
            pass
        self.toasts.show("No game windows detected yet -- use Retry on a tile.",
                         kind="warn", title="Grid", timeout=5000)

    def embed_found_windows(self, hwnds):
        if self.tile_grid is None:
            return
        try:
            self.game_area.update_idletasks()
        except Exception:
            pass
        bound = self.tile_grid.assign_many(list(hwnds))
        self.embedded_hwnds = self.tile_grid.hwnds()
        try:
            self.grid_status_label.configure(text=self._tiles_status_text())
        except Exception:
            pass
        if bound:
            self.toasts.show(f"{bound} game window(s) embedded.",
                             kind="ok", timeout=3000)

    def mark_tiles_dead(self, hwnds):
        """A window disappeared mid-run: grey its tile and offer a Retry."""
        if self.tile_grid is None:
            return
        changed = self.tile_grid.mark_dead(hwnds)
        self.embedded_hwnds = self.tile_grid.hwnds()
        if changed:
            try:
                self.grid_status_label.configure(text=self._tiles_status_text())
            except Exception:
                pass
            self.toasts.show(f"{changed} tile(s) lost their window -- Retry to recover.",
                             kind="warn", timeout=4000)

    def _retry_tile(self, tile):
        """Recover a stale tile without restarting training (M4 acceptance).

        Discovery runs off the main thread -- ``EnumWindows`` walks every
        top-level window, which we must not do in the Tk callback.
        """
        if self.proc is None or self.proc.poll() is not None:
            self.toasts.show("Training has stopped; nothing to retry.",
                             kind="warn", timeout=2500)
            return
        threading.Thread(target=self._retry_tile_worker, args=(tile,),
                         daemon=True).start()

    def _retry_tile_worker(self, tile):
        try:
            pids = get_descendant_pids(self.root_pid)
            current = set(enum_visible_windows_for_pids(pids))
        except Exception:
            current = set()
        fresh = [h for h in current if h not in self.embedded_hwnds]
        if fresh:
            hwnd = fresh[0]
        elif tile.hwnd and tile.hwnd in current:
            hwnd = tile.hwnd
        else:
            self.after(0, lambda: self._retry_failed(tile))
            return
        self.after(0, lambda h=hwnd: self._retry_apply(tile, h))

    def _retry_failed(self, tile):
        tile.mark_stale("No window found")
        self.toasts.show(f"CPU {tile.index + 1} still has no window.",
                         kind="warn", timeout=3000)

    def _retry_apply(self, tile, hwnd):
        if self.tile_grid is None:
            return
        tile.hwnd = None  # let assign() re-embed from scratch
        tile.assign(hwnd, Tiles.window_title(hwnd))
        self.embedded_hwnds = self.tile_grid.hwnds()
        try:
            self.grid_status_label.configure(text=self._tiles_status_text())
        except Exception:
            pass
        self.toasts.show(f"CPU {tile.index + 1} recovered.", kind="ok", timeout=2500)

    def _copy_tile(self, tile):
        """Copy one tile's emulator screenshot to the Windows clipboard."""
        try:
            from PIL import ImageGrab
            self.game_area.update_idletasks()
            x, y = tile.stage.winfo_rootx(), tile.stage.winfo_rooty()
            w, h = tile.stage.winfo_width(), tile.stage.winfo_height()
            if w < 2 or h < 2:
                raise ValueError("tile is not visible")
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            self._clipboard_image(img)
            self.toasts.show(f"CPU {tile.index + 1} copied to the clipboard.",
                             kind="ok", timeout=2000)
        except Exception as e:  # noqa: BLE001 - report, never crash the grid
            self.toasts.show(f"Copy failed: {e}", kind="error", timeout=3000)

    @staticmethod
    def _clipboard_image(img):
        import io
        import win32clipboard
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
        data = buf.getvalue()[14:]  # drop the 14-byte BMP file header -> CF_DIB
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
        finally:
            win32clipboard.CloseClipboard()

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
            "appearance": self.appearance,
            "accent": self.accent,
            "reduce_motion": bool(self.reduce_motion_var.get()),
        })

    def on_close(self):
        self.save_settings()
        A.set_performance(False)
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
