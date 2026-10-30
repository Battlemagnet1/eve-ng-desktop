# -*- coding: utf-8 -*-
"""抓包：在 EVE 服务器上 tcpdump，抓完拉回本机给 Wireshark 打开。

为什么不用 EVE 的抓包按钮：网页版那个是 `capture://<host>/vunl0_1_0` 的
自定义协议，交给本机装的「EVE Client」插件去调 Wireshark，只对 Windows
装了客户端的环境有效；CE 的 REST API 里**根本没有抓包接口**
（api.php 里搜不到 pcap/capture），所以只能自己走 SSH。

实现的对应关系：
  节点网卡 e0/0 → 宿主机 tap `vunl{tenant}_{node_id}_{接口ID}`
  接口 ID 不是序号：e0/0=0、e0/1=16、e0/2=32……（EVE 每块网卡占 16 个槽）

流程：tcpdump 后台写文件 → 停止时 `kill -INT`（SIGINT 才会 flush 出完整
pcap，SIGKILL 会留半截）→ SFTP 拉回本机 → 本机 Wireshark 打开。
"""
import os
import shutil
import struct
import subprocess
import threading
import time

CAP_DIR = "/var/tmp/eve-desktop-cap"
DEFAULT_SNAPLEN = 0            # 0 = 抓完整包（-s 0）
LOCAL_SUBDIR = "eve-captures"
_BPF_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .:()_-/!<>=&|")


class CapError(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.message = str(msg)


def tap_name(tenant, node_id, ifid):
    """EVE 里网卡的 tap 名。ifid 用 /nodes/{id}/interfaces 里 ethernet 的键。"""
    return "vunl%s_%s_%s" % (int(tenant or 0), int(node_id), int(ifid))


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
    return shutil.which("wireshark") or shutil.which("wireshark.exe")


class Session:
    def __init__(self, sid, **kw):
        self.sid = sid
        self.node_id = kw["node_id"]
        self.node_name = kw["node_name"]
        self.ifid = kw["ifid"]
        self.iface = kw["iface"]
        self.ifname = kw["ifname"]
        self.remote = kw["remote"]
        self.pid = kw["pid"]
        self.started = time.time()
        self.bpf = kw.get("bpf") or ""
        self.lab = kw.get("lab") or ""
        self.stopped = False
        self.saved = ""

    def size(self):
        try:
            rc, o, _e = self.remote.run("stat -c %%s %s 2>/dev/null" % self.remote_file)
            return int(o.strip()) if rc == 0 and o.strip().isdigit() else 0
        except Exception:
            return 0

    @property
    def remote_file(self):
        return "%s/%s.pcap" % (CAP_DIR, self.sid)

    def info(self):
        return {
            "sid": self.sid,
            "node_id": self.node_id, "node": self.node_name,
            "iface": self.iface, "ifname": self.ifname,
            "bpf": self.bpf, "elapsed": int(time.time() - self.started),
            "bytes": self.size(), "stopped": self.stopped, "saved": self.saved,
        }


class Manager:
    """抓包会话登记表。所有会话都在服务器上跑 tcpdump，这里只记 pid/文件名。"""

    def __init__(self):
        self.items = {}
        self._n = 0
        self._lock = threading.RLock()

    # ---------- 起 ----------
    def start(self, remote, tenant, lab, node_id, node_name, ifid, iface,
              snaplen=DEFAULT_SNAPLEN, bpf=""):
        if not remote:
            raise CapError("抓包需要 SSH 连接（在连接设置里填 SSH 账号）")
        ifid = int(ifid)
        ifname = tap_name(tenant, node_id, ifid)
        bpf = _check_bpf(bpf)
        rc, o, e = remote.run("ip -o link show %s 2>&1" % ifname)
        if rc != 0:
            raise CapError("网卡 %s 不存在 —— 节点 %s 启动后才会建这块 tap"
                           % (ifname, node_name or node_id))
        with self._lock:
            self._n += 1
            sid = "cap%d_%d" % (int(time.time()), self._n)
        remote.run("mkdir -p %s && chmod 700 %s" % (CAP_DIR, CAP_DIR))
        cmd = ("nohup tcpdump -i %s -s %d -U -n -w %s/%s.pcap %s "
               ">%s/%s.log 2>&1 & echo $!" % (
                   ifname, int(snaplen or 0), CAP_DIR, sid, bpf, CAP_DIR, sid))
        rc, o, e = remote.run(cmd)
        pid = (o.strip().splitlines() or [""])[-1].strip()
        if rc != 0 or not pid.isdigit():
            raise CapError("tcpdump 起不来：%s" % ((e or o).strip()[:160] or "未知原因"))
        time.sleep(0.35)
        if not int(pid) or not remote.ok("kill -0 %s" % pid):
            log = remote.out("tail -3 %s/%s.log 2>/dev/null" % (CAP_DIR, sid))
            raise CapError("tcpdump 立刻退出了：%s" % (log or "见服务器日志"))
        s = Session(sid, node_id=node_id, node_name=node_name, ifid=ifid,
                    iface=iface, ifname=ifname, remote=remote, pid=pid,
                    bpf=bpf, lab=lab)
        with self._lock:
            self.items[sid] = s
        return s.info()

    # ---------- 停 ----------
    def stop(self, sid):
        s = self.items.get(sid)
        if not s:
            raise CapError("没有这个抓包会话（可能已经保存过）")
        if not s.stopped:
            s.remote.run("kill -INT %s 2>/dev/null; sleep 0.45" % s.pid)
            if s.remote.ok("kill -0 %s" % s.pid):
                s.remote.run("kill -TERM %s 2>/dev/null; sleep 0.3" % s.pid)
            s.stopped = True
        log = s.remote.out("tail -5 %s/%s.log 2>/dev/null" % (CAP_DIR, sid))
        info = s.info()
        info["log"] = log
        return info

    def stop_all(self):
        for sid in list(self.items):
            try:
                self.stop(sid)
            except CapError:
                pass

    # ---------- 存 ----------
    def save(self, sid, local_dir=None):
        """拉回本机。返回本地路径与包数。"""
        s = self.items.get(sid)
        if not s:
            raise CapError("没有这个抓包会话")
        self.stop(sid)
        if not s.remote.exists(s.remote_file):
            raise CapError("服务器上没生成抓包文件（可能一个包都没抓到就退出了）")
        out_dir = local_dir or os.path.join(os.path.expanduser("~"), "Documents", LOCAL_SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(s.started))
        node = _safe(s.node_name or ("node" + str(s.node_id)))
        label = _safe(os.path.splitext(os.path.basename(s.lab or "lab"))[0]) or "lab"
        local = os.path.join(out_dir, "%s_%s_%s_%s.pcap"
                             % (label, node, _safe(s.iface), stamp))
        s.remote.download(s.remote_file, local)
        st = pcap_stats(local)
        s.saved = local
        with self._lock:
            self.items.pop(sid, None)
        s.remote.run("rm -f %s %s" % (s.remote_file, s.remote_file.replace(".pcap", ".log")))
        return {"sid": sid, "file": local, "packets": st["packets"],
                "bytes": st["bytes"], "iface": s.iface, "node": s.node_name}

    def discard(self, sid):
        s = self.items.get(sid)
        if not s:
            raise CapError("没有这个抓包会话")
        s.remote.run("kill -INT %s 2>/dev/null; sleep 0.3; rm -f %s %s"
                     % (s.pid, s.remote_file, s.remote_file.replace(".pcap", ".log")))
        with self._lock:
            self.items.pop(sid, None)
        return {"ok": True}

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
