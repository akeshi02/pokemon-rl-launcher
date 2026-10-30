# Launcher assets

Optional, and empty by default — the launcher works fine with nothing here
(`theme.asset_path()` returns `None` and callers fall back).

Drop files here and `tools/make_assets.py` will record them in
`manifest.json` (name, size, sha256) so a build can tell what shipped:

```
python tools/make_assets.py            # refresh manifest.json
python tools/make_assets.py --icons    # also render assets/icons/* from logo.png
python tools/make_assets.py --check    # verify without writing (CI-friendly)
```

Conventions:

- `fonts/` — optional `.ttf`/`.otf`. If present they are registered at startup
  (process-private) and used for the UI/monospace type; if absent, the
  customtkinter default (Roboto) is kept — which is the 1.0.0 look.
- `icons/` — generated from `logo.png`; not hand-edited.

Anything larger than 2 MB fails the pipeline: these files ride inside a
single-file PyInstaller EXE, so every megabyte is paid on every download.
