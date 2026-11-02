'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const S = {
  info: null,
  profiles: [],
  cwd: '',
  labs: { folders: [], labs: [] },
  labPath: '',
  lab: null,
  sel: null,
  terms: [],
  tasks: {},
  z: 1, tx: 0, ty: 0,
  poll: null,
  icons: {},
  umtime: null,        // 实验文件时间戳：写操作前比对，防止盖掉别处的修改
  tpl: [],             // 模板库
  caps: [],            // 正在抓的会话
  capTimer: null,
  lastPcap: '',
  selIds: [],          // 选中的节点 id（支持多选，顺序即点选顺序）
  linking: null,       // Shift 拖线接线的临时状态
  ifaces: {},          // {节点id: [{name, network_id, id}]}，给链路标注接口名用
  showP2P: false,      // 是否把「只挂 2 个接口的直连桥」也画成图标（默认隐藏）
  texts: [],           // 拓扑文本框（存在本机，不进 EVE 的 .unl）
  tbxSel: [],          // 选中的文本框 id（框选/加选，可与节点各选各的）
  viewReady: false,    // 当前实验的视角是否已定（定了就别再自动 fit，免得冲掉用户调的位置）
};

/* ------------------------------------------------------------------ 工具 */
async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  let j;
  try { j = await r.json(); } catch (e) { j = { ok: false, error: 'HTTP ' + r.status }; }
  if (!j.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

let toastTimer = null;
function toast(msg, kind = '', ms = 2600) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast on ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast ' + kind; }, ms);
}
function fail(e) { toast(e.message || String(e), 'err'); console.error(e); }

function isRunning(n) {
  // EVE 节点状态：0=停止 1=就绪 2=运行 3=运行且锁定
  return !!n && Number(n.status) >= 2;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function bytes(n) {
  n = Number(n) || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + ' ' + u[i];
}
function b64bytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/* ------------------------------------------------------------------ 弹窗 */
function modal(title, fields) {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'modal-mask';
    const rows = fields.map((f, i) => {
      if (f.type === 'select') {
        const opts = f.options.map(o =>
          `<option value="${esc(o.value)}"${o.value == f.value ? ' selected' : ''}>${esc(o.label)}</option>`).join('');
        return `<label>${esc(f.label)}<select data-i="${i}">${opts}</select></label>`;
      }
      return `<label>${esc(f.label)}<input data-i="${i}" type="${f.type || 'text'}"
        value="${esc(f.value == null ? '' : f.value)}"></label>`;
    }).join('');
    wrap.innerHTML = `<div class="modal"><h3>${esc(title)}</h3><div class="mbody">${rows}</div>
      <div class="mfoot"><button class="btn ghost" data-x="0">取消</button>
      <button class="btn" data-x="1">确定</button></div></div>`;
    document.body.appendChild(wrap);
    const first = wrap.querySelector('input,select');
    if (first) { first.focus(); first.select && first.select(); }
    const done = ok => {
      const out = {};
      if (ok) wrap.querySelectorAll('[data-i]').forEach(el => { out[fields[+el.dataset.i].key] = el.value; });
      wrap.remove();
      resolve(ok ? out : null);
    };
    wrap.addEventListener('click', e => {
      const b = e.target.closest('[data-x]');
      if (b) done(b.dataset.x === '1');
      else if (e.target === wrap) done(false);
    });
    wrap.addEventListener('keydown', e => {
      if (e.key === 'Enter') done(true);
      if (e.key === 'Escape') done(false);
    });
  });
}

/* ------------------------------------------------------------------ 连接 */
function renderConn() {
  const dot = $('#connDot'), txt = $('#connText');
  if (!S.info || !S.info.host) {
    dot.className = 'dot off'; txt.textContent = '未连接';
  } else if (S.info.ssh) {
    dot.className = 'dot on';
    txt.textContent = `${S.info.host} · EVE ${S.info.version} · SSH`;
  } else {
    dot.className = 'dot warn';
    txt.textContent = `${S.info.host} · EVE ${S.info.version} · 仅 API`;
  }
}

async function connect(payload) {
  toast('连接中…');
  const r = await api('/api/connect', { method: 'POST', body: payload });
  S.info = r.info;
  renderConn();
  if (!r.info.ssh && r.info.ssh_error) {
    toast('已连上 API，但 SSH 不可用：' + r.info.ssh_error, 'err');
  } else {
    toast('已连接 ' + r.info.host, 'ok');
  }
  await loadLabs('');
  loadImages().catch(() => {});
  loadNet().catch(() => {});
  renderLogs();
}

