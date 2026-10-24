// 网盘资源搜索页：独立搜索模式（搜索框 + 网盘筛选 + 结果筛选）与
// Stremio 锚定模式（URL 带 vtype&id，自动搜 TMDB 双语名）二合一。
// 动态数据一律 textContent 渲染（common.js 注释的安全约定）。
const params = new URLSearchParams(location.search);
const vtype = params.get('vtype') || '';
const vid = params.get('id') || '';
const anchored = !!(vtype && vid);  // Stremio 锚定模式（外部搜索跳转）

const DISKS = [
  ['quark', '夸克', ''], ['all', '全部', 'all'], ['uc', 'UC', 'uc'],
  ['baidu', '百度', 'baidu'], ['aliyun', '阿里', 'aliyun'], ['xunlei', '迅雷', 'xunlei'],
  ['115', '115', '115'], ['123', '123', '123'], ['tianyi', '天翼', 'tianyi'],
];
const DISK_NAMES = { quark: '夸克', uc: 'UC网盘', baidu: '百度网盘', aliyun: '阿里云盘',
                     xunlei: '迅雷云盘', 115: '115网盘', 123: '123网盘', tianyi: '天翼云盘' };
const ALIVE_FILTERS = [['all', '全部'], ['ok', '有效'], ['locked', '需提取码'],
                       ['bad', '已失效'], ['none', '未检测']];
const V_TEXT = { ok: '有效', bad: '已失效', locked: '需提取码',
                 unsupported: '暂不支持', uncertain: '不确定' };

let curDisk = 'quark';      // 当前网盘筛选（服务端重搜）
let curAlive = 'all';       // 当前存活筛选（客户端过滤）
let lastCands = [];         // 最近一次搜索结果（存活筛选用）
let lastMeta = null;        // 最近一次影片元数据（锚定模式）
let lastKeyword = '';

const kwEl = document.getElementById('kw');
const metaEl = document.getElementById('meta');
const listEl = document.getElementById('list');

// ---- 筛选 chips ----
function buildChips(boxId, items, cur, onpick) {
  const box = document.getElementById(boxId);
  box.textContent = '';
  for (const [val, label] of items) {
    const b = el('button', 'chip' + (val === cur ? ' on' : ''), label);
    b.onclick = () => onpick(val);
    box.appendChild(b);
  }
}
function renderDiskChips() { buildChips('diskchips', DISKS, curDisk, (v) => {
  if (curDisk === v) return;
  curDisk = v;
  renderDiskChips();
  if (kwEl.value.trim() || anchored) dosearch();  // 切网盘 → 服务端重搜
}); }
function renderAliveChips() { buildChips('alivechips', ALIVE_FILTERS, curAlive, (v) => {
  curAlive = v;
  renderAliveChips();
  renderResults();
}); }

// ---- 搜索 ----
async function dosearch() {
  const kw = kwEl.value.trim();
  if (!anchored && !kw) { showMsg('请输入搜索关键词', false); return; }
  listEl.textContent = '';
  lastCands = [];
  metaEl.textContent = '搜索中…';
  showMsg('', null);
  const qs = new URLSearchParams();
  if (anchored) { qs.set('vtype', vtype); qs.set('id', vid); }
  if (kw) qs.set('kw', kw);
  qs.set('disks', (DISKS.find((d) => d[0] === curDisk) || DISKS[0])[2]);
  let data;
  try {
    data = await api('/api/candidates/search?' + qs.toString());
  } catch (e) {
    metaEl.textContent = e.message;
    return;
  }
  kwEl.value = data.keyword || kw;
  lastMeta = data.meta;
  lastKeyword = data.keyword || kw;
  lastCands = data.candidates || [];
  if (data.errors) showMsg('部分来源异常：' + data.errors, false);
  renderResults();
  // 服务端搜索已带存活检测；万一缺失则兜底补检
  verifyLinks(lastCands.filter((c) => c.disk === 'quark' && !c.alive_state));
}

