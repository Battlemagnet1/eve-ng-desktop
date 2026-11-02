# -*- coding: utf-8 -*-
"""实验（拓扑 + 节点配置）打包导出 / 导入。

## 为什么导出 .unl 就等于导出了配置

EVE 的节点配置**本来就在 .unl 里**，不是单独的文件。依据 EVE 自身源码：

    __lab.php:1338   $xml->objects->configs->addChild('config', base64_encode($config_data));
    __lab.php:212    $result = $xml->xpath('//lab/objects/configs/config[@id="N"]');
    cli.php:843      if ($n->getConfig()=='1' && $n->getConfigData()!='')
                         dumpConfig($n->getConfigData(), $n->getRunningPath().'/startup-config');

即：
    <lab>
      <topology><nodes>…</nodes><networks>…</networks></topology>
      <objects><configs>
        <config id="1">BASE64 编码的设备配置</config>
      </configs></objects>
    </lab>

节点属性 `config="1"` 表示「有保存的配置」，启动时 EVE 会把 `getConfigData()`
写到运行目录的 `startup-config`。所以**原样搬运 .unl，拓扑与节点配置就都完整了**。

## 为什么绝对不要预先往运行目录写配置

`cli.php::prepareNode()` 里有一句：

    if (is_dir($n->getRunningPath())) !touch($n->getRunningPath().'/.prepared');
    if (!is_file('.prepared') && !is_file('.lock')) { …真正的 prepare… }

`!touch()` 在 PHP 里仍会执行 touch()，于是**只要运行目录存在，EVE 就直接认定
"已 prepare"并跳过全部准备工作**（IOL 缺 jail、qemu 缺 linked clone），节点起不来
（实测 `Failed to start node (12)` / status 恒为 0）。所以本模块只写 .unl，
运行目录一律交给 EVE 自己创建。

## 包里装什么

    <实验名>-<时间戳>.zip
    ├── manifest.json    版本、来源、节点清单、校验信息
    ├── lab.unl          EVE 实验文件（拓扑 + 内嵌配置，原样）
    ├── textboxes.json   拓扑文本框（本机侧车数据，不导出就丢了）
    └── configs/         仅当有「没存进 .unl 的运行目录残留配置」时才出现
        └── <id>-<name>.cfg
"""
import base64
import json
import os
import re
import time
import zipfile
from urllib.parse import quote as _q
from xml.sax.saxutils import escape

FORMAT = "eve-desktop-labpack"
VERSION = 1

LABS_DIR = "/opt/unetlab/labs"
TMP_DIR = "/opt/unetlab/tmp"


class PackError(ValueError):
    """导出/导入过程中的可读错误（直接显示给用户）。

    继承 ValueError 是刻意的：server.py 的 do_POST/do_GET 会把 ValueError
    映射成 HTTP 400 + 原样 message，不用再改异常分派。
    """


# ---------------------------------------------------------------- 工具
def _clean_path(path):
    """把 `1.unl` / `/1.unl` / `/Users/x/1.unl` 统一成 `Users/x/1.unl`。"""
    p = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not p:
        raise PackError("缺少实验路径")
    if not p.lower().endswith(".unl"):
        raise PackError("不是实验文件：%s" % p)
    return p


def _safe_name(name, default="lab"):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", str(name or "")).strip(" .-")
    return (s or default)[:60]


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _lab_dir(path):
    d = os.path.dirname(_clean_path(path))
    return d


def ensure_ssh(app):
    """导出/导入都要读写服务器文件，必须有 SSH。"""
    remote = getattr(app, "remote", None)
    if remote is None:
        raise PackError("这个功能需要 SSH：请在连接设置里填 SSH 账号（EVE 默认 root/eve）")
    return remote


