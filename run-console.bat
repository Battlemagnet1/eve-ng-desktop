@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 带控制台运行：能看到连接日志和报错，排查问题时用这个
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if not defined PY if exist "..\.venv\Scripts\python.exe" set "PY=%CD%\..\.venv\Scripts\python.exe"
if not defined PY set "PY=python"

"%PY%" app.py %*
echo.
echo [已退出] 按任意键关闭窗口
pause >nul
