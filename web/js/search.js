/* 网盘资源搜索页（阶段 6/7）：独立搜索模式与 Stremio 锚定模式二合一。
 * 搜索结果单栏直显内容详情摘要（灰字统计 + 季集/规格 badge 行），卡片按钮按
 * 内容自动切换 全部剧集/当前剧集；「查看内容」弹窗提供独立【转存全部剧集】
 * 按钮（多视频时）与动态主按钮（点选文件行 → 转存所选（N），未选 → 当前剧集）。
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

/* ---- 规格格式化（后端 parse_file_spec 返回对象：resolution/source/codec/audio/subtitle/languages） ---- */
function formatSpec(s) {
  if (!s || typeof s !== 'object') return '';
  const parts = [];
  if (s.resolution) parts.push(s.resolution);
  if (s.source) parts.push(s.source);
  if (s.codec) parts.push(s.codec);
  if (s.audio && s.audio.length) parts.push(s.audio.join('/'));
  if (s.subtitle && s.subtitle.length) parts.push('字幕' + s.subtitle.join('/'));
  if (s.languages && s.languages.length) parts.push(s.languages.join('/'));
  return parts.join(' · ');
}
function aggregateSpec(d) {
  /* 多个文件的规格各维度去重合并，返回 badge 数组（如 ["4K","WEB-DL","H.265","AAC"]） */
  const dims = { resolution: new Set(), source: new Set(), codec: new Set(),
    audio: new Set(), subtitle: new Set() };
  let any = false;
  for (const f of (d.files || [])) {
    const s = f.spec;
    if (!s || typeof s !== 'object') continue;
    any = true;
    for (const k of Object.keys(dims)) {
      if (k === 'audio' || k === 'subtitle') (s[k] || []).forEach((v) => dims[k].add(v));
      else if (s[k]) dims[k].add(s[k]);
    }
  }
  if (!any) return [];
  const parts = [];
  if (dims.resolution.size) parts.push([...dims.resolution].join('/'));
  if (dims.source.size) parts.push([...dims.source].join('/'));
  if (dims.codec.size) parts.push([...dims.codec].join('/'));
  if (dims.audio.size) parts.push([...dims.audio].join('/'));
  if (dims.subtitle.size) parts.push('字幕' + [...dims.subtitle].join('/'));
  return parts;
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
  listEl.textContent = '';
  showLoading('搜索中…');  // 顶部气泡 + spinner 动画
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
    hideLoading();
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
    if (!dead) {
      /* 卡片按钮规则（用户需求）：多个视频 → 转存全部剧集；
         只有 1 个视频或锚定了 Stremio 剧集 → 转存当前剧集。 */
      const vc = c.detail ? c.detail.video_count : null;
      const btns = [];
      if (anchored && vtype === 'series') {
        btns.push([`转存当前剧集（S${pad(curSeason || 0)}E${pad(curEpisode || 0)}）`, 'episode']);
        if (vc == null || vc > 1) btns.push(['转存全部剧集', 'whole']);
      } else if (anchored) {
        btns.push(['转存当前影片', 'episode']);
        if (vc == null || vc > 1) btns.push(['转存全部文件', 'whole']);
      } else if (vc === 1) {
        btns.push(['转存当前剧集', 'whole']);   // 单视频分享：整包即该视频
      } else if (vc > 1) {
        btns.push(['转存全部剧集', 'whole']);
      }  // vc == null（内容未解析）：不放转存按钮，引导点「查看内容」
      for (const [text, mode] of btns) {
        const b = el('button', 'save', text);
        b.type = 'button';
        b.onclick = () => confirmSave(b, c, mode);
        actions.appendChild(b);
      }
    }
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
  /* 季集与影片规格 badges 同行显示 */
  const chips = el('div', 'seas');
  for (const s of (d.seasons || [])) {
    chips.appendChild(el('span', 'seaschip',
      s.season ? `S${pad(s.season)} · ${s.count} 集 · ${s.size_h}` : `共 ${s.count} 集 · ${s.size_h}`));
  }
  for (const t of aggregateSpec(d)) chips.appendChild(el('span', 'seaschip', t));
  if (chips.children.length) card.appendChild(chips);
  const sum = el('div', 'dsum',
    `视频 ${d.video_count} 个 · 共 ${d.total_size_h}` +
    (d.other_count ? ` · 其他文件 ${d.other_count} 个` : ''));
  card.appendChild(sum);
}