# ---------------------------------------------------------------- 解析
def parse_lab(text):
    """从 .unl 文本里提取节点/网络清单（只读，不改写原文）。"""
    nodes, networks = [], []
    # 节点：<node …/> 或 <node …>…</node>
    for m in re.finditer(r"<node\b([^>]*?)(/?)>", text):
        attrs = m.group(1)
        d = dict(re.findall(r'(\w+)="([^"]*)"', attrs))
        if "id" not in d:
            continue
        nodes.append({
            "id": d.get("id"),
            "name": d.get("name") or ("node%s" % d.get("id")),
            "template": d.get("template") or "",
            "type": d.get("type") or "",
            "image": d.get("image") or "",
            "ram": d.get("ram") or "",
            "ethernet": d.get("ethernet") or "",
            "config": d.get("config") or "0",
        })
    for m in re.finditer(r"<network\b([^>]*?)/?>", text):
        d = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if "id" in d:
            networks.append({
                "id": d.get("id"),
                "name": d.get("name") or "",
                "type": d.get("type") or "",
                "count": d.get("count") or "",
            })
    return nodes, networks


def embedded_configs(text):
    """返回 {节点 id: 已内嵌在 .unl 里的配置原文}（<config id="N">base64</config>）。"""
    out = {}
    for m in re.finditer(r'<config\s+id="(\d+)"\s*>([^<]*)</config>', text):
        nid, b64 = m.group(1), (m.group(2) or "").strip()
        if not b64:
            continue
        try:
            out[nid] = base64.b64decode(b64).decode("utf-8", "replace")
        except Exception:
            pass
    return out


def inject_config(text, node_id, cfg):
    """把一份配置注入 .unl：写进 <objects><configs>，并把节点标记成 config="1"。

    结构必须与 EVE 自己保存时一致（base64 + <config id>），否则 EVE 读不到。
    """
    b64 = base64.b64encode(str(cfg).encode("utf-8")).decode("ascii")
    entry = '<config id="%s">%s</config>' % (node_id, b64)

    # 已经有同 id 的配置就替换，别叠加
    exist = re.search(r'<config\s+id="%s"\s*>[^<]*</config>' % node_id, text)
    if exist:
        text = text[:exist.start()] + entry + text[exist.end():]
    elif "<objects>" in text:
        if "<configs>" in text:
            text = text.replace("<configs>", "<configs>" + entry, 1)
        else:
            text = text.replace("<objects>", "<objects><configs>" + entry + "</configs>", 1)
    else:
        text = text.replace("</lab>", "<objects><configs>" + entry + "</configs></objects></lab>", 1)

    # 节点标 config="1"（有则改，没有就补）
    m = re.search(r'(<node id="%s"[^>]*?)config="[^"]*"' % node_id, text)
    if m:
        text = text[:m.start()] + m.group(1) + 'config="1"' + text[m.end():]
    else:
        m2 = re.search(r'<node id="%s"' % node_id, text)
        if m2:
            text = text[:m2.end()] + ' config="1"' + text[m2.end():]
    return text


def config_source(text, node_id):
    """返回节点当前生效的配置（来自 .unl 内嵌）；没有则空串。"""
    return embedded_configs(text).get(str(node_id), "")


def enable_configs(text):
    """把所有「有 <config> 数据」的节点的 config 属性置为 "1"。

    这是 EVE 真正决定「要不要用这份配置」的开关（cli.php:843）：

        if ($n->getConfig()=='1' && $n->getConfigData()!='') dumpConfig(…)

    数据写进去了、属性还是 "0"，配置就永远不会变成 startup-config ——
    「导入后配置没了」绝大多数都是这一条。节点上原本没有 config 属性的也补上。
    """
    out = text
    for nid in embedded_configs(text):
        if _config_enabled(out, nid):
            continue
        m = re.search(r'(<node\b[^>]*?\bid="%s"[^>]*?)\sconfig="([^"]*)"' % nid, out)
        if m:
            out = out[:m.start()] + m.group(1) + ' config="1"' + out[m.end():]
            continue
        m2 = re.search(r'(<node\b[^>]*?\bid="%s")' % nid, out)
        if m2:
            out = out[:m2.end()] + ' config="1"' + out[m2.end():]
    return out


