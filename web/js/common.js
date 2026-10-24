// 管理页共用工具：访问令牌管理、API 调用封装、DOM 与消息提示。
// 安全约定：第三方标题/任务字段等动态数据一律 textContent 渲染，禁止 innerHTML（防 XSS）。
const TOKEN_KEY = 'qk_admin_token';

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
