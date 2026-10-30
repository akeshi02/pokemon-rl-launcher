@echo off
REM Builds dist\PokemonRLSetup.exe (the setup wizard).
REM
REM The wizard embeds dist\PokemonRLLauncher.exe, so that file is built first
REM automatically whenever it is missing. PokemonRLLauncher.exe is only an
REM internal build artifact: it is never shipped on its own.
REM
REM Run from the repo root (the folder that contains installer.py and overlay\).

setlocal
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail

if not exist "dist\PokemonRLLauncher.exe" (
  echo.
  echo === dist\PokemonRLLauncher.exe not found - building it first ===
  pyinstaller --noconfirm --clean PokemonRLLauncher.spec
  if errorlevel 1 goto :fail
)

echo.
echo === Building dist\PokemonRLSetup.exe ===
pyinstaller --noconfirm --clean PokemonRLSetup.spec
if errorlevel 1 goto :fail

echo.
echo Built: dist\PokemonRLSetup.exe
exit /b 0

:fail
echo.
echo BUILD FAILED - see the error above.
exit /b 1