def _config_enabled(text, nid):
    """节点是否既有 <config> 数据、又把 config 属性标成了 "1"。"""
    if str(nid) not in embedded_configs(text):
        return False
    m = re.search(r'<node\b[^>]*?\bid="%s"[^>]*?>' % nid, text)
    if not m:
        return False
    return ('config="1"' in m.group(0))


# ---------------------------------------------------------------- 从设备抓配置
# Pro 7.x 起节点时给控制台分配**随机端口**，API 也不返回，只能从 wrapper 进程
# 的 `-C <端口>` 参数里读（工作目录正好是 tmp/<tenant>/<uuid>/<nid>）。
PORT_CMD = (
    "ps -eo pid,args | grep -E '(qemu|iol|dynamips|docker)_wrapper' | grep -v grep | "
    "while read -r pid rest; do cwd=$(readlink /proc/$pid/cwd 2>/dev/null) || continue; "
    "echo \"$cwd|$rest\"; done")


def _node_port(app, eve, path, nid):
    """节点控制台端口：CE 用公式，Pro 现查 wrapper（查不到退回公式）。"""
    fallback = eve.console_port(nid)
    remote = getattr(app, "remote", None)
    if not (app.info.get("caps") or {}).get("pro") or remote is None:
        return fallback
    try:
        uuid = str((eve.lab_meta(path) or {}).get("id") or "")
        if not uuid:
            return fallback
        _rc, out, _err = remote.run(PORT_CMD, timeout=20)
        tail = "/" + str(nid)
        for line in (out or "").splitlines():
            cwd, _sep, args = line.partition("|")
            if uuid not in cwd or not cwd.rstrip("/").endswith(tail):
                continue
            m = re.search(r"-C\s+(\d+)", args)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return fallback


def _config_script(remote, template):
    """模板对应的 config_script 名。

    EVE 只给「能用脚本灌配置」的模板填这个字段（模板 yml 里的 config_script）。
    没填的就是 linux / windows 这类，跳过。
    CE 6.x 的模板平铺在 `templates/`，Pro 7.x 按架构分在 `templates/intel/`
    与 `templates/amd/` 下，两侧都要试。
    """
    for base in ("templates", "templates/intel", "templates/amd"):
        try:
            _rc, out, _err = remote.run(
                "grep -h '^config_script:' '/opt/unetlab/html/%s/%s.yml' 2>/dev/null"
                % (base, template), timeout=15)
        except Exception:
            continue
        for line in (out or "").splitlines():
            v = line.split(":", 1)[-1].strip().strip("'\"")
            # `embedded` / `null` 不是可执行脚本（iol 走 NVRAM 那条路）
            if v and v not in ("null", "embedded"):
                return v
    return ""


