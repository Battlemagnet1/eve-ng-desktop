# -*- coding: utf-8 -*-
"""拓扑文本框的本地存储。

为什么不存在实验文件里
----------------------
EVE 的 `.unl` 只有两种对象：节点（`<node>`）和网络（`<network>`），没有
「自由文本」。往里面塞自定义元素的风险是：EVE 自己读写实验时会把它当脏数据
丢掉，或者干脆解析失败 —— 之前接口名那件事已经证明 EVE 对不合规的写入
会**静默忽略**，所以这类附加信息一律不碰实验文件。

存在本机侧车文件：
    %APPDATA%\\eve-desktop\\textboxes.json
结构是 `{"<host>/<实验路径>": [{id,text,x,y,size,color}, ...]}`。
换服务器、换实验互不干扰；卸载重装客户端也不丢（该目录不会被安装包清理）。
"""
import json
import os
import threading
import uuid

from core.config import app_dir

SIZES = ("1", "2", "3")
COLORS = ("", "accent", "ok", "warn", "err")


def _num(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    # NaN / inf 落进 JSON 会变成非法值，前端读出来全是 NaN
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


class Notes:
    def __init__(self, path=None):
        self.path = path or os.path.join(app_dir(), "textboxes.json")
        self._lock = threading.RLock()
        self.data = {}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data = {k: v for k, v in d.items() if isinstance(v, list)}
        except (IOError, OSError, ValueError):
            self.data = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def key(host, lab):
        return "%s/%s" % ((host or "").strip(), (lab or "").strip().lstrip("/"))

    def list(self, host, lab):
        with self._lock:
            return list(self.data.get(self.key(host, lab)) or [])

    def set(self, host, lab, items):
        """整组覆盖。前端每次改动（拖动/编辑/删除）都提交全量，逻辑最简单。"""
        out = []
        for it in items or []:
            it = it or {}
            text = str(it.get("text") or "")
            if not text.strip():
                continue            # 空文本没有意义，直接丢掉
            size = str(it.get("size") or "2")
            color = str(it.get("color") or "")
            out.append({
                "id": str(it.get("id") or uuid.uuid4().hex[:8])[:16],
                "text": text[:2000],
                "x": round(_num(it.get("x")), 1),
                "y": round(_num(it.get("y")), 1),
                "size": size if size in SIZES else "2",
                "color": color if color in COLORS else "",
            })
        k = self.key(host, lab)
        with self._lock:
            if out:
                self.data[k] = out
            else:
                self.data.pop(k, None)
            self.save()
        return out