async function boot() {
  try {
    let st = await api('/api/state');
    S.info = st.info && st.info.host ? st.info : null;
    // 设置页先填好再等连接：自动连接可能要等几秒，别让用户在这个空档
    // 看到「80 / HTTP」这种默认值，误以为配置丢了（连 Pro 时尤其误导）。
    S.profiles = st.profiles || [];
    renderProfiles(st.active);
    // 启动时的「自动连接」在后台线程里跑，页面往往比它先就绪 —— 等一会儿，
    // 否则用户看到的是「未连接」然后一直不动（旧版本就卡在这儿）。
    for (let i = 0; !S.info && i < 20; i++) {
      await new Promise(r => setTimeout(r, 500));
      st = await api('/api/state');
      S.info = st.info && st.info.host ? st.info : null;
    }
    renderConn();
    renderLogs(st.logs);
    if (S.info) { await loadLabs(''); }
    else { $('#labTree').innerHTML = '<div class="empty">请到「设置」填写服务器并连接</div>'; }
    const h0 = location.hash || '';
    const vw = /view=(\w+)/.exec(h0);
    if (vw) switchView(vw[1]);
    const deep = /lab=([^&]+)/.exec(h0);
    if (S.info && deep) {
      const wantTerm = /term=(\d+)/.exec(h0);
      await openLab(decodeURIComponent(deep[1]));
      const wantSel = /sel=(\d+)/.exec(h0);
      if (wantSel) selectNode(wantSel[1]).catch(() => {});
      if (wantTerm) {
        const n = (S.lab.nodes || {})[wantTerm[1]];
        openNodeTerm(wantTerm[1], n ? n.name : 'node-' + wantTerm[1]);
      }
      const wantH5 = /html5=(\d+)/.exec(h0);
      if (wantH5) {
        const n = (S.lab.nodes || {})[wantH5[1]] || {};
        const d = await api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${wantH5[1]}`);
        openHtml5(d.html5, n.name || ('node-' + wantH5[1]));
      }
    }
  } catch (e) { fail(e); }
}

function renderLogs(logs) {
  const el = $('#logs');
  if (!logs) return;
  el.innerHTML = logs.slice(-40).reverse().map(l =>
    `<div>[${new Date(l.t * 1000).toLocaleTimeString()}] ${esc(l.msg)}</div>`).join('');
}

/* ------------------------------------------------------------------ 实验树 */
async function loadLabs(path) {
  S.cwd = path || '';
  const d = await api('/api/labs?path=' + encodeURIComponent(S.cwd));
  S.labs = d;
  renderLabs();
}

function renderLabs() {
  const tree = $('#labTree');
  const parts = S.cwd ? S.cwd.split('/') : [];
  $('#crumbs').innerHTML = '<a data-p="">根目录</a>' + parts.map((p, i) =>
    ' / <a data-p="' + esc(parts.slice(0, i + 1).join('/')) + '" data-notrans>' + esc(p) + '</a>').join('');

  let html = '';
  if (S.cwd) html += `<div class="lab-item" data-up="1"><span class="ico">↑</span>上一级</div>`;
  (S.labs.folders || []).forEach(f => {
    html += `<div class="lab-item" data-dir="${esc(f.path || f.name)}">
      <span class="ico">▸</span><span data-notrans>${esc(f.name)}</span></div>`;
  });
  (S.labs.labs || []).forEach(l => {
    const on = l.path === S.labPath ? ' on' : '';
    html += `<div class="lab-item${on}" data-lab="${esc(l.path)}">
      <span class="ico">◈</span><span data-notrans>${esc(l.name)}</span>
      <span class="tag">${esc((l.mtime || '').slice(0, 11))}</span></div>`;
  });
  tree.innerHTML = html || '<div class="empty">空目录</div>';
}

/* 新建空实验：建在当前浏览的目录下，建好直接打开 */
async function newLab() {
  if (!S.info) { toast('先连接服务器', 'warn'); return; }
  const f = await modal('新建实验', [
    { key: 'name', label: '实验名（自动加 .unl）', type: 'text', value: '' },
    { key: 'folder', label: '所在目录', type: 'text', value: S.cwd || '', },
    { key: 'description', label: '说明（可留空）', type: 'text', value: '' },
  ]);
  if (!f) return;
  const name = (f.name || '').trim();
  if (!name) { toast('实验名不能为空', 'warn'); return; }
  const r = await api('/api/lab/new', {
    method: 'POST',
    body: { name, folder: (f.folder || '').trim(), description: f.description || '' },
  });
  toast('已新建实验 ' + r.path, 'ok');
  await loadLabs(S.cwd);
  await openLab(r.path);
}

/* ------------------------------------------------------------------ 节点图标 */
/* 图标只影响画布上怎么画，不改 EVE 的数据（EVE 的 icon 是服务器上的 SVG 文件，
   我们这边是自绘节点，所以按「实验 + 节点 id」存在浏览器本地）。
   三种形状：圆形＝路由器 / 横置长方体＝交换机 / 竖置长方体＝防火墙。*/
const ICO_PREFIX = 'eve.icons.';

function iconMap() {
  try { return JSON.parse(localStorage.getItem(ICO_PREFIX + (S.labPath || '')) || '{}'); }
  catch (e) { return {}; }
}

function setNodeIcon(ids, shape) {
  const m = iconMap();
  (ids || []).forEach(id => { if (shape) m[id] = shape; else delete m[id]; });
  try { localStorage.setItem(ICO_PREFIX + (S.labPath || ''), JSON.stringify(m)); } catch (e) {}
  renderTopology();
}

async function pickIcon(ids) {
  ids = ids || (S.selIds || []);
  if (!ids.length) return;
  const cur = ids.length === 1 ? (iconMap()[ids[0]] || '') : '';
  const f = await modal('节点图标（' + ids.length + ' 个节点）', [{
    key: 'shape', label: '图形', type: 'select', value: cur,
    options: [
      { value: '', label: '默认（方框）' },
      { value: 'router', label: '圆形 — 路由器' },
      { value: 'switch', label: '横置长方体 — 交换机' },
      { value: 'fw', label: '竖置长方体 — 防火墙' },
    ],
  }]);
  if (!f) return;
  setNodeIcon(ids, f.shape);
  toast('图标已更新', 'ok');
}

/* ------------------------------------------------------------------ 拓扑 */
async function openLab(path) {
  S.labPath = path;
  S.sel = null;
  S.selIds = [];
  S.ifaces = {};           // 旧实验的接口信息不能留在新画布上
  S.texts = [];            // 旧实验的文本框同理（异步拉回来后再画）
  S.tbxSel = [];           // 切换实验时清掉文本框选择
  try { history.replaceState(null, '', '#lab=' + encodeURIComponent(path)); } catch (e) {}
  const d = await api('/api/lab?path=' + encodeURIComponent(path));
  S.lab = d;
  S.umtime = d.umtime;
  setLabTitle(d);
  $('#stageHint').style.display = 'none';
  renderInspector(null);
  // 视角按实验各记一份：上次调到哪就回到哪，只有第一次打开才自动适应窗口
  S.viewReady = loadView();
  renderTopology();
  paintSel();
  renderLabs();
  startPoll();
  loadIfaces();
  loadTexts();
}

/* 标题 = 实验名（用户数据，不翻译）+ 节点统计（走词典） */
function setLabTitle(d) {
  const name = (d && d.meta && (d.meta.name || S.labPath)) || S.labPath || '';
  const total = (d && d.total) || 0, running = (d && d.running) || 0;
  $('#labTitle').innerHTML = `<span data-notrans>${esc(name)}</span>　${total} 节点 · 运行 ${running}`;
}

/* 把整个实验的网卡拉回来：只用来给「已接线的链路」标接口名 */
async function loadIfaces() {
  const p = S.labPath;
  try {
    const d = await api('/api/lab/ifaces?path=' + encodeURIComponent(p));
    if (p !== S.labPath) return;            // 期间切了实验就别覆盖
    S.ifaces = d.nodes || {};
    drawLinks();                            // 有了接口信息才画得出标签
  } catch (e) { /* 接口信息拿不到就只少了标签，不影响其它功能 */ }
}

/* 某节点接到某网络上的接口名 */
function ifacesOnNet(nodeId, netId) {
  const arr = (S.ifaces || {})[String(nodeId)] || [];
  return arr.filter(it => it.network_id && String(it.network_id) === String(netId))
            .map(it => it.name);
}

function startPoll() {
  stopPoll();
  S.poll = setInterval(async () => {
    if (!S.labPath || document.hidden) return;
    try {
      const d = await api('/api/lab?path=' + encodeURIComponent(S.labPath));
      S.lab = d;
      $$('.nd').forEach(el => {
        const n = d.nodes[el.dataset.id];
        if (n) el.classList.toggle('running', isRunning(n));
      });
      setLabTitle(d);
    } catch (e) { /* 静默 */ }
  }, 3000);
}
function stopPoll() { if (S.poll) { clearInterval(S.poll); S.poll = null; } }

function endpointPos(kind, id) {
  const L = S.lab;
  if (!L) return null;
  if (kind === 'node') {
    const n = L.nodes[id];
    if (!n) return null;
    return { x: Number(n.left || 0), y: Number(n.top || 0) };
  }
  const n = L.networks[id];
  if (!n) return null;
  return { x: Number(n.left || 0), y: Number(n.top || 0) };
}

/* 云/物理网卡这类网络永远当图标显示，不会被当成「直连」隐掉 */
function isCloudNet(t) {
  return /^pnet\d+$|^ovs$|^cloud/.test(t || '');
}

/* 哪些网络是「纯直连」：只挂 2 个接口、又不是云。
   EVE 自己就是这么判的（api_topology.php：count==2 且非云且 visibility!=1
   时输出 node→node 的边，不画网络图标），这里照抄，另外把 visibility=1 的
   也算进来——否则界面上会出现「两个节点中间夹一个多余的 Net-N 图标」。*/
function p2pNetIds() {
  const out = new Set();
  const L = S.lab;
  if (!L) return out;
  const cnt = {};
  const add = nid => {
    const k = String(nid || '0');
    if (k && k !== '0') cnt[k] = (cnt[k] || 0) + 1;
  };
  const ifs = S.ifaces || {};
  if (Object.keys(ifs).length) {
    Object.values(ifs).forEach(list => (list || []).forEach(it => add(it.network_id)));
  } else {
    // 接口信息还没拉回来时，用拓扑里的 node→network 边反推（一条边就是一个接口）
    (L.links || []).forEach(l => {
      [[l.source, l.source_type], [l.destination, l.destination_type]]
        .forEach(([v, t]) => {
          const s = String(v == null ? '' : v);
          if (String(t || '').toLowerCase() === 'network' || /^network/i.test(s)) {
            add(s.replace(/\D/g, ''));
          }
        });
    });
  }
  Object.entries(L.networks || {}).forEach(([id, n]) => {
    if (isCloudNet(n.type)) return;
    if (cnt[String(id)] === 2) out.add(String(id));
  });
  return out;
}

function renderTopology() {
  const L = S.lab;
  const nodesEl = $('#nodes'), linksEl = $('#links'), world = $('#world');
  nodesEl.innerHTML = '';
  linksEl.innerHTML = '';
  if (!L) return;

  const pts = {};
  const icons = iconMap();            // 每个实验各自的节点图标偏好
  Object.entries(L.nodes || {}).forEach(([id, n]) => {
    pts['node' + id] = { x: Number(n.left || 0), y: Number(n.top || 0), node: n, id };
    const el = document.createElement('div');
    const shape = icons[id] ? ' ico-' + icons[id] : '';
    el.className = 'nd' + shape + (isRunning(n) ? ' running' : '');
    el.dataset.id = id;
    el.style.left = (Number(n.left || 0)) + 'px';
    el.style.top = (Number(n.top || 0)) + 'px';
    const glyph = (n.name || '?').slice(0, 5);
    el.innerHTML = `<div class="nd-box"><span class="nd-glyph">${esc(glyph)}</span>
        <span class="nd-dot"></span></div>
      <div class="nd-name" title="${esc(n.name)} · ${esc(n.template || n.type)}">${esc(n.template || n.type || '')}</div>`;
    nodesEl.appendChild(el);
  });

  Object.entries(L.networks || {}).forEach(([id, n]) => {
    pts['network' + id] = { x: Number(n.left || 0), y: Number(n.top || 0), net: n, id };
  });

  const p2p = S.showP2P ? new Set() : p2pNetIds();
  Object.entries(L.networks || {}).forEach(([id, n]) => {
    if (String(n.visibility) === '0' && !isCloudNet(n.type)) return;
    if (p2p.has(String(id))) return;        // 纯直连桥：不画图标，线直接连两个节点
    const el = document.createElement('div');
    el.className = 'net' + (isCloudNet(n.type) ? ' cloud' : '');
    el.dataset.netId = id;
    el.style.left = (Number(n.left || 0)) + 'px';
    el.style.top = (Number(n.top || 0)) + 'px';
    el.textContent = (n.name || n.type || '').slice(0, 7);
    el.title = n.name + ' · ' + n.type;
    nodesEl.appendChild(el);
  });

  // 第一次画这个实验才定视角；之后（改图标、重载）保持用户自己调好的位置
  if (!S.viewReady) { fit(); S.viewReady = true; }
  drawLinks();
  applyT();
  renderTexts();
}

/* ------------------------------------------------------------------ 文本框
   文本框不是 EVE 的对象（.unl 里只有 node / network），往实验文件里塞自定义
   元素会被 EVE 静默丢掉，所以存本机侧车文件，走 /api/textboxes。
   画布上就是一组可拖动、双击可改字的 div，坐标与节点同一套世界坐标。   */
function textBoxId() {
  return Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4);
}

function renderTexts() {
  const host = $('#nodes');
  if (!host) return;
  $$('.tbx').forEach(el => el.remove());
  (S.texts || []).forEach(t => {
    const el = document.createElement('div');
    el.className = 'tbx s' + (t.size || '2') + (t.color ? ' c-' + t.color : '');
    el.dataset.tid = t.id;
    el.style.left = Number(t.x || 0) + 'px';
    el.style.top = Number(t.y || 0) + 'px';
    el.textContent = t.text;              // 用户自己写的字，不翻译
    el.setAttribute('data-notrans', '');
    host.appendChild(el);
  });
  paintTbxSel();
}

function paintTbxSel() {
  const ids = S.tbxSel || [];
  $$('.tbx').forEach(el => el.classList.toggle('sel', ids.includes(el.dataset.tid)));
}

function deleteSelectedTexts() {
  const ids = S.tbxSel || [];
  if (!ids.length) return;
  S.texts = (S.texts || []).filter(t => !ids.includes(t.id));
  S.tbxSel = [];
  renderTexts();
  saveTexts(true);
  toast(`已删除 ${ids.length} 个文本框`, 'ok');
}

async function loadTexts() {
  const p = S.labPath;
  if (!p) { S.texts = []; renderTexts(); return; }
  try {
    const d = await api('/api/textboxes?path=' + encodeURIComponent(p));
    if (p !== S.labPath) return;           // 期间切了实验就别覆盖
    S.texts = d.items || [];
  } catch (e) {
    S.texts = [];
  }
  renderTexts();
}

let textsTimer = null;
function saveTexts(now) {
  const path = S.labPath;
  if (!path) return Promise.resolve();
  const snapshot = (S.texts || []).map(t => ({ ...t }));   // 拖拽时对象还在改，先快照
  const push = () => api('/api/textboxes', { method: 'PUT', body: { path, items: snapshot } })
    .catch(fail);
  clearTimeout(textsTimer);
  if (now) return push();
  textsTimer = setTimeout(push, 400);      // 连续拖动时合并成一次写盘
  return Promise.resolve();
}

/* 添加文本框：一个输入框 = 一个文本框，点「增加」一次追加两个，想要多少加多少 */
function textBoxDialog() {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'modal-mask';
    wrap.innerHTML = `<div class="modal wide"><h3>添加文本框</h3>
      <div class="mbody">
        <div class="mlabel">文本框内容</div>
        <div class="tbxlist" id="tbxList"></div>
        <button class="btn sm ghost tbxmore" id="tbxMore"
          title="再加两个文本输入框">＋ 增加</button>
        <p class="mhint"><span class="L-zh">一个输入框会变成<b>一个独立文本框</b>，框内可以按回车换行，
          空的自动忽略；点「增加」一次加两个，想要多少个就加多少个，<code>Ctrl+Enter</code> 提交。
          放好后可拖动位置、双击改字、右键删除。</span>
          <span class="L-en">Each input becomes <b>a separate text box</b> and can hold several
          lines — just press Enter inside it; empty ones are skipped. Click Add to get two more
          inputs, as many times as you like; <code>Ctrl+Enter</code> submits. Afterwards drag them
          around, double-click to edit, or right-click to delete.</span></p>
        <div class="mrow">
          <label>字号<select id="tbxSize">
            <option value="1">小</option><option value="2" selected>中</option><option value="3">大</option>
          </select></label>
          <label>颜色<select id="tbxColor">
            <option value="">默认</option><option value="accent">蓝色</option>
            <option value="ok">绿色</option><option value="warn">橙色</option><option value="err">红色</option>
          </select></label>
        </div>
      </div>
      <div class="mfoot"><button class="btn ghost" data-x="0">取消</button>
        <button class="btn" data-x="1">确定</button></div></div>`;
    document.body.appendChild(wrap);

    const list = wrap.querySelector('#tbxList');
    const rows = () => [...list.querySelectorAll('.tbxrow')];
    const inputs = () => [...list.querySelectorAll('.tbxinput')];

    // 只剩一行时藏掉删除键，免得把弹窗删成空的
    const tidy = () => {
      const n = rows().length;
      rows().forEach(r => { r.querySelector('.tbxdel').style.visibility = n > 1 ? '' : 'hidden'; });
    };

    const addRows = n => {
      for (let i = 0; i < n; i++) {
        const row = document.createElement('div');
        row.className = 'tbxrow';
        row.innerHTML = '<textarea class="tbxinput" rows="2" placeholder="输入文字…"></textarea>' +
          '<button class="tbxdel" tabindex="-1" title="删除这一行">×</button>';
        list.appendChild(row);
      }
      tidy();
      const all = rows();
      const first = all[all.length - n];
      if (first) first.querySelector('textarea').focus();
      list.scrollTop = list.scrollHeight;
    };

    addRows(2);

    list.addEventListener('click', e => {
      const d = e.target.closest('.tbxdel');
      if (!d || rows().length <= 1) return;
      d.closest('.tbxrow').remove();
      tidy();
      const all = inputs();
      if (all.length) all[all.length - 1].focus();
    });

    const done = ok => {
      let out = null;
      if (ok) {
        out = {
          lines: inputs().map(i => i.value.trim()).filter(Boolean),
          size: wrap.querySelector('#tbxSize').value,
          color: wrap.querySelector('#tbxColor').value,
        };
      }
      wrap.remove();
      resolve(out);
    };

    wrap.addEventListener('click', e => {
      if (e.target.closest('#tbxMore')) { addRows(2); return; }   // 「增加」只加框，不关弹窗
      const b = e.target.closest('[data-x]');
      if (b) done(b.dataset.x === '1');
      else if (e.target === wrap) done(false);
    });

    wrap.addEventListener('keydown', e => {
      if (e.isComposing) return;                // 输入法选词的回车别当提交
      if (e.key === 'Escape') done(false);
      // 敲回车照常换行（每个框里可以写多行），Ctrl+Enter 才提交
      else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) done(true);
    });
  });
}

/* 一次放多个：按列铺开，避免全叠在一个点上 */
async function addTextBoxes(x, y) {
  if (!S.labPath) return toast('先打开一个实验', 'warn');
  const f = await textBoxDialog();
  if (!f) return;
  const lines = f.lines || [];
  if (!lines.length) return toast('没有可用的文本：至少填一个输入框', 'warn');
  // 框里可能有多行，按内容实际高度往下累加，否则会上下叠在一起
  const size = f.size || '2';
  const lh = { '1': 17, '2': 20, '3': 24 }[size] || 20;
  const perCol = 8, colW = 220;
  const baseX = Number.isFinite(x) ? x : 320;
  const baseY = Number.isFinite(y) ? y : 320;
  for (let i = 0; i < lines.length; i += perCol) {
    let top = baseY;
    lines.slice(i, i + perCol).forEach(text => {
      const hpx = Math.max(1, String(text).split('\n').length) * lh;
      S.texts.push({
        id: textBoxId(), text,
        x: Math.round(baseX + (i / perCol) * colW),
        y: Math.round(top + hpx / 2),     // 坐标是框中心
        size, color: f.color || '',
      });
      top += hpx + 14;
    });
  }
  renderTexts();
  await saveTexts(true);
  toast(`已添加 ${lines.length} 个文本框`, 'ok');
}

/* 双击原地改字（contenteditable），失焦或 Ctrl+Enter 存盘，Esc 放弃 */
function editTextBox(el) {
  if (!el || el.classList.contains('editing')) return;
  const t = (S.texts || []).find(x => x.id === el.dataset.tid);
  if (!t) return;
  el.classList.add('editing');
  el.setAttribute('contenteditable', 'true');
  // preventScroll 必须给：文本框会把 #stage 撑出滚动范围，默认 focus 会把舞台
  // 滚过去，而 fit()/applyT() 的坐标是按未滚动算的，一滚就整片内容跑出视野
  el.focus({ preventScroll: true });
  try {
    const sel = window.getSelection();
    const r = document.createRange();
    r.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(r);
  } catch (e) { /* 选区失败也能打字 */ }
  const finish = save => {
    el.removeEventListener('blur', onBlur);
    el.removeEventListener('keydown', onKey);
    el.classList.remove('editing');
    el.removeAttribute('contenteditable');
    if (!save) return renderTexts();
    // 必须用 innerText：contenteditable 里回车生成的是 <div>/<br>，textContent 会把
    // 换行吞掉，"第一行\n第二行" 存进去只剩 "第一行第二行"
    const v = String(el.innerText || '').replace(/[ \t]+$/gm, '').replace(/^\s+|\s+$/g, '');
    if (!v) {                       // 清空 = 删掉这个文本框
      S.texts = (S.texts || []).filter(x => x.id !== t.id);
      renderTexts();
      return saveTexts(true);
    }
    t.text = v;
    renderTexts();
    saveTexts(true);
  };
  const onBlur = () => finish(true);
  const onKey = ev => {
    ev.stopPropagation();           // 别让画布的快捷键把字吃掉
    if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); finish(true); }
  };
  el.addEventListener('blur', onBlur);
  el.addEventListener('keydown', onKey);
}

function deleteTextBox(el) {
  const id = el && el.dataset.tid;
  if (!id) return;
  S.texts = (S.texts || []).filter(x => x.id !== id);
  S.tbxSel = (S.tbxSel || []).filter(x => x !== id);
  renderTexts();
  saveTexts(true);
}

/* 连线单独一层：拖动节点时只重画线，不重算缩放（否则视角会跳） */
function drawLinks() {
  const L = S.lab;
  const linksEl = $('#links');
  if (!L || !linksEl) return;
  linksEl.innerHTML = '';
  const pts = {};
  Object.entries(L.nodes || {}).forEach(([id, n]) => {
    pts['node' + id] = { x: Number(n.left || 0), y: Number(n.top || 0), kind: 'node', id };
  });
  Object.entries(L.networks || {}).forEach(([id, n]) => {
    pts['network' + id] = { x: Number(n.left || 0), y: Number(n.top || 0), kind: 'network', id };
  });
  const svgNS = 'http://www.w3.org/2000/svg';
  const w = Math.max(3200, ...Object.values(pts).map(p => p.x + 400));
  const h = Math.max(2200, ...Object.values(pts).map(p => p.y + 400));
  linksEl.setAttribute('width', w);
  linksEl.setAttribute('height', h);

  /* 端点可能是 "node1" / "network2" / 纯数字，三种都认 */
  const endOf = (v, type) => {
    if (v === null || v === undefined) return null;
    const s = String(v);
    if (pts[s]) return pts[s];
    const m = s.match(/(\d+)/);
    if (!m) return null;
    const cands = [];
    if (type) cands.push(String(type).toLowerCase() + m[1]);
    cands.push('node' + m[1], 'network' + m[1]);
    for (const k of cands) if (pts[k]) return pts[k];
    return null;
  };

  const p2p = S.showP2P ? new Set() : p2pNetIds();
  const placed = new Map();          // 节点 → 已占用标签数，同一节点旁的多个标签依次错开

  const stroke = (A, B, ox, oy) => {
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', A.x + ox); line.setAttribute('y1', A.y + oy);
    line.setAttribute('x2', B.x + ox); line.setAttribute('y2', B.y + oy);
    line.setAttribute('stroke', '#33404e');
    line.setAttribute('stroke-width', '2');
    linksEl.appendChild(line);
  };

  /* 末端是「被隐藏的直连桥」的边先攒起来：一条边 = 一个接口，两个节点配好对再画成直连 */
  const viaP2P = {};                 // netId → {nodeId: [接口名, ...]}
  const direct = [];                 // EVE 对隐藏点对点桥输出的原生 node→node 直连边
  (L.links || []).forEach(l => {
    const A = endOf(l.source, l.source_type);
    const B = endOf(l.destination, l.destination_type);
    if (!A || !B) return;
    const hidA = A.kind === 'network' && p2p.has(String(A.id));
    const hidB = B.kind === 'network' && p2p.has(String(B.id));
    if (hidA || hidB) {
      const net = hidA ? A : B;
      const nd = hidA ? B : A;
      if (nd.kind !== 'node') return;
      const iface = (hidA ? l.source_label : l.destination_label)
        || ifacesOnNet(nd.id, net.id).join(' ') || '';
      const box = (viaP2P[net.id] = viaP2P[net.id] || {});
      (box[nd.id] = box[nd.id] || []).push(iface);
      return;
    }
    /* 直连边（两端都是节点）：不在这里画，攒到下面按节点对分组并排错开，
       否则两台路由器之间的多根网线会全部叠成一根 */
    if (A.kind === 'node' && B.kind === 'node') {
      direct.push({ A, B, la: l.source_label || '', lb: l.destination_label || '' });
      return;
    }
    stroke(A, B, 0, 0);

    /* EVE 在拓扑里直接给了接口名：source_label / destination_label。
       两个节点共用一个隐藏网桥时，EVE 给的是 node→node 的直连边、两端都带名字，
       所以这一条线上就能同时看到「互联的两个接口」。*/
    const la = l.source_label
      || (A.kind === 'node' && B.kind === 'network' ? ifacesOnNet(A.id, B.id).join(' ') : '');
    const lb = l.destination_label
      || (B.kind === 'node' && A.kind === 'network' ? ifacesOnNet(B.id, A.id).join(' ') : '');
    addIfaceLabel(linksEl, svgNS, A, B, la, placed);
    addIfaceLabel(linksEl, svgNS, B, A, lb, placed);
  });

  /* 被隐藏的直连桥 → 两个节点之间的直线（多根网线时并排错开 9px，不叠成一条粗线） */
  const pairs = direct.slice();
  Object.entries(viaP2P).forEach(([nid, byNode]) => {
    const ids = Object.keys(byNode);
    if (ids.length !== 2) return;    // 挂 3 个以上节点的桥本来就不该隐藏，兜底不动
    const A = pts['node' + ids[0]], B = pts['node' + ids[1]];
    if (!A || !B) return;
    const n = Math.max(byNode[ids[0]].length, byNode[ids[1]].length);
    for (let i = 0; i < n; i++) {
      pairs.push({
        A, B,
        la: byNode[ids[0]][Math.min(i, byNode[ids[0]].length - 1)] || '',
        lb: byNode[ids[1]][Math.min(i, byNode[ids[1]].length - 1)] || '',
      });
    }
  });
  const tot = {}, seen = {};
  pairs.forEach(p => {
    p.key = [p.A.id, p.B.id].sort().join('-');
    tot[p.key] = (tot[p.key] || 0) + 1;
  });
  pairs.forEach(p => {
    const i = seen[p.key] = (seen[p.key] || 0);
    seen[p.key] = i + 1;
    const n = tot[p.key];
    const off = n > 1 ? (i - (n - 1) / 2) * 9 : 0;
    const dx = p.B.x - p.A.x, dy = p.B.y - p.A.y;
    const len = Math.hypot(dx, dy) || 1;
    const ox = -dy / len * off, oy = dx / len * off;
    stroke({ x: p.A.x + ox, y: p.A.y + oy }, { x: p.B.x + ox, y: p.B.y + oy }, 0, 0);
    // 标签锚点用「没偏移」的线算；第 i 根线（最上）的标签排最上，两端顺序一致
    const s = n > 1 ? n - 1 - i : 0;
    addIfaceLabel(linksEl, svgNS, p.A, p.B, p.la, placed, s);
    addIfaceLabel(linksEl, svgNS, p.B, p.A, p.lb, placed, s);
  });
}

/* 把接口名写在线路靠某一端的位置上（离开节点一段距离，沿法线错开避免压线）。
   placed 按「节点 + 方向 + 线通向哪个点」分组：去往同一个目标的线（一对节点间的
   并排网线）共用一个栈、按固定行距往上排；去往不同目标的线（比如 R6 同时连
   R7 和 R8）各算各的锚点——标签永远贴在自己的线上，位置才能和线对上。
   slotOverride 给并排线直接指定行号（第 i 根线 → 倒数第 i 行，两端顺序一致）。*/
function addIfaceLabel(parent, svgNS, from, to, text, placed, slotOverride) {
  if (!text) return;
  const dx = to.x - from.x, dy = to.y - from.y;
  const len = Math.hypot(dx, dy) || 1;
  const d = Math.min(len * 0.42, 56);            // 31（半宽）+ 15（半个标签宽）+ 留白
  let x = from.x + dx / len * d;
  let y = from.y + dy / len * d;
  // 统一放在线的「上方/外侧」，躲开节点下方那行名字
  if (Math.abs(dy) > Math.abs(dx)) x += 10; else y -= 9;
  x = Math.round(x); y = Math.round(y);
  const side = Math.abs(dx) > Math.abs(dy) ? (dx >= 0 ? 'r' : 'l') : (dy >= 0 ? 'd' : 'u');
  const key = from.kind + from.id + ':' + side + ':' + Math.round(to.x) + ',' + Math.round(to.y);
  let baseY = y, slot = 0;
  if (slotOverride !== undefined) {
    slot = slotOverride;
  } else {
    const rec = placed && placed.get(key);
    if (rec) { baseY = rec.y; slot = rec.n; placed.set(key, { y: rec.y, n: rec.n + 1 }); }
    else if (placed) placed.set(key, { y, n: 1 });
  }
  const rowH = (labelFontSize() + 6) / Math.max(S.z || 1, 0.2);
  if (slot) y = Math.round(baseY - slot * rowH);
  const el = document.createElementNS(svgNS, 'text');
  el.setAttribute('class', 'iflabel');
  el.setAttribute('x', x);
  el.setAttribute('y', y);
  el.setAttribute('text-anchor', 'middle');
  el.setAttribute('dominant-baseline', 'middle');
  el.setAttribute('fill', '#9dc0e6');
  el.setAttribute('stroke', '#0b1117');               // 描边当底，压到线也能读
  el.setAttribute('paint-order', 'stroke');
  el.style.fontSize = labelFontSize() + 'px';
  el.style.strokeWidth = (labelFontSize() / 4) + 'px';
  el.textContent = text;
  parent.appendChild(el);
}

function labelFontSize() {
  return Math.min(18, Math.max(11, 11 / Math.max(S.z || 1, 0.5)));
}

function applyT() {
  const stage = $('#stage');
  // 兜底：万一浏览器不支持 overflow:clip，舞台仍会被当成滚动容器，这里每次
  // 变换都把滚动量清零，保证「画出来的」和「算出来的」是同一套坐标
  if (stage && (stage.scrollTop || stage.scrollLeft)) {
    stage.scrollTop = 0;
    stage.scrollLeft = 0;
  }
  $('#world').style.transform = `translate(${S.tx}px,${S.ty}px) scale(${S.z})`;
  $('#zoomLabel').textContent = Math.round(S.z * 100) + '%';
  const fs = labelFontSize();
  $$('#links .iflabel').forEach(el => {
    el.style.fontSize = fs + 'px';
    el.style.strokeWidth = (fs / 4) + 'px';
  });
}

function contentBox() {
  const L = S.lab || {};
  const xs = [], ys = [];
  Object.values(L.nodes || {}).forEach(n => { xs.push(Number(n.left || 0)); ys.push(Number(n.top || 0)); });
  Object.values(L.networks || {}).forEach(n => { xs.push(Number(n.left || 0)); ys.push(Number(n.top || 0)); });
  (S.texts || []).forEach(t => { xs.push(Number(t.x || 0)); ys.push(Number(t.y || 0)); });
  if (!xs.length) return null;
  return { minX: Math.min(...xs), maxX: Math.max(...xs),
           minY: Math.min(...ys), maxY: Math.max(...ys) };
}

function fit() {
  if (!S.lab) return;
  const b = contentBox();
  if (!b) return;
  const box = $('#stage').getBoundingClientRect();
  const pad = 90;
  const zx = (box.width - pad * 2) / Math.max(b.maxX - b.minX, 120);
  const zy = (box.height - pad * 2) / Math.max(b.maxY - b.minY, 90);
  S.z = Math.min(1.35, Math.max(0.25, Math.min(zx, zy)));
  S.tx = box.width / 2 - ((b.minX + b.maxX) / 2) * S.z;
  S.ty = box.height / 2 - ((b.minY + b.maxY) / 2) * S.z;
  applyT();
  saveView();
}

/* ------------------------------------------------------------------ 视角
   平移 / 缩放 / 每个实验各记一份视角。缩放锚点定在鼠标位置（按钮则锚中心），
   这样「放大看某处」不会跑偏——这也是拓扑图最常被抱怨的地方。            */
const VIEW_PREFIX = 'eve.view.';
let viewTimer = 0, spaceDown = false, rightPanAt = 0;

function viewKey() { return VIEW_PREFIX + (S.labPath || ''); }

function saveView(now) {
  if (!S.labPath) return;
  clearTimeout(viewTimer);
  const dump = () => {
    try {
      localStorage.setItem(viewKey(), JSON.stringify({
        z: Math.round(S.z * 1000) / 1000,
        tx: Math.round(S.tx), ty: Math.round(S.ty),
      }));
    } catch (e) { /* 隐私模式下 localStorage 会抛，忽略即可 */ }
  };
  if (now) dump(); else viewTimer = setTimeout(dump, 400);
}

/* 打开实验时把上次的视角读回来；没有记录就返回 false，交给 fit() */
function loadView() {
  if (!S.labPath) return false;
  try {
    const v = JSON.parse(localStorage.getItem(viewKey()) || 'null');
    if (!v || !Number.isFinite(v.z) || v.z <= 0) return false;
    S.z = Math.min(3, Math.max(0.15, v.z));
    S.tx = Number(v.tx) || 0;
    S.ty = Number(v.ty) || 0;
    return true;
  } catch (e) { return false; }
}

/* k = 缩放倍数；cx/cy 是屏幕坐标，省略则绕舞台中心 */
function zoomAt(k, cx, cy) {
  if (!S.lab) return;
  const box = $('#stage').getBoundingClientRect();
  const mx = (cx == null) ? box.width / 2 : cx - box.left;
  const my = (cy == null) ? box.height / 2 : cy - box.top;
  const z2 = Math.min(3, Math.max(0.15, S.z * k));
  if (Math.abs(z2 - S.z) < 1e-4) return;
  S.tx = mx - (mx - S.tx) * (z2 / S.z);
  S.ty = my - (my - S.ty) * (z2 / S.z);
  S.z = z2;
  applyT();
  saveView();
}

/* 100% 并让内容回到视野中央 */
function zoomReset() {
  if (!S.lab) return;
  const b = contentBox();
  const box = $('#stage').getBoundingClientRect();
  S.z = 1;
  S.tx = box.width / 2 - (b ? (b.minX + b.maxX) / 2 : 0);
  S.ty = box.height / 2 - (b ? (b.minY + b.maxY) / 2 : 0);
  applyT();
  saveView();
}

function panBy(dx, dy) {
  if (!S.lab) return;
  S.tx += dx; S.ty += dy;
  applyT();
  saveView();
}

/* 焦点在输入框里时别抢键 */
function typing() {
  const a = document.activeElement;
  return !!(a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)));
}

/* ------------------------------------------------------------------ 检查器 */
function paintSel() {
  const ids = S.selIds || [];
  $$('.nd').forEach(el => el.classList.toggle('sel', ids.includes(el.dataset.id)));
}

async function selectNode(id, mode) {
  // mode: '' 单选 / 'add' 加选（Ctrl）/ 'toggle' 切换
  const cur = S.selIds || [];
  if (mode === 'toggle') {
    S.selIds = cur.includes(id) ? cur.filter(x => x !== id) : cur.concat([id]);
  } else if (mode === 'add') {
    S.selIds = cur.includes(id) ? cur : cur.concat([id]);
  } else {
    S.selIds = [id];
  }
  S.sel = S.selIds[0] || null;
  paintSel();
  await syncInspector();
}

/* 按当前选择刷新右侧检查器（单选 → 详情，多选 → 批量面板） */
async function syncInspector() {
  if (!S.selIds || !S.selIds.length) { renderInspector(null); return; }
  if (S.selIds.length > 1) { renderMultiInspector(); return; }
  try {
    const d = await api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${S.selIds[0]}`);
    renderInspector(d);
  } catch (e) { renderInspector(null); }
}

/* 多选时的检查器：只给批量操作，不显示单个节点属性 */
function renderMultiInspector() {
  const ids = S.selIds || [];
  if (!ids.length) { renderInspector(null); return; }
  const names = ids.map(id => (S.lab.nodes[id] || {}).name || ('#' + id));
  const run = ids.filter(id => isRunning(S.lab.nodes[id] || {})).length;
  const box = $('#inspBody');
  box.onchange = null;
  box.innerHTML = `
    <div class="multi-box">
      <div class="kv"><span class="k">已选中</span><span class="v"><b>${ids.length}</b> 个节点</span></div>
      <div class="kv"><span class="k">运行中</span><span class="v">${run} / ${ids.length}</span></div>
      <div class="sel-list">${names.map(esc).join('、')}</div>
      <div class="insp-actions">
        <button class="btn sm" data-mact="start">启动</button>
        <button class="btn sm ghost" data-mact="stop">停止</button>
        <button class="btn sm ghost" data-mact="wipe">擦除</button>
        <button class="btn sm ghost" data-mact="del">删除</button>
      </div>
      <div class="row" style="gap:6px">
        <button class="btn sm ghost" data-mact="link">批量接线…</button>
        <button class="btn sm ghost" data-mact="clear">取消选择</button>
      </div>
    </div>`;
  box.onclick = ev => {
    const b = ev.target.closest('[data-mact]');
    if (!b) return;
    const act = b.dataset.mact;
    if (act === 'clear') { S.selIds = []; paintSel(); renderInspector(null); return; }
    if (act === 'link') { linkDialog(ids).catch(fail); return; }
    bulkAct(ids, act);
  };
}

/* 批量启动/停止/擦除/删除：逐个下发，最后汇总 */
async function bulkAct(ids, action) {
  if (!ids.length) return;
  const verb = { start: '启动', stop: '停止', wipe: '擦除', del: '删除' }[action] || action;
  if ((action === 'del' || action === 'wipe')
      && !confirm(`${verb} ${ids.length} 个节点？${action === 'del' ? '\n连带配置目录一起删，不可恢复。' : ''}`)) return;
  let ok = 0; const errs = [];
  for (const id of ids) {
    try {
      if (action === 'del') {
        const r = await api('/api/lab/node?path=' + encodeURIComponent(S.labPath)
          + '&id=' + encodeURIComponent(id) + '&umtime=' + encodeURIComponent(S.umtime),
          { method: 'DELETE' });
        afterWrite(r);   // 每删一个 umtime 都会变，不跟上第二个就会被并发保护拦下
      } else {
        const r = await api('/api/node/action', { method: 'POST', body: { path: S.labPath, id, action } });
        afterWrite(r);
      }
      ok++;
    } catch (e) { errs.push(`${(S.lab.nodes[id] || {}).name || id}: ${e.message || e}`); }
  }
  if (action === 'del') S.selIds = [];
  toast(`${verb} ${ok}/${ids.length} 个节点` + (errs.length ? `，${errs.length} 个失败` : ''),
    errs.length ? 'err' : 'ok');
  if (errs.length) console.warn(errs);
  await openLab(S.labPath);
}

function renderInspector(d) {
  $$('.net').forEach(x => x.classList.remove('sel'));
  const box = $('#inspBody');
  if (!d) { box.innerHTML = '<div class="empty">选中一个节点</div>'; box.onchange = null; return; }
  const n = d.node || {};
  const nets = Object.entries(S.lab.networks || {}).map(([id, v]) => ({ id, ...v }));
  const ifs = (d.interfaces && d.interfaces.ethernet) || {};
  const rows = Object.entries(ifs).map(([idx, it]) => {
    const opts = ['<option value="0">未连接</option>'].concat(nets.map(x =>
      `<option value="${x.id}"${String(it.network_id) === String(x.id) ? ' selected' : ''}>${esc(x.name)} (${esc(x.type)})</option>`))
      .concat(['<option value="__new">＋ 新建网络…</option>']).join('');
    const on = String(it.network_id) !== '0' && it.network_id != null && it.network_id !== '';
    return `<div class="iface-row"><span class="nm">${esc(it.name)}</span>
      <select data-if="${esc(it.name)}">${opts}</select>
      <span class="peer ${on ? 'on' : ''}">${on ? '已连' : '未连'}</span>
      <button class="mini cap-btn" data-cap="${esc(it.name)}"
        title="在 ${esc(it.name)} 上抓包（实时灌进本机 Wireshark）">抓包</button></div>`;
  }).join('');

  // Linux / Windows 这类节点的 console 是 vnc 或 rdp，根本没有串口，
  // 「CLI 控制台」连上去只会是一堆乱码，这里按节点实际的控制台类型给按钮。
  const con = String(n.console || '').toLowerCase();
  const graphical = con === 'vnc' || con === 'rdp';
  box.innerHTML = `
    <div class="kv"><span class="k">名称</span><span class="v">${esc(n.name)}</span></div>
    <div class="kv"><span class="k">模板</span><span class="v">${esc(n.template || n.type)}</span></div>
    <div class="kv"><span class="k">镜像</span><span class="v">${esc(n.image || '-')}</span></div>
    <div class="kv"><span class="k">内存</span><span class="v">${esc(n.ram || '-')} MB</span></div>
    <div class="kv"><span class="k">状态</span><span class="v">${isRunning(n) ? '运行中' : '已停止'}</span></div>
    <div class="kv"><span class="k">控制台</span><span class="v">${graphical ? esc(con.toUpperCase()) : '串口（telnet）'}</span></div>
    <div class="kv"><span class="k">控制台端口</span><span class="v">${d.console_port}</span></div>
    <div class="insp-actions">
      <button class="btn sm" data-act="start">启动</button>
      <button class="btn sm ghost" data-act="stop">停止</button>
      <button class="btn sm ghost" data-act="wipe">擦除</button>
      ${graphical
        ? (d.html5
            ? '<button class="btn sm" data-act="html5" title="在底部 dock 里打开">图形控制台</button>'
              + '<button class="btn sm" data-act="html5win" title="开一个独立的大窗口：可缩放/最大化，Ctrl+Alt+V 把 Windows 剪贴板送进虚拟机">⤢ 独立窗口</button>'
            : '<button class="btn sm ghost" disabled title="该节点只有 VNC/RDP 控制台，但这台服务器没启用 html5">图形控制台</button>')
        : '<button class="btn sm" data-act="term">CLI 控制台</button>'
          + (d.html5 ? '<button class="btn sm ghost" data-act="html5win" title="开一个独立大窗口显示图形控制台">⤢ 独立窗口</button>' : '')}
      <button class="btn sm ghost" data-act="rename">改名</button>
      <button class="btn sm ghost" data-act="icon">图标…</button>
      <button class="btn sm ghost" data-act="edit">属性…</button>
      <button class="btn sm ghost" data-act="del">删除节点</button>
    </div>
    <div class="sub-title">网卡 <span class="sub-hint">${rows ? Object.keys(ifs).length + ' 个接口' : ''}</span></div>
    ${rows || '<div class="empty">无以太网卡</div>'}
    <div style="margin-top:10px"><button class="btn sm ghost" data-act="savif">保存网卡接线</button>
      <button class="btn sm ghost" data-act="link">接线…</button>
      <button class="btn sm ghost" data-act="newcloud">新建云</button></div>`;

  box.onclick = async ev => {
    const capBtn = ev.target.closest('[data-cap]');
    if (capBtn) {
      await startCapture(n.id, capBtn.dataset.cap);
      return;
    }
    const b = ev.target.closest('[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    try {
      if (act === 'start' || act === 'stop' || act === 'wipe') {
        if (act === 'wipe' && !confirm(`擦除 ${n.name} 的所有配置？`)) return;
        await api('/api/node/action', { method: 'POST', body: { path: S.labPath, id: n.id, action: act } });
        toast(act === 'start' ? '已下发启动' : act === 'stop' ? '已停止' : '已擦除', 'ok');
        setTimeout(() => openLab(S.labPath), 900);
      } else if (act === 'term') {
        openNodeTerm(n.id, n.name);
      } else if (act === 'html5') {
        openHtml5(d.html5, n.name);
      } else if (act === 'html5win') {
        popHtml5(d.html5, n.name);
      } else if (act === 'rename') {
        await renameNode(n.id, n.name);
      } else if (act === 'icon') {
        await pickIcon([n.id]);
      } else if (act === 'edit') {
        await editNodeProps(n, d);
      } else if (act === 'link') {
        await linkDialog([n.id]);
      } else if (act === 'del') {
        await deleteNodeById(n.id, n.name);
      } else if (act === 'savif') {
        const arr = $$('select[data-if]').map(s => ({ name: s.dataset.if, network_id: Number(s.value) }))
          .filter(x => Number.isFinite(x.network_id));
        const r = await api('/api/node/ifaces', { method: 'PUT', body: guarded({
          path: S.labPath, id: n.id, ifaces: arr }) });
        afterWrite(r);
        toast('网卡接线已保存', 'ok');
        setTimeout(() => openLab(S.labPath), 600);
      } else if (act === 'newcloud') {
        await createCloud();
      }
    } catch (e) { fail(e); }
  };
  // 网卡下拉里的「＋ 新建网络…」：先建云，建好后用户再从下拉里选它
  box.onchange = async ev => {
    const s = ev.target.closest('select[data-if]');
    if (!s || s.value !== '__new') return;
    s.value = '0';
    try {
      await createCloud();
      toast('网络已建好，回到下拉里选它就能接上', 'ok');
    } catch (e) { fail(e); }
  };
}

async function createCloud() {
  const f = await modal('新建网络（云）', [
    { key: 'name', label: '名称', value: 'Cloud' },
    { key: 'type', label: '类型', type: 'select', value: 'pnet0', options: [
      { value: 'pnet0', label: 'pnet0 · 管理网（直连局域网）' },
      { value: 'pnet1', label: 'pnet1' }, { value: 'pnet2', label: 'pnet2' },
      { value: 'pnet3', label: 'pnet3' }, { value: 'pnet4', label: 'pnet4' },
      { value: 'bridge', label: 'bridge · 内部桥（不连外网）' },
    ] },
  ]);
  if (!f) return;
  const box = $('#stage').getBoundingClientRect();
  const cx = (box.width / 2 - S.tx) / S.z, cy = (box.height / 2 - S.ty) / S.z;
  await api('/api/lab/network', { method: 'PUT', body: {
    path: S.labPath,
    net: { name: f.name, type: f.type, left: Math.round(cx), top: Math.round(cy),
           visibility: 1, icon: f.type === 'bridge' ? 'lan.png' : 'cloud.png' } } });
  toast('已新建网络', 'ok');
  await openLab(S.labPath);
}

/* ------------------------------------------- 二期：拓扑编辑（新增 / 移动 / 删除） */
function guarded(body) {
  // 写操作统一带上 umtime，让后端挡掉「别处已经改过这个实验」的情况
  return Object.assign({ umtime: S.umtime }, body);
}
function afterWrite(r) { if (r && r.umtime) S.umtime = r.umtime; }

function suggestName(prefix) {
  const p = (prefix || 'R').trim() || 'R';
  let max = 0;
  const re = new RegExp('^' + p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(\\d+)$');
  Object.values((S.lab && S.lab.nodes) || {}).forEach(n => {
    const m = re.exec((n.name || '').trim());
    if (m) max = Math.max(max, Number(m[1]));
  });
  return p + (max + 1);
}

function imgField(o, cur) {
  const list = (o.image && o.image.list) || null;
  const def = cur || (o.image && o.image.value) || '';
  if (list) {
    const keys = Object.keys(list);
    return { key: 'image', label: '镜像', type: 'select', value: def || keys[0],
             options: keys.map(k => ({ value: k, label: k })) };
  }
  return { key: 'image', label: '镜像', value: def };
}

/* 新建节点：模板默认值打底，名字/镜像/内存/网卡数都能改（对齐 EVE 的节点对话框） */
async function addNodeAt(tpl, x, y) {
  if (!S.labPath) { toast('先打开一个实验', 'warn'); return; }
  let o = {};
  try {
    const d = await api('/api/template?name=' + encodeURIComponent(tpl));
    o = ((d.data || {}).options) || {};
  } catch (e) { /* 取不到默认值就只填模板 */ }
  const fields = [
    { key: 'name', label: '名称', value: suggestName((o.name && o.name.value) || 'R') },
    imgField(o, ''),
    { key: 'ram', label: '内存 (MB)', type: 'number', value: (o.ram && o.ram.value) || 1024 },
    { key: 'ethernet', label: '网卡数量', type: 'number', value: (o.ethernet && o.ethernet.value) || 2 },
    { key: 'serial', label: '串口数量', type: 'number', value: (o.serial && o.serial.value) || 0 },
  ];
  const f = await modal('新建节点 · ' + tpl, fields);
  if (!f) return;
  const props = { template: tpl, name: (f.name || '').trim(),
                  left: Math.round(x), top: Math.round(y) };
  if (f.image) props.image = f.image;
  if (f.ram !== '' && f.ram != null) props.ram = Number(f.ram);
  if (f.ethernet !== '' && f.ethernet != null) props.ethernet = Number(f.ethernet);
  if (f.serial !== '' && f.serial != null) props.serial = Number(f.serial);
  if (o.icon && o.icon.value) props.icon = o.icon.value;
  try {
    const r = await api('/api/lab/node', { method: 'POST', body: guarded({ path: S.labPath, props }) });
    afterWrite(r);
    toast('已添加节点 ' + props.name, 'ok');
    await openLab(S.labPath);
    if (r.id != null) selectNode(String(r.id)).catch(() => {});
  } catch (e) { fail(e); }
}

/* 改已有节点的属性（EVE 右键 Edit 的同款） */
async function editNodeProps(nd, detail) {
  const tpl = nd.template || nd.type || '';
  let o = {};
  try {
    const r = await api('/api/template?name=' + encodeURIComponent(tpl));
    o = ((r.data || {}).options) || {};
  } catch (e) { /* 忽略 */ }
  const fields = [
    { key: 'name', label: '名称', value: nd.name || '' },
    imgField(o, nd.image || ''),
    { key: 'ram', label: '内存 (MB)', type: 'number', value: nd.ram || 1024 },
    { key: 'ethernet', label: '网卡数量（改了会增删接口）', type: 'number',
      value: nd.ethernet || (o.ethernet && o.ethernet.value) || 1 },
    { key: 'serial', label: '串口数量', type: 'number', value: nd.serial || 0 },
  ];
  const f = await modal('节点属性 · ' + (nd.name || nd.id), fields);
  if (!f) return;
  const props = { name: (f.name || '').trim() || nd.name };
  if (f.image) props.image = f.image;
  if (f.ram !== '' && f.ram != null) props.ram = Number(f.ram);
  if (f.ethernet !== '' && f.ethernet != null) props.ethernet = Number(f.ethernet);
  if (f.serial !== '' && f.serial != null) props.serial = Number(f.serial);
  try {
    const r = await api('/api/node', { method: 'PUT', body: guarded({
      path: S.labPath, id: nd.id, props }) });
    afterWrite(r);
    toast('节点属性已保存', 'ok');
    await openLab(S.labPath);
    selectNode(String(nd.id)).catch(() => {});
  } catch (e) { fail(e); }
}

/* 接线：选接口 + 选网络，一次接一个（批量时按同序号接口套到每个节点） */
async function linkDialog(ids) {
  ids = (ids || []).filter(Boolean);
  if (!ids.length) return;
  if (!S.labPath) { toast('先打开一个实验', 'warn'); return; }
  const nets = Object.entries(S.lab.networks || {}).map(([id, v]) => ({ id, ...v }));
  if (!nets.length) {
    toast('这个实验里还没有网络/云，先新建一个再接', 'warn');
    return;
  }
  let d0;
  try { d0 = await api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${ids[0]}`); }
  catch (e) { return fail(e); }
  const ifs = Object.values((d0.interfaces || {}).ethernet || {});
  if (!ifs.length) { toast('这个节点没有以太网卡', 'warn'); return; }
  const free = ifs.find(it => !it.network_id || String(it.network_id) === '0') || ifs[0];
  const f = await modal(ids.length > 1 ? `批量接线（${ids.length} 个节点）` : '接线', [
    { key: 'iface', label: '接口', type: 'select', value: free.name,
      options: ifs.map(it => ({ value: it.name,
        label: it.name + ((it.network_id && String(it.network_id) !== '0') ? '（已接）' : '（空）') })) },
    { key: 'net', label: '接到网络', type: 'select', value: String(nets[0].id),
      options: nets.map(n => ({ value: String(n.id), label: `${n.name} (${n.type})` })) },
  ]);
  if (!f) return;
  let ok = 0; const errs = [];
  for (const id of ids) {
    try {
      const d = (id === ids[0] && d0) ? d0
        : await api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${id}`);
      const list = Object.values((d.interfaces || {}).ethernet || {});
      let name = f.iface;
      if (ids.length > 1) {
        // 批量时各节点接口名一致（e0/0…），按「第几个接口」对齐更稳
        const idx = Math.max(0, ifs.findIndex(x => x.name === f.iface));
        name = (list[idx] || {}).name || f.iface;
      }
      await api('/api/node/ifaces', { method: 'PUT', body: guarded({
        path: S.labPath, id, ifaces: [{ name, network_id: Number(f.net) }] }) });
      ok++;
    } catch (e) { errs.push(`${(S.lab.nodes[id] || {}).name || id}: ${e.message || e}`); }
  }
  toast(`接线 ${ok}/${ids.length} 个节点` + (errs.length ? `，${errs.length} 个失败` : ''),
    errs.length ? 'err' : 'ok');
  if (errs.length) console.warn(errs);
  await openLab(S.labPath);
}

