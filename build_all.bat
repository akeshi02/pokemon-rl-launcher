@echo off
REM Builds both shipped EXEs:
REM   dist\PokemonRLLauncher.exe  -- the launcher app (windowed, no console)
REM   dist\PokemonRLSetup.exe     -- the setup wizard (bundles the launcher app)
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
echo   dist\PokemonRLLauncher.exe
echo   dist\PokemonRLSetup.exe
echo.
echo Upload PokemonRLSetup.exe to a GitHub Release. Users run that one.
exit /b 0

:fail
echo.
echo BUILD FAILED - see the error above.
exit /b 1
