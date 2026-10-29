# -*- coding: utf-8 -*-
"""宿主机网络：pnet 查看、NAT 出口（让拓扑内节点上外网）。

设计原则：
  - pnet0 是管理网，永不修改；
  - NAT 全部落在独立脚本 + systemd unit 里，可一键卸载，不碰 /etc/network/interfaces；
  - 所有命令幂等，重复执行不会叠加 iptables 规则。
"""
import posixpath
import re

BRIDGE_FILE = "/etc/network/interfaces"
NAT_DIR = "/opt/eve-desktop"
NAT_UNIT = "/etc/systemd/system/eve-nat-pnet{n}.service"


def parse_interfaces(text):
    """{pnetN: [ethX...]}"""
    cur, out = None, {}
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"iface\s+(pnet\d+)\s+inet", line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
            continue
        m = re.match(r"bridge_ports\s+(.+)", line)
        if m and cur:
            out[cur] = [p for p in m.group(1).split() if p and p != "none"]
            continue
        if line.startswith("iface ") or line.startswith("auto "):
            cur = None
    return out


def list_pnets(remote):
    rc, txt, _e = remote.run("cat %s 2>/dev/null" % BRIDGE_FILE, timeout=20)
    ports = parse_interfaces(txt or "")
    addrs = {}
    rc2, ipout, _ = remote.run("ip -br -4 a show 2>/dev/null", timeout=20)
    states = {}
    for line in (ipout or "").splitlines():
        p = line.split()
        if not p:
            continue
        name = p[0].split("@")[0]
        states[name] = p[1] if len(p) > 1 else ""
        m = re.search(r"(\d+\.\d+\.\d+\.\d+/\d+)", line)
        if m:
            addrs[name] = m.group(1)
    out = []
    for n in range(0, 10):
        name = "pnet%d" % n
        out.append({
            "name": name,
            "ports": ports.get(name, []),
            "ip": addrs.get(name, ""),
            "state": states.get(name, "DOWN"),
            "mgmt": n == 0,
            "exists": name in states,
            "managed": name in ports,
        })
    return out


def host_ifaces(remote):
    """可用物理网卡。

    必须排除 EVE 自己造的动态接口：vnet0_x（云）、vunl0_x_y（节点接网桥）、
    vunl2_x（IOL 的 veth 对），它们随实验启停出现/消失，选成 NAT 上游会立刻失效。
    """
    out = remote.out("ls -1 /sys/class/net 2>/dev/null", timeout=20)
    bad = re.compile(r"^(lo|pnet\d+|vnet\d+_\d+|vunl\d+.*|docker.*|veth.*|virbr.*|"
                     r"tun.*|tap.*|br-.*|dummy.*)$")
    return [x for x in out.splitlines() if x.strip() and not bad.match(x.strip())]


def pnet_in_use(remote):
    """已带 IP 或已 up 的 pnet（创建 NAT 时避开）"""
    rc, o, _ = remote.run("ip -br a show 2>/dev/null | grep -E '^pnet[0-9]+'", timeout=20)
    used = set()
    for line in (o or "").splitlines():
        p = line.split()
        if len(p) >= 2 and (p[1] == "UP" or re.search(r"\d+\.\d+\.\d+\.\d+", line)):
            used.add(p[0])
    return used


NAT_SCRIPT = r"""#!/bin/sh
# 由 EVE Desktop 生成 · pnet{n} NAT 出口 · 删除本文件与对应 systemd unit 即可卸载
set -e
BR=pnet{n}
CIDR={cidr}
GW={gw}
UP={upstream}

ip link show $BR >/dev/null 2>&1 || ip link add name $BR type bridge
ip link set $BR up
ip addr replace $GW/24 dev $BR

echo 1 > /proc/sys/net/ipv4/ip_forward
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf 2>/dev/null || \
  echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

iptables -t nat -C POSTROUTING -s $CIDR -o $UP -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -s $CIDR -o $UP -j MASQUERADE
iptables -C FORWARD -i $BR -o $UP -j ACCEPT 2>/dev/null || \
  iptables -A FORWARD -i $BR -o $UP -j ACCEPT
iptables -C FORWARD -i $UP -o $BR -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
  iptables -A FORWARD -i $UP -o $BR -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

mkdir -p /etc/dnsmasq.d
cat > /etc/dnsmasq.d/eve-nat-$BR.conf <<EOF
interface=$BR
bind-interfaces
dhcp-range={dhcp_start},{dhcp_end},255.255.255.0,12h
dhcp-option=option:router,$GW
dhcp-option=option:dns-server,$GW
EOF
systemctl restart dnsmasq >/dev/null 2>&1 || systemctl start dnsmasq >/dev/null 2>&1 || \
  (dnsmasq --conf-file=/etc/dnsmasq.d/eve-nat-$BR.conf 2>/dev/null || true)
echo "ok $BR $CIDR via $UP"
"""

