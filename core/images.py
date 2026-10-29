# -*- coding: utf-8 -*-
"""镜像库：扫描 / 导出 / 导入 / IOL 许可证修复（全部经 SSH 远端执行）。"""
import hashlib
import os
import posixpath
import re
import struct
import time

from .tasks import human

QEMU_ROOT = "/opt/unetlab/addons/qemu"
IOL_ROOT = "/opt/unetlab/addons/iol/bin"
DYN_ROOT = "/opt/unetlab/addons/dynamips"
DOCKER_ROOT = "/opt/unetlab/addons/docker"
TPL_ROOT = "/opt/unetlab/html/templates"
ICON_ROOT = "/opt/unetlab/html/images/icons"
TMP_ROOT = "/tmp/eve-desktop"

_PAD1 = b"\x4B\x58\x21\x81\x56\x7B\x0D\xF3\x21\x43\x9B\x7E\xAC\x1D\xE6\x8A"
_PAD2 = b"\x80" + 39 * b"\x00"

_SAFE = re.compile(r"[^A-Za-z0-9._+\-]")


def safe_name(s):
    return _SAFE.sub("_", (s or "").strip()) or "image"


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
SCAN_CMD = r"""
echo '=QEMU='
for d in {qemu}/*; do [ -d "$d" ] || continue;
  printf 'Q|%s|%s|%s\n' "$(basename "$d")" "$(du -sm "$d" 2>/dev/null | cut -f1)" "$(find "$d" -maxdepth 1 -type f | wc -l)"; done
echo '=IOL='
for f in {iol}/*.bin; do [ -f "$f" ] || continue;
  printf 'I|%s|%s\n' "$(basename "$f")" "$(( $(stat -c %s "$f") / 1048576 ))"; done
echo '=DYN='
for f in {dyn}/*; do [ -f "$f" ] || continue;
  printf 'D|%s|%s\n' "$(basename "$f")" "$(( $(stat -c %s "$f") / 1048576 ))"; done
echo '=TPL='
ls -1 {tpl}/*.yml 2>/dev/null | sed 's#.*/##'
echo '=ICON='
ls -1 {icons} 2>/dev/null
"""


def scan(remote, tpl_arch="intel"):
    """返回 {images:[...], templates:[...], icons:[...]}"""
    cmd = SCAN_CMD.format(qemu=QEMU_ROOT, iol=IOL_ROOT, dyn=DYN_ROOT,
                          tpl="%s/%s" % (TPL_ROOT, tpl_arch), icons=ICON_ROOT)
    _rc, out, _err = remote.run(cmd, timeout=120)
    sec = "?"
    images, templates, icons = [], [], []
    for line in out.splitlines():
        line = line.rstrip("\r")
        if not line:
            continue
        if line.startswith("=") and line.endswith("="):
            sec = line.strip("=")
            continue
        if sec == "QEMU":
            p = line.split("|")
            if len(p) >= 4:
                images.append({"type": "qemu", "name": p[1],
                               "path": posixpath.join(QEMU_ROOT, p[1]),
                               "size_mb": _int(p[2]), "files": _int(p[3])})
        elif sec == "IOL":
            p = line.split("|")
            if len(p) >= 3:
                images.append({"type": "iol", "name": p[1],
                               "path": posixpath.join(IOL_ROOT, p[1]),
                               "size_mb": _int(p[2]), "files": 1})
        elif sec == "DYN":
            p = line.split("|")
            if len(p) >= 3:
                images.append({"type": "dynamips", "name": p[1],
                               "path": posixpath.join(DYN_ROOT, p[1]),
                               "size_mb": _int(p[2]), "files": 1})
        elif sec == "TPL":
            if line.endswith((".yml", ".yaml")):
                templates.append(posixpath.basename(line)[: -4] if line.endswith(".yml")
                                 else posixpath.basename(line)[: -5])
        elif sec == "ICON":
            icons.append(line)
    tset = set(templates)
    for im in images:
        tpl = pick_template(im["name"], im["type"], tset)
        im["template"] = tpl
        im["has_template"] = bool(tpl)
        im["icon"] = pick_icon(im["name"], im["type"], icons)
        im["size_h"] = ("%d MB" % im["size_mb"]) if im["size_mb"] < 1024 \
            else ("%.1f GB" % (im["size_mb"] / 1024.0))
    images.sort(key=lambda x: (x["type"], x["name"].lower()))
    return {"images": images, "templates": sorted(templates), "icons": icons,
            "tpl_arch": tpl_arch}


