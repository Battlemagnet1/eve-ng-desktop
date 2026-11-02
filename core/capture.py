# -*- coding: utf-8 -*-
"""抓包：SSH 上跑 tcpdump，把 pcap 流**实时**灌进本机 Wireshark。

为什么不用 EVE 的抓包按钮：网页版那个是 `capture://<host>/vunl0_1_0` 的
自定义协议，交给本机装的「EVE Client」插件去调 Wireshark；CE 的 REST API
里**根本没有抓包接口**（api.php 里搜不到 pcap/capture），所以只能走 SSH。

工作方式（与旧版「抓完再拉回」不同）：
  1. SSH 执行 `tcpdump -i <tap> -s0 -U -n -w -`，stdout 是裸 pcap 流
  2. 本机线程边读边做两件事：写本地 .pcap 文件（保底留档）+ 写进
     `Wireshark -k -i -` 的 stdin（界面里实时滚包，点了就能看）
  3. 停止 = 给远端 tcpdump 发 SIGINT、关掉 Wireshark 的 stdin。
     文件本来就在本地持续写，所以停止后不需要回传，直接可用。
  4. 用户直接关掉 Wireshark 窗口 → 写管道失败 → 自动结束抓包并保留文件

接口 → tap 的对应关系（两个大版本各一套，见 tap_name / tap_name_legacy）：
  EVE 6.x：`vunl{tenant}_{node_id}_{接口ID}`
  EVE 7.x：`vun{lab_id:03x}{tenant:04x}{node_id:03x}{接口ID:02x}`
  接口 ID 不是序号：e0/0=0、e0/1=16、e0/2=32……（EVE 每块网卡占 16 个槽）
"""
import os
import struct
import subprocess
import threading
import time

LOCAL_SUBDIR = "eve-captures"
CHUNK = 1 << 16
_BPF_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .:()_-/!<>=&|")


class CapError(ValueError):
    """抓包失败。继承 ValueError 是为了复用 server 的 400 映射 ——
    否则前端会拿到 `CapError: 网卡 xxx 不存在` 这种带类名的技术文案。"""

    def __init__(self, msg):
        super().__init__(msg)
        self.message = str(msg)


def _hexw(v, width):
    """EVE 前端 `("000" + v.toString(16)).slice(-width)` 的等价物。

    定宽小写十六进制、超出就取低位 —— 必须跟着前端走，不能按十进制补零，
    否则 e0/1（接口 ID 16）会写成 016 而服务器上是 10。
    """
    try:
        s = "%x" % int(v)
    except (TypeError, ValueError):
        s = "0"
    return ("0" * width + s)[-width:]


def tap_name(tenant, node_id, ifid, lab_id=1, vpcs=False):
    """EVE 7.x 的 tap 名，ifid 用 /nodes/{id}/interfaces 里 ethernet 的键。

    拼法照抄 7.2 前端 labview 里生成 realname 的那段：
        "000"+lab_id.toString(16)  取后 3 位
      + "0000"+tenant.toString(16) 取后 4 位
      + "000"+node.id.toString(16) 取后 3 位
      + "00"+if.id.toString(16)    取后 2 位
    实测 Pro 7.2.0-4：lab 1 / tenant 0 / 节点 1 的四个口是
    vun001000000100 …… vun001000000133（e1/0 的 16 写成 hex 的 10）。
    """
    b = 255 if (vpcs and int(ifid) == 0) else int(ifid)
    return ("vun" + _hexw(lab_id, 3) + _hexw(tenant, 4)
            + _hexw(node_id, 3) + _hexw(b, 2))


def tap_name_legacy(tenant, node_id, ifid):
    """EVE 6.x 的 tap 名：`vunl{tenant}_{node_id}_{ifid}`。"""
    return "vunl%s_%s_%s" % (int(tenant or 0), int(node_id), int(ifid))


def tap_candidates(tenant, node_id, ifid, lab_id=1, vpcs=False):
    """两代命名都试一遍，新版本优先。

    版本号靠不住（6.2 的 pro 包、7.2 的 CE 包都见过），与其判版本，
    不如把两个名字拿去服务器上问一句「有没有这块网卡」。
    """
    out = [tap_name(tenant, node_id, ifid, lab_id, vpcs)]
    old = tap_name_legacy(tenant, node_id, ifid)
    if old not in out:
        out.append(old)
    return out


def _check_bpf(bpf):
    bpf = (bpf or "").strip()
    if not bpf:
        return ""
    bad = set(bpf) - _BPF_OK
    if bad:
        raise CapError("过滤表达式里有不支持的字符：%s（只支持常规 BPF 语法）"
                       % "".join(sorted(bad)))
    return bpf


