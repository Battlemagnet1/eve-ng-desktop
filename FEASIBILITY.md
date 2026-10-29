# EVE-NG 桌面 GUI 客户端 — 可行性分析

> 目标机：**EVE-NG Community 6.2.0-4**（实测环境地址下以 `192.168.1.50` 代指）
> 分析日期：2026-10-29
> 定位：把现有网页版 `eveimgd` 的能力（导入/导出/镜像管理）搬进桌面 GUI，并新增「实验与拓扑浏览 + 节点操控 + 内置终端 + 网卡/外网桥接配置」。
> 明确剔除：镜像源（OpenList）、百度网盘、镜像在线下载。

---

## 一、目标环境实测结论

| 项目 | 实测值 | 影响 |
|---|---|---|
| EVE 版本 | **6.2.0-4 Community** | 无多租户，控制台端口 `32768 + node_id` |
| 系统 / 内核 | Ubuntu + `6.7.5-eveng-6-ksm+` x86_64 | — |
| CPU / 内存 / 磁盘 | 8C（Ryzen 7 255）/ 7.9G / 76G（用 9.1G） | 可同时跑 5–8 台 IOL |
| 模板 | `/opt/unetlab/html/templates/{intel,amd}`，intel 下 **172 个 yml** | 节点属性面板可全量枚举 |
| 镜像 | QEMU 空；IOL 1 个（`i86bi_...157_3.bin` + `iourc`）；Dynamips/Docker 空 | 导入功能需从零验证 |
| 实验 | `/opt/unetlab/labs/1.unl`（明文 XML，5×IOL + 4 bridge） | 拓扑可直读 |
| 网桥 | `pnet0`=管理网（eth0，`192.168.1.50/20`，DHCP）；`pnet1–9` 已定义但**无物理网卡**、全部 DOWN | 外网桥接只能走 NAT 或复用 pnet0 |
| 转发 / NAT | `ip_forward=0`，iptables 空，`dnsmasq` 已装但未启用 | NAT 出口需自建，可用现成组件 |
| API 认证 | `POST /api/auth/login` → cookie `unetlab_session`，与网页版同源 | 无需装任何 agent |
| HTML5 控制台 | `/html5/` 可用（Apache 直出，**无 X-Frame-Options**） | 可内嵌 iframe |
| 服务器 Python | 3.10.12 | 若将来加 agent 可用 |

### 关键实测：CE 6.2 的 API 完整度

已逐条打通（带 cookie 实测返回 200）：

```
GET  /api/status                          # 版本/负载/运行中节点数
GET  /api/folders/(:path+)                # 实验与文件夹列表（CE 用它代替 /api/labs）
GET  /api/labs/{path}                     # 实验元数据
GET  /api/labs/{path}/nodes               # 节点列表 + status
GET  /api/labs/{path}/nodes/{id}          # 单节点全属性（含 html5 控制台 url+token）
GET  /api/labs/{path}/nodes/{id}/interfaces   # 接口 → network_id 映射
GET  /api/labs/{path}/nodes/{id}/start|stop|wipe
PUT  /api/labs/{path}/nodes/{id}          # 改节点属性（内存/网卡数/镜像…）
PUT  /api/labs/{path}/nodes/{id}/interfaces   # 改网卡接线（NIC ↔ 网络）
POST /api/labs/{path}/nodes               # 新增节点
POST /api/labs/{path}/networks            # 新增网络（bridge / pnet0–9 / ovs）
GET  /api/list/networks                   # 可选网络类型
GET  /api/list/templates/{template}       # 模板全部可填字段（含 image 列表、图标列表）
POST /api/export  /  POST /api/import     # 实验级导出/导入（zip）
GET  /api/labs/{path}/nodes/{id}/export   # 单节点配置导出
```
**`GET /api/labs` 在 CE 上返回 404**（Pro 独有）——这是必须做兼容分叉的第一处。

---

## 二、能力矩阵

### ✅ 完全可做（已有实测依据）

| 功能 | 实现路径 |
|---|---|
| 实验/文件夹树浏览 | `GET /api/folders/`，递归；Pro 走 `/api/labs` 兜底 |
| 拓扑图形化展示 | `GET /api/labs/{p}/nodes` + `/topology`（返回 `source/destination/network_id` 边表），自绘 Canvas/SVG，坐标直接用 `.unl` 里的 `left/top` |
| 节点启停/擦除 | `nodes/{id}/start|stop|wipe`，轮询 `status`（0 停 / 1 运行 / 2 启动中） |
| 批量整实验启动/停止 | `nodes/start`、`nodes/stop` |
| 镜像导入（本机→服务器） | paramiko SFTP 分块上传 → 远端 `tar xzf` → 生成/校验 `iourc`（IOL）→ 落位 `addons/qemu|iol` |
| 镜像导出（服务器→本机） | 远端 `tar czf` → SFTP 下载 → 本地解包成「目录+yml」标准包（复用网页版逻辑） |
| 节点属性编辑 | `PUT nodes/{id}`，字段来自 `list/templates/{t}` |
| 网卡接线编辑（拓扑内） | `PUT nodes/{id}/interfaces`，数组 `[{name, network_id}]` |
| 主机 Shell 终端 | paramiko `invoke_shell()` PTY + xterm.js |
| 节点控制台终端 | telnet `32768+128*tenant+id`（tenant=0）→ xterm.js |
| 链路增删 | `POST networks` + `PUT nodes/{id}/interfaces` |

