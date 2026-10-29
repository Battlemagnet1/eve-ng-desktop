# -*- coding: utf-8 -*-
"""本地服务：给 pywebview 窗口提供 UI 与全部后端能力。

只监听 127.0.0.1，端口随机；前端所有请求同源，无需鉴权。
"""
import json
import mimetypes
import os
import posixpath
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import clip, images, netcfg
from .config import Config
from .eve import EveError, EveSession
from .remote import Remote, RemoteError
from .tasks import TaskManager
from .term import TermManager, TermSession
from .images import safe_name

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def _req(body, key, label=None):
    """取必填参数；缺了报 400 而不是 KeyError→500"""
    v = (body or {}).get(key)
    if v in (None, ""):
        raise EveError("缺少参数：%s" % (label or key))
    return v


class App:
    def __init__(self, config=None):
        self.cfg = config or Config()
        self.eve = None
        self.remote = None
        self.info = {}
        self.terms = TermManager()
        self.tasks = TaskManager()
        self.logs = []
        self._lock = threading.RLock()

    # ---------- 日志 ----------
    def log(self, msg, level="info"):
        self.logs.append({"t": time.time(), "level": level, "msg": str(msg)})
        if len(self.logs) > 400:
            del self.logs[:100]

    # ---------- 连接 ----------
    def connect(self, pid=None, overrides=None):
        p = self.cfg.profile_secrets(pid)
        for k, v in (overrides or {}).items():
            if v not in (None, ""):
                p[k] = v
        host = (p.get("host") or "").strip()
        if not host:
            raise EveError("请填写服务器地址")
        if not (p.get("eve_pass") or "").strip():
            raise EveError("还没保存 EVE 密码：请在「连接设置」里填写账号密码后重试")
        self.disconnect()
        sess = EveSession(host, p.get("api_port") or 80, p.get("scheme") or "http")
        sess.login(p.get("eve_user") or "admin", p.get("eve_pass") or "")
        info = {
            "host": host, "port": int(p.get("api_port") or 80), "scheme": p.get("scheme"),
            "version": sess.version, "caps": sess.caps,
            "user": p.get("eve_user"), "profile": p.get("id"),
            "ssh": False, "ssh_error": "", "ident": {}, "tools": {},
        }
        if p.get("use_ssh", True):
            try:
                r = Remote(host, p.get("ssh_port") or 22, p.get("ssh_user") or "root",
                           p.get("ssh_pass") or "")
                r.connect()
                cap = r.probe_capabilities()
                info.update({"ssh": True, "ident": cap["ident"], "tools": cap["tools"]})
                self.remote = r
            except RemoteError as e:
                info["ssh_error"] = str(e)
                self.remote = None
        self.eve = sess
        self.info = info
        self.log("已连接 %s（EVE %s%s）" % (host, sess.version,
                                           " · SSH 可用" if info["ssh"] else " · 仅 API"))
        return info

    def disconnect(self):
        self.terms.close_all()
        if self.remote:
            self.remote.close()
        self.remote = None
        self.eve = None
        self.info = {}

    def require(self, ssh=False):
        if self.eve is None:
            raise EveError("尚未连接服务器")
        if ssh and self.remote is None:
            raise RemoteError("该操作需要 SSH 连接（请在连接设置里填写 SSH 账号）")
        return self.eve

    # ---------- 实验 ----------
    def labs(self, path=""):
        eve = self.require()
        data = eve.list_labs(path)
        base = (path or "").strip("/")
        for f in data["folders"]:
            name = f.get("name") or f.get("path") or ""
            f["name"] = name
            f["path"] = posixpath.join(base, name) if base else name
        for l in data["labs"]:
            p = l.get("path") or l.get("file") or ""
            l["path"] = p.lstrip("/")
            l["name"] = posixpath.basename(l["path"])
            if l["name"].lower().endswith(".unl"):
                l["name"] = l["name"][:-4]
        data["cwd"] = base
        return data

    def lab_detail(self, path):
        eve = self.require()
        meta = eve.lab_meta(path) or {}
        nodes = eve.lab_nodes(path) or {}
        nets = eve.lab_networks(path) or {}
        links = eve.lab_topology(path) or []
        run = sum(1 for n in nodes.values() if _status(n) >= 2)
        return {"meta": meta, "nodes": nodes, "networks": nets, "links": links,
                "path": path, "running": run, "total": len(nodes)}

    def node_detail(self, path, nid):
        eve = self.require()
        if nid in (None, ""):
            raise EveError("缺少节点 id")
        node = eve.node(path, nid) or {}
        if isinstance(node, dict):
            # EVE 的节点详情里没有 id 字段，前端按钮要发回 id，这里补上
            node.setdefault("id", str(nid))
            node.setdefault("name", "node-%s" % nid)
        tpl = node.get("template") or node.get("type") or ""
        opts = {}
        try:
            opts = (eve.template_options(tpl) or {}).get("options") or {}
        except EveError:
            opts = {}
        ifaces = {}
        try:
            ifaces = (eve.node_interfaces(path, nid) or {})
        except EveError:
            pass
        url = node.get("url") or ""
        if url and not url.startswith("http"):
            url = eve.base() + url
        return {"node": node, "options": opts, "interfaces": ifaces, "html5": url,
                "console_port": eve.console_port(nid)}

    # ---------- 终端 ----------
    def _tpl_arch(self):
        return (self.info.get("ident") or {}).get("tpl_arch") or "intel"

    def open_terminal(self, path=None, nid=None, kind="node", cols=120, rows=32):
        eve = self.require()
        if kind == "host":
            remote = self.remote
            if remote is None:
                raise RemoteError("需要 SSH 连接才能打开主机终端")
            ch = remote.shell(cols, rows)
            sess = TermSession("EVE Shell · %s" % self.info.get("host", ""), "host", ch,
                               resize_fn=lambda c, r: ch.resize_pty(width=c, height=r),
                               telnet=False, closers=[ch.close])
            sess.sid = self.terms.add(sess)
            self.log("打开主机终端 %s" % sess.sid)
            return {"sid": sess.sid, "title": sess.title}

        port = eve.console_port(nid)
        title = "node-%s" % nid
        try:
            node = eve.node(path, nid) or {}
            title = node.get("name") or title
        except EveError:
            pass
        # 节点没跑起来时控制台端口是关的。直接给出能照做的提示，
        # 别把 paramiko 的 ChannelException 原样甩给用户。
        try:
            peers = eve.lab_nodes(path) or {}
            st = int((peers.get(str(nid)) or {}).get("status") or 0)
        except Exception:
            st = None
        if st is not None and st < 2:
            raise EveError("节点 %s 还没启动（控制台端口 %s 未监听），先在右侧面板点「启动」"
                           % (title, port))
        try:
            if self.remote is not None:
                ch = self.remote.channel(port, timeout=12)
            else:
                from .remote import open_tcp
                ch = open_tcp(self.info.get("host"), port, timeout=10)
            closers = [ch.close]
        except Exception as e:
            raise EveError("连不上控制台端口 %s（%s）。节点刚启动时要等几秒；"
                           "若一直失败，用主机终端执行 `ss -lntp | grep %s` 看端口" % (port, e, port))
        sess = TermSession("%s · %s" % (title, path), "node", ch, telnet=True, closers=closers)
        sess.sid = self.terms.add(sess)
        self.log("打开控制台 %s → %s:%s" % (sess.sid, self.info.get("host"), port))
        return {"sid": sess.sid, "title": sess.title, "port": port}


