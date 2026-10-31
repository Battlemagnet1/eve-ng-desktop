@echo off
rem Build EVE Desktop installer. Requires: Inno Setup 6.3+, Python venv with pyinstaller.
rem Usage: installer\build-installer.bat   (run from any directory; it locates the venv itself)
rem
rem NOTE: this used to use the relative path .venv\Scripts\python.exe, which only
rem resolved when the CWD happened to be evegui with a venv inside it. Otherwise it
rem silently fell back to the system python and produced a bundle without paramiko
rem (ModuleNotFoundError at runtime). Now it resolves paths from the script location
rem and fails loudly instead of using the wrong interpreter.
rem
rem Keep this file ASCII-only: cmd.exe reads .bat in the OEM codepage, and UTF-8
rem comments/echo lines get mangled and break parsing.

setlocal
for %%i in ("%~dp0..") do set HERE=%%~fi
for %%i in ("%HERE%\..") do set ROOT=%%~fi

set PY=%ROOT%\.venv\Scripts\python.exe
if not exist "%PY%" set PY=%HERE%\.venv\Scripts\python.exe
if not exist "%PY%" goto :no_venv

echo [i] Python: %PY%
"%PY%" -c "import paramiko, webview" 2>nul
if errorlevel 1 goto :no_deps

cd /d "%HERE%"
"%PY%" -m pip install -q pyinstaller pillow
"%PY%" -m PyInstaller EVE-Desktop.spec --noconfirm
if errorlevel 1 exit /b 1

rem Smoke test the bundle before packaging: pure-python deps live inside the PYZ,
rem so checking for _internal\paramiko is meaningless (it never exists). Running
rem the exe in server-only mode is the only reliable check.
"%PY%" "%HERE%\installer\smoke_test.py" "%HERE%\dist\EVE-Desktop\EVE-Desktop.exe"
if errorlevel 1 goto :bad_bundle

"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "%HERE%\installer\EVE-Desktop.iss"
if errorlevel 1 exit /b 1

echo.
echo Installer ready: installer\Output\EVE-Desktop-Setup-*.exe
endlocal
exit /b 0

:no_venv
echo [x] venv not found: %ROOT%\.venv\Scripts\python.exe
echo     create it first: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
exit /b 1

:no_deps
echo [x] this interpreter lacks paramiko or pywebview
echo     install: %PY% -m pip install -r requirements.txt
exit /b 1

:bad_bundle
echo [x] paramiko missing under dist\EVE-Desktop\_internal - bundle would not start
exit /b 1