### ⚠️ 可做但有明显难点（需专门设计）

| 难点 | 说明 | 对策 |
|---|---|---|
| **控制台端口跨网可达性** | 32769+ 端口是否被防火墙/云安全组拦住，本机未验证（需节点运行态才能测） | **默认走 SSH 本地端口转发**（paramiko `direct-tcpip`）：不依赖端口开放，还能顺带解决 Pro 多租户端口偏移 |
| **telnet 协议协商** | IOL/IOS 会发 `IAC WILL/DO` 序列，不回应会显示乱码或吞按键 | 内置极简 telnet 客户端（剥 IAC、回 WILL/DO、Ctrl-C 走 `IAC IP`），已明确要做，不是猜测性工作 |
| **VNC / RDP 节点** | Windows、Linux 桌面类节点没有 telnet | 分两类终端：字符类走原生 xterm；图形类**内嵌 EVE 自带 `/html5/` 页面**（节点详情接口直接给到带 token 的 url，且无 X-Frame-Options，可 iframe） |
| **外网桥接（只有 1 块物理网卡）** | `pnet1–9` 无对应 eth，直接桥接无从谈起 | 两条路：① 节点挂 `pnet0` → 直接拿局域网 IP（最简单，等于和 EVE 同网）；② 新建 `pnetN` 空桥 + 网关 IP + `dnsmasq` + `ip_forward=1` + `MASQUERADE -o eth0` → **NAT 出口**，节点拿 10.255.x.x 后能上外网 |
| **改 `/etc/network/interfaces` 有断网风险** | 写错就 SSH 掉线，救不回来 | 改前备份 + `at`/`nohup` **看门狗**：60 秒内没收到 GUI 的"确认存活"就自动还原并 `ifup`；`pnet0` 永远只读 |
| **大镜像上传** | QEMU 镜像 1–4G，SFTP 单流 ~30–80MB/s（千兆内网） | 分块 + 断点续传（记录 offset）+ 上传后 sha 校验；另提供"服务器已有文件，直接登记"的秒导入 |
| **拓扑编辑（拖拽新增/连线）** | 只读渲染容易，交互式编辑器要复刻 EVE 画布行为 | **一期只读 + 节点右键操作 + 连线改接**；二期再做"从模板库拖出新节点" |
| **并发编辑冲突** | 用户可能同时开着 EVE 网页版改同一个实验 | 操作前读 `lock` 字段；写入前比对 `umtime`，变了就提示重载 |

### ❌ 做不了 / 不建议做

| 项 | 原因 |
|---|---|
| 镜像在线下载、改镜像源 | 你已明确不要；且百度分享直链需 BDUSS，不可自动化（此前已验证） |
| 无 SSH 权限时改网桥/pnet | 纯 REST API 没有改宿主机网卡配置的接口（`/api/networks` 是实验内网络，不是宿主网桥） |
| 在 GUI 里跑网页版"镜像源浏览" | 已剔除 |
| 跨 NAT/公网的 EVE 直连 | 需要端口映射，属部署问题，不在 GUI 范围（SSH 隧道可绕过） |

---

## 三、架构方案

```
┌──────────────────────── Windows 本机 ────────────────────────┐
│  pywebview 原生窗口 (Edge WebView2 155.x 已就绪)             │
│   ├─ 前端：HTML/CSS/JS 单页（沿用网页版视觉）                │
│   │    ├─ 实验树 / 拓扑画布 (Canvas)                         │
│   │    ├─ 终端面板 (xterm.js × N 个 tab)                     │
│   │    └─ 镜像库 / 导入导出 / 网卡配置                       │
│   └─ 本地 HTTP 服务 127.0.0.1:<随机端口>  ← 纯标准库          │
│         ├─ /api/eve/*     → EVEClient (REST + cookie)        │
│         ├─ /api/term/*    → TermSession (SSE 推流 + POST 输入)│
│         ├─ /api/img/*     → ImageStore (SFTP 上传/下载/解包)  │
│         └─ /api/net/*     → NetConfig (pnet/NAT)             │
└──────────────────────────────┬───────────────────────────────┘
                               │ SSH :22 (paramiko)
                               │ HTTP :80 (EVE REST/HTML5)
┌──────────────────────────────▼───────────────────────────────┐
│  EVE-NG 6.2.0-4 (CE)  /  7.x (Pro)                           │
│  Apache(/api, /html5) · nodes: telnet 32769+ · /opt/unetlab  │
└──────────────────────────────────────────────────────────────┘
```

