// 控制面板：账户概览、网盘 Cookie / 保存目录管理、Stremio 接入、密码与任务。
// 全部动态数据 textContent 渲染（防 XSS）。
(function () {
  var meData = null;

  function badge(text, cls) {
    var s = el('span', 'badge' + (cls ? ' ' + cls : ''));
    s.textContent = text;
    return s;
  }

  function quotaText(q) {
    if (!q) return '…';
    // Pro/管理员不限 → "2/∞"；Lite → "2/50"
    return q.used + ' / ' + (q.unlimited ? '∞' : q.limit);
  }

  function renderAccount() {
    document.getElementById('acc-email').textContent = meData.email;
    var roleEl = document.getElementById('acc-role');
    roleEl.textContent = meData.role === 'admin' ? '管理员' : '用户';
    roleEl.className = 'badge' + (meData.role === 'admin' ? ' ok' : '');
    roleEl.hidden = false;
    var tierEl = document.getElementById('acc-tier');
    tierEl.textContent = meData.tier === 'pro' ? 'Pro' : 'Lite';
    tierEl.className = 'badge' + (meData.tier === 'pro' ? ' ok' : ' warn');
    tierEl.hidden = false;
    document.getElementById('acc-quota-search').textContent =
      quotaText(meData.quota && meData.quota.search);
    document.getElementById('acc-quota-transfer').textContent =
      quotaText(meData.quota && meData.quota.transfer);
  }

  function renderCookie() {
    var state = document.getElementById('ck-state');
    state.textContent = '';
    state.appendChild(meData.cookie_bound
      ? badge('已绑定', 'ok')
      : badge('未绑定', 'warn'));
    // 检测/解绑按钮：仅已绑定时可用（检测按钮实时验证 Cookie 有效性）
    document.getElementById('ck-unbind').hidden = !meData.cookie_bound;
    document.getElementById('ck-check').hidden = !meData.cookie_bound;
  }

  function renderSaveDir() {
    document.getElementById('sd-input').value = meData.save_dir || '';
    // 默认目录提示：写出管理员配置的服务器默认目录（空 = 网盘根目录）
    var d = meData.default_save_dir || '';
    document.getElementById('sd-default-tip').textContent = d
      ? '留空表示使用服务器默认目录「' + d + '」（管理员配置）。'
      : '留空表示直接保存到网盘根目录（服务器未配置默认目录）。';
  }

  function renderAddon() {
    document.getElementById('addon-url').value = meData.addon_url || '';
  }

  function loadMe() {
    return me().then(function (data) {
      meData = data;
      renderAccount();
      renderCookie();
      renderSaveDir();
      renderAddon();
      return data;
    }).catch(function (e) {
      // 401 已由 api() 跳登录；其余错误行内提示
      showMsg(e.message, false);
      throw e;
    });
  }

  // ---- 网盘 Cookie ----
  document.getElementById('form-cookie').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var cookie = document.getElementById('ck-input').value;
    if (!cookie.trim()) { showMsg('请粘贴 Cookie 内容', false); return; }
    api('/api/me/cookie', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cookie: cookie }) })
      .then(function (r) {
        document.getElementById('ck-input').value = '';
        showMsg(r.message, true);
        return loadMe();
      })
      .catch(function (e) { showMsg(e.message, false); });
  });

  document.getElementById('ck-unbind').addEventListener('click', function () {
    if (!confirm('确定解除夸克网盘 Cookie 绑定？解除后搜索与转存将不可用。')) return;
    api('/api/me/cookie', { method: 'DELETE' })
      .then(function (r) { showMsg(r.message, true); return loadMe(); })
      .catch(function (e) { showMsg(e.message, false); });
  });

  // 绑定状态检测：实时验证已绑定 Cookie 是否有效（结果同步刷新徽章）
  document.getElementById('ck-check').addEventListener('click', function () {
    var btn = document.getElementById('ck-check');
    btn.disabled = true;
    api('/api/me/cookie/check', { method: 'POST' })
      .then(function (r) {
        showMsg(r.message, r.ok);
        var state = document.getElementById('ck-state');
        state.textContent = '';
        state.appendChild(badge(r.ok ? '已绑定' : '已失效',
                                r.ok ? 'ok' : 'err'));
      })
      .catch(function (e) { showMsg(e.message, false); })
      .finally(function () { btn.disabled = false; });
  });

  // ---- 保存目录 ----
  document.getElementById('sd-save').addEventListener('click', function () {
    var v = document.getElementById('sd-input').value;
    api('/api/me/save_dir', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ save_dir: v }) })
      .then(function (r) { showMsg(r.message, true); return loadMe(); })
      .catch(function (e) { showMsg(e.message, false); });
  });

  document.getElementById('sd-clear').addEventListener('click', function () {
    api('/api/me/save_dir', { method: 'DELETE' })
      .then(function (r) { showMsg(r.message, true); return loadMe(); })
      .catch(function (e) { showMsg(e.message, false); });
  });

  // ---- Stremio 接入 ----
  document.getElementById('addon-copy').addEventListener('click', function () {
    var url = document.getElementById('addon-url').value;
    if (!url) return;
    navigator.clipboard.writeText(url)
      .then(function () { showMsg('已复制到剪贴板', true); })
      .catch(function () {
        // 剪贴板权限不可用时的兜底：选中内容让用户手动 Ctrl+C
        var input = document.getElementById('addon-url');
        input.select();
        showMsg('已选中内容，请按 Ctrl+C 复制', true);
      });
  });

  document.getElementById('addon-rotate').addEventListener('click', function () {
    if (!confirm('确定重置 Add-on 地址？旧地址将立即失效。')) return;
    api('/api/me/token/rotate', { method: 'POST' })
      .then(function () { showMsg('Add-on 地址已重置', true); return loadMe(); })
      .catch(function (e) { showMsg(e.message, false); });
  });

  // ---- 修改密码 ----
  document.getElementById('form-pass').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var oldv = document.getElementById('pw-old').value;
    var newv = document.getElementById('pw-new').value;
    if (newv.length < 8) { showMsg('新密码至少 8 位', false); return; }
    api('/api/me/password', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldv, new_password: newv }) })
      .then(function (r) {
        document.getElementById('pw-old').value = '';
        document.getElementById('pw-new').value = '';
        showMsg(r.message, true);
      })
      .catch(function (e) { showMsg(e.message, false); });
  });

  // ---- 我的任务 ----
  function renderTasks(tasks) {
    var list = document.getElementById('tasklist');
    list.textContent = '';
    if (!tasks.length) {
      var li = el('li');
      li.appendChild(el('span', 't-title meta', '暂无任务'));
      list.appendChild(li);
      return;
    }
    tasks.forEach(function (t) {
      var li = el('li');
      var a = el('a', 't-title', t.title || t.candidate_title || '外部资源');
      a.href = '/task/' + t.id;
      li.appendChild(a);
      li.appendChild(el('span', 't-time', t.created_at || ''));
      var s = el('span', 'status s-' + t.status);
      s.textContent = t.status_text || t.status;
      li.appendChild(s);
      list.appendChild(li);
    });
  }

  function loadTasks() {
    api('/api/me/tasks')
      .then(function (r) { renderTasks(r.tasks || []); })
      .catch(function (e) { showMsg(e.message, false); });
  }

  // ---- 登出 ----
  document.getElementById('btn-logout').addEventListener('click', logout);

  loadMe().then(loadTasks).catch(function () {});
})();
