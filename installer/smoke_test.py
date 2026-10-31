# -*- coding: utf-8 -*-
"""打包产物的冒烟测试：确认 exe 真能起来、本地服务能应答。

PyInstaller 的 onedir 把纯 Python 包装进 PYZ（base_library.zip / exe 里的归档），
`_internal/paramiko` 这类目录根本不会出现，所以「看文件在不在」判断不了依赖是否
打全 —— 之前就是这样漏掉了 paramiko，打出来的包一启动就 ModuleNotFoundError。
这里直接跑一次 server-only 模式，能应答才算过。

    python installer/smoke_test.py dist/EVE-Desktop/EVE-Desktop.exe
"""
import json
import subprocess
import sys
import time
import urllib.request

exe = sys.argv[1] if len(sys.argv) > 1 else r"dist\EVE-Desktop\EVE-Desktop.exe"
port = 18999
p = subprocess.Popen([exe, "--no-gui", "--port", str(port), "--no-auto-connect"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok, why = False, "启动后 25 秒内没有应答"
try:
    for _ in range(50):
        if p.poll() is not None:
            why = "进程提前退出（exit=%s），依赖没打全或启动异常" % p.returncode
            break
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=2) as r:
                json.loads(r.read() or b"{}")
            ok, why = True, "本地服务应答正常"
            break
        except Exception:
            time.sleep(0.5)
finally:
    try:
        p.terminate()
        p.wait(timeout=8)
    except Exception:
        p.kill()

print(("OK   " if ok else "FAIL ") + why)
sys.exit(0 if ok else 1)
