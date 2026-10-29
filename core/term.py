# -*- coding: utf-8 -*-
"""终端会话：节点控制台（telnet）与 EVE 主机 Shell（SSH PTY）。

输出通过 SSE 推给前端 xterm.js；键盘输入走 POST。
内置极简 telnet 协商：节点控制台若不回应 IAC，IOL/IOS 会吞按键或乱码。
"""
import base64
import queue
import socket
import threading
import time
import uuid

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 1, 3, 24, 31


class TelnetFilter:
    """剥掉 IAC 序列，必要时回包；其余字节原样透传。

    协商策略（按 IOS/IOL 实际行为校准）：
      - WILL ECHO  → DO ECHO：由设备回显。xterm.js 不做本地回显，必须让设备回显，
        否则用户打字看不见；IOL 在某些版本下即便收到 DONT 也会继续回显，但不能赌。
      - WILL SGA   → DO SGA（我们不处理 GA）
      - WILL NAWS  → DO NAWS，并置 self.naws=True；**只有协商过才允许发 resize**，
        否则 NAWS 载荷（含 \\x00）会被设备当成键盘输入，污染命令行。
      - WILL TTYPE → DO TTYPE
      - DO *       → WONT（保持 NVT ASCII，不进入二进制模式）
    """

    def __init__(self):
        self.state = 0  # 0 数据 1 IAC 2 选项 3 SB 4 SB-IAC
        self.naws = False          # 设备是否协商了窗口尺寸
        self._sb_opt = 0
        self._sb_buf = bytearray()

    def feed(self, data):
        out = bytearray()
        reply = bytearray()
        for b in data:
            if self.state == 0:
                if b == IAC:
                    self.state = 1
                else:
                    out.append(b)
            elif self.state == 1:
                if b == IAC:
                    out.append(IAC)
                    self.state = 0
                elif b in (DO, DONT, WILL, WONT):
                    self._cmd = b
                    self.state = 2
                elif b == SB:
                    self._sb_opt = 0
                    self._sb_buf = bytearray()
                    self.state = 3
                else:
                    self.state = 0
            elif self.state == 2:
                cmd, opt = self._cmd, b
                if cmd == DO:
                    if opt == OPT_TTYPE:
                        # 答应后等设备发 SB TTYPE SEND，再回 vt100
                        reply += bytes([IAC, WILL, opt])
                    else:
                        # 不进入二进制模式；其余一律拒绝
                        reply += bytes([IAC, WONT, opt])
                elif cmd == WILL:
                    if opt == OPT_NAWS:
                        self.naws = True
                        reply += bytes([IAC, DO, opt])
                    elif opt in (OPT_ECHO, OPT_SGA, OPT_TTYPE):
                        reply += bytes([IAC, DO, opt])
                    else:
                        reply += bytes([IAC, DONT, opt])
                self.state = 0
            elif self.state == 3:
                if b == IAC:
                    self.state = 4
                else:
                    if self._sb_opt == 0:
                        self._sb_opt = b     # SB 后第一个字节是选项号
                    else:
                        self._sb_buf.append(b)
            elif self.state == 4:
                if b == SE:
                    if self._sb_opt == OPT_TTYPE and bytes(self._sb_buf[:1]) == b"\x01":
                        reply += self.ttype_reply()   # 1 = SEND
                    self._sb_opt = 0
                    self._sb_buf = bytearray()
                    self.state = 0
                elif b == IAC:
                    self._sb_buf.append(IAC)
                    self.state = 3
                else:
                    self.state = 3
        return bytes(out), bytes(reply)

    def ttype_reply(self):
        name = b"vt100"
        return bytes([IAC, SB, OPT_TTYPE, 0]) + name + bytes([IAC, SE])

    def naws(self, cols, rows):
        return bytes([IAC, SB, OPT_NAWS, (cols >> 8) & 255, cols & 255,
                      (rows >> 8) & 255, rows & 255, IAC, SE])


