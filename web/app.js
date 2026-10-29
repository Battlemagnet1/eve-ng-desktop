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
function toast(msg, kind = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast on ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast ' + kind; }, 2600);
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
    const st = await api('/api/state');
    S.info = st.info && st.info.host ? st.info : null;
    S.profiles = st.profiles || [];
    renderConn();
    renderProfiles(st.active);
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
    ' / <a data-p="' + esc(parts.slice(0, i + 1).join('/')) + '">' + esc(p) + '</a>').join('');

  let html = '';
  if (S.cwd) html += `<div class="lab-item" data-up="1"><span class="ico">↑</span>上一级</div>`;
  (S.labs.folders || []).forEach(f => {
    html += `<div class="lab-item" data-dir="${esc(f.path || f.name)}">
      <span class="ico">▸</span>${esc(f.name)}</div>`;
  });
  (S.labs.labs || []).forEach(l => {
    const on = l.path === S.labPath ? ' on' : '';
    html += `<div class="lab-item${on}" data-lab="${esc(l.path)}">
      <span class="ico">◈</span>${esc(l.name)}
      <span class="tag">${esc((l.mtime || '').slice(0, 11))}</span></div>`;
  });
  tree.innerHTML = html || '<div class="empty">空目录</div>';
}

/* ------------------------------------------------------------------ 拓扑 */
async function openLab(path) {
  S.labPath = path;
  S.sel = null;
  try { history.replaceState(null, '', '#lab=' + encodeURIComponent(path)); } catch (e) {}
  const d = await api('/api/lab?path=' + encodeURIComponent(path));
  S.lab = d;
  $('#labTitle').textContent = (d.meta && (d.meta.name || path)) + `　${d.total} 节点 · 运行 ${d.running}`;
  $('#stageHint').style.display = 'none';
  renderInspector(null);
  renderTopology();
  renderLabs();
  startPoll();
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
      $('#labTitle').textContent = (d.meta && (d.meta.name || S.labPath)) + `　${d.total} 节点 · 运行 ${d.running}`;
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

function renderTopology() {
  const L = S.lab;
  const nodesEl = $('#nodes'), linksEl = $('#links'), world = $('#world');
  nodesEl.innerHTML = '';
  linksEl.innerHTML = '';
  if (!L) return;

  const pts = {};
  Object.entries(L.nodes || {}).forEach(([id, n]) => {
    pts['node' + id] = { x: Number(n.left || 0), y: Number(n.top || 0), node: n, id };
    const el = document.createElement('div');
    el.className = 'nd' + (isRunning(n) ? ' running' : '');
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

  const isCloud = t => /^pnet\d+$|^ovs$|^cloud/.test(t || '');
  Object.entries(L.networks || {}).forEach(([id, n]) => {
    if (String(n.visibility) === '0' && !isCloud(n.type)) return;
    const el = document.createElement('div');
    el.className = 'net' + (isCloud(n.type) ? ' cloud' : '');
    el.dataset.netId = id;
    el.style.left = (Number(n.left || 0)) + 'px';
    el.style.top = (Number(n.top || 0)) + 'px';
    el.textContent = (n.name || n.type || '').slice(0, 7);
    el.title = n.name + ' · ' + n.type;
    nodesEl.appendChild(el);
  });

  const svgNS = 'http://www.w3.org/2000/svg';
  const w = Math.max(3200, ...Object.values(pts).map(p => p.x + 400));
  const h = Math.max(2200, ...Object.values(pts).map(p => p.y + 400));
  linksEl.setAttribute('width', w);
  linksEl.setAttribute('height', h);

  (L.links || []).forEach(l => {
    const A = pts[l.source] || pts[(l.source_type || 'node') + String(l.source).replace(/\D/g, '')];
    const B = pts[l.destination] || pts[(l.destination_type || 'node') + String(l.destination).replace(/\D/g, '')];
    if (!A || !B) return;
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', A.x); line.setAttribute('y1', A.y);
    line.setAttribute('x2', B.x); line.setAttribute('y2', B.y);
    line.setAttribute('stroke', '#33404e');
    line.setAttribute('stroke-width', '2');
    linksEl.appendChild(line);
  });

  applyT();
  fit();
}

function applyT() {
  $('#world').style.transform = `translate(${S.tx}px,${S.ty}px) scale(${S.z})`;
  $('#zoomLabel').textContent = Math.round(S.z * 100) + '%';
}

function fit() {
  const L = S.lab;
  if (!L) return;
  const xs = [], ys = [];
  Object.values(L.nodes || {}).forEach(n => { xs.push(Number(n.left || 0)); ys.push(Number(n.top || 0)); });
  Object.values(L.networks || {}).forEach(n => { xs.push(Number(n.left || 0)); ys.push(Number(n.top || 0)); });
  if (!xs.length) return;
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const box = $('#stage').getBoundingClientRect();
  const pad = 90;
  const zx = (box.width - pad * 2) / Math.max(maxX - minX, 120);
  const zy = (box.height - pad * 2) / Math.max(maxY - minY, 90);
  S.z = Math.min(1.35, Math.max(0.25, Math.min(zx, zy)));
  S.tx = box.width / 2 - ((minX + maxX) / 2) * S.z;
  S.ty = box.height / 2 - ((minY + maxY) / 2) * S.z;
  applyT();
}

/* ------------------------------------------------------------------ 检查器 */
async function selectNode(id) {
  S.sel = id;
  $$('.nd').forEach(el => el.classList.toggle('sel', el.dataset.id === id));
  const d = await api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${id}`);
  renderInspector(d);
}

function renderInspector(d) {
  const box = $('#inspBody');
  if (!d) { box.innerHTML = '<div class="empty">选中一个节点</div>'; return; }
  const n = d.node || {};
  const nets = Object.entries(S.lab.networks || {}).map(([id, v]) => ({ id, ...v }));
  const ifs = (d.interfaces && d.interfaces.ethernet) || {};
  const rows = Object.entries(ifs).map(([idx, it]) => {
    const opts = ['<option value="0">未连接</option>'].concat(nets.map(x =>
      `<option value="${x.id}"${String(it.network_id) === String(x.id) ? ' selected' : ''}>${esc(x.name)} (${esc(x.type)})</option>`)).join('');
    return `<div class="iface-row"><span class="nm">${esc(it.name)}</span>
      <select data-if="${esc(it.name)}">${opts}</select></div>`;
  }).join('');

  box.innerHTML = `
    <div class="kv"><span class="k">名称</span><span class="v">${esc(n.name)}</span></div>
    <div class="kv"><span class="k">模板</span><span class="v">${esc(n.template || n.type)}</span></div>
    <div class="kv"><span class="k">镜像</span><span class="v">${esc(n.image || '-')}</span></div>
    <div class="kv"><span class="k">内存</span><span class="v">${esc(n.ram || '-')} MB</span></div>
    <div class="kv"><span class="k">状态</span><span class="v">${isRunning(n) ? '运行中' : '已停止'}</span></div>
    <div class="kv"><span class="k">控制台端口</span><span class="v">${d.console_port}</span></div>
    <div class="insp-actions">
      <button class="btn sm" data-act="start">启动</button>
      <button class="btn sm ghost" data-act="stop">停止</button>
      <button class="btn sm ghost" data-act="wipe">擦除</button>
      <button class="btn sm" data-act="term">CLI 控制台</button>
      ${d.html5 ? '<button class="btn sm ghost" data-act="html5">图形控制台</button>' : ''}
    </div>
    <div class="sub-title">网卡</div>
    ${rows || '<div class="empty">无以太网卡</div>'}
    <div style="margin-top:10px"><button class="btn sm ghost" data-act="savif">保存网卡接线</button>
      <button class="btn sm ghost" data-act="newcloud">新建云</button></div>`;

  box.onclick = async ev => {
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
      } else if (act === 'savif') {
        const arr = $$('select[data-if]').map(s => ({ name: s.dataset.if, network_id: Number(s.value) }));
        await api('/api/node/ifaces', { method: 'PUT', body: { path: S.labPath, id: n.id, ifaces: arr } });
        toast('网卡接线已保存', 'ok');
        setTimeout(() => openLab(S.labPath), 600);
      } else if (act === 'newcloud') {
        await createCloud();
      }
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

/* ------------------------------------------------------------------ 终端 */
function dockShow() {
  $('#dock').classList.add('open');
  $('#dockToggle').classList.remove('show');
  setTimeout(() => { if (S.lab) fit(); }, 200);
}
function dockHide() {
  $('#dock').classList.remove('open');
  $('#dockToggle').classList.add('show');
  setTimeout(() => { if (S.lab) fit(); }, 200);
}

async function openNodeTerm(id, name) {
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

function openHtml5(url, name) {
  if (!url) return toast('该节点没有图形控制台', 'err');
  addIframe('html5-' + name, name + '（图形）', url);
}

function addIframe(key, title, url) {
  const sid = 'iframe:' + key + ':' + Date.now();
  const tab = document.createElement('div');
  tab.className = 'dtab';
  tab.dataset.sid = sid;
  tab.innerHTML = `${esc(title)}<span class="x">×</span>`;
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
async function pollTasks() {
  clearTimeout(taskTimer);
  try {
    const d = await api('/api/tasks');
    const el = $('#tasks');
    if (!d.items.length) { el.innerHTML = ''; }
    else {
      el.innerHTML = d.items.map(t => {
        const cls = t.status === 'done' ? 'done' : t.status === 'failed' ? 'fail' : '';
        const right = t.status === 'done' && t.file
          ? `<a class="btn sm ghost" href="/api/download?tid=${t.id}">下载</a>` : '';
        return `<div class="task">
          <div class="t1"><span>${esc(t.title)}</span><span class="badge">${esc(t.status)}</span></div>
          <div class="t2">${esc(t.stage)} ${t.total ? '· ' + t.sent_h + ' / ' + t.total_h : ''} ${t.speed_h}</div>
          <div class="bar ${cls}"><i style="width:${t.percent}%"></i></div>
          ${t.message ? `<div class="t2">${esc(t.message)}</div>` : ''}
          <div style="text-align:right">${right}</div>
        </div>`;
      }).join('');
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
  $('#btnReload').onclick = () => S.labPath && openLab(S.labPath).catch(fail);
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

  // 画布交互
  const stage = $('#stage');
  let drag = null;
  stage.addEventListener('mousedown', e => {
    if (e.target.closest('.nd') || e.target.closest('.net') || e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, tx: S.tx, ty: S.ty };
    stage.classList.add('dragging');
  });
  window.addEventListener('mousemove', e => {
    if (!drag) return;
    S.tx = drag.tx + (e.clientX - drag.x);
    S.ty = drag.ty + (e.clientY - drag.y);
    applyT();
  });
  window.addEventListener('mouseup', () => { drag = null; stage.classList.remove('dragging'); });
  stage.addEventListener('wheel', e => {
    if (!S.lab) return;
    e.preventDefault();
    const box = stage.getBoundingClientRect();
    const mx = e.clientX - box.left, my = e.clientY - box.top;
    const k = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    const z2 = Math.min(3, Math.max(0.15, S.z * k));
    S.tx = mx - (mx - S.tx) * (z2 / S.z);
    S.ty = my - (my - S.ty) * (z2 / S.z);
    S.z = z2;
    applyT();
  }, { passive: false });

  $('#nodes').onclick = e => {
    const nd = e.target.closest('.nd');
    if (nd) return selectNode(nd.dataset.id).catch(fail);
  };
  $('#nodes').ondblclick = e => {
    const nd = e.target.closest('.nd');
    if (nd && S.lab.nodes[nd.dataset.id]) {
      const n = S.lab.nodes[nd.dataset.id];
      openNodeTerm(nd.dataset.id, n.name);
    }
  };
  $('#nodes').oncontextmenu = e => {
    const nd = e.target.closest('.nd');
    if (!nd) return;
    e.preventDefault();
    const n = S.lab.nodes[nd.dataset.id] || {};
    const menu = [
      ['打开 CLI 控制台', () => openNodeTerm(nd.dataset.id, n.name)],
      ['启动', () => act(nd.dataset.id, 'start')],
      ['停止', () => act(nd.dataset.id, 'stop')],
      null,
      ['擦除配置', () => act(nd.dataset.id, 'wipe')],
    ];
    showCtx(e.clientX, e.clientY, menu);
  };

  // 终端 dock
  $('#dtabs').onclick = e => {
    const x = e.target.closest('.x');
    const tab = e.target.closest('.dtab');
    if (!tab) return;
    if (x) closeTerm(tab.dataset.sid); else activateTerm(tab.dataset.sid);
  };
  $('#btnDockHide').onclick = dockHide;
  $('#dockToggle').onclick = dockShow;
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
  $('#profForm').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    const payload = {
      id: f.dataset.id || '',
      name: f.name.value, host: f.host.value.trim(),
      api_port: Number(f.api_port.value) || 80, scheme: 'http',
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
setInterval(() => { if (S.info) api('/api/state').then(s => { S.info = s.info; renderConn(); }).catch(() => {}); }, 15000);
