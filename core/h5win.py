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
Guacamole 的粘贴是「先把文本传到远端剪贴板，再在虚拟机里按 Ctrl+V」。
最干净的做法是直接调它内部的 Clipboard 流：

    mc.client.createClipboardStream('text/plain') → Guacamole.StringWriter

这条路径不碰任何 UI。退回方案才是「打开侧边菜单 → 找到 textarea.clipboard
→ 写值 → 派发 input」（菜单是 ng-if 渲染的，不打开 DOM 里根本没有那个框），
而那个面板打开后很碍事，所以用完必须关回去 —— 见 doPaste 里的注释。

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
    b.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483646;display:flex;'
      + 'gap:6px;align-items:center;background:rgba(22,24,30,.72);padding:5px 6px;'
      + 'border-radius:9px;border:1px solid rgba(255,255,255,.14);opacity:.35;'
      + 'transition:opacity .15s;backdrop-filter:blur(3px);'
      + 'font:12px/1 "Segoe UI",system-ui,sans-serif;user-select:none';
    b.onmouseenter = function () { b.style.opacity = '1'; };
    b.onmouseleave = function () { b.style.opacity = '.35'; };

    function btn(text, tip, fn) {
      var x = document.createElement('button');
      x.textContent = text;
      x.title = tip;
      x.style.cssText = 'background:rgba(255,255,255,.1);color:#e8eaee;border:0;'
        + 'border-radius:6px;padding:6px 10px;font:12px/1 "Segoe UI",system-ui,sans-serif;'
        + 'cursor:pointer';
      x.onmouseenter = function () { x.style.background = 'rgba(255,255,255,.22)'; };
      x.onmouseleave = function () { x.style.background = 'rgba(255,255,255,.1)'; };
      x.onclick = function (e) { e.preventDefault(); fn(); };
      b.appendChild(x);
      return x;
    }

    btn('粘贴', '把 Windows 剪贴板送进虚拟机剪贴板（Ctrl+Alt+V）', doPaste);
    btn('键入', '把剪贴板内容当成键盘逐字打进去（纯文本终端没有剪贴板时的备选）', doType);
    btn('全屏', '切换全屏（F11）', fullscreen);
    btn('×', '隐藏这个工具条（下次打开窗口还在）', function () { b.remove(); });
    document.body.appendChild(b);
    return b;
  }

  /* ---------- Guacamole 剪贴板写入 ----------
     首选：拿到 Guacamole 的 ManagedClient，直接用 Clipboard 流把文本发给远端。
     这样**完全不用打开左侧菜单**（旧做法是强行 `menu.shown = true` 拿到
     textarea 再派发 input，用完那个面板还杵在屏幕左边，关也关不掉）。

     其次：真拿不到 client 时，才退回「打开菜单 → 写 textarea → 关菜单」的老路，
     并且记得把菜单关回去。                                                  */

  function injector() {
    try { return (window.angular && window.angular.element(document.body).injector()) || null; }
    catch (e) { return null; }
  }

  /* 找到 ManagedClient。
     Guacamole 1.5 的 clientController 把连接挂在 `$scope.clientGroup.clients[]`
     上（`clientGroup` 是 ManagedClientGroup），**没有** `$scope.client` 这个字段；
     老版本才是 `$scope.client`。两种都要认，否则直发永远拿不到 client、
     每次都退回到「开菜单」那条路 —— 面板关不掉就是这么来的。 */
  function clientOf(sc) {
    if (!sc) { return null; }
    try {
      var g = sc.clientGroup;
      if (g && g.clients && g.clients.length) {
        // 多标签时挑当前聚焦的那个
        for (var i = 0; i < g.clients.length; i++) {
          var c = g.clients[i];
          if (c && c.clientProperties && c.clientProperties.focused) { return c; }
        }
        return g.clients[0];
      }
      if (sc.client && sc.client.client) { return sc.client; }
    } catch (e) { /* 忽略 */ }
    return null;
  }

  function findClient() {
    if (!window.angular) { return null; }
    // 视图内部的元素会从 clientController 的作用域继承 clientGroup
    var sels = ['.client-view', '.client-body', 'guac-tiled-clients',
                '#guac-menu', 'body'];
    for (var s = 0; s < sels.length; s++) {
      var els;
      try { els = document.querySelectorAll(sels[s]); } catch (e) { continue; }
      for (var i = 0; i < els.length && i < 64; i++) {
        var hit = null;
        try { hit = clientOf(window.angular.element(els[i]).scope()); } catch (e) {}
        if (hit) { return hit; }
      }
    }
    // 兜底：菜单作用域（几乎所有版本都能从这里上溯到同一个控制器）
    var sc = menuScope();
    for (var up = 0; sc && up < 12; up++) {
      var c = clientOf(sc);
      if (c) { return c; }
      sc = sc.$parent;
    }
    return null;
  }

  /* 直接建 clipboard 流写文本。
     用「临时包一层 createClipboardStream 看有没有真被调用」来判定成功，
     而不是靠不抛异常 —— 否则明明没发出去也会报成功。 */
  function sendDirect(text) {
    var mc = findClient();
    if (!mc || !mc.client) { return false; }
    var C = mc.client;
    if (typeof C.createClipboardStream !== 'function') { return false; }
    if (!window.Guacamole || !window.Guacamole.StringWriter) { return false; }
    var orig = C.createClipboardStream, used = 0;
    try {
      C.createClipboardStream = function () { used++; return orig.apply(C, arguments); };
      var stream = C.createClipboardStream('text/plain');
      var writer = new window.Guacamole.StringWriter(stream);
      writer.sendText(text);
      writer.sendEnd();
    } catch (e) {
      return false;
    } finally {
      C.createClipboardStream = orig;
    }
    return used === 1;
  }

  /* ---------- 兜底：菜单 + textarea ---------- */

  function menuScope() {
    try {
      var el = document.getElementById('guac-menu');
      if (!el || !window.angular) { return null; }
      var sc = window.angular.element(el).scope();
      return sc && sc.menu ? sc : null;
    } catch (e) { return null; }
  }

  function menuShown() {
    var sc = menuScope();
    return !!(sc && sc.menu && sc.menu.shown);
  }

  function setMenu(shown) {
    var sc = menuScope();
    if (!sc) { return false; }
    try {
      sc.$apply(function () { sc.menu.shown = shown; });
      return true;
    } catch (e) { return false; }
  }

  function writeClipboard(text, cb) {
    var tries = 0;
    (function loop() {
      var ta = document.querySelector('textarea.clipboard') || document.querySelector('.clipboard');
      if (ta) {
        try {
          ta.disabled = false;
          ta.value = text;
          ta.dispatchEvent(new Event('input', { bubbles: true }));
          ta.dispatchEvent(new Event('change', { bubbles: true }));
          cb(true);
          return;
        } catch (e) { /* 继续重试 */ }
      }
      if (++tries > 25) { cb(false); return; }
      setTimeout(loop, 120);
    })();
  }

  /* ---------- 动作 ---------- */

  /* 逐字「打」进虚拟机：有些环境（纯文本 tty、没跑剪贴板管理器的桌面）压根
     没有远端的「粘贴」，Ctrl+Shift+V 只会没反应。这时候只能按键盘事件发。
     只支持 ASCII —— 中文等字符没有对应的 keysym，只能走剪贴板。 */
  function typeInto(text, done) {
    var mc = findClient();
    if (!mc || !mc.client || typeof mc.client.sendKeyEvent !== 'function') { done(false); return; }
    var C = mc.client;
    for (var k = 0; k < text.length; k++) {
      var code = text.charCodeAt(k);
      if (code !== 9 && code !== 10 && (code < 32 || code > 126)) { done(false); return; }
    }
    var i = 0;
    (function step() {
      if (i >= text.length) { done(true); return; }
      var ch = text.charAt(i++);
      var ks = ch === '\n' ? 0xFF0D : ch === '\t' ? 0xFF09 : ch.charCodeAt(0);
      try {
        C.sendKeyEvent(true, ks);
        C.sendKeyEvent(false, ks);
      } catch (e) { done(false); return; }
      setTimeout(step, 10);
    })();
  }

  function doType() {
    var api = bridge();
    if (!api || !api.paste) { toast('本地服务还没就绪，稍等一秒再试', true); return; }
    api.paste().then(function (r) {
      var text = ((r && r.text) || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      if (!text) { toast('Windows 剪贴板里没有文本'); return; }
      if (text.length > 4000) { toast('内容太长（' + text.length + ' 字符），键入太慢，请用「粘贴」', true); return; }
      typeInto(text, function (ok) {
        if (ok) { toast('已逐字键入（' + text.length + ' 字符）'); }
        else { toast('键入失败：内容里有中文/特殊字符，或者没找到控制台连接', true); }
      });
    }).catch(function (e) { toast('读取 Windows 剪贴板失败：' + e, true); });
  }

  function doPaste() {
    var api = bridge();
    if (!api || !api.paste) {
      toast('本地服务还没就绪，稍等一秒再试', true);
      return;
    }
    api.paste().then(function (r) {
      var text = (r && r.text) || '';
      if (!text) { toast('Windows 剪贴板里没有文本'); return; }

      /* Windows 剪贴板里的换行是 \r\n。原样送进 Linux 终端，\r 会被当成
         「回车」立刻执行，现象就是满屏 ^M 加 `-bash: $'\r': command not found`。
         统一成 \n：终端里是正常换行，图形界面里也能接受。 */
      var norm = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

      // 首选：直连 clipboard 流，不碰左侧菜单
      if (sendDirect(norm)) {
        toast('已送进虚拟机剪贴板（' + norm.length + ' 字符）\n在虚拟机里按 Ctrl+Shift+V / Shift+Insert 粘贴');
        return;
      }

      // 兜底：只能借菜单里的输入框，用完立刻关回去
      var wasOpen = menuShown();
      setMenu(true);
      writeClipboard(norm, function (ok) {
        if (!wasOpen) { setTimeout(function () { setMenu(false); }, 260); }
        if (ok) {
          toast('已送进虚拟机剪贴板（备用通道，' + norm.length + ' 字符）\n在虚拟机里按 Ctrl+Shift+V / Shift+Insert 粘贴');
        } else {
          toast('没找到 Guacamole 的剪贴板通道。\n按 Ctrl+Alt+Shift 打开侧边菜单，手动粘到「剪贴板」框里', true);
        }
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
    if (k === 'f11') { ev.preventDefault(); ev.stopPropagation(); fullscreen(); }
  }, true);

  /* ---------- 启动 ---------- */
  window.__eveH5.paste = doPaste;      // 排错/自动化用得到
  window.__eveH5.type = doType;
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


def workarea():
    """屏幕工作区（去掉任务栏）的宽高；取不到就给个保守值。"""
    try:
        import ctypes
        from ctypes import wintypes

        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            w, h = r.right - r.left, r.bottom - r.top
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
        aw, ah = workarea()
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
            w, h = min(sw, aw), min(sh, ah)
        w = max(760, min(w, aw))
        h = max(480, min(h, ah))
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
            self.log("已在新窗口打开图形控制台：%s" % title)

        return {"mode": "window", "size": [w, h]}

    def _inject(self, win):
        try:
            win.evaluate_js(_JS)
            return True
        except Exception as e:
            self.log("控制台窗口注入脚本失败：%s" % e, "warn")
            return False

    def _inject_later(self, win):
        """在后台线程里注入。

        **不能**在 pywebview 的事件回调（loaded 等）里调 evaluate_js：
        那些回调跑在 WinForms 的 UI 线程上，而 evaluate_js 会 Invoke 回 UI
        线程再等一个信号量 —— 直接死锁，现象就是窗口一片黑、整个窗口点不动。
        所以这里用独立线程轮询着注入，脚本自身幂等（重复执行不会叠工具条）。
        """
        ok = False
        for delay in (1.2, 2.5, 4.0, 7.0, 10.0):
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
        self.log("控制台窗口脚本注入%s" % ("成功（粘贴按钮可用）" if ok
                                          else "失败：用 Ctrl+Alt+Shift 打开 Guacamole 剪贴板面板"),
                 "info" if ok else "warn")
        try:
            self._remember(win)
            win.events.closed += lambda *a: self._forget(win)
        except Exception:
            pass

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
