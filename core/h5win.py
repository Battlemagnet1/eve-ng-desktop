# -*- coding: utf-8 -*-
"""图形控制台（HTML5 / Guacamole）独立窗口。

为什么要有这个模块
------------------
图形控制台原来只能塞在底部 dock 的 iframe 里，高度固定 300px，Linux 桌面
1024x768 挤进去只剩一条缝，既看不清也没法好好操作。

这里给它开一个**独立的原生窗口**：

* 尺寸按屏幕工作区的 92% 算（150% 缩放的笔记本也不会跑到屏幕外），
  可以自由缩放、最大化，F11 全屏；
* 窗口里直接加载 EVE 的 Guacamole 页面（不是 iframe），所以鼠标、键盘、
  滚轮、剪贴板都是浏览器原生行为；
* 额外注入一小段脚本，把「Windows 剪贴板 → 虚拟机剪贴板」做成
  一个按钮 + Ctrl+Alt+V，不用记 Guacamole 的 Ctrl+Alt+Shift 面板。

为什么要注入而不是直接靠浏览器剪贴板
------------------------------------
Guacamole 的「粘贴」有两套完全不同的机制，用错就白忙：

* **图形桌面**：把文本送到远端剪贴板（`clipboardService.setClipboard`），
  用户再在虚拟机里按 Ctrl+Shift+V 贴出来。
* **纯文本控制台（tty）**：根本没有剪贴板，Ctrl+Shift+V 永远没反应 ——
  只能把内容当成**按键**逐字打进去（`$broadcast('guacKeydown', keysym)`）。

所以工具条上两个按钮各有分工：「粘贴」走剪贴板，「键入」逐字打。
两条通道都经过真机验证（EVE 7.2 / Guacamole 1.5，见下面函数上的注释）。

WebView2 里 navigator.clipboard 读权限又常被拒，所以走本地服务读 Windows
剪贴板（core.clip），再交给页面里的脚本送进去。

自签证书不用管：WebView2 的 SPKI 白名单是进程级环境变量，主窗口和这个
窗口共用同一个 WebView2 环境（见 core/h5trust.py）。
"""
import os
import threading
import time
import urllib.parse

from . import clip

