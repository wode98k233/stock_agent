// 深度报告卡片：对话内展示 + 状态轮询 + 打开阅读页
// 约定：消息正文里的哨兵块 @@DEEP_REPORT_START@@{json}@@DEEP_REPORT_END@@，
// 由 chat-core.js 解析后交给本模块渲染；卡片自身按 rid 拉快照（不在消息里塞状态）。
//
// 生命周期：
// - rid 缺失/非法 → 不渲染；
// - 3s 轮询（tick 用「单飞」防重叠 + AbortController 超时），终态自动停；
// - 连续 5 次 404 或 30 分钟仍未终态 → 停并显示「状态未知」；
// - 卡片被移出 DOM（切对话 / 清空消息列表）时由 MutationObserver 清理定时器；
// - 对外提供 window.StockRadar.deepReportCard.destroy(el)。
(function () {
  const POLL_MS = 3000;
  const MAX_404 = 5;                       // 连续 404 上限（≈15s）
  const MAX_RUN_MS = 30 * 60 * 1000;       // 超过 30 分钟仍未终态就停止轮询（10 节报告约 12–15 分钟）
  const FETCH_TIMEOUT_MS = 5000;           // 单次快照请求超时，防止挂死的请求叠住轮询
  const READER_TIMEOUT_MS = 4000;          // 阅读页预检超时
  const TERMINAL = new Set(['done', 'partial', 'failed', 'cancelled']);
  const HAS_READER = new Set(['done', 'partial']);
  const RID_RE = /^[A-Za-z0-9_-]{2,64}$/;  // 与服务端 routes/deep_reports.py::_RID_RE 对齐
  const UNKNOWN_HINT = '状态未知，点阅读页查看';
  const STATUS_LABEL = {
    done: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消',
    outlining: '大纲中', writing: '写作中',
  };

  const live = new WeakMap();   // cardEl -> { el, rid, timer, notFound, dead, startedAt, fetching, status }
  let observer = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function badge(status) {
    const map = {
      done: ['done', '已完成'], partial: ['warn', '部分完成'],
      failed: ['err', '失败'], cancelled: ['warn', '已取消'],
    };
    const [cls, text] = map[status] || ['run', '生成中'];
    return `<span class="dr-badge ${cls}">${text}</span>`;
  }

  function setHint(el, text, isErr) {
    const hint = el.querySelector('.dr-hint');
    if (!hint) return;
    hint.textContent = text;
    if (isErr) hint.classList.add('err');
  }

  function stopPolling(card) {
    if (!card || card.dead) return;
    card.dead = true;
    if (card.timer) {
      clearInterval(card.timer);
      card.timer = null;
    }
    live.delete(card.el);
    card.el.dataset.drStopped = '1';
  }

  function destroy(el) {
    if (!el) return;
    const card = live.get(el);
    if (card) stopPolling(card);
  }

  // 卡片被 app.js 的 innerHTML="" / 切对话移除后，DOM 里没有引用能触发清理，
  // 用全局 MutationObserver 兜底（只挂一次，观察 childList，不碰属性/文本）。
  function ensureObserver() {
    if (observer || typeof MutationObserver === 'undefined') return;
    const root = document.body || document.documentElement;
    if (!root) return;
    observer = new MutationObserver(records => {
      records.forEach(rec => rec.removedNodes.forEach(node => {
        if (!node || node.nodeType !== 1) return;
        const self = live.get(node);
        if (self) { stopPolling(self); return; }
        if (node.querySelectorAll) {
          node.querySelectorAll('.dr-chat-card').forEach(c => {
            const card = live.get(c);
            if (card) stopPolling(card);
          });
        }
      }));
    });
    observer.observe(root, { childList: true, subtree: true });
  }

  async function fetchWithTimeout(url, ms, options) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms);
    try {
      return await fetch(url, Object.assign({ signal: ctrl.signal, cache: 'no-store' }, options || {}));
    } finally {
      clearTimeout(timer);
    }
  }

  async function refresh(card) {
    const el = card.el;
    if (card.dead) return null;
    if (Date.now() - card.startedAt > MAX_RUN_MS) {
      setHint(el, UNKNOWN_HINT, true);
      stopPolling(card);
      return null;
    }
    let resp;
    try {
      resp = await fetchWithTimeout(`/api/deep-reports/${encodeURIComponent(card.rid)}`, FETCH_TIMEOUT_MS);
    } catch (e) {
      return null;   // 网络抖动/超时：不累计 404，下一轮再试
    }
    if (card.dead) return null;
    if (resp.status === 404) {
      card.notFound += 1;
      if (card.notFound >= MAX_404) {
        setHint(el, UNKNOWN_HINT, true);
        stopPolling(card);
      }
      return null;
    }
    card.notFound = 0;
    if (!resp.ok) return null;
    let body = null;
    try { body = await resp.json(); } catch (e) { return null; }
    const d = body && body.data;
    if (!d) return null;
    card.status = d.status || '';

    const title = el.querySelector('.dr-title');
    const meta = el.querySelector('.dr-meta');
    const bd = el.querySelector('.dr-badge');
    if (title && d.title) title.textContent = d.title;
    if (meta) {
      const parts = [
        d.status === 'done' || d.status === 'partial'
          ? `${d.words || 0} 字 · ${d.sections_done || 0}/${d.sections_total || 0} 节`
          : `进度 ${d.progress || 0}% · ${d.sections_done || 0}/${d.sections_total || 0} 节`,
        d.charts ? `${d.charts} 图` : '',
      ].filter(Boolean);
      meta.textContent = parts.join(' · ');
    }
    if (bd) bd.outerHTML = badge(d.status);

    if (TERMINAL.has(d.status)) {
      const hints = {
        done: '已完成 · 点击打开阅读页',
        partial: '部分完成 · 点击打开阅读页',
        failed: '生成失败 · 点阅读页查看',
        cancelled: '已取消 · 点阅读页查看',
      };
      setHint(el, hints[d.status] || '已结束', d.status === 'failed' || d.status === 'cancelled');
      stopPolling(card);
    }
    return d;
  }

  // 单飞：上一次快照还没回来就跳过这一拍，避免慢请求叠住轮询
  async function runRefresh(card) {
    if (!card || card.dead || card.fetching) return null;
    card.fetching = true;
    try {
      return await refresh(card);
    } finally {
      card.fetching = false;
    }
  }

  // 阅读页预检：部分完成/中断的报告没有 report.html，直接开窗会看到 404 JSON。
  // 用 GET 预检（FastAPI 的 GET 路由不会自动挂 HEAD），只读状态，body 立即 cancel。
  async function readerAvailable(url) {
    try {
      const resp = await fetchWithTimeout(url, READER_TIMEOUT_MS);
      if (!resp.ok) return false;
      try { if (resp.body) await resp.body.cancel(); } catch (e) { /* ignore */ }
      return true;
    } catch (e) {
      return false;
    }
  }

  function openReader(card) {
    const el = card.el;
    const readerUrl = `/api/deep-reports/${encodeURIComponent(card.rid)}/html`;
    // 已知未完成的报告：不请求、不打开，直接明确提示
    if (card.status && !HAS_READER.has(card.status)) {
      setHint(el, `阅读页未生成（${STATUS_LABEL[card.status] || card.status}），请稍后重试`, true);
      return;
    }
    // 先同步开窗（保住用户手势，避免弹窗拦截），预检失败再关掉
    const win = window.open('', '_blank');
    if (!win) {
      setHint(el, '浏览器拦截了新窗口，请允许弹窗后重试', true);
      return;
    }
    readerAvailable(readerUrl).then(ok => {
      if (ok) {
        win.location.href = readerUrl;
        return;
      }
      try { win.close(); } catch (e) { /* ignore */ }
      setHint(el, '阅读页未生成（报告未完成或已中断），请稍后重试', true);
    });
  }

  function render(payload) {
    const rid = payload && payload.rid;
    // 哨兵块来自消息正文，可能被截断/手改；rid 非法就不渲染，避免拿坏 rid 反复打 404
    if (typeof rid !== 'string' || !RID_RE.test(rid.trim())) {
      console.warn('[DeepReport] 卡片 rid 缺失或非法，跳过渲染:', payload);
      return null;
    }
    const cleanRid = rid.trim();
    const el = document.createElement('div');
    el.className = 'dr-chat-card';
    el.dataset.drRid = cleanRid;
    el.innerHTML = `
      <div class="dr-head">
        <span class="dr-badge run">生成中</span>
        <span class="dr-title">${esc(payload.universe || '深度报告')}</span>
        <span class="dr-meta"></span>
      </div>
      <div class="dr-actions">
        <button class="dr-btn primary" type="button">打开阅读页</button>
        <a class="dr-btn" target="_blank" rel="noopener">下载 .md</a>
        <a class="dr-btn" target="_blank" rel="noopener">导出 PDF</a>
        <span class="dr-hint">约 10–15 分钟，可离开本页</span>
      </div>`;

    const card = {
      el, rid: cleanRid, timer: null, notFound: 0, dead: false, fetching: false,
      startedAt: Date.now(), status: '',
    };
    live.set(el, card);
    el.drDestroy = () => destroy(el);
    el.querySelector('.dr-btn.primary').onclick = () => openReader(card);
    const dl = el.querySelectorAll('a.dr-btn');
    if (dl[0]) dl[0].href = `/api/deep-reports/${encodeURIComponent(cleanRid)}/markdown`;
    if (dl[1]) dl[1].href = `/api/deep-reports/${encodeURIComponent(cleanRid)}/pdf`;
    ensureObserver();

    const tick = () => {
      if (card.dead) return;
      if (!el.isConnected) {   // observer 没覆盖到的极端场景（整棵 body 被替换）兜底
        stopPolling(card);
        return;
      }
      runRefresh(card);
    };
    // 先拉一次再起定时器：首次就终态/404 时不留空转 interval
    runRefresh(card).finally(() => {
      if (!card.dead && !card.timer) card.timer = setInterval(tick, POLL_MS);
    });
    return el;
  }

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.deepReportCard = { render, destroy };
})();
