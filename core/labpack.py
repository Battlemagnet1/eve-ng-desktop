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

    # 内嵌在 .unl 里的配置
    step("收集节点配置")
    inline = embedded_configs(text)

    # 运行目录里还有、但没存进 .unl 的配置 —— 一并带走（用户可能忘了 Save）
    extra = {}
    warnings = []
    if uuid:
        for nd in nodes:
            nid = str(nd["id"])
            if nid in inline:
                continue
            p = "%s/%d/%s/%s/startup-config" % (TMP_DIR, tenant, uuid, nid)
            try:
                if remote.exists(p):
                    c = remote.read_text(p, limit=4 << 20)
                    if c and c.strip():
                        extra[nid] = c
            except Exception as e:
                warnings.append("节点 %s 的运行配置读不到：%s" % (nd["name"], e))

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
        },
        "nodes": [dict(n, has_config=("1" if (str(n["id"]) in inline or str(n["id"]) in extra)
                                      else n["config"])) for n in nodes],
        "networks": networks,
    }

    out_dir = out_dir or _default_export_dir(app)
    os.makedirs(out_dir, exist_ok=True)
    base = _safe_name(manifest["lab"]["name"] or os.path.basename(path)[:-4])
    out = os.path.join(out_dir, "%s-%s.zip" % (base, _stamp()))
    n = 1
    while os.path.exists(out):
        out = os.path.join(out_dir, "%s-%s-%d.zip" % (base, _stamp(), n))
        n += 1

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("lab.unl", text)
        z.writestr("textboxes.json", json.dumps(notes, ensure_ascii=False, indent=1))
        for nid, cfg in extra.items():
            nm = _safe_name((node_map.get(nid) or {}).get("name") or nid, "node")
            z.writestr("configs/%s-%s.cfg" % (nid, nm), cfg)

    return {
        "file": out,
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

    # 把「运行目录里捞出来的、.unl 里没存」的配置补进 .unl，这样 EVE 下次启动才认
    warnings = []
    inline = embedded_configs(lab_text)
    merged = lab_text
    for nid, cfg in extra.items():
        if nid in inline:
            continue
        merged = inject_config(merged, nid, cfg)

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

    cfg_count = len(embedded_configs(merged))
    return {
        "path": new_path,
        "name": final_name,
        "nodes": len(got),
        "networks": len(networks),
        "configs": cfg_count,
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
