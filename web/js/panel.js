// 控制面板：账户概览、网盘 Cookie / 保存目录管理、Stremio 接入、密码与任务。
// 全部动态数据 textContent 渲染（防 XSS）。
(function () {
  var meData = null;

  function badge(text, cls) {
    var s = el('span', 'badge' + (cls ? ' ' + cls : ''));
    s.textContent = text;
    return s;
  }

  // 配额小卡片（仿 admin API 统计卡风格）
  var QUOTA_LABEL = {
    search:   { label: '搜索', desc: '网盘资源搜索' },
    transfer: { label: '转存', desc: '保存到我的网盘' },
    play:     { label: '播放', desc: '在线播放' },
  };

  function _quotaCard(qk, q) {
    var label = QUOTA_LABEL[qk] || { label: qk, desc: '' };
    var card = el('div');
    card.style.cssText = 'flex:1;min-width:160px;padding:12px 14px;'
      + 'border:1px solid var(--border,rgba(255,255,255,.1));'
      + 'border-radius:10px;background:rgba(255,255,255,.03);';

    var h4 = el('div');
    h4.style.cssText = 'font-size:.78rem;opacity:.6;margin-bottom:4px';
    h4.textContent = label.label + ' · ' + label.desc;
    card.appendChild(h4);

    var big = el('div');
    big.style.cssText = 'font-size:1.6rem;font-weight:600';
    big.textContent = (q && q.total) || 0;
    card.appendChild(big);

    var sub = el('div');
    sub.style.cssText = 'font-size:.78rem;opacity:.7;margin-top:4px';
    if (!q) {
      sub.textContent = '加载失败';
    } else if (q.limit == null) {
      sub.textContent = '今日 ' + q.used + ' 次';
    } else {
      var limitTxt = q.unlimited ? '∞' : q.limit;
      sub.textContent = '今日 ' + q.used + ' / ' + limitTxt + ' 次';
    }
    card.appendChild(sub);
    return card;
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
    // 配额小卡片区
    var box = document.getElementById('quota-summary');
    box.textContent = '';
    var q = meData.quota || {};
    box.appendChild(_quotaCard('search',   q.search));
    box.appendChild(_quotaCard('transfer', q.transfer));
    box.appendChild(_quotaCard('play',     q.play));
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
  // 剪贴板写入：clipboard API 仅在 HTTPS 可用，HTTP 环境回退 execCommand
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext)
      return navigator.clipboard.writeText(text);
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { /* 忽略 */ }
    document.body.removeChild(ta);
    return ok ? Promise.resolve()
              : Promise.reject(new Error('浏览器拒绝了剪贴板访问'));
  }

  document.getElementById('addon-copy').addEventListener('click', function () {
    var url = document.getElementById('addon-url').value;
    if (!url) return;
    copyText(url)
      .then(function () { showMsg('已复制到剪贴板', true); })
      .catch(function () {
        // 复制彻底不可用时的兜底：选中内容让用户手动 Ctrl+C
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