def match_template(dir_name, templates):
    """EVE 规则：目录名以模板名开头，取最长匹配"""
    best, bl = None, 0
    for stem in templates:
        if dir_name.startswith(stem) and len(stem) > bl:
            best, bl = stem, len(stem)
    return best


def pick_template(name, kind, templates):
    """按镜像类型选模板。

    QEMU 目录名靠前缀匹配（EVE 的规则）；
    IOL / dynamips 的文件名（i86bi_xxx.bin、c7200-xxx.bin）对不上模板名，
    必须按类型回落到固定模板，否则导出包里会缺 yml。
    """
    tset = set(templates or [])
    tpl = match_template(name, tset)
    if tpl:
        return tpl
    if kind == "iol":
        for cand in ("iol", "iol2", "iol-l2"):
            if cand in tset:
                return cand
    if kind == "dynamips":
        for cand in ("c7200", "c3600", "c3725", "c2691"):
            if cand in tset:
                return cand
    return None


def icon_for(dir_name, icons):
    best, bl = None, 0
    for f in icons:
        stem, ext = posixpath.splitext(f)
        if ext.lower() not in (".png", ".jpg", ".jpeg", ".svg", ".gif"):
            continue
        if dir_name.startswith(stem) and len(stem) > bl:
            best, bl = f, len(stem)
    return best


def pick_icon(name, kind, icons):
    ic = icon_for(name, icons)
    if ic:
        return ic
    pool = set(icons or [])
    if kind == "iol":
        chain = ("iol.png", "iol.svg", "Router.png", "Switch.png")
    elif kind == "dynamips":
        chain = ("Router.png", "Router-2D-Gen-Grey-S.svg", "Router.svg")
    else:
        chain = ("Router.png", "linux.png", "Router-2D-Gen-Grey-S.svg")
    for cand in chain:
        if cand in pool:
            return cand
    return None


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------
def export_image(remote, src_path, dir_name, is_dir, local_file, t, tpl_arch="intel",
                 icons=None):
    """在远端打标准包（目录 + <名>.yml + <名>.png），下载到本地。"""
    stage_dir = "%s/exp-%s" % (TMP_ROOT, t.id)
    tgz = "%s.tgz" % stage_dir
    remote.run("rm -rf %s && mkdir -p %s" % (stage_dir, stage_dir), timeout=30)

    t.update(stage="准备文件（硬链接，不占额外空间）", percent=2)
    if is_dir:
        remote.run("cp -al '{s}' '{d}/{n}' 2>/dev/null || cp -a '{s}' '{d}/{n}'".format(
            s=src_path, d=stage_dir, n=dir_name), timeout=600)
    else:
        remote.run("cp -l '{s}' '{d}/{n}' 2>/dev/null || cp '{s}' '{d}/{n}'".format(
            s=src_path, d=stage_dir, n=dir_name), timeout=600)

    entries = [dir_name]

    # 模板 yml
    name_only = dir_name[:-4] if dir_name.lower().endswith(".bin") else dir_name
    kind = "qemu" if is_dir else ("iol" if dir_name.lower().endswith(".bin")
                                  else "dynamips")
    tpl = pick_template(name_only, kind, _tpl_list(remote, tpl_arch))
    if tpl:
        yml = "%s/%s/%s.yml" % (TPL_ROOT, tpl_arch, tpl)
        if remote.ok("test -f '%s'" % yml):
            remote.run("cp -f '%s' '%s/%s.yml'" % (yml, stage_dir, name_only), timeout=30)
            entries.append("%s.yml" % name_only)
            t.update(stage="已打包模板 %s.yml" % name_only)

    # 图标（导出包只认 png，svg 直接跳过，避免改后缀后图片打不开）
    icons = icons if icons is not None else _icon_list(remote)
    pngs = [x for x in icons if x.lower().endswith(".png")]
    ic = pick_icon(name_only, kind, pngs)
    if ic:
        remote.run("cp -f '%s/%s' '%s/%s.png'" % (ICON_ROOT, ic, stage_dir, name_only),
                   timeout=30)
        entries.append("%s.png" % name_only)

    # 打包（同时轮询大小，给出真实进度感）
    log = "%s/tar.log" % stage_dir
    cmd = "cd %s && tar -czf %s %s >%s 2>&1" % (
        stage_dir, tgz, " ".join("'%s'" % e for e in entries), log)
    t.update(stage="打包中（gzip）", percent=5)
    _in, out, _err = remote.client.exec_command(cmd, timeout=7200)
    while not out.channel.exit_status_ready():
        try:
            sz = _int(remote.out("stat -c %%s '%s' 2>/dev/null" % tgz))
        except Exception:
            sz = 0
        if sz:
            t.update(stage="打包中 · 已生成 %s" % human(sz))
        time.sleep(1.5)
    rc = out.channel.recv_exit_status()
    if rc != 0:
        detail = remote.out("tail -c 400 '%s'" % log)
        raise RuntimeError("远端打包失败（rc=%s）%s" % (rc, detail.strip()))

    total = _int(remote.out("stat -c %%s '%s'" % tgz))
    t.update(stage="下载中", percent=8, total=total)
    os.makedirs(os.path.dirname(local_file) or ".", exist_ok=True)
    tmp_local = local_file + ".part"

    def _dl(sent, tt, sp):
        t.progress(sent, tt, sp)
        if tt:
            t.update(percent=round(8 + sent / tt * 90.0, 1))

    remote.download(tgz, tmp_local, progress=_dl)
    os.replace(tmp_local, local_file)
    t.update(stage="清理远端临时文件")
    remote.run("rm -rf '%s' '%s'" % (stage_dir, tgz), timeout=120)
    t.update(file=local_file, percent=100, stage="完成", message=os.path.basename(local_file))
    return local_file


