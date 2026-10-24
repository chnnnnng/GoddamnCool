/* 网盘资源搜索页（阶段 6/7）：独立搜索模式与 Stremio 锚定模式二合一。
 * 搜索结果单栏直显内容详情摘要（季集/规格/大小）；「查看内容」弹窗展示完整
 * 文件清单并提供三种转存方式：全部剧集 / 所选剧集（可多选）/ 当前剧集（仅锚定）。
 * 默认只显示检测"有效"的结果；TV 端方向键空间导航（spatial-nav.js）。 */
'use strict';
const params = new URLSearchParams(location.search);
const vtype = params.get('vtype') || '';
const vid = params.get('id') || '';
const anchored = !!(vtype && vid);
const vidParts = vid.split(':');
const curSeason = vidParts[1] ? Number(vidParts[1]) : null;
const curEpisode = vidParts[2] ? Number(vidParts[2]) : null;

const DISKS = ['quark', 'all', 'uc', 'baidu', 'aliyun', 'xunlei', '115', '123', 'tianyi'];
const DISK_NAMES = { quark: '夸克', all: '全部', uc: 'UC', baidu: '百度', aliyun: '阿里',
  xunlei: '迅雷', 115: '115', 123: '123', tianyi: '天翼' };
const ALIVE_FILTERS = [['ok', '有效'], ['locked', '需提取码'], ['bad', '已失效'],
  ['none', '未检测'], ['all', '全部']];
const V_TEXT = { ok: '有效', bad: '已失效', locked: '需提取码', unsupported: '不支持',
  uncertain: '不确定' };

let curDisk = 'quark';
let curAlive = 'ok';          // 默认仅展示检测"有效"的结果
let lastCands = [];
let lastMeta = null;
let lastKeyword = '';
let lastErrors = '';
let verifyTimer = 0;
let modalCand = null;         // 弹窗当前候选
let pickedFids = new Set();   // 弹窗内多选的文件 fid

const $ = (id) => document.getElementById(id);
const kwEl = $('kw'), btnEl = $('btn'), metaEl = $('meta'), listEl = $('list'),
  diskEl = $('diskchips'), aliveEl = $('alivechips'), msgEl = $('msg'),
  footEl = $('foot'), modalEl = $('modal'), mtitleEl = $('mtitle'),
  mbodyEl = $('mbody'), mcloseEl = $('mclose');

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function showMsg(text, ok) {
  msgEl.textContent = text || '';
  msgEl.className = 'msg ' + (ok === null ? 'null' : ok ? 'ok' : 'err');
}
function pad(n) { return String(n).padStart(2, '0'); }
function labelOf(c) {
  return `S${pad(c.season)}E${pad(c.episode)}`;
}

/* ---- 筛选 chips ---- */
function buildChips() {
  for (const d of DISKS) {
    const c = el('button', 'chip', DISK_NAMES[d]);
    c.type = 'button';
    c.dataset.disk = d;
    c.onclick = () => { curDisk = d; buildChips(); if (lastCands.length) renderResults(); };
    diskEl.appendChild(c);
  }
  buildAliveChips();
}
function buildAliveChips() {
  Array.from(aliveEl.querySelectorAll('.chip')).forEach((x) => x.remove());
  for (const [k, text] of ALIVE_FILTERS) {
    const c = el('button', 'chip', text);
    c.type = 'button';
    c.dataset.alive = k;
    c.onclick = () => { curAlive = k; buildAliveChips(); if (lastCands.length) renderResults(); };
    aliveEl.appendChild(c);
  }
  aliveEl.querySelectorAll('.chip').forEach((x) =>
    x.classList.toggle('on', x.dataset.alive === curAlive));
}

/* ---- 搜索 ---- */
async function dosearch() {
  const kw = kwEl.value.trim();
  if (!kw && !anchored) { kwEl.focus(); return; }  // 锚定模式空关键词由服务端 TMDB 双语名补齐
  btnEl.disabled = true;
  btnEl.textContent = '搜索中…';
  metaEl.textContent = '搜索中…';
  listEl.textContent = '';
  showMsg('', null);
  clearTimeout(verifyTimer);
  try {
    const q = new URLSearchParams({ kw });
    if (anchored) { q.set('vtype', vtype); q.set('id', vid); }
    if (curDisk !== 'quark') q.set('disks', curDisk);
    const data = await api(`/api/candidates/search?${q}`);
    lastCands = data.candidates || [];
    lastMeta = data.meta;
    lastKeyword = data.keyword || kw;
    if (!kw) kwEl.value = lastKeyword;  // 锚定模式自动回填实际使用的关键词
    lastErrors = (data.errors || '').trim();
    renderResults();
    verifyTimer = setTimeout(verifyLinks, 500);
  } catch (e) {
    showMsg('搜索失败：' + (e.message || e), false);
    metaEl.textContent = '';
  } finally {
    btnEl.disabled = false;
    btnEl.textContent = '搜索';
  }
}

