# -*- mode: python ; coding: utf-8 -*-
"""Builds PokemonRLLauncher.exe -- the windowed launcher app.

    pyinstaller --noconfirm --clean PokemonRLLauncher.spec

The result is a single-file, console-free EXE. It is NOT the training program:
at runtime it reads launcher_config.json, finds the Python environment chosen in
the setup wizard, and starts train_cli.py with that interpreter. torch / PyBoy /
stable-baselines3 are deliberately excluded (they live in the user's env).
"""

import os

from PyInstaller.utils.hooks import collect_all

ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")

hiddenimports = ctk_hidden + ["win32timezone"]

a = Analysis(
    ["overlay/v2/pokemon_launcher_gui.py"],
    pathex=["overlay/v2"],
    binaries=ctk_binaries,
    datas=ctk_datas,
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