def _tpl_list(remote, arch):
    out = remote.out("ls -1 %s/%s/*.yml 2>/dev/null" % (TPL_ROOT, arch))
    return [posixpath.basename(x)[:-4] for x in out.splitlines() if x.strip()]


def _icon_list(remote):
    return [x for x in remote.out("ls -1 %s 2>/dev/null" % ICON_ROOT).splitlines() if x.strip()]


def _dir_size(remote, path):
    v = remote.out("du -sb '%s' 2>/dev/null | cut -f1" % path)
    return _int(v)


# --------------------------------------------------------------------------
# 导入
# --------------------------------------------------------------------------
def import_image(remote, local_file, t, tpl_arch="intel"):
    """上传 → 解包 → 落位 → 修权限 → 同步模板/图标"""
    # 本地路径是 Windows 路径，必须用 os.path；posixpath 拆不出反斜杠文件名
    base = os.path.basename(local_file.replace("\\", "/"))
    ext = _ext(base)
    up = "%s/up-%s%s" % (TMP_ROOT, t.id, ext)
    work = "%s/x-%s" % (TMP_ROOT, t.id)
    remote.run("mkdir -p %s" % TMP_ROOT, timeout=20)

    t.update(stage="上传", percent=1)
    remote.upload(local_file, up,
                  progress=lambda s, tt, sp: t.progress(s, tt, sp))

    t.update(stage="解包", percent=55)
    remote.run("rm -rf '%s' && mkdir -p '%s'" % (work, work), timeout=30)
    if ext in (".tgz", ".tar.gz", ".tar"):
        rc, _o, e = remote.run("cd '%s' && tar -xf '%s'" % (work, up), timeout=3600)
    elif ext == ".zip":
        rc, _o, e = remote.run(
            "cd '%s' && (unzip -oq '%s' || python3 -c \"import zipfile,sys;"
            "zipfile.ZipFile(sys.argv[1]).extractall('.')\" '%s')" % (work, up, up),
            timeout=3600)
    else:
        rc, _o, e = 1, "", "不支持的文件类型：%s" % ext
    if rc != 0:
        raise RuntimeError("解包失败：%s" % (e or "").strip()[:300])

    # 认目录名
    dir_name, inner = _detect_root(remote, work, base)
    kind = _kind_of(dir_name)

    t.update(stage="落位到 %s" % ("iol/bin" if kind == "iol" else "qemu"), percent=70)
    if kind == "iol":
        bins = remote.out("cd '%s/%s' && ls -1 *.bin 2>/dev/null" % (work, inner)).split()
        if not bins:
            bins = remote.out("cd '%s' && ls -1 *.bin 2>/dev/null" % work).split()
        if not bins:
            raise RuntimeError("包里没找到 .bin 镜像")
        for b in bins:
            srcp = "%s/%s/%s" % (work, inner, b) if remote.exists("%s/%s/%s" % (work, inner, b)) \
                else "%s/%s" % (work, b)
            remote.run("cp -f '%s' '%s/' && chmod 755 '%s/%s'" % (
                srcp, IOL_ROOT, IOL_ROOT, b), timeout=600)
        result = {"type": "iol", "names": bins}
    elif kind == "dynamips":
        files = remote.out("cd '%s/%s' && ls -1" % (work, inner)).split()
        for b in files:
            remote.run("cp -f '%s/%s/%s' '%s/' && chmod 755 '%s/%s'" % (
                work, inner, b, DYN_ROOT, DYN_ROOT, b), timeout=600)
        result = {"type": "dynamips", "names": files}
    else:
        dst = "%s/%s" % (QEMU_ROOT, dir_name)
        remote.run("mkdir -p '%s'" % dst, timeout=30)
        # 排除顶层 yml / 图片，只把镜像本体搬进去
        remote.run(
            "cd '%s/%s' && find . -maxdepth 1 -type f ! -name '*.yml' ! -name '*.yaml' "
            "! -name '*.png' ! -name '*.jpg' ! -name '*.svg' ! -name 'eim-export.json' "
            "-exec cp -a -t '%s/' {} + ; "
            "find . -mindepth 1 -maxdepth 1 -type d -exec cp -a -t '%s/' {} +" % (
                work, inner, dst, dst), timeout=3600)
        result = {"type": "qemu", "names": [dir_name]}

    # 模板 / 图标
    #
    # QEMU 镜像的惯例是「一个镜像一套模板/图标」(<dir>.yml / <dir>.png)，
    # 必须写进 templates/ 和 icons/；而 IOL / dynamips 用的是 EVE 内置模板
    # (iol.yml / c7200.yml)，导出包里那份 yml 只是内置模板的副本。
    # 若照文件名写回去，会凭空多出 "i86bi_xxx.yml" 这种伪模板，
    # 让后续匹配把镜像名当成模板名（EVE 模板列表也会被污染）。
    t.update(stage="同步模板与图标", percent=85)
    if kind == "qemu":
        for f in remote.out("cd '%s' && ls -1" % work).split():
            low = f.lower()
            if low.endswith((".yml", ".yaml")):
                target = "%s.yml" % result["names"][0]
                remote.run("cp -f '%s/%s' '%s/%s'" % (work, f, TPL_ROOT + "/" + tpl_arch,
                                                     target), timeout=30)
                result["template"] = target[:-4]
            elif low.endswith((".png", ".jpg", ".svg")):
                dest = result["names"][0] + posixpath.splitext(f)[1]
                remote.run("cp -f '%s/%s' '%s/%s'" % (work, f, ICON_ROOT, dest), timeout=30)

    # 权限
    t.update(stage="修正权限", percent=92)
    remote.run("chown -R root:root '%s' '%s' 2>/dev/null; "
               "/opt/unetlab/wrappers/unl_wrapper -a fixpermissions >/dev/null 2>&1 || true"
               % (QEMU_ROOT, IOL_ROOT), timeout=600)
    if result["type"] == "iol":
        iol_fix(remote)
    remote.run("rm -rf '%s' '%s'" % (work, up), timeout=300)
    t.update(stage="完成", percent=100, message="已导入 %s" % ", ".join(result["names"]))
    return result


