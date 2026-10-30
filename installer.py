"""
installer.py -- setup wizard for Pokemon RL Launcher (builds into PokemonRLSetup.exe).

What it does
  1. Downloads the upstream project (PWhiddy/PokemonRedExperiments) from GitHub.
  2. Gets this project's added files (downloads them from your repo, or uses the
     copy bundled inside the installer if offline / repo not configured yet).
  3. Asks the user to pick THEIR OWN Pokemon Red ROM, checks it, and copies it to
     the right place named PokemonRed.gb (renaming it, or unzipping it, if needed).
  4. Lets the user choose which Python environment runs the project -- an
     existing conda/venv environment, or a new venv -- and installs the
     training packages into that environment.
  5. Writes "Start Launcher.bat" (+ optional desktop shortcut) and a
     launcher_config.json that tells the launcher which interpreter to use.

It never downloads, searches for, or links to a ROM.

Also usable without a window (for testing / silent installs):
    python installer.py --cli --dest D:\\pokemon --rom "C:\\path\\Pokemon Red.gb" --env antigravity
    python installer.py --cli --dest D:\\pokemon --python "C:\\...\\envs\\x\\python.exe" --cpu
"""

import argparse
import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "Pokémon RL Launcher"
APP_VERSION = "1.0.0"

# ---- edit these two lines before you build the installer -------------------
OVERLAY_REPO = "KanishkPatel0206/pokemon-rl-launcher"   # repo holding the overlay/ folder
OVERLAY_REF = "main"
# -----------------------------------------------------------------------------
UPSTREAM_REPO = "PWhiddy/PokemonRedExperiments"
UPSTREAM_REF = "master"
ROM_SHA1 = "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
ROM_SIZE = 1_048_576
PRESERVE = {Path("v2/reward_config.json")}  # never overwrite the user's edited weights


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _import_env_utils():
    """env_utils.py ships inside overlay/v2 (and is copied into the install).

    Frozen builds bundle it as a normal module (PokemonRLSetup.spec lists it in
    hiddenimports) instead of importing the .py file out of the extracted data
    folder: a module loaded that way is invisible to PyInstaller's analysis, so
    its own `from __future__ import annotations` failed with "No module named
    '__future__'". Only source checkouts need the sys.path fallback.
    """
    if not getattr(sys, "frozen", False):
        for c in (Path(__file__).resolve().parent / "overlay" / "v2",
                  Path(__file__).resolve().parent):
            if (c / "env_utils.py").is_file():
                sys.path.insert(0, str(c))
                break
    try:
        import env_utils  # noqa: E402  (deliberate late import)
    except ImportError:
        for c in (resource_dir() / "overlay" / "v2", resource_dir()):
            if (c / "env_utils.py").is_file():
                sys.path.insert(0, str(c))
                break
        import env_utils  # noqa: E402
    return env_utils


env_utils = _import_env_utils()

TORCH_INDEX_GPU = env_utils.TORCH_INDEX_GPU
TORCH_INDEX_CPU = env_utils.TORCH_INDEX_CPU


# ============================== core logic ===================================
def download(url, log, label):
    log(f"Downloading {label} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "pokemon-rl-setup"})
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf, done, last = io.BytesIO(), 0, -1
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            buf.write(chunk)
            done += len(chunk)
            if total and done * 10 // total != last:
                last = done * 10 // total
                log(f"  {done * 100 // total}%")
    return buf.getvalue()


def safe_extract(data, dest, prefix=None):
    """Extract a GitHub zip, dropping its top-level folder. If prefix is given,
    only members under '<top>/<prefix>/' are extracted (and that prefix is dropped)."""
    dest = Path(dest)
    dest_res = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for m in zf.infolist():
            parts = Path(m.filename).parts[1:]
            if prefix:
                pp = Path(prefix).parts
                if parts[:len(pp)] != pp:
                    continue
                parts = parts[len(pp):]
            if not parts:
                continue
            target = (dest / Path(*parts)).resolve()
            if dest_res != target and dest_res not in target.parents:
                raise RuntimeError(f"Unsafe path in zip: {m.filename}")
            if m.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as s, open(target, "wb") as o:
                    shutil.copyfileobj(s, o)


def get_overlay_dir(work, log, offline_zip=None):
    """Return a folder containing v2/... : downloaded from the repo if possible, else bundled."""
    configured = "YOUR-USERNAME" not in OVERLAY_REPO
    if configured:
        try:
            data = offline_zip or download(
                f"https://github.com/{OVERLAY_REPO}/archive/{OVERLAY_REF}.zip", log, "launcher files")
            out = Path(work) / "overlay"
            safe_extract(data, out, prefix="overlay")
            if (out / "v2" / "train_cli.py").exists():
                return out
            log("Downloaded repo had no overlay/ folder; using bundled files.")
        except Exception as e:
            log(f"Couldn't download launcher files ({e}); using the copy bundled in this installer.")
    bundled = resource_dir() / "overlay"
    if not (bundled / "v2" / "train_cli.py").exists():
        raise RuntimeError("Launcher files are missing from this installer.")
    log("Using launcher files bundled with the installer.")
    return bundled


def copy_overlay(overlay, dest, log):
    n = 0
    for src in sorted(Path(overlay).rglob("*")):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(overlay)
        tgt = Path(dest) / rel
        if rel in PRESERVE and tgt.exists():
            log(f"  kept your existing {rel}")
            continue
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, tgt)
        n += 1
    log(f"Installed {n} launcher files.")