NAT_OFF = r"""#!/bin/sh
BR=pnet{n}
CIDR={cidr}
UP={upstream}
ip link set $BR down 2>/dev/null || true
ip link del $BR 2>/dev/null || true
iptables -t nat -D POSTROUTING -s $CIDR -o $UP -j MASQUERADE 2>/dev/null || true
iptables -D FORWARD -i $BR -o $UP -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i $UP -o $BR -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
rm -f /etc/dnsmasq.d/eve-nat-$BR.conf
systemctl restart dnsmasq >/dev/null 2>&1 || true
echo "off $BR"
"""


def have_dnsmasq(remote):
    return remote.ok("command -v dnsmasq >/dev/null 2>&1")


def nat_state(remote, n, upstream=None):
    upstream = upstream or detect_upstream(remote)
    br = "pnet%d" % n
    ip = remote.out("ip -br -4 a show %s 2>/dev/null" % br)
    unit = NAT_UNIT.format(n=n)
    # 注意：apply 时带 -o <upstream>，这里 -C 必须带同样的匹配条件，
    # 否则永远匹配不到自己写的规则，会误报「未生效」
    return {
        "pnet": br,
        "up": bool(ip.strip()),
        "ip": ip.strip(),
        "script": remote.exists("%s/nat-%s.sh" % (NAT_DIR, br)),
        "unit_enabled": remote.ok("systemctl is-enabled %s >/dev/null 2>&1"
                                  % unit.split("/")[-1]),
        "masq": remote.ok("iptables -t nat -C POSTROUTING -s 10.255.%d.0/24 -o %s "
                          "-j MASQUERADE 2>/dev/null" % (n, upstream)),
        "dhcp": have_dnsmasq(remote),
    }


def plan_nat(n, cidr_base="10.255"):
    return {
        "pnet": "pnet%d" % n,
        "cidr": "%s.%d.0/24" % (cidr_base, n),
        "gw": "%s.%d.1" % (cidr_base, n),
        "dhcp_start": "%s.%d.100" % (cidr_base, n),
        "dhcp_end": "%s.%d.200" % (cidr_base, n),
    }


def nat_apply(remote, n, upstream=None, persist=True, cidr_base="10.255"):
    """建 NAT 出口云。返回执行结果与给用户看的信息。"""
    if n == 0:
        raise ValueError("pnet0 是管理网，不允许改成 NAT 出口")
    if not upstream:
        upstream = detect_upstream(remote)
    p = plan_nat(n, cidr_base)
    remote.run("mkdir -p %s" % NAT_DIR, timeout=20)
    script = "%s/nat-%s.sh" % (NAT_DIR, p["pnet"])
    body = NAT_SCRIPT.format(n=n, cidr=p["cidr"], gw=p["gw"], upstream=upstream,
                             dhcp_start=p["dhcp_start"], dhcp_end=p["dhcp_end"])
    remote.write_text(script, body, mode=0o755)

    rc, o, e = remote.run("sh %s" % script, timeout=120)
    if rc != 0:
        raise RuntimeError("NAT 配置失败：%s" % ((e or o or "").strip()[:400]))

    out = {"pnet": p["pnet"], "cidr": p["cidr"], "gw": p["gw"], "upstream": upstream,
           "stdout": (o or "").strip(), "persist": False,
           "dhcp": have_dnsmasq(remote), "dhcp_start": p["dhcp_start"],
           "dhcp_end": p["dhcp_end"]}
    if not out["dhcp"]:
        # 没有 dnsmasq 就没有 DHCP：桥和 NAT 都正常，但节点必须手工配静态地址
        out["hint"] = ("宿主机未安装 dnsmasq，未下发 DHCP。请在节点网卡上手工配置 "
                       "IP %s/24、网关 %s；或安装 dnsmasq 后重建（apt-get install -y dnsmasq）。"
                       % (p["dhcp_start"], p["gw"]))
    if persist and remote.ok("command -v systemctl"):
        unit = NAT_UNIT.format(n=n)
        remote.write_text(unit, (
            "[Unit]\nDescription=EVE Desktop NAT egress on pnet{n}\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            "ExecStart=/bin/sh {script}\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        ).format(n=n, script=script), mode=0o644)
        if remote.ok("systemctl daemon-reload && systemctl enable --now eve-nat-pnet%d.service "
                     ">/dev/null 2>&1 && systemctl is-active eve-nat-pnet%d.service >/dev/null"
                     % (n, n), timeout=60):
            out["persist"] = True
    return out