async function verifyLinks() {
  const need = lastCands.filter((c) => c.disk === 'quark' && !c.alive_state);
  if (!need.length) return;
  try {
    const data = await api('/api/candidates/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: need.map((c) => ({
        share_url: c.share_url, passcode: c.passcode })) }),
    });
    const by = {};
    (data.results || []).forEach((r) => { by[r.share_url] = r; });
    for (const c of lastCands) {
      const r = by[c.share_url];
      if (r && r.state) { c.alive_state = r.state; c.alive_summary = r.summary || ''; }
    }
    if (lastCands.length) renderResults();
  } catch { /* 兜底失败保持未检测状态 */ }
}

/* ---- 渲染 ---- */
function metaText() {
  const head = lastMeta ? `${lastMeta.title}(${lastMeta.year || ''})` : lastKeyword;
  let t = `${head} · ${lastCands.length} 候选`;
  if (lastErrors) t += ` · 部分来源异常：${lastErrors}`;
  return t;
}

function renderResults() {
  metaEl.textContent = metaText();
  const shown = lastCands.filter((c) => {
    if (curAlive === 'all') return true;
    if (curAlive === 'none') return !c.alive_state;
    return c.alive_state === curAlive;
  });
  listEl.textContent = '';
  if (!shown.length) {
    listEl.appendChild(el('div', 'empty',
      lastCands.length ? '当前筛选条件下无结果，可切换上方"存活"筛选。' : '无候选。'));
    return;
  }
  for (const c of shown) listEl.appendChild(renderCard(c));
}

function renderCard(c) {
  const card = el('div', 'cand');
  if (c.alive_state === 'bad' || c.alive_state === 'unsupported') card.classList.add('dead');
  card.appendChild(el('div', 'ct', c.title || '(无标题)'));
  const badges = el('div', 'badges');
  badges.appendChild(el('span', 'bd', DISK_NAMES[c.disk] || c.disk));
  if (c.score) badges.appendChild(el('span', 'bd', `匹配 ${Math.round(c.score * 100)}%`));
  if (c.source) badges.appendChild(el('span', 'bd', c.source));
  if (c.published) badges.appendChild(el('span', 'bd', c.published.slice(0, 10)));
  if (c.passcode) badges.appendChild(el('span', 'bd code', `提取码 ${c.passcode}`));
  const v = el('span', 'vstate ' + (c.alive_state ? 'v-' + c.alive_state : 'v-none'),
    V_TEXT[c.alive_state] || '未检测');
  if (c.alive_summary) v.title = c.alive_summary;
  badges.appendChild(v);
  card.appendChild(badges);

  // 内容详情摘要直显（后端随搜索返回有效候选的解析结果）
  if (c.detail) appendDetailSummary(card, c.detail);

  if (c.share_url) {
    const a = el('a', 'link', c.share_url);
    a.href = c.share_url;
    a.target = '_blank';
    a.rel = 'noreferrer';
    card.appendChild(a);
  }
  const actions = el('div', 'actions');
  if (c.disk === 'quark') {
    const dead = c.alive_state === 'bad' || c.alive_state === 'unsupported';
    const save = el('button', 'save', '转存到我的网盘');
    save.disabled = dead;
    save.onclick = () => confirmSave(save, c, anchored ? 'episode' : 'whole');
    actions.appendChild(save);
    const info = el('button', 'sec', '查看内容');
    info.disabled = dead;
    info.onclick = () => openDetail(info, c);
    actions.appendChild(info);
  } else {
    actions.appendChild(el('span', 'hint', '非夸克资源：仅支持打开链接手动保存'));
  }
  card.appendChild(actions);
  return card;
}

function appendDetailSummary(card, d) {
  const sum = el('div', 'dsum',
    `视频 ${d.video_count} 个 · 共 ${d.total_size_h}` +
    (d.other_count ? ` · 其他文件 ${d.other_count} 个` : ''));
  card.appendChild(sum);
  if (d.seasons && d.seasons.length) {
    const seas = el('div', 'seas');
    for (const s of d.seasons) {
      seas.appendChild(el('span', 'seaschip',
        s.season ? `S${pad(s.season)} · ${s.count} 集 · ${s.size_h}` : `共 ${s.count} 集 · ${s.size_h}`));
    }
    card.appendChild(seas);
  }
}

/* ---- 查看内容弹窗 ---- */
async function openDetail(btn, c) {
  btn.disabled = true;
  btn.textContent = '解析中…';
  try {
    if (!c.detail) {
      const data = await api('/api/candidates/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ share_url: c.share_url, passcode: c.passcode }),
      });
      c.detail = data;
    }
    showModal(c);
  } catch (e) {
    showMsg('查看内容失败：' + (e.message || e), false);
  } finally {
    btn.disabled = false;
    btn.textContent = '查看内容';
  }
}

function showModal(c) {
  modalCand = c;
  pickedFids = new Set();
  mtitleEl.textContent = c.title || '(无标题)';
  mbodyEl.textContent = '';
  mbodyEl.appendChild(renderDetail(c, c.detail));
  modalEl.hidden = false;
  mcloseEl.focus();
}

function hideModal() {
  modalEl.hidden = true;
  modalCand = null;
  pickedFids.clear();
}