async function renameNode(id, cur) {
  const name = prompt('节点名称', cur || '');
  if (name == null || !name.trim()) return;
  try {
    const r = await api('/api/node', { method: 'PUT', body: guarded({
      path: S.labPath, id, props: { name: name.trim() } }) });
    afterWrite(r);
    toast('已改名', 'ok');
    await openLab(S.labPath);
  } catch (e) { fail(e); }
}

async function deleteNodeById(id, name) {
  if (!confirm(`删除节点 ${name || id}？\n连带它的配置目录一起删，不可恢复。`)) return;
  try {
    const r = await api('/api/lab/node?path=' + encodeURIComponent(S.labPath)
      + '&id=' + encodeURIComponent(id) + '&umtime=' + encodeURIComponent(S.umtime),
      { method: 'DELETE' });
    afterWrite(r);
    toast('已删除节点', 'ok');
    await openLab(S.labPath);
  } catch (e) { fail(e); }
}

async function deleteNetById(id, name) {
  if (!confirm(`删除网络 ${name || id}？接在上面的网卡会自动断开。`)) return;
  try {
    const r = await api('/api/lab/network?path=' + encodeURIComponent(S.labPath)
      + '&id=' + encodeURIComponent(id) + '&umtime=' + encodeURIComponent(S.umtime),
      { method: 'DELETE' });
    afterWrite(r);
    toast('已删除网络', 'ok');
    await openLab(S.labPath);
  } catch (e) { fail(e); }
}

