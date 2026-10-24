// 授权页交互：表单经 fetch 提交到 POST /setup（返回 JSON），结果行内展示。
const form = document.getElementById('form');
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('btn');
  const busy = document.getElementById('busy');
  btn.disabled = true;
  busy.style.display = 'inline';
  try {
    const r = await fetch('/setup', {method: 'POST', body: new FormData(form)});
    const data = await r.json().catch(() => ({}));
    showMsg(data.message || ('HTTP ' + r.status), r.ok && data.ok);
  } catch (err) {
    showMsg('请求失败：' + err.message, false);
  } finally {
    btn.disabled = false;
    busy.style.display = 'none';
  }
});
