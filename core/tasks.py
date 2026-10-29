# -*- coding: utf-8 -*-
"""后台任务（导入 / 导出）与进度上报。"""
import threading
import time
import uuid


def human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "-"


class Task:
    def __init__(self, kind, title):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.title = title
        self.status = "queued"     # queued / running / done / failed / cancelled
        self.stage = "排队中"
        self.percent = 0.0
        self.sent = 0
        self.total = 0
        self.speed = 0.0
        self.message = ""
        self.file = ""             # 下载产物（本地路径）
        self.created = time.time()
        self.updated = time.time()
        self.cancel = False
        self._lock = threading.Lock()

    def update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.updated = time.time()
        return self

    def progress(self, sent, total, speed=None):
        pct = (sent / total * 100.0) if total else 0.0
        self.update(sent=sent, total=total, percent=round(min(pct, 100.0), 1),
                    speed=speed or 0.0)

    def snap(self):
        with self._lock:
            return {
                "id": self.id, "kind": self.kind, "title": self.title,
                "status": self.status, "stage": self.stage,
                "percent": self.percent, "sent": self.sent, "total": self.total,
                "sent_h": human(self.sent), "total_h": human(self.total),
                "speed_h": (human(self.speed) + "/s") if self.speed else "",
                "message": self.message, "file": self.file,
                "created": self.created, "updated": self.updated,
            }


class TaskManager:
    def __init__(self):
        self.items = {}
        self._lock = threading.Lock()

    def new(self, kind, title):
        """只登记，不启动（用于上传先建任务、后跑 worker）"""
        t = Task(kind, title)
        with self._lock:
            self.items[t.id] = t
            self._trim()
        return t

    def _trim(self):
        if len(self.items) <= 40:
            return
        for k in sorted(self.items, key=lambda k: self.items[k].created)[:10]:
            if self.items[k].status in ("done", "failed", "cancelled"):
                self.items.pop(k, None)

    def create(self, kind, title, fn):
        t = Task(kind, title)
        with self._lock:
            self.items[t.id] = t
            self._trim()

        def runner():
            t.update(status="running", stage="开始")
            try:
                fn(t)
                if t.status == "running":
                    t.update(status="done", stage="完成", percent=100.0)
            except Exception as e:
                if t.cancel:
                    t.update(status="cancelled", stage="已取消")
                else:
                    t.update(status="failed", stage="失败", message=str(e))

        threading.Thread(target=runner, daemon=True).start()
        return t

    def get(self, tid):
        return self.items.get(tid)

    def list(self):
        with self._lock:
            return [t.snap() for t in sorted(self.items.values(),
                                             key=lambda x: x.created, reverse=True)]
