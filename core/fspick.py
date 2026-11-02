# -*- coding: utf-8 -*-
"""本机目录选择 / 在资源管理器里定位文件。

导出实验要能自己决定存哪，所以需要一个真正的系统级目录选择框。这里直接借
pywebview 自带的那套（Windows 上是 IFileDialog，和「另存为」同一套原生控件），
不引新依赖、也不去开 tkinter（打包体积和外观都不划算）。

两个实测结论（tmp/dialog_probe.py，pywebview 6.2.1 + WebView2）：
- `webview.FOLDER_DIALOG` 在 6.x 已标记废弃，要用 `webview.FileDialog.FOLDER`，
  两套常量都做兜底；
- 在**非 GUI 线程**（本地 HTTP 服务线程）调用是安全的：对话框正常弹出、
  按 ESC 取消返回 None、不抛跨线程异常。所以路由里可以直接同步调用。

没有图形窗口时（`--no-gui` 或直接用浏览器开）没有 GUI 可用，这里明确抛
ValueError，让前端退回「导出到默认目录」并说明原因，而不是静默失败。
"""
import os
import subprocess
import sys


def _folder_const():
    """取「选目录」这个对话框类型的常量，兼容 pywebview 新旧版本。"""
    import webview
    fd = getattr(webview, "FileDialog", None)
    if fd is not None and hasattr(fd, "FOLDER"):
        return fd.FOLDER
    return webview.FOLDER_DIALOG


def pick_folder(start=""):
    """弹出原生目录选择框。选定返回绝对路径，取消返回 None。

    没有图形界面时抛 ValueError（前端会降级到默认导出目录）。
    """
    try:
        import webview
    except ImportError:
        raise ValueError("没有图形界面（未安装 pywebview），只能导出到默认目录")

    wins = list(getattr(webview, "windows", None) or [])
    if not wins:
        raise ValueError("当前没有图形窗口（--no-gui 模式），只能导出到默认目录")

    start = (start or "").strip()
    if not start or not os.path.isdir(start):
        start = _default_start()
    try:
        res = wins[0].create_file_dialog(_folder_const(), directory=start)
    except Exception as e:
        raise ValueError("打不开目录选择框：%s" % e)

    if not res:
        return None
    path = res[0] if isinstance(res, (list, tuple)) else res
    path = str(path or "").strip().strip('"')
    if not path:
        return None
    return os.path.abspath(path)


def _default_start():
    for d in (os.path.join(os.path.expanduser("~"), "Desktop"),
              os.path.join(os.path.expanduser("~"), "Documents"),
              os.path.expanduser("~")):
        if os.path.isdir(d):
            return d
    return ""


def reveal(path):
    """在资源管理器里打开所在文件夹（文件则选中它）。"""
    p = (path or "").strip().strip('"')
    if not p:
        raise ValueError("没有给出路径")
    p = os.path.abspath(p)
    if os.path.isdir(p):
        folder, target = p, None
    else:
        folder, target = os.path.dirname(p), p
    if not os.path.isdir(folder):
        raise ValueError("目录不存在：%s" % folder)

    if sys.platform.startswith("win"):
        # explorer 的怪癖：/select, 与路径必须是两个参数，且路径要加引号
        if target:
            subprocess.Popen(['explorer', '/select,', target])
        else:
            subprocess.Popen(['explorer', folder])
    elif sys.platform == "darwin":
        args = ['open', '-R', target] if target else ['open', folder]
        subprocess.Popen(args)
    else:
        subprocess.Popen(['xdg-open', folder])
    return {"folder": folder, "file": target}