# 注入到 EVE 页面里的脚本（纯 ES5，兼容性优先；带 __eveH5 幂等标记）
_JS = r"""
(function () {
  if (window.__eveH5) { return; }
  window.__eveH5 = { v: 1 };

  function bridge() { return (window.pywebview && window.pywebview.api) || null; }

  /* ---------- 轻提示（不依赖页面自己的样式） ---------- */
  function toast(msg, bad) {
    var id = '__eve_h5_toast', t = document.getElementById(id);
    if (!t) {
      t = document.createElement('div');
      t.id = id;
      t.style.cssText = 'position:fixed;left:50%;bottom:76px;transform:translateX(-50%);'
        + 'z-index:2147483647;background:rgba(22,24,30,.94);color:#fff;'
        + 'font:13px/1.5 "Segoe UI",system-ui,sans-serif;padding:9px 15px;border-radius:8px;'
        + 'box-shadow:0 10px 28px rgba(0,0,0,.45);opacity:0;transition:opacity .18s;'
        + 'pointer-events:none;max-width:72vw;text-align:center;white-space:pre-line';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.background = bad ? 'rgba(150,38,38,.96)' : 'rgba(22,24,30,.94)';
    t.style.opacity = '1';
    clearTimeout(window.__eveH5.tt);
    window.__eveH5.tt = setTimeout(function () { t.style.opacity = '0'; }, bad ? 4200 : 2600);
  }

  /* ---------- 悬浮工具条 ---------- */
  function bar() {
    var b = document.getElementById('__eve_h5_bar');
    if (b) { return b; }
    b = document.createElement('div');
    b.id = '__eve_h5_bar';
    /* 常驻可见：以前默认 0.35 透明度、移开鼠标就淡回去，浅色画面上基本看不见，
       用户以为「工具条没了」。现在常态 0.95，鼠标移开也只降到 0.75。 */
    b.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483646;display:flex;'
      + 'gap:6px;align-items:center;background:rgba(18,20,26,.95);padding:5px 6px;'
      + 'border-radius:9px;border:1px solid rgba(255,255,255,.28);opacity:.95;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,.55);'
      + 'transition:opacity .15s;backdrop-filter:blur(3px);'
      + 'font:12px/1 "Segoe UI",system-ui,sans-serif;user-select:none';
    b.onmouseenter = function () { b.style.opacity = '1'; };
    b.onmouseleave = function () { b.style.opacity = '.75'; };

    function btn(text, tip, fn) {
      var x = document.createElement('button');
      x.textContent = text;
      x.title = tip;
      x.style.cssText = 'background:rgba(255,255,255,.14);color:#f2f4f8;border:0;'
        + 'border-radius:6px;padding:6px 10px;font:12px/1 "Segoe UI",system-ui,sans-serif;'
        + 'cursor:pointer';
      x.onmouseenter = function () { x.style.background = 'rgba(255,255,255,.28)'; };
      x.onmouseleave = function () { x.style.background = 'rgba(255,255,255,.14)'; };
      x.onclick = function (e) { e.preventDefault(); fn(); };
      b.appendChild(x);
      return x;
    }

    btn('粘贴', '把 Windows 剪贴板送进虚拟机剪贴板（Ctrl+Alt+V）\n'
      + '图形桌面里再用 Ctrl+Shift+V 贴出来；纯文本控制台没有剪贴板，请用「键入」', doPaste);
    btn('键入', '逐字打进去（Ctrl+Alt+T）——纯文本控制台没有剪贴板，只能这样\n'
      + '只支持英文/数字/符号，中文打不进去', doType);
    btn('全屏', '切换全屏（F11）', fullscreen);
    btn('×', '隐藏这个工具条（下次打开窗口还在）', function () { b.remove(); });
    document.body.appendChild(b);
    return b;
  }

  /* ---------- 和 Guacamole 会话打交道 ----------
     下面这两条通道都是**真机验证过**的（Guacamole 1.5 / EVE 7.2）：

     * 剪贴板：clipboardService.setClipboard(ClipboardData)
       它内部会写本地剪贴板，然后 $broadcast('guacClipboard')，
       guacClient 指令收到后调 ManagedClient.setClipboard 建真正的剪贴板流。
       全程不碰 UI —— 不需要打开左侧菜单，也不需要自己找 client 对象。

     * 打字：$rootScope.$broadcast('guacKeydown'/'guacKeyup', keysym)
       Guacamole 自己的键盘链路（Guacamole.Keyboard → 广播 → guacClient →
       client.sendKeyEvent）就是这个。注意两条前提：
         1) 目标会话的 clientProperties.focused 必须是 true；
         2) 必须走广播，直接调 client.sendKeyEvent 实测**没有任何效果**
            （2026-10-31 真机对照：广播能打进去，直发打不进去）。

     为什么打字这么重要：这台 linux 节点是纯文本控制台（rocky login:），
     tty 根本没有剪贴板，Ctrl+Shift+V 永远不会有反应，只能逐字打。          */

  function injector() {
    try { return (window.angular && window.angular.element(document.body).injector()) || null; }
    catch (e) { return null; }
  }

  function isConn(mc) {
    try {
      var s = mc.clientState.connectionState;
      return s === 3 || String(s).toUpperCase() === 'CONNECTED';
    } catch (e) { return false; }
  }

  /* 遍历 scope 树收集所有会话对象。
     不写死键名（focusedClient / client / clientGroup.clients 各版本都不一样），
     只认特征：有 clientState + client.sendKeyEvent 的对象就是 ManagedClient。 */
  function allClients() {
    var inj = injector(), out = [], q, root, n = 0;
    if (!inj) { return out; }
    try { root = inj.get('$rootScope'); } catch (e) { return out; }
    if (!root) { return out; }
    q = [root];
    while (q.length && n++ < 3000) {
      var sc = q.shift();
      try {
        for (var k in sc) {
          if (k.charAt(0) === '$') { continue; }
          var v = sc[k];
          if (v && v.clientState && v.client && typeof v.client.sendKeyEvent === 'function'
              && out.indexOf(v) < 0) { out.push(v); }
        }
      } catch (e) { /* 个别 scope 取属性会抛，跳过 */ }
      var ch = sc.$$childHead;
      while (ch) { q.push(ch); ch = ch.$$nextSibling; }
    }
    return out;
  }

  function pickClient() {
    var all = allClients(), i, c;
    for (i = 0; i < all.length; i++) {          // 先找「已聚焦且已连接」的
      c = all[i];
      if (isConn(c) && c.clientProperties && c.clientProperties.focused) { return c; }
    }
    for (i = 0; i < all.length; i++) { if (isConn(all[i])) { return all[i]; } }
    return all.length ? all[0] : null;
  }

  /* 送剪贴板：官方 clipboardService 通道，不碰 UI */
  function sendClipboard(text) {
    var inj = injector(), CD, CS;
    if (!inj) { return false; }
    try {
      CD = inj.get('ClipboardData');
      CS = inj.get('clipboardService');
    } catch (e) { return false; }
    if (!CD || !CS || typeof CS.setClipboard !== 'function') { return false; }
    try {
      CS.setClipboard(new CD({ type: 'text/plain', data: text }));
      return true;
    } catch (e) { return false; }
  }

  /* 逐字打进虚拟机（tty 唯一可行的办法）。
     只支持 ASCII：keysym 就是字符码；中文没有对应 keysym，只能走剪贴板。
     换行用 0xFF0D（Return），制表符 0xFF09。                              */
  function sendKeys(text, done) {
    done = done || function () {};
    var inj = injector(), root = null, mc;
    if (!inj) { done(false, '页面还没加载完'); return; }
    try { root = inj.get('$rootScope'); } catch (e) { root = null; }
    if (!root) { done(false, '找不到控制台会话'); return; }
    mc = pickClient();
    if (!mc) { done(false, '控制台还没连上，等画面出来再试'); return; }
    if (mc.clientProperties) { mc.clientProperties.focused = true; }

    var kb = { pressed: {} };     // 部分监听器会读第 2 个参数的 pressed
    var i = 0;
    (function step() {
      if (i >= text.length) { done(true); return; }
      var ch = text.charAt(i++), ks;
      if (ch === '\n') { ks = 0xFF0D; }
      else if (ch === '\t') { ks = 0xFF09; }
      else {
        var c = ch.charCodeAt(0);
        if (c < 32 || c > 126) {
          done(false, '第 ' + i + ' 个字符「' + ch + '」不是英文/数字/符号，'
            + '纯文本控制台只能逐字打 ASCII');
          return;
        }
        ks = c;
      }
      try {
        root.$broadcast('guacKeydown', ks, kb);
        root.$broadcast('guacKeyup', ks, kb);
      } catch (e) { done(false, '发送按键失败：' + e); return; }
      setTimeout(step, 12);
    })();
  }

  /* 统一换行：Windows 剪贴板是 \r\n，原样送进终端会变成 ^M 加
     `-bash: $'\r': command not found`，一律折成 \n。 */
  function norm(s) {
    return String(s || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  }

  /* ---------- 动作 ---------- */
  function doPaste() {
    var api = bridge();
    if (!api || !api.paste) { toast('本地服务还没就绪，稍等一秒再试', true); return; }
    api.paste().then(function (r) {
      var text = norm(r && r.text);
      if (!text) { toast('Windows 剪贴板里没有文本'); return; }
      if (sendClipboard(text)) {
        toast('已送进虚拟机剪贴板（' + text.length + ' 字符）\n'
          + '图形桌面里按 Ctrl+Shift+V 粘贴；纯文本控制台没有剪贴板，请改用「键入」');
      } else {
        toast('这台控制台没有剪贴板通道\n请改用「键入」（Ctrl+Alt+T）', true);
      }
    }).catch(function (e) { toast('读取 Windows 剪贴板失败：' + e, true); });
  }

  function doType() {
    var api = bridge();
    if (!api || !api.paste) { toast('本地服务还没就绪，稍等一秒再试', true); return; }
    api.paste().then(function (r) {
      var text = norm(r && r.text);
      if (!text) { toast('Windows 剪贴板里没有文本'); return; }
      if (text.length > 4000) {
        toast('内容太长（' + text.length + ' 字符），逐字键入要很久，请用「粘贴」', true);
        return;
      }
      toast('正在键入…（' + text.length + ' 字符）');
      sendKeys(text, function (ok, why) {
        if (ok) { toast('已键入 ' + text.length + ' 字符'); }
        else { toast('键入失败：' + why, true); }
      });
    }).catch(function (e) { toast('读取 Windows 剪贴板失败：' + e, true); });
  }

  function fullscreen() {
    var api = bridge();
    if (api && api.fullscreen) { api.fullscreen().catch(function () {}); }
  }

  /* ---------- 快捷键（捕获阶段，先于 Guacamole 自己的键盘处理） ---------- */
  document.addEventListener('keydown', function (ev) {
    if (ev.defaultPrevented) { return; }
    var k = (ev.key || '').toLowerCase();
    if (ev.ctrlKey && ev.altKey && !ev.shiftKey && k === 'v') {
      ev.preventDefault(); ev.stopPropagation(); doPaste(); return;
    }
    if (ev.ctrlKey && ev.altKey && !ev.shiftKey && k === 't') {
      ev.preventDefault(); ev.stopPropagation(); doType(); return;
    }
    if (k === 'f11') { ev.preventDefault(); ev.stopPropagation(); fullscreen(); }
  }, true);

  /* ---------- 启动 ---------- */
  window.__eveH5.paste = doPaste;      // 排错/自动化用得到
  window.__eveH5.type = doType;
  window.__eveH5.clip = sendClipboard;
  window.__eveH5.keys = sendKeys;
  window.__eveH5.client = pickClient;
  window.__eveH5.bar = bar;
  window.__eveH5.fullscreen = fullscreen;
  function boot() { try { bar(); } catch (e) {} }
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(boot, 60);
  } else {
    document.addEventListener('DOMContentLoaded', boot);
  }
  // SPA 切页可能把 body 换掉，隔一会儿补一次（bar() 幂等）
  setInterval(function () {
    if (!document.getElementById('__eve_h5_bar')) { try { bar(); } catch (e) {} }
  }, 4000);
})();
"""


