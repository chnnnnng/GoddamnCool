// 转存任务页：读取路径中的任务 ID，轮询 /api/task/{id} 渲染六态状态。
// 统一展示转存结果，不再区分是否关联 Stremio 影片。
const taskId = location.pathname.split('/').pop();
const STATUS_TEXT = {PENDING: '等待执行', SUBMITTING: '正在提交转存',
                     PROCESSING: '转存处理中', COMPLETED: '转存完成',
                     FAILED: '转存失败', EXPIRED: '任务超时或失效'};

function row(dt, dd) {
  const d = document.createElement('dt'); d.textContent = dt;
  const v = document.createElement('dd'); v.textContent = dd;
  return [d, v];
}
function badge(text, cls) {
  const b = document.createElement('span');
  b.className = 'badge' + (cls ? ' ' + cls : '');
  b.textContent = text;
  return b;
}
function humanSize(n) {
  if (n == null || isNaN(n)) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(2)) + ' ' + units[i];
}
// COMPLETED 任务的落盘明细表：文件名（原名 → 规范名）/ 大小 / 资源规格
function fileTable(files) {
  const total = files.reduce((s, f) => s + (Number(f.size) || 0), 0);
  const box = document.createElement('div');
  box.style.marginTop = '14px';
  const cap = document.createElement('div');
  cap.style.cssText = 'font-weight:600;margin-bottom:6px';
  cap.textContent = '文件明细（' + files.length + ' 个'
    + (total > 0 ? '，共 ' + humanSize(total) : '') + '）';
  box.appendChild(cap);
  const table = document.createElement('table');
  const trh = document.createElement('tr');
  for (const label of ['文件名', '大小', '资源规格']) {
    const th = document.createElement('th'); th.textContent = label;
    trh.appendChild(th);
  }
  table.appendChild(trh);
  for (const f of files) {
    const tr = document.createElement('tr');
    const tdName = document.createElement('td');
    tdName.className = 'wrap';
    /* 【SxxEyy】badge 最前 → 原名称 → 规范名 → 智能解析/已在网盘 badge
       （需求第 10 条，与搜索页弹窗条目同一布局；不再显示"文件名不规范"） */
    if (f.season != null || f.episode != null) {
      tdName.appendChild(badge(
        (f.season != null ? 'S' + String(f.season).padStart(2, '0') : '')
        + (f.episode != null ? 'E' + String(f.episode).padStart(2, '0') : ''),
        'ep'));
      tdName.appendChild(document.createTextNode(' '));
    }
    tdName.appendChild(document.createTextNode(f.name));
    if (f.exists) {  // 转存前按 精确大小+粗略片名 匹配到网盘已有文件
      tdName.appendChild(document.createTextNode(' '));
      const tick = badge('✓ 已在网盘', 'tick');
      tick.title = '转存前按 精确文件大小+粗略片名 匹配到网盘已有文件，本次转存仍会执行';
      tdName.appendChild(tick);
    }
    if (f.renamed && f.final) {  // 已智能重命名为规范名（第二行显示原名对照）
      const cn = document.createElement('span');
      cn.className = 'cname';
      cn.textContent = '原：' + f.name + ' → ' + f.final;
      cn.title = '转存时已智能重命名为规范名（媒体库可直接识别）';
      tdName.appendChild(cn);
      tdName.appendChild(document.createTextNode(' '));
      const b = badge(f.canonical_source === 'ai' ? 'AI智能解析' : '智能解析',
        f.canonical_source === 'ai' ? 'tick' : '');
      b.title = f.canonical_source === 'ai'
        ? '转存时结合 AI 解析出的规范文件名'
        : '转存时已智能重命名为规范文件名';
      tdName.appendChild(b);
    } else if (f.canonical && f.canonical !== f.name) {  // 规范名建议（第二行）
      const cn = document.createElement('span');
      cn.className = 'cname';
      cn.textContent = '→ ' + f.canonical;
      cn.title = '规范文件名（按此命名可被媒体库正确识别）';
      tdName.appendChild(cn);
      tdName.appendChild(document.createTextNode(' '));
      const b = badge('智能解析');
      b.title = '规则解析出的规范文件名';
      tdName.appendChild(b);
    }
    const tdSize = document.createElement('td');
    tdSize.textContent = f.size > 0 ? (f.size_h || humanSize(f.size)) : '—';
    const tdSpec = document.createElement('td');
    tdSpec.className = 'wrap';
    (f.spec || []).forEach(s => tdSpec.appendChild(badge(s)));
    tr.append(tdName, tdSize, tdSpec);
    table.appendChild(tr);
  }
  box.appendChild(table);
  return box;
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
    else if (res.mode === 'picks')
      rows.push(row('转存方式', '所选文件（' + ((res.names || []).length || '?') + ' 项）'));
    else if (res.name)
      rows.push(row('转存方式', '单文件'));
    if (res.renamed || t.renamed_count)  // 智能重命名（需求第 8 条）
      rows.push(row('智能重命名',
        '已将 ' + (t.renamed_count || (res.renamed || []).length)
        + ' 个文件重命名为规范名'
        + (t.rename_note ? '（网盘目录已生成「' + t.rename_note + '」记录）' : '')));
    if (t.target_fid) rows.push(row('网盘 fid', t.target_fid));
  }
  if (t.error) rows.push(row('状态信息', t.error));
  for (const [a, b] of rows) { dl.appendChild(a); dl.appendChild(b); }
  body.appendChild(dl);

  if (t.status === 'COMPLETED') {
    if ((t.files || []).length) body.appendChild(fileTable(t.files));
    const tip = document.createElement('div'); tip.className = 'meta';
    tip.style.marginTop = '14px';
    tip.textContent = '文件已保存到网盘根目录「' + (t.save_dir || 'GODDAMNCOOL')
      + '」文件夹，可在网盘中直接查看/播放。';
    body.appendChild(tip);
  }
  const note = document.createElement('div');
  note.className = 'meta';
  note.textContent = '任务由后台 Worker 自动执行：解析分享 → 选取文件 → 防重 → 转存。'
    + '页面每 5 秒自动刷新，可留在本页等待结果。';
  body.appendChild(note);
}
async function tick(first) {
  if (first) showLoading('加载中…');  // 首次加载气泡提示（轮询期间不弹）
  try {
    const t = await api('/api/task/' + taskId);
    hideLoading();
    render(t);
    return ['PENDING', 'SUBMITTING', 'PROCESSING'].includes(t.status);
  } catch (e) {
    hideLoading();
    showMsg(e.message, false);
    return false;
  }
}
(async () => {
  if (await tick(true)) setInterval(tick, 5000);
})();
