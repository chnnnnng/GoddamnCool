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
      rows.push(row('转存文件', res.name + (res.fid ? '　(fid: ' + res.fid + ')' : '')));
    if (t.target_fid) rows.push(row('网盘 fid', t.target_fid));
  }
  if (t.error) rows.push(row('状态信息', t.error));
  for (const [a, b] of rows) { dl.appendChild(a); dl.appendChild(b); }
  body.appendChild(dl);

  if (t.status === 'COMPLETED') {
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
