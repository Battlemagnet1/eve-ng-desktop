@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 找 Python：本目录 .venv -> 上级 .venv -> 系统 PATH
set "PY="
if exist ".venv\Scripts\pythonw.exe" set "PY=%CD%\.venv\Scripts\pythonw.exe"
if not defined PY if exist "..\.venv\Scripts\pythonw.exe" set "PY=%CD%\..\.venv\Scripts\pythonw.exe"

if defined PY goto run
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
  set "PY=%%i"
  goto run
)

echo [x] 没找到 Python。请先装 Python 3.10+ 并勾选 "Add Python to PATH"
echo     装好后在本目录执行： pip install -r requirements.txt
pause
exit /b 1

:run
start "" "%PY%" app.py