def device_config(app, eve, path, node, uuid, tenant):
    """从**运行中的设备**抓它当前生效的配置，返回 (配置文本, 错误说明)。

    为什么需要这一步：用户在设备里敲了配置、却没在 EVE 里点「保存配置」时，
    `.unl` 里没有、运行目录里也没有，配置只在设备自己的 NVRAM 里。EVE 界面上的
    「导出配置」走的就是这条流程（cli.php 的 'iol' 分支）：

        wrconf_iol.py -p <port> -t 15      让设备把 running-config 写进 NVRAM
        iou_export <nvram> <tmp>           再从 NVRAM 里抽出配置文本

    qemu 类（vios / csr / nxos 等）走 `config_scripts/<脚本> -a get -p <port>`。
    linux / windows / docker 没有 config_script，直接跳过（不是配置文件驱动的）。
    """
    remote = getattr(app, "remote", None)
    if remote is None:
        return "", "没有 SSH"
    nid = str(node["id"])
    typ = (node.get("type") or "").lower()
    tpl = node.get("template") or ""
    if int(node.get("status") or 0) < 2:
        return "", "节点没在运行"
    port = _node_port(app, eve, path, nid)
    tmp = "/tmp/evegui-cfg-%s-%s.cfg" % ((uuid or "x")[:8], nid)
    rundir = "%s/%d/%s/%s" % (TMP_DIR, tenant, uuid, nid)

    try:
        if typ == "iol":
            # 目录名里有 uuid，用引号包住防特殊字符
            remote.run("cd '%s' 2>/dev/null && /opt/unetlab/scripts/wrconf_iol.py "
                       "-p %d -t 15 >/dev/null 2>&1; true" % (rundir, port), timeout=40)
            _rc, out, _err = remote.run(
                "ls '%s'/nvram_* 2>/dev/null | head -1" % rundir, timeout=15)
            nvram = (out or "").strip()
            if not nvram:
                return "", "找不到 NVRAM 文件"
            remote.run("/opt/unetlab/scripts/iou_export '%s' '%s' >/dev/null 2>&1; true"
                       % (nvram, tmp), timeout=60)
        else:
            script = _config_script(remote, tpl)
            if not script:
                return "", "这个模板不支持抓配置"
            remote.run("/opt/unetlab/config_scripts/%s -a get -p %d -f '%s' -t 20 "
                       ">/dev/null 2>&1; true" % (script, port, tmp), timeout=60)

        if not remote.exists(tmp):
            return "", "没抓到内容"
        cfg = remote.read_text(tmp, limit=8 << 20)
        remote.run("rm -f '%s'" % tmp, timeout=15)
        return (cfg or ""), ""
    except Exception as e:
        try:
            remote.run("rm -f '%s'" % tmp, timeout=10)
        except Exception:
            pass
        return "", str(e)


def collect_configs(app, eve, path, nodes, uuid, tenant, text="", progress=None):
    """按「用户眼里最真实」的顺序收集每个节点的配置。

        1. 节点在运行 → 直接从设备抓 running-config
           （用户在设备里敲了配置却没在 EVE 里点保存时，.unl 里是旧的，
             只有设备上才是最新的；EVE 界面上的「导出配置」也是这么取）
        2. 没运行 / 抓不到 → `.unl` 内嵌的 <config>（EVE 保存过的）
        3. 再不行 → 运行目录的 startup-config

    返回 ({nid: (配置文本, 来源)}, [警告])。
    """
    remote = getattr(app, "remote", None)
    out, warnings = {}, []

    # 1. 设备现抓
    running = [nd for nd in nodes if int(nd.get("status") or 0) >= 2]
    for nd in running:
        nid = str(nd["id"])
        nm = nd.get("name") or nid
        if progress:
            try:
                progress("从设备读配置：%s" % nm)
            except Exception:
                pass
        cfg, err = device_config(app, eve, path, nd, uuid, tenant)
        if (cfg or "").strip():
            out[nid] = (cfg, "device")
        elif err and err not in ("节点没在运行",):
            warnings.append("节点 %s 的配置没读出来（%s），改用存档里的" % (nm, err))

    # 2. .unl 内嵌
    if progress:
        try:
            progress("核对存档配置")
        except Exception:
            pass
    for nid, cfg in embedded_configs(text).items():
        if str(nid) not in out and (cfg or "").strip():
            out[str(nid)] = (cfg, "unl")

    # 3. 运行目录残留
    if remote and uuid:
        for nd in nodes:
            nid = str(nd["id"])
            if nid in out:
                continue
            p = "%s/%d/%s/%s/startup-config" % (TMP_DIR, tenant, uuid, nid)
            try:
                if remote.exists(p):
                    c = remote.read_text(p, limit=4 << 20)
                    if (c or "").strip():
                        out[nid] = (c, "startup-config")
            except Exception as e:
                warnings.append("节点 %s 的运行配置读不到：%s" % (nd.get("name") or nid, e))
    return out, warnings