class _Bridge:
    """暴露给页面脚本的极小 API（window.pywebview.api.*）。

    **所有属性都必须以下划线开头**：pywebview 生成桥的时候会用 `dir()` 把
    api 对象的公开属性递归遍历一遍（util.inject_pywebview），而 Window.native
    是 WinForms 的 COM 对象，能无限往下钻 —— 现象就是日志刷屏
    `win.native.AccessibilityObject.Bounds.Empty.Empty...` 直到递归上限，
    UI 线程被占死，页面停在白屏、`window.pywebview` 也永远不出现。
    """

    def __init__(self, win=None):
        self._win = win

    def paste(self):
        """读 Windows 剪贴板，交给注入脚本写进远端剪贴板。"""
        try:
            text = clip.get_text() or ""
        except Exception:
            text = ""
        return {"text": text, "len": len(text)}

    def copy(self, text=""):
        try:
            return bool(clip.set_text(text or ""))
        except Exception:
            return False

    def fullscreen(self):
        try:
            self._win.toggle_fullscreen()
        except Exception:
            return False
        return True

    def close(self):
        try:
            self._win.destroy()
        except Exception:
            pass
        return True


_hooks_done = False


def install_webview_hooks():
    """让 WebView2 直接放行剪贴板权限，别弹「想查看剪贴板」的授权框。

    Guacamole 连上之后会调 `navigator.clipboard.readText()` 做一次
    「本地剪贴板 → 远端」同步，WebView2 默认会弹一个系统授权条：
    不点它页面就不往下走，看着就像「控制台打不开」。

    pywebview 6 没有处理 CoreWebView2.PermissionRequested，这里在它
    `on_webview_ready`（CoreWebView2 初始化完成）之后补一个处理函数，
    只对剪贴板放行，其它权限保持默认（该弹还是弹）。
    """
    global _hooks_done
    if _hooks_done:
        return True
    try:
        import webview.platforms.edgechromium as ec
    except Exception:
        return False
    orig = ec.EdgeChrome.on_webview_ready

    def patched(self, sender, args):
        orig(self, sender, args)
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState

            def on_perm(_s, ev):
                try:
                    if "clipboard" in str(ev.PermissionKind).lower():
                        ev.State = CoreWebView2PermissionState.Allow
                except Exception:
                    pass

            sender.CoreWebView2.PermissionRequested += on_perm
        except Exception as e:
            print("[eve-desktop] 剪贴板权限放行没挂上：%s" % e, flush=True)

    ec.EdgeChrome.on_webview_ready = patched
    _hooks_done = True
    return True


