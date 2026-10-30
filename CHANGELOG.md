# Changelog

All notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [1.0.0] — 2026-10-30

First packaged release: a double-clickable Windows installer plus a windowed
launcher, both built with PyInstaller.

### Added
- **`PokemonRLSetup.exe`** — setup wizard (and `--cli` mode for silent installs):
  downloads the upstream project, asks for your own ROM and checks its SHA-1,
  lets you pick an existing conda/venv/system Python (or create a venv), installs
  the training packages into it, and installs the launcher app.
- **`PokemonRLLauncher.exe`** — windowed launcher: parameters, reward-weight
  editor, headless toggle, embedded emulator grid, live reward graph, checkpoint
  picker, environment picker, package installer.
- `launcher_config.json` as the single source of truth for the chosen
  environment (written by the wizard, updated by the launcher).
- `env_utils.py`: environment discovery (conda/venv/system, deduped), package
  status checks, pip installs, and `clean_env()` for spawning child processes
  from a frozen EXE.
- Training log per run (`runs/train_<timestamp>.log`) with a **Log** button, and
  `launcher_error.log` + a dialog for any launcher crash.
- `POKE_AUTOSTART=1` to start a configured run automatically (smoke tests).
- `build_all.bat`, `build_installer.bat`, hand-written PyInstaller specs, and a
  GitHub Actions workflow that builds and publishes both EXEs on a `v*` tag.
- `CREDITS.md` with full attribution to Peter Whidden's original project.

### Fixed
- Frozen setup EXE crashed on import (`ModuleNotFoundError: __future__`) because
  `env_utils.py` was loaded from the extracted data folder instead of being
  bundled; it now ships as a real module.
- Windowed EXE had `sys.stdout`/`sys.stderr = None`, which made `--cli --help`
  fail inside argparse and left only a modal traceback dialog.
- Frozen apps polluted child processes with `_MEIPASS` `PATH`/`PYTHONHOME`,
  breaking `conda.exe`/pip/Python; all child processes now use `clean_env()`.
- `launcher_config.json` written with a UTF-8 BOM was silently ignored.
- Training crashed for odd `num_cpu` (`einops` 2-row hardcode in upstream
  `tensorboard_callback.py`) — now patched.
- Resume `batch_size` is clamped to `n_steps * n_envs`; `save_freq` is at least 1.
- `revive_fossil` no longer shadows ~20 earlier quests in `quest_definitions.py`.
