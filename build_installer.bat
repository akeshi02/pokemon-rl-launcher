@echo off
REM Builds only dist\PokemonRLSetup.exe (the setup wizard).
REM If dist\PokemonRLLauncher.exe exists it is bundled inside the wizard;
REM run build_all.bat instead to build both in the right order.
REM Run from the repo root (the folder that contains installer.py and overlay\).

setlocal
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail

pyinstaller --noconfirm --clean PokemonRLSetup.spec
if errorlevel 1 goto :fail

echo.
echo Built: dist\PokemonRLSetup.exe
exit /b 0

:fail
echo.
echo BUILD FAILED - see the error above.
exit /b 1