def _ext(name):
    low = name.lower()
    for e in (".tar.gz", ".tgz", ".tar", ".zip"):
        if low.endswith(e):
            return e
    return posixpath.splitext(low)[1]


def _detect_root(remote, work, base):
    """返回 (镜像名, 包内相对路径)。

    以**包内容**为准，不拿压缩包文件名当镜像名：
    包名可能是别人随手改的，甚至是 Windows 全路径（`posixpath.basename`
    对反斜杠路径无效），据此命名会把 `C__Users_a_AppData_...` 这种脏名字
    写进 templates/ 和 icons/ 目录。
    """
    def _meta(f):
        return f.lower().endswith((".yml", ".yaml", ".png", ".jpg", ".jpeg",
                                   ".svg", ".json", ".md", ".txt"))

    def _isdir(p):
        return remote.ok("test -d '%s/%s'" % (work, p))

    entries = [x for x in remote.out("cd '%s' && ls -1A" % work).splitlines() if x.strip()]
    dirs = [e for e in entries if _isdir(e)]
    bins = [e for e in entries if e.lower().endswith(".bin")]

    # 1) 只有一个目录：EVE 标准导出包（<dir>/ + <dir>.yml + <dir>.png）
    if len(dirs) == 1:
        only = dirs[0]
        sub = [x for x in remote.out("cd '%s/%s' && ls -1A" % (work, only)).splitlines()
               if x.strip()]
        real = [x for x in sub if not _meta(x)]
        # 再套一层且只有这一个子目录，且外层没有其它实体文件
        if len(real) == 1 and _isdir(posixpath.join(only, real[0])) \
                and not [e for e in entries if e not in (only,) and not _meta(e)]:
            return real[0], posixpath.join(only, real[0])
        return only, only

    # 2) 顶层就是单个 .bin：IOL / dynamips 单文件镜像
    if len(bins) == 1 and not dirs:
        return bins[0], "."

    # 3) 多个目录：整包当一个镜像集合，用包名（清理后）命名
    stem = base
    for e in (".tar.gz", ".tgz", ".tar", ".zip"):
        if stem.lower().endswith(e):
            stem = stem[: -len(e)]
            break
    stem = safe_name(stem)
    stage = "%s/%s" % (work, stem)
    remote.run("mkdir -p '%s' && cd '%s' && for f in *; do "
               "case \"$f\" in *.yml|*.yaml|*.png|*.jpg|*.jpeg|*.svg|eim-export.json) ;; "
               "*) [ \"$f\" = '%s' ] || mv \"$f\" '%s/' ;; esac; done"
               % (stage, work, stem, stage), timeout=600)
    return stem, stem