function metaText() {
  const n = visibleCands().length;
  const total = lastCands.length;
  let head = '';
  if (anchored && lastMeta) {
    head = '《' + lastMeta.title + '》' + (lastMeta.year ? ' (' + lastMeta.year + ')' : '') + ' · ';
  }
  return head + (lastKeyword ? '关键词「' + lastKeyword + '」 · ' : '')
    + '共 ' + total + ' 个候选' + (n !== total ? '，筛选后显示 ' + n + ' 个' : '');
}

function visibleCands() {
  if (curAlive === 'all') return lastCands;
  if (curAlive === 'none') return lastCands.filter((c) => !c.alive_state);
  return lastCands.filter((c) => c.alive_state === curAlive);
}

function renderResults() {
  listEl.textContent = '';
  const cands = visibleCands();
  metaEl.textContent = lastCands.length ? metaText()
    : (anchored ? '加载中…' : '输入关键词开始搜索网盘资源');
  if (!lastCands.length) {
    if (anchored) listEl.appendChild(el('div', 'meta',
      '未搜索到候选资源。可尝试修改上方关键词（如改用中文片名）后重新搜索。'));
    return;
  }
  if (!cands.length) {
    listEl.appendChild(el('div', 'meta', '当前筛选条件下无结果，可切换上方「存活」筛选。'));
    return;
  }
  for (const c of cands) listEl.appendChild(renderCard(c));
}

function renderCard(c) {
  const card = el('div', 'cand');
  card.appendChild(el('div', 't', c.title || '(无标题)'));
  const badges = el('div', 'badges');
  badges.appendChild(el('span', c.disk === 'quark' ? 'diskq' : '',
    DISK_NAMES[c.disk] || c.disk));
  badges.appendChild(el('span', '', '匹配 ' + Math.round((c.score || 0) * 100) + '%'));
  if (c.source) badges.appendChild(el('span', '', c.source));
  if (c.published) badges.appendChild(el('span', '', c.published.slice(0, 10)));
  if (c.passcode) badges.appendChild(el('span', 'warn', '提取码 ' + c.passcode));
  const vbadge = el('span', 'vstate v-pending', '存活 …');
  badges.appendChild(vbadge);
  card.appendChild(badges);
  const link = el('a', 'link', c.share_url);
  link.href = c.share_url; link.target = '_blank'; link.rel = 'noopener';
  card.appendChild(link);
  const actions = el('div', 'actions');
  let infoBtn = null;
  if (c.disk === 'quark') {
    const btn = el('button', '', '转存到我的网盘');
    btn.onclick = () => confirmSave(btn, c);
    actions.appendChild(btn);
    infoBtn = el('button', 'sec', '查看内容');
    infoBtn.onclick = () => toggleInspect(infoBtn, c, card);
    actions.appendChild(infoBtn);
  } else {
    actions.appendChild(el('span', 'hint', '非夸克资源：仅支持打开链接手动保存'));
  }
  card.appendChild(actions);
  setAlive(c, c.alive_state || '', aliveText(c, c.alive_state || '', c.alive_summary || ''), vbadge, card, infoBtn);
  card._cand = c;
  return card;
}

