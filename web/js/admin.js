// 管理后台：用户管理（建号/级别/禁用/重置密码/删除）与运行时配置。
// 非管理员访问自动跳回搜索页；全部动态数据 textContent 渲染（防 XSS）。
(function () {
  // 权限门卫：非管理员直接离开
  me().then(function (data) {
    if (data.role !== 'admin') location.href = '/search';
  }).catch(function () {});

  document.getElementById('btn-logout').addEventListener('click', logout);

  // ---- 用户表格 ----

  function opBtn(text, fn, cls) {
    var b = el('button', cls || 'sec', text);
    b.type = 'button';
    b.addEventListener('click', fn);
    return b;
  }

  function act(userId, body, okMsg) {
    return api('/api/admin/users/' + userId, { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) })
      .then(function () { showMsg(okMsg, true); loadUsers(); })
      .catch(function (e) { showMsg(e.message, false); });
  }

  function renderUsers(users) {
    var tbody = document.getElementById('user-rows');
    tbody.textContent = '';
    if (!users.length) {
      var tr0 = el('tr');
      var td0 = el('td', 'meta', '暂无用户');
      td0.colSpan = 9;
      tr0.appendChild(td0);
      tbody.appendChild(tr0);
      return;
    }
    users.forEach(function (u) {
      var tr = el('tr');
      tr.appendChild(el('td', null, u.email));
      tr.appendChild(el('td', null, u.role === 'admin' ? '管理员' : '用户'));
      tr.appendChild(el('td', null, u.tier === 'pro' ? 'Pro' : 'Lite'));
      tr.appendChild(el('td', null, u.cookie_bound ? '已绑定' : '未绑定'));
      tr.appendChild(el('td', null, String(u.quota_used)));
      tr.appendChild(el('td', null, String(u.transfer_used)));
      tr.appendChild(el('td', null, String(u.task_count)));
      tr.appendChild(el('td', null, u.created_at || ''));
      if (u.disabled) tr.style.opacity = '.5';

      var ops = el('td', 'wrap');
      ops.appendChild(opBtn(u.tier === 'pro' ? '降 Lite' : '升 Pro',
        function () { return act(u.id, { tier: u.tier === 'pro' ? 'lite' : 'pro' },
          '已更新级别'); }));
      ops.appendChild(opBtn(u.role === 'admin' ? '撤管理员' : '设管理员',
        function () { return act(u.id, { role: u.role === 'admin' ? 'user' : 'admin' },
          '已更新角色'); }));
      ops.appendChild(opBtn(u.disabled ? '启用' : '禁用',
        function () { return act(u.id, { disabled: !u.disabled },
          u.disabled ? '已启用' : '已禁用（会话已清除）'); }));
      ops.appendChild(opBtn('重置密码', function () {
        var pw = prompt('输入新密码（至少 8 位）');
        if (pw === null) return;
        if (pw.length < 8) { showMsg('密码至少 8 位', false); return; }
        return act(u.id, { password: pw }, '密码已重置');
      }));
      ops.appendChild(opBtn('删除', function () {
        if (!confirm('确定删除用户 ' + u.email + '？其会话与网盘绑定将被清除，任务记录保留。')) return;
        api('/api/admin/users/' + u.id, { method: 'DELETE' })
          .then(function () { showMsg('已删除', true); loadUsers(); })
          .catch(function (e) { showMsg(e.message, false); });
      }, 'danger'));
      tr.appendChild(ops);
      tbody.appendChild(tr);
    });
  }

  function loadUsers() {
    api('/api/admin/users')
      .then(function (r) { renderUsers(r.users || []); })
      .catch(function (e) { showMsg(e.message, false); });
  }

  // ---- 新建用户 ----
  document.getElementById('form-newuser').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var email = document.getElementById('nu-email').value.trim();
    var pass = document.getElementById('nu-pass').value;
    var tier = document.getElementById('nu-tier').value;
    var role = document.getElementById('nu-role').value;
    api('/api/admin/users', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, password: pass, tier: tier, role: role }) })
      .then(function () {
        showMsg('用户已创建', true);
        document.getElementById('nu-email').value = '';
        document.getElementById('nu-pass').value = '';
        loadUsers();
      })
      .catch(function (e) { showMsg(e.message, false); });
  });

  // ---- 运行时配置 ----

  function configInput(item) {
    var input = document.createElement('input');
    input.id = 'cfg-' + item.key;
    input.value = item.value === null || item.value === undefined ? '' : String(item.value);
    if (typeof item.value === 'number') input.type = 'number';
    input.step = 'any';
    return input;
  }

  function renderConfig(items) {
    var box = document.getElementById('config-rows');
    box.textContent = '';
    items.forEach(function (item) {
      var lab = el('label');
      lab.textContent = item.key + (item.meta ? '（' + item.meta + '）' : '');
      lab.htmlFor = 'cfg-' + item.key;
      box.appendChild(lab);
      box.appendChild(configInput(item));
    });
  }

  function loadConfig() {
    api('/api/admin/config')
      .then(function (r) { renderConfig(r.config || []); })
      .catch(function (e) { showMsg(e.message, false); });
  }

  document.getElementById('cfg-save').addEventListener('click', function () {
    var updates = {};
    document.querySelectorAll('#config-rows input').forEach(function (input) {
      var key = input.id.slice(4);
      var raw = input.value.trim();
      updates[key] = input.type === 'number' ? Number(raw) : raw;
    });
    api('/api/admin/config', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates) })
      .then(function () { showMsg('配置已保存并生效', true); })
      .catch(function (e) { showMsg(e.message, false); });
  });

  loadUsers();
  loadConfig();
})();
