'use strict';
/* ==================================================================
   界面语言与主题
   ------------------------------------------------------------------
   为什么不用「给每个文案加 id」的老办法：
   这个前端的文字大量来自模板字符串（innerHTML），逐个加 data-i18n 要改
   一百多处调用点，还容易漏。这里改成**以中文原文为 key** 的运行时词典：

   - 静态 HTML 与动态 innerHTML 都走同一个 DOM 扫描器，DOM 一变
     （MutationObserver）就补翻，代码里不用写任何标记
   - 带变量的句子用 `{}` 通配：词条写 `已启动 {}/{} 个节点` 就能命中
     运行期拼好的 `已启动 3/5 个节点`
   - 原文存在 WeakMap 里，切回中文时按原文还原，不靠反查英文
   - 用户数据（实验名等）标 data-notrans 跳过，避免把「网络」翻成 Network

   主题：html[data-theme] 上挂 light / dark，颜色全部走 CSS 变量。
   ================================================================== */

(function () {
  var CJK = /[\u4e00-\u9fff]/;

  /* ---------------- 英文词条（中文原文 → 英文） ---------------- */
  var EN = {
    /* 通用动词 / 名词 */
    '下载': 'Download',
    '丢弃': 'Discard',
    '停止': 'Stop',
    '内存': 'Memory',
    '删除': 'Delete',
    '刷新': 'Refresh',
    '卸载': 'Remove',
    '取消': 'Cancel',
    '名称': 'Name',
    '启动': 'Start',
    '图形': 'GUI',
    '大小': 'Size',
    '字符': 'chars',
    '实验': 'Lab',
    '导出': 'Export',
    '抓包': 'Capture',
    '拓扑': 'Topology',
    '接口': 'Interface',
    '接线': 'Connect',
    '操作': 'Actions',
    '擦除': 'Wipe',
    '收起': 'Collapse',
    '改名': 'Rename',
    '模板': 'Template',
    '状态': 'Status',
    '直连': 'Direct links',
    '确定': 'OK',
    '类型': 'Type',
    '终端': 'Terminal',
    '编号': 'ID',
    '网卡': 'NICs',
    '网络': 'Network',
    '节点': 'Node',
    '设置': 'Settings',
    '说明': 'Description',
    '适应': 'Fit',
    '重载': 'Reload',
    '镜像': 'Image',
    '上一级': 'Up',
    '控制台': 'Console',
    '运行中': 'Running',
    '已停止': 'Stopped',
    '已连接': 'Connected',
    '未连接': 'Not connected',
    '根目录': 'Root',
    '空目录': 'Empty folder',
    '重命名': 'Rename',
    '镜像库': 'Image library',
    '模板库': 'Templates',
    '宿主机网络': 'Host network',
    '连接设置': 'Connection',
    '连接协议': 'Protocol',
    '服务器地址': 'Server address',
    '控制台端口': 'Console port',
    '启动失败:': 'Start failed:',
    '全部启动': 'Start all',
    '全部停止': 'Stop all',
    '适应窗口': 'Fit to window',
    '重新加载': 'Reload',
    '重新加载拓扑': 'Reload topology',
    '保存并连接': 'Save & connect',
    '删除网络': 'Delete network',
    '删除节点': 'Delete node',
    '取消选择': 'Clear selection',
    '全选节点': 'Select all nodes',
    '新建实验': 'New lab',
    '新建云': 'New cloud',
    '网络名称': 'Network name',
    '节点名称': 'Node name',
    '串口数量': 'Serial ports',
    '网卡数量': 'NIC count',
    '网卡数量（改了会增删接口）': 'NIC count (changing adds/removes interfaces)',
    '出口网卡': 'Uplink NIC',
    '默认路由所在网卡': 'Default-route NIC',
    '开机自动生效': 'Enable on boot',
    '创建 / 更新': 'Create / update',
    '立即修复': 'Fix now',
    '所在目录': 'Folder',
    '接到网络': 'To network',
    '接到哪个接口': 'Which interface',
    '经由网络': 'Via network',
    '擦除配置': 'Wipe config',
    '网桥 +': 'bridge +',
    '连接中…': 'Connecting…',
    '正在停止…': 'Stopping…',
    '加载中…': 'Loading…',
    '加载失败': 'Load failed',
    '上传失败': 'Upload failed',
    '复制失败': 'Copy failed',
    '图标已更新': 'Icon updated',
    '已下发启动': 'Start issued',
    '已停止全部': 'All stopped',
    '已删除网络': 'Network deleted',
    '已删除节点': 'Node deleted',
    '网络已改名': 'Network renamed',
    '网卡接线已保存': 'NIC links saved',
    '节点属性已保存': 'Node properties saved',
    '实验名不能为空': 'Lab name is required',
    '实验名（自动加 .unl）': 'Lab name (.unl added automatically)',
    '先连接服务器': 'Connect to a server first',
    '未连接服务器': 'Not connected to a server',
    '先打开一个实验': 'Open a lab first',
    '从左侧选择实验': 'Choose a lab on the left',
    '未选择实验': 'No lab selected',
    '选中一个节点': 'Select a node',
    '没有任何镜像': 'No images',
    '没有匹配的模板': 'No matching template',
    '留空表示不修改': 'Leave blank to keep current',
    '说明（可留空）': 'Description (optional)',
    '主机 Shell': 'Host shell',
    '剪贴板里没有文本': 'Clipboard has no text',
    '先选中要复制的文字': 'Select some text first',
    '已用系统浏览器打开': 'Opened in system browser',
    '用 Wireshark 打开': 'Open in Wireshark',
    '抓包并用 Wireshark 打开': 'Capture and open in Wireshark',
    '打开图形控制台': 'Open GUI console',
    '打开 CLI 控制台': 'Open CLI console',
    '该节点没有图形控制台': 'This node has no GUI console',
    '这个控制台已经开着了': 'This console is already open',
    '这个节点没有以太网卡': 'This node has no Ethernet interfaces',
    '还没有在抓的会话': 'No active captures',
    '节点没运行，先启动再抓包': 'Node is not running — start it first',
    '打开 EVE 主机 Shell': 'Open EVE host shell',
    '添加节点…': 'Add node…',
    '添加网络（云）…': 'Add network (cloud)…',
    '统一图标…': 'Uniform icon…',
    '批量接线…': 'Bulk connect…',
    '重命名网络…': 'Rename network…',
    '接线…': 'Connect…',
    '接线到网络…': 'Link to network…',
    '抓包…': 'Capture…',
    '图标…': 'Icon…',
    '属性…': 'Properties…',
    '属性（名字 / 内存 / 镜像）…': 'Properties (name / memory / image)…',
    '搜索…': 'Search…',
    '＋ 新建': '+ New',
    '＋ 节点': '+ Node',
    '＋ 新建网络…': '+ New network…',
    '新建节点 ·': 'New node ·',
    '节点属性 ·': 'Node properties ·',
    '新建网络（云）': 'New network (cloud)',
    '接线 ·': 'Connect ·',
    '批量接线（{} 个节点）': 'Bulk connect ({} nodes)',
    '默认（方框）': 'Default (box)',
    '圆形 — 路由器': 'Circle — router',
    '横置长方体 — 交换机': 'Horizontal bar — switch',
    '竖置长方体 — 防火墙': 'Vertical bar — firewall',
    '镜像库': 'Image library',
    '导入镜像…': 'Import image…',
    'IOL 许可证检查': 'IOL license check',
    'IOL 许可证正常': 'IOL license OK',
    'IOL 许可证异常': 'IOL license problem',
    '未安装 IOL 镜像': 'IOL image not installed',
    'IOL 正常（{}）': 'IOL OK ({})',
    'NAT 出口': 'NAT uplink',
    '云编号': 'Cloud no.',
    'HTTP（旧版 EVE，端口一般 80）': 'HTTP (older EVE, usually port 80)',
    'HTTPS（新版 EVE Pro，端口一般 443）': 'HTTPS (newer EVE Pro, usually port 443)',
    'pnet0 · 管理网（直连局域网）': 'pnet0 · management (LAN)',
    'bridge · 内部桥（不连外网）': 'bridge · internal (no internet)',
    'API 端口': 'API port',
    'EVE 账号': 'EVE user',
    'EVE 密码': 'EVE password',
    'SSH 用户': 'SSH user',
    'SSH 密码': 'SSH password',
    'SSH 端口': 'SSH port',
    '实验室 EVE': 'Lab EVE',
    '同时建立 SSH 连接（镜像导入导出、主机终端、网络配置需要）':
      'Also open SSH (needed for image import/export, host shell, network setup)',
    '选中即复制 · 右键粘贴': 'Select to copy · right-click to paste',
    '上下拖动调整高度，双击在「半屏 / 300px」之间切换':
      'Drag to resize; double-click toggles half-screen / 300px',
    '拖到画布放置，或单击直接添加': 'Drag onto the canvas, or click to add',
    '当前目录新建空实验': 'Create an empty lab in this folder',
    '在当前目录新建空实验': 'Create an empty lab in this folder',
    '从模板库新建节点（也可直接拖到画布）': 'Create a node from templates (or drag onto the canvas)',
    '只挂 2 个接口的桥＝直连，默认不画图标；点一下可临时显示出来（便于改名/删除）':
      'A 2-port bridge is a direct link and is hidden by default; click to show it (to rename/delete)',
    '“直连”': 'Direct',
    '（空）': '(free)',
    '（已接）': '(in use)',
    '（未接线）': '(no link)',
    '（图形）': '(GUI)',
    '（新建 {}）': '(new {})',
    '个节点': 'nodes',
    '个节点）': 'nodes)',
    '接口': 'Interface',
    '{} 的接口': '{} interfaces',
    '{} 没有以太网卡': '{} has no Ethernet interfaces',
    '{} 节点 · 运行 {}': '{} nodes · {} running',
    '网卡：{}': 'NICs: {}',
    '网络（{}）': 'Network ({})',
    '{} 个已装镜像）': '{} images installed)',
    '{} {}/{} 个节点': '{} {}/{} nodes',
    '{} {} 个节点？{}': '{} {} nodes?{}',
    '已启动 {}/{} 个节点': 'Started {}/{} nodes',
    '接线 {}/{} 个节点': 'Linked {}/{} nodes',
    '已存：{}': 'Saved: {}',
    '已新建实验 {}': 'Created lab {}',
    '已新建网络 {}': 'Created network {}',
    '已添加节点 {}': 'Added node {}',
    '已复制 {}': 'Copied {}',
    '已粘贴 {}': 'Pasted {}',
    '已连接 {}': 'Connected to {}',
    '已卸载 pnet{}': 'Removed pnet{}',
    '上传中：{}': 'Uploading: {}',
    '开始导出，完成后可在任务里打开': 'Export started; open it from Tasks when done',
    '粘贴失败：{}': 'Paste failed: {}',
    '已选 {} 个节点': '{} nodes selected',
    '已选中 {}': '{} selected',
    '，{} 个失败': ', {} failed',
    '，清理残留网卡 {} 个': ', cleaned {} stale NICs',
    '已删除 {} 个': 'Deleted {}',
    '已停止 {} 个': 'Stopped {}',
    '已启动 {} 个': 'Started {}',
    '已擦除 {} 个': 'Wiped {}',
    '已丢弃': 'Discarded',
    '已复制': 'Copied',
    '已粘贴': 'Pasted',
    '已擦除': 'Wiped',
    '已改名': 'Renamed',
    '已启动': 'Started',
    '已停止': 'Stopped',
    '已卸载': 'Removed',
    '开启': 'On',
    '关闭': 'Off',
    '是': 'Yes',
    '否': 'No',
    '保存': 'Save',
    '关闭窗口': 'Close',
  };

  /* 带变量的长句单独放，方便对照 */
  var EN2 = {
    '已连上 API，但 SSH 不可用：': 'API connected, but SSH unavailable: ',
    'NAT 出口已就绪：': 'NAT uplink ready: ',
    '已写入 iourc，补链': 'iourc written; linked',
    '擦除 {} 的所有配置？': 'Wipe all configuration of {}?',
    '接线 · {} → {}': 'Connect · {} → {}',
    '接线 · {} ─ {}': 'Connect · {} ─ {}',
    '{} 的 {} 已接到 {}': "{}'s {} connected to {}",
    '{}:{} ─ {}:{} 已接': '{}:{} ─ {}:{} connected',
    '不能连自己：拖到另一个节点或网络/云图标上':
      "Can't connect to itself: drop on another node or a network/cloud icon",
    '松手时落在另一个节点或网络/云图标上才会接线':
      'Drop on another node or a network/cloud icon to connect',
    '删除网络 {}？接在上面的网卡会自动断开。':
      'Delete network {}? Attached NICs will be disconnected.',
    '删除节点 {}？\n连带它的配置目录一起删，不可恢复。':
      'Delete node {}?\nIts config directory is removed too — this cannot be undone.',
    '卸载 pnet{} 的 NAT 出口？': 'Remove the NAT uplink on pnet{}?',
    '这个实验里还没有网络/云，先新建一个再接':
      'This lab has no network/cloud yet — create one first',
    '宿主没装这个模板的镜像，先导入镜像':
      'The image for this template is missing — import it first',
    '网络已建好，回到下拉里选它就能接上':
      'Network created — pick it in the dropdown to connect',
    '直连桥已隐藏，线直接连两个节点':
      'Direct bridges hidden; lines run straight between nodes',
    '已显示直连桥（可右键改名/删除）':
      'Direct bridges shown (right-click to rename/delete)',
    '右键节点或检查器里的「抓包」开始':
      'Right-click a node, or use Capture in the inspector',
    '开始抓包，正在拉起 Wireshark…': 'Capturing — launching Wireshark…',
    'Wireshark 已打开，包里实时滚': 'Wireshark is open and streaming live',
    '抓包中 · Wireshark 已打开（窗口里实时滚包）':
      'Capturing · Wireshark is open and streaming',
    '抓包中（没找到 Wireshark，只在本机存文件）':
      'Capturing (Wireshark not found; saving to a local file)',
    '已停止，共 {} 个包（{}），文件已存到本机':
      'Stopped: {} packets ({}), saved locally',
    '个包 · {} · {}s{}': 'packets · {} · {}s{}',
    '已创建 {} → {}（网关 {}，出口 {}）\n':
      'Created {} → {} (gateway {}, uplink {})\n',
    '已写入 systemd，开机自动生效\n':
      'Written to systemd; enabled on boot\n',
    '注意：未持久化，重启后失效\n':
      'Note: not persistent — lost on reboot\n',
    'IOL 异常：iourc {}，缺授权节点 {}':
      'IOL problem: iourc {}, unlicensed nodes {}',
    'iourc {} · 节点缺授权 {}': 'iourc {} · nodes missing license {}',
    '{} · EVE {} · 仅 API': '{} · EVE {} · API only',
    '＋ 新建内部桥（{}，放两节点中间）':
      '+ New internal bridge ({} between the two nodes)',
    '在独立窗口打开（Ctrl+Alt+V 可粘贴）':
      'Open in a separate window (Ctrl+Alt+V to paste)',
    '已在新窗口打开 · 右下角有「粘贴 / 键入」，Ctrl+Alt+T 逐字键入':
      'Opened in a new window · Paste / Type buttons at bottom-right; Ctrl+Alt+T types characters',
    '宿主机网络信息需要 SSH 连接 —— 请到「设置」填写 root 密码后重新连接':
      'Host network info needs SSH — set the root password in Settings and reconnect',
    '镜像导入/导出需要 SSH 连接 —— 请到「设置」填写 root 密码后重新连接':
      'Image import/export needs SSH — set the root password in Settings and reconnect',
    '请到「设置」填写服务器并连接':
      'Fill in a server in Settings and connect',
    '这是一个网络/云，节点网卡接上来即可互通。想看某个接口的流量，选中节点后在「网卡」里点「抓包」。':
      'This is a network/cloud: attach a node NIC to it and they can talk. To watch one interface, select the node and press Capture under NICs.',
    '停止选中的 {} 个': 'Stop {} selected',
    '启动选中的 {} 个': 'Start {} selected',
    '删除选中的 {} 个': 'Delete {} selected',
    '擦除选中的 {} 个': 'Wipe {} selected',
    '拖到画布放置，或单击直接添加（{} 个模板，':
      'Drag onto the canvas, or click to add ({} templates, ',
    '节点图标（': 'Node icon (',
    '个节点）': 'nodes)',

    /* 拓扑文本框 */
    '＋ 文本': '+ Text',
    '添加文本框': 'Add text boxes',
    '添加文本框…': 'Add text boxes…',
    '文本框内容（每行一个）': 'Text boxes (one per line)',
    '字号': 'Font size',
    '小': 'Small',
    '中': 'Medium',
    '大': 'Large',
    '颜色': 'Color',
    '默认': 'Default',
    '蓝色': 'Blue',
    '绿色': 'Green',
    '橙色': 'Orange',
    '红色': 'Red',
    '编辑文字': 'Edit text',
    '复制文字': 'Copy text',
    '删除文本框': 'Delete text box',
    '已添加 {} 个文本框': 'Added {} text boxes',
    '没有可用的文本：每一行写一个文本框': 'Nothing to add — put one text box per line',

    /* 按钮 tooltip（index.html 里 title= 的那几条） */
    '核心区\n接入层\n服务器区': 'Core\nAccess\nServers',
    '在拓扑上放文本框（也可以直接在画布空白处右键）':
      'Drop text boxes on the topology (or right-click empty canvas)',
    '启动全部节点': 'Start all nodes',
    '停止全部节点': 'Stop all nodes',

    /* 后端错误：这些串是 Python 抛上来的，经 toast 显示，翻译放前端最省事 */
    '尚未连接服务器': 'Not connected to a server',
    '请填写服务器地址': 'Please fill in the server address',
    '请填实验名': 'Please fill in the lab name',
    '连接失败': 'Connection failed',
    '连接失败：{}': 'Connection failed: {}',
    'SSH 认证失败：用户名或密码不对': 'SSH authentication failed: wrong user or password',
    'SSH 连接失败：{}': 'SSH connection failed: {}',
    '命令执行失败：{}': 'Command failed: {}',
    '缺少参数：{}': 'Missing parameter: {}',
    '缺少参数：实验路径': 'Missing parameter: lab path',
    '缺少节点 id': 'Missing node id',
    '缺少节点 id，无法计算控制台端口': 'Missing node id; cannot resolve the console port',
    '缺少控制台地址': 'Missing console URL',
    '新增节点缺少模板': 'Creating a node requires a template',
    '实验名里不能带斜杠': 'Lab name cannot contain a slash',
    '实验已被别处修改（EVE 网页版或另一个窗口？），请先点「重载」再操作':
      'The lab was modified elsewhere (EVE web UI or another window). Click Reload first.',
    '新建网络没成功：EVE 没有返回新网络': 'Network creation failed: EVE returned no new network',
    '接口 {} 不存在（这个节点有：{}）': 'Interface {} does not exist (this node has: {})',
    '接线没生效：网络 {} 上只挂了 {} 个接口（应为 2）':
      'Link did not take effect: network {} only has {} interfaces (expected 2)',
    '节点 {} 没有编号 {} 的网卡': 'Node {} has no NIC numbered {}',
    '节点 {} 没有网卡 {}（可选：{}）': 'Node {} has no interface {} (available: {})',
    '节点 {} 还没启动（控制台端口 {} 未监听），先在右侧面板点「启动」':
      'Node {} is not running yet (console port {} not listening) — press Start in the right panel',
    '节点 {} 还没运行，网卡上抓不到东西：先启动再抓包':
      'Node {} is not running, so there is nothing to capture — start it first',
    '同一个节点上的两个网卡直接相连没有意义':
      'Two NICs on the same node cannot be connected directly',
    'pnet0 是管理网，不允许改成 NAT 出口':
      'pnet0 is the management network and cannot be used as a NAT uplink',
    '该操作需要 SSH 连接（请在连接设置里填写 SSH 账号）':
      'This action needs SSH (fill in the SSH account under Settings)',
    '需要 SSH 连接才能打开主机终端': 'A host shell needs an SSH connection',
    '该节点没有图形控制台地址': 'This node has no GUI console URL',
    '还没保存 EVE 密码：请在「连接设置」里填写账号密码后重试':
      'No EVE password saved yet — fill in the account under Settings and retry',
    '返回不是 JSON（HTTP {}）{}': 'Response was not JSON (HTTP {}) {}',
    '这个控制台地址不属于当前服务器（{} ≠ {}）':
      'This console URL belongs to a different server ({} ≠ {})',
    '远端端口 {} 不可达：{}': 'Remote port {} unreachable: {}',
    '连不上控制台端口 {}（{}）。节点刚启动时要等几秒；':
      'Cannot reach console port {} ({}). A node needs a few seconds after start;',
    '会话被其它客户端顶掉（EVE 同一账号只允许一个会话）：{}':
      'Session was taken over by another client (EVE allows one session per account): {}',
    '当前是「仅服务」模式：请在浏览器里打开，或直接启动图形版':
      'Running in server-only mode: open it in a browser, or start the GUI build',

    /* 后端日志（设置页底部那几条最常见的） */
    '已连接 {}（EVE {} · 仅 API）': 'Connected to {} (EVE {} · API only)',
    '已连接 {}（EVE {} · SSH 可用）': 'Connected to {} (EVE {} · SSH available)',
    '新建实验 {}': 'Created lab {}',
    '已记住 {} 使用 {}': 'Remembered: {} uses {}',
    '导入完成：{}': 'Import finished: {}',
    '导入失败：{}': 'Import failed: {}',
    '图形控制台：这台服务器的自签证书本次未放行，重启客户端后生效（CLI 控制台不受影响）':
      'GUI console: this server\'s self-signed certificate is not trusted yet — restart the client to apply (CLI console is unaffected)',
  };

  var DICT = Object.assign({}, EN, EN2);

  /* ---------------- 匹配引擎 ---------------- */
  function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  var pats = null;
  function buildPats() {
    pats = [];
    Object.keys(DICT).forEach(function (k) {
      if (k.indexOf('{}') < 0) return;
      var parts = k.split('{}');
      pats.push({
        re: new RegExp('^' + parts.map(escRe).join('(.+?)') + '$'),
        v: DICT[k], len: k.length,
      });
    });
    // 长的优先，避免「{} 个节点」抢在「{} 个已装镜像）」前面
    pats.sort(function (a, b) { return b.len - a.len; });
  }

  function tr(s) {
    if (lang === 'zh' || !s || !CJK.test(s)) return s;
    if (Object.prototype.hasOwnProperty.call(DICT, s)) return DICT[s];
    if (!pats) buildPats();
    for (var i = 0; i < pats.length; i++) {
      var m = pats[i].re.exec(s);
      if (m) {
        var n = 0;
        return pats[i].v.replace(/\{\}/g, function () { n += 1; return m[n] || ''; });
      }
    }
    return s;
  }

  /* ---------------- DOM 扫描 ---------------- */
  var origText = new WeakMap();     // 文本节点 → 中文原文
  var origAttr = new WeakMap();     // 元素 → {title: 原文, ...}
  var ATTRS = ['title', 'placeholder', 'aria-label'];

  function doText(node) {
    var cur = node.data;
    var rec = origText.get(node);
    if (rec === undefined) {
      if (!CJK.test(cur)) return;
      origText.set(node, cur);
      rec = cur;
    }
    var m = /^(\s*)([\s\S]*?)(\s*)$/.exec(rec);
    if (!m) return;
    var want = m[1] + (lang === 'zh' ? m[2] : tr(m[2])) + m[3];
    if (node.data !== want) node.data = want;
  }

  function doAttr(el) {
    var rec = origAttr.get(el);
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      if (!el.hasAttribute(a)) continue;
      var cur = el.getAttribute(a);
      if (!rec) { rec = {}; origAttr.set(el, rec); }
      if (!(a in rec)) {
        if (!CJK.test(cur)) continue;
        rec[a] = cur;
      }
      var want = lang === 'zh' ? rec[a] : tr(rec[a]);
      if (cur !== want) el.setAttribute(a, want);
    }
  }

  function localizeNode(n) {
    if (!n) return;
    if (n.nodeType === 3) { doText(n); return; }
    if (n.nodeType !== 1) return;
    var tag = n.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK') return;
    if (n.hasAttribute && n.hasAttribute('data-notrans')) return;
    doAttr(n);
    var c = n.firstChild;
    while (c) { var next = c.nextSibling; localizeNode(c); c = next; }
  }

  var obs = null;
  function watch() {
    if (obs || !window.MutationObserver || !document.body) return;
    obs = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type === 'childList') {
          for (var j = 0; j < m.addedNodes.length; j++) localizeNode(m.addedNodes[j]);
        } else if (m.type === 'attributes') {
          doAttr(m.target);
        }
      }
    });
    obs.observe(document.body, {
      childList: true, subtree: true, attributes: true, attributeFilter: ATTRS,
    });
  }

  /* ---------------- 状态 ---------------- */
  var lang = 'zh';

  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function read(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  function paintBtns() {
    var lb = document.getElementById('btnLang');
    var tb = document.getElementById('btnTheme');
    var light = document.documentElement.getAttribute('data-theme') === 'light';
    if (lb) {
      lb.textContent = lang === 'zh' ? 'EN' : '中文';
      lb.title = lang === 'zh' ? 'Switch to English' : '切换到中文';
    }
    if (tb) {
      // 用文字而不是 ☀/☾：这两个符号在 12px 下几乎看不清
      tb.textContent = light ? (lang === 'zh' ? '暗色' : 'Dark')
                             : (lang === 'zh' ? '亮色' : 'Light');
      tb.title = light
        ? (lang === 'zh' ? '切换到暗色主题' : 'Switch to dark theme')
        : (lang === 'zh' ? '切换到亮色主题' : 'Switch to light theme');
      tb.setAttribute('data-notrans', '');
    }
  }

  function setLang(next, save) {
    lang = next === 'en' ? 'en' : 'zh';
    document.documentElement.setAttribute('data-lang', lang);
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    if (save !== false) store('eve.lang', lang);
    if (document.body) localizeNode(document.body);
    paintBtns();
  }

  function setTheme(next, save) {
    var t = next === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
    if (save !== false) store('eve.theme', t);
    paintBtns();
  }

  function toggleLang() { setLang(lang === 'zh' ? 'en' : 'zh'); }
  function toggleTheme() {
    setTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light');
  }

  /* ---------------- 启动 ---------------- */
  function boot() {
    setLang(read('eve.lang') === 'en' ? 'en' : 'zh', false);
    setTheme(read('eve.theme') === 'light' ? 'light' : 'dark', false);
    localizeNode(document.body);
    watch();
    var lb = document.getElementById('btnLang');
    var tb = document.getElementById('btnTheme');
    if (lb) lb.onclick = toggleLang;
    if (tb) tb.onclick = toggleTheme;
    paintBtns();
  }

  window.I18N = {
    t: tr,
    lang: function () { return lang; },
    setLang: setLang,
    setTheme: setTheme,
    toggleLang: toggleLang,
    toggleTheme: toggleTheme,
    localize: localizeNode,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