// ---- 存活徽章 ----
function aliveText(c, state, summary) {
  let t = V_TEXT[state] || state || '未检测';
  if (state === 'locked') t = c.passcode ? '提取码错误' : '需要提取码';
  if (summary && state) t += ' · ' + summary;
  return t;
}
function setAlive(c, state, text, vbadge, card, infoBtn) {
  c.alive_state = state;
  vbadge.className = 'vstate v-' + (state || 'none');
  vbadge.textContent = '存活 ' + text;
  if (state === 'bad') {
    card.classList.add('dead');
    if (infoBtn) infoBtn.disabled = true;  // 失效分享无需解析内容
  }
}
async function verifyLinks(cands) {
  if (!cands.length) return;
  const cardOf = {};
  for (const card of listEl.querySelectorAll('.cand')) cardOf[card._cand.share_url] = card;
  let results = [];
  try {
    const data = await api('/api/candidates/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: cands.map((c) => ({
        share_url: c.share_url, passcode: c.passcode })) }),
    });
    results = data.results || [];
  } catch (e) {
    for (const c of cands) applyAlive(c, 'uncertain', '检测失败');
    return;
  }
  const byUrl = {};
  for (const r of results) byUrl[r.share_url] = r;
  for (const c of cands) {
    const r = byUrl[c.share_url];
    if (!r) { applyAlive(c, 'uncertain', '未检测'); continue; }
    applyAlive(c, r.state, r.summary || '');
  }
  function applyAlive(c, state, summary) {
    c.alive_summary = summary;
    const card = cardOf[c.share_url];
    if (!card) return;  // 可能已被筛选隐藏
    const vbadge = card.querySelector('.vstate');
    const infoBtn = card.querySelector('.actions button.sec');
    setAlive(c, state, aliveText(c, state, summary), vbadge, card, infoBtn);
  }
}

// ---- 转存（锚定模式带 id；独立模式 share-only 任务） ----
async function confirmSave(btn, cand, mode = 'episode', targetId = null, extra = {}) {
  btn.disabled = true;
  try {
    const data = await api('/api/candidates/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({
        vtype: anchored ? vtype : '',
        id: targetId || (anchored ? vid : ''),
        share_url: cand.share_url, passcode: cand.passcode,
        title: cand.title, source: cand.source, mode: mode,
      }, extra)),
    });
    showMsg('任务已创建' + (data.created ? '' : '（已有进行中的相同任务）') + '，正在跳转…', true);
    setTimeout(() => { location.href = '/task/' + data.task_id; }, 800);
  } catch (e) {
    showMsg('创建失败：' + e.message, false);
    btn.disabled = false;
  }
}

// ---- 分享内容详情：文件规格 + 季集结构 + 整包/单集转存 ----
const curPart = vid.split(':');
const curS = curPart[1] ? +curPart[1] : null;
const curE = curPart[2] ? +curPart[2] : null;

function toggleInspect(btn, cand, card) {
  if (btn.dataset.open === '1') {
    const d = card.querySelector('.detail');
    if (d) d.remove();
    btn.dataset.open = '';
    btn.textContent = '查看内容';
    return;
  }
  inspectShare(btn, cand, card);
}

async function inspectShare(btn, cand, card) {
  btn.disabled = true;
  btn.textContent = '解析中…';
  let data;
  try {
    data = await api('/api/candidates/inspect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ share_url: cand.share_url, passcode: cand.passcode }),
    });
  } catch (e) {
    showMsg('解析失败：' + e.message, false);
    btn.disabled = false;
    btn.textContent = '查看内容';
    return;
  }
  btn.disabled = false;
  btn.dataset.open = '1';
  btn.textContent = '收起内容';
  card.appendChild(renderDetail(cand, data));
}

function specBadges(f) {
  const sp = f.spec || {};
  return [sp.resolution, sp.source, sp.codec]
    .concat(sp.audio || [], sp.subtitle || [], sp.languages || [])
    .filter(Boolean);
}