# ---------------------------------------------------------------- 导出
def export_lab(app, path, out_dir=None, progress=None):
    """把实验打包成 zip。返回 {file, size, nodes, configs, warnings}。"""
    remote = ensure_ssh(app)
    eve = app.require()
    path = _clean_path(path)

    def step(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    step("读取实验文件")
    lab_file = "%s/%s" % (LABS_DIR, path)
    if not remote.exists(lab_file):
        raise PackError("服务器上找不到这个实验：%s" % path)
    text = remote.read_text(lab_file, limit=32 << 20)
    if not text or "<lab" not in text:
        raise PackError("实验文件内容异常（不是有效的 .unl）")

    nodes, networks = parse_lab(text)
    if not nodes:
        raise PackError("这个实验里没有节点")

    meta = {}
    try:
        meta = eve.lab_meta("/" + path) or {}
    except Exception:
        pass
    uuid = str(meta.get("id") or "")
    tenant = (app.info.get("caps") or {}).get("tenant") or 0

    # 节点的运行状态（.unl 里没有 status，得从 API 拿），后面判断
    # 「能不能从设备现抓配置」要用
    try:
        live = eve.lab_nodes("/" + path) or {}
        for nd in nodes:
            st = (live.get(str(nd["id"])) or live.get(nd["id"]) or {}).get("status")
            nd["status"] = int(st or 0)
    except Exception:
        for nd in nodes:
            nd["status"] = 0

    # 配置的三个来源（.unl 内嵌 / 运行目录 / 设备现抓），按权威度取
    found, warnings = collect_configs(app, eve, path, nodes, uuid, tenant,
                                      text=text, progress=progress)
    inline = {k: v[0] for k, v in found.items() if v[1] == "unl"}
    extra = {k: v[0] for k, v in found.items() if v[1] != "unl"}

    # 文本框（本机侧车）
    notes = []
    try:
        notes = app.notes.list(app.info.get("host"), "/" + path) or []
    except Exception:
        pass

    step("打包")
    node_map = {str(n["id"]): n for n in nodes}
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": {
            "host": app.info.get("host"),
            "version": app.info.get("version"),
            "lab": "/" + path,
            "lab_id": uuid,
            "tenant": tenant,
        },
        "lab": {
            "name": meta.get("name") or os.path.basename(path)[:-4],
            "file": os.path.basename(path),
            "description": meta.get("description") or "",
        },
        "counts": {
            "nodes": len(nodes),
            "networks": len(networks),
            "textboxes": len(notes),
            "configs_inline": len(inline),
            "configs_extra": len(extra),
            "configs": len(found),
            "configs_from_device": sum(1 for v in found.values() if v[1] == "device"),
        },
        "nodes": [dict(n, has_config=("1" if str(n["id"]) in found else n["config"]),
                       config_source=(found.get(str(n["id"])) or ("", ""))[1])
                  for n in nodes],
        "networks": networks,
    }

    out_dir = os.path.abspath((out_dir or "").strip().strip('"')
                              or _default_export_dir(app))
    # 用户自己选的目录可能不存在（手输）或没权限（C:\ 根、别人的目录），
    # 这两种都要说人话，不能让异常冒到前端变成一串英文堆栈
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        raise PackError("导出目录建不出来：%s（%s）" % (out_dir, e))
    if not os.path.isdir(out_dir):
        raise PackError("导出目录不是一个文件夹：%s" % out_dir)

    base = _safe_name(manifest["lab"]["name"] or os.path.basename(path)[:-4])
    out = os.path.join(out_dir, "%s-%s.zip" % (base, _stamp()))
    n = 1
    while os.path.exists(out):
        out = os.path.join(out_dir, "%s-%s-%d.zip" % (base, _stamp(), n))
        n += 1

    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            z.writestr("lab.unl", text)
            z.writestr("textboxes.json", json.dumps(notes, ensure_ascii=False, indent=1))
            # 每个有配置的节点都单独存一份：导入时逐台写回 EVE 的配置接口，
            # 比「只依赖 .unl 内嵌」可靠（内嵌那套还要求节点的 config 标志为 1）
            for nid, (cfg, _src) in found.items():
                nm = _safe_name((node_map.get(str(nid)) or {}).get("name") or nid, "node")
                z.writestr("configs/%s-%s.cfg" % (nid, nm), cfg)
    except OSError as e:
        try:
            os.remove(out)
        except OSError:
            pass
        raise PackError("写不进这个目录：%s（换一个位置，或换个有写权限的文件夹）" % out_dir)
    except Exception as e:
        try:
            os.remove(out)
        except OSError:
            pass
        raise PackError("打包失败：%s" % e)

    return {
        "file": out,
        "dir": out_dir,
        "size": os.path.getsize(out),
        "path": "/" + path,
        "name": manifest["lab"]["name"],
        "nodes": len(nodes),
        "networks": len(networks),
        "textboxes": len(notes),
        "configs": len(inline) + len(extra),
        "warnings": warnings,
    }


