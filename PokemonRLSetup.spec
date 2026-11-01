# -*- mode: python ; coding: utf-8 -*-
"""Builds PokemonRLSetup.exe -- the setup wizard.

    pyinstaller --noconfirm --clean PokemonRLSetup.spec

The wizard bundles the overlay/ folder (project additions), the logo and -- when
it has been built first -- dist/PokemonRLLauncher.exe, which it installs next to
the project so end users only ever double-click an EXE.
"""

import os
import sys

datas = [("overlay", "overlay"), ("logo.png", ".")]

# Same conda note as PokemonRLLauncher.spec: DLLs live in <env>\Library\bin,
# which is only on PATH while the env is activated.
_conda_bin = os.path.join(sys.prefix, "Library", "bin")
if os.path.isdir(_conda_bin) and _conda_bin.lower() not in os.environ["PATH"].lower().split(os.pathsep):
    os.environ["PATH"] = _conda_bin + os.pathsep + os.environ["PATH"]

_launcher = os.path.join("dist", "PokemonRLLauncher.exe")
if os.path.isfile(_launcher):
    datas.append((_launcher, "launcher"))
else:
    print("[build] dist/PokemonRLLauncher.exe not found -- the installed app will "
          "fall back to running pokemon_launcher_gui.py with the chosen Python.")

a = Analysis(
    ["installer.py"],
    pathex=["overlay/v2"],
    binaries=[],
    datas=datas,
    # env_utils.py is imported at runtime by installer.py. Keep it a bundled
    # module rather than a data file: a .py loaded straight out of _MEIxxxx is
    # not analyzed, so its stdlib imports (__future__, ...) would be missing.
    hiddenimports=["env_utils"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="PokemonRLSetup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version_setup.txt",
    icon=os.path.join("overlay", "v2", "logo.ico"),
)