/* ------------------------------------------------------------ 模板库面板 */
async function loadTemplates(force) {
  if (S.tpl.length && !force) return S.tpl;
  const d = await api('/api/templates');
  S.tpl = d.items || [];
  S.tplReady = d.ready || 0;
  return S.tpl;
}

async function togglePalette(on) {
  const el = $('#palette');
  const open = on == null ? !el.classList.contains('on') : on;
  el.classList.toggle('on', open);
  if (!open) return;
  $('#palList').innerHTML = '<div class="empty">加载中…</div>';
  try {
    await loadTemplates();
    $('#palHint').textContent = `拖到画布放置，或单击直接添加（${S.tpl.length} 个模板，`
      + `${S.tplReady} 个已装镜像）`;
    renderPalette($('#palSearch').value);
  } catch (e) {
    $('#palList').innerHTML = '<div class="empty">加载失败</div>';
    fail(e);
  }
}

function renderPalette(filter) {
  const q = (filter || '').trim().toLowerCase();
  const list = S.tpl.filter(t => !q
    || t.template.toLowerCase().includes(q) || (t.desc || '').toLowerCase().includes(q));
  $('#palList').innerHTML = list.slice(0, 400).map(t => `
    <div class="pal-item${t.ready ? '' : ' off'}" draggable="${t.ready ? 'true' : 'false'}"
         data-tpl="${esc(t.template)}" data-ready="${t.ready ? 1 : 0}"
         title="${esc(t.ready ? t.desc : '宿主没装这个模板对应的镜像')}">
      <span class="pal-name">${esc(t.desc || t.template)}</span>
      <span class="pal-id">${esc(t.template)}</span>
    </div>`).join('') || '<div class="empty">没有匹配的模板</div>';
}

/* -------------------------------------------------------------- 抓包面板 */
async function toggleCapPanel(on) {
  const el = $('#capPanel');
  const open = on == null ? !el.classList.contains('on') : on;
  el.classList.toggle('on', open);
  clearInterval(S.capTimer);
  S.capTimer = null;
  if (!open) return;
  await refreshCaps().catch(fail);
  S.capTimer = setInterval(() => { if (!document.hidden) refreshCaps().catch(() => {}); }, 2000);
}

async function refreshCaps() {
  const d = await api('/api/captures');
  S.caps = d.items || [];
  S.capDir = d.dir || '';
  renderCaps();
}

function renderCaps() {
  const box = $('#capList');
  const items = S.caps || [];
  if (!items.length) {
    box.innerHTML = '<div class="empty">还没有在抓的会话<br><span style="opacity:.6">'
      + '右键节点或检查器里的「抓包」开始</span></div>';
    return;
  }
  box.innerHTML = items.map(c => `
    <div class="cap-item" data-sid="${esc(c.sid)}">
      <div class="cap-top"><b>${esc(c.node)}</b> · ${esc(c.iface)}
        <span class="cap-tag">${esc(c.ifname)}</span></div>
      <div class="cap-meta"><b>${c.packets}</b> 个包 · ${bytes(c.bytes)} · ${c.elapsed}s${c.bpf ? ' · ' + esc(c.bpf) : ''}</div>
      ${c.ws ? '<div class="cap-ws">Wireshark 已打开，包里实时滚</div>'
             : (c.note ? `<div class="cap-note">${esc(c.note)}</div>` : '')}
      <div class="cap-btns">
        <button class="btn sm" data-cap-act="save">停止并保存</button>
        <button class="btn sm ghost" data-cap-act="discard">丢弃</button>
      </div>
    </div>`).join('');
  if (S.lastPcap) {
    box.insertAdjacentHTML('beforeend',
      `<div class="cap-saved">已存：${esc(S.lastPcap.split(/[\\/]/).pop())}
        <button class="mini" id="capOpenLast">用 Wireshark 打开</button></div>`);
  }
}