def _default_export_dir(app):
    try:
        d = app.cfg.ui().get("export_dir")
    except Exception:
        d = None
    if d and os.path.isdir(d):
        return d
    desk = os.path.join(os.path.expanduser("~"), "Desktop")
    return desk if os.path.isdir(desk) else os.path.expanduser("~")


# ---------------------------------------------------------------- 导入
def _unique_name(app, folder, name):
    """目标目录已有同名实验时，自动加 -1 / -2 …（不覆盖用户的实验）。"""
    eve = app.require()
    folder = (folder or "").strip("/")
    existing = set()
    try:
        data = eve.list_labs(folder) or {}
        for lb in data.get("labs") or []:
            f = lb.get("file") if isinstance(lb, dict) else None
            if f:
                existing.add(str(f).lower())
    except Exception:
        pass
    cand, i = name, 1
    while ("%s.unl" % cand).lower() in existing:
        cand = "%s-%d" % (name, i)
        i += 1
    return cand


def read_pack(zip_path):
    """读包并做基本校验，返回 (manifest, lab_text, notes, extra_cfgs)。"""
    if not zipfile.is_zipfile(zip_path):
        raise PackError("这个文件不是 zip 包（请选择本程序导出的 .zip）")
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        if "manifest.json" not in names:
            raise PackError("包里没有 manifest.json，不是本程序导出的拓扑包")
        if "lab.unl" not in names:
            raise PackError("包里没有 lab.unl")
        try:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        except Exception as e:
            raise PackError("manifest.json 读不动：%s" % e)
        if manifest.get("format") != FORMAT:
            raise PackError("不是本程序导出的包（format=%r）" % manifest.get("format"))
        ver = int(manifest.get("version") or 0)
        if ver > VERSION:
            raise PackError("包版本 %s 比程序支持的 %s 新，请升级程序" % (ver, VERSION))
        lab_text = z.read("lab.unl").decode("utf-8", "replace")
        notes = []
        if "textboxes.json" in names:
            try:
                notes = json.loads(z.read("textboxes.json").decode("utf-8")) or []
            except Exception:
                notes = []
        extra = {}
        for n in names:
            m = re.match(r"configs/(\d+)-.*\.cfg$", n)
            if m:
                extra[m.group(1)] = z.read(n).decode("utf-8", "replace")
    return manifest, lab_text, notes, extra