mcloseEl.onclick = hideModal;
modalEl.addEventListener('click', (e) => { if (e.target === modalEl) hideModal(); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !modalEl.hidden) hideModal();
});

function isSeriesLike(d) {
  return (anchored && vtype === 'series') || !!d.is_series;
}

function renderDetail(c, d) {
  const isSeries = isSeriesLike(d);
  const wrap = el('div');
  wrap.appendChild(el('div', 'dsum',
    `视频 ${d.video_count} 个 · 共 ${d.total_size_h}` +
    (d.other_count ? ` · 其他文件 ${d.other_count} 个` : '')));
  if (d.seasons && d.seasons.length) {
    const seas = el('div', 'seas');
    for (const s of d.seasons) {
      seas.appendChild(el('span', 'seaschip',
        s.season ? `S${pad(s.season)} · ${s.count} 集 · ${s.size_h}` : `共 ${s.count} 集 · ${s.size_h}`));
    }
    wrap.appendChild(seas);
  }

  const picksBtn = el('button', 'save', '转存所选剧集');
  picksBtn.type = 'button';
  picksBtn.disabled = true;

  const list = el('div', 'filelist');
  for (const f of (d.files || [])) {
    const row = el('div', 'frow');
    row.tabIndex = 0;
    row._fid = f.fid;
    row._ep = f.season != null || f.episode != null
      ? `${f.season != null ? 'S' + pad(f.season) : ''}${f.episode != null ? 'E' + pad(f.episode) : ''}`
      : '';
    const nm = el('div', 'nm', f.path || f.name);
    row.appendChild(nm);
    row.appendChild(el('div', 'fmeta',
      `${f.size_h}${f.spec && f.spec.length ? ' · ' + f.spec.join(' / ') : ''}${row._ep ? ' · ' + row._ep : ''}`));
    if (row._ep) row.appendChild(el('span', 'ep', row._ep));
    const toggle = () => {
      if (pickedFids.has(f.fid)) pickedFids.delete(f.fid);
      else pickedFids.add(f.fid);
      row.classList.toggle('sel', pickedFids.has(f.fid));
      picksBtn.disabled = pickedFids.size === 0;
      picksBtn.textContent = pickedFids.size
        ? `转存所选剧集（${pickedFids.size}）` : '转存所选剧集';
    };
    row.onclick = toggle;
    list.appendChild(row);
  }
  wrap.appendChild(list);
  wrap.appendChild(el('div', 'hint', '点选文件行可多选（再次点击取消）。'));

  const acts = el('div', 'actions');
  if (isSeries) {
    const whole = el('button', 'save', '转存全部剧集');
    whole.type = 'button';
    whole.onclick = () => confirmSave(whole, c, 'whole');
    acts.appendChild(whole);
    picksBtn.onclick = () =>
      confirmSave(picksBtn, c, 'picks', null, { fids: Array.from(pickedFids) });
    acts.appendChild(picksBtn);
    if (anchored && vtype === 'series') {
      const cur = el('button', 'save',
        `转存当前剧集（S${pad(curSeason || 0)}E${pad(curEpisode || 0)}）`);
      cur.type = 'button';
      cur.onclick = () => confirmSave(cur, c, 'episode');
      acts.appendChild(cur);
    }
  } else {
    const whole = el('button', 'save', '转存全部文件');
    whole.type = 'button';
    whole.onclick = () => confirmSave(whole, c, 'whole');
    acts.appendChild(whole);
  }
  wrap.appendChild(acts);
  return wrap;
}

/* ---- 转存 ---- */
async function confirmSave(btn, c, mode = 'episode', targetId = null, extra = {}) {
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = '创建任务…';
  try {
    const body = {
      share_url: c.share_url, passcode: c.passcode,
      title: c.title, source: c.source, mode,
    };
    if (anchored) { body.vtype = vtype; body.id = targetId || vid; }
    if (extra.season != null) { body.season = extra.season; body.episode = extra.episode; }
    if (extra.fids) body.fids = extra.fids;
    const t = await api('/api/candidates/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    showMsg('任务已创建，正在跳转…', true);
    setTimeout(() => { location.href = `/task/${t.task_id}`; }, 400);
  } catch (e) {
    showMsg('创建失败：' + (e.message || e), false);
    btn.disabled = false;
    btn.textContent = prev;
  }
}

/* ---- 合规声明（foot 常驻） ---- */
footEl.textContent = '本工具仅聚合第三方公开的网盘分享链接，不存储、不转码任何资源文件；'
  + '请仅转存您拥有合法权利的内容，并遵守当地法律法规与网盘服务条款。'
  + '搜索结果与存活状态由第三方服务提供，有效性不作保证。'
  + '仅夸克资源支持自动转存，其他网盘请打开链接手动保存。'
  + 'TV 端可用遥控器方向键移动焦点、OK 键确认。';

/* ---- 初始化 ---- */
buildChips();
btnEl.onclick = dosearch;
kwEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') dosearch(); });
if (anchored) dosearch();
else kwEl.focus();
