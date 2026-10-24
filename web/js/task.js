// 转存任务页：读取路径中的任务 ID，轮询 /api/task/{id} 渲染六态状态。
// 统一展示转存结果，不再区分是否关联 Stremio 影片。
const taskId = location.pathname.split('/').pop();
const STATUS_TEXT = {PENDING: '等待执行', SUBMITTING: '正在提交转存',
                     PROCESSING: '转存处理中', COMPLETED: '转存完成',
                     FAILED: '转存失败', EXPIRED: '任务超时或失效'};
// 定位方式（result_json.located）→ 展示文案
const LOCATED_TEXT = {save_as: '转存响应定位',
                      dedup: '网盘已有同名文件（防重跳过）',
                      search: '网盘搜索定位'};

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
// COMPLETED 任务的落盘明细表：文件名 / 大小 / 资源规格 / 命名规范警告
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
    tdName.textContent = f.name;
    if (f.warn) {  // 疑似不符合命名规范（与 matcher 规则一致）：黄色提示
      tdName.appendChild(document.createTextNode(' '));
      const b = badge('⚠ 文件名不规范', 'warn');
      b.title = f.warn;  // 悬停查看完整建议
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
    if (res.located && LOCATED_TEXT[res.located])
      rows.push(row('定位方式', LOCATED_TEXT[res.located]));
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
async function tick() {
  try {
    const t = await api('/api/task/' + taskId);
    render(t);
    return ['PENDING', 'SUBMITTING', 'PROCESSING'].includes(t.status);
  } catch (e) {
    showMsg(e.message, false);
    return false;
  }
}
(async () => {
  if (await tick()) setInterval(tick, 5000);
})();