function renderDetail(cand, data) {
  const box = el('div', 'detail');
  const head = el('div', 'meta');
  head.textContent = '视频 ' + data.video_count + ' 个 · 共 ' + data.total_size_h
    + (data.other_count ? ' · 其他文件 ' + data.other_count + ' 个' : '');
  box.appendChild(head);
  if (data.seasons && data.seasons.length) {
    const chips = el('div', 'badges');
    for (const s of data.seasons) {
      chips.appendChild(el('span', '', 'S' + String(s.season).padStart(2, '0')
        + ' · ' + s.count + ' 集 · ' + s.size_h));
    }
    box.appendChild(chips);
  }
  // 文件列表：点选行作为"仅转存所选剧集"的目标集
  let target = null;
  let part = null;   // 按钮在文件行之后创建，点选行时刷新其文字
  const rows = [];
  const list = el('div', 'filelist');
  const files = data.files || [];
  const epTag = (f) => (f.season != null ? 'S' + String(f.season).padStart(2, '0') : '')
    + (f.episode != null ? 'E' + String(f.episode).padStart(2, '0') : '');
  const setTarget = (row, f) => {
    for (const r of rows) r.classList.remove('sel');
    row.classList.add('sel');
    target = { s: f.season, e: f.episode };
    if (part && f.episode != null) part.textContent = '仅转存所选剧集（' + epTag(f) + '）';
  };
  const buildRow = (f) => {
    const row = el('div', 'frow');
    row.tabIndex = 0;                        // TV 遥控器可聚焦，Enter/空格点选
    row._ep = { s: f.season, e: f.episode }; // 默认选中匹配用
    if (f.season != null || f.episode != null) {
      row.appendChild(el('span', 'ep', epTag(f)));
    }
    const nm = el('span', 'fname', f.path || f.name);
    nm.title = f.path || f.name;
    row.appendChild(nm);
    for (const b of specBadges(f)) row.appendChild(el('span', 'spec', b));
    row.appendChild(el('span', 'fsize', f.size_h));
    row.onclick = () => setTarget(row, f);
    rows.push(row);
    return row;
  };
  for (const f of files.slice(0, 20)) list.appendChild(buildRow(f));
  if (files.length > 20) {
    const more = el('div', 'meta', '… 其余 ' + (files.length - 20) + ' 个文件');
    more.style.padding = '6px 10px';
    const exp = el('button', 'sec', '展开全部');
    exp.onclick = () => {
      for (const f of files.slice(20)) list.appendChild(buildRow(f));
      more.remove();
    };
    more.appendChild(exp);
    list.appendChild(more);
  }
  box.appendChild(list);
  const btns = el('div', 'actions');
  const isSeriesLike = (anchored && vtype === 'series') || data.is_series;
  if (isSeriesLike && files.length) {
    const whole = el('button', '', '转存完整剧集');
    whole.onclick = () => confirmSave(whole, cand, 'whole', null);
    part = el('button', 'sec', '仅转存所选剧集');
    part.onclick = () => {
      if (!target) return showMsg('请先在文件列表中点选目标剧集', false);
      if (target.e == null) return showMsg('所选文件未标注集数，无法定位单集', false);
      if (anchored) {
        const s = target.s != null ? target.s : (curS != null ? curS : 1);
        confirmSave(part, cand, 'episode', vid.split(':')[0] + ':' + s + ':' + target.e);
      } else {  // 独立模式：share-only 任务，季集由 body 提供
        confirmSave(part, cand, 'episode', null,
                    { season: target.s != null ? target.s : 1, episode: target.e });
      }
    };
    btns.appendChild(whole);
    btns.appendChild(part);
    btns.appendChild(el('span', 'hint', '（点选文件行可切换目标集）'));
  }
  box.appendChild(btns);
  // 默认选中当前观看的集（锚定模式）；独立模式默认第一行
  let def = null;
  if (curS != null && curE != null) {
    def = rows.find((r) => r._ep && r._ep.e === curE
          && (r._ep.s == null || r._ep.s === curS)) || null;
  }
  (def || rows[0])?.onclick?.();  // 按钮就绪后再触发默认选中，顺带刷新按钮文字
  return box;
}

// ---- 初始化 ----
document.getElementById('btn').addEventListener('click', dosearch);
kwEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') dosearch(); });
renderDiskChips();
renderAliveChips();
document.getElementById('foot').textContent =
  'TV 端可用遥控器方向键移动焦点、OK 键确认；仅夸克资源支持自动转存与内容解析。';
if (anchored) {
  dosearch();
} else {
  metaEl.textContent = '输入关键词开始搜索网盘资源';
  kwEl.focus();
}
