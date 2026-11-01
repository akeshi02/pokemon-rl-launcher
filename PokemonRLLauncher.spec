# -*- mode: python ; coding: utf-8 -*-
"""Builds PokemonRLLauncher.exe -- the windowed launcher app.

    pyinstaller --noconfirm --clean PokemonRLLauncher.spec

The result is a single-file, console-free EXE. It is NOT the training program:
at runtime it reads launcher_config.json, finds the Python environment chosen in
the setup wizard, and starts train_cli.py with that interpreter. torch / PyBoy /
stable-baselines3 are deliberately excluded (they live in the user's env).
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")

# Conda environments keep runtime DLLs (libexpat.dll, zlib1.dll, ...) in
# <env>\Library\bin, which is only on PATH while the env is activated.  If the
# build is run through python.exe directly the dependency scan misses them and
# the frozen EXE fails to start ("DLL load failed while importing pyexpat").
# Put the directory on PATH before Analysis so extensions pick up their real
# dependencies wherever this spec is driven from.
_conda_bin = os.path.join(sys.prefix, "Library", "bin")
if os.path.isdir(_conda_bin) and _conda_bin.lower() not in os.environ["PATH"].lower().split(os.pathsep):
    os.environ["PATH"] = _conda_bin + os.pathsep + os.environ["PATH"]

hiddenimports = ctk_hidden + ["win32timezone", "theme", "anim", "dashboard", "tiles", "psutil"]

# Bundle design assets (fonts/icons) next to the app as "assets/".  theme.py
# looks them up via sys._MEIPASS when frozen; missing files fall back cleanly.
datas = list(ctk_datas)
if os.path.isdir(os.path.join("overlay", "v2", "assets")):
    datas.append((os.path.join("overlay", "v2", "assets"), "assets"))

a = Analysis(
    ["overlay/v2/pokemon_launcher_gui.py"],
    pathex=["overlay/v2"],
    binaries=ctk_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Training-side packages: the launcher only *spawns* the env's python.
        "torch", "stable_baselines3", "pyboy", "gym", "gymnasium",
        "tensorflow", "scipy", "pandas", "IPython", "notebook", "jupyter",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PokemonRLLauncher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version_launcher.txt",
    icon=os.path.join("overlay", "v2", "logo.ico"),
)
