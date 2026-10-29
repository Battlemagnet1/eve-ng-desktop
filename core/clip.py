# -*- coding: utf-8 -*-
"""Windows 剪贴板读写（纯 ctypes，不依赖第三方库、不起子进程）。

为什么要走本地服务而不是浏览器 API：
WebView2 里 `navigator.clipboard.readText()` 需要页面拿到 clipboard-read
权限，右键这种手势不一定放行；`document.execCommand('paste')` 早已被弃用。
直接读系统剪贴板最稳，也顺带能跟 SecureCRT / 记事本互传。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

_IS_WIN = sys.platform.startswith("win")

if _IS_WIN:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _user32.OpenClipboard.argtypes = [wt.HWND]
    _user32.OpenClipboard.restype = wt.BOOL
    _user32.CloseClipboard.restype = wt.BOOL
    _user32.EmptyClipboard.restype = wt.BOOL
    _user32.GetClipboardData.argtypes = [wt.UINT]
    _user32.GetClipboardData.restype = wt.HANDLE
    _user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    _user32.SetClipboardData.restype = wt.HANDLE
    _kernel32.GlobalLock.argtypes = [wt.HANDLE]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [wt.HANDLE]
    _kernel32.GlobalUnlock.restype = wt.BOOL
    _kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wt.HANDLE
    _kernel32.GlobalFree.argtypes = [wt.HANDLE]


def _open(retries=8):
    """剪贴板可能被别的程序短暂占用，重试几次再放弃。"""
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_text():
    """读剪贴板文本；没有文本 / 读不到都返回空串。"""
    if not _IS_WIN:
        return ""
    if not _open():
        return ""
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = _kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p) or ""
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def set_text(text):
    """写剪贴板文本；成功返回 True。"""
    if not _IS_WIN:
        return False
    if text is None:
        text = ""
    buf = ctypes.create_unicode_buffer(str(text))
    size = ctypes.sizeof(buf)
    if not _open():
        return False
    h = None
    try:
        _user32.EmptyClipboard()
        h = _kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, size)
        if not h:
            return False
        p = _kernel32.GlobalLock(h)
        if not p:
            return False
        try:
            ctypes.memmove(p, buf, size)
        finally:
            _kernel32.GlobalUnlock(h)
        # SetClipboardData 成功后所有权归系统，不能再 GlobalFree
        if not _user32.SetClipboardData(CF_UNICODETEXT, h):
            return False
        h = None
        return True
    finally:
        if h:
            _kernel32.GlobalFree(h)
        _user32.CloseClipboard()