def pcap_stats(path):
    """数一数 pcap 里的包数（只走记录头，不读包体，几十 MB 也很快）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            hdr = f.read(24)
            if len(hdr) < 24:
                return {"packets": 0, "bytes": size}
            magic = hdr[:4]
            if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
                endian = "<"
            elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
                endian = ">"
            else:
                return {"packets": 0, "bytes": size}
            n, off = 0, 24
            while True:
                f.seek(off)
                rh = f.read(16)
                if len(rh) < 16:
                    break
                caplen = struct.unpack(endian + "I", rh[8:12])[0]
                if caplen > size:          # 文件正在写，尾部半条记录
                    break
                off += 16 + caplen
                n += 1
            return {"packets": n, "bytes": size}
    except OSError:
        return {"packets": 0, "bytes": 0}


def wireshark_exe():
    """找本机 Wireshark。找不到返回 None。"""
    cands = [
        os.environ.get("WIRESHARK_EXE"),
        r"C:\Program Files\Wireshark\Wireshark.exe",
        r"C:\Program Files (x86)\Wireshark\Wireshark.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Wireshark\Wireshark.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


# Wireshark 官网下载页。安装包里原先那个「在线下载」已经废掉了：连
# 2.na.dl.wireshark.org / 1.eu.dl.wireshark.org 在国内基本超时，装到一半弹
# 「下载失败」比不提供还难受。现在只给页面链接，用户自己在浏览器里下
# （能走代理、能挑镜像），装完回来这里就能认出来。
WIRESHARK_URL = "https://www.wireshark.org/download.html"


def wireshark_info():
    """本机 Wireshark 状态。前端据此决定要不要提示「还没装」。"""
    exe = wireshark_exe()
    return {"installed": bool(exe), "exe": exe, "url": WIRESHARK_URL}


def open_wireshark_download():
    """用系统浏览器打开 Wireshark 下载页（不走应用内的窗口）。"""
    try:
        os.startfile(WIRESHARK_URL)
    except OSError as e:
        raise CapError("打不开浏览器（%s）。手动访问：%s" % (str(e)[:80], WIRESHARK_URL))
    return {"opened": True, "url": WIRESHARK_URL}


def _link_names(remote):
    """服务器上所有网卡名。`ip -o link` 的 `vun001000000100@if7` 要切掉 @ 那一截。"""
    rc, out, _e = remote.run(
        "ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1")
    return (out or "").split() if rc == 0 else []


def _find_tap(remote, tenant, node_id, ifid, lab_id, vpcs, node_name):
    """到服务器上确认这次要抓的 tap 到底叫什么。

    先按公式试（6.x / 7.x 各一个），都对不上就按「节点 id + 网卡编号」
    在 vun* 里扫一遍 —— 万一哪天 EVE 又把命名改一遍（7.x 就是这么改的），
    至少不会因为公式过期就抓不了包。
    """
    cands = tap_candidates(tenant, node_id, ifid, lab_id, vpcs)
    for c in cands:
        if remote.run("ip -o link show dev %s 2>&1" % c)[0] == 0:
            return c

    names = _link_names(remote)
    tail = _hexw(node_id, 3) + _hexw(255 if (vpcs and int(ifid) == 0) else ifid, 2)
    for nm in names:
        if nm.startswith("vun") and len(nm) == 15 and nm.endswith(tail):
            return nm

    # 报错时把「看着像」的几个名字带上，省得用户再上服务器查
    near = [n for n in names if n.startswith(("vun", "vunl"))][:6]
    raise CapError(
        "节点 %s 的这块网卡上找不到 tap（试过 %s）。先确认节点真的在跑；"
        "如果确实在跑，多半是这台 EVE 的网卡命名又变了，把服务器上 "
        "`ip -o link show` 里 vun 开头的几行发我%s"
        % (node_name, "、".join(cands),
           ("（这台机器上现有：" + "、".join(near) + "）") if near else
           "（这台机器上现在一块 vun/vunl 网卡都没有）"))


class Session:
    """一次抓包：一个远端 tcpdump 通道 + 一个本地文件 + 一个 Wireshark 进程。"""

    def __init__(self, sid, **kw):
        self.sid = sid
        self.node_id = kw["node_id"]
        self.node_name = kw["node_name"]
        self.ifid = kw["ifid"]
        self.iface = kw["iface"]
        self.ifname = kw["ifname"]
        self.tap = kw["tap"]
        self.remote = kw["remote"]
        self.bpf = kw.get("bpf") or ""
        self.lab = kw.get("lab") or ""
        self.file = kw["file"]
        self.started = time.time()
        self.bytes = 0
        self.packets = 0
        self.stopped = False
        self.note = ""            # 给前端看的一句话（Wireshark 关了之类）
        self.ws = None            # Wireshark 进程
        self.ws_dead = False
        self._killed = False
        self.ch = None            # paramiko channel
        self.thread = None
        self._fh = open(self.file, "wb", buffering=0)
        self._buf = b""
        self._hdr = False         # pcap 全局头是否已消费
        self._lock = threading.Lock()

    # ---------- 数据流 ----------
    def _count(self, data):
        """顺带数包：只走 pcap 记录头，用来给面板显示实时包数。"""
        buf = self._buf + data
        if not self._hdr:
            if len(buf) < 24:
                self._buf = buf
                return
            magic = buf[:4]
            if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
                self._endian = "<"
            elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
                self._endian = ">"
            else:
                self._endian = "<"
            buf = buf[24:]
            self._hdr = True
        endian = getattr(self, "_endian", "<")
        off = 0
        while len(buf) - off >= 16:
            caplen = struct.unpack(endian + "I", buf[off + 8:off + 12])[0]
            if len(buf) - off < 16 + caplen:
                break
            off += 16 + caplen
            self.packets += 1
        self._buf = buf[off:] if off else buf
        if len(self._buf) > (1 << 20):        # 异常大包不至于把内存拖爆
            self._buf = b""

    def _feed(self, data):
        self._fh.write(data)
        self.bytes += len(data)
        self._count(data)
        ws = self.ws
        if ws is not None and not self.ws_dead and ws.stdin:
            try:
                ws.stdin.write(data)
                ws.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.ws_dead = True
                self.note = "Wireshark 已关闭，抓包跟着停了（包已存到本机）"

    def _pump(self):
        try:
            while not self.stopped:
                data = self.ch.recv(CHUNK)
                if not data:
                    break
                self._feed(data)
                if self.ws_dead:
                    break
        except Exception as e:              # 通道被关、连接断开
            if not self.stopped:
                self.note = self.note or ("抓包中断：%s" % str(e)[:120])
        finally:
            # Wireshark 被关 / 通道异常时，远端 tcpdump 也要一起收掉，
            # 否则会话没了它还挂在服务器上抓
            if not self.stopped:
                self._kill_remote()
            self._close_local()

    def _kill_remote(self):
        if getattr(self, "_killed", False):
            return
        self._killed = True
        try:
            # SIGINT 才会让 tcpdump 把最后一个包写干净
            self.remote.run("pkill -INT -f 'tcpdump -i %s '" % self.tap, timeout=8)
        except Exception:
            pass

    def _close_local(self):
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass
            ws = self.ws
            if ws is not None and ws.stdin:
                try:
                    ws.stdin.close()        # 让 Wireshark 停止捕获，但窗口留着看包
                except Exception:
                    pass
            self.stopped = True

    # ---------- 信息 ----------
    def info(self):
        return {
            "sid": self.sid,
            "node_id": self.node_id, "node": self.node_name,
            "iface": self.iface, "ifname": self.ifname, "bpf": self.bpf,
            "elapsed": int(time.time() - self.started),
            "bytes": self.bytes, "packets": self.packets,
            "stopped": self.stopped, "file": self.file,
            "ws": bool(self.ws is not None and not self.ws_dead),
            "note": self.note,
        }

    # ---------- 停 ----------
    def stop(self):
        if not self.stopped:
            self._kill_remote()
            try:
                if self.ch:
                    self.ch.close()
            except Exception:
                pass
            if self.thread:
                self.thread.join(5)
        self._close_local()
        return self.info()


class Manager:
    """抓包会话登记表。所有会话都在服务器上跑 tcpdump，这里管本地文件与 Wireshark。"""

    def __init__(self):
        self.items = {}
        self._n = 0
        self._lock = threading.RLock()

    def start(self, remote, tenant, lab, node_id, node_name, ifid, iface,
              snaplen=0, bpf="", local_dir=None, lab_id=1, vpcs=False):
        if not remote:
            raise CapError("抓包需要 SSH 连接（在连接设置里填 SSH 账号）")
        ifid = int(ifid)
        bpf = _check_bpf(bpf)
        tap = _find_tap(remote, tenant, node_id, ifid, lab_id, vpcs,
                        node_name or node_id)

        out_dir = local_dir or os.path.join(os.path.expanduser("~"), "Documents", LOCAL_SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        base = "%s_%s_%s_%s" % (_safe(os.path.splitext(os.path.basename(lab or "lab"))[0])
                                or "lab", _safe(node_name or ("node" + str(node_id))),
                                _safe(iface), stamp)
        local = os.path.join(out_dir, base + ".pcap")

        with self._lock:
            self._n += 1
            sid = "cap%d_%d" % (int(time.time()), self._n)

        s = Session(sid, node_id=node_id, node_name=node_name, ifid=ifid,
                    iface=iface, ifname=tap, tap=tap, remote=remote,
                    bpf=bpf, lab=lab, file=local)

        # 先把 Wireshark 拉起来（它在 stdin 上等第一个字节）
        exe = wireshark_exe()
        if exe:
            try:
                s.ws = subprocess.Popen([exe, "-k", "-i", "-"],
                                        stdin=subprocess.PIPE, bufsize=0)
            except OSError as ex:
                s.ws = None
                s.note = "本机 Wireshark 启动失败（%s），只在本机存文件" % str(ex)[:80]
        else:
            s.note = ("本机没装 Wireshark —— 包照样实时存到本机，装好后点"
                      "「用 Wireshark 打开」就能看")

        cmd = "exec tcpdump -i %s -s %d -U -n -w - %s" % (tap, int(snaplen or 0), bpf)
        try:
            s.ch = remote.exec_stream(cmd)
            # 稍等一下，让 tcpdump 立刻出错（比如没权限）时能反映出来
            time.sleep(0.25)
            if not s.ch.recv_ready() and s.ch.exit_status_ready():
                err = s.ch.recv_stderr(400).decode("utf-8", "replace")
                raise CapError("tcpdump 起不来：%s" % (err.strip()[:160] or "见服务器日志"))
        except CapError:
            s._close_local()
            raise
        except Exception as ex:
            s._close_local()
            raise CapError("tcpdump 起不来：%s" % str(ex)[:160])

        s.thread = threading.Thread(target=s._pump, name="cap-%s" % sid, daemon=True)
        s.thread.start()
        with self._lock:
            self.items[sid] = s
        return s.info()

    def _get(self, sid):
        s = self.items.get(sid)
        if not s:
            raise CapError("没有这个抓包会话（可能已经停止过）")
        return s

    def stop(self, sid):
        s = self._get(sid)
        info = s.stop()
        return info

    def save(self, sid, local_dir=None):
        """停止并保留文件。文件一直写在本地，这里只是收尾 + 报包数。

        local_dir 仅为兼容旧调用（路径在 start 时就定了，这里忽略）。
        """
        s = self._get(sid)
        s.stop()
        st = pcap_stats(s.file) if os.path.isfile(s.file) else {"packets": 0, "bytes": 0}
        if st["packets"] == 0 and not os.path.isfile(s.file):
            raise CapError("没生成抓包文件（可能一个包都没抓到）")
        with self._lock:
            self.items.pop(sid, None)
        return {"sid": sid, "file": s.file, "packets": st["packets"],
                "bytes": st["bytes"], "iface": s.iface, "node": s.node_name,
                "note": s.note}

    def discard(self, sid):
        s = self._get(sid)
        s.stop()
        try:
            os.remove(s.file)
        except OSError:
            pass
        with self._lock:
            self.items.pop(sid, None)
        return {"ok": True}

    def stop_all(self):
        for sid in list(self.items):
            try:
                self.stop(sid)
            except CapError:
                pass

    def list(self):
        with self._lock:
            items = list(self.items.values())
        out = []
        for s in items:
            try:
                out.append(s.info())
            except CapError:
                pass
        return out


def open_local(path):
    """用本机 Wireshark 打开 pcap；没有 Wireshark 就用系统默认程序，再不行只报路径。"""
    if not path or not os.path.isfile(path):
        raise CapError("文件不存在：%s" % path)
    exe = wireshark_exe()
    if exe:
        subprocess.Popen([exe, path], close_fds=True)
        return {"opened": True, "how": "wireshark", "exe": exe, "file": path}
    try:
        os.startfile(path)          # 关联了 .pcap 的程序（可能是 Wireshark）
        return {"opened": True, "how": "assoc", "file": path}
    except OSError:
        return {"opened": False, "how": "none", "file": path,
                "hint": "本机没找到 Wireshark，也没关联 .pcap：先装 Wireshark，"
                        "或手工打开上面这个文件"}


def _safe(name):
    keep = "-_.()"
    return "".join(c for c in str(name) if c.isalnum() or c in keep).strip() or "x"
