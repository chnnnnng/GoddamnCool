// 管理页共用工具：会话令牌管理、API 调用封装、DOM 与消息提示。
// 安全约定：第三方标题/任务字段等动态数据一律 textContent 渲染，禁止 innerHTML（防 XSS）。
// 会话约定：token 存 localStorage（X-Session-Token 头），401 时清除并跳转 /login。
const SESSION_KEY = 'qk_session';

function getToken() {
  return localStorage.getItem(SESSION_KEY) || '';
}

function setToken(t) {
  if (t) localStorage.setItem(SESSION_KEY, t);
  else localStorage.removeItem(SESSION_KEY);
}

async function api(url, opts = {}) {
  const token = getToken();
  opts.headers = Object.assign(
    token ? {'X-Session-Token': token} : {}, opts.headers || {});
  const r = await fetch(url, opts);
  if (r.status === 401) {
    setToken('');
    if (location.pathname !== '/login') {
      location.href = '/login';
      throw new Error('请先登录');
    }
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}

// 当前登录用户信息（GET /api/auth/me 的封装）
function me() {
  return api('/api/auth/me');
}

async function logout() {
  try { await api('/api/auth/logout', {method: 'POST'}); } catch (e) {}
  setToken('');
  location.href = '/login';
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function showMsg(text, ok) {
  const m = document.getElementById('msg');
  if (!m) return;
  if (!text) {  // 空文本 = 清除消息（还原为隐藏态，避免残留空的消息条）
    m.className = 'msg';
    m.textContent = '';
    return;
  }
  m.className = 'msg ' + (ok ? 'ok' : 'err');
  m.textContent = text;
}