/* ---- 查看内容弹窗 ---- */
async function openDetail(btn, c) {
  btn.disabled = true;
  btn.textContent = '解析中…';
  // 先弹窗 + 顶部加载气泡（分享解析可能较慢），完成后填充内容
  modalCand = c;
  pickedFids = new Set();
  mtitleEl.textContent = c.title || '(无标题)';
  mbodyEl.textContent = '';
  mbodyEl.appendChild(el('div', 'hint', '加载中…'));
  modalEl.hidden = false;
  mcloseEl.focus();
  showLoading('解析分享中…');
  try {
    if (!c.detail) {
      const data = await api('/api/candidates/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ share_url: c.share_url, passcode: c.passcode }),
      });
      c.detail = data;
    }
    mbodyEl.textContent = '';
    mbodyEl.appendChild(renderDetail(c, c.detail));
  } catch (e) {
    showMsg('查看内容失败：' + (e.message || e), false);
    hideModal();
  } finally {
    hideLoading();
    btn.disabled = false;
    btn.textContent = '查看内容';
  }
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
  const files = d.files || [];
  const wrap = el('div');
  /* 季集与影片规格 badges 同行显示 */
  const chips = el('div', 'seas');
  for (const s of (d.seasons || [])) {
    chips.appendChild(el('span', 'seaschip',
      s.season ? `S${pad(s.season)} · ${s.count} 集 · ${s.size_h}` : `共 ${s.count} 集 · ${s.size_h}`));
  }
  for (const t of aggregateSpec(d)) chips.appendChild(el('span', 'seaschip', t));
  if (chips.children.length) wrap.appendChild(chips);
  wrap.appendChild(el('div', 'dsum',
  `视频 ${d.video_count} 个 · 共 ${d.total_size_h}` +
  (d.other_count ? ` · 其他文件 ${d.other_count} 个` : '')));

  /* 按钮：
     - 独立【转存全部剧集】按钮：仅多视频时显示，无需手动全选；
     - 动态主按钮：点选部分 → 转存所选（N）；未选 → 锚定时当前剧集，
       独立单视频时当前剧集（整包即该视频），独立多视频时隐藏（全部按钮已提供）。 */
  const wholeBtn = el('button', 'save', isSeries ? '转存全部剧集' : '转存全部文件');
  wholeBtn.type = 'button';
  wholeBtn.onclick = () => confirmSave(wholeBtn, c, 'whole');

  const mainBtn = el('button', 'save');
  mainBtn.type = 'button';
  let mainMode = 'whole';
  const updateMainBtn = () => {
    if (!files.length) { mainBtn.hidden = true; return; }
    if (pickedFids.size) {
      mainMode = 'picks';
      mainBtn.hidden = false;
      mainBtn.textContent = `转存所选${isSeries ? '剧集' : '文件'}（${pickedFids.size}）`;
    } else if (anchored) {
      mainMode = 'episode';
      mainBtn.hidden = false;
      mainBtn.textContent = vtype === 'series'
        ? `转存当前剧集（S${pad(curSeason || 0)}E${pad(curEpisode || 0)}）` : '转存当前影片';
    } else if (files.length > 1) {
      mainBtn.hidden = true;   // 独立多视频：「转存全部剧集」按钮已覆盖
    } else {
      mainMode = 'whole';
      mainBtn.hidden = false;
      mainBtn.textContent = isSeries ? '转存当前剧集' : '转存当前影片';
    }
  };
  mainBtn.onclick = () => confirmSave(mainBtn, c, mainMode, null,
    mainMode === 'picks' ? { fids: Array.from(pickedFids) } : {});

  const list = el('div', 'filelist');
  for (const f of files) {
    const row = el('div', 'frow');
    row.tabIndex = 0;
    row._fid = f.fid;
    row._ep = f.season != null || f.episode != null
      ? `${f.season != null ? 'S' + pad(f.season) : ''}${f.episode != null ? 'E' + pad(f.episode) : ''}`
      : '';
    const nm = el('div', 'nm', f.path || f.name);
    row.appendChild(nm);
    const sp = formatSpec(f.spec);
    row.appendChild(el('div', 'fmeta',
      `${f.size_h}${sp ? ' · ' + sp : ''}${row._ep ? ' · ' + row._ep : ''}`));
    if (row._ep) row.appendChild(el('span', 'ep', row._ep));
    if (f.warn) {  // 文件名疑似不符合规范（无标题等），与任务页同一规则
      const w = el('span', 'badge warn', '⚠ 文件名不规范');
      w.title = f.warn;
      row.appendChild(w);
    }
    const toggle = () => {
      if (pickedFids.has(f.fid)) pickedFids.delete(f.fid);
      else pickedFids.add(f.fid);
      row.classList.toggle('sel', pickedFids.has(f.fid));
      updateMainBtn();
    };
    row.onclick = toggle;
    list.appendChild(row);
  }
  wrap.appendChild(list);
  wrap.appendChild(el('div', 'hint',
    '点选文件行可仅转存所选剧集（再次点击取消）。'));

  const acts = el('div', 'actions');
  updateMainBtn();
  acts.appendChild(mainBtn);
  if (files.length > 1) acts.appendChild(wholeBtn);
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

/* ---- 顶部导航 ---- */
var logoutBtn = document.getElementById('btn-logout');
if (logoutBtn) logoutBtn.addEventListener('click', function () { logout(); });
kwEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') dosearch(); });
if (anchored) dosearch();
else kwEl.focus();