def import_lab(app, zip_path, folder="", name="", progress=None):
    """把包还原成一台新实验。返回 {path, nodes, configs, textboxes, warnings}。"""
    remote = ensure_ssh(app)
    app.require()

    def step(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    step("读取包")
    manifest, lab_text, notes, extra = read_pack(zip_path)

    nodes, networks = parse_lab(lab_text)
    if not nodes:
        raise PackError("包里的拓扑没有节点")

    # 把包里的配置全部写回 .unl。
    # 注意：包里的 configs/*.cfg 是导出时收集到的**完整**配置（不只是「.unl 里
    # 没存的那部分」），所以这里以包里的为准覆盖同名 id，别跳过 —— 否则导出
    # 之后用户又改过的配置就还原不回去了。
    warnings = []
    inline = embedded_configs(lab_text)
    merged = lab_text
    for nid, cfg in extra.items():
        merged = inject_config(merged, nid, cfg)

    # 光有数据不够：节点属性 config 必须是 "1"，EVE 才会把它 dump 成
    # startup-config。老包、或者源实验里节点是 config="0" 的，都在这里补上。
    merged = enable_configs(merged)

    wanted = (name or "").strip() or (manifest.get("lab") or {}).get("name") \
        or os.path.basename(str((manifest.get("lab") or {}).get("file") or "lab.unl"))[:-4]
    wanted = _safe_name(wanted)
    final_name = _unique_name(app, folder, wanted)
    if final_name != wanted:
        warnings.append("目标已有同名实验，已改名为 %s" % final_name)

    step("创建实验")
    created = app.create_lab(folder, final_name,
                             (manifest.get("lab") or {}).get("description") or "")
    new_path = created["path"]
    lab_file = "%s/%s" % (LABS_DIR, new_path)

    step("写入拓扑与配置")
    try:
        remote.write_text(lab_file, merged)
        # 只调整这个文件本身的属主；**千万不要对 labs 目录 chmod**
        # （把它 chmod 664 会去掉目录的 x 位，EVE 立刻建不了任何实验，
        #   实测报 tempnam(): file created in the system's temporary directory）
        remote.run("chown www-data:www-data '%s' && chmod 644 '%s'" % (lab_file, lab_file))
    except Exception as e:
        # 写失败就别留个空实验在用户眼前
        try:
            remote.run("rm -f %s" % lab_file)
        except Exception:
            pass
        raise PackError("写入实验文件失败：%s" % e)

    # 立即验证 EVE 能不能解析（解析失败说明 .unl 不兼容，回滚掉）
    step("校验")
    try:
        got = app.require().lab_nodes("/" + new_path) or {}
        if len(got) != len(nodes):
            warnings.append("节点数对不上：包里 %d 个，服务器解析出 %d 个"
                            % (len(nodes), len(got)))
    except Exception as e:
        try:
            remote.run("rm -f %s" % lab_file)
        except Exception:
            pass
        raise PackError("导入的拓扑 EVE 解析失败，已回滚：%s" % e)

    # ---- 让配置真正生效（这是「导入后配置没了」的根因）----
    # 光把 base64 塞进 .unl 不够。EVE 启动节点时的条件是：
    #     if ($n->getConfig()=='1' && $n->getConfigData()!='') dumpConfig(...)
    # 也就是**节点属性 config 必须是 "1"**。而写配置的接口（PUT …/configs/{id}）
    # 只写数据、不动这个标志 —— 实测写完节点仍是 config="0"，数据躺在 .unl 里
    # 却永远不会变成 startup-config，表现出来就是「配置没了」。
    #
    # 两条路都要走：
    #   CE 6.x：有 /api/labs/{path}/configs/{id}，写数据 + 打开标志
    #   Pro 7.x：这个接口不存在（api.php 加密，回 500/60027），配置只在 .unl 里 ——
    #            上面 inject_config / enable_configs 已经把它写好了，这里跳过即可
    step("写入节点配置")
    eve = app.require()
    cfg_map = embedded_configs(merged)
    api_ok, warnings2 = 0, []
    api_off = False
    for nid, cfg in sorted(cfg_map.items(), key=lambda kv: int(kv[0])):
        if api_off:
            break
        try:
            eve.put("/api/labs/%s/configs/%s" % (_q(new_path), nid),
                    {"id": int(nid), "data": cfg})
            eve.edit_node("/" + new_path, nid, {"config": "1"})
            api_ok += 1
        except Exception as e:
            # 这台服务器没有可用的配置接口（Pro 7.x 的 api.php 是加密的，
            # 实测回 500 / 60027 / 60061）。配置已经写进 .unl 了，
            # 只记日志，不再对后面每个节点重复试错。
            api_off = True
            try:
                app.log("目标机的配置接口不可用，改由 .unl 承载配置（%s）"
                        % str(e)[:120], "warn")
            except Exception:
                pass

    # 以「服务器上的 .unl」为准复核 —— 不管 API 通不通，配置必须真的在里面、
    # 且节点的 config 标志是 1，否则启动时 EVE 不会把它 dump 成 startup-config
    step("复核配置")
    effective, still = len(cfg_map), []
    try:
        final = remote.read_text(lab_file, limit=32 << 20)
        effective = len(embedded_configs(final))
        for nid in cfg_map:
            if not _config_enabled(final, nid):
                still.append(nid)
        if effective < len(cfg_map):
            warnings.append("有 %d 个节点的配置没落到实验文件里"
                            % (len(cfg_map) - effective))
        if still:
            warnings.append("这些节点有配置但没打开「使用配置」开关，启动不会生效：%s"
                            % "、".join(still[:5]))
    except Exception as e:
        warnings.append("复核配置时读不回实验文件：%s" % e)

    cfg_wrote = effective if not still else len(cfg_map) - len(still)

    # 目标机缺镜像时拓扑照样完整，只是那些节点起不来 —— 提前讲清楚，
    # 免得用户以为是导坏了（EVE 的模板描述里没镜像的会以 `.missing` 结尾）
    try:
        tpl = app.require().templates() or {}
        lack = []
        for nd in nodes:
            tm = nd.get("template") or ""
            desc = tpl.get(tm)
            nm = nd.get("name") or nd.get("id")
            if (desc is None or str(desc).endswith(".missing")) and nm not in lack:
                lack.append(nm)
        if lack:
            warnings.append("目标机缺镜像，这些节点起不来：%s%s（先用「镜像 → 导入」补上）"
                            % (", ".join(lack[:5]), " …" if len(lack) > 5 else ""))
    except Exception:
        pass

    step("恢复文本框")
    tb = 0
    if notes:
        try:
            kept = app.notes.set(app.info.get("host"), "/" + new_path, notes) or []
            tb = len(kept)
        except Exception as e:
            warnings.append("文本框没恢复上：%s" % e)

    return {
        "path": new_path,
        "name": final_name,
        "nodes": len(got),
        "networks": len(networks),
        "configs": cfg_wrote,       # 已就位（数据在 + 开关打开）的节点数
        "configs_total": len(cfg_map),
        "configs_api": api_ok,      # 走服务器配置接口写成功的个数（Pro 通常为 0）
        "textboxes": tb,
        "warnings": warnings,
        "source": manifest.get("source") or {},
    }


# ---------------------------------------------------------------- 批量导出（多实验）
def export_many(app, paths, out_dir=None, progress=None):
    """一次导多个实验，各自一个 zip，再合成一个大包（带 labs/ 子目录）。"""
    remote = ensure_ssh(app)
    out_dir = out_dir or _default_export_dir(app)
    os.makedirs(out_dir, exist_ok=True)
    results, errors = [], []
    for i, p in enumerate(paths, 1):
        try:
            if progress:
                progress("导出 %d/%d：%s" % (i, len(paths), p))
            results.append(export_lab(app, p, out_dir=out_dir))
        except Exception as e:
            errors.append("%s：%s" % (p, e))
    return {"items": results, "errors": errors}