def read_rom_bytes(src):
    """Read a ROM from a .gb/.gbc file, or from the first .gb/.gbc inside a .zip."""
    src = Path(src)
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".gb", ".gbc"))]
            if not names:
                raise ValueError("That zip doesn't contain a .gb file.")
            return zf.read(names[0])
    return src.read_bytes()


def inspect_rom(src):
    """Returns (ok, message). ok=True only for an exact SHA-1 match."""
    try:
        data = read_rom_bytes(src)
    except Exception as e:
        return False, f"Can't read that file: {e}"
    sha1 = hashlib.sha1(data).hexdigest()
    if sha1 == ROM_SHA1:
        return True, "Verified: this is the expected Pokémon Red ROM."
    if len(data) != ROM_SIZE:
        return False, (f"Unexpected size ({len(data):,} bytes; expected {ROM_SIZE:,}). "
                       "This probably isn't Pokémon Red.")
    return False, "Size looks right but the checksum differs: possibly another revision. It may not work."


def place_rom(src, root, log):
    data = read_rom_bytes(src)
    target = Path(root) / "PokemonRed.gb"
    target.write_bytes(data)
    log(f"ROM copied and named {target.name}")
    return target


# ---------------------------------------------------------------------------
# generated files: launcher config, Start Launcher.bat, Install Packages.bat
# ---------------------------------------------------------------------------
def write_launcher_config(v2, python_exe, env_mode, env=None, cpu_only=False):
    """Tell the launcher which interpreter to run train_cli.py with."""
    cfg = {
        "app": APP_NAME,
        "app_version": APP_VERSION,
        "python": str(python_exe or ""),
        "env_mode": env_mode,
        "env_name": (env or {}).get("name", ""),
        "env_prefix": (env or {}).get("prefix", ""),
        "env_source": (env or {}).get("source", ""),
        "cpu_only": bool(cpu_only),
        "installed_at": int(time.time()),
    }
    path = Path(v2) / "launcher_config.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


LAUNCHER_EXE = "PokemonRLLauncher.exe"


def bundled_launcher_exe():
    """The launcher EXE bundled inside this installer, if it was built."""
    here = Path(__file__).resolve().parent
    for base in (resource_dir() / "launcher", resource_dir(), here / "dist", here):
        cand = base / LAUNCHER_EXE
        if cand.is_file():
            return cand
    return None


def write_launcher_bat(root, python_exe=None):
    """Fallback launcher entry point; prefers the packaged EXE when present."""
    py = str(python_exe or "").replace('"', "")
    bat = Path(root) / "Start Launcher.bat"
    bat.write_text(
        "@echo off\r\n"
        'set "EXE=%~dp0PokemonRedExperiments\\v2\\' + LAUNCHER_EXE + '"\r\n'
        'if exist "%EXE%" (\r\n'
        '  start "" "%EXE%"\r\n'
        '  exit /b 0\r\n'
        ')\r\n'
        'cd /d "%~dp0PokemonRedExperiments\\v2"\r\n'
        f'set "PY={py}"\r\n'
        'if not exist "%PY%" set "PY=venv\\Scripts\\python.exe"\r\n'
        'if not exist "%PY%" set "PY=python"\r\n'
        'echo Starting launcher with: %PY%\r\n'
        '"%PY%" pokemon_launcher_gui.py\r\n'
        "pause\r\n",
        encoding="ascii", errors="replace", newline="")
    return bat


