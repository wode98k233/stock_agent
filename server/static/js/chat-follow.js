/* ─────────────────────────────────────────────
   选股雷达 Web — 对话跟随（Follow）

   解决的问题：Agent 运行时思考链持续增长，但页面不会自动往下滚，
   用户看到「输出超过屏幕了也没往下滚动」。

   行为：
   - 任务开始默认开启跟随；新内容（过程流节点 / 打字机每帧 / 进度行 /
     最终回答逐字）持续把视口钉在底部；
   - 用户主动向上滚动 → 跟随暂停，按钮变「回到底部 ↓」；
   - 滚回底部或点按钮 → 恢复跟随并立即贴底。

   ⚠️ 滚动容器不是 #messageList：`.message-list` 没有 overflow-y，
   真正可滚动的是 `.chat-pane`（overflow-y: auto）。写
   `messageList.scrollTop = …` 完全无效 —— 这是「不滚动」的根因。
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.chatFollow = (() => {
  const THRESHOLD = 48;          // 距底 ≤48px 视为「在底部」
  const state = { enabled: true, running: false, btn: null, scroller: null };
  let pendingFrame = null;

  /* 向上找第一个真正可滚动的祖先；兜底返回自身 */
  /* ⚠️ 只按 overflowY 判定，**不能**再加 scrollHeight > clientHeight：
     空对话/短对话时 .chat-pane 并不溢出，会被跳过而落到 document.body，
     之后 state.scroller 缓存了 body，pin() 全程静默失效。 */
  function scrollParent(el) {
    let n = el;
    while (n && n !== document.body && n !== document.documentElement) {
      const s = window.getComputedStyle(n);
      if (/(auto|scroll|overlay)/.test(s.overflowY)) return n;
      n = n.parentElement;
    }
    return el;
  }

  function scroller() {
    if (state.scroller && state.scroller.isConnected) return state.scroller;
    const list = document.getElementById("messageList");
    state.scroller = scrollParent(list || document.body);
    return state.scroller;
  }

  function isAtBottom() {
    const el = scroller();
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= THRESHOLD;
  }

  function _render() {
    const b = state.btn;
    if (!b) return;
    const on = state.enabled;
    b.classList.toggle("is-on", on);
    // 运行中常驻（让用户知道有跟随这回事），暂停时常驻；空闲且在底部则隐藏
    b.classList.toggle("show", state.running || !on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on ? "跟随最新内容中（点击暂停）" : "已暂停跟随（点击回到底部并恢复）";
    b.querySelector(".cf-label").textContent = on ? "跟随中" : "回到底部";
  }

  function setEnabled(on) {
    if (state.enabled === on) return;
    state.enabled = on;
    _render();
  }

  function pin() {
    if (!state.enabled) return;
    const el = scroller();
    if (!el) return;
    if (pendingFrame != null) return;
    pendingFrame = window.requestAnimationFrame(() => {
      pendingFrame = null;
      const cur = scroller();
      if (!state.enabled || !cur) return;
      cur.scrollTop = cur.scrollHeight;
    });
  }

  /* 用户「主动上滚」的识别：只看输入意图（wheel / touch / 键盘），
     不用 scroll 事件 —— 程序自身 scrollTop 赋值同样会派发 scroll，
     且其 isTrusted 也是 true，两者不可区分。 */
  function isUpIntent(ev) {
    if (ev.type === "wheel") return ev.deltaY < 0;
    if (ev.type === "touchmove") return (ev.touches[0] || {}).clientY > (state._lastTouchY || 0);
    if (ev.type === "keydown") {
      return ["PageUp", "ArrowUp", "Home", " "].indexOf(ev.key) >= 0;
    }
    return false;
  }

  function onIntent(ev) {
    const el = scroller();
    if (!el) return;
    if (ev.type === "touchmove") state._lastTouchY = (ev.touches[0] || {}).clientY;
    if (isUpIntent(ev)) { setEnabled(false); return; }
    // 非上滚意图（下滑 / 按到底部）：已到底部才恢复跟随
    if (isAtBottom()) setEnabled(true);
  }

  function init() {
    state.btn = document.getElementById("chatFollowBtn");
    if (state.btn && !state.btn.dataset.bound) {
      state.btn.dataset.bound = "1";
      state.btn.addEventListener("click", () => {
        state.enabled = true;
        _render();
        // 先贴底再启用，保证当帧就到底
        const el = scroller();
        if (el) el.scrollTop = el.scrollHeight;
        pin();
      });
    }
    if (!state.scroller || !state.scroller.isConnected) scroller();
    const el = scroller();
    if (el && !el.dataset.followBound) {
      el.dataset.followBound = "1";
      ["wheel", "touchmove", "keydown"].forEach((t) => {
        el.addEventListener(t, onIntent, { passive: true });
      });
      // 拖动滚动条也会上滚：scroll 后不在底部即视为暂停（scrollTop 赋值会自我纠正）
      el.addEventListener("scroll", () => {
        if (isAtBottom()) setEnabled(true);
        else if (state.running) setEnabled(false);
      });
    }
    _render();
  }

  /* 任务开始/结束：开始时默认开启跟随（用户此前手动暂停过也重置 —— 新一轮该从头跟） */
  function setRunning(on) {
    state.running = !!on;
    if (on) { state.enabled = true; pin(); }
    _render();
  }

  return { init, pin, setRunning, isAtBottom, setEnabled };
})();