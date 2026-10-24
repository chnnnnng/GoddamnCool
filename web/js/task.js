// 转存任务页：读取路径中的任务 ID，轮询 /api/task/{id} 渲染六态状态。
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
    showMsg(e.message, false);
    return false;
  }
}
(async () => {
  if (await tick()) setInterval(tick, 5000);
})();
