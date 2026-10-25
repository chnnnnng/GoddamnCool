// 管理后台：用户管理（建号/级别/禁用/重置密码/删除）与运行时配置。
// 非管理员访问自动跳回搜索页；全部动态数据 textContent 渲染（防 XSS）。
(function () {
  // 权限门卫：非管理员直接离开
  me().then(function (data) {
    if (data.role !== 'admin') location.href = '/search';
  }).catch(function () {});

  document.getElementById('btn-logout').addEventListener('click', logout);

  // ---- 用户表格（仅邮箱/角色/级别 + 详情入口；完整信息在弹窗中） ----

  var allUsers = [];  // 缓存全量用户，搜索框做前端过滤

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
      var td0 = el('td', 'meta', '无匹配用户');
      td0.colSpan = 4;
      tr0.appendChild(td0);
      tbody.appendChild(tr0);
      return;
    }
    users.forEach(function (u) {
      var tr = el('tr');
      if (u.disabled) tr.style.opacity = '.5';
      tr.appendChild(el('td', null, u.email));
      tr.appendChild(el('td', null, u.role === 'admin' ? '管理员' : '用户'));
      tr.appendChild(el('td', null, u.tier === 'pro' ? 'Pro' : 'Lite'));
      var ops = el('td', 'wrap');
      ops.appendChild(opBtn('详情', function () { openUserModal(u); }));
      tr.appendChild(ops);
      tbody.appendChild(tr);
    });
  }

  // 用户详情弹窗：完整信息 + 全部操作（表格行与搜索结果共用）
  function openUserModal(u) {
    document.getElementById('um-email').textContent = u.email;
    var kv = document.getElementById('um-kv');
    kv.textContent = '';
    function kvRow(k, v) {
      kv.appendChild(el('dt', null, k));
      kv.appendChild(el('dd', null, v));
    }
    kvRow('角色', u.role === 'admin' ? '管理员' : '用户');
    kvRow('级别', u.tier === 'pro' ? 'Pro' : 'Lite');
    kvRow('状态', u.disabled ? '已禁用' : '正常');
    kvRow('网盘', u.cookie_bound ? '夸克网盘 - 已绑定' : '未绑定');
    kvRow('今日搜索', String(u.quota_used));
    kvRow('今日转存', String(u.transfer_used));
    kvRow('任务数', String(u.task_count));
    kvRow('保存目录', u.save_dir || '（默认）');
    kvRow('注册时间', u.created_at || '');

    var ops = document.getElementById('um-ops');
    ops.textContent = '';
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
    document.getElementById('u-modal').classList.add('show');
  }

  function closeUserModal() {
    document.getElementById('u-modal').classList.remove('show');
  }
  document.getElementById('u-modal-close').addEventListener('click', closeUserModal);
  document.getElementById('u-modal').addEventListener('click', function (ev) {
    if (ev.target === this) closeUserModal();  // 点遮罩关闭
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') closeUserModal();
  });

  // 用户搜索：按邮箱子串过滤（前端过滤缓存列表）
  document.getElementById('u-search').addEventListener('input', function () {
    var kw = this.value.trim().toLowerCase();
    renderUsers(!kw ? allUsers
      : allUsers.filter(function (u) {
          return u.email.toLowerCase().indexOf(kw) >= 0; }));
  });

  function loadUsers() {
    api('/api/admin/users')
      .then(function (r) {
        allUsers = r.users || [];
        var kw = document.getElementById('u-search').value.trim().toLowerCase();
        renderUsers(!kw ? allUsers
          : allUsers.filter(function (u) {
              return u.email.toLowerCase().indexOf(kw) >= 0; }));
      })
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

  // ---- 名称解析问题名单（无法解析出标题的文件名） ----

  var issueRows = document.getElementById('issue-rows');
  var SOURCE_TEXT = { sample: '采样', search: '搜索', task: '任务' };

  function renderIssues(issues) {
    issueRows.textContent = '';
    if (!issues.length) {
      emptyIssuesRow('暂无记录');
      return;
    }
    issues.forEach(function (it) {
      var tr = document.createElement('tr');
      var tdN = document.createElement('td');
      tdN.className = 'wrap';
      tdN.textContent = it.name;  // textContent 渲染，防 XSS
      var tdS = document.createElement('td');
      tdS.textContent = SOURCE_TEXT[it.source] || it.source || '—';
      var tdH = document.createElement('td');
      tdH.textContent = it.hits;
      var tdT = document.createElement('td');
      tdT.className = 'meta';
      tdT.textContent = it.last_seen;
      tr.append(tdN, tdS, tdH, tdT);
      issueRows.appendChild(tr);
    });
  }

  function emptyIssuesRow(text) {
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = 4;
    td.className = 'meta';
    td.textContent = text;
    tr.appendChild(td);
    issueRows.appendChild(tr);
  }

  function loadIssues() {
    api('/api/admin/name-issues')
      .then(function (r) { renderIssues(r.issues || []); })
      .catch(function (e) { emptyIssuesRow('加载失败：' + e.message); });
  }

  document.getElementById('issues-reload').addEventListener('click', loadIssues);

  // ---- 缓存管理 ----

  function renderCacheInfo(data) {
    var c = data.caches || {};
    var lines = [];
    if (!Object.keys(c).length) {
      lines.push('（无缓存）');
    } else {
      Object.keys(c).forEach(function (k) {
        lines.push(k + '：' + c[k] + ' 条');
      });
    }
    document.getElementById('cache-info').textContent = lines.join('  ·  ');
  }

  function loadCache() {
    api('/api/admin/cache-info').then(renderCacheInfo).catch(function () {});
  }

  function clearCache(prefix, label) {
    api('/api/admin/cache-clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prefix: prefix }),
    }).then(function (data) {
      alert(label + '：已清理 ' + data.removed + ' 条');
      loadCache();
    }).catch(function (e) { alert('清理失败：' + (e.message || e)); });
  }

  document.getElementById('cache-clear-all').addEventListener('click', function () {
    if (!confirm('确定清理**所有**缓存？\n（搜索结果、LLM 精选、TMDB 元数据全部清空）')) return;
    clearCache(null, '全部缓存');
  });
  document.getElementById('cache-clear-search').addEventListener('click', function () {
    clearCache('search', '搜索缓存');
  });
  document.getElementById('cache-clear-llm').addEventListener('click', function () {
    clearCache('llm', 'LLM 缓存');
  });
  document.getElementById('cache-clear-tmdb').addEventListener('click', function () {
    clearCache('tmdb', 'TMDB 缓存');
  });

  // ---- API 调用统计 ----

  var KIND_LABEL = {
    pansou: { label: 'PanSou', desc: '网盘搜索' },
    tmdb:   { label: 'TMDB',   desc: '元数据查询' },
    llm:    { label: 'LLM',    desc: 'AI 精选 + 文件名解析' },
  };

  function renderStats(data) {
    var sumBox = document.getElementById('stats-summary');
    sumBox.textContent = '';

    // 总数徽标
    document.getElementById('stats-total').textContent =
      '累计 ' + (data.total || 0) + ' 次';

    var kinds = ['pansou', 'tmdb', 'llm'];
    kinds.forEach(function (k) {
      var s = (data.summary || {})[k] || { total: 0, ok: 0, fail: 0, avg_ms: 0, max_ms: 0 };
      var rate = s.total ? Math.round((s.ok / s.total) * 100) : 0;
      var label = KIND_LABEL[k] || { label: k, desc: '' };

      var card = document.createElement('div');
      card.style.cssText = 'flex:1;min-width:180px;padding:12px 14px;border:1px solid var(--border,rgba(255,255,255,.1));border-radius:10px;background:rgba(255,255,255,.03);';

      var h4 = document.createElement('div');
      h4.style.cssText = 'font-size:.78rem;opacity:.6;margin-bottom:4px';
      h4.textContent = label.label + ' · ' + label.desc;
      card.appendChild(h4);

      var big = document.createElement('div');
      big.style.cssText = 'font-size:1.6rem;font-weight:600';
      big.textContent = s.total;
      card.appendChild(big);

      var sub = document.createElement('div');
      sub.style.cssText = 'font-size:.78rem;opacity:.7;margin-top:4px';
      sub.textContent = '成功率 ' + rate + '%  ·  平均 ' + s.avg_ms + 'ms  ·  失败 ' + s.fail;
      card.appendChild(sub);

      sumBox.appendChild(card);
    });

    // 最近 30 条
    var tbody = document.getElementById('stats-recent');
    tbody.textContent = '';
    var recent = data.recent || [];
    if (!recent.length) {
      var tr0 = document.createElement('tr');
      var td0 = document.createElement('td');
      td0.colSpan = 6;
      td0.className = 'meta';
      td0.textContent = '暂无调用记录';
      tr0.appendChild(td0);
      tbody.appendChild(tr0);
    } else {
      recent.forEach(function (r) {
        var tr = document.createElement('tr');
        tr.appendChild(el('td', 'meta', r.created_at || ''));
        tr.appendChild(el('td', null, (KIND_LABEL[r.kind] || {}).label || r.kind || ''));
        tr.appendChild(el('td', null, r.endpoint || ''));
        tr.appendChild(el('td', null, String(r.duration_ms || 0)));
        tr.appendChild(el('td', r.success ? '' : 'bad', r.success ? '成功' : '失败'));
        var metaText = '';
        try { metaText = typeof r.meta === 'string' ? r.meta : JSON.stringify(r.meta || {}); }
        catch (e) { metaText = ''; }
        if (r.error) metaText += (metaText ? ' · ' : '') + 'err: ' + r.error;
        tr.appendChild(el('td', 'meta', metaText.slice(0, 80)));
        tbody.appendChild(tr);
      });
    }
  }

  function loadStats() {
    api('/api/admin/api-stats')
      .then(renderStats)
      .catch(function (e) {
        document.getElementById('stats-total').textContent = '加载失败';
      });
  }

  loadUsers();
  loadConfig();
  loadIssues();
  loadCache();
  loadStats();
})();
