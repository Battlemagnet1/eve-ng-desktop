# -*- coding: utf-8 -*-
"""EVE-NG REST 客户端。

只依赖标准库，兼容 Community 6.x / Pro 7.x：
  - 认证：POST /api/auth/login，cookie `unetlab_session`
  - 实验列表：Pro 用 /api/labs，CE 只有 /api/folders（自动探测）
  - 控制台端口：32768 + 128*tenant + node_id
"""
import http.cookiejar
import json
import posixpath
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


# EVE 的状态码大多是内部编号（98、2、60069…），原样甩给用户等于没说 ——
# 「Eve Unlicensed (98)」这种只在服务器上翻源码才看得懂。
# 这里只给「需要用户跑去服务器上动手」的几个配上人话，其余保持原样（不制造噪音）。
CODE_HINTS = {
    98: "服务器授权校验没通过 —— 它连不上 www.eve-ng.net（这台机器多半没配 DNS，或者断了外网）。"
        "在服务器上执行 resolvectl status 看一眼，网配通后约 1 分钟会自动恢复",
    2: "服务器拒绝了提权 —— 常见原因是 /opt/unetlab/data/Logs 里的日志被 root 占着、"
       "www-data 追加不进去。上服务器执行 "
       "chown -R www-data:www-data /opt/unetlab/data/Logs 再试",
}


class EveError(Exception):
    def __init__(self, message, code=None, payload=None):
        # 原话（多半是英文）留在 raw_message 里给日志和排查用，界面上给中文人话
        self.raw_message = message
        hint = CODE_HINTS.get(code) if isinstance(code, int) else None
        if hint:
            message = hint
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}


