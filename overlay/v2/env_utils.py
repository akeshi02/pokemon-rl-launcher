"""
env_utils.py

Discover Python environments (conda, venv, system) and install packages into
them. Used by both the setup wizard (installer.py) and the launcher GUI so the
two always agree about which interpreter the project runs in.

Nothing here imports third-party packages: it only shells out to the
interpreter being inspected, which keeps it usable before any packages exist
and safe to bundle inside the frozen installer.

Public API
    discover_environments(extra_roots=()) -> list[dict]
    find_env(name_or_prefix_or_python) -> dict | None
    python_info(path) -> dict | None
    package_status(python, names=REQUIRED) -> dict
    has_modules(python, names) -> dict[str, bool]
    pip_install(python, args, log=print, cwd=None) -> int
    activation_env(prefix) -> dict   # os.environ copy with conda-like PATH
    write_req_win(v2) -> Path | None
    write_install_bat(v2, python_exe, cpu_only=False) -> Path
    describe(env) -> str
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# torch 2.5.x wheels exist for 3.10-3.12; 3.13+ is not supported by the pinned
# versions in upstream's requirements.txt.
MIN_PY = (3, 10)
MAX_PY = (3, 12)

REQUIRED_PACKAGES = ("torch", "stable_baselines3", "pyboy", "gymnasium", "customtkinter")

_INFO_CODE = (
    "import json,sys;"
    "print(json.dumps({'version':list(sys.version_info[:3]),"
    "'prefix':sys.prefix,'base':sys.base_prefix,"
    "'executable':sys.executable}))"
)

_STATUS_CODE = (
    "import importlib.util, importlib.metadata as m, json\n"
    "names=%r\n"
    "out={}\n"
    "for n in names:\n"
    "    try: ok=importlib.util.find_spec(n) is not None\n"
    "    except Exception: ok=False\n"
    "    try: v=m.version(n)\n"
    "    except Exception: v=None\n"
    "    out[n]={'installed':ok,'version':v}\n"
    "print(json.dumps(out))\n"
)


# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------
def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def clean_env() -> dict:
    """os.environ minus PyInstaller's bundled-path pollution.

    A frozen (onefile) app prepends its _MEIxxxx extraction folder to PATH and
    may set PYTHONHOME/PYTHONPATH. Child processes -- conda.exe, pip, or another
    Python -- must not inherit that, or they resolve DLLs/stdlib from the
    bundle and fail with exit code 1 and no output. This is why a frozen
    installer must launch conda with a clean environment.
    """
    env = os.environ.copy()
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return env
    try:
        target = os.path.normcase(os.path.realpath(str(meipass)))
    except OSError:
        target = os.path.normcase(str(meipass))
    kept = []
    for part in env.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        try:
            same = os.path.normcase(os.path.realpath(part)) == target
        except OSError:
            same = False
        if not same:
            kept.append(part)
    env["PATH"] = os.pathsep.join(kept)
    for key in ("PYTHONHOME", "PYTHONPATH", "_MEIPASS2",
                "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE"):
        env.pop(key, None)
    return env


def _run(cmd, timeout=60, env=None):
    try:
        return subprocess.run(
            [str(c) for c in cmd],
            capture_output=True, text=True, errors="replace",
            timeout=timeout, env=env if env is not None else clean_env(),
            creationflags=_no_window(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _key(info) -> str:
    return os.path.normcase(os.path.realpath(str(info.get("python", ""))))


def python_info(path) -> dict | None:
    """Return metadata for a Python interpreter path, or None if unusable.

    ``name``/``source`` are filled in here so every caller can label the
    interpreter sensibly: a folder containing conda-meta is a conda env, a
    prefix differing from its base is a virtualenv, anything else is a plain
    path / system Python.
    """
    p = Path(str(path))
    if not p.is_file():
        return None
    r = _run([p, "-c", _INFO_CODE], timeout=30)
    if not r or r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, KeyError):
        return None
    ver = tuple(int(x) for x in d.get("version", [0, 0, 0]))
    prefix = str(d.get("prefix") or p.parent)
    base = str(d.get("base") or prefix)
    is_venv = os.path.normcase(prefix) != os.path.normcase(base)
    prefix_path = Path(prefix)
    if (prefix_path / "conda-meta").is_dir():
        name, source = prefix_path.name, "conda"
    elif is_venv:
        name, source = f"Python {'.'.join(str(x) for x in ver)}", "venv"
    else:
        name, source = f"Python {'.'.join(str(x) for x in ver)}", "path"
    return {
        "python": str(p.resolve()),
        "version": ".".join(str(x) for x in ver),
        "version_tuple": ver,
        "prefix": prefix,
        "base": base,
        "kind": "venv" if is_venv else "base",
        "compatible": MIN_PY <= ver[:2] <= MAX_PY,
        "name": name,
        "source": source,
    }


def _python_in(prefix) -> str | None:
    """python.exe at a conda/venv prefix (Windows or POSIX layout)."""
    prefix = Path(str(prefix))
    for cand in (prefix / "python.exe", prefix / "Scripts" / "python.exe", prefix / "bin" / "python"):
        if cand.is_file():
            return str(cand)
    return None


# --------------------------------------------------------------------------
# conda
# --------------------------------------------------------------------------
def find_conda_exe() -> str | None:
    cands = []
    if os.environ.get("CONDA_EXE"):
        cands.append(os.environ["CONDA_EXE"])
    w = shutil.which("conda")
    if w:
        cands.append(w)
    if os.environ.get("CONDA_PREFIX"):
        cands.append(str(Path(os.environ["CONDA_PREFIX"]) / "Scripts" / "conda.exe"))
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home)
    roots = [
        home / "miniconda3", home / "anaconda3", home / "Miniconda3", home / "Anaconda3",
        Path("C:/ProgramData/miniconda3"), Path("C:/ProgramData/anaconda3"),
        Path("C:/ProgramData/Miniconda3"), Path("C:/ProgramData/Anaconda3"),
        local / "miniconda3", local / "anaconda3",
        local / "Continuum/anaconda3",
    ]
    for root in roots:
        for sub in ("Scripts/conda.exe", "condabin/conda.bat", "conda.exe"):
            cands.append(str(root / sub))
    for c in cands:
        if c and Path(c).is_file():
            return str(Path(c))
    return None


def _conda_root(conda_exe) -> str | None:
    r = _run([conda_exe, "info", "--json"], timeout=30)
    if not r or r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("root_prefix")
    except ValueError:
        return None


def conda_envs(conda_exe=None) -> list[dict]:
    exe = str(conda_exe or find_conda_exe() or "")
    if not exe:
        return []
    r = _run([exe, "env", "list", "--json"], timeout=45)
    if not r or r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return []
    details = data.get("envs_details") or {}
    root = _conda_root(exe)
    out = []
    for prefix in data.get("envs") or []:
        py = _python_in(prefix)
        info = python_info(py) if py else None
        if not info:
            continue
        detail = details.get(prefix) or {}
        name = detail.get("name") or Path(prefix).name
        if detail.get("base") or (root and os.path.normcase(prefix) == os.path.normcase(root)):
            name = "base"
        info.update(name=name, prefix=prefix, source="conda", conda_exe=exe)
        out.append(info)
    return out


# --------------------------------------------------------------------------
# venvs + system pythons
# --------------------------------------------------------------------------
def _scan_venvs(roots) -> list[dict]:
    bases = set()
    for r in roots:
        if not r:
            continue
        p = Path(str(r))
        bases.add(p)
        bases.add(p.parent)
    bases.update({Path.cwd(), Path.home()})
    out, seen = [], set()
    for base in bases:
        for name in ("venv", ".venv", "env", ".env"):
            for sub in (base / name / "Scripts" / "python.exe", base / name / "bin" / "python"):
                if not sub.is_file():
                    continue
                info = python_info(sub)
                if not info or _key(info) in seen:
                    continue
                seen.add(_key(info))
                info.update(name=f"{base.name}/{name}", source="venv")
                out.append(info)
    return out


def _system_pythons() -> list[dict]:
    cands = []
    w = shutil.which("python")
    if w:
        cands.append(w)
    py = shutil.which("py")
    if py:
        r = _run([py, "-0p"], timeout=20)
        if r and r.returncode == 0:
            for line in r.stdout.splitlines():
                m = re.search(r"([A-Za-z]:\\.*?python\.exe)", line)
                if m:
                    cands.append(m.group(1))
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)
    out, seen = [], set()
    for c in cands:
        info = python_info(c)
        if not info or _key(info) in seen:
            continue
        seen.add(_key(info))
        info.update(name=f"Python {info['version']}", source="system")
        out.append(info)
    return out


# --------------------------------------------------------------------------
# public discovery
# --------------------------------------------------------------------------
def discover_environments(extra_roots=()) -> list[dict]:
    """All usable interpreters: conda envs, venvs near extra_roots, system."""
    found = {}
    for e in conda_envs():
        found[_key(e)] = e
    for e in _scan_venvs(extra_roots):
        found.setdefault(_key(e), e)
    for e in _system_pythons():
        found.setdefault(_key(e), e)
    order = {"conda": 0, "venv": 1, "system": 2}
    return sorted(
        found.values(),
        key=lambda e: (order.get(e.get("source"), 9), e.get("name", "").lower(),
                       e.get("python", "").lower()),
    )


def find_env(value, envs=None):
    """Resolve a conda env name, a prefix path, or a python.exe path."""
    v = str(value or "").strip()
    if not v:
        return None
    p = Path(v)
    if p.is_file():
        info = python_info(p)
        if info:
            info.setdefault("name", f"Python {info['version']}")
            info.setdefault("source", "path")
        return info
    if p.is_dir():
        py = _python_in(p)
        info = python_info(py) if py else None
        if info:
            return info
    envs = envs if envs is not None else discover_environments()
    for pred in (
        lambda e: e.get("name") == v,
        lambda e: e.get("name", "").lower() == v.lower(),
        lambda e: os.path.normcase(e.get("prefix", "")) == os.path.normcase(v),
    ):
        for e in envs:
            if pred(e):
                return e
    return None


# --------------------------------------------------------------------------
# packages
# --------------------------------------------------------------------------
def package_status(python, names=REQUIRED_PACKAGES) -> dict:
    r = _run([python, "-c", _STATUS_CODE % (list(names),)], timeout=60)
    if not r or r.returncode != 0 or not r.stdout.strip():
        return {n: {"installed": False, "version": None} for n in names}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except ValueError:
        return {n: {"installed": False, "version": None} for n in names}


def has_modules(python, names) -> dict:
    return {n: bool(v.get("installed")) for n, v in package_status(python, names).items()}


def missing_packages(python, names=REQUIRED_PACKAGES) -> list[str]:
    return [n for n, v in package_status(python, names).items() if not v.get("installed")]


def pip_install(python, args, log=print, cwd=None, env=None) -> int:
    """Run `<python> -m pip install <args>`, streaming output to log()."""
    cmd = [str(python), "-m", "pip", "install", *[str(a) for a in args]]
    log("  " + " ".join(cmd))
    child_env = env or clean_env()
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    flags = _no_window()
    try:
        p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", env=child_env,
                             creationflags=flags)
    except OSError as e:
        log(f"  could not start pip: {e}")
        return 1
    for line in p.stdout:
        line = line.rstrip()
        if line:
            log("  " + line[:200])
    return p.wait()


# --------------------------------------------------------------------------
# requirements + repair script
# --------------------------------------------------------------------------
TORCH_INDEX_GPU = "https://download.pytorch.org/whl/cu124"
TORCH_INDEX_CPU = "https://download.pytorch.org/whl/cpu"

# Pins that only make sense on Linux (triton) or are installed by the torch
# wheel itself (nvidia-*, "torch==..."). Upstream's requirements.txt lists
# them; pip on Windows fails on the Linux-only names.
_REQ_SKIP = re.compile(r"\s*(triton|nvidia-|torch==)", re.IGNORECASE)


def write_req_win(v2) -> Path | None:
    """Windows-friendly copy of upstream's requirements (no triton/nvidia pins)."""
    src = Path(v2) / "requirements.txt"
    if not src.exists():
        return None
    lines = [l for l in src.read_text(errors="replace").splitlines() if not _REQ_SKIP.match(l)]
    out = Path(v2) / "req-win.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    return out


