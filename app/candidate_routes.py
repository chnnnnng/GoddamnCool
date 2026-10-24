"""候选资源搜索与转存任务网页（阶段 3）。

结构：HTML 页面为空壳，数据经 /api/* JSON 接口获取；所有 /api/* 接口
均要求 X-Admin-Token 头等于 ADMIN_TOKEN（需求第 10 条），token 由页面
提示输入并保存于 localStorage，不进 URL、不落日志。

安全要点：
- 第三方候选标题一律 textContent 渲染，防存储型 XSS；
- 确认转存时服务端校验分享链接格式（仅夸克官方分享域，防 SSRF/任意 URL）；
- 任务 ID 为 uuid4 hex，防猜测遍历；影片标题以服务端 TMDB 结果为准。
"""
import hmac
import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, db
from .tmdb import fetch_meta
from .providers.search import SHARE_URL_RE
from .candidate_service import CandidateService
from .stream_service import parse_video_id

router = APIRouter()
candidate_service = CandidateService()

_STATUS_TEXT = {
    "PENDING": "等待执行",
    "SUBMITTING": "正在提交转存",
    "PROCESSING": "转存处理中",
    "COMPLETED": "转存完成",
    "FAILED": "转存失败",
    "EXPIRED": "任务超时或失效",
}

_CSS = """
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto;
         padding: 0 16px; color: #222; }
  .card { border: 1px solid #e3e3e3; border-radius: 12px; padding: 20px 24px;
          box-shadow: 0 2px 8px rgba(0,0,0,.04); margin-bottom: 16px; }
  h1 { font-size: 20px; } code { background: #f4f4f4; padding: 2px 6px; border-radius: 4px; }
  .meta { color: #666; font-size: 13px; margin: 4px 0 12px; }
  .cand { border: 1px solid #eee; border-radius: 10px; padding: 14px 16px; margin: 10px 0; }
  .cand .t { font-weight: 600; margin-bottom: 6px; word-break: break-all; }
  .badges span { display: inline-block; background: #f1f0fb; color: #5b4bc4;
                 border-radius: 10px; padding: 1px 10px; font-size: 12px; margin: 2px 6px 2px 0; }
  .badges .warn { background: #fff3e0; color: #a05a00; }
  .vstate.v-ok { background: #e8f8ee; color: #177245; }
  .vstate.v-bad { background: #fdecec; color: #b3261e; }
  .vstate.v-locked { background: #fff3e0; color: #a05a00; }
  .vstate.v-unsupported, .vstate.v-uncertain, .vstate.v-pending { background: #f0f0f0; color: #888; }
  .cand.dead { opacity: .55; }
  button, a.button { background: #7b5cff; color: #fff; border: 0; border-radius: 8px;
                     padding: 8px 18px; font-size: 14px; cursor: pointer; margin-right: 8px;
                     display: inline-block; text-decoration: none; }
  button.sec, a.button.sec { background: #eee; color: #333; }
  button:disabled { opacity: .5; }
  a { color: #5b4bc4; word-break: break-all; }
  .msg { margin: 12px 0; padding: 10px 14px; border-radius: 8px; display: none; }
  .ok { background: #e8f8ee; color: #177245; display: block; }
  .err { background: #fdecec; color: #b3261e; display: block; }
  .status { display: inline-block; padding: 4px 14px; border-radius: 8px;
            font-weight: 600; font-size: 15px; }
  .s-PENDING, .s-SUBMITTING, .s-PROCESSING { background: #fff8e1; color: #9a6b00; }
  .s-COMPLETED { background: #e8f8ee; color: #177245; }
  .s-FAILED, .s-EXPIRED { background: #fdecec; color: #b3261e; }
  dl { display: grid; grid-template-columns: 96px 1fr; row-gap: 6px; font-size: 14px; }
  dt { color: #666; } dd { margin: 0; word-break: break-all; }
  .detail { margin-top: 12px; border-top: 1px dashed #ddd; padding-top: 10px; }
  .filelist { margin: 8px 0; max-height: 260px; overflow-y: auto;
              border: 1px solid #eee; border-radius: 8px; }
  .frow { display: flex; align-items: center; gap: 6px; padding: 6px 8px;
          font-size: 13px; border-bottom: 1px solid #f2f2f2; cursor: pointer; }
  .frow:last-child { border-bottom: none; }
  .frow:hover { background: #f7f7fb; }
  .frow.sel { background: #eef0ff; }
  .ep { flex: none; background: #ececf5; color: #4a4a6a; border-radius: 5px;
        padding: 1px 6px; font-size: 12px; font-family: monospace; }
  .spec { flex: none; background: #eef7ee; color: #256c3a; border-radius: 5px;
          padding: 1px 6px; font-size: 12px; }
  .fname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fsize { flex: none; color: #666; font-size: 12px; }
"""


