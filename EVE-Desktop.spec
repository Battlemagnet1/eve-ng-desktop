# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建配置：EVE Desktop 单目录打包。

    .venv/Scripts/python.exe -m PyInstaller EVE-Desktop.spec --noconfirm

产物 dist/EVE-Desktop/（EVE-Desktop.exe + _internal/），
接着用 installer/EVE-Desktop.iss 打安装包（Output/EVE-Desktop-Setup-*.exe）。
"""
import os

ROOT = os.path.dirname(os.path.abspath(SPEC))   # evegui/

a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'web'), 'web'),      # 前端静态资源（core/server.py 按 __file__ 相对定位）
    ],
    hiddenimports=[
        # pywebview Windows 后端（WebView2 / WinForms）
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'webview.platforms.dotnet',
        'clr_loader',
        'clr_loader.netfx',
        'pythonnet',
        # paramiko 依赖链的冷门分支
        'paramiko.ed25519',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc_data', 'test', 'xmlrpc',
        # 没用到的后端（防误触发）
        'webview.platforms.cef', 'webview.platforms.gtk', 'webview.platforms.cocoa',
        'webview.platforms.qt',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EVE-Desktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                     # 桌面程序不开黑窗
    icon=os.path.join(ROOT, 'assets', 'eve-desktop.ico') if os.path.exists(
        os.path.join(ROOT, 'assets', 'eve-desktop.ico')) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='EVE-Desktop',
)
