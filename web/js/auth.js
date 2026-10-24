// 登录/注册页：双 Tab 切换；成功后写入会话令牌并跳转搜索页。
(function () {
  // 已登录用户直接进搜索页
  if (getToken()) {
    me().then(() => { location.href = '/search'; }).catch(() => {});
  }

  var tabLogin = document.getElementById('tab-login');
  var tabReg = document.getElementById('tab-reg');
  var formLogin = document.getElementById('form-login');
  var formReg = document.getElementById('form-reg');
  var title = document.getElementById('page-title');

  function switchTab(showReg) {
    tabLogin.classList.toggle('active', !showReg);
    tabReg.classList.toggle('active', showReg);
    formReg.hidden = !showReg;
    formLogin.hidden = showReg;
    title.textContent = showReg ? '注册' : '登录';
    showMsg('');
  }

  tabLogin.addEventListener('click', function () { switchTab(false); });
  tabReg.addEventListener('click', function () { switchTab(true); });

  formLogin.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var email = document.getElementById('li-email').value.trim();
    var password = document.getElementById('li-pass').value;
    api('/api/auth/login', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, password: password }) })
      .then(function (data) {
        setToken(data.token);
        location.href = '/search';
      })
      .catch(function (e) { showMsg(e.message, false); });
  });

  formReg.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var email = document.getElementById('rg-email').value.trim();
    var password = document.getElementById('rg-pass').value;
    var invite = document.getElementById('rg-invite').value.trim();
    api('/api/auth/register', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, password: password, invite: invite }) })
      .then(function (data) {
        setToken(data.token);
        location.href = '/panel';
      })
      .catch(function (e) { showMsg(e.message, false); });
  });
})();
