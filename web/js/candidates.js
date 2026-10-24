// 候选资源搜索页：搜索 → 候选卡片（存活徽章）→ 查看内容详情 → 整包/单集转存。
// 动态数据一律 textContent 渲染（common.js 注释的安全约定）。
const params = new URLSearchParams(location.search);
const vtype = params.get('vtype') || 'movie';
const vid = params.get('id') || '';

async function dosearch() {
  const list = document.getElementById('list');
  list.textContent = '';
  document.getElementById('meta').textContent = '搜索中…';
  const pending = [];
  const kw = document.getElementById('kw').value.trim();
  let data;
  try {
    data = await api('/api/candidates/search?vtype=' + encodeURIComponent(vtype)
                     + '&id=' + encodeURIComponent(vid)
                     + '&kw=' + encodeURIComponent(kw));
  } catch (e) {
    document.getElementById('meta').textContent = e.message;
    return;
  }
  document.getElementById('kw').value = data.keyword;
  const meta = document.getElementById('meta');
  meta.textContent = '《' + data.meta.title + '》'
    + (data.meta.year ? ' (' + data.meta.year + ')' : '')
    + ' · 关键词「' + data.keyword + '」 · 共 ' + data.candidates.length + ' 个候选';
  if (data.errors) showMsg('部分来源异常：' + data.errors, false);
  if (!data.candidates.length) {
    list.appendChild(el('div', 'meta',
      '未搜索到候选资源。可尝试修改上方关键词（如改用中文片名）后重新搜索。'));
    return;
  }
  for (const c of data.candidates) {
    const card = el('div', 'cand');
    card.appendChild(el('div', 't', c.title || '(无标题)'));
    const badges = el('div', 'badges');
    badges.appendChild(el('span', '', '匹配 ' + Math.round(c.score * 100) + '%'));
    if (c.source) badges.appendChild(el('span', '', c.source));
    if (c.published) badges.appendChild(el('span', '', c.published.slice(0, 10)));
    if (c.passcode) badges.appendChild(el('span', 'warn', '提取码 ' + c.passcode));
    const vbadge = el('span', 'vstate v-pending', '存活 检测中…');
    badges.appendChild(vbadge);
    card.appendChild(badges);
    const link = el('a', '', c.share_url);
    link.href = c.share_url; link.target = '_blank'; link.rel = 'noopener';
    card.appendChild(document.createElement('br'));
    card.appendChild(link);
    const actions = el('div', '');
    actions.style.marginTop = '10px';
    const btn = el('button', '', '确认转存到我的网盘');
    btn.onclick = () => confirmSave(btn, c);
    actions.appendChild(btn);
    const infoBtn = el('button', 'sec', '查看内容');
    infoBtn.style.marginLeft = '8px';
    infoBtn.onclick = () => toggleInspect(infoBtn, c, card);
    actions.appendChild(infoBtn);
    card.appendChild(actions);
    list.appendChild(card);
    pending.push({share_url: c.share_url, passcode: c.passcode,
                  badge: vbadge, card: card, infoBtn: infoBtn,
                  state: c.alive_state || '', summary: c.alive_summary || ''});
  }
  // 搜索阶段已带存活检测结果：直接渲染；缺失时兜底走 verify 接口逐个补检
  const toCheck = [];
  for (const it of pending) {
    if (it.state) setAlive(it, it.state, aliveText(it, it.state, it.summary));
    else toCheck.push(it);
  }
  verifyLinks(toCheck);
}

const V_TEXT = {ok: '有效', bad: '已失效', locked: '需提取码',
                unsupported: '暂不支持', uncertain: '不确定'};