class EveSession:
    """一个到某台 EVE 的会话（含 cookie）。"""

    def __init__(self, host, port=80, scheme="http", timeout=20):
        self.host = host
        self.port = int(port or 80)
        self.scheme = scheme or "http"
        self.timeout = timeout
        # 自己管 cookie，不用 CookieJar：6.2 的登录响应里
        # `Set-Cookie: unetlab_session=...; expires=Sun, 29-Oct-3600 ...`
        # 年份 3600 是非法的，CookieJar / 浏览器都会直接丢弃，
        # 于是「登录成功」但后续所有请求 412（90001 未认证）。
        self.cookies = {}
        handlers = []
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [
            ("User-Agent", "EVE-Desktop/1.0"),
            ("Accept", "application/json"),
        ]
        self.user = ""
        self.password = ""
        self._lock = threading.RLock()
        self._relogin = False      # 重登重入标记，防 login↔probe 递归
        self._last_login = 0.0     # 上次登录时间，用于限制重登频率
        self.version = ""
        self.caps = {}

    # ---------- 底层 ----------
    def base(self):
        port = "" if (self.scheme == "http" and self.port == 80) else ":%d" % self.port
        return "%s://%s%s" % (self.scheme, self.host, port)

    def url(self, path):
        if not path.startswith("/"):
            path = "/" + path
        return self.base() + path

    def _absorb(self, headers):
        """只取 name=value，忽略 domain/path/expires 等属性（6.2 的 expires 是坏的）"""
        if headers is None:
            return
        try:
            items = headers.get_all("Set-Cookie") or []
        except AttributeError:
            return
        for line in items:
            head = line.split(";", 1)[0]
            if "=" not in head:
                continue
            name, val = head.split("=", 1)
            name, val = name.strip(), val.strip()
            if not name:
                continue
            if val in ("", "deleted", "null"):
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = val

    def request(self, method, path, params=None, body=None, raw=False, timeout=None):
        url = self.url(path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.cookies:
            headers["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                self._absorb(resp.headers)
                raw_bytes = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            self._absorb(e.headers)
            raw_bytes = e.read() or b""
            status = e.code
        except Exception as e:
            raise EveError("连接失败：%s" % e)
        if raw:
            return status, raw_bytes
        try:
            payload = json.loads(raw_bytes.decode("utf-8", "replace") or "{}")
        except ValueError:
            hint = ""
            if status in (401, 403):
                hint = "（账号或密码错误）"
            elif status >= 500:
                hint = "（服务端内部错误：常见于密码为空、Web 服务异常，详见服务器 /var/log/apache2/error.log）"
            raise EveError("返回不是 JSON（HTTP %s）%s" % (status, hint))
        code = payload.get("code", status)
        if isinstance(code, str):
            try:
                code = int(code)
            except ValueError:
                code = status
        return code, payload

    def api(self, method, path, params=None, body=None):
        """统一处理错误码；返回 data"""
        code, payload = self.request(method, path, params=params, body=body)
        if 200 <= code < 300:
            return payload.get("data")
        if code in (401, 412) and not self._relogin:
            # 会话过期，重登一次；带重入标记，避免 login→probe→api 无限递归。
            # 同时限制频率：EVE 同一账号只保留一个会话，登录会删掉其它会话，
            # 两个客户端（或轮询风暴）互踢时会疯狂重登，反而谁都连不上。
            now = time.time()
            if now - self._last_login < 2.0:
                raise EveError("会话被其它客户端顶掉（EVE 同一账号只允许一个会话）：%s"
                               % (payload.get("message") or code), code, payload)
            self._relogin = True
            try:
                self.login(probe=False)
            finally:
                self._relogin = False
            code, payload = self.request(method, path, params=params, body=body)
            if 200 <= code < 300:
                return payload.get("data")
        raise EveError(payload.get("message") or "接口返回 %s" % code, code, payload)

    def _soft(self, path, default=None):
        """探测用：任何错误都返回 default，且绝不触发重登"""
        try:
            code, payload = self.request("GET", path)
        except EveError:
            return default
        if 200 <= code < 300:
            return payload.get("data") if payload.get("data") is not None else default
        return default

    def get(self, path, params=None):
        return self.api("GET", path, params=params)

    def put(self, path, body=None):
        return self.api("PUT", path, body=body or {})

    def post(self, path, body=None):
        return self.api("POST", path, body=body or {})

    def delete(self, path):
        return self.api("DELETE", path)

    # ---------- 认证 ----------
    def login(self, user=None, password=None, probe=True):
        if user is not None:
            self.user = user
        if password is not None:
            self.password = password
        # 不要传 html5：EVE 会把 users.html5 置 0 并删掉 guacamole 用户，
        # 图形控制台（/html5/）就废了。不传时 EVE 默认按 1 处理。
        code, payload = self.request(
            "POST", "/api/auth/login",
            body={"username": self.user, "password": self.password})
        if code != 200:
            raise EveError(payload.get("message") or "登录失败（%s）" % code, code, payload)
        self._last_login = time.time()
        if probe:
            self.probe()
        return payload

    def probe(self):
        """探测能力，决定走哪套接口（不按版本号硬编码）。

        只用 `_soft()`：探测期间的 401/412 不能再触发重登，
        否则会 login → probe → api → login … 无限递归（曾经卡死连接）。
        """
        caps = {"html5": False, "list_labs": False, "tenant": 0, "ovs": False,
                "pro": False, "stop_mode": None}
        st = self._soft("/api/status", {}) or {}
        self.version = str(st.get("version") or "")
        # 7.x 的版本号形如 "7.2.0-4-PRO"；stop 接口在 Pro 上要求 stopmode
        caps["pro"] = "PRO" in self.version.upper()
        caps["list_labs"] = self._soft("/api/labs") is not None
        try:
            code, _ = self.request("GET", "/html5/", raw=True, timeout=8)
            caps["html5"] = (code == 200)
        except EveError:
            pass
        me = self._soft("/api/auth", {}) or {}
        try:
            caps["tenant"] = int(me.get("tenant") or 0)
        except (TypeError, ValueError):
            caps["tenant"] = 0
        nets = self._soft("/api/list/networks", {}) or {}
        caps["ovs"] = isinstance(nets, dict) and "ovs" in nets
        self.caps = caps
        return caps

    def console_port(self, node_id):
        if node_id in (None, ""):
            raise EveError("缺少节点 id，无法计算控制台端口")
        return 32768 + 128 * int(self.caps.get("tenant") or 0) + int(node_id)

    # ---------- 实验 ----------
    def list_labs(self, path=""):
        """返回 {folders:[...], labs:[...]}，路径统一以 / 开头。"""
        path = (path or "").strip("/")
        if self.caps.get("list_labs"):
            try:
                data = self.get("/api/labs/" + urllib.parse.quote(path)) or {}
                return {
                    "folders": [{"name": f} if isinstance(f, str) else f for f in (data.get("folders") or [])],
                    "labs": data.get("labs") or [],
                    "mode": "pro",
                }
            except EveError:
                pass
        data = self.get("/api/folders/" + urllib.parse.quote(path) if path else "/api/folders/") or {}
        return {
            "folders": data.get("folders") or [],
            "labs": data.get("labs") or [],
            "mode": "ce",
        }

    def lab_meta(self, path):
        return self.get("/api/labs/%s" % _q(path))

    def delete_lab(self, path):
        """删掉整个实验（.unl）。**调用前必须先把节点停掉**。

        Pro 7.2 在节点还运行时直接删会回
        400「Failed to delete Lab - Nodes still running (60069)」，删不掉；
        CE 6.2 虽然回 200，但会把 qemu/iol 进程和 /opt/unetlab/tmp/<tenant>/<uuid>
        留在那儿（之后起节点抢端口、status 报 2 的假象就是从这来的）。
        停机 + 等待的逻辑放在 server 层，这里只发请求。
        """
        path = (path or "").strip("/")
        if not path:
            raise EveError("没指定实验路径")
        return self.delete("/api/labs/%s" % _q(path))

    def lab_exists(self, path):
        """实验还在不在。删除后复核用 —— EVE 偶尔回 200 却没真删。"""
        try:
            return bool(self.lab_meta(path))
        except EveError as e:
            if getattr(e, "code", None) in (404, 60000):
                return False
            raise

    def lab_nodes(self, path):
        data = self.get("/api/labs/%s/nodes" % _q(path))
        return _keyed(data)

    def lab_networks(self, path):
        try:
            data = self.get("/api/labs/%s/networks" % _q(path))
        except EveError:
            return {}
        return _keyed(data)

    def lab_topology(self, path):
        data = self.get("/api/labs/%s/topology" % _q(path))
        return data if isinstance(data, list) else []

    def node(self, path, nid):
        return self.get("/api/labs/%s/nodes/%s" % (_q(path), nid))

    def node_interfaces(self, path, nid):
        """节点网卡。CE 6.x 是 `{槽位ID: {...}}`，Pro 7.x 是数组（无 ID，顺序即编号）。

        这里统一成 CE 的形状（键 = 接口编号字符串），这样接线、抓包、
        检查器下拉全都按同一套逻辑走，不需要到处判断版本。
        Pro 的数组第 i 个元素就对应接口编号 i（实测 PUT {"2": "1"} 改的是 e2）。
        """
        data = self.get("/api/labs/%s/nodes/%s/interfaces" % (_q(path), nid))
        if isinstance(data, dict):
            eth = data.get("ethernet")
            if isinstance(eth, list):
                data["ethernet"] = {str(i): it for i, it in enumerate(eth)
                                    if isinstance(it, dict)}
        return data

    def node_action(self, path, nid, action):
        """start / stop / wipe

        Pro 7.x 的 stop 必须带 stopmode（前端 `pn(path,id,3)` →
        `.../stop/stopmode=3`），不带会 400 `Request not valid (60027)`；
        CE 6.x 的 `/stop` 直接可用。两种写法都试，谁通了记进 caps，
        之后的调用不再试错（也有兜底作用：万一版本号判错也能自愈）。
        """
        if action != "stop":
            return self.get("/api/labs/%s/nodes/%s/%s" % (_q(path), nid, action))
        head = "/api/labs/%s/nodes/%s/stop" % (_q(path), nid)
        mode = self.caps.get("stop_mode")
        if mode is None:
            mode = 3 if self.caps.get("pro") else 0
        first, second = (head + "/stopmode=3", head) if mode == 3 else (head, head + "/stopmode=3")
        try:
            data = self.get(first)
        except EveError as e:
            # 换另一种写法再试一次；两种都失败就把**原错误**抛出去，
            # 免得「节点本来没开」这类业务错误被另一种写法的报错盖住
            try:
                data = self.get(second)
            except EveError:
                raise e
            self.caps["stop_mode"] = 0 if first.endswith("/stopmode=3") else 3
            return data
        self.caps["stop_mode"] = 3 if first.endswith("/stopmode=3") else 0
        return data

    def lab_action(self, path, action):
        return self.get("/api/labs/%s/nodes/%s" % (_q(path), action))

    def start_lab(self, path):
        """逐个启动节点，替代 EVE 的「一次性启动全部」。

        原因：EVE 6.2 的 unl_wrapper 在 start() 里直接对自己
        `posix_setgid(32768)`，IOL 节点还会 `posix_setuid`。批量模式下
        所有 IOL 节点在同一个进程里顺序启动，于是从第二个 IOL 节点起就以
        unl0 身份去 `tunctl`，报 EPERM（TUNSETIFF: Operation not permitted），
        整批中止（日志里的 "Failed to start node"）。

        逐节点调用时每次都是独立进程（root 起），天然绕开该问题，
        而且单点失败不影响其它节点。返回每个节点的结果。
        """
        nodes = self.lab_nodes(path) or {}
        results = []
        for nid in sorted(nodes, key=lambda x: (len(str(x)), str(x))):
            node = nodes.get(nid) or {}
            if str(node.get("status") or "0") not in ("0", "1"):
                results.append({"id": nid, "ok": True, "skipped": "已在运行"})
                continue
            try:
                self.node_action(path, nid, "start")
                results.append({"id": nid, "ok": True})
            except EveError as e:
                results.append({"id": nid, "ok": False, "error": str(e)})
        return results

    def edit_node(self, path, nid, props):
        """改节点属性。

        EVE 的 node->edit() 是「部分字段」语义 (isset 才改)，但：
          - 必须带 id，否则 $p['id'] 取不到，直接报 20024；
          - 改 image 时 checkNodeImage() 还要 type/template，缺了会被静默忽略，
            所以这里从当前节点补齐，避免用户改镜像后「看着成功其实没变」。
        """
        body = dict(props or {})
        body["id"] = int(nid)
        if "image" in body:
            cur = self.node(path, nid) or {}
            body.setdefault("type", cur.get("type"))
            body.setdefault("template", cur.get("template"))
        return self.put("/api/labs/%s/nodes/%s" % (_q(path), nid), body)

    def set_interfaces(self, path, nid, ifaces):
        """改接口接线。

        EVE 的 PUT nodes/{id}/interfaces 收的是**扁平映射** `{接口ID: 网络ID}`，
        不是对象数组；`""` 表示断开。接口 ID 是 EVE 内部的编号（如 e0/0=0、
        e0/1=16），跟名字不是一回事，必须先查一次 interfaces 做名字→ID 映射。

        ifaces 支持两种写法：
          [{"name": "e0/0", "network_id": 3}, ...]   或   {"e0/0": 3, ...}
        只提交需要改的接口，未提交的保持原样。
        """
        cur = self.node_interfaces(path, nid) or {}
        eth = cur.get("ethernet") or {}
        # 接口名有两套：EVE 内部名（e0、e0/1）和厂商真名（Gi0/0、Ethernet0/1）。
        # 界面上显示/传的是内部名，而 CE 的 ethernet 里只有厂商名，直接按名字查
        # 会查不到——曾经因此「接线看着成功、实际一根线都没接」（CE 6.2 实测）。
        # 这里给每个接口多挂几个别名，内部名按**顺序**换算，两套都能命中。
        order = sorted(eth.keys(), key=lambda k: int(k) if str(k).isdigit() else 1 << 30)
        by_name = {}
        for idx, iid in enumerate(order):
            nm = (eth.get(iid) or {}).get("name")
            if nm:
                by_name[str(nm)] = str(iid)
            by_name["e%d" % idx] = str(iid)        # e0、e1…
            by_name["e0/%d" % idx] = str(iid)      # e0/0、e0/1…

        if isinstance(ifaces, dict):
            items = list(ifaces.items())
        else:
            items = [((x or {}).get("name") or (x or {}).get("id"),
                      (x or {}).get("network_id")) for x in (ifaces or [])]

        body = {}
        for key, net in items:
            iid = by_name.get(str(key), str(key))
            if iid not in eth:
                continue                     # 名字对不上就跳过，不瞎写
            body[iid] = "" if net in (None, "", 0, "0") else str(net)
        if not body:
            # 一个都没匹配上就别当成功返回，否则上层会以为接好了
            want = [str((x or {}).get("name") or (x or {}).get("id"))
                    if isinstance(x, dict) else str(x) for x in items]
            have = [str((eth.get(i) or {}).get("name") or i) for i in order]
            raise EveError("接口 %s 不存在（这个节点有：%s）"
                           % ("、".join(want), "、".join(have)))
        return self.put("/api/labs/%s/nodes/%s/interfaces" % (_q(path), nid), body)

    def add_network(self, path, net):
        """新建一个网络（云）。EVE 需要 name/type/left/top 等字段。"""
        return self.post("/api/labs/%s/networks" % _q(path), net)

    def edit_network(self, path, nid, props):
        """改网络属性（名字、坐标、类型）。EVE 的 editNetwork 也是「给了才改」。"""
        body = {"id": int(nid)}
        body.update({k: v for k, v in (props or {}).items() if v is not None})
        return self.put("/api/labs/%s/networks/%s" % (_q(path), nid), body)

    def move_network(self, path, nid, left, top):
        """只改网络（云）坐标。和 move_node 一样只发 id/left/top。

        云图标原来在界面上拖不动，只能删了重加 —— 但 EVE 的 `.unl` 里
        network 也有 left/top，走 editNetwork 就能挪，不用动别的属性。
        """
        return self.edit_network(path, nid, {"left": int(left), "top": int(top)})

    def delete_network(self, path, nid):
        """删网络。EVE 会连带把接在上面的网卡断线。

        Pro 7.x 删一个**已经不存在**的网络会回 500（Slim 错误页、非 JSON），
        不是我们请求写错了。批量删除、或者刚删完节点导致桥自动消失时很容易
        踩到，所以这里先复核一遍：确实已经没了就当成功，别报假失败。
        """
        try:
            return self.delete("/api/labs/%s/networks/%s" % (_q(path), nid))
        except EveError:
            if self._gone(path, "networks", nid):
                return None
            raise

    def _gone(self, path, kind, nid):
        """实体是否已经不在实验里了（kind = "nodes" / "networks"）。

        查不出来（网络问题、权限）时返回 False —— 宁可把原错误抛上去，
        也不能凭一次失败的查询就假装删成功。
        """
        try:
            data = _keyed(self.get("/api/labs/%s/%s" % (_q(path), kind)))
        except EveError:
            return False
        return str(nid) not in {str(k) for k in (data or {})}

    def templates(self):
        """模板清单：{模板名: 描述}。没装镜像的模板描述里带 `.missing`。"""
        return self.get("/api/list/templates/") or {}

    def template_defaults(self, name):
        """模板的默认参数（把 EVE 的 options 摊平成 字段→默认值）。

        options 的形状是 `{字段: {name, type, value, list?}}`，其中 `name`
        是**节点名前缀**（如 IOL 的 "R"），不是节点名本身，所以单独摘出去。
        """
        t = self.template_options(name) or {}
        out, prefix = {}, ""
        for k, o in (t.get("options") or {}).items():
            if not isinstance(o, dict):
                continue
            if k == "name":
                prefix = o.get("value") or ""
                continue
            v = o.get("value")
            if v not in (None, ""):
                out[k] = v
        return out, prefix

    def template_type(self, name):
        """模板对应的节点类型（qemu/iol/docker/dynamips…）。

        EVE 的 `type` 不等于模板名：vios 是 qemu、iol 是 iol、docker 是 docker。
        Pro 7.x 会校验这个字段，填成模板名直接 20022「Cannot add node」。
        模板详情里带 `type`，拿不到就退回模板名（CE 老版本能容忍）。
        """
        try:
            t = self.template_options(name) or {}
        except EveError:
            return name
        return (t.get("type") or name) if isinstance(t, dict) else name

    def add_node(self, path, props):
        """新增节点。

        EVE 的 `POST labs/{path}/nodes` 必须带 name/left/top/template/type，
        少一个 `addNode()` 就返回错误码；模板的其它参数（image/ram/ethernet…）
        它**不会**自己补，缺了要等启动时才炸。所以这里先用模板默认值打底。

        名字取用户给的；用户留空时用模板前缀（EVE 自己会把节点 id 拼在后面，
        例如前缀 R → R1、R2）。
        """
        body = dict(props or {})
        tpl = (body.get("template") or body.get("type") or "").strip()
        if not tpl:
            raise EveError("新增节点缺少模板")
        defaults, prefix = self.template_defaults(tpl)
        merged = dict(defaults)
        merged.update({k: v for k, v in body.items() if v not in (None, "")})
        merged["template"] = tpl
        merged["type"] = self.template_type(tpl)   # vios→qemu 这类映射，不能直接填模板名
        merged["name"] = (body.get("name") or "").strip() or prefix or "R"
        merged["left"] = int(body.get("left") or 300)
        merged["top"] = int(body.get("top") or 300)
        data = self.post("/api/labs/%s/nodes" % _q(path), merged)
        return data

    def delete_node(self, path, nid):
        """删节点。EVE 会先停掉节点、清 tmp 目录，再写回 .unl。

        和 delete_network 一样，Pro 对「已经不存在」的节点也回 500，
        复核后当成功处理（批量删除时常见：别人已经删掉了）。
        """
        try:
            return self.delete("/api/labs/%s/nodes/%s" % (_q(path), nid))
        except EveError:
            if self._gone(path, "nodes", nid):
                return None
            raise

    def move_node(self, path, nid, left, top):
        """只改坐标。EVE 的 editNode 是「给了才改」的语义，所以只发 id/left/top。"""
        return self.put("/api/labs/%s/nodes/%s" % (_q(path), nid),
                        {"id": int(nid), "left": int(left), "top": int(top)})

    def lab_umtime(self, path):
        """实验文件的修改时间。

        `GET /api/labs/{path}` 的 meta 里**没有** umtime，只有目录列表
        （`/api/folders/`）里每个实验项带 umtime。并发保护要靠它，所以
        这里回目录里找一次。
        """
        p = (path or "").strip("/")
        folder, name = posixpath.dirname(p), posixpath.basename(p)
        try:
            data = self.list_labs(folder) or {}
        except EveError:
            return None
        for lab in data.get("labs") or []:
            lp = (lab.get("path") or lab.get("file") or "").strip("/")
            if posixpath.basename(lp) == name:
                return lab.get("umtime")
        return None


    def list_networks(self):
        return self.get("/api/list/networks") or {}

    def template_options(self, name):
        return self.get("/api/list/templates/%s" % urllib.parse.quote(name)) or {}


def _q(path):
    """实验路径 → API 路径片段（保留 / 分隔，去掉开头斜杠）"""
    p = (path or "").strip("/")
    return "/".join(urllib.parse.quote(seg) for seg in p.split("/") if seg != "")


def _keyed(data):
    """节点/网络集合统一成 {id: {...}}，并**保证每个元素带 id 字段**。

    EVE 的 `/api/labs/x/nodes` 返回的是以 id 为键的对象，元素本身没有 `id`。
    前端（检查器按钮、右键菜单）都靠 `node.id`，缺了就会把 undefined 发回来，
    表现为 `KeyError: 'id'` 或 `int(None)`。
    """
    if isinstance(data, list):
        out = {}
        for it in data:
            if not isinstance(it, dict):
                continue
            nid = str(it.get("id") or len(out) + 1)
            it = dict(it)
            it["id"] = nid
            out[nid] = it
        return out
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            v = dict(v) if isinstance(v, dict) else {"name": str(v)}
            v["id"] = str(k)
            out[str(k)] = v
        return out
    return {}
