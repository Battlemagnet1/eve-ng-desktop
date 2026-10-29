# -*- coding: utf-8 -*-
"""配置与凭据存储。

密码用 Windows DPAPI 加密（当前用户绑定），非 Windows 退化为 base64。
配置文件：%APPDATA%\\eve-desktop\\config.json
"""
import base64
import json
import os
import threading
import uuid


APP_DIR_NAME = "eve-desktop"


def app_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, APP_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _blob(data):
    import ctypes
    import ctypes.wintypes as w

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", w.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    return BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def protect(text):
    if not text:
        return ""
    data = text.encode("utf-8")
    if os.name != "nt":
        return "b64:" + base64.b64encode(data).decode()
    try:
        import ctypes

        din, _keep = _blob(data)
        dout = _blob(b"")[0]
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(din), None, None, None, None, 0, ctypes.byref(dout))
        if not ok:
            raise OSError("CryptProtectData failed")
        try:
            out = ctypes.string_at(dout.pbData, dout.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(ctypes.cast(dout.pbData, ctypes.c_void_p))
        return "dpapi:" + base64.b64encode(out).decode()
    except Exception:
        return "b64:" + base64.b64encode(data).decode()


def unprotect(token):
    if not token:
        return ""
    try:
        if token.startswith("b64:"):
            return base64.b64decode(token[4:]).decode("utf-8")
        if token.startswith("dpapi:"):
            raw = base64.b64decode(token[6:])
            import ctypes

            din, _keep = _blob(raw)
            dout = _blob(b"")[0]
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(din), None, None, None, None, 0, ctypes.byref(dout))
            if not ok:
                return ""
            try:
                out = ctypes.string_at(dout.pbData, dout.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(ctypes.cast(dout.pbData, ctypes.c_void_p))
            return out.decode("utf-8")
    except Exception:
        return ""
    # 没有任何前缀：早期版本存过明文，直接当明文用，下次保存时会被重新加密
    return token


DEFAULT_PROFILE = {
    "id": "",
    "name": "本地 EVE",
    "host": "192.168.1.50",
    "api_port": 80,
    "scheme": "http",
    "eve_user": "admin",
    "eve_pass": "",
    "ssh_port": 22,
    "ssh_user": "root",
    "ssh_pass": "",
    "use_ssh": True,
}


class Config:
    """单文件配置；线程安全保存。"""

    def __init__(self, path=None):
        self.path = path or os.path.join(app_dir(), "config.json")
        self._lock = threading.RLock()
        self.data = {"profiles": [], "active": "", "ui": {}}
        self.load()

    # ---------- 基本读写 ----------
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data.update(d)
        except (OSError, ValueError):
            pass
        if not self.data.get("profiles"):
            p = dict(DEFAULT_PROFILE)
            p["id"] = uuid.uuid4().hex[:8]
            self.data["profiles"] = [p]
            self.data["active"] = p["id"]

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ---------- profile ----------
    def profiles(self):
        """返回脱敏后的 profile 列表（不含密码）"""
        out = []
        for p in self.data.get("profiles", []):
            q = {k: v for k, v in p.items() if not k.endswith("_pass")}
            q["has_eve_pass"] = bool(p.get("eve_pass"))
            q["has_ssh_pass"] = bool(p.get("ssh_pass"))
            out.append(q)
        return out

    def profile(self, pid=None):
        pid = pid or self.data.get("active")
        for p in self.data.get("profiles", []):
            if p.get("id") == pid:
                return dict(p)
        return dict(DEFAULT_PROFILE)

    def profile_secrets(self, pid=None):
        """解出明文密码，仅内存使用"""
        p = self.profile(pid)
        p["eve_pass"] = unprotect(p.get("eve_pass", ""))
        p["ssh_pass"] = unprotect(p.get("ssh_pass", ""))
        return p

    def upsert_profile(self, incoming):
        with self._lock:
            pid = incoming.get("id") or uuid.uuid4().hex[:8]
            incoming["id"] = pid
            # 落盘前加密；留空表示沿用旧密码（下面 pop 掉，不覆盖）
            for k in ("eve_pass", "ssh_pass"):
                if incoming.get(k):
                    incoming[k] = protect(str(incoming[k]))
            old = self.profile(pid) if incoming.get("id") else {}
            for p in self.data["profiles"]:
                if p.get("id") == pid:
                    # 密码留空表示"不修改"
                    for k in ("eve_pass", "ssh_pass"):
                        if not incoming.get(k):
                            incoming.pop(k, None)
                    p.update(incoming)
                    break
            else:
                self.data["profiles"].append(incoming)
            self.save()
            return pid

    def set_active(self, pid):
        with self._lock:
            self.data["active"] = pid
            self.save()

    def delete_profile(self, pid):
        with self._lock:
            self.data["profiles"] = [p for p in self.data["profiles"] if p.get("id") != pid]
            if self.data.get("active") == pid and self.data["profiles"]:
                self.data["active"] = self.data["profiles"][0]["id"]
            self.save()

    def ui(self):
        return self.data.setdefault("ui", {})

    def set_ui(self, k, v):
        with self._lock:
            self.data.setdefault("ui", {})[k] = v
            self.save()