def make_desktop_shortcut(target, workdir, log):
    if os.name != "nt":
        return
    ps = ("$s=(New-Object -COM WScript.Shell).CreateShortcut("
          f"[Environment]::GetFolderPath('Desktop')+'\\{APP_NAME}.lnk');"
          f"$s.TargetPath='{target}';$s.WorkingDirectory='{workdir}';$s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log("Desktop shortcut created.")
    except Exception as e:
        log(f"Couldn't create the shortcut ({e}).")


# ---------------------------------------------------------------------------
# Python environments + packages
# ---------------------------------------------------------------------------
def find_system_python():
    """Path to a compatible (3.10-3.12) system Python, or None."""
    for v in ("3.11", "3.10", "3.12"):
        try:
            r = subprocess.run(["py", f"-{v}", "-c", "import sys;print(sys.executable)"],
                               capture_output=True, text=True, timeout=20,
                               env=env_utils.clean_env(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            r = None
        if r and r.returncode == 0 and r.stdout.strip():
            info = env_utils.python_info(r.stdout.strip().splitlines()[-1])
            if info and info["compatible"]:
                return info["python"]
    candidates = [shutil.which("python")]
    if not getattr(sys, "frozen", False):
        candidates.append(sys.executable)
    for cand in candidates:
        if not cand:
            continue
        info = env_utils.python_info(cand)
        if info and info["compatible"]:
            return info["python"]
    return None


def run_logged(cmd, log, cwd=None):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", env=env_utils.clean_env(),
                         creationflags=flags)
    for line in p.stdout:
        line = line.rstrip()
        if line:
            log("  " + line[:160])
    return p.wait()


def install_packages(v2, log, python_exe=None, cpu_only=False, dry_run=False):
    """Create a venv (python_exe=None) or use an existing interpreter, then pip install.

    Returns the interpreter path the project should use, or None on failure.
    """
    v2 = Path(v2)
    env = None
    if python_exe:
        info = env_utils.python_info(python_exe)
        if not info:
            log(f"'{python_exe}' is not a usable Python interpreter.")
            return None
        log(f"Using existing environment: {env_utils.describe(info)}")
        if not info["compatible"]:
            log(f"WARNING: this environment is Python {info['version']}; the training "
                f"packages expect {env_utils.MIN_PY[0]}.{env_utils.MIN_PY[1]}-"
                f"{env_utils.MAX_PY[0]}.{env_utils.MAX_PY[1]}.")
        py_exe, env = info["python"], info
    else:
        py = find_system_python()
        if not py:
            log("Python 3.10-3.12 was not found on PATH, so packages were NOT installed.\n"
                "Install Python 3.11 from python.org (tick 'Add to PATH'), then run "
                "Install Packages.bat.")
            return None
        if dry_run:
            log(f"[dry-run] would create venv with {py}")
            return str(v2 / "venv" / "Scripts" / "python.exe")
        log("Creating Python environment (venv) ...")
        if run_logged([py, "-m", "venv", "venv"], log, cwd=v2) != 0:
            return None
        py_exe = str(v2 / "venv" / "Scripts" / "python.exe")
        env = env_utils.python_info(py_exe)

    req = env_utils.write_req_win(v2)
    if req is None:
        log("requirements.txt not found next to the project -- cannot install packages.")
        return py_exe

    index = TORCH_INDEX_CPU if cpu_only else TORCH_INDEX_GPU
    steps = [
        ("Upgrading pip ...", ["--upgrade", "pip"]),
        (f"PyTorch ({'CPU' if cpu_only else 'CUDA 12.4'} build, large download) ...",
         ["torch", "--index-url", index]),
        ("Training packages ...", ["-r", "req-win.txt"]),
        ("Launcher packages ...", ["-r", "requirements-launcher.txt"]),
    ]
    child_env = env_utils.activation_env((env or {}).get("prefix") or Path(py_exe).parent)

    for label, args in steps:
        log(label)
        if dry_run:
            log("  [dry-run] " + " ".join([py_exe, "-m", "pip", "install", *args]))
            continue
        code = env_utils.pip_install(py_exe, args, log, cwd=v2, env=child_env)
        if code != 0 and label != "Upgrading pip ...":
            log("A package install failed -- see the lines above.")
            return py_exe

    missing = env_utils.missing_packages(py_exe)
    if missing and not dry_run:
        log(f"Warning: still missing after install: {', '.join(missing)}")
    else:
        log("Python packages installed.")
    return py_exe


def run_install(opts, log, step):
    """opts: dest, rom, env_mode (venv|existing|skip), python, install_packages,
    cpu_only, shortcut, dry_run, offline_upstream_zip, offline_overlay_zip."""
    root = Path(opts["dest"]) / "PokemonRedExperiments"
    v2 = root / "v2"
    Path(opts["dest"]).mkdir(parents=True, exist_ok=True)

    step(0.05)
    if (v2 / "red_gym_env_v2.py").exists():
        log("Original project already present -- reusing it.")
    else:
        data = opts.get("offline_upstream_zip") or download(
            f"https://github.com/{UPSTREAM_REPO}/archive/{UPSTREAM_REF}.zip", log, "original project")
        log("Extracting ...")
        safe_extract(data, root)
    step(0.35)

    with tempfile.TemporaryDirectory() as tmp:
        overlay = get_overlay_dir(tmp, log, opts.get("offline_overlay_zip"))
        copy_overlay(overlay, root, log)
    launcher_exe = bundled_launcher_exe()
    if launcher_exe:
        shutil.copy2(launcher_exe, v2 / LAUNCHER_EXE)
        log(f"Installed the launcher app ({LAUNCHER_EXE}).")
    step(0.5)

    if opts.get("rom"):
        place_rom(opts["rom"], root, log)
    else:
        log(f"No ROM selected. Later, copy your ROM to:\n  {root / 'PokemonRed.gb'}")
    step(0.6)

    mode = opts.get("env_mode", "skip")
    env_info = opts.get("env_info")
    py_exe = None
    if mode == "existing":
        if not env_info:
            env_info = (env_utils.find_env(opts.get("python"))
                        if opts.get("python") else None)
        if env_info:
            py_exe = env_info["python"]
        else:
            log(f"Selected environment is no longer usable: {opts.get('python')!r}")
    if mode in ("venv", "existing") and opts.get("install_packages", True):
        log("")
        py_exe = install_packages(
            v2, log,
            python_exe=env_info["python"] if env_info else None,
            cpu_only=opts.get("cpu_only", False),
            dry_run=opts.get("dry_run", False),
        ) or py_exe
        if py_exe and not env_info:
            env_info = env_utils.python_info(py_exe)
    step(0.85)

    env_utils.write_req_win(v2)
    write_launcher_config(v2, py_exe, mode, env_info, opts.get("cpu_only", False))
    env_utils.write_install_bat(v2, py_exe, opts.get("cpu_only", False))
    bat = write_launcher_bat(opts["dest"], py_exe)
    if opts.get("shortcut"):
        exe = v2 / LAUNCHER_EXE
        if exe.exists():
            make_desktop_shortcut(exe, v2, log)
        else:
            make_desktop_shortcut(bat, bat.parent, log)
    step(1.0)

    if py_exe:
        log(f"\nTraining will run with: {py_exe}")
    log("\nDone.")
    return bat


# ================================= GUI =======================================
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} Setup")
    root.geometry("660x560")
    root.resizable(False, False)

    v_dest = tk.StringVar(value=str(Path.home() / "PokemonRLLauncher"))
    v_rom = tk.StringVar()
    v_skip = tk.BooleanVar(value=False)
    v_pkgs = tk.BooleanVar(value=True)
    v_short = tk.BooleanVar(value=True)
    v_romstatus = tk.StringVar()
    v_envmode = tk.StringVar(value="existing")      # existing | venv | skip
    v_envpick = tk.StringVar(value="")
    v_envstatus = tk.StringVar(value="")
    v_cpu = tk.BooleanVar(value=False)
    q = queue.Queue()
    state = {"page": 0, "installing": False, "bat": None,
             "envs": [], "env_by_label": {}, "selected_env": None,
             "env_combo": None}

    body = ttk.Frame(root, padding=22)
    body.pack(fill="both", expand=True)
    nav = ttk.Frame(root, padding=(22, 0, 22, 18))
    nav.pack(fill="x")
    btn_back = ttk.Button(nav, text="Back")
    btn_next = ttk.Button(nav, text="Next")
    btn_back.pack(side="left")
    btn_next.pack(side="right")

    pages = [ttk.Frame(body) for _ in range(6)]

    def head(p, text, sub=None):
        ttk.Label(p, text=text, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        if sub:
            ttk.Label(p, text=sub, wraplength=610, justify="left").pack(anchor="w", pady=(6, 14))

    # 0 welcome
    p = pages[0]
    try:
        logo = tk.PhotoImage(file=str(resource_dir() / "logo.png")).subsample(5, 5)
        lbl = ttk.Label(p, image=logo)
        lbl.image = logo
        lbl.pack(anchor="w", pady=(0, 8))
    except Exception:
        pass
    head(p, f"Welcome to {APP_NAME}",
         "This wizard will:\n\n  •  download the original project by Peter Whidden (MIT) from GitHub\n"
         "  •  add the launcher, reward editor and quest tools\n"
         "  •  let you choose which Python environment runs the project\n"
         "  •  install the training libraries into that environment\n"
         "  •  ask for your Pokémon Red ROM and put it in the right place, renamed if needed\n\n"
         "You need an internet connection and a copy of the ROM that you legally own. "
         "The ROM is not included and is never downloaded.")

    # 1 folder
    p = pages[1]
    head(p, "Where should it be installed?", "Choose a folder you can write to (avoid Program Files).")
    row = ttk.Frame(p)
    row.pack(fill="x")
    ttk.Entry(row, textvariable=v_dest, width=64).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="Browse…", command=lambda: v_dest.set(
        filedialog.askdirectory(initialdir=v_dest.get()) or v_dest.get())).pack(side="left", padx=(8, 0))
    ttk.Checkbutton(p, text="Create a desktop shortcut", variable=v_short).pack(anchor="w", pady=(18, 4))
    ttk.Label(p, text="The project files are downloaded here; Python packages go into the "
                      "environment you pick on the next step.",
              wraplength=610, foreground="gray40").pack(anchor="w", pady=(2, 0))

    # 2 ROM
    p = pages[2]
    head(p, "Select your Pokémon Red ROM",
         "Pick the ROM file you legally own (.gb, or a .zip containing it). Any file name is fine; "
         "the installer copies it into the project as PokemonRed.gb.")
    row = ttk.Frame(p)
    row.pack(fill="x")
    ttk.Entry(row, textvariable=v_rom, width=64).pack(side="left", fill="x", expand=True)

    def browse_rom():
        f = filedialog.askopenfilename(title="Select Pokémon Red ROM",
                                       filetypes=[("Game Boy ROM", "*.gb *.gbc *.zip"), ("All files", "*.*")])
        if f:
            v_rom.set(f)

    ttk.Button(row, text="Browse…", command=browse_rom).pack(side="left", padx=(8, 0))
    status_lbl = ttk.Label(p, textvariable=v_romstatus, wraplength=610, justify="left")
    status_lbl.pack(anchor="w", pady=(12, 0))
    ttk.Checkbutton(p, text="I'll add the ROM later (skip this step)", variable=v_skip).pack(anchor="w", pady=(18, 0))

    def on_rom_change(*_):
        f = v_rom.get().strip()
        if not f:
            v_romstatus.set("")
        elif not Path(f).is_file():
            v_romstatus.set("File not found.")
        else:
            ok, msg = inspect_rom(f)
            v_romstatus.set(("✔ " if ok else "⚠ ") + msg)
    v_rom.trace_add("write", on_rom_change)

    # 3 environment
    p = pages[3]
    head(p, "Which Python environment should run the project?",
         "Pick the environment that will run train_cli.py and the launcher. The training "
         "libraries are installed into it; the project files always go to the folder above.")

    mode_box = ttk.Frame(p)
    mode_box.pack(fill="x")
    rb_venv = ttk.Radiobutton(mode_box, text="Create a new environment (venv) in the install folder",
                              variable=v_envmode, value="venv", command=lambda: on_envmode(scan=True))
    rb_exist = ttk.Radiobutton(mode_box, text="Use an existing conda / venv / system environment",
                               variable=v_envmode, value="existing", command=lambda: on_envmode(scan=True))
    rb_skip = ttk.Radiobutton(mode_box, text="Skip — I'll set it up later",
                              variable=v_envmode, value="skip", command=lambda: on_envmode())
    rb_venv.pack(anchor="w", pady=2)
    rb_exist.pack(anchor="w", pady=2)

    exist_row = ttk.Frame(p)
    exist_row.pack(fill="x", pady=(6, 0))
    combo = ttk.Combobox(exist_row, textvariable=v_envpick, state="disabled", width=62)
    combo.pack(side="left", fill="x", expand=True)
    state["env_combo"] = combo
    btn_refresh = ttk.Button(exist_row, text="Refresh", width=8, command=lambda: scan_envs())
    btn_refresh.pack(side="left", padx=(6, 0))
    btn_browse_env = ttk.Button(exist_row, text="Browse…", width=9, command=lambda: browse_env())
    btn_browse_env.pack(side="left", padx=(6, 0))
    rb_skip.pack(anchor="w", pady=(8, 0))

    chk_pkgs = ttk.Checkbutton(p, text="Install the training packages into this environment (~3 GB)",
                               variable=v_pkgs)
    chk_pkgs.pack(anchor="w", pady=(14, 2))
    chk_cpu = ttk.Checkbutton(p, text="Use CPU-only PyTorch (smaller download, works without an NVIDIA GPU)",
                              variable=v_cpu)
    chk_cpu.pack(anchor="w")
    env_lbl = ttk.Label(p, textvariable=v_envstatus, wraplength=610, justify="left",
                        foreground="gray30")
    env_lbl.pack(anchor="w", pady=(12, 0))

    def on_envmode(scan=False):
        existing = v_envmode.get() == "existing"
        combo.configure(state="readonly" if existing else "disabled")
        btn_refresh.configure(state="normal" if existing else "disabled")
        btn_browse_env.configure(state="normal" if existing else "disabled")
        chk_pkgs.configure(state="normal" if v_envmode.get() != "skip" else "disabled")
        chk_cpu.configure(state="normal" if v_envmode.get() != "skip" else "disabled")
        if existing:
            if scan and not state["envs"]:
                scan_envs()
            elif not state["selected_env"] and state["envs"]:
                pick_default_env()
        v_envstatus.set("" if not existing else v_envstatus.get())

    def pick_default_env():
        labels = list(state["env_by_label"])
        if not labels:
            return
        preferred = next((l for l, e in state["env_by_label"].items()
                          if e.get("source") == "conda" and e.get("compatible")
                          and e.get("name") != "base"), None)
        preferred = preferred or next((l for l, e in state["env_by_label"].items()
                                       if e.get("compatible")), labels[0])
        v_envpick.set(preferred)
        on_envpick()

    def on_envpick(*_):
        env = state["env_by_label"].get(v_envpick.get())
        if not env:
            return
        state["selected_env"] = env
        missing = env_utils.missing_packages(env["python"])
        text = env_utils.describe(env)
        if missing:
            text += "\nWill install: " + ", ".join(missing)
        else:
            text += "\nAll required packages are already installed."
        if not env.get("compatible", True):
            text += (f"\n⚠ Unsupported Python version for the pinned training packages "
                     f"(need {env_utils.MIN_PY[0]}.{env_utils.MIN_PY[1]}-"
                     f"{env_utils.MAX_PY[0]}.{env_utils.MAX_PY[1]}).")
        v_envstatus.set(text)
    v_envpick.trace_add("write", on_envpick)

    def scan_envs():
        v_envstatus.set("Scanning for Python environments…")
        dest = Path(v_dest.get() or Path.cwd())

        def work():
            try:
                found = env_utils.discover_environments(extra_roots=[dest])
            except Exception:
                found = []
            q.put(("envs", found))
        threading.Thread(target=work, daemon=True).start()

    def browse_env():
        f = filedialog.askopenfilename(title="Select the environment's python.exe",
                                       filetypes=[("python.exe", "python.exe"), ("All files", "*.*")])
        if not f:
            return
        info = env_utils.python_info(f)
        if not info:
            messagebox.showwarning("Not a Python interpreter",
                                   f"Couldn't run:\n{f}\n\nPick the environment's python.exe.")
            return
        info.setdefault("name", f"Python {info['version']}")
        info.setdefault("source", "path")
        label = env_utils.describe(info)
        state["env_by_label"][label] = info
        values = list(combo["values"])
        if label not in values:
            values.append(label)
        combo["values"] = values
        v_envmode.set("existing")
        v_envpick.set(label)
        on_envmode()
        on_envpick()

    # 4 install
    p = pages[4]
    head(p, "Installing…")
    bar = ttk.Progressbar(p, maximum=1.0)
    bar.pack(fill="x", pady=(0, 10))
    logbox = tk.Text(p, height=16, wrap="word", state="disabled", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True)

    # 5 done
    p = pages[5]
    head(p, "Setup complete")
    done_lbl = ttk.Label(p, wraplength=610, justify="left")
    done_lbl.pack(anchor="w")

    def log_ui(msg):
        logbox.configure(state="normal")
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    log_ui(val)
                elif kind == "step":
                    bar["value"] = val
                elif kind == "envs":
                    state["envs"] = val
                    state["env_by_label"] = {}
                    labels = []
                    for e in val:
                        label = env_utils.describe(e)
                        if label in state["env_by_label"]:
                            label += "  [duplicate name]"
                        state["env_by_label"][label] = e
                        labels.append(label)
                    combo["values"] = labels
                    if not labels:
                        v_envstatus.set("No Python environments found. Use Browse… or create a new venv.")
                    elif v_envmode.get() == "existing":
                        pick_default_env()
                elif kind == "done":
                    state["installing"] = False
                    state["bat"] = val
                    root.after(600, lambda: show(5))
                elif kind == "error":
                    state["installing"] = False
                    messagebox.showerror("Setup failed", val)
                    btn_back.state(["!disabled"])
                    btn_next.state(["!disabled"])
                    btn_next.configure(text="Retry")
        except queue.Empty:
            pass
        root.after(100, poll)

    def start_install():
        state["installing"] = True
        btn_back.state(["disabled"])
        btn_next.state(["disabled"])
        mode = v_envmode.get()
        selected = state.get("selected_env") or {}
        opts = {
            "dest": v_dest.get().strip(),
            "rom": "" if v_skip.get() else v_rom.get().strip(),
            "env_mode": mode,
            "python": selected.get("python", "") if mode == "existing" else "",
            "env_info": selected if mode == "existing" else None,
            "install_packages": bool(v_pkgs.get()) and mode != "skip",
            "cpu_only": bool(v_cpu.get()),
            "shortcut": v_short.get(),
        }

        def work():
            try:
                bat = run_install(opts, lambda m: q.put(("log", m)), lambda f: q.put(("step", f)))
                q.put(("done", bat))
            except Exception as e:
                q.put(("error", f"{e}"))
        threading.Thread(target=work, daemon=True).start()

    def show(i):
        state["page"] = i
        for pg in pages:
            pg.pack_forget()
        pages[i].pack(fill="both", expand=True)
        btn_back.state(["disabled"] if i in (0, 4, 5) else ["!disabled"])
        btn_next.state(["!disabled"])
        btn_next.configure(text={0: "Next", 1: "Next", 2: "Next", 3: "Install", 4: "Install", 5: "Finish"}[i])
        if i == 3 and v_envmode.get() == "existing" and not state["envs"]:
            scan_envs()
        if i == 5:
            has_rom = bool(v_rom.get().strip()) and not v_skip.get()
            rom_line = ("Your ROM was copied and renamed to PokemonRed.gb.\n" if has_rom else
                        "Remember to copy your ROM to PokemonRedExperiments\\PokemonRed.gb\n")
            selected = state.get("selected_env") or {}
            if v_envmode.get() == "existing" and selected:
                env_line = f"Environment: {env_utils.describe(selected)}\n"
            elif v_envmode.get() == "venv" and v_pkgs.get():
                env_line = "Environment: new venv in PokemonRedExperiments\\v2\\venv\n"
            else:
                env_line = "Environment: not configured (set it later in the launcher).\n"
            done_lbl.configure(text=(
                "Installed to:\n  " + v_dest.get() + "\n\n" + env_line + rom_line +
                "\nStart the launcher with the desktop shortcut, 'Start Launcher.bat', or\n"
                "PokemonRedExperiments\\v2\\PokemonRLLauncher.exe.\n"
                "Tip: you can change the environment later with 'Detect Environments…' in the launcher."))
        if i == 4:
            btn_back.state(["disabled"])
            btn_next.state(["disabled"])

    def go_next():
        i = state["page"]
        if i == 1 and not v_dest.get().strip():
            messagebox.showwarning("Folder needed", "Please choose an install folder.")
            return
        if i == 2:
            f = v_rom.get().strip()
            if not v_skip.get():
                if not f or not Path(f).is_file():
                    messagebox.showwarning("ROM needed", "Select your ROM file, or tick "
                                           "'I'll add the ROM later'.")
                    return
                ok, msg = inspect_rom(f)
                if not ok and not messagebox.askyesno("Unverified ROM", msg + "\n\nUse it anyway?"):
                    return
            show(3)
            return
        if i == 3:
            if v_envmode.get() == "existing":
                env = state.get("selected_env")
                if not env:
                    messagebox.showwarning("Environment needed",
                                           "Pick an environment from the list, use Browse…, "
                                           "or choose 'Create a new environment'.")
                    return
                if not env.get("compatible", True):
                    if not messagebox.askyesno(
                            "Unsupported Python version",
                            f"{env_utils.describe(env)}\n\n"
                            f"The training packages need Python {env_utils.MIN_PY[0]}.{env_utils.MIN_PY[1]}-"
                            f"{env_utils.MAX_PY[0]}.{env_utils.MAX_PY[1]}. Continue anyway?"):
                        return
            show(4)
            start_install()
            return
        if i == 4:  # retry
            show(4)
            start_install()
            return
        if i == 5:
            root.destroy()
            return
        show(i + 1)

    btn_next.configure(command=go_next)
    btn_back.configure(command=lambda: show(state["page"] - 1))
    root.protocol("WM_DELETE_WINDOW",
                  lambda: None if state["installing"] else root.destroy())
    show(0)
    poll()
    on_envmode()
    root.mainloop()


# ================================= CLI =======================================
def run_cli(a):
    log_file = None
    if a.log:
        try:
            log_file = open(a.log, "w", encoding="utf-8", errors="replace")
        except OSError as e:
            sys.exit(f"Can't write the log file: {e}")

    def log(msg):
        text = str(msg)
        if log_file is not None:
            try:
                log_file.write(text + "\n")
                log_file.flush()
            except OSError:
                pass
        try:
            print(text)
        except Exception:
            pass  # windowed EXE: there is no stdout

    def fail(msg, code=1):
        log(msg)
        if log_file is not None:
            log_file.close()
        sys.exit(code)

    mode = "skip"
    python_exe = ""
    env_info = None
    try:
        if a.env:
            env_info = env_utils.find_env(a.env)
            if not env_info:
                fail(f"Environment '{a.env}' was not found. "
                     f"Try `conda env list` or pass --python <path>.")
            log(f"Using environment: {env_utils.describe(env_info)}")
            python_exe, mode = env_info["python"], "existing"
        elif a.python:
            env_info = env_utils.python_info(a.python)
            if not env_info:
                fail(f"'{a.python}' is not a usable Python interpreter.")
            env_info.setdefault("name", f"Python {env_info['version']}")
            env_info.setdefault("source", "path")
            log(f"Using environment: {env_utils.describe(env_info)}")
            python_exe, mode = env_info["python"], "existing"
        elif a.packages:
            mode = "venv"

        packages = a.packages or bool(python_exe)
        if a.no_packages:
            packages = False

        opts = {
            "dest": a.dest, "rom": a.rom or "", "install_packages": packages,
            "env_mode": mode, "python": python_exe, "env_info": env_info,
            "cpu_only": a.cpu, "shortcut": False, "dry_run": a.dry_run,
        }
        if a.upstream_zip:
            opts["offline_upstream_zip"] = Path(a.upstream_zip).read_bytes()
        if a.rom:
            ok, msg = inspect_rom(a.rom)
            log(("[ok] " if ok else "[!]  ") + msg)
        run_install(opts, log, lambda f: None)
    except SystemExit:
        raise
    except Exception:
        import traceback
        log(traceback.format_exc())
        fail("Setup failed -- see the log above.", 2)
    finally:
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass


def _ensure_stdio():
    """A console-free (windowed) build has sys.stdout/stderr == None.

    argparse writes usage/help to those streams, so `PokemonRLSetup.exe --cli
    --help` used to die inside argparse and leave only a modal traceback dialog.
    Point them at the null device instead; --log captures the useful lines.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def _crash_log_path():
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        for cand in (exe.with_name(exe.stem + "-crash.log"),
                     Path(tempfile.gettempdir()) / "PokemonRLSetup-crash.log"):
            try:
                cand.parent.mkdir(parents=True, exist_ok=True)
                return cand
            except OSError:
                continue
    return Path(tempfile.gettempdir()) / "PokemonRLSetup-crash.log"


def _report_crash(detail):
    """Never let a windowed EXE fail silently: log the traceback and tell the user."""
    path = None
    try:
        path = _crash_log_path()
        path.write_text(detail, encoding="utf-8", errors="replace")
    except OSError:
        path = None
    where = f"\n\nDetails were written to:\n{path}" if path else ""
    text = ("Pokemon RL Setup hit an unexpected error and has to close."
            f"{where}\n\n{detail.strip().splitlines()[-1] if detail.strip() else ''}")
    try:
        if sys.stderr is not None:
            print(detail, file=sys.stderr)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "Pokemon RL Setup", 0x10)
        except Exception:
            pass


def main(argv=None):
    _ensure_stdio()
    ap = argparse.ArgumentParser(prog="PokemonRLSetup")
    ap.add_argument("--cli", action="store_true", help="run without a window")
    ap.add_argument("--dest", default=str(Path.home() / "PokemonRLLauncher"))
    ap.add_argument("--rom", help="path to your ROM (.gb or .zip)")
    ap.add_argument("--env", help="conda environment name (or prefix) to install into")
    ap.add_argument("--python", help="python.exe of the environment to install into")
    ap.add_argument("--packages", action="store_true", help="create a venv and install packages")
    ap.add_argument("--no-packages", action="store_true", help="use the environment but skip pip install")
    ap.add_argument("--cpu", action="store_true", help="install CPU-only PyTorch")
    ap.add_argument("--dry-run", action="store_true", help="print the pip commands without running them")
    ap.add_argument("--log", help="write the install log to this file (useful for the windowed EXE)")
    ap.add_argument("--upstream-zip", help="use a local upstream zip instead of downloading (testing)")
    args = ap.parse_args(argv)
    if args.cli:
        run_cli(args)
    else:
        run_gui()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        _report_crash(traceback.format_exc())
        sys.exit(1)