async function startCapture(nodeId, iface) {
  if (!S.labPath) { toast('先打开一个实验', 'warn'); return; }
  try {
    toast('开始抓包，正在拉起 Wireshark…');
    const r = await api('/api/capture/start', { method: 'POST', body: {
      path: S.labPath, id: nodeId, iface } });
    if (r.ws) toast('抓包中 · Wireshark 已打开（窗口里实时滚包）', 'ok');
    else toast(r.note || '抓包中（没找到 Wireshark，只在本机存文件）', 'warn');
    await toggleCapPanel(true);
  } catch (e) { fail(e); }
}

async function saveCap(sid) {
  try {
    toast('正在停止…');
    const r = await api('/api/capture/save', { method: 'POST', body: { sid } });
    S.lastPcap = r.file;
    toast(`已停止，共 ${r.packets} 个包（${bytes(r.bytes)}），文件已存到本机`, 'ok');
    await refreshCaps();
  } catch (e) { fail(e); refreshCaps().catch(() => {}); }
}

async function discardCap(sid) {
  try {
    await api('/api/capture/discard', { method: 'POST', body: { sid } });
    toast('已丢弃', 'ok');
    await refreshCaps();
  } catch (e) { fail(e); }
}

async function openPcap(file) {
  try {
    const r = await api('/api/capture/open', { method: 'POST', body: { file } });
    if (!r.opened) toast(r.hint || '打不开这个文件', 'warn');
  } catch (e) { fail(e); }
}

/* ------------------------------------------------------------------ 终端 */
const DOCK_MIN = 140;

/* dock 高度可拖拽：图形控制台塞在 300px 里根本没法用，至少要能自己拉大。
   高度记在 localStorage，下次打开还是这个高度。 */
function dockSetHeight(h) {
  const dock = $('#dock');
  if (!dock) return;
  const max = Math.max(DOCK_MIN, window.innerHeight - 160);
  h = Math.round(Math.max(DOCK_MIN, Math.min(h, max)));
  dock.style.height = h + 'px';
  dock.style.flex = '0 0 ' + h + 'px';
  try { localStorage.setItem('dockH', String(h)); } catch (e) {}
  if (S.lab) setTimeout(fit, 120);
}

function dockShow() {
  $('#dock').classList.add('open');
  $('#dockToggle').classList.remove('show');
  const h = Number(localStorage.getItem('dockH') || 0);
  if (h > 0) dockSetHeight(h);
  setTimeout(() => { if (S.lab) fit(); }, 200);
}
function dockHide() {
  const dock = $('#dock');
  dock.classList.remove('open');
  dock.style.height = '';
  dock.style.flex = '';
  $('#dockToggle').classList.add('show');
  setTimeout(() => { if (S.lab) fit(); }, 200);
}

async function openNodeTerm(id, name) {
  // 深链（#lab=..&term=1）和旧习惯都可能直接叫 CLI 控制台。Linux/Windows 这类
  // 节点只有 VNC/RDP，串口连上去是乱码，这里直接换成图形控制台。
  // 而且直接开**独立窗口**：dock 只有 300px 高，1024x768 的桌面挤进去没法用。
  const n = ((S.lab || {}).nodes || {})[id] || {};
  const con = String(n.console || '').toLowerCase();
  if (con === 'vnc' || con === 'rdp') return popHtml5(n.url, n.name || name);
  try {
    const r = await api('/api/term/open', { method: 'POST', body: {
      kind: 'node', path: S.labPath, id } });
    addTerm(r.sid, name || ('node-' + id));
  } catch (e) { fail(e); }
}

async function openHostShell() {
  try {
    const r = await api('/api/term/open', { method: 'POST', body: { kind: 'host' } });
    addTerm(r.sid, r.title || 'EVE Shell');
  } catch (e) { fail(e); }
}

/* 图形控制台默认开**独立窗口**，不再塞进 dock。
   两个理由：dock 只有 300px 高，Linux 桌面根本看不清；更关键的是
   「粘贴 / 键入」工具条由后端在独立窗口里注入（core/h5win.py），
   放进 iframe 就什么辅助都没有，用户只会觉得“粘贴又坏了”。
   真开不出窗口（没装 pywebview 等）才退回 dock 里的 iframe。 */
async function openHtml5(url, name) {
  if (!url) return toast('该节点没有图形控制台', 'err');
  try {
    const r = await api('/api/console/window', { method: 'POST', body: { url, title: name } });
    if (r.mode === 'window') {
      if (r.warn) toast(r.warn, 'warn');
      else if (r.reused) toast('这个控制台已经开着了', 'ok');
      else toast('已在新窗口打开 · 右下角有「粘贴 / 键入」，Ctrl+Alt+T 逐字键入', 'ok');
      return;
    }
    if (r.why) toast(r.why, 'warn');
  } catch (e) { /* 落到内嵌面板 */ }
  addIframe('html5-' + name, name + '（图形）', url);
}

/* 图形控制台开成独立窗口：dock 里只有 300px 高，Linux 桌面根本看不清。
   窗口由后端（core/h5win.py）创建，那里还会注入剪贴板桥接脚本。 */
async function popHtml5(url, name) {
  if (!url) return toast('该节点没有图形控制台', 'err');
  try {
    const r = await api('/api/console/window', { method: 'POST', body: { url, title: name } });
    if (r.warn) toast(r.warn, 'warn');
    else if (r.mode === 'browser') toast(r.why || '已用系统浏览器打开', 'warn');
    else if (r.reused) toast('这个控制台已经开着了', 'ok');
    else toast('已在新窗口打开 · 右下角有「粘贴 / 键入」，Ctrl+Alt+T 逐字键入', 'ok');
  } catch (e) { fail(e); }
}

function addIframe(key, title, url) {
  const sid = 'iframe:' + key + ':' + Date.now();
  const tab = document.createElement('div');
  tab.className = 'dtab';
  tab.dataset.sid = sid;
  tab.dataset.url = url;
  tab.dataset.title = title;
  tab.innerHTML = `${esc(title)}<span class="pop" title="在独立窗口打开（可缩放、支持粘贴）">⤢</span><span class="x">×</span>`;
  $('#dtabs').appendChild(tab);

  const pane = document.createElement('div');
  pane.className = 'term';
  pane.dataset.sid = sid;
  pane.innerHTML = `<iframe src="${esc(url)}" style="width:100%;height:100%;border:0;background:#000"></iframe>`;
  $('#dockBody').appendChild(pane);
  dockShow();
  activateTerm(sid);
}

/* --------------------------------------------------- 剪贴板（SecureCRT 习惯）
   选中即复制、右键粘贴。浏览器 clipboard API 需要权限（WebView2 里 readText
   常被拒），所以一律有回落到本地服务：写 /api/clip、读走 /api/term/paste。 */
async function clipWrite(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 落到服务端 */ }
  try {
    await api('/api/clip', { method: 'POST', body: { text } });
    return true;
  } catch (e) { return false; }
}

function copySelection(rec, quiet) {
  let sel = '';
  try { sel = rec.term.getSelection() || ''; } catch (e) { sel = ''; }
  if (!sel) {
    if (!quiet) toast('先选中要复制的文字', 'warn');
    return false;
  }
  const n = sel.length;
  clipWrite(sel).then(ok => {
    if (ok) toast('已复制 ' + n + ' 字符', 'ok');
    else toast('复制失败', 'err');
  });
  return true;
}

// 右键 / Ctrl+Shift+V：让服务端读系统剪贴板再写进会话，不依赖浏览器权限
async function pasteInto(rec) {
  try {
    const r = await api('/api/term/paste', { method: 'POST', body: { sid: rec.sid } });
    if (!r.len) toast('剪贴板里没有文本', 'warn');
    else toast('已粘贴 ' + r.len + ' 字符', 'ok');
  } catch (e) { fail(e); }
  try { rec.term.focus(); } catch (e) {}
}

function wireClipboard(rec) {
  const { term, pane } = rec;

  // 选中即复制（松开鼠标才触发，避免拖选过程中反复写剪贴板）
  // 只认左键：右键是粘贴，不能顺手把选区也复制了
  pane.addEventListener('mouseup', ev => {
    if (ev.button !== 0) return;
    setTimeout(() => {
      let sel = '';
      try { sel = term.getSelection() || ''; } catch (e) { return; }
      if (sel && sel.trim()) clipWrite(sel);
    }, 10);
  });

  // 右键粘贴（SecureCRT 习惯），顺带屏蔽浏览器自带菜单
  pane.addEventListener('contextmenu', ev => {
    ev.preventDefault();
    ev.stopPropagation();
    pasteInto(rec);
  });

  // 键盘：Ctrl+C 有选区时本地复制（没选区照常发中断）；Shift+Insert 粘贴
  const copyKey = ev => {
    if (ev.type !== 'keydown') return true;
    const k = (ev.key || '').toLowerCase();
    if (ev.ctrlKey && ev.shiftKey && k === 'c') { copySelection(rec); return false; }
    if (ev.ctrlKey && ev.shiftKey && k === 'v') { pasteInto(rec); return false; }
    if (ev.shiftKey && !ev.ctrlKey && !ev.altKey && k === 'insert') { pasteInto(rec); return false; }
    if (ev.ctrlKey && !ev.shiftKey && !ev.altKey && k === 'insert') {
      if (copySelection(rec, true)) return false;
    }
    if (ev.ctrlKey && !ev.shiftKey && !ev.altKey && k === 'c') {
      let sel = '';
      try { sel = term.getSelection() || ''; } catch (e) {}
      if (sel) { copySelection(rec); return false; }   // 有选区→复制；无选区→交给设备（Ctrl+C 中断）
    }
    if (ev.ctrlKey && !ev.shiftKey && !ev.altKey && k === 'v') {
      // 浏览器原生 paste 事件通常可用；这里主动接管更稳
      pasteInto(rec);
      return false;
    }
    return true;
  };
  if (term.attachCustomKeyEventHandler) term.attachCustomKeyEventHandler(copyKey);
}