**为什么是 pywebview 而不是 PySide6/Qt**：现有网页版是 HTML+纯标准库 HTTP，直接复用等于省掉 70% 工作量；pywebview 只提供原生窗口壳（无地址栏、无书签、可打包 exe），装完才 ~6MB（Qt 方案 200MB+）。已在本机跑通（`pywebview 6.2.1` + WebView2 `155.0.4283.62`）。

**终端为什么用 SSE 而不是 WebSocket**：输出是单向流（服务器→浏览器），SSE 天然合适，且**不用引第三方 WS 库、也不用自己实现握手分帧**；键盘输入用普通 POST。延迟在局域网下 <10ms，足够 IOS CLI。

---

## 四、兼容性设计（重点：留更新接口）

核心原则：**按能力探测，不按版本号判断**。

```python
class EVECompat:
    """所有版本差异的唯一收敛点，其它模块只依赖这层。"""
    caps = {
        "list_labs":   None,   # 探测：/api/labs 200 → True，404 → 用 /api/folders
        "tenant":      None,   # 探测：/api/auth 返回 tenant 字段
        "port_base":   32768,  # CE 32768；Pro 多租户 32768+128*tenant
        "html5":       None,   # 探测：/html5/ 是否 200
        "ovs":         None,   # 探测：list/networks 是否含 ovs
        "templates_dir": None,  # intel / amd，按 /proc/cpuinfo 判架构
        "labs_root":   None,   # /opt/unetlab/labs 或 Pro 的租户子目录
    }
```

| 差异点 | CE 6.2 | Pro 7.x | 收敛方式 |
|---|---|---|---|
| 实验列表 | `GET /api/folders/` | `GET /api/labs` | 探测后二选一，统一成 `LabRef` |
| 控制台端口 | `32768+id` | `32768+128*tenant+id` | `EVECompat.console_port(node, tenant)` |
| 认证 | cookie `unetlab_session` | 同 | 统一 Session 对象 |
| 模板目录 | intel / amd | intel / amd | `detect_arch()` 复用网页版实现 |
| 网络类型 | bridge/pnet0-9/ovs | 同 + 更多 | 以 `list/networks` 返回为准，不硬编码 |
| 节点属性字段 | `.unl` 字段直出 | 多了 `uuid`/`console` 等 | 以 `list/templates/{t}` 的 `options` 为准，未知字段原样透传（**不做白名单**） |
| 实验文件 | 明文 XML | 可能 gzip | 只经 API 读写，**不直接解析磁盘文件**（绕开格式差异） |

> 「更新接口」的落点：新增版本差异**只改 `EVECompat` 一个文件 + 一份 `capabilities` 表**，业务代码零改动。所有 EVE 特有的常量（端口基址、路径、状态码含义）集中在此。

---

## 五、实施阶段（建议）

| 阶段 | 内容 | 产出 |
|---|---|---|
| P1 骨架 | pywebview 壳 + 本地服务 + 连接配置（多服务器 profile，密码 DPAPI 加密）+ EVEClient/EVECompat | 能连上、看 status |
| P2 拓扑 | 实验树 → 拓扑画布（节点/连线/云）+ 节点右键（启停/擦除/打开终端） | 只读浏览 + 启停 |
| P3 终端 | xterm.js + SSH 隧道 + telnet 协商 + 主机 Shell + 图形控制台 iframe | 内置终端可用 |
| P4 镜像 | 导入（SFTP 断点续传 + IOL 许可证/keygen 复用网页版）+ 导出（远端打包下载）+ 镜像库 | 复刻网页版核心 |
| P5 网络 | 网卡接线编辑、pnet 绑定（只读展示 + 守卫式修改）、外网出口向导（pnet0 直桥 / NAT） | 桥接外网 |
| P6 收尾 | 拓扑编辑（拖拽新增节点）、打包 exe（PyInstaller）、README | 可分发 |

---

## 六、需要你拍板的 4 件事

1. **GUI 技术栈**：pywebview（轻、复用网页版，推荐）／ PySide6（纯原生控件、体积大）／ 直接 Edge `--app` 模式（零依赖但像浏览器）。
2. **连接方式**：SSH 全功能（需 root 密码，才能上传镜像、改网桥、开主机终端）／ 仅 REST（只要 EVE 账号，功能受限）。推荐前者，密码本地 DPAPI 加密。
3. **一期拓扑**：只读 + 右键操作（推荐，稳）／ 直接上可拖拽编辑器（工期长、易出 bug）。
4. **外网出口默认方案**：`pnet0` 直桥（节点直接拿局域网 IP，最省事）／ NAT 新建 `pnetN`（节点在 10.255.x.x 后 NAT 出网，更隔离）。两者都会做，只是默认选哪个。
