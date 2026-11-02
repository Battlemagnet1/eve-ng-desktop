# -*- coding: utf-8 -*-
"""本地服务：给 pywebview 窗口提供 UI 与全部后端能力。

只监听 127.0.0.1，端口随机；前端所有请求同源，无需鉴权。
"""
import json
import mimetypes
import os
import posixpath
import re
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import capture, clip, fspick, images, labpack, netcfg
from .config import Config
from .notes import Notes
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
        self.caps = capture.Manager()
        self.notes = Notes()        # 拓扑文本框（本机侧车文件，不进 .unl）
        self.logs = []
        self.debug = False          # --debug：把每个本地接口调用打到控制台，方便排错
        self.h5win = None           # 图形控制台独立窗口（只有图形版启动时才注入）
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
        port = int(p.get("api_port") or 80)
        scheme = (p.get("scheme") or "").strip().lower()
        if scheme not in ("http", "https"):
            # 老配置没有这个字段：443 就是 HTTPS（新版 EVE Pro 的默认）
            scheme = "https" if port in (443, 8443) else "http"
        # 协议填错是致命错误，但改起来很烦（尤其升级到 Pro 之后）。
        # 这里按「先按配置/端口猜，失败再换另一种」自动试探，成功了就把
        # 结果写回配置，下次直接用对的那个。
        sess, last = None, None
        for sc in [scheme] + [x for x in ("https", "http") if x != scheme]:
            cand = EveSession(host, port, sc)
            try:
                cand.login(p.get("eve_user") or "admin", p.get("eve_pass") or "")
            except EveError as e:
                last = e
                self.log("用 %s 连接 %s:%s 失败，换一种协议再试：%s"
                         % (sc.upper(), host, port, e), "warn")
                continue
            sess, scheme = cand, sc
            if sc != (p.get("scheme") or "").strip().lower():
                self._remember_scheme(p.get("id"), sc)
            break
        if sess is None:
            raise last or EveError("连接失败")
        info = {
            "host": host, "port": port, "scheme": scheme,
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
        if scheme == "https":
            # 图形控制台要 WebView2 放行这张证书，白名单只能在进程启动时设，
            # 所以这里把指纹存进 profile（下次启动直接用），并提示本次是否已生效。
            self._remember_cert(p.get("id"), host, port)
        self.log("已连接 %s（EVE %s%s）" % (host, sess.version,
                                           " · SSH 可用" if info["ssh"] else " · 仅 API"))
        return info

    def _remember_cert(self, pid, host, port):
        """记住服务器的证书指纹，供下次启动时给内嵌浏览器放行。"""
        try:
            from core import h5trust
            h = h5trust.spki_hash(host, port)
            if not h:
                return
            if h not in h5trust.current_hashes():
                self.log("图形控制台：这台服务器的自签证书本次未放行，重启客户端后生效"
                         "（CLI 控制台不受影响）", "warn")
            pid = pid or self.cfg.data.get("active")
            for q in self.cfg.data.get("profiles", []):
                if q.get("id") == pid and q.get("cert_spki") != h:
                    q["cert_spki"] = h
                    self.cfg.save()
                    break
        except Exception as e:
            self.log("保存证书指纹失败（不影响本次连接）：%s" % e, "warn")

    def _remember_scheme(self, pid, scheme):
        """把探到的协议写回配置，下次连接就不用再试错了。"""
        try:
            pid = pid or self.cfg.data.get("active")
            for q in self.cfg.data.get("profiles", []):
                if q.get("id") == pid:
                    if q.get("scheme") != scheme:
                        q["scheme"] = scheme
                        self.cfg.save()
                        self.log("已记住 %s 使用 %s" % (q.get("host") or "", scheme.upper()))
                    return
        except Exception as e:
            self.log("保存协议设置失败（不影响本次连接）：%s" % e, "warn")

    def disconnect(self):
        self.terms.close_all()
        try:
            self.caps.stop_all()          # 别把 tcpdump 留在服务器上跑
        except Exception:
            pass
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
    def create_lab(self, folder="", name="", description=""):
        """新建空实验。

        EVE 的 POST /api/labs 要求 `path`（所在目录）和 `name`（**不带 .unl**，
        带了自己会再补一个变成 x.unl.unl）。CE 6.x 与 Pro 7.x 语义一致。
        返回新实验的相对路径，前端据此直接打开。
        """
        eve = self.require()
        name = (name or "").strip()
        if not name:
            raise EveError("请填实验名")
        if name.lower().endswith(".unl"):
            name = name[:-4]
        if "/" in name or "\\" in name:
            raise EveError("实验名里不能带斜杠")
        folder = (folder or "").strip("/")
        eve.post("/api/labs", {
            "path": "/" + folder if folder else "/",
            "name": name,
            "version": "1",
            "author": "",
            "description": (description or "").strip(),
        })
        path = (folder + "/" + name + ".unl").lstrip("/")
        self.log("新建实验 %s" % path)
        return {"path": path, "name": name, "folder": folder}

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
                "path": path, "running": run, "total": len(nodes),
                "umtime": eve.lab_umtime(path)}

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

    # ---------- 拓扑编辑 ----------
    def _guard(self, path, umtime):
        """并发保护：写之前看一眼实验有没有被别人改过。

        EVE 网页版和本工具同时开同一个实验时，后写的会盖掉先写的。
        前端把加载时的 umtime 一起带上来，对不上就让用户先「重载」。
        """
        if umtime in (None, "", 0, "0"):
            return
        cur = self.eve.lab_umtime(path)
        if cur and str(cur) != str(umtime):
            raise EveError("实验已被别处修改（EVE 网页版或另一个窗口？），请先点「重载」再操作")

    def templates(self):
        """可新建的模板。描述带 `.missing` 表示宿主没装对应镜像，标成不可用。"""
        eve = self.require()
        raw = eve.templates() or {}
        items = []
        for name, desc in raw.items():
            desc = desc or ""
            missing = desc.endswith(".missing")
            items.append({"template": name,
                          "desc": (desc[:-8] if missing else desc).strip(" ."),
                          "ready": not missing})
        items.sort(key=lambda x: (not x["ready"], x["template"]))
        return {"items": items, "total": len(items),
                "ready": sum(1 for x in items if x["ready"])}

    def add_node(self, path, props, umtime=None):
        self.require()
        self._guard(path, umtime)
        data = self.eve.add_node(path, props)
        nid = (data or {}).get("id") if isinstance(data, dict) else data
        self.log("新建节点 %s（id=%s）" % (props.get("name") or props.get("template"), nid))
        return {"id": nid, "umtime": self.eve.lab_umtime(path)}

    def delete_node(self, path, nid, umtime=None):
        self.require()
        self._guard(path, umtime)
        self.eve.delete_node(path, nid)
        self.log("删除节点 %s" % nid)
        return {"ok": True, "umtime": self.eve.lab_umtime(path)}

    def delete_network(self, path, nid, umtime=None):
        self.require()
        self._guard(path, umtime)
        self.eve.delete_network(path, nid)
        self.log("删除网络 %s" % nid)
        return {"ok": True, "umtime": self.eve.lab_umtime(path)}

    def delete_lab(self, path):
        """删掉整个实验（拓扑 + 节点配置，删了就没了）。

        顺序是硬性的：先停掉还在跑的节点、等 EVE 把它们收干净，再删文件。
        Pro 7.2 在节点运行时直接删会回 400「Nodes still running (60069)」；
        CE 6.2 会回 200，但把进程和 /opt/unetlab/tmp/<tenant>/<uuid> 留下，
        后面再建实验起节点就抢端口、status 报 2 的假象就是从这来的。
        """
        eve = self.require()
        path = (path or "").strip("/")
        if not path:
            raise EveError("没指定实验路径")
        if ".." in path.split("/"):
            raise EveError("实验路径不合法：%s" % path)
        if not path.lower().endswith(".unl"):
            raise EveError("只能删实验文件（.unl），%s 不是" % path)

        try:
            nodes = eve.lab_nodes(path) or {}
        except EveError as e:
            if getattr(e, "code", None) in (404, 60000):
                raise EveError("这个实验已经不在了（可能被别的会话删掉），刷新一下列表")
            raise
        running = [nid for nid, nd in nodes.items() if _status(nd) >= 2]
        if running:
            self.log("删除实验前先停 %d 个运行中的节点" % len(running))
        wait = []                       # 停成功、需要等它真的退出的节点
        for nid in sorted(running, key=lambda x: int(x)):
            try:
                eve.node_action(path, nid, "stop")
                wait.append(nid)
            except EveError as e:
                # 报错一般是「本来就没在跑」；这种不用等，直接删
                self.log("停节点 %s 失败（跳过等待）：%s" % (nid, e), "warn")

        # stop 是异步的：进程没收完之前 delete 只会回 60069，先等一会儿。
        # 但 CE 的 getStatus 是 `netstat | grep :<端口>`，端口一旦被别的进程占着，
        # 没跑起来的节点也会永远报 2（假运行）。所以等不到不能直接放弃 ——
        # 照样往下删，让 EVE 自己裁决：真没停 Pro 会回 60069，那时再报错。
        left = list(wait)
        deadline = time.time() + 5
        while left and time.time() < deadline:
            time.sleep(0.6)
            still = []
            for nid in left:
                try:
                    if _status(eve.node(path, nid) or {}) >= 2:
                        still.append(nid)
                except EveError:
                    pass            # 查不到就不当它还在跑，交给 delete 的返回值说话
            left = still
        if left:
            self.log("节点 %s 仍报运行（CE 上常是端口被占的假象），直接尝试删除"
                     % "、".join(str(x) for x in left), "warn")

        try:
            eve.delete_lab(path)
        except EveError as e:
            if left:
                raise EveError("删不掉：还有 %d 个节点没停下来（%s）。先手动停机再删。"
                               "服务器原话：%s"
                               % (len(left), "、".join(str(x) for x in left), e))
            raise
        if eve.lab_exists(path):
            raise EveError("服务器回了成功，但实验文件还在（%s）—— 可能被别的会话锁着，"
                           "去网页里确认一下" % path)
        self.log("已删除实验 %s（%d 个节点）" % (path, len(nodes)))
        return {"ok": True, "path": path, "nodes": len(nodes), "stopped": len(running)}

    def lab_ifaces(self, path):
        """一次把整个实验所有节点的网卡拉回来，供拓扑上画接口点。

        逐个调 EVE 的 nodes/{id}/interfaces（它是单节点接口），节点一般
        只有十来个，串行可接受；一个节点查失败不影响其它节点。
        """
        eve = self.require()
        nodes = eve.lab_nodes(path) or {}
        out = {}
        for nid in sorted(nodes, key=lambda x: (len(str(x)), str(x))):
            try:
                data = eve.node_interfaces(path, nid) or {}
            except Exception:
                continue
            eth = data.get("ethernet") or {}
            items = []
            for iid, v in sorted(eth.items(),
                                 key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
                items.append({"id": str(iid),
                              "name": (v or {}).get("name") or ("e0/%s" % iid),
                              "network_id": (v or {}).get("network_id") or 0})
            out[str(nid)] = items
        return {"nodes": out}

    def move_node(self, path, nid, left, top, umtime=None):
        self.require()
        self._guard(path, umtime)
        self.eve.move_node(path, nid, left, top)
        return {"ok": True, "umtime": self.eve.lab_umtime(path)}

    def move_network(self, path, nid, left, top, umtime=None):
        """挪网络/云图标。和挪节点一样只写坐标，不碰接在它上面的网卡。"""
        self.require()
        self._guard(path, umtime)
        self.eve.move_network(path, nid, left, top)
        return {"ok": True, "umtime": self.eve.lab_umtime(path)}

    def link_nodes(self, path, a, b, net_id=None, name=None, left=None, top=None,
                   umtime=None):
        """把两个节点的网卡接到同一个网络上（界面上就是「一台拖到另一台」。）

        EVE 的拓扑模型里没有「节点→节点」的边：两点之间必须共用一个
        network（bridge/pnetX）。所以连线 = 建或选一个网络，再把两边的
        网卡都指过去；net_id 为空时就新建一个内部桥，放在两节点中点。
        """
        eve = self.require()
        self._guard(path, umtime)
        a = a or {}
        b = b or {}
        aid = _req(a, "id", "源节点 id")
        bid = _req(b, "id", "目标节点 id")
        aif = _req(a, "iface", "源接口")
        bif = _req(b, "iface", "目标接口")
        if str(aid) == str(bid):
            raise EveError("同一个节点上的两个网卡直接相连没有意义")

        created = False
        if net_id in (None, "", 0, "0"):
            before = set((eve.lab_networks(path) or {}).keys())
            # 注意：必须先用 visibility=1 落盘。EVE 的 save() 会丢弃
            # 「没挂接口且 visibility=0」的网络（__lab.php 序列化里
            # count==0 && visibility==0 → continue），vis=0 的新桥接口还没接
            # 就直接消失了，接口还没接就先没了 → 界面报「新建网络没成功」。
            eve.add_network(path, {
                "name": name or "Net", "type": "bridge",
                "left": int(left if left is not None else 400),
                "top": int(top if top is not None else 300),
                "visibility": 1, "icon": "lan.png"})
            after = eve.lab_networks(path) or {}
            new = [k for k in after if k not in before]
            if not new:
                raise EveError("新建网络没成功：EVE 没有返回新网络")
            net_id = max(new, key=lambda x: int(x) if str(x).isdigit() else 0)
            created = True

        eve.set_interfaces(path, aid, [{"name": aif, "network_id": int(net_id)}])
        eve.set_interfaces(path, bid, [{"name": bif, "network_id": int(net_id)}])
        if created:
            # 两边接口都接上了（count=2），现在改成 visibility=0 就能安全落盘，
            # EVE 网页版也会把它当直连、不画图标，与本界面一致
            try:
                eve.edit_network(path, net_id, {"visibility": 0})
            except EveError:
                pass    # 改不了也只是 EVE 网页版多个图标，不影响接线本身
        # 结果复核：EVE 对「接口名不认识」是静默忽略的，HTTP 照样 200。
        # 必须看网络上的接口数真的变成 2，否则界面上会出现「点了接线没线」。
        try:
            cnt = int(((eve.lab_networks(path) or {}).get(str(net_id)) or {}).get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt < 2:
            raise EveError("接线没生效：网络 %s 上只挂了 %d 个接口（应为 2）" % (net_id, cnt))
        self.log("接线 %s:%s ─ %s:%s（网络 %s%s）"
                 % (aid, aif, bid, bif, net_id, "，新建" if created else ""))
        return {"ok": True, "net": int(net_id), "created": created,
                "umtime": eve.lab_umtime(path)}

    # ---------- 抓包 ----------
    def _iface_of(self, path, nid, iface=None, ifid=None):
        """网卡名 → EVE 的接口编号（e0/0=0、e0/1=16…），也是 tap 名最后一截。"""
        data = self.eve.node_interfaces(path, nid) or {}
        eth = data.get("ethernet") or {}
        if ifid not in (None, ""):
            v = eth.get(str(ifid))
            if v is None:
                raise EveError("节点 %s 没有编号 %s 的网卡" % (nid, ifid))
            return int(ifid), (v or {}).get("name") or ("if%s" % ifid)
        for iid, v in eth.items():
            if str((v or {}).get("name")) == str(iface):
                return int(iid), iface
        names = ", ".join(str((v or {}).get("name")) for v in eth.values()) or "无"
        raise EveError("节点 %s 没有网卡 %s（可选：%s）" % (nid, iface, names))

    def capture_start(self, path, nid, iface=None, ifid=None, snaplen=0, bpf=""):
        eve = self.require(ssh=True)
        node = eve.node(path, nid) or {}
        name = node.get("name") or ("node-%s" % nid)
        if _status(node) < 2:
            raise EveError("节点 %s 还没运行，网卡上抓不到东西：先启动再抓包" % name)
        iid, nm = self._iface_of(path, nid, iface, ifid)
        tenant = (self.info.get("caps") or {}).get("tenant") or 0
        info = self.caps.start(self.remote, tenant, path, nid, name, iid, nm,
                               snaplen=snaplen, bpf=bpf, local_dir=self.capture_dir(),
                               lab_id=node.get("lab_id") or 1,
                               vpcs=(str(node.get("template") or node.get("type")
                                         or "").strip() == "vpcs"))
        self.log("开始抓包 %s %s（%s）" % (name, nm, info["ifname"]))
        return info

    def capture_dir(self):
        return (self.cfg.ui().get("capture_dir")
                or os.path.join(os.path.expanduser("~"), "Documents", "eve-captures"))

    # ---------- 拓扑文本框 ----------
    def _lab_host(self):
        """文本框按「哪台服务器 + 哪个实验」分开存，换机器不会串。"""
        return (self.info or {}).get("host") or ""

    def textboxes(self, path):
        if not (path or "").strip():
            return {"items": []}
        return {"items": self.notes.list(self._lab_host(), path)}

    def save_textboxes(self, path, items):
        path = (path or "").strip()
        if not path:
            raise EveError("缺少参数：实验路径")
        if not self._lab_host():
            raise EveError("尚未连接服务器")
        out = self.notes.set(self._lab_host(), path, items)
        return {"items": out}


    # ---------- 图形控制台窗口 ----------
    def open_console_window(self, url, title=""):
        """把 HTML5 图形控制台开成独立窗口（大、能缩放、能粘贴）。

        只允许打开「当前连接的这台服务器」的控制台：本地服务虽然只绑
        127.0.0.1，也没必要让它变成一个任意 URL 打开器。
        """
        url = (url or "").strip()
        if not url:
            raise EveError("该节点没有图形控制台地址")
        if not url.startswith("http"):
            eve = self.require()
            url = eve.base().rstrip("/") + "/" + url.lstrip("/")
        host = self.info.get("host")
        try:
            uh = urllib.parse.urlparse(url).hostname
        except Exception:
            uh = None
        if host and uh and uh != host:
            raise EveError("这个控制台地址不属于当前服务器（%s ≠ %s）" % (uh, host))
        if self.h5win is None:
            raise EveError("当前是「仅服务」模式：请在浏览器里打开，或直接启动图形版")
        warn = ""
        try:
            from . import h5win as _h5

            need, why = _h5.ConsoleWindows.cert_state(url)
            if need:
                warn = why
        except Exception:
            pass
        res = self.h5win.open(url, title or "EVE Console")
        res["warn"] = warn
        return res

    # ---------- 终端 ----------
    def _tpl_arch(self):
        return (self.info.get("ident") or {}).get("tpl_arch") or "intel"

    # Pro 7.x 起节点时给控制台分配**随机端口**（不走 32768+id），API 也不返回，
    # 只能从服务器上的 wrapper 进程参数里读：`qemu_wrapper -C <端口> -D <节点id>`，
    # 而它的工作目录正好是 `/opt/unetlab/tmp/<tenant>/<实验uuid>/<节点id>`，
    # 所以「实验 uuid + 节点 id」就能唯一定位到端口。
    CONSOLE_PORT_CMD = (
        "ps -eo pid,args | grep -E '(qemu|iol|dynamips|docker)_wrapper' | grep -v grep | "
        "while read -r pid rest; do cwd=$(readlink /proc/$pid/cwd 2>/dev/null) || continue; "
        "echo \"$cwd|$rest\"; done")

    def _console_port(self, eve, path, nid):
        """节点控制台端口：CE 用公式，Pro 去服务器上现查（查不到再退回公式）。"""
        fallback = eve.console_port(nid)
        if not (self.info.get("caps") or {}).get("pro") or self.remote is None:
            return fallback
        try:
            uuid = str((eve.lab_meta(path) or {}).get("id") or "")
            if not uuid:
                return fallback
            _rc, out, _err = self.remote.run(self.CONSOLE_PORT_CMD, timeout=20)
            tail = "/" + str(nid)
            for line in (out or "").splitlines():
                cwd, _sep, args = line.partition("|")
                if uuid not in cwd or not cwd.rstrip("/").endswith(tail):
                    continue
                m = re.search(r"-C\s+(\d+)", args)
                if m:
                    return int(m.group(1))
        except Exception as e:
            self.log("查 Pro 控制台端口失败（退回公式 %s）：%s" % (fallback, e), "warn")
        return fallback

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

        port = self._console_port(eve, path, nid)
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
        if self.app.debug and path.startswith("/api/"):
            print("[api] GET  %s %s" % (path, json.dumps(q, ensure_ascii=False)[:160]), flush=True)
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
        if self.app.debug and path != "/api/upload":
            print("[api] POST %s" % path, flush=True)
        try:
            if path == "/api/upload":
                return self._upload(q)
            if path == "/api/lab/import":
                return self._lab_import(q)      # 请求体是 zip，不能当 JSON 解析
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
            if path == "/api/lab/node":
                return self._json({"ok": True, **self.app.delete_node(
                    q.get("path"), q.get("id"), q.get("umtime"))})
            if path == "/api/lab/network":
                return self._json({"ok": True, **self.app.delete_network(
                    q.get("path"), q.get("id"), q.get("umtime"))})
            if path == "/api/lab":
                return self._json(self.app.delete_lab(q.get("path")))
            return self._err("未知接口", 404)
        except (EveError, RemoteError, ValueError) as e:
            return self._err(getattr(e, "message", str(e)), 400)
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
        if path == "/api/lab/ifaces":
            return self._json({"ok": True, **a.lab_ifaces(q.get("path", ""))})
        if path == "/api/template":
            return self._json({"ok": True, "data": a.require().template_options(q.get("name"))})
        if path == "/api/templates":
            return self._json({"ok": True, **a.templates()})
        if path == "/api/captures":
            return self._json({"ok": True, "items": a.caps.list(), "dir": a.capture_dir()})
        if path == "/api/wireshark":
            return self._json({"ok": True, **capture.wireshark_info()})
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
        if path == "/api/textboxes":
            return self._json({"ok": True, **a.textboxes(q.get("path", ""))})
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
        # ---------- 拓扑编辑 ----------
        if path == "/api/lab/new":
            return self._json({"ok": True, **a.create_lab(
                body.get("folder"), _req(body, "name", "实验名"),
                body.get("description"))})
        if path == "/api/lab/node":
            return self._json({"ok": True, **a.add_node(
                _req(body, "path", "实验路径"),
                body.get("props") or body.get("node") or {},
                body.get("umtime"))})
        if path == "/api/node/move":
            return self._json({"ok": True, **a.move_node(
                _req(body, "path", "实验路径"), _req(body, "id", "节点 id"),
                body.get("left"), body.get("top"), body.get("umtime"))})
        if path == "/api/net/move":
            return self._json({"ok": True, **a.move_network(
                _req(body, "path", "实验路径"), _req(body, "id", "网络 id"),
                body.get("left"), body.get("top"), body.get("umtime"))})
        if path == "/api/lab/link":
            return self._json({"ok": True, **a.link_nodes(
                _req(body, "path", "实验路径"), body.get("a"), body.get("b"),
                body.get("net_id"), body.get("name"),
                body.get("left"), body.get("top"), body.get("umtime"))})
        # ---------- 抓包 ----------
        if path == "/api/capture/start":
            return self._json({"ok": True, **a.capture_start(
                _req(body, "path", "实验路径"), _req(body, "id", "节点 id"),
                body.get("iface"), body.get("ifid"),
                int(body.get("snaplen") or 0), body.get("bpf") or "")})
        if path == "/api/capture/stop":
            return self._json({"ok": True, **a.caps.stop(_req(body, "sid", "抓包会话"))})
        if path == "/api/capture/save":
            return self._json({"ok": True, **a.caps.save(
                _req(body, "sid", "抓包会话"), a.capture_dir())})
        if path == "/api/capture/discard":
            return self._json({"ok": True, **a.caps.discard(_req(body, "sid", "抓包会话"))})
        if path == "/api/capture/open":
            return self._json({"ok": True, **capture.open_local(body.get("file") or "")})
        if path == "/api/wireshark/download":
            # 只开固定这一个网址，不接受前端传 URL —— 本地接口别变成通用「打开任意链接」
            return self._json({"ok": True, **capture.open_wireshark_download()})
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
        if path == "/api/console/window":
            return self._json({"ok": True, **a.open_console_window(
                _req(body, "url", "控制台地址"), body.get("title") or "")})
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
        if path == "/api/jslog":
            # 前端异常回传（WebView2 没有控制台，出错只能从这里看）
            msg = "%s @%s:%s" % (body.get("msg"), body.get("src"), body.get("line"))
            a.log("[js] " + msg, "error")
            print("[js]", msg, "\n", (body.get("stack") or "")[:1500], flush=True)
            return self._json({"ok": True})
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
        if path == "/api/lab/export":
            a.require(ssh=True)
            return self._lab_export(body)
        if path == "/api/dialog/dir":
            # 原生目录选择框。取消时 dir=None，前端据此中止导出。
            # 没传起始目录就用上次导出用过的（进程刚起、前端还没记住时也能停对地方）
            start = (body.get("start") or "").strip()
            if not start:
                try:
                    start = a.cfg.ui().get("export_dir") or ""
                except Exception:
                    start = ""
            a.log("选目录：起始目录 %s" % (start or "（系统默认）"))
            return self._json({"ok": True, "dir": fspick.pick_folder(start)})
        if path == "/api/reveal":
            return self._json({"ok": True, **fspick.reveal(body.get("path"))})
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
            path_ = _req(body, "path", "实验路径")
            a._guard(path_, body.get("umtime"))
            a.eve.edit_node(path_, _req(body, "id", "节点 id"),
                            body.get("props") or {})
            return self._json({"ok": True, "umtime": a.eve.lab_umtime(path_)})
        if path == "/api/node/ifaces":
            a.require()
            p_ = _req(body, "path", "实验路径")
            a._guard(p_, body.get("umtime"))
            a.eve.set_interfaces(p_, _req(body, "id", "节点 id"),
                                 body.get("ifaces") or [])
            return self._json({"ok": True, "umtime": a.eve.lab_umtime(p_)})
        if path == "/api/lab/network":
            a.require()
            p_ = _req(body, "path", "实验路径")
            a._guard(p_, body.get("umtime"))
            net = body.get("net") or {}
            if body.get("id") not in (None, ""):
                a.eve.edit_network(p_, body.get("id"), net)     # 带 id = 改属性
            else:
                a.eve.add_network(p_, net)                      # 不带 = 新建
            return self._json({"ok": True, "umtime": a.eve.lab_umtime(p_)})
        if path == "/api/textboxes":
            return self._json({"ok": True, **a.save_textboxes(
                body.get("path"), body.get("items"))})
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

    # ---------- 拓扑导出 / 导入（core/labpack.py） ----------
    def _lab_export(self, body):
        """导出实验：.unl（拓扑 + 内嵌的节点配置）+ 文本框 → 一个 zip。

        配置本来就存在 .unl 的 <objects><configs> 里（base64），所以原样搬文件
        就等于连配置一起搬了；运行目录里没存进 .unl 的残留配置也会被捞进包里。
        """
        a = self.app
        path = _req(body, "path", "实验路径")
        out_dir = (body.get("out_dir") or "").strip().strip('"')
        if out_dir:
            # 统一成本机绝对路径（斜杠方向、`.`/`..` 都归一），再记下来给下次选目录用
            out_dir = os.path.abspath(out_dir)
            try:
                a.cfg.set_ui("export_dir", out_dir)
            except Exception:
                pass
        box = {}

        def worker(t):
            res = labpack.export_lab(a, path, out_dir=out_dir or None,
                                     progress=lambda m: t.update(stage=m))
            box.update(res)
            t.update(file=res["file"],
                     message="%d 节点 · %d 网络 · %d 个配置 · %d 个文本框 → %s"
                             % (res["nodes"], res["networks"], res["configs"],
                                res["textboxes"], res["dir"]))

        t = a.tasks.create("lab-export", "导出拓扑 %s" % os.path.basename(path), worker)
        a.log("导出拓扑 %s → %s（任务 %s）" % (path, out_dir or "默认目录", t.id))
        return self._json({"ok": True, "tid": t.id})

    def _lab_import(self, q):
        """导入拓扑：请求体是 zip，同步还原成一台新实验。"""
        a = self.app
        a.require(ssh=True)
        total = int(self.headers.get("Content-Length") or 0)
        if total <= 0:
            raise labpack.PackError("没有收到文件内容")
        if total > (64 << 20):
            raise labpack.PackError("包太大（%.1f MB，上限 64MB）" % (total / 1048576.0))

        tmpdir = os.path.join(tempfile.gettempdir(), "eve-desktop")
        os.makedirs(tmpdir, exist_ok=True)
        local = os.path.join(tmpdir, "labpack-%d.zip" % int(time.time() * 1000))
        with open(local, "wb") as f:
            remain = total
            while remain > 0:
                chunk = self.rfile.read(min(262144, remain))
                if not chunk:
                    break
                f.write(chunk)
                remain -= len(chunk)
        try:
            res = labpack.import_lab(a, local, q.get("folder") or "", q.get("name") or "")
        finally:
            try:
                os.remove(local)
            except OSError:
                pass
        a.log("导入拓扑 → %s（%d 节点 / %d 个配置）"
              % (res["path"], res["nodes"], res["configs"]))
        return self._json({"ok": True, **res})

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