function addTerm(sid, title) {
  const tab = document.createElement('div');
  tab.className = 'dtab';
  tab.dataset.sid = sid;
  tab.innerHTML = `${esc(title)}<span class="x">×</span>`;
  $('#dtabs').appendChild(tab);

  const pane = document.createElement('div');
  pane.className = 'term';
  pane.dataset.sid = sid;
  $('#dockBody').appendChild(pane);

  const term = new Terminal({
    fontSize: 13,
    fontFamily: 'Consolas, "Cascadia Mono", "Courier New", monospace',
    cursorBlink: true,
    scrollback: 8000,
    theme: {
      background: '#0b1015', foreground: '#cfdae4', cursor: '#4fa3ff',
      selectionBackground: '#26456b',
      black: '#0b1015', red: '#e06c75', green: '#98c379', yellow: '#e5c07b',
      blue: '#61afef', magenta: '#c678dd', cyan: '#56b6c2', white: '#cfdae4',
      brightBlack: '#5c6370', brightRed: '#ef596f', brightGreen: '#a5e075',
      brightYellow: '#f0c674', brightBlue: '#7cc0ff', brightMagenta: '#d8a0f0',
      brightCyan: '#7fd6e0', brightWhite: '#ffffff',
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(pane);
  setTimeout(() => { try { fitAddon.fit(); } catch (e) {} }, 30);

  const rec = { sid, term, fit: fitAddon, pane, offset: 0, es: null };
  S.terms.push(rec);
  wireClipboard(rec);

  term.onData(d => {
    api('/api/term/input', { method: 'POST', body: { sid, data: d } }).catch(() => {});
  });
  term.onResize(ev => {
    api('/api/term/resize', { method: 'POST', body: {
      sid, cols: ev.cols, rows: ev.rows } }).catch(() => {});
  });

  stream(rec);
  dockShow();
  activateTerm(sid);
  setTimeout(() => term.focus(), 60);
}

function stream(rec) {
  if (rec.es) rec.es.close();
  const es = new EventSource(`/api/term/stream?sid=${encodeURIComponent(rec.sid)}&o=${rec.offset}`);
  rec.es = es;
  es.onmessage = ev => {
    let j;
    try { j = JSON.parse(ev.data); } catch (e) { return; }
    if (j.beat) return;
    if (j.d) {
      rec.term.write(b64bytes(j.d));
      rec.offset = j.o;
    }
    if (j.e) {
      rec.term.write(`\r\n\x1b[38;5;244m[连接已关闭${j.e !== 'closed' ? '：' + j.e : ''}]\x1b[0m\r\n`);
      es.close();
    }
  };
  es.onerror = () => { es.close(); };
}

function activateTerm(sid) {
  $$('.dtab').forEach(t => t.classList.toggle('on', t.dataset.sid === sid));
  $$('.term').forEach(p => p.classList.toggle('on', p.dataset.sid === sid));
  const rec = S.terms.find(t => t.sid === sid);
  if (rec) {
    setTimeout(() => {
      try { rec.fit.fit(); } catch (e) {}
      if (rec.term) rec.term.focus();
    }, 40);
  }
}

function closeTerm(sid) {
  const rec = S.terms.find(t => t.sid === sid);
  if (rec && rec.es) rec.es.close();
  if (rec && rec.term) { try { rec.term.dispose(); } catch (e) {} }
  S.terms = S.terms.filter(t => t.sid !== sid);
  const tab = $(`.dtab[data-sid="${sid}"]`);
  const pane = $(`.term[data-sid="${sid}"]`);
  if (tab) tab.remove();
  if (pane) pane.remove();
  if (!S.terms.length) dockHide();
  else activateTerm(S.terms[S.terms.length - 1].sid);
  if (!sid.startsWith('iframe:')) {
    api('/api/term/close', { method: 'POST', body: { sid } }).catch(() => {});
  }
}

/* ------------------------------------------------------------------ 镜像 */
async function loadImages() {
  if (!S.info) {
    $('#imgTable tbody').innerHTML = '<tr><td colspan="5" class="empty">未连接服务器</td></tr>';
    return;
  }
  if (!S.info.ssh) {
    $('#imgTable tbody').innerHTML =
      '<tr><td colspan="5" class="empty">镜像导入/导出需要 SSH 连接 —— 请到「设置」填写 root 密码后重新连接</td></tr>';
    $('#iolBanner').innerHTML = '';
    return;
  }
  const d = await api('/api/images');
  const tb = $('#imgTable tbody');
  if (!d.images.length) {
    tb.innerHTML = '<tr><td colspan="5" class="empty">没有任何镜像</td></tr>';
  } else {
    tb.innerHTML = d.images.map(im => `
      <tr>
        <td>${esc(im.name)}</td>
        <td><span class="badge">${esc(im.type)}</span></td>
        <td>${esc(im.size_h)}</td>
        <td>${im.has_template ? '<span class="badge ok">' + esc(im.template || 'ok') + '</span>'
                              : '<span class="badge warn">无模板</span>'}</td>
        <td><button class="btn sm ghost" data-exp="${esc(im.name)}">导出</button></td>
      </tr>`).join('');
  }
  tb.onclick = async ev => {
    const b = ev.target.closest('[data-exp]');
    if (!b) return;
    const im = d.images.find(x => x.name === b.dataset.exp);
    if (!im) return;
    try {
      const r = await api('/api/images/export', { method: 'POST', body: { image: {
        name: im.name, path: im.path, is_dir: im.type === 'qemu' } } });
      toast('开始导出，完成后可在任务里打开', 'ok');
      pollTasks();
      void r;
    } catch (e) { fail(e); }
  };
  refreshIol().catch(() => {});
}

async function refreshIol() {
  const d = await api('/api/iol');
  const el = $('#iolBanner');
  if (!d.bins) { el.innerHTML = '<span class="badge">未安装 IOL 镜像</span>'; return; }
  el.innerHTML = d.ok
    ? `<span class="badge ok">IOL 许可证正常</span> <span class="muted">${esc(d.key)} · ${esc(d.hostname)}</span>`
    : `<span class="badge err">IOL 许可证异常</span>
       <span class="muted">iourc ${d.iourc ? '已存在' : '缺失'} · 节点缺授权 ${d.missing}</span>
       <button class="btn sm" id="btnFixIol">立即修复</button>`;
  const b = $('#btnFixIol');
  if (b) b.onclick = async () => {
    try { const r = await api('/api/iol/fix', { method: 'POST' });
      toast('已写入 iourc，补链 ' + r.linked + ' 个节点', 'ok'); refreshIol(); }
    catch (e) { fail(e); }
  };
}

function startImport(file) {
  const xhr = new XMLHttpRequest();
  api('/api/images/import', { method: 'POST', body: { name: file.name } }).then(r => {
    const tid = r.tid;
    xhr.open('POST', `/api/upload?tid=${tid}&name=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = ev => {
      if (ev.lengthComputable) $('#tasks').dataset.pct = Math.round(ev.loaded / ev.total * 100);
    };
    xhr.onload = () => { pollTasks(); };
    xhr.onerror = () => { toast('上传失败', 'err'); pollTasks(); };
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.send(file);
    toast('上传中：' + file.name);
    pollTasks();
  }).catch(fail);
}

let taskTimer = null;
const exportNotified = new Set();   // 已经弹过「已导出到 …」的任务 id，别重复弹

/* ---------------------------------------------------------------- 实验导出 / 导入
   导出：先弹原生目录选择框（默认停在上次选的目录），选完再打包。
        .unl 原样打包（拓扑 + 内嵌在 <objects><configs> 里的节点配置都在里面），
        外加本机的文本框侧车。后端生成 zip 后走现成的任务 + 下载通道。
   导入：把 zip 传上去，后端建实验 → 写 .unl → 恢复文本框，回来直接打开。   */
let exportLastDir = '';

async function pickExportDir() {
  try {
    const d = await api('/api/dialog/dir', { method: 'POST', body: { start: exportLastDir } });
    return { ok: true, dir: d.dir || '' };
  } catch (e) {
    // 没图形窗口（--no-gui / 直接用浏览器打开）时弹不出选择框，
    // 不能因此不让导出 —— 退回默认目录，并把原因说清楚
    console.warn('[选目录]', e);
    return { ok: false, dir: '' };
  }
}

async function exportLab() {
  if (!S.labPath) return toast('先打开一个实验', 'warn');
  const pick = await pickExportDir();
  if (pick.ok && !pick.dir) return toast('已取消导出', 'warn');   // 用户点了取消
  if (!pick.ok) toast('弹不出目录选择框，先导出到默认目录', 'warn');

  try {
    const d = await api('/api/lab/export', {
      method: 'POST', body: { path: S.labPath, out_dir: pick.dir },
    });
    if (pick.dir) exportLastDir = pick.dir;
    toast('正在打包实验…');
    pollTasks();
    return d;
  } catch (e) { fail(e); }
}

async function revealPath(p) {
  try {
    await api('/api/reveal', { method: 'POST', body: { path: p } });
  } catch (e) { fail(e); }
}

/* 导出完成后浮一张卡：完整路径 + 打开文件夹。
   任务列表在「镜像库」页里，拓扑页上看不到，光靠 toast（2.6s）也不够。 */
let fcardTimer = null;
function exportCard(file) {
  let el = $('#exportCard');
  if (!el) {
    el = document.createElement('div');
    el.id = 'exportCard';
    el.className = 'fcard';
    document.body.appendChild(el);
  }
  el.innerHTML = `<div class="h"><span>已导出到</span><span class="x" title="关闭">✕</span></div>
    <div class="p">${esc(file)}</div>
    <div class="acts"><button class="btn sm ghost" id="ecOpen">打开文件夹</button></div>`;
  el.querySelector('.x').onclick = () => el.classList.remove('on');
  el.querySelector('#ecOpen').onclick = () => revealPath(file);
  requestAnimationFrame(() => el.classList.add('on'));
  clearTimeout(fcardTimer);
  fcardTimer = setTimeout(() => el.classList.remove('on'), 25000);
}

function importLab(file) {
  if (!file) return;
  const name = String(file.name || '').replace(/\.zip$/i, '') || 'imported';
  toast('正在导入 ' + file.name + '…');
  fetch('/api/lab/import?name=' + encodeURIComponent(name), {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: file,
  }).then(async r => {
    let d;
    try { d = await r.json(); } catch (e) { throw new Error('HTTP ' + r.status); }
    if (!d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    await loadLabs('');
    toast(`已导入 ${d.name}：${d.nodes} 节点 · ${d.configs} 个配置 · ${d.textboxes} 个文本框`, 'ok');
    if ((d.warnings || []).length) {
      console.warn('[导入警告]', d.warnings);
      setTimeout(() => toast(d.warnings[0], 'warn'), 1400);
    }
    return openLab(d.path);
  }).catch(fail);
}

async function pollTasks() {
  clearTimeout(taskTimer);
  try {
    const d = await api('/api/tasks');
    const el = $('#tasks');
    if (!d.items.length) { el.innerHTML = ''; }
    else {
      el.innerHTML = d.items.map(t => {
        const cls = t.status === 'done' ? 'done' : t.status === 'failed' ? 'fail' : '';
        // 状态是后端给的英文枚举，先映射成中文再交给 i18n 翻（否则中文界面里全是 done/failed）
        const stName = { queued: '排队中', running: '进行中', done: '完成',
                         failed: '失败', cancelled: '已取消' }[t.status] || t.status;
        const right = t.status === 'done' && t.file
          ? `<button class="btn sm ghost" data-reveal="${esc(t.file)}">打开文件夹</button>
             <a class="btn sm ghost" href="/api/download?tid=${t.id}">下载</a>` : '';
        return `<div class="task">
          <div class="t1"><span>${esc(t.title)}</span><span class="badge">${esc(stName)}</span></div>
          <div class="t2">${esc(t.stage)} ${t.total ? '· ' + t.sent_h + ' / ' + t.total_h : ''} ${t.speed_h}</div>
          <div class="bar ${cls}"><i style="width:${t.percent}%"></i></div>
          ${t.message ? `<div class="t2">${esc(t.message)}</div>` : ''}
          <div style="text-align:right">${right}</div>
        </div>`;
      }).join('');
      // 「打开文件夹」：导出完直接把资源管理器定位到那个文件
      el.querySelectorAll('[data-reveal]').forEach(b => {
        b.onclick = () => revealPath(b.getAttribute('data-reveal'));
      });
      // 导出完成：浮一张卡说明存哪了（列表在镜像库页，这里看不到），
      // 失败则直接报错，别让人以为成功了
      d.items.forEach(t => {
        if (t.status === 'done' && t.file && !exportNotified.has(t.id)) {
          exportNotified.add(t.id);
          if (t.kind === 'lab-export') exportCard(t.file);
        }
        if (t.status === 'failed' && t.kind === 'lab-export' && !exportNotified.has('f' + t.id)) {
          exportNotified.add('f' + t.id);
          toast(t.message || '导出失败', 'err', 5000);
        }
      });
    }
    const busy = d.items.some(t => t.status === 'running' || t.status === 'queued');
    if (busy) taskTimer = setTimeout(pollTasks, 1200);
  } catch (e) { /* ignore */ }
}

/* ------------------------------------------------------------------ 网络 */
async function loadNet() {
  if (!S.info || !S.info.ssh) {
    $('#pnetGrid').innerHTML =
      '<div class="empty" style="grid-column:1/-1">宿主机网络信息需要 SSH 连接 —— 请到「设置」填写 root 密码后重新连接</div>';
    $('#natPnet').innerHTML = [1, 2, 3, 4, 5, 6, 7, 8, 9].map(n =>
      `<option value="${n}">pnet${n}</option>`).join('');
    $('#natUp').innerHTML = '<option value="eth0">eth0</option>';
    return;
  }
  const d = await api('/api/net');
  $('#pnetGrid').innerHTML = d.pnets.map(p => `
    <div class="pnet ${p.mgmt ? 'mgmt' : ''} ${p.state === 'UP' ? 'up' : ''}">
      <div class="nm">${esc(p.name)}
        ${p.mgmt ? '<span class="badge">管理网</span>' : ''}
        ${p.state === 'UP' ? '<span class="badge ok">UP</span>' : '<span class="badge">DOWN</span>'}
      </div>
      <div class="sub">
        网卡：${p.ports.length ? esc(p.ports.join(', ')) : '（未绑定）'}<br>
        IP：${esc(p.ip || '—')}
      </div>
    </div>`).join('');

  $('#natPnet').innerHTML = [1, 2, 3, 4, 5, 6, 7, 8, 9].map(n =>
    `<option value="${n}"${n === d.suggest ? ' selected' : ''}>pnet${n}</option>`).join('');
  // 默认选中的必须是"默认路由所在网卡"（EVE 里通常是 pnet0，不是 eth0），
  // 选错的话 MASQUERADE 的 -o 匹配不到流量，表现为 NAT 配好却上不了网
  const up = d.upstream || 'eth0';
  $('#natUp').innerHTML = d.ifaces.map(i =>
    `<option value="${esc(i)}"${i === up ? ' selected' : ''}>${esc(i)}${i === up ? ' · 默认路由' : ''}</option>`).join('');
}

/* ------------------------------------------------------------------ 设置 */
function renderProfiles(active) {
  const sel = $('#profList');
  sel.innerHTML = S.profiles.map(p =>
    `<option value="${esc(p.id)}"${p.id === active ? ' selected' : ''}>${esc(p.name || p.host)}</option>`).join('');
  const p = S.profiles.find(x => x.id === active) || S.profiles[0];
  if (p) fillForm(p);
}

function fillForm(p) {
  const f = $('#profForm');
  ['name', 'host', 'api_port', 'eve_user', 'ssh_user', 'ssh_port'].forEach(k => {
    if (f[k]) f[k].value = p[k] == null ? '' : p[k];
  });
  // 老配置里没有 scheme 字段：按端口猜（443 基本就是 HTTPS）
  if (f.scheme) f.scheme.value = p.scheme || (Number(p.api_port) === 443 ? 'https' : 'http');
  f.eve_pass.value = '';
  f.ssh_pass.value = '';
  f.use_ssh.checked = p.use_ssh !== false;
  f.dataset.id = p.id || '';
}

/* ------------------------------------------------------------------ 事件 */
function switchView(name) {
  $$('#nav button').forEach(x => x.classList.toggle('on', x.dataset.view === name));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'view-' + name));
  if (name === 'images') loadImages().catch(fail);
  if (name === 'net') loadNet().catch(fail);
}

function bind() {
  $('#nav').onclick = e => {
    const b = e.target.closest('button[data-view]');
    if (b) switchView(b.dataset.view);
  };

  $('#btnRefreshLabs').onclick = () => loadLabs(S.cwd).catch(fail);
  $('#btnNewLab').onclick = () => newLab().catch(fail);
  $('#crumbs').onclick = e => {
    const a = e.target.closest('a[data-p]');
    if (a) loadLabs(a.dataset.p).catch(fail);
  };
  $('#labTree').onclick = e => {
    const up = e.target.closest('[data-up]');
    if (up) { const p = S.cwd.split('/'); p.pop(); loadLabs(p.join('/')).catch(fail); return; }
    const d = e.target.closest('[data-dir]');
    if (d) { loadLabs(d.dataset.dir).catch(fail); return; }
    const l = e.target.closest('[data-lab]');
    if (l) openLab(l.dataset.lab).catch(fail);
  };

  $('#btnFit').onclick = fit;
  $('#btnFit2').onclick = fit;
  $('#btnZoomIn').onclick = () => zoomAt(1.2);
  $('#btnZoomOut').onclick = () => zoomAt(1 / 1.2);
  $('#zoomLabel').onclick = zoomReset;
  $('#btnP2P').onclick = () => {
    S.showP2P = !S.showP2P;
    $('#btnP2P').classList.toggle('on', S.showP2P);
    renderTopology();
    toast(S.showP2P ? '已显示直连桥（可右键改名/删除）' : '直连桥已隐藏，线直接连两个节点', 'ok');
  };
  $('#btnReload').onclick = () => S.labPath && openLab(S.labPath).catch(fail);
  $('#btnLabExport').onclick = exportLab;
  $('#btnLabImport').onclick = () => {
    if (!(S.info || {}).host) return toast('先连接一台 EVE 服务器', 'warn');
    $('#labImportFile').click();
  };
  $('#labImportFile').onchange = e => {
    const f = (e.target.files || [])[0];
    e.target.value = '';          // 允许连续导入同一个文件
    importLab(f);
  };
  $('#btnAddNode').onclick = () => togglePalette();
  $('#btnAddText').onclick = () => {
    if (!S.labPath) return toast('先打开一个实验', 'warn');
    const box = $('#stage').getBoundingClientRect();
    addTextBoxes((box.width / 2 - S.tx) / S.z - 60,
                 (box.height / 2 - S.ty) / S.z - 60).catch(fail);
  };
  $('#btnCapture').onclick = () => toggleCapPanel();
  $('#palClose').onclick = () => togglePalette(false);
  $('#capClose').onclick = () => toggleCapPanel(false);
  $('#palSearch').oninput = e => renderPalette(e.target.value);
  $('#palList').onclick = e => {
    const it = e.target.closest('.pal-item');
    if (!it) return;
    if (it.dataset.ready !== '1') { toast('宿主没装这个模板的镜像，先导入镜像', 'warn'); return; }
    const box = $('#stage').getBoundingClientRect();
    const x = (box.width / 2 - S.tx) / S.z - 60, y = (box.height / 2 - S.ty) / S.z - 30;
    addNodeAt(it.dataset.tpl, x, y);
  };
  $('#palList').addEventListener('dragstart', e => {
    const it = e.target.closest('.pal-item');
    if (!it || it.dataset.ready !== '1') { e.preventDefault(); return; }
    e.dataTransfer.setData('text/eve-template', it.dataset.tpl);
    e.dataTransfer.effectAllowed = 'copy';
  });
  $('#capList').onclick = e => {
    const openLast = e.target.closest('#capOpenLast');
    if (openLast && S.lastPcap) { openPcap(S.lastPcap); return; }
    const b = e.target.closest('[data-cap-act]');
    if (!b) return;
    const sid = b.closest('.cap-item').dataset.sid;
    if (b.dataset.capAct === 'save') saveCap(sid);
    else discardCap(sid);
  };
  $('#btnStartAll').onclick = async () => {
    if (!S.labPath) return;
    try {
      const r = await api('/api/lab/action', { method: 'POST', body: { path: S.labPath, action: 'start' } });
      const total = (r.nodes || []).length;
      const bad = (r.failed || []);
      const clean = ((r.cleaned || {}).removed || []);
      let msg = `已启动 ${total - bad.length}/${total} 个节点`;
      if (clean.length) msg += `，清理残留网卡 ${clean.length} 个`;
      toast(msg, bad.length ? 'err' : 'ok');
      if (bad.length) console.warn('启动失败:', r.error);
      await openLab(S.labPath);
    } catch (e) { fail(e); }
  };
  $('#btnStopAll').onclick = async () => {
    if (!S.labPath) return;
    try { await api('/api/lab/action', { method: 'POST', body: { path: S.labPath, action: 'stop' } });
      toast('已停止全部', 'ok'); } catch (e) { fail(e); }
  };
  $('#btnCloseInspector').onclick = () => { S.sel = null; renderInspector(null);
    $$('.nd').forEach(el => el.classList.remove('sel')); };

  // 画布交互：左键空白拖 = 框选；中键 / Alt+左键 = 平移；节点上拖 = 改坐标（多选一起动）
  const stage = $('#stage');
  const SVGNS = 'http://www.w3.org/2000/svg';
  let pan = null, ndrag = null, mq = null, suppressClick = 0, tbxDrag = null;
  const ndEl = id => document.querySelector(`.nd[data-id="${id}"]`);

  stage.addEventListener('mousedown', e => {
    // 右键先当「平移候选」：真的拖了才平移，没拖就是普通右键菜单（节点/文本框上也一样）
    if (e.button === 2) {
      pan = { x: e.clientX, y: e.clientY, tx: S.tx, ty: S.ty, btn: 2, moved: false };
      stage.classList.add('dragging');
      return;
    }
    if (e.target.closest('.nd') || e.target.closest('.net') || e.target.closest('.tbx')) return;
    // 平移视角：中键 / Alt+左键 / 按住空格+左键
    if (e.button === 1 || (e.button === 0 && (e.altKey || spaceDown))) {
      e.preventDefault();
      pan = { x: e.clientX, y: e.clientY, tx: S.tx, ty: S.ty, btn: e.button, moved: false };
      stage.classList.add('dragging');
      return;
    }
    if (e.button !== 0 || !S.labPath) return;
    const r = stage.getBoundingClientRect();
    const additive = e.ctrlKey || e.shiftKey;
    mq = { x0: e.clientX, y0: e.clientY, left: r.left, top: r.top,
           base: additive ? new Set(S.selIds || []) : new Set(),
           baseTbx: additive ? new Set(S.tbxSel || []) : new Set(),
           additive, moved: false,
           cur: additive ? new Set(S.selIds || []) : new Set(),
           curTbx: additive ? new Set(S.tbxSel || []) : new Set() };
    if (!additive) { S.selIds = []; paintSel(); S.tbxSel = []; paintTbxSel(); }
  });

  $('#nodes').addEventListener('mousedown', e => {
    const tb = e.target.closest('.tbx');
    if (tb) {                                                      // 拖文本框（多选时整组走）
      if (e.button !== 0 || tb.classList.contains('editing')) return;
      const t = (S.texts || []).find(x => x.id === tb.dataset.tid);
      if (!t) return;
      e.preventDefault();          // 别把拖动变成选文字
      if (!(S.tbxSel || []).includes(t.id)) { S.tbxSel = [t.id]; paintTbxSel(); }
      const ids = S.tbxSel.slice();
      tbxDrag = {
        x: e.clientX, y: e.clientY, moved: false,
        items: ids.map(id => {
          const o = (S.texts || []).find(x => x.id === id);
          const el = document.querySelector(`.tbx[data-tid="${id}"]`);
          return o && el ? { t: o, el, ox: Number(o.x || 0), oy: Number(o.y || 0) } : null;
        }).filter(Boolean),
      };
      suppressClick = Date.now();
      return;
    }
    const el = e.target.closest('.nd');
    if (!el || !S.lab) return;
    const id = el.dataset.id;
    if (e.button === 0 && e.shiftKey) {                            // Shift+拖 = 拉线接线
      e.preventDefault();
      startLink(id, e);
      return;
    }
    if (e.button !== 0) return;
    // 注意：这里**不**改选择。选中统一交给 click（Ctrl 加选 / 普通单选），
    // 否则 mousedown 先设成单选、click 再 toggle 一次，Ctrl 多选会被抵消。
    const selected = (S.selIds || []).includes(id);
    const ids = selected ? S.selIds.slice() : [id];
    const start = {};
    ids.forEach(x => {
      const n = S.lab.nodes[x] || {};
      start[x] = { left: Number(n.left || 0), top: Number(n.top || 0) };
    });
    ndrag = { ids, start, x: e.clientX, y: e.clientY, moved: false, pending: !selected };
    ids.forEach(x => { const e2 = ndEl(x); if (e2) e2.classList.add('dragging'); });
    e.preventDefault();          // 别把拖动变成选文字
  });

  function startLink(id, ev, iface) {
    const src = S.lab.nodes[id];
    if (!src) return;
    const x1 = Number(src.left || 0), y1 = Number(src.top || 0);
    const line = document.createElementNS(SVGNS, 'line');
    line.setAttribute('x1', x1);
    line.setAttribute('y1', y1);
    line.setAttribute('x2', x1);
    line.setAttribute('y2', y1);
    line.setAttribute('class', 'linkline');
    $('#links').appendChild(line);
    S.linking = { id, iface: iface || '', line, x0: ev.clientX, y0: ev.clientY };
    $('#stage').classList.add('linking');
  }

  // 拖线时把鼠标底下的节点标出来，松手才知道会接到谁身上
  let linkHover = null;
  function setLinkTarget(el) {
    if (linkHover === el) return;
    if (linkHover) linkHover.classList.remove('linktarget');
    linkHover = el;
    if (el) el.classList.add('linktarget');
  }
  function clearLinkTarget() {
    setLinkTarget(null);
    const st = $('#stage');
    if (st) st.classList.remove('linking');
  }

  // 拖线时切窗口会收不到 mouseup，留着虚线会一直跟鼠标走
  window.addEventListener('blur', () => {
    if (!S.linking) return;
    try { S.linking.line.remove(); } catch (e) {}
    S.linking = null;
    clearLinkTarget();
  });

  window.addEventListener('mousemove', e => {
    if (tbxDrag) {
      const dx = (e.clientX - tbxDrag.x) / S.z, dy = (e.clientY - tbxDrag.y) / S.z;
      if (!tbxDrag.moved && Math.abs(dx) + Math.abs(dy) < 2) return;
      tbxDrag.moved = true;
      tbxDrag.items.forEach(it => {
        const nx = Math.round(it.ox + dx), ny = Math.round(it.oy + dy);
        it.el.style.left = nx + 'px';
        it.el.style.top = ny + 'px';
        it.t.x = nx;
        it.t.y = ny;
      });
      return;
    }
    if (ndrag) {
      const dx = (e.clientX - ndrag.x) / S.z, dy = (e.clientY - ndrag.y) / S.z;
      if (!ndrag.moved && Math.abs(dx) + Math.abs(dy) < 2) return;
      ndrag.moved = true;
      if (ndrag.pending) {             // 拖没选中的节点：拖动真的发生了才改成选中它
        ndrag.pending = false;
        S.selIds = ndrag.ids.slice();
        paintSel();
      }
      ndrag.ids.forEach(id => {
        const st = ndrag.start[id];
        const nx = Math.round(st.left + dx), ny = Math.round(st.top + dy);
        const el = ndEl(id);
        if (el) { el.style.left = nx + 'px'; el.style.top = ny + 'px'; }
        const n = S.lab.nodes[id];
        if (n) { n.left = nx; n.top = ny; }
      });
      drawLinks();
      return;
    }
    if (S.linking) {
      const r = stage.getBoundingClientRect();
      const x = (e.clientX - r.left - S.tx) / S.z, y = (e.clientY - r.top - S.ty) / S.z;
      S.linking.line.setAttribute('x2', Math.round(x));
      S.linking.line.setAttribute('y2', Math.round(y));
      const over = document.elementFromPoint(e.clientX, e.clientY);
      const nd = over && over.closest ? over.closest('.nd') : null;
      setLinkTarget(nd && nd.dataset.id !== String(S.linking.id) ? nd : null);
      return;
    }
    if (pan) {
      const dx = e.clientX - pan.x, dy = e.clientY - pan.y;
      if (!pan.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
      pan.moved = true;
      if (pan.btn === 2) rightPanAt = Date.now();  // 右键拖动过 → 松手别弹菜单
      S.tx = pan.tx + dx;
      S.ty = pan.ty + dy;
      applyT();
      return;
    }
    if (mq) {
      const dx = e.clientX - mq.x0, dy = e.clientY - mq.y0;
      if (!mq.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
      mq.moved = true;
      const el = $('#marquee');
      el.classList.add('on');
      el.style.left = (Math.min(e.clientX, mq.x0) - mq.left) + 'px';
      el.style.top = (Math.min(e.clientY, mq.y0) - mq.top) + 'px';
      el.style.width = Math.abs(dx) + 'px';
      el.style.height = Math.abs(dy) + 'px';
      const L = Math.min(e.clientX, mq.x0), R = Math.max(e.clientX, mq.x0);
      const T = Math.min(e.clientY, mq.y0), B = Math.max(e.clientY, mq.y0);
      const hit = new Set(mq.base);
      $$('.nd').forEach(nd => {
        const r = nd.getBoundingClientRect();
        if (!(r.right < L || r.left > R || r.bottom < T || r.top > B)) hit.add(nd.dataset.id);
      });
      mq.cur = hit;
      S.selIds = Array.from(hit);
      paintSel();
      // 文本框一起框：和节点各算各的集合，互不干扰
      const hitT = new Set(mq.baseTbx);
      $$('.tbx').forEach(el => {
        const r = el.getBoundingClientRect();
        if (!(r.right < L || r.left > R || r.bottom < T || r.top > B)) hitT.add(el.dataset.tid);
      });
      mq.curTbx = hitT;
      S.tbxSel = Array.from(hitT);
      paintTbxSel();
    }
  });

  window.addEventListener('mouseup', e => {
    if (pan && pan.moved) {                 // 平移结束才落一次盘，拖的过程中不写 localStorage
      if (pan.btn === 2) rightPanAt = Date.now();
      saveView();
    }
    pan = null;
    stage.classList.remove('dragging');

    if (tbxDrag) {
      const d = tbxDrag;
      tbxDrag = null;
      if (d.moved) { saveTexts(); suppressClick = Date.now(); }
      return;
    }

    if (S.linking) {
      const lk = S.linking;
      S.linking = null;
      try { lk.line.remove(); } catch (err) {}
      clearLinkTarget();
      const moved = Math.abs(e.clientX - lk.x0) + Math.abs(e.clientY - lk.y0) > 4;
      const tgt = document.elementFromPoint(e.clientX, e.clientY);
      const netEl = tgt && tgt.closest ? tgt.closest('.net') : null;
      const ndEl2 = tgt && tgt.closest ? tgt.closest('.nd') : null;
      const dstId = ndEl2 ? ndEl2.dataset.id : null;
      if (!moved) {
        // Shift+单击节点本来就是「加选」，别因为顺手起了一条线就弹警告
      } else if (netEl) { linkToNet(lk.id, netEl.dataset.netId, lk.iface).catch(fail); }
      else if (ndEl2 && dstId !== String(lk.id)) {
        suppressClick = Date.now();       // 松手后紧跟的 click 别把选择改了
        linkNodes(lk.id, dstId).catch(fail);
      } else if (ndEl2) {
        toast('不能连自己：拖到另一个节点或网络/云图标上', 'warn');
      } else {
        toast('松手时落在另一个节点或网络/云图标上才会接线', 'warn');
      }
    }

    if (mq) {
      const cur = mq;
      mq = null;
      const el = $('#marquee');
      el.classList.remove('on');
      el.style.width = el.style.height = '0px';
      if (cur.moved) {
        suppressClick = Date.now();      // 框选结束会紧跟一个 click，别把它当成「点空白取消选择」
        S.selIds = Array.from(cur.cur || []);
        paintSel();
        S.tbxSel = Array.from(cur.curTbx || []);
        paintTbxSel();
        syncInspector().catch(() => {});
      } else if (!cur.additive) {
        S.selIds = [];
        paintSel();
        S.tbxSel = [];
        paintTbxSel();
        renderInspector(null);
      }
      return;
    }

    if (ndrag) {
      const d = ndrag;
      ndrag = null;
      d.ids.forEach(x => { const el = ndEl(x); if (el) el.classList.remove('dragging'); });
      if (!d.moved) return;
      suppressClick = Date.now();      // 拖动后紧跟的 click 别把多选重置成单选
      (async () => {
        try {
          for (const id of d.ids) {
            const n = S.lab.nodes[id] || {};
            const r = await api('/api/node/move', { method: 'POST', body: guarded({
              path: S.labPath, id,
              left: Math.round(Number(n.left || 0)), top: Math.round(Number(n.top || 0)) }) });
            afterWrite(r);
          }
        } catch (err) { fail(err); openLab(S.labPath).catch(() => {}); }
      })();
    }
  });

  // Shift+拖到网络图标上松开 → 选接口，接上
  async function linkToNet(nodeId, netId, ifname) {
    const nd = S.lab.nodes[nodeId] || {};
    const net = (S.lab.networks || {})[netId] || {};
    let useIf = ifname;
    if (!useIf) {
      const d = await api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${nodeId}`);
      const ifs = Object.values((d.interfaces || {}).ethernet || {});
      if (!ifs.length) { toast('这个节点没有以太网卡', 'warn'); return; }
      const f = await modal(`接线 · ${nd.name || nodeId} → ${net.name || netId}`, [
        { key: 'iface', label: '接到哪个接口', type: 'select',
          value: freeIfaceOf(ifs).name, options: ifaceOptions(ifs) },
      ]);
      if (!f) return;
      useIf = f.iface;
    }
    const r = await api('/api/node/ifaces', { method: 'PUT', body: guarded({
      path: S.labPath, id: nodeId, ifaces: [{ name: useIf, network_id: Number(netId) }] }) });
    afterWrite(r);
    toast(`${nd.name || nodeId} 的 ${useIf} 已接到 ${net.name || netId}`, 'ok');
    await openLab(S.labPath);
    selectNode(String(nodeId)).catch(() => {});
  }

  /* Shift+拖到另一个节点上松开 → 两边各选一个网卡，共用一个网络（= 直连）。
     EVE 的模型里没有「节点→节点」的边，两点必须经 network；所以这里默认
     新建一个内部桥放在两节点中间，也可以选已有网络（等于接到同一个交换机）。 */
  function freeIfaceOf(list) {
    return list.find(it => !it.network_id || String(it.network_id) === '0') || list[0];
  }
  function ifaceOptions(list) {
    return list.map(it => {
      const used = it.network_id && String(it.network_id) !== '0';
      return { value: it.name, label: it.name + (used ? '（已接）' : '（空）') };
    });
  }
  function autoNetName() {
    let max = 0;
    Object.values((S.lab && S.lab.networks) || {}).forEach(n => {
      const m = /^Net-(\d+)$/.exec((n.name || '').trim());
      if (m) max = Math.max(max, Number(m[1]));
    });
    return 'Net-' + (max + 1);
  }

  async function linkNodes(aId, bId) {
    if (!S.labPath) return;
    const A = S.lab.nodes[aId] || {}, B = S.lab.nodes[bId] || {};
    let da, db;
    try {
      [da, db] = await Promise.all([
        api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${aId}`),
        api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${bId}`),
      ]);
    } catch (e) { return fail(e); }
    const ifsA = Object.values((da.interfaces || {}).ethernet || {});
    const ifsB = Object.values((db.interfaces || {}).ethernet || {});
    if (!ifsA.length || !ifsB.length) {
      toast(`${!ifsA.length ? A.name || aId : B.name || bId} 没有以太网卡`, 'warn');
      return;
    }
    const nets = Object.entries(S.lab.networks || {}).map(([id, v]) => ({ id, ...v }));
    const fields = [
      { key: 'aif', label: `${A.name || aId} 的接口`, type: 'select',
        value: freeIfaceOf(ifsA).name, options: ifaceOptions(ifsA) },
      { key: 'bif', label: `${B.name || bId} 的接口`, type: 'select',
        value: freeIfaceOf(ifsB).name, options: ifaceOptions(ifsB) },
      { key: 'net', label: '经由网络', type: 'select', value: '__new',
        options: [{ value: '__new', label: `＋ 新建内部桥（${autoNetName()}，放两节点中间）` }]
          .concat(nets.map(n => ({ value: String(n.id), label: `${n.name} (${n.type})` }))) },
    ];
    const f = await modal(`接线 · ${A.name || aId} ─ ${B.name || bId}`, fields);
    if (!f) return;
    const body = guarded({
      path: S.labPath,
      a: { id: aId, iface: f.aif },
      b: { id: bId, iface: f.bif },
    });
    if (!f.net || f.net === '__new') {
      body.name = autoNetName();
      body.left = Math.round((Number(A.left || 0) + Number(B.left || 0)) / 2);
      body.top = Math.round((Number(A.top || 0) + Number(B.top || 0)) / 2);
    } else {
      body.net_id = Number(f.net);
    }
    try {
      const r = await api('/api/lab/link', { method: 'POST', body });
      afterWrite(r);
      const via = (S.lab.networks || {})[r.net] || {};
      toast(`${A.name || aId}:${f.aif} ─ ${B.name || bId}:${f.bif} 已接`
        + (r.created ? `（新建 ${via.name || '网络'}）` : ''), 'ok');
      await openLab(S.labPath);
    } catch (e) { fail(e); }
  }

  // 从模板库拖到画布
  stage.addEventListener('dragover', e => {
    if (!S.labPath) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  stage.addEventListener('drop', e => {
    const tpl = e.dataTransfer.getData('text/eve-template');
    if (!tpl || !S.labPath) return;
    e.preventDefault();
    const box = stage.getBoundingClientRect();
    const x = (e.clientX - box.left - S.tx) / S.z;
    const y = (e.clientY - box.top - S.ty) / S.z;
    addNodeAt(tpl, x - 40, y - 30);
  });
  stage.addEventListener('wheel', e => {
    if (!S.lab) return;
    e.preventDefault();
    // 触控板横向两指、Shift+滚轮 → 平移；其余（鼠标滚轮、Ctrl+滚轮）→ 以光标为中心缩放
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY) && !e.ctrlKey) { panBy(-e.deltaX, 0); return; }
    if (e.shiftKey) { panBy(-e.deltaY, 0); return; }
    zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
  }, { passive: false });

  /* 视角键盘：按住空格＝抓手（左键拖动平移）；方向键推视角；Ctrl+0/1/± */
  const VIEW_STEP = { ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
  const focusOnCanvas = el => !el || el === document.body || stage.contains(el);
  window.addEventListener('keydown', e => {
    if (typing() || document.querySelector('.modal-mask')) return;
    if (e.code === 'Space' && focusOnCanvas(document.activeElement)) {
      e.preventDefault();                       // 空格默认会滚页/触发聚焦按钮
      if (!spaceDown) { spaceDown = true; stage.classList.add('panmode'); }
      return;
    }
    if (VIEW_STEP[e.key] && S.lab) {
      e.preventDefault();
      const st = e.shiftKey ? 260 : 80;
      panBy(VIEW_STEP[e.key][0] * st, VIEW_STEP[e.key][1] * st);
      return;
    }
    if (!(e.ctrlKey || e.metaKey) || !S.lab) return;
    if (e.key === '0') { e.preventDefault(); fit(); }
    else if (e.key === '1') { e.preventDefault(); zoomReset(); }
    else if (e.key === '=' || e.key === '+') { e.preventDefault(); zoomAt(1.2); }
    else if (e.key === '-' || e.key === '_') { e.preventDefault(); zoomAt(1 / 1.2); }
  });
  window.addEventListener('keyup', e => {
    if (e.code !== 'Space') return;
    spaceDown = false;
    stage.classList.remove('panmode');
  });
  window.addEventListener('blur', () => { spaceDown = false; stage.classList.remove('panmode'); });

  // Delete / Backspace 删掉选中的文本框；正在输入框里打字时不拦
  window.addEventListener('keydown', e => {
    if (e.key !== 'Delete' && e.key !== 'Backspace') return;
    if (!(S.tbxSel || []).length) return;
    const a = document.activeElement;
    if (a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return;
    if (document.querySelector('.modal-mask')) return;
    e.preventDefault();
    deleteSelectedTexts();
  });

  stage.addEventListener('click', e => {
    if (Date.now() - suppressClick < 350) return;
    if (e.target.closest('.nd') || e.target.closest('.net')
        || e.target.closest('.tbx') || e.target.closest('.marquee')) return;
    if (S.selIds && S.selIds.length) { S.selIds = []; paintSel(); renderInspector(null); }
    $$('.net').forEach(x => x.classList.remove('sel'));
    if ((S.tbxSel || []).length) { S.tbxSel = []; paintTbxSel(); }
  });
  $('#nodes').onclick = e => {
    if (Date.now() - suppressClick < 350) return;
    const nd = e.target.closest('.nd');
    if (nd) {
      const mode = (e.ctrlKey || e.metaKey) ? 'toggle' : (e.shiftKey ? 'add' : '');
      return selectNode(nd.dataset.id, mode).catch(fail);
    }
    const netEl = e.target.closest('.net');
    if (netEl) { selectNet(netEl.dataset.netId); }
  };
  $('#nodes').ondblclick = e => {
    const tb = e.target.closest('.tbx');
    if (tb) { editTextBox(tb); return; }
    const nd = e.target.closest('.nd');
    if (nd && S.lab.nodes[nd.dataset.id]) {
      const n = S.lab.nodes[nd.dataset.id];
      openNodeTerm(nd.dataset.id, n.name);
    }
  };

  // 右键菜单：网络 / 节点（单选或多选）/ 画布空白
  stage.addEventListener('contextmenu', e => {
    if (!S.lab) return;
    // 刚用右键拖过画布＝在平移视角，松手别弹菜单
    if ((pan && pan.btn === 2 && pan.moved) || Date.now() - rightPanAt < 400) {
      e.preventDefault();
      rightPanAt = 0;
      return;
    }
    const tbxEl = e.target.closest('.tbx');
    const netEl = e.target.closest('.net');
    const nd = e.target.closest('.nd');
    if (tbxEl) {
      e.preventDefault();
      const tid = tbxEl.dataset.tid;
      const t = (S.texts || []).find(x => x.id === tid) || {};
      // 右键的框若不在当前选中集里，就把选择切成它自己
      if (!(S.tbxSel || []).includes(tid)) { S.tbxSel = [tid]; paintTbxSel(); }
      const n = S.tbxSel.length;
      showCtx(e.clientX, e.clientY, [
        ['编辑文字', () => editTextBox(tbxEl)],
        ['复制文字', () => clipWrite(t.text || '').catch(fail)],
        null,
        n > 1 ? [`删除选中的 ${n} 个文本框`, () => deleteSelectedTexts()]
              : ['删除文本框', () => deleteTextBox(tbxEl)],
      ]);
      return;
    }
    if (!netEl && !nd) {
      e.preventDefault();
      const box = stage.getBoundingClientRect();
      const wx = (e.clientX - box.left - S.tx) / S.z;
      const wy = (e.clientY - box.top - S.ty) / S.z;
      showCtx(e.clientX, e.clientY, [
        ['添加文本框…', () => addTextBoxes(wx, wy).catch(fail)],
        ['添加节点…', () => togglePalette(true)],
        ['添加网络（云）…', () => createCloud()],
        null,
        ['全选节点', () => {
          S.selIds = Object.keys(S.lab.nodes || {});
          paintSel(); syncInspector().catch(() => {});
        }],
        ['适应窗口', () => fit()],
        ['恢复 100%', () => zoomReset()],
        ['重新加载', () => S.labPath && openLab(S.labPath).catch(fail)],
      ]);
      return;
    }
    if (netEl) {
      e.preventDefault();
      const nid = netEl.dataset.netId;
      const net = S.lab.networks[nid] || {};
      showCtx(e.clientX, e.clientY, [
        ['重命名网络…', () => renameNet(nid, net.name)],
        ['删除网络', () => deleteNetById(nid, net.name)],
      ]);
      return;
    }
    e.preventDefault();
    const id = nd.dataset.id;
    const n = S.lab.nodes[id] || {};
    const multi = (S.selIds || []).includes(id) && S.selIds.length > 1;
    if (multi) {
      const ids = S.selIds.slice();
      showCtx(e.clientX, e.clientY, [
        [`启动选中的 ${ids.length} 个`, () => bulkAct(ids, 'start')],
        [`停止选中的 ${ids.length} 个`, () => bulkAct(ids, 'stop')],
        ['批量接线…', () => linkDialog(ids)],
        ['统一图标…', () => pickIcon(ids)],
        null,
        [`擦除选中的 ${ids.length} 个`, () => bulkAct(ids, 'wipe')],
        [`删除选中的 ${ids.length} 个`, () => bulkAct(ids, 'del')],
        null,
        ['取消选择', () => { S.selIds = []; paintSel(); renderInspector(null); }],
      ]);
      return;
    }
    const con = String(n.console || '').toLowerCase();
    const wantGraph = con === 'vnc' || con === 'rdp';   // 这类节点没有串口
    const menu = [
      wantGraph ? ['打开图形控制台', () => openHtml5(n.url, n.name)]
                : ['打开 CLI 控制台', () => openNodeTerm(id, n.name)],
      ...(wantGraph && n.url
        ? [['在独立窗口打开（Ctrl+Alt+V 可粘贴）', () => popHtml5(n.url, n.name)]]
        : []),
      ['启动', () => act(id, 'start')],
      ['停止', () => act(id, 'stop')],
      null,
      ['属性（名字 / 内存 / 镜像）…', () => editNodeProps(n, null)],
      ['接线到网络…', () => linkDialog([id])],
      ['图标…', () => pickIcon([id])],
      ['抓包…', async () => {
        try {
          const d = await api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${id}`);
          const ifs = Object.values((d.interfaces || {}).ethernet || {});
          if (!ifs.length) { toast('这个节点没有以太网卡', 'warn'); return; }
          if (!isRunning(n)) {
            toast('节点没运行，先启动再抓包', 'warn');
            return;
          }
          showCtx(e.clientX, e.clientY, ifs.map(it =>
            ['抓包 ' + it.name + (it.network_id && String(it.network_id) !== '0' ? '' : '（未接线）'),
              () => startCapture(id, it.name)]));
        } catch (err) { fail(err); }
      }],
      null,
      ['擦除配置', () => act(id, 'wipe')],
      ['删除节点', () => deleteNodeById(id, n.name)],
    ];
    showCtx(e.clientX, e.clientY, menu);
  });

  // 选中多个节点后，右键批量；单选时按 id 处理——批量入口在菜单里已按 multi 分流

  async function renameNet(nid, cur) {
    const name = prompt('网络名称', cur || '');
    if (name == null || !name.trim()) return;
    try {
      const r = await api('/api/lab/network', { method: 'PUT', body: guarded({
        path: S.labPath, id: nid, net: { name: name.trim() } }) });
      afterWrite(r);
      toast('网络已改名', 'ok');
      await openLab(S.labPath);
    } catch (e) { fail(e); }
  }

  function selectNet(nid) {
    $$('.net').forEach(x => x.classList.toggle('sel', x.dataset.netId === nid));
    $$('.nd').forEach(x => x.classList.remove('sel'));
    S.selIds = [];
    const net = (S.lab.networks || {})[nid] || {};
    const box = $('#inspBody');
    box.onchange = null;
    box.innerHTML = `
      <div class="kv"><span class="k">类型</span><span class="v">网络（${esc(net.type || '-')}）</span></div>
      <div class="kv"><span class="k">名称</span><span class="v">${esc(net.name || nid)}</span></div>
      <div class="kv"><span class="k">编号</span><span class="v">${esc(nid)}</span></div>
      <div class="insp-actions">
        <button class="btn sm ghost" data-nact="rename">重命名</button>
        <button class="btn sm ghost" data-nact="del">删除网络</button>
      </div>
      <div class="sub-title">说明</div>
      <div class="hint-sm">这是一个网络/云，节点网卡接上来即可互通。想看某个接口的流量，选中节点后在「网卡」里点「抓包」。</div>`;
    box.onclick = ev => {
      const b = ev.target.closest('[data-nact]');
      if (!b) return;
      if (b.dataset.nact === 'rename') renameNet(nid, net.name);
      else deleteNetById(nid, net.name);
    };
  }

  // 终端 dock
  $('#dtabs').onclick = e => {
    const x = e.target.closest('.x');
    const pop = e.target.closest('.pop');
    const tab = e.target.closest('.dtab');
    if (!tab) return;
    if (x) closeTerm(tab.dataset.sid);
    else if (pop) popHtml5(tab.dataset.url, tab.dataset.title);
    else activateTerm(tab.dataset.sid);
  };
  $('#btnDockHide').onclick = dockHide;
  $('#dockToggle').onclick = dockShow;
  const rz = $('#dockResize');
  if (rz) {
    rz.onmousedown = ev => {
      ev.preventDefault();
      const startY = ev.clientY;
      const startH = $('#dock').getBoundingClientRect().height;
      document.body.classList.add('dock-resizing');
      const move = e2 => dockSetHeight(startH + (startY - e2.clientY));
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        document.body.classList.remove('dock-resizing');
        if (S.lab) fit();
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    };
    rz.ondblclick = () => dockSetHeight(Math.round(window.innerHeight * 0.62));
  }
  $('#btnHostShell').onclick = () => openHostShell();

  // 镜像
  $('#btnImport').onclick = () => $('#fileInput').click();
  $('#fileInput').onchange = e => {
    const f = e.target.files[0];
    if (f) startImport(f);
    e.target.value = '';
  };
  $('#btnScan').onclick = () => loadImages().catch(fail);
  $('#btnIol').onclick = async () => {
    try { const r = await api('/api/iol');
      toast(r.ok ? `IOL 正常（${r.key}）` : `IOL 异常：iourc ${r.iourc ? 'ok' : '缺'}，缺授权节点 ${r.missing}`,
        r.ok ? 'ok' : 'err');
      refreshIol(); } catch (e) { fail(e); }
  };

  // 网络
  $('#btnNetRefresh').onclick = () => loadNet().catch(fail);
  $('#btnNatApply').onclick = async () => {
    try {
      const r = await api('/api/net/nat', { method: 'POST', body: {
        pnet: Number($('#natPnet').value), upstream: $('#natUp').value,
        persist: $('#natPersist').checked } });
      $('#natOut').textContent = `已创建 ${r.pnet} → ${r.cidr}（网关 ${r.gw}，出口 ${r.upstream}）\n`
        + (r.persist ? '已写入 systemd，开机自动生效\n' : '注意：未持久化，重启后失效\n')
        + (r.stdout || '');
      toast('NAT 出口已就绪：' + r.pnet, 'ok');
      loadNet().catch(() => {});
    } catch (e) { $('#natOut').textContent = String(e.message); fail(e); }
  };
  $('#btnNatRemove').onclick = async () => {
    const n = Number($('#natPnet').value);
    if (!confirm(`卸载 pnet${n} 的 NAT 出口？`)) return;
    try {
      await api('/api/net/nat/remove', { method: 'POST', body: { pnet: n, upstream: $('#natUp').value } });
      $('#natOut').textContent = `已卸载 pnet${n}`;
      toast('已卸载', 'ok');
      loadNet().catch(() => {});
    } catch (e) { fail(e); }
  };

  // 设置
  $('#profList').onchange = e => {
    const p = S.profiles.find(x => x.id === e.target.value);
    if (p) fillForm(p);
  };
  // 端口和协议联动：填 443 基本就是 HTTPS，填 80 就是 HTTP（其它端口保持用户选择）
  const portEl = $('#profForm').api_port;
  if (portEl) {
    portEl.onchange = () => {
      const sc = $('#profForm').scheme;
      if (!sc) return;
      const n = Number(portEl.value);
      if (n === 443 || n === 8443) sc.value = 'https';
      else if (n === 80) sc.value = 'http';
    };
  }
  $('#profForm').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    const payload = {
      id: f.dataset.id || '',
      name: f.name.value, host: f.host.value.trim(),
      api_port: Number(f.api_port.value) || 80,
      scheme: (f.scheme && f.scheme.value) || 'http',
      eve_user: f.eve_user.value, eve_pass: f.eve_pass.value,
      use_ssh: f.use_ssh.checked, ssh_user: f.ssh_user.value,
      ssh_port: Number(f.ssh_port.value) || 22, ssh_pass: f.ssh_pass.value,
    };
    try {
      const r = await api('/api/profile', { method: 'POST', body: payload });
      S.profiles = r.items;
      await api('/api/profile/active', { method: 'POST', body: { id: r.id } });
      f.dataset.id = r.id;
      await connect({ profile: r.id });
    } catch (err) { fail(err); }
  };

  window.addEventListener('resize', () => {
    S.terms.forEach(t => { if (t.fit) { try { t.fit.fit(); } catch (e) {} } });
  });
  window.addEventListener('keydown', e => {
    if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 't') { e.preventDefault(); openHostShell(); }
  });
}

function act(id, action) {
  api('/api/node/action', { method: 'POST', body: { path: S.labPath, id, action } })
    .then(() => { toast('已' + (action === 'start' ? '启动' : action === 'stop' ? '停止' : '擦除'), 'ok');
      setTimeout(() => S.labPath && openLab(S.labPath).catch(() => {}), 800); })
    .catch(fail);
}

function showCtx(x, y, items) {
  const m = $('#ctxmenu');
  m.innerHTML = items.map(it => it === null ? '<hr>' :
    `<div data-i="${items.indexOf(it)}">${esc(it[0])}</div>`).join('');
  m.style.left = x + 'px';
  m.style.top = y + 'px';
  m.classList.add('on');
  const close = () => { m.classList.remove('on'); document.removeEventListener('click', close); };
  setTimeout(() => document.addEventListener('click', close), 0);
  m.onclick = e => {
    const d = e.target.closest('[data-i]');
    if (!d) return;
    const it = items[Number(d.dataset.i)];
    close();
    if (it) it[1]();
  };
}

/* ------------------------------------------------------------------ 启动 */
bind();
boot();
setInterval(() => {
  api('/api/state').then(s => {
    const was = !!S.info;
    S.info = s.info && s.info.host ? s.info : null;
    renderConn();
    // 后台自动连接终于连上了（或者刚被断开）——把实验列表补上
    if (!was && S.info) loadLabs(S.cwd).catch(() => {});
    if (was && !S.info) { $('#labTree').innerHTML = '<div class="empty">未连接</div>'; }
  }).catch(() => {});
}, 15000);