function aliveText(it, state, summary) {
  let t = V_TEXT[state] || state;
  if (state === 'locked') t = it.passcode ? '提取码错误' : '需要提取码';
  if (summary) t += ' · ' + summary;
  return t;
}
function setAlive(it, state, text) {
  it.badge.className = 'vstate v-' + state;
  it.badge.textContent = '存活 ' + text;
  if (state === 'bad') {
    it.card.classList.add('dead');
    if (it.infoBtn) it.infoBtn.disabled = true;  // 失效分享无需解析内容
  }
}
async function verifyLinks(items) {
  if (!items.length) return;
  let results = [];
  try {
    const data = await api('/api/candidates/verify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items: items.map(i => ({
        share_url: i.share_url, passcode: i.passcode}))}),
    });
    results = data.results || [];
  } catch (e) {
    for (const it of items) setAlive(it, 'uncertain', '检测失败');
    return;
  }
  const byUrl = {};
  for (const r of results) byUrl[r.share_url] = r;
  for (const it of items) {
    const r = byUrl[it.share_url];
    if (!r) { setAlive(it, 'uncertain', '未检测'); continue; }
    setAlive(it, r.state, aliveText(it, r.state, r.summary || ''));
  }
}
async function confirmSave(btn, cand, mode, targetId) {
  btn.disabled = true;
  try {
    const data = await api('/api/candidates/confirm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        vtype: vtype, id: targetId || vid,
        share_url: cand.share_url, passcode: cand.passcode,
        title: cand.title, source: cand.source, mode: mode || 'episode',
      }),
    });
    showMsg('任务已创建' + (data.created ? '' : '（已有进行中的相同任务）') + '，正在跳转…', true);
    setTimeout(() => { location.href = '/task/' + data.task_id; }, 800);
  } catch (e) {
    showMsg('创建失败：' + e.message, false);
    btn.disabled = false;
  }
}

// ---- 分享内容详情（阶段 5）：文件规格 + 季集结构 + 整包/单集转存 ----
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
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({share_url: cand.share_url, passcode: cand.passcode}),
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
    chips.style.marginTop = '6px';
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
    target = {s: f.season, e: f.episode};
    if (part && f.episode != null) part.textContent = '仅转存所选剧集（' + epTag(f) + '）';
  };
  const buildRow = (f) => {
    const row = el('div', 'frow');
    row._ep = {s: f.season, e: f.episode};   // 默认选中匹配用
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
    more.style.padding = '6px 8px';
    const exp = el('button', 'sec', '展开全部');
    exp.style.marginLeft = '8px';
    exp.onclick = () => {
      for (const f of files.slice(20)) list.appendChild(buildRow(f));
      more.remove();
    };
    more.appendChild(exp);
    list.appendChild(more);
  }
  // 默认选中当前观看的集（裸集数合集按集数匹配；无匹配则第一个文件行）
  let def = null;
  if (curS != null && curE != null) {
    def = rows.find((r) => r._ep && r._ep.e === curE
          && (r._ep.s == null || r._ep.s === curS)) || null;
  }
  box.appendChild(list);
  const btns = el('div', '');
  btns.style.marginTop = '10px';
  if (vtype === 'series' && files.length) {
    const whole = el('button', '', '转存完整剧集');
    whole.onclick = () => confirmSave(whole, cand, 'whole', null);
    part = el('button', 'sec', '仅转存所选剧集');
    part.style.marginLeft = '8px';
    part.onclick = () => {
      if (!target) return showMsg('请先在文件列表中点选目标剧集', false);
      if (target.e == null) return showMsg('所选文件未标注集数，无法定位单集', false);
      const s = target.s != null ? target.s : (curS != null ? curS : 1);
      confirmSave(part, cand, 'episode', vid.split(':')[0] + ':' + s + ':' + target.e);
    };
    btns.appendChild(whole);
    btns.appendChild(part);
    btns.appendChild(el('span', 'meta', '（点选文件行可切换目标集）'));
  }
  box.appendChild(btns);
  (def || rows[0])?.onclick?.();  // 按钮就绪后再触发默认选中，顺带刷新按钮文字
  return box;
}
document.getElementById('rebtn').addEventListener('click', dosearch);
dosearch();
