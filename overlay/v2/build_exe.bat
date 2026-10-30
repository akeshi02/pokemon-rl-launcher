@echo off
REM Builds PokemonRLLauncher.exe from pokemon_launcher_gui.py
REM Run from the v2 folder, inside the Python env that has the launcher requirements.
pip install pyinstaller
if exist logo.ico (set ICON=--icon logo.ico) else (set ICON=)
pyinstaller --noconfirm --clean --onefile --name PokemonRLLauncher %ICON% ^
  --collect-all customtkinter ^
  --hidden-import win32timezone ^
  --exclude-module torch --exclude-module stable_baselines3 --exclude-module pyboy ^
  --exclude-module tensorflow --exclude-module scipy --exclude-module pandas ^
  pokemon_launcher_gui.py
echo.
echo Built: dist\PokemonRLLauncher.exe
echo Copy it into your v2 folder (next to train_cli.py) and run it there.
