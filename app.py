# -*- coding: utf-8 -*-
"""EVE Desktop 启动入口。

    python app.py            打开桌面窗口
    python app.py --no-gui   只起本地服务（调试/浏览器访问）
    python app.py --port 8765
"""
import argparse
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import server  # noqa: E402
from core.config import Config, app_dir  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="EVE-NG Desktop")
    ap.add_argument("--no-gui", action="store_true", help="不打开窗口，仅提供本地服务")
    ap.add_argument("--port", type=int, default=0, help="本地服务端口（默认随机）")
    ap.add_argument("--debug", action="store_true", help="打开 WebView 调试")
    ap.add_argument("--no-auto-connect", action="store_true", help="启动时不自动连接上次的服务器")
    args = ap.parse_args()

    cfg = Config()
    app = server.App(cfg)
    httpd = server.serve(app, "127.0.0.1", args.port)
    port = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("[eve-desktop] %s" % url, flush=True)

    # 记住上次的连接：profile 里存了密码就自动连，前端一打开就是有数据的
    if not args.no_auto_connect:
        p = cfg.profile_secrets()
        if (p.get("host") or "").strip() and (p.get("eve_pass") or "").strip():
            def _auto():
                try:
                    info = app.connect()
                    print("[eve-desktop] 已自动连接 %s（%s）"
                          % (info.get("host"), info.get("version") or "版本未知"), flush=True)
                except Exception as e:
                    print("[eve-desktop] 自动连接失败：%s"
                          % getattr(e, "message", e), flush=True)
            threading.Thread(target=_auto, daemon=True).start()

    if args.no_gui:
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
        return

    try:
        import webview
    except ImportError:
        print("未安装 pywebview，已退化为仅服务模式：%s" % url)
        print("安装：pip install pywebview")
        args.no_gui = True
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
        return

    webview.create_window(
        # 标题只用 ASCII：中文环境里非 ASCII 会被按 GBK 解释成乱码（· → ¡¤）
        "EVE Desktop - %s" % (cfg.profile().get("host") or ""),
        url, width=1480, height=920, min_size=(1080, 680),
        text_select=True, confirm_close=False,
    )
    try:
        # private_mode=False：用固定用户数据目录，窗口关掉时不会去删临时 profile
        # （否则每次退出都打印 "Failed to delete user data folder"）
        webview.start(debug=args.debug, private_mode=False,
                      storage_path=os.path.join(app_dir(), "webview"))
    finally:
        try:
            app.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
