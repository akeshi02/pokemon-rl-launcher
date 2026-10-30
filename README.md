# Pokémon RL Launcher

A GUI launcher and extra reward tooling for
[PWhiddy/PokemonRedExperiments](https://github.com/PWhiddy/PokemonRedExperiments),
the Pokémon Red reinforcement-learning project by **Peter Whidden** (MIT).
Set training parameters from a window instead of editing the training script.

**Credit where it belongs:** the emulator environment, training loop, reward
design and the original training script are Peter Whidden's work — this repo is
an unofficial add-on. See [`CREDITS.md`](CREDITS.md) for full attribution and
licenses, and please star/credit the
[original project](https://github.com/PWhiddy/PokemonRedExperiments).

This repo contains the add-on code plus **one patched upstream file**
(`overlay/v2/tensorboard_callback.py`, marked in the file and explained in
`CREDITS.md`). The upstream project itself is downloaded by `setup_overlay.py`
or by the installer — it is not redistributed here.

## Quick start (Windows, no Python needed)

1. Download **`PokemonRLSetup.exe`** from
   [Releases](../../releases/latest).
2. Run it: pick your own ROM, pick the conda/venv environment that should run
   training, install.
3. Double-click **Pokémon RL Launcher** (desktop shortcut, `Start Launcher.bat`,
   or `PokemonRedExperiments\v2\PokemonRLLauncher.exe`).

No console window, no `conda activate`, no Python on your PATH. Details and the
source install are below.

## What's added

| File (in `overlay/v2/`) | Purpose |
|---|---|
| `train_cli.py` | CLI training entry point (replaces editing `baseline_fast_v2.py`): `--num-cpu`, `--ep-length`, `--headless`, ... Saves a checkpoint on Ctrl+C. |
| `pokemon_launcher_gui.py` | Launcher GUI: parameters, reward-weight editor, headless on/off, embedded grid of emulator windows, live reward graph, environment picker. Windows only. |
| `env_utils.py` | Finds conda/venv/system Python environments and installs the training packages into the one you pick. Shared by the installer and the launcher. |
| `configurable_reward_env.py` | Subclass of `RedGymEnv` whose reward weights come from `reward_config.json` (hot-reloaded). |
| `quest_reward_env.py`, `quest_system/` | Story-quest waypoint shaping, optional no-reset mode with a stuck watchdog, curriculum snapshots. |
| `tensorboard_callback.py` | Patched copy of upstream's file: its explore-map image no longer hardcodes 2 rows, which crashed training whenever `num_cpu` was odd (e.g. 1). |
| `reward_config.json` | Default reward weights. |

## Install

You need Python 3.10–3.12, `ffmpeg` on your PATH, and **your own legally obtained
Pokémon Red ROM** (not provided, and I can't point you to one).

### Plain Python

```bash
git clone https://github.com/KanishkPatel0206/pokemon-rl-launcher.git
cd pokemon-rl-launcher
python setup_overlay.py            # downloads upstream, copies the overlay into its v2/ folder
cd PokemonRedExperiments/v2
pip install -r requirements.txt            # upstream's list; on Windows see its windows-setup-guide.md
pip install -r requirements-launcher.txt
```

Replace `YOUR-USERNAME` above with the account that hosts the repo (this project is
published at <https://github.com/KanishkPatel0206/pokemon-rl-launcher>), or use the
"Code → Download ZIP" button.

### Conda

```bash
conda create -n antigravity python=3.11 -y
conda activate antigravity
# then run the setup_overlay.py + pip steps above inside that environment
python pokemon_launcher_gui.py      # launcher now runs training with this env
```

The setup wizard and the launcher can both point at an existing conda environment,
so you never have to activate it by hand: the launcher starts training with that
environment's `python.exe` and an activated-like `PATH`.

Place the ROM at `PokemonRedExperiments/PokemonRed.gb` (SHA-1
`ea9bcae617fdf159b045185467ae58b2e4a48b9a`).

Already have the upstream zip? `python setup_overlay.py --zip master.zip`.
Want reproducible installs? Pin an upstream commit: `--ref <sha>`.

## Run

```bash
python pokemon_launcher_gui.py                  # GUI
python train_cli.py --headless --num-cpu 8      # terminal
```

Visible emulator windows cost CPU; use `--headless` (or the GUI toggle) for speed
and only show a few windows when you want to watch.

### Environment variables (optional)
`POKE_NO_RESET`, `POKE_STUCK_RESET_STEPS`, `POKE_CURRICULUM_SNAPSHOTS`,
`POKE_CURRICULUM_DIR`, `POKE_INIT_STATE_OVERRIDE` — see the docstring in
`quest_reward_env.py`.

## Windows installer (recommended)

`PokemonRLSetup.exe` is a setup wizard. It:

1. downloads the original project from GitHub,
2. adds the launcher files (downloaded from this repo, or the copy bundled in the installer),
3. asks you to pick **your own** Pokémon Red ROM, checks it, and copies it into the project as
   `PokemonRed.gb` (it renames it, or unzips it, for you),
4. asks **which Python environment should run the project** — an existing conda environment,
   a virtualenv, a system Python, or a fresh `venv` it creates — and installs the training
   packages into that environment (with a CPU-only PyTorch option),
5. installs `PokémonRLLauncher.exe` (the windowed launcher app) into
   `PokemonRedExperiments\v2\`, and writes `launcher_config.json` (which interpreter to use),
   `Start Launcher.bat`, `Install Packages.bat`, and an optional desktop shortcut.

The ROM is never downloaded or linked. The wizard warns if the file's checksum doesn't match
the expected revision.

After that, the only thing you double-click is **Pokémon RL Launcher** (the desktop shortcut,
`Start Launcher.bat`, or `PokemonRedExperiments\v2\PokemonRLLauncher.exe`). It reads
`launcher_config.json`, starts training with *that environment's* `python.exe`, and needs no
Python on your PATH and no console window.

The launcher never installs anything by itself; **Install Packages…** in the app (or the
wizard) does that. Training output goes to `runs\train_<date>.log` — open it with the
launcher's **Log** button; if something goes wrong inside the launcher, the traceback is
written to `launcher_error.log` next to the EXE and shown in a dialog.

Download `PokemonRLSetup.exe` from the Releases page. Windows SmartScreen may warn about an
unsigned app: choose "More info" then "Run anyway", or build it yourself (below).

### Build the installers yourself

Building needs Python 3.10–3.12 with `pyinstaller` (the `antigravity` conda env works).

```bat
:: OVERLAY_REPO in installer.py is already set to KanishkPatel0206/pokemon-rl-launcher;
:: change it if you forked the project. Then, from the repo root:
build_all.bat
```

`build_all.bat` builds the installer, and the launcher app it embeds along the way:

| Output | What it is |
|---|---|
| `dist\PokemonRLLauncher.exe` | the launcher app — onefile, windowed (no console). An **intermediate** file: the wizard embeds it and installs it into `PokemonRedExperiments\v2\`. It is never published on its own. |
| `dist\PokemonRLSetup.exe` | the setup wizard — bundles the launcher EXE and `overlay\`; **this is the only file that ships** |

Both are described by hand-written spec files (`PokemonRLLauncher.spec`,
`PokemonRLSetup.spec`); do not delete them, and rebuild the launcher *before* the wizard so
the wizard picks it up. Upload `PokemonRLSetup.exe` to a GitHub Release. If `OVERLAY_REPO` is
left unset, the installer simply uses the files bundled inside it.

`build_installer.bat` builds only the wizard; it builds `dist\PokemonRLLauncher.exe` first if
that file is missing, so the wizard is never produced without the GUI app inside it.

Silent/testing mode (works from the EXE too — pass `--log`, since a windowed EXE has no
console):

```bat
python installer.py --cli --dest D:\pokemon --rom "C:\path\rom.gb" --env antigravity
python installer.py --cli --dest D:\pokemon --python "C:\path\envs\x\python.exe" --cpu
python installer.py --cli --dest D:\pokemon --env antigravity --dry-run --log install.log
PokemonRLSetup.exe --cli --dest D:\pokemon --env antigravity --no-packages --log install.log
```

`POKE_AUTOSTART=1` makes the launcher start the run configured in `launcher_config.json` as
soon as its window opens (used for automated smoke tests).

### Publishing your own release

`.github/workflows/release.yml` builds the installer on `windows-latest` and attaches it to a
GitHub Release whenever you push a `v*` tag.

```bash
git remote add origin https://github.com/KanishkPatel0206/pokemon-rl-launcher.git   # your fork
git push -u origin main

git tag v1.0.0
git push origin v1.0.0        # builds + publishes the release
```

Checklist before tagging:

1. `OVERLAY_REPO` in `installer.py` must name **your** repo (it is pre-set to this
   project's), so the wizard can pull overlay updates from it; if it's wrong the wizard
   just falls back to the copy bundled inside the EXE.
2. Bump `APP_VERSION` in `installer.py`, `version_launcher.txt`, `version_setup.txt`, and
   add a `CHANGELOG.md` entry.
3. Locally run `build_all.bat`, then `dist\PokemonRLSetup.exe --cli --help` and a real
   install, before tagging.

Prefer doing it by hand? `gh release create v1.0.0 dist\PokemonRLSetup.exe --generate-notes`
after `build_all.bat` works too. Users only ever need `PokemonRLSetup.exe`.

### Using your own conda environment

Pick it on the wizard's environment page ("Use an existing conda / venv / system
environment") — environments are detected automatically and the wizard lists what's
missing before installing. The choice is saved to `launcher_config.json`.

Later, in the launcher: **Detect Environments…** switches environments, **Install
Packages…** installs or repairs the libraries in the selected one, and the status line
under the paths shows the environment and any missing packages. The launcher remembers
your settings between runs, and training is started with that environment's `python.exe`
plus an activated-like `PATH`, so `conda activate` is not required.

## Notes
- Public-map streaming (upstream's `StreamWrapper`) is **off** unless you pass `--stream`.
- No ROM, save states, or training output are included in this repo.

## Credits

- **Peter Whidden** — author of
  [PokemonRedExperiments](https://github.com/PWhiddy/PokemonRedExperiments): the
  emulator environment, training loop and reward design this launcher drives,
  and the original `tensorboard_callback.py` that `overlay/v2/` patches.
- PyBoy, stable-baselines3, PyTorch, Gym/Gymnasium, einops, TensorBoard,
  customtkinter, matplotlib, psutil, pywin32 and PyInstaller — licenses and
  links are listed in [`CREDITS.md`](CREDITS.md).

## License

MIT for this project's code (see [`LICENSE`](LICENSE)). The patched
`overlay/v2/tensorboard_callback.py` remains under upstream's MIT copyright
(© 2023 Peter Whidden), reproduced in [`CREDITS.md`](CREDITS.md).

Pokémon and Pokémon Red are trademarks of Nintendo / Creatures Inc. / GAME FREAK
inc. This project is unofficial and non-commercial, is not affiliated with or
endorsed by them, and ships **no ROM, save state or trained model**.
