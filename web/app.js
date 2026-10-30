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
    let st = await api('/api/state');
    S.info = st.info && st.info.host ? st.info : null;
    // 启动时的「自动连接」在后台线程里跑，页面往往比它先就绪 —— 等一会儿，
    // 否则用户看到的是「未连接」然后一直不动（旧版本就卡在这儿）。
    for (let i = 0; !S.info && i < 20; i++) {
      await new Promise(r => setTimeout(r, 500));
      st = await api('/api/state');
      S.info = st.info && st.info.host ? st.info : null;
    }
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
  S.selIds = [];
  try { history.replaceState(null, '', '#lab=' + encodeURIComponent(path)); } catch (e) {}
  const d = await api('/api/lab?path=' + encodeURIComponent(path));
  S.lab = d;
  S.umtime = d.umtime;
  $('#labTitle').textContent = (d.meta && (d.meta.name || path)) + `　${d.total} 节点 · 运行 ${d.running}`;
  $('#stageHint').style.display = 'none';
  renderInspector(null);
  renderTopology();
  paintSel();
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

  drawLinks();
  applyT();
  fit();
}

/* 连线单独一层：拖动节点时只重画线，不重算缩放（否则视角会跳） */
function drawLinks() {
  const L = S.lab;
  const linksEl = $('#links');
  if (!L || !linksEl) return;
  linksEl.innerHTML = '';
  const pts = {};
  Object.entries(L.nodes || {}).forEach(([id, n]) => {
    pts['node' + id] = { x: Number(n.left || 0), y: Number(n.top || 0) };
  });
  Object.entries(L.networks || {}).forEach(([id, n]) => {
    pts['network' + id] = { x: Number(n.left || 0), y: Number(n.top || 0) };
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
        await api('/api/lab/node?path=' + encodeURIComponent(S.labPath)
          + '&id=' + encodeURIComponent(id) + '&umtime=' + encodeURIComponent(S.umtime),
          { method: 'DELETE' });
      } else {
        await api('/api/node/action', { method: 'POST', body: { path: S.labPath, id, action } });
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
      <button class="btn sm ghost" data-act="rename">改名</button>
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
      } else if (act === 'rename') {
        await renameNode(n.id, n.name);
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
  $('#btnAddNode').onclick = () => togglePalette();
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
  let pan = null, ndrag = null, mq = null, suppressClick = 0;
  const ndEl = id => document.querySelector(`.nd[data-id="${id}"]`);

  stage.addEventListener('mousedown', e => {
    if (e.target.closest('.nd') || e.target.closest('.net')) return;
    if (e.button === 1 || (e.button === 0 && e.altKey)) {          // 平移
      e.preventDefault();
      pan = { x: e.clientX, y: e.clientY, tx: S.tx, ty: S.ty };
      stage.classList.add('dragging');
      return;
    }
    if (e.button !== 0 || !S.labPath) return;
    const r = stage.getBoundingClientRect();
    const additive = e.ctrlKey || e.shiftKey;
    mq = { x0: e.clientX, y0: e.clientY, left: r.left, top: r.top,
           base: additive ? new Set(S.selIds || []) : new Set(),
           additive, moved: false, cur: additive ? new Set(S.selIds || []) : new Set() };
    if (!additive) { S.selIds = []; paintSel(); }
  });

  $('#nodes').addEventListener('mousedown', e => {
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

  function startLink(id, ev) {
    const src = S.lab.nodes[id];
    if (!src) return;
    const line = document.createElementNS(SVGNS, 'line');
    line.setAttribute('x1', Number(src.left || 0));
    line.setAttribute('y1', Number(src.top || 0));
    line.setAttribute('x2', Number(src.left || 0));
    line.setAttribute('y2', Number(src.top || 0));
    line.setAttribute('class', 'linkline');
    $('#links').appendChild(line);
    S.linking = { id, line };
  }

  window.addEventListener('mousemove', e => {
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
      return;
    }
    if (pan) {
      S.tx = pan.tx + (e.clientX - pan.x);
      S.ty = pan.ty + (e.clientY - pan.y);
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
    }
  });

  window.addEventListener('mouseup', e => {
    pan = null;
    stage.classList.remove('dragging');

    if (S.linking) {
      const lk = S.linking;
      S.linking = null;
      try { lk.line.remove(); } catch (err) {}
      const tgt = document.elementFromPoint(e.clientX, e.clientY);
      const netEl = tgt && tgt.closest ? tgt.closest('.net') : null;
      if (netEl) { linkToNet(lk.id, netEl.dataset.netId).catch(fail); }
      else { toast('要接到网络/云图标上（Shift+拖动 = 接线）', 'warn'); }
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
        syncInspector().catch(() => {});
      } else if (!cur.additive) {
        S.selIds = [];
        paintSel();
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
  async function linkToNet(nodeId, netId) {
    const nd = S.lab.nodes[nodeId] || {};
    const net = (S.lab.networks || {})[netId] || {};
    const d = await api(`/api/node?path=${encodeURIComponent(S.labPath)}&id=${nodeId}`);
    const ifs = Object.values((d.interfaces || {}).ethernet || {});
    if (!ifs.length) { toast('这个节点没有以太网卡', 'warn'); return; }
    const free = ifs.find(it => !it.network_id || String(it.network_id) === '0') || ifs[0];
    const f = await modal(`接线 · ${nd.name || nodeId} → ${net.name || netId}`, [
      { key: 'iface', label: '接到哪个接口', type: 'select', value: free.name,
        options: ifs.map(it => ({ value: it.name,
          label: it.name + ((it.network_id && String(it.network_id) !== '0') ? '（已接）' : '（空）') })) },
    ]);
    if (!f) return;
    const r = await api('/api/node/ifaces', { method: 'PUT', body: guarded({
      path: S.labPath, id: nodeId, ifaces: [{ name: f.iface, network_id: Number(netId) }] }) });
    afterWrite(r);
    toast(`${nd.name || nodeId} 的 ${f.iface} 已接到 ${net.name || netId}`, 'ok');
    await openLab(S.labPath);
    selectNode(String(nodeId)).catch(() => {});
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
    const box = stage.getBoundingClientRect();
    const mx = e.clientX - box.left, my = e.clientY - box.top;
    const k = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    const z2 = Math.min(3, Math.max(0.15, S.z * k));
    S.tx = mx - (mx - S.tx) * (z2 / S.z);
    S.ty = my - (my - S.ty) * (z2 / S.z);
    S.z = z2;
    applyT();
  }, { passive: false });

  stage.addEventListener('click', e => {
    if (Date.now() - suppressClick < 350) return;
    if (e.target.closest('.nd') || e.target.closest('.net') || e.target.closest('.marquee')) return;
    if (S.selIds && S.selIds.length) { S.selIds = []; paintSel(); renderInspector(null); }
    $$('.net').forEach(x => x.classList.remove('sel'));
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
    const nd = e.target.closest('.nd');
    if (nd && S.lab.nodes[nd.dataset.id]) {
      const n = S.lab.nodes[nd.dataset.id];
      openNodeTerm(nd.dataset.id, n.name);
    }
  };

  // 右键菜单：网络 / 节点（单选或多选）/ 画布空白
  stage.addEventListener('contextmenu', e => {
    if (!S.lab) return;
    const netEl = e.target.closest('.net');
    const nd = e.target.closest('.nd');
    if (!netEl && !nd) {
      e.preventDefault();
      showCtx(e.clientX, e.clientY, [
        ['添加节点…', () => togglePalette(true)],
        ['添加网络（云）…', () => createCloud()],
        null,
        ['全选节点', () => {
          S.selIds = Object.keys(S.lab.nodes || {});
          paintSel(); syncInspector().catch(() => {});
        }],
        ['适应窗口', () => fit()],
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
        null,
        [`擦除选中的 ${ids.length} 个`, () => bulkAct(ids, 'wipe')],
        [`删除选中的 ${ids.length} 个`, () => bulkAct(ids, 'del')],
        null,
        ['取消选择', () => { S.selIds = []; paintSel(); renderInspector(null); }],
      ]);
      return;
    }
    const menu = [
      ['打开 CLI 控制台', () => openNodeTerm(id, n.name)],
      ['启动', () => act(id, 'start')],
      ['停止', () => act(id, 'stop')],
      null,
      ['属性（名字 / 内存 / 镜像）…', () => editNodeProps(n, null)],
      ['接线到网络…', () => linkDialog([id])],
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
