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

// 消息提示：页面顶部气泡 toast（自动消失），替代底部行内消息条。
function showMsg(text, ok) {
  if (!text) return;  // 空文本 = 无操作（气泡自动消失，无需手动清除）
  var t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = text;
  t.className = 'show ' + (ok ? 'ok' : 'err');
  clearTimeout(showMsg._timer);
  showMsg._timer = setTimeout(function () { t.className = ''; }, 3500);
}