def nat_remove(remote, n, upstream=None):
    upstream = upstream or detect_upstream(remote)
    p = plan_nat(n)
    script = "%s/nat-%s.sh" % (NAT_DIR, p["pnet"])
    off = "%s/nat-%s-off.sh" % (NAT_DIR, p["pnet"])
    remote.write_text(off, NAT_OFF.format(n=n, cidr=p["cidr"], upstream=upstream), mode=0o755)
    unit_name = "eve-nat-pnet%d.service" % n
    remote.run("systemctl disable --now %s >/dev/null 2>&1 || true" % unit_name, timeout=60)
    rc, o, e = remote.run("sh %s" % off, timeout=120)
    remote.run("rm -f '%s' '%s' '%s'" % (script, off, NAT_UNIT.format(n=n)), timeout=30)
    remote.run("systemctl daemon-reload >/dev/null 2>&1 || true", timeout=30)
    if rc != 0:
        raise RuntimeError("卸载失败：%s" % ((e or o or "").strip()[:300]))
    return {"pnet": p["pnet"], "removed": True}


def suggest_pnet(remote):
    used = pnet_in_use(remote)
    for n in range(1, 10):
        if "pnet%d" % n not in used:
            return n
    return 1


def detect_upstream(remote):
    """出口网卡 = 默认路由所在接口。

    EVE 里 eth0 被桥进 pnet0，IP 和默认路由都在 pnet0 上；
    直接写死 eth0 会让 MASQUERADE 的 `-o eth0` 永远匹配不到包
    （iptables 计数为 0，表现为「NAT 配好了但上不了网」）。
    """
    dev = remote.out("ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++)"
                     " if($i==\"dev\"){print $(i+1); exit}}'", timeout=20).strip()
    if dev:
        return dev
    for cand in ("pnet0", "eth0"):
        if remote.ok("ip link show %s >/dev/null 2>&1" % cand):
            return cand
    return "eth0"


def bridge_for_cloud(remote):
    """给前端展示：哪些云网络可用"""
    up = detect_upstream(remote)
    cands = []
    for x in ([up] if up else []) + host_ifaces(remote):
        if x not in cands:
            cands.append(x)
    return {
        "pnets": list_pnets(remote),
        "ifaces": cands,
        "upstream": up,
        "suggest": suggest_pnet(remote),
    }


# --------------------------------------------------------------------------
# 残留 TAP 清理
#
# EVE 停节点时不会回收 vunl* tap（常见于异常退出、批量启动中途失败），
# 这些 tap 会一直 DOWN 挂着；下次启动同名 tap 时 tunctl 直接报
# "TUNSETIFF: Operation not permitted / Device or resource busy"，
# 导致 unl_wrapper -a start 整批中止（报 Failed to start node）。
# 这不是本工具引入的，但我们可以在启动前顺手清掉。
#
# 命名规则：vunl{tenant}_{node_id}_{if_id}（同 tenant 下不同实验会撞名，
# 所以只在「本实验该节点已停止」且「tap 不是 UP」时才删，
# 避免误删别的实验正在用的 tap）。
# --------------------------------------------------------------------------
_TAP_RE = re.compile(r"^vunl(\d+)_(\d+)_(\d+)$")


def stale_taps(remote, tenant, node_ids):
    """找出本实验内、属于已停止节点、且当前不是 UP 的 tap 名"""
    if not node_ids:
        return []
    ids = set(int(x) for x in node_ids)
    txt = remote.out("ip -br link 2>/dev/null | awk '{print $1\" \"$2}'", timeout=30)
    out = []
    for line in (txt or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        m = _TAP_RE.match(parts[0])
        if not m:
            continue
        t, nid, _if = m.groups()
        if int(t) != int(tenant) or int(nid) not in ids:
            continue
        if parts[1].upper() != "UP":      # UP 说明正在被某个节点使用，别碰
            out.append(parts[0])
    return out


def clean_taps(remote, names):
    removed, failed = [], []
    for n in names:
        rc, _o, _e = remote.run("tunctl -d %s 2>&1" % n, timeout=30)
        (removed if rc == 0 else failed).append(n)
    return {"removed": removed, "failed": failed}


def lab_tap_cleanup(remote, tenant, nodes):
    """按 lab_nodes 字典清理本实验的残留 tap"""
    stopped = [nid for nid, n in (nodes or {}).items()
               if str((n or {}).get("status") or "0") in ("0", "1")]
    taps = stale_taps(remote, tenant, stopped)
    res = clean_taps(remote, taps)
    res["stopped_nodes"] = stopped
    return res


_ = posixpath  # 保留导入，便于后续扩展
