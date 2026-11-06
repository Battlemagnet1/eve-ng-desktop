# -*- coding: utf-8 -*-
"""CI：校验版本号在安装脚本与两份 README 之间一致。

改版本时要动三处，漏一处就会让文档里的示例包名与实际产物对不上。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer" / "EVE-Desktop.iss"
READMES = [ROOT / "README.md", ROOT / "README.en.md"]

errors = []

m = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', ISS.read_text(encoding="utf-8"))
if not m:
    print("FAIL  installer/EVE-Desktop.iss 里找不到 MyAppVersion")
    sys.exit(1)
version = m.group(1)
print("安装脚本版本 : %s" % version)

for r in READMES:
    if not r.exists():
        errors.append("%s 不存在" % r.name)
        continue
    text = r.read_text(encoding="utf-8")
    found = re.findall(r"EVE-Desktop-Setup-(\d+\.\d+\.\d+)", text)
    if not found:
        print("SKIP  %-14s 未提示例包名" % r.name)
        continue
    bad = sorted({v for v in found if v != version})
    if bad:
        errors.append("%s 里写着 %s，与 %s 不一致" % (r.name, "、".join(bad), version))
    else:
        print("OK    %-14s 示例包名 %s" % (r.name, "、".join(sorted(set(found)))))

if errors:
    print()
    for e in errors:
        print("FAIL ", e)
    sys.exit(1)
print("\n版本号一致")