MAX_BUF = 512 * 1024


class TermSession:
    def __init__(self, title, kind, reader_chan, writer_chan=None, resize_fn=None,
                 telnet=True, closers=()):
        self.sid = uuid.uuid4().hex[:12]
        self.title = title
        self.kind = kind
        self.chan = reader_chan
        self.wchan = writer_chan or reader_chan
        self.resize_fn = resize_fn
        self.telnet = TelnetFilter() if telnet else None
        self.buf = bytearray()
        self.base = 0          # buf[0] 对应全局偏移
        self.total = 0
        self.closed = False
        self.error = ""
        self.created = time.time()
        self.closers = list(closers)
        self._ev = threading.Event()
        self._stop = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    # ---------- 读 ----------
    def _pump(self):
        while not self._stop:
            try:
                data = self.chan.recv(8192)
            except socket.timeout:
                continue
            except Exception as e:
                self.error = str(e)
                break
            if not data:
                break
            if self.telnet:
                data, reply = self.telnet.feed(data)
                if reply:
                    try:
                        self.wchan.sendall(reply)
                    except Exception:
                        pass
                if not data:
                    continue
            with self._lock:
                self.buf += data
                self.total += len(data)
                if len(self.buf) > MAX_BUF:
                    drop = len(self.buf) - MAX_BUF
                    del self.buf[:drop]
                    self.base += drop
            self._ev.set()
        self.closed = True
        self._ev.set()

    # ---------- 推送 ----------
    def chunks(self, offset, timeout=15.0):
        """SSE 生成器：从 offset 开始按增量推送"""
        last_beat = time.time()
        while True:
            with self._lock:
                if offset < self.base:
                    offset = self.base            # 落后太多，从缓冲区起点接上
                if offset < self.base + len(self.buf):
                    piece = bytes(self.buf[offset - self.base:])
                    offset = self.base + len(self.buf)
                else:
                    piece = b""
            if piece:
                last_beat = time.time()
                yield {"o": offset, "d": base64.b64encode(piece).decode()}
                continue
            if self.closed:
                yield {"o": offset, "e": self.error or "closed"}
                return
            if self._ev.wait(0.4):
                self._ev.clear()
            elif time.time() - last_beat > timeout:
                last_beat = time.time()
                yield {"beat": 1}

    # ---------- 写 ----------
    def write(self, data):
        if self.closed:
            return 0
        try:
            if isinstance(data, str):
                data = data.encode("utf-8")
            self.wchan.sendall(data)
            return len(data)
        except Exception as e:
            self.error = str(e)
            return 0

    def resize(self, cols, rows):
        cols, rows = max(20, int(cols)), max(5, int(rows))
        try:
            if self.resize_fn:
                self.resize_fn(cols, rows)
            elif self.telnet and self.telnet.naws:
                # 只有设备协商过 NAWS 才能发，否则字节会被当成键盘输入
                self.write(self.telnet.naws(cols, rows))
        except Exception:
            pass

    def close(self):
        self._stop = True
        for fn in self.closers:
            try:
                fn()
            except Exception:
                pass
        try:
            self.chan.close()
        except Exception:
            pass
        self.closed = True
        self._ev.set()


class TermManager:
    def __init__(self):
        self.items = {}
        self._lock = threading.Lock()

    def add(self, sess):
        with self._lock:
            self.items[sess.sid] = sess
        return sess.sid

    def get(self, sid):
        return self.items.get(sid)

    def close(self, sid):
        with self._lock:
            s = self.items.pop(sid, None)
        if s:
            s.close()

    def close_all(self):
        with self._lock:
            items = list(self.items.values())
            self.items.clear()
        for s in items:
            s.close()

    def list(self):
        return [{"sid": s.sid, "title": s.title, "kind": s.kind, "closed": s.closed}
                for s in self.items.values()]