def _status(node):
    """EVE 的节点状态：0=停止 1=停止/就绪 2=运行 3=运行且锁定。
    只有 >=2 才算运行中（早期版本误判成 ==1，导致 running 恒为 0）。"""
    try:
        return int(node.get("status") or 0)
    except (TypeError, ValueError):
        return 0


class Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"
    server_version = "EveDesktop/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": str(msg)}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def _query(self):
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, self._query()
        try:
            if path.startswith("/api/"):
                return self._api_get(path, q)
            return self._static(path)
        except (EveError, RemoteError, ValueError) as e:
            return self._err(getattr(e, "message", str(e)), 400)
        except Exception as e:
            traceback.print_exc()
            return self._err("%s: %s" % (type(e).__name__, e), 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        q = self._query()
        try:
            if path == "/api/upload":
                return self._upload(q)
            body = self._body()
            return self._api_post(path, body)
        except (EveError, RemoteError, ValueError) as e:
            return self._err(getattr(e, "message", str(e)), 400)
        except Exception as e:
            traceback.print_exc()
            return self._err("%s: %s" % (type(e).__name__, e), 500)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            return self._api_put(path, self._body())
        except (EveError, RemoteError, ValueError) as e:
            return self._err(getattr(e, "message", str(e)), 400)
        except Exception as e:
            traceback.print_exc()
            return self._err("%s: %s" % (type(e).__name__, e), 500)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        q = self._query()
        try:
            if path == "/api/profile":
                self.app.cfg.delete_profile(q.get("id"))
                return self._json({"ok": True})
            return self._err("未知接口", 404)
        except Exception as e:
            return self._err(str(e), 500)

    # ---------- GET ----------
    def _api_get(self, path, q):
        a = self.app
        if path == "/api/state":
            return self._json({"ok": True, "info": a.info,
                               "profiles": a.cfg.profiles(),
                               "active": a.cfg.data.get("active"),
                               "logs": a.logs[-80:]})
        if path == "/api/labs":
            return self._json({"ok": True, **a.labs(q.get("path", ""))})
        if path == "/api/lab":
            return self._json({"ok": True, **a.lab_detail(q.get("path", ""))})
        if path == "/api/node":
            return self._json({"ok": True, **a.node_detail(q.get("path"), q.get("id"))})
        if path == "/api/template":
            return self._json({"ok": True, "data": a.require().template_options(q.get("name"))})
        if path == "/api/term/stream":
            return self._term_stream(q)
        if path == "/api/term/list":
            return self._json({"ok": True, "items": a.terms.list()})
        if path == "/api/clip":
            return self._json({"ok": True, "text": clip.get_text()})
        if path == "/api/images":
            a.require(ssh=True)
            return self._json({"ok": True, **images.scan(a.remote, a._tpl_arch())})
        if path == "/api/iol":
            a.require(ssh=True)
            return self._json({"ok": True, **images.iol_status(a.remote)})
        if path == "/api/tasks":
            return self._json({"ok": True, "items": a.tasks.list()})
        if path == "/api/download":
            return self._download(q)
        if path == "/api/net":
            a.require(ssh=True)
            return self._json({"ok": True, **netcfg.bridge_for_cloud(a.remote)})
        if path == "/api/profiles":
            return self._json({"ok": True, "items": a.cfg.profiles(),
                               "active": a.cfg.data.get("active")})
        return self._err("未知接口 %s" % path, 404)

    # ---------- POST ----------
    def _api_post(self, path, body):
        a = self.app
        if path == "/api/connect":
            over = {k: body.get(k) for k in
                    ("host", "api_port", "scheme", "eve_user", "eve_pass",
                     "ssh_port", "ssh_user", "ssh_pass") if body.get(k)}
            if "use_ssh" in body:
                over["use_ssh"] = bool(body["use_ssh"])
            info = a.connect(body.get("profile") or None, over)
            return self._json({"ok": True, "info": info})
        if path == "/api/disconnect":
            a.disconnect()
            return self._json({"ok": True})
        if path == "/api/node/action":
            a.require()
            a.eve.node_action(_req(body, "path", "实验路径"),
                              _req(body, "id", "节点 id"),
                              _req(body, "action", "动作"))
            return self._json({"ok": True})
        if path == "/api/lab/action":
            a.require()
            action = _req(body, "action", "动作")
            lab_path = _req(body, "path", "实验路径")
            cleaned = {}
            if action == "start" and a.remote is not None:
                # EVE 停节点不回收 vunl* tap，残留会让启动在同名 tap 上失败，
                # 启动前先清一次（只清本实验已停止节点的 tap）
                try:
                    cleaned = netcfg.lab_tap_cleanup(
                        a.remote, a.eve.caps.get("tenant") or 0,
                        a.eve.lab_nodes(lab_path))
                    if cleaned.get("removed"):
                        a.log("清理残留 tap：%s" % ", ".join(cleaned["removed"]))
                except Exception as e:
                    a.log("清理残留 tap 失败（忽略）：%s" % e, "error")
                res = a.eve.start_lab(lab_path)
                bad = [r for r in res if not r.get("ok")]
                for r in bad:
                    a.log("节点 %s 启动失败：%s" % (r["id"], r.get("error")), "error")
                a.log("启动实验：%d 个成功 / %d 个失败" % (len(res) - len(bad), len(bad)))
                # 请求本身成功，是否有节点失败放在 failed 里，交给前端汇总提示
                return self._json({"ok": True, "nodes": res, "cleaned": cleaned,
                                   "failed": [r["id"] for r in bad],
                                   "error": ("；".join("节点 %s：%s" % (r["id"], r.get("error"))
                                                       for r in bad) or None)})
            a.eve.lab_action(lab_path, action)
            return self._json({"ok": True, "cleaned": cleaned})
        if path == "/api/lab/fixnet":
            a.require(ssh=True)
            return self._json({"ok": True, **netcfg.lab_tap_cleanup(
                a.remote, a.eve.caps.get("tenant") or 0,
                a.eve.lab_nodes(_req(body, "path", "实验路径")))})
        if path == "/api/term/open":
            kind = body.get("kind") or "node"
            if kind == "node":
                _req(body, "id", "节点 id")
            return self._json({"ok": True, **a.open_terminal(
                body.get("path"), body.get("id"), kind,
                int(body.get("cols") or 120), int(body.get("rows") or 32))})
        if path == "/api/term/input":
            s = a.terms.get(body.get("sid"))
            if not s:
                return self._err("会话不存在", 404)
            s.write(body.get("data") or "")
            return self._json({"ok": True})
        if path == "/api/term/paste":
            # 右键粘贴：直接读系统剪贴板再写进会话，避免 WebView2 里
            # navigator.clipboard.readText() 的权限问题
            s = a.terms.get(body.get("sid"))
            if not s:
                return self._err("会话不存在", 404)
            text = body.get("text")
            if text is None:
                text = clip.get_text()
            text = (text or "").replace("\r\n", "\n").replace("\n", "\r")
            if text:
                s.write(text)
            return self._json({"ok": True, "len": len(text)})
        if path == "/api/clip":
            return self._json({"ok": clip.set_text(body.get("text") or ""),
                               "len": len(body.get("text") or "")})
        if path == "/api/term/resize":
            s = a.terms.get(body.get("sid"))
            if s:
                s.resize(body.get("cols") or 120, body.get("rows") or 32)
            return self._json({"ok": True})
        if path == "/api/term/close":
            a.terms.close(body.get("sid"))
            return self._json({"ok": True})
        if path == "/api/images/import":
            a.require(ssh=True)
            t = a.tasks.new("import", "导入 %s" % body.get("name", ""))
            t.update(status="queued", stage="等待上传")
            return self._json({"ok": True, "tid": t.id})
        if path == "/api/images/export":
            a.require(ssh=True)
            return self._export(body)
        if path == "/api/iol/fix":
            a.require(ssh=True)
            return self._json({"ok": True, **images.iol_fix(a.remote)})
        if path == "/api/net/nat":
            a.require(ssh=True)
            return self._json({"ok": True, **netcfg.nat_apply(
                a.remote, int(body.get("pnet") or 1), body.get("upstream") or None,
                bool(body.get("persist", True)))})
        if path == "/api/net/nat/remove":
            a.require(ssh=True)
            return self._json({"ok": True, **netcfg.nat_remove(
                a.remote, int(body.get("pnet") or 1), body.get("upstream") or None)})
        if path == "/api/profile":
            pid = a.cfg.upsert_profile(body)
            return self._json({"ok": True, "id": pid, "items": a.cfg.profiles()})
        if path == "/api/profile/active":
            a.cfg.set_active(body.get("id"))
            return self._json({"ok": True})
        return self._err("未知接口 %s" % path, 404)

    # ---------- PUT ----------
    def _api_put(self, path, body):
        a = self.app
        if path == "/api/node":
            a.require()
            a.eve.edit_node(_req(body, "path", "实验路径"), _req(body, "id", "节点 id"),
                            body.get("props") or {})
            return self._json({"ok": True})
        if path == "/api/node/ifaces":
            a.require()
            a.eve.set_interfaces(_req(body, "path", "实验路径"), _req(body, "id", "节点 id"),
                                  body.get("ifaces") or [])
            return self._json({"ok": True})
        if path == "/api/lab/network":
            a.require()
            a.eve.add_network(_req(body, "path", "实验路径"), body.get("net") or {})
            return self._json({"ok": True})
        return self._err("未知接口 %s" % path, 404)

    # ---------- 终端 SSE ----------
    def _term_stream(self, q):
        a = self.app
        sess = a.terms.get(q.get("sid"))
        if not sess:
            return self._err("会话不存在或已关闭", 404)
        try:
            offset = int(q.get("o") or 0)
        except ValueError:
            offset = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for item in sess.chunks(offset):
                if item.get("beat"):
                    self.wfile.write(b": beat\n\n")
                else:
                    payload = json.dumps(item).encode()
                    self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
                if item.get("e"):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---------- 上传 ----------
    def _upload(self, q):
        a = self.app
        tid = q.get("tid")
        name = safe_name(q.get("name") or "image.tgz")
        t = a.tasks.get(tid) if tid else None
        total = int(self.headers.get("Content-Length") or 0)
        tmpdir = os.path.join(tempfile.gettempdir(), "eve-desktop")
        os.makedirs(tmpdir, exist_ok=True)
        local = os.path.join(tmpdir, "%s-%s" % (tid or int(time.time()), name))
        sent, t0 = 0, time.time()
        with open(local, "wb") as f:
            remain = total
            while remain > 0:
                chunk = self.rfile.read(min(262144, remain))
                if not chunk:
                    break
                f.write(chunk)
                sent += len(chunk)
                remain -= len(chunk)
                if t:
                    t.progress(sent, total, sent / max(time.time() - t0, 0.001))
                    t.update(stage="上传中")
        if t:
            t.update(stage="上传完成，开始解包")
            threading.Thread(target=_run_import, args=(a, t, local), daemon=True).start()
        return self._json({"ok": True, "size": sent})

    # ---------- 导出 / 下载 ----------
    def _export(self, body):
        a = self.app
        im = body.get("image") or {}
        name = im.get("name") or ""
        path = im.get("path") or ""
        if not path:
            return self._err("缺少镜像路径")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = a.cfg.ui().get("export_dir") or os.path.join(
            os.path.expanduser("~"), "Desktop")
        local = os.path.join(out_dir, "%s-%s.tgz" % (safe_name(name), stamp))

        def worker(t):
            images.export_image(a.remote, path, name, bool(im.get("is_dir", True)),
                                local, t, a._tpl_arch())

        t = a.tasks.create("export", "导出 %s" % name, worker)
        return self._json({"ok": True, "tid": t.id, "file": local})

    def _download(self, q):
        t = self.app.tasks.get(q.get("tid"))
        if not t or not t.file or not os.path.isfile(t.file):
            return self._err("文件不存在", 404)
        size = os.path.getsize(t.file)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s"
                         % urllib.parse.quote(os.path.basename(t.file)))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(t.file, "rb") as f:
            while True:
                buf = f.read(262144)
                if not buf:
                    break
                self.wfile.write(buf)

    # ---------- 静态 ----------
    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        if ".." in rel:
            return self._err("非法路径", 400)
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            return self._err("未找到 %s" % rel, 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        if full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        if full.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 全是本地文件，缓存没收益，反而会让改完前端后窗口里还是旧脚本
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)


def _run_import(app, task, local):
    try:
        task.update(status="running", stage="解包中")
        result = images.import_image(app.remote, local, task, app._tpl_arch())
        # 这里是「先建任务、上传完再起线程」的路径，不走 TaskManager.create，
        # 所以必须自己收尾，否则任务永远停在 running
        if task.status == "running":
            names = ", ".join(result.get("names") or [])
            task.update(status="done", stage="完成", percent=100.0,
                        message="已导入 %s" % names)
        app.log("导入完成：%s" % ", ".join(result.get("names") or []))
    except Exception as e:
        task.update(status="failed", stage="失败", message=str(e))
        app.log("导入失败：%s" % e, "error")
    finally:
        try:
            os.remove(local)
        except OSError:
            pass


class LocalServer(ThreadingHTTPServer):
    """本地服务：窗口关掉时 SSE 长连接会被强行掐断，这类错误不必刷屏。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys as _sys

        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def serve(app, host="127.0.0.1", port=0):
    Handler.app = app
    httpd = LocalServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