def dpi_scale():
    """主屏缩放倍数（96 DPI = 1.0）。取不到就当 1.0。

    `SystemParametersInfoW(SPI_GETWORKAREA)` 在 **DPI 感知进程**里返回物理像素，
    而 pywebview 的 `create_window(width=…)` 用的是**逻辑像素（DIP）**。两者在
    150% 的笔记本上差 1.5 倍 —— 照物理像素算出来的窗口比屏幕还大，
    右下角（「粘贴 / 键入」工具条）就被推到屏幕外面，用户只会说“工具条没了”。
    实测（2560x1440 @150%）：请求 1725x947 DIP 的窗口落在 (418,188)-(2143,1135)，
    右边缘超出屏幕 435、下边缘超出 205。
    """
    try:
        import ctypes

        u = ctypes.windll.user32
        d = 0
        try:
            d = int(u.GetDpiForSystem())        # Win10 1607+
        except Exception:
            d = 0
        if not d:
            dc = u.GetDC(0)
            try:
                d = int(ctypes.windll.gdi32.GetDeviceCaps(dc, 88))   # LOGPIXELSX
            finally:
                u.ReleaseDC(0, dc)
        return (d / 96.0) if d and d > 0 else 1.0
    except Exception:
        return 1.0


def workarea():
    """主屏工作区（去掉任务栏）的宽高，**逻辑像素（DIP）** —— 给 pywebview 用。

    见 `dpi_scale()` 的注释：不折算 DPI，窗口就会比屏幕大。
    """
    try:
        import ctypes
        from ctypes import wintypes

        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            s = dpi_scale()
            w, h = int((r.right - r.left) / s), int((r.bottom - r.top) / s)
            if w > 640 and h > 480:
                return w, h
    except Exception:
        pass
    return 1366, 768


