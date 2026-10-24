// TV 遥控器适配：方向键空间导航 + 非按钮元素的 Enter/空格激活。
// 原理：按键时以当前焦点元素为原点，向按键方向投影，选"前向距离 + 垂直偏轴
// 惩罚"最小的可聚焦元素；输入框内左右键保留光标移动，上下键跳出。
(function () {
  const KEYMAP = { ArrowUp: [0, -1], ArrowDown: [0, 1],
                   ArrowLeft: [-1, 0], ArrowRight: [1, 0] };

  function focusables() {
    // 弹窗打开时焦点锁定在弹窗内（TV 遥控器不会导航到背景内容）
    const modal = document.querySelector('.modal:not([hidden])');
    return Array.from(document.querySelectorAll(
      'a[href],button,input,textarea,select,[tabindex]'))
      .filter((e) => !e.disabled && e.tabIndex >= 0
        && e.offsetParent !== null && !e.closest('[hidden]')
        && (!modal || modal.contains(e)));
  }

  function center(el) {
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  function pick(cur, dir) {
    const [dx, dy] = dir;
    const c = center(cur);
    let best = null;
    let bestScore = Infinity;
    for (const e of focusables()) {
      if (e === cur) continue;
      const p = center(e);
      const vx = p.x - c.x;
      const vy = p.y - c.y;
      const dot = vx * dx + vy * dy;           // 沿方向的前向距离
      if (dot <= 0) continue;                   // 只考虑方向前方的元素
      const off = Math.abs(vx * dy - vy * dx);  // 垂直偏轴距离
      const score = dot + off * 2.5;
      if (score < bestScore) { bestScore = score; best = e; }
    }
    return best;
  }

  document.addEventListener('keydown', (ev) => {
    // div[tabindex]（如文件行）等非原生可激活元素：Enter/空格触发 click
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target
        && ev.target.matches && ev.target.matches('.frow')) {
      ev.preventDefault();
      ev.target.click();
      return;
    }
    const dir = KEYMAP[ev.key];
    if (!dir) return;
    const cur = document.activeElement;
    const inText = cur && (cur.tagName === 'INPUT' || cur.tagName === 'TEXTAREA');
    // 文本框内：左右键移动光标（保留默认行为），上下键跳出输入框
    if (inText && dir[1] === 0) return;
    if (!cur || cur === document.body) {
      const first = focusables()[0];
      if (first) { first.focus(); ev.preventDefault(); }
      return;
    }
    const next = pick(cur, dir);
    if (next) {
      next.focus();
      next.scrollIntoView({ block: 'nearest' });
      ev.preventDefault();
    }
  });
})();