def _kind_of(name):
    low = (name or "").lower()
    if low.endswith(".bin"):
        return "iol"
    if low.endswith((".image", ".bin")) or "c7200" in low or "c3725" in low:
        return "dynamips"
    return "qemu"


# --------------------------------------------------------------------------
# IOL 许可证
# --------------------------------------------------------------------------
def iol_key(hostid, hostname):
    ioukey = int(hostid or "007f0101", 16)
    for ch in hostname:
        ioukey += ord(ch)
    raw = _PAD1 + _PAD2 + struct.pack("!L", ioukey & 0xFFFFFFFF) + _PAD1
    return hashlib.md5(raw).hexdigest()[:16]


def iol_status(remote):
    ident = remote.identity()
    key = iol_key(ident["hostid"], ident["hostname"])
    st = {"hostid": ident["hostid"], "hostname": ident["hostname"], "key": key,
          "iourc": False, "bins": 0, "nodes": 0, "missing": 0}
    txt = remote.out("cat %s/iourc 2>/dev/null" % IOL_ROOT)
    st["iourc"] = bool(txt) and key in txt
    st["bins"] = _int(remote.out("ls -1 %s/*.bin 2>/dev/null | wc -l" % IOL_ROOT))
    nodes = _iol_nodes(remote)
    st["nodes"] = len(nodes)
    st["missing"] = sum(1 for d in nodes if not remote.exists(d + "/iourc"))
    st["ok"] = st["iourc"] and st["missing"] == 0
    return st


def _iol_nodes(remote):
    """已 prepare 过的节点目录。

    注意：EVE 的 `.prepared` 是 `touch` 出来的**空文件**（不是目录），
    只能用 -e / -type f 判断；用 test -d 会永远匹配不到。
    """
    cmd = ("for f in $(find /opt/unetlab/tmp -maxdepth 5 -name .prepared -type f "
           "2>/dev/null); do dirname \"$f\"; done")
    out = remote.out(cmd, timeout=40)
    return [x.strip() for x in out.splitlines() if x.strip().startswith("/")]


def iol_fix(remote):
    """写 iourc，并给已 prepare 的节点目录补软链"""
    ident = remote.identity()
    key = iol_key(ident["hostid"], ident["hostname"])
    text = "[license]\n%s = %s;\n" % (ident["hostname"], key)
    remote.run("mkdir -p %s" % IOL_ROOT, timeout=20)
    remote.write_text(IOL_ROOT + "/iourc", text, mode=0o755)
    nodes = _iol_nodes(remote)
    created = remote.out(
        "for f in $(find /opt/unetlab/tmp -maxdepth 5 -name .prepared -type f 2>/dev/null); do "
        "d=$(dirname \"$f\"); [ -e \"$d/iourc\" ] || { ln -sf %s/iourc \"$d/iourc\" && echo \"$d\"; }; "
        "done" % IOL_ROOT, timeout=90)
    linked = len([x for x in created.splitlines() if x.strip()])
    return {"key": key, "hostname": ident["hostname"], "text": text.strip(),
            "nodes": len(nodes), "linked": linked}