def _ascii_title(s):
    """窗口标题只留 ASCII。

    中文标题在某些 Windows 代码页下会变成乱码（主窗口当初就踩过，
    注释见 app.py），节点名基本都是 Linux/R1 这种，够用。
    """
    out = "".join(c if 32 <= ord(c) < 127 else "?" for c in (s or "")).strip()
    return out or "EVE Console"


class ConsoleWindows:
    """管理弹出式图形控制台窗口（同一 URL 只开一个）。"""

    def __init__(self, cfg=None, log=None):
        self.cfg = cfg
        self.log = log or (lambda *a, **k: None)
        self._wins = {}
        self._lock = threading.Lock()

    # ---------- 尺寸 ----------
    def _size(self):
        aw, ah = workarea()                       # 逻辑像素，见 workarea() 注释
        # 上限按 96% 工作区算：贴满屏幕时窗口边框会压到边缘，右下角就悬了
        lim_w, lim_h = max(760, int(aw * 0.96)), max(480, int(ah * 0.96))
        w, h = int(aw * 0.92), int(ah * 0.92)
        saved = {}
        try:
            saved = (self.cfg.data.get("h5win") or {}) if self.cfg else {}
        except Exception:
            saved = {}
        try:
            sw, sh = int(saved.get("w") or 0), int(saved.get("h") or 0)
        except Exception:
            sw = sh = 0
        if sw > 640 and sh > 400:
            w, h = min(sw, lim_w), min(sh, lim_h)
        w = max(760, min(w, lim_w))
        h = max(480, min(h, lim_h))
        x = max(0, (aw - w) // 2)
        y = max(0, (ah - h) // 2)
        return w, h, x, y

    def _remember(self, win):
        """把用户拖出来的大小记进配置，下次照这个开。"""
        try:
            w, h = int(win.width or 0), int(win.height or 0)
            if w > 640 and h > 400 and self.cfg:
                self.cfg.data["h5win"] = {"w": w, "h": h}
                self.cfg.save()
        except Exception:
            pass

    # ---------- 打开 ----------
    def open(self, url, title=""):
        """在独立窗口里打开图形控制台。

        返回 {"mode": "window" | "browser", ...}；调用方据此提示用户。
        """
        url = (url or "").strip()
        if not url:
            raise ValueError("缺少控制台地址")
        try:
            import webview
        except ImportError:
            return self._browser(url, "没装 pywebview，改用系统浏览器打开")
        install_webview_hooks()

        with self._lock:
            win = self._wins.get(url)
            alive = False
            if win is not None:
                try:
                    win.show()
                    win.on_top(True)
                    win.on_top(False)
                    alive = True
                except Exception:
                    alive = False
                if alive:
                    return {"mode": "window", "reused": True}

            w, h, x, y = self._size()
            bridge = _Bridge()
            try:
                win = webview.create_window(
                    _ascii_title(title),
                    url,
                    js_api=bridge,
                    width=w, height=h, x=x, y=y,
                    min_size=(760, 480),
                    text_select=True,
                    confirm_close=False,
                )
            except Exception as e:
                return self._browser(url, "开独立窗口失败（%s）" % e)
            if win is None:
                return self._browser(url, "开独立窗口失败")

            bridge._win = win
            self._wins[url] = win
            threading.Thread(target=self._inject_later, args=(win,), daemon=True).start()
            aw, ah = workarea()
            self.log("已在新窗口打开图形控制台：%s（%dx%d @ %d,%d，工作区 %dx%d，缩放 %.0f%%）"
                     % (title, w, h, x, y, aw, ah, dpi_scale() * 100))

        return {"mode": "window", "size": [w, h]}

    def _inject(self, win):
        try:
            win.evaluate_js(_JS)
            return True
        except Exception as e:
            self.log("控制台窗口注入脚本失败：%s" % e, "warn")
            return False

    def _inject_later(self, win):
        """在后台线程里注入，并且**一直看着**这个窗口。

        **不能**在 pywebview 的事件回调（loaded 等）里调 evaluate_js：
        那些回调跑在 WinForms 的 UI 线程上，而 evaluate_js 会 Invoke 回 UI
        线程再等一个信号量 —— 直接死锁，现象就是窗口一片黑、整个窗口点不动。
        所以这里用独立线程轮询着注入，脚本自身幂等（重复执行不会叠工具条）。

        以前注入成功一次就收工，结果是：控制台页面在注入之后又整页导航
        （登录跳转、换 token、EVE 前端换路由）时，注入的脚本跟着旧文档一起
        没了，工具条再也不会出现 —— 用户看到「窗口好好的，但粘贴 / 键入没了」。
        现在成功后每 6 秒复查一次，发现 `__eveH5` 不在就补注入。
        """
        ok = False
        for delay in (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0):
            time.sleep(delay)
            try:
                if win.evaluate_js("!!window.__eveH5"):
                    ok = True
                    break
                if self._inject(win) and win.evaluate_js("!!window.__eveH5"):
                    ok = True
                    break
            except Exception:
                continue          # 还在导航 / 窗口已关，下一轮再说
        self.log("控制台窗口脚本注入%s" % ("成功（粘贴 / 键入可用）" if ok
                                          else "失败：可先用 Ctrl+Alt+Shift 打开 Guacamole 自带的剪贴板面板"),
                 "info" if ok else "warn")
        try:
            self._remember(win)
            win.events.closed += lambda *a: self._forget(win)
        except Exception:
            pass

        # 看门狗：页面再导航一次就得补注入，否则工具条凭空消失
        deadline = time.time() + 3600
        while time.time() < deadline:
            time.sleep(6)
            try:
                if win not in self._wins.values():
                    return                       # 窗口关了
                if win.evaluate_js("!!window.__eveH5"):
                    continue
            except Exception:
                return                           # 窗口已销毁
            if self._inject(win):
                self.log("控制台页面重新导航过，已补注入工具条")

    def _forget(self, win):
        with self._lock:
            for k, v in list(self._wins.items()):
                if v is win:
                    self._wins.pop(k, None)

    def _browser(self, url, why):
        """开不出原生窗口就退回系统浏览器 —— 功能不缺，只是少了个窗口壳。"""
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception as e:
            raise ValueError(why + "，且无法调用系统浏览器：%s" % e)
        return {"mode": "browser", "why": why}

    # ---------- 证书提示 ----------
    @staticmethod
    def cert_state(url):
        """https 控制台的证书是否已经进了 WebView2 白名单。

        返回 (是否需要提醒, 说明)。白名单是进程启动时定死的，连上服务器后才
        发现的证书要重启客户端才生效 —— 与其让用户对着白屏猜，不如提前说。
        """
        try:
            u = urllib.parse.urlparse(url)
        except Exception:
            return False, ""
        if (u.scheme or "").lower() != "https" or not u.hostname:
            return False, ""
        try:
            from . import h5trust

            h = h5trust.spki_hash(u.hostname, u.port or 443)
            if h and h not in h5trust.current_hashes():
                return True, "这台服务器的自签证书本次未放行，控制台可能白屏；重启客户端后生效"
        except Exception:
            pass
        return False, ""