def write_install_bat(v2, python_exe=None, cpu_only=False) -> Path:
    """A double-click repair/install script for one environment."""
    py = str(python_exe or "python").replace('"', "")
    index = TORCH_INDEX_CPU if cpu_only else TORCH_INDEX_GPU
    bat = Path(v2) / "Install Packages.bat"
    bat.write_text(
        "@echo off\r\n"
        'cd /d "%~dp0"\r\n'
        f'echo Using Python: "{py}"\r\n'
        f'"{py}" -m pip install --upgrade pip\r\n'
        f'"{py}" -m pip install torch --index-url {index}\r\n'
        f'"{py}" -m pip install -r req-win.txt\r\n'
        f'"{py}" -m pip install -r requirements-launcher.txt\r\n'
        "echo.\r\n"
        "echo Done. You can close this window.\r\n"
        "pause\r\n",
        encoding="ascii", errors="replace", newline="")
    return bat


# --------------------------------------------------------------------------
# runtime helpers
# --------------------------------------------------------------------------
def activation_env(prefix, base_env=None) -> dict:
    """Approximate `conda activate` for a directly-invoked python.exe.

    Running <prefix>\\python.exe without activation can miss DLLs that live in
    Library\\bin (cudnn, zlib, SDL2 shims). Prepending the standard conda dirs
    to PATH plus CONDA_PREFIX makes the child behave like an activated env.
    """
    env = dict(base_env if base_env is not None else clean_env())
    prefix = Path(str(prefix))
    parts = [
        prefix,
        prefix / "Library" / "mingw-w64" / "bin",
        prefix / "Library" / "usr" / "bin",
        prefix / "Library" / "bin",
        prefix / "Scripts",
        prefix / "bin",
    ]
    existing = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(
        [str(p) for p in parts if p.is_dir()] + ([existing] if existing else [])
    )
    env["CONDA_PREFIX"] = str(prefix)
    if env.get("CONDA_DEFAULT_ENV") is None:
        env["CONDA_DEFAULT_ENV"] = prefix.name
    return env


def describe(env) -> str:
    if not env:
        return "no environment selected"
    bits = [f"{env.get('name', '?')} (Python {env.get('version', '?')})"]
    if env.get("kind") == "venv":
        bits.append("venv")
    bits.append(env.get("python", ""))
    if not env.get("compatible", True):
        bits.append(f"UNSUPPORTED: needs Python {MIN_PY[0]}.{MIN_PY[1]}-{MAX_PY[0]}.{MAX_PY[1]}")
    return "  |  ".join(bits)
