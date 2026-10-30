@echo off
REM Builds the shipped EXE, dist\PokemonRLSetup.exe (the setup wizard).
REM It also builds dist\PokemonRLLauncher.exe first, because the wizard embeds it;
REM that one is an intermediate build artifact and is never shipped on its own.
REM
REM Run from the repo root, inside a Python 3.10-3.12 environment with pyinstaller.
REM Before releasing, set OVERLAY_REPO in installer.py to your GitHub repo.

setlocal
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail

echo.
echo === Building dist\PokemonRLLauncher.exe ===
pyinstaller --noconfirm --clean PokemonRLLauncher.spec
if errorlevel 1 goto :fail

echo.
echo === Building dist\PokemonRLSetup.exe ===
pyinstaller --noconfirm --clean PokemonRLSetup.spec
if errorlevel 1 goto :fail

echo.
echo Built:
echo   dist\PokemonRLLauncher.exe   (embedded in the wizard, not shipped)
echo   dist\PokemonRLSetup.exe      (ship this one)
echo.
echo Upload PokemonRLSetup.exe to a GitHub Release. Users run that one.
exit /b 0

:fail
echo.
echo BUILD FAILED - see the error above.
exit /b 1