def _page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body></html>"""
    # 管理页内嵌脚本随版本演进，禁止缓存（避免浏览器用旧 JS 调新 API）
    return HTMLResponse(doc, headers={"Cache-Control": "no-store"})


_CANDIDATES_BODY = """
<div class="card">
  <h1>候选资源搜索</h1>
  <div class="meta" id="meta">加载中…</div>
  <div style="display:flex;gap:8px;margin-bottom:8px">
    <input id="kw" placeholder="搜索关键词（跨语言影片可改用中文名，如：余罪）"
           style="flex:1;padding:8px 12px;border:1px solid #ccc;border-radius:8px;font-size:14px">
    <button class="sec" id="rebtn" onclick="dosearch()">重新搜索</button>
  </div>
  <div id="list"></div>
  <div id="msg" class="msg"></div>
</div>
<script>
const TOKEN_KEY = 'qk_admin_token';
const params = new URLSearchParams(location.search);
const vtype = params.get('vtype') || 'movie';
const vid = params.get('id') || '';

function getToken() {
  let t = localStorage.getItem(TOKEN_KEY);
  if (!t) {
    t = prompt('请输入访问令牌（服务器 .env 中的 ADMIN_TOKEN）') || '';
    t = t.trim();
    if (t) localStorage.setItem(TOKEN_KEY, t);
  }
  return t;
}
async function api(url, opts = {}) {
  opts.headers = Object.assign({'X-Admin-Token': getToken()}, opts.headers || {});
  const r = await fetch(url, opts);
  if (r.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    throw new Error('访问令牌无效，请刷新页面重新输入');
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function showMsg(text, ok) {
  const m = document.getElementById('msg');
  m.className = 'msg ' + (ok ? 'ok' : 'err');
  m.textContent = text;
}
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
dosearch();
</script>
"""

_TASK_BODY = """
<div class="card">
  <h1>转存任务</h1>
  <div id="body">加载中…</div>
  <div id="msg" class="msg"></div>
</div>
<script>
const TOKEN_KEY = 'qk_admin_token';
const taskId = location.pathname.split('/').pop();
const STATUS_TEXT = {PENDING: '等待执行', SUBMITTING: '正在提交转存',
                     PROCESSING: '转存处理中', COMPLETED: '转存完成',
                     FAILED: '转存失败', EXPIRED: '任务超时或失效'};
async function api(url) {
  const r = await fetch(url, {headers: {'X-Admin-Token': localStorage.getItem(TOKEN_KEY) || ''}});
  if (r.status === 401) throw new Error('访问令牌无效，请清除浏览器 localStorage 后重试');
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}
function row(dt, dd) {
  const d = document.createElement('dt'); d.textContent = dt;
  const v = document.createElement('dd'); v.textContent = dd;
  return [d, v];
}
function render(t) {
  const body = document.getElementById('body');
  body.textContent = '';
  const st = document.createElement('span');
  st.className = 'status s-' + t.status;
  st.textContent = STATUS_TEXT[t.status] || t.status;
  body.appendChild(st);
  const dl = document.createElement('dl'); dl.style.marginTop = '14px';
  let res = {};
  try { res = JSON.parse(t.result_json || '{}'); } catch (e) {}
  const rows = [
    row('影片', '《' + t.title + '》'
        + (t.season != null ? ' 第' + t.season + '季' : '')
        + (t.episode != null ? ' 第' + t.episode + '集' : '')),
    row('资源', t.candidate_title),
    row('分享链接', t.share_url),
    row('提取码', t.passcode || '无'),
    row('创建时间', t.created_at),
  ];
  if (t.status === 'COMPLETED' && (res.name || res.mode)) {
    if (res.mode === 'bulk')
      rows.push(row('转存方式', '整个分享（' + (res.share_entries || '?') + ' 项）'));
    else if (res.name)
      rows.push(row('转存文件', res.name + (res.fid ? '　(fid: ' + res.fid + ')' : '')));
    if (t.target_fid) rows.push(row('网盘 fid', t.target_fid));
  }
  if (t.error) rows.push(row('状态信息', t.error));
  for (const [a, b] of rows) { dl.appendChild(a); dl.appendChild(b); }
  body.appendChild(dl);

  if (t.status === 'COMPLETED') {
    const box = document.createElement('div'); box.style.marginTop = '16px';
    const ep = (t.video_type === 'series' && t.season != null && t.episode != null)
      ? '/' + t.season + '/' + t.episode : '';
    const play = document.createElement('a');
    play.className = 'button'; play.textContent = '在 Stremio 中播放';
    play.href = 'stremio://detail/' + t.video_type + '/' + t.imdb_id + ep;
    const web = document.createElement('a');
    web.className = 'button sec'; web.textContent = '网页版 Stremio';
    web.href = 'https://web.stremio.com/#/detail/' + t.video_type + '/'
      + t.imdb_id + ep; web.target = '_blank'; web.rel = 'noopener';
    const tip = document.createElement('span'); tip.className = 'meta';
    tip.textContent = ' 若客户端未响应，请在 Stremio 中刷新该片源重新获取播放流。';
    box.appendChild(play); box.appendChild(document.createTextNode(' '));
    box.appendChild(web); box.appendChild(tip);
    body.appendChild(box);
  }
  const note = document.createElement('div');
  note.className = 'meta';
  note.textContent = '任务由后台 Worker 自动执行（解析分享 → 选片 → 转存 → 定位文件），'
    + '完成后回到 Stremio 刷新片源即可播放。';
  body.appendChild(note);
}
async function tick() {
  try {
    const t = await api('/api/task/' + taskId);
    render(t);
    return ['PENDING', 'SUBMITTING', 'PROCESSING'].includes(t.status);
  } catch (e) {
    document.getElementById('msg').className = 'msg err';
    document.getElementById('msg').textContent = e.message;
    return false;
  }
}
(async () => {
  if (await tick()) setInterval(tick, 5000);
})();
</script>
"""


def _check_token(request: Request) -> bool:
    token = request.headers.get("X-Admin-Token", "")
    return bool(config.ADMIN_TOKEN) and hmac.compare_digest(token, config.ADMIN_TOKEN)


@router.get("/candidates", response_class=HTMLResponse)
async def candidates_page():
    return _page("候选资源搜索", _CANDIDATES_BODY)


@router.get("/api/candidates/search")
async def candidates_search(request: Request, vtype: str = "movie", id: str = "",
                            kw: str = ""):
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    if not id:
        return JSONResponse({"detail": "缺少影片 ID"}, status_code=400)
    meta, cands, errors, keyword = await candidate_service.search(vtype, id, keyword=kw)
    if meta is None:
        return JSONResponse({"detail": errors or "影片元数据获取失败"}, status_code=404)
    return {
        "meta": {"id": meta.id, "title": meta.title, "year": meta.year, "type": meta.type},
        "keyword": keyword,
        "candidates": [{
            "title": c.title, "share_url": c.share_url, "passcode": c.passcode,
            "source": c.source, "published": c.published,
            "provider": c.provider, "score": c.score,
            "alive_state": c.alive_state, "alive_summary": c.alive_summary,
        } for c in cands],
        "errors": errors,
    }


@router.post("/api/candidates/confirm")
async def candidates_confirm(request: Request):
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    vtype = body.get("vtype", "")
    video_id = body.get("id", "")
    share_url = (body.get("share_url") or "").strip()
    if vtype not in ("movie", "series") or not video_id:
        return JSONResponse({"detail": "vtype/id 参数无效"}, status_code=400)
    # 分享链接服务端强校验：仅接受夸克官方分享域（需求第 10 条）
    if not SHARE_URL_RE.match(share_url):
        return JSONResponse({"detail": "分享链接无效（仅支持 pan.quark.cn 分享链接）"},
                            status_code=400)
    imdb_id, season, episode = parse_video_id(video_id)
    meta = await fetch_meta(imdb_id, vtype, candidate_service.db_path)
    if meta is None or not meta.title:
        return JSONResponse({"detail": "影片元数据获取失败，请稍后重试"}, status_code=400)
    mode = body.get("mode") or "episode"
    if mode not in ("episode", "whole"):
        return JSONResponse({"detail": "mode 参数无效（episode/whole）"}, status_code=400)
    # whole（转存完整剧集）：任务不锚定季集，整包落盘；episode 仅转存所选集
    task, created = db.create_task(
        config.DB_PATH, vtype=vtype, imdb_id=imdb_id,
        season=None if mode == "whole" else season,
        episode=None if mode == "whole" else episode,
        title=meta.title,
        candidate_title=(body.get("title") or "")[:300],
        share_url=share_url,
        passcode=(body.get("passcode") or "")[:32],
        source=(body.get("source") or "")[:100],
        mode=mode,
    )
    return {"task_id": task["id"], "created": created, "status": task["status"]}


@router.post("/api/candidates/inspect")
async def candidates_inspect(request: Request):
    """解析分享链接内容清单与文件规格（大小/清晰度/音轨/字幕/季集结构）。

    实时查询夸克分享目录树并解析文件名规格，供候选详情展示与转存选择。"""
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    url = (body.get("share_url") or "").strip()
    if not SHARE_URL_RE.match(url):
        return JSONResponse({"detail": "分享链接无效（仅支持 pan.quark.cn 分享链接）"},
                            status_code=400)
    try:
        return await candidate_service.inspect_share(url, (body.get("passcode") or "")[:32])
    except Exception as e:
        return JSONResponse({"detail": f"分享解析失败: {e}"}, status_code=400)


@router.post("/api/candidates/verify")
async def candidates_verify(request: Request):
    """批量检测候选分享链接存活状态（代理 PanSou /api/check/links）。

    前端列出候选后逐个调用本接口标注存活状态；非法链接直接跳过不送检。"""
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON"}, status_code=400)
    raw = body.get("items")
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"detail": "items 不能为空"}, status_code=400)
    items = []
    for it in raw[:40]:  # 单次检测上限，防滥用
        if not isinstance(it, dict):
            continue
        url = (it.get("share_url") or "").strip()
        if not SHARE_URL_RE.match(url):
            continue  # 非法链接跳过（前端保持"未检测"）
        items.append({"share_url": url, "passcode": (it.get("passcode") or "")[:32]})
    if not items:
        return JSONResponse({"detail": "无有效的夸克分享链接"}, status_code=400)
    return {"results": await candidate_service.verify_links(items)}


@router.get("/task/{task_id}", response_class=HTMLResponse)
async def task_page(task_id: str):
    return _page("转存任务", _TASK_BODY)


@router.get("/api/task/{task_id}")
async def task_api(request: Request, task_id: str):
    if not _check_token(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    task = db.get_task(config.DB_PATH, task_id)
    if task is None:
        return JSONResponse({"detail": "任务不存在"}, status_code=404)
    task["status_text"] = _STATUS_TEXT.get(task["status"], task["status"])
    return task
