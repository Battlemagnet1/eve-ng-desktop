@echo off
rem Build EVE Desktop installer. Requires: Inno Setup 6.3+, Python venv with pyinstaller.
rem Usage: installer\build-installer.bat  (run from repo root)

set PY=.venv\Scripts\python.exe
if not exist %PY% set PY=python

%PY% -m pip install -q pyinstaller pillow
%PY% -m PyInstaller EVE-Desktop.spec --noconfirm
if errorlevel 1 exit /b 1

"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\EVE-Desktop.iss
if errorlevel 1 exit /b 1

echo.
echo Installer ready: installer\Output\EVE-Desktop-Setup-*.exe
