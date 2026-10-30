# -*- coding: utf-8 -*-
"""SSH 远程操作封装（paramiko）。

一台 EVE 只需要一条 Transport，多路复用：
  - exec()     执行命令（配置探测 / 镜像落位 / 网络配置）
  - sftp       上传下载（镜像导入导出）
  - channel()  直连远端端口（节点 telnet 控制台，绕开防火墙）
  - shell()    交互式 PTY（EVE 主机终端）
"""
import socket
import threading
import time

import paramiko


class RemoteError(Exception):
    pass


class Remote:
    def __init__(self, host, port=22, user="root", password="", timeout=15):
        self.host = host
        self.port = int(port or 22)
        self.user = user
        self.password = password
        self.timeout = timeout
        self.client = None
        self._lock = threading.RLock()
        self._ident = None

    # ---------- 连接 ----------
    def connect(self):
        with self._lock:
            if self.client is not None and self.client.get_transport() is not None \
                    and self.client.get_transport().is_active():
                return self
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                cli.connect(self.host, port=self.port, username=self.user,
                            password=self.password, timeout=self.timeout,
                            allow_agent=False, look_for_keys=False,
                            banner_timeout=self.timeout, auth_timeout=self.timeout)
            except paramiko.AuthenticationException:
                raise RemoteError("SSH 认证失败：用户名或密码不对")
            except Exception as e:
                raise RemoteError("SSH 连接失败：%s" % e)
            self.client = cli
            self._ident = self.identity()
            return self

    def close(self):
        with self._lock:
            if self.client:
                try:
                    self.client.close()
                except Exception:
                    pass
            self.client = None

    @property
    def transport(self):
        t = self.client.get_transport() if self.client else None
        if t is None or not t.is_active():
            self.connect()
            t = self.client.get_transport()
        return t

    # ---------- 执行 ----------
    def run(self, cmd, timeout=30):
        self.connect()
        try:
            _in, out, err = self.client.exec_command(cmd, timeout=timeout)
            o = out.read().decode("utf-8", "replace")
            e = err.read().decode("utf-8", "replace")
            rc = out.channel.recv_exit_status()
        except Exception as ex:
            raise RemoteError("命令执行失败：%s" % ex)
        return rc, o, e

    def exec_stream(self, cmd):
        """前台执行一条命令，返回可流式读取的 channel（抓包用：tcpdump -w -）。

        和 run() 的区别：不把 stdout 读成字符串，直接交给调用方 recv()，
        这样 pcap 二进制能边收边用。停止就关掉这个 channel。
        """
        self.connect()
        try:
            ch = self.transport.open_session()
            ch.settimeout(None)
            ch.exec_command(cmd)
            return ch
        except Exception as ex:
            raise RemoteError("命令执行失败：%s" % ex)

    def out(self, cmd, timeout=30, default=""):
        rc, o, _e = self.run(cmd, timeout=timeout)
        return o.strip() if rc == 0 else default

    def ok(self, cmd, timeout=30):
        try:
            rc, _o, _e = self.run(cmd, timeout=timeout)
            return rc == 0
        except RemoteError:
            return False

    def sudo(self, cmd, timeout=30):
        return self.run(cmd, timeout=timeout)

    # ---------- 文件 ----------
    def sftp(self):
        return self.transport.open_sftp_client()

    def read_text(self, path, limit=1 << 20):
        with self.sftp() as s:
            with s.open(path, "rb") as f:
                return f.read(limit).decode("utf-8", "replace")

    def write_text(self, path, text, mode=None):
        if isinstance(text, str):
            text = text.encode("utf-8")
        with self.sftp() as s:
            with s.open(path, "wb") as f:
                f.write(text)
            if mode:
                s.chmod(path, mode)

    def exists(self, path):
        try:
            with self.sftp() as s:
                s.stat(path)
            return True
        except (IOError, OSError):
            return False

    def upload(self, local, remote, progress=None, chunk=262144):
        """分块上传，progress(sent, total) 回调用于进度条"""
        import os
        total = os.path.getsize(local)
        with self.sftp() as s:
            sent = 0
            t0 = time.time()
            with open(local, "rb") as src, s.open(remote, "wb") as dst:
                dst.set_pipelined(True)
                while True:
                    buf = src.read(chunk)
                    if not buf:
                        break
                    dst.write(buf)
                    sent += len(buf)
                    if progress:
                        el = max(time.time() - t0, 0.001)
                        progress(sent, total, sent / el)
        return total

    def download(self, remote, local, progress=None, chunk=262144):
        with self.sftp() as s:
            try:
                total = s.stat(remote).st_size
            except IOError:
                total = 0
            sent = 0
            t0 = time.time()
            with s.open(remote, "rb") as src, open(local, "wb") as dst:
                while True:
                    buf = src.read(chunk)
                    if not buf:
                        break
                    dst.write(buf)
                    sent += len(buf)
                    if progress:
                        el = max(time.time() - t0, 0.001)
                        progress(sent, total, sent / el)
        return sent

    # ---------- 通道 ----------
    def channel(self, port, host="127.0.0.1", timeout=10):
        """打开到远端端口的数据通道（等价 ssh -L 的远端直连）"""
        t = self.transport
        try:
            ch = t.open_channel("direct-tcpip", (host, int(port)), ("127.0.0.1", 0),
                                timeout=timeout)
        except Exception as e:
            raise RemoteError("远端端口 %s 不可达：%s" % (port, e))
        ch.settimeout(timeout)
        return ch

    def shell(self, term_cols=120, term_rows=32):
        self.connect()
        ch = self.transport.open_session()
        ch.get_pty(term="xterm-256color", width=term_cols, height=term_rows)
        ch.invoke_shell()
        ch.settimeout(0.5)
        return ch

    # ---------- 环境 ----------
    def identity(self):
        """缓存一次系统信息，避免反复探测"""
        if self._ident:
            return self._ident
        rc, o, _ = self.run(
            "echo ARCH=$(uname -m); echo HOST=$(hostname); echo HOSTID=$(hostid 2>/dev/null); "
            "echo PY=$(command -v python3); echo VER=$(cat /opt/unetlab/html/themes/adminLTE/VERSION 2>/dev/null)",
            timeout=20)
        info = {"arch": "x86_64", "hostname": "eve-ng", "hostid": "007f0101",
                "python": "/usr/bin/python3", "version": ""}
        for line in o.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "ARCH" and v:
                info["arch"] = v
            elif k == "HOST" and v:
                info["hostname"] = v
            elif k == "HOSTID" and v:
                info["hostid"] = v.lower()
            elif k == "PY" and v:
                info["python"] = v
            elif k == "VER" and v:
                info["version"] = v
        info["tpl_arch"] = "amd" if info["arch"] in ("aarch64", "arm64") else "intel"
        self._ident = info
        return info

    def probe_capabilities(self):
        """宿主机侧能力（决定哪些按钮可用）"""
        ident = self.identity()
        rc, o, _ = self.run(
            "for p in tar gzip ip brctl iptables dnsmasq hostid; do "
            "printf '%s=%s\\n' $p \"$(command -v $p 2>/dev/null)\"; done", timeout=20)
        tools = {}
        for line in o.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                tools[k.strip()] = v.strip()
        return {"ident": ident, "tools": tools}


def open_tcp(host, port, timeout=8):
    """无 SSH 时直连远端端口（返回 socket 风格对象）"""
    s = socket.create_connection((host, int(port)), timeout=timeout)
    s.settimeout(timeout)
    return s
