/**
 * 短线情绪观测页 —— 数据来自 /api/short-line/overview + /history
 * 设计：docs/superpowers/specs/2026-09-10-short-line-monitor-design.md
 *
 * 布局：12 栅格一屏（2560×1279 不整页滚动）；卡片 = 壳 + .sl-scroll 滚动体；
 * 可选块（降级横幅 / 历史曲线）通过 .sl-body 上的 has-banner / has-hist 切换行数。
 * 导出：临时把页面壳设为目标像素尺寸，用容器查询切布局分支后 html-to-image 出图。
 */
(function () {
  'use strict';

  const $page = () => document.getElementById('slPage');
  const $body = () => document.getElementById('slBody');

  // 导出目标尺寸：与用户的 2560×1279 显示器一屏对齐
  const EXPORT_W = 2560;
  const EXPORT_H = 1279;

  // 布局全部交给 CSS flexbox：卡片高度由行容器分配，滚动体吃满卡片剩余高度。
  // 这里**不做任何高度测量与回写** —— 用 JS 算高度会在字体/行高/缩放的边界上出偏差，
  // 表现为"有的卡撑破、有的卡空白"，是之前几版反复翻车的根因。
  // 需要调整某张卡占多少高度，改 CSS 里的 flex 权重（见 .sl-row-main / .sl-row-foot）。

  let _bound = false;
  let _histChart = null;
  let _exporting = false;

  function openPage() {
    const p = $page();
    if (!p) return;
    p.classList.add('open');
    const chat = document.querySelector('.chat-pane');
    if (chat) chat.style.display = 'none';
    bindEvents();
    load();
  }

  function closePage() {
    $page()?.classList.remove('open');
    const chat = document.querySelector('.chat-pane');
    if (chat) chat.style.display = '';
  }

  function bindEvents() {
    if (_bound) return;
    _bound = true;
    document.getElementById('slRefreshBtn')?.addEventListener('click', () => load());
    document.getElementById('slCopyBtn')?.addEventListener('click', (e) => exportImage(e.currentTarget, 'copy'));
    document.getElementById('slDownloadBtn')?.addEventListener('click', (e) => exportImage(e.currentTarget, 'download'));
  }

  async function load() {
    const body = $body();
    if (!body) return;
    body.className = 'sl-body';
    body.innerHTML = '<div class="sl-loading">加载中（首次聚合约 10 秒，同花顺限流节流）…</div>';
    try {
      const resp = await fetch('/api/short-line/overview');
      const payload = await resp.json();
      const data = payload.data || {};
      render(data);
      loadHistory();
    } catch (e) {
      body.innerHTML = `<div class="sl-empty">加载失败：${esc(e.message || e)}</div>`;
    }
  }

  async function loadHistory() {
    try {
      const resp = await fetch('/api/short-line/history?days=30');
      const payload = await resp.json();
      const items = (payload.data && payload.data.items) || [];
      if (items.length >= 2) renderHistory(items);
    } catch (e) { /* 历史曲线非关键路径 */ }
  }

  /* ── 渲染 ── */
  function render(data) {
    const body = $body();
    const s = data.summary;
    body.className = 'sl-body';
    if (!s) {
      body.innerHTML = `<div class="sl-empty">暂无数据${data.error ? '：' + esc(data.error) : '（可能为非交易日或同花顺 Key 未配置）'}</div>`;
      return;
    }
    const dateEl = document.getElementById('slDate');
    if (dateEl) dateEl.textContent = s.trade_date || '—';

    const degraded = (data.degraded || []);
    const staleHtml = data.stale
      ? `<div class="sl-degraded">⚠ 上游数据源暂不可达，展示最近快照（${esc(data.stale_date || '—')}）· 稍后刷新重试</div>`
      : '';
    const degradedHtml = (!data.stale && degraded.length)
      ? `<div class="sl-degraded">⚠ 部分数据源降级：${degraded.map(esc).join(' / ')}（面板其余部分正常）</div>` : '';
    if (staleHtml || degradedHtml) body.classList.add('has-banner');

    body.innerHTML = staleHtml + degradedHtml + `
      <div class="sl-card sl-card-gauge">
        <div class="sl-gauge-inner">
          <div class="sl-gauge-box">
            <div class="sl-gauge-arc">
              <svg width="150" height="84" viewBox="0 0 220 128">
                <path d="M 20 115 A 90 90 0 0 1 200 115" fill="none" stroke="#e5e7eb" stroke-width="18" stroke-linecap="round"/>
                <path id="slGaugeFill" d="M 20 115 A 90 90 0 0 1 200 115" fill="none" stroke="#e74c3c" stroke-width="18"
                      stroke-linecap="round" stroke-dasharray="283" stroke-dashoffset="283"/>
              </svg>
              <div class="sl-gauge-needle" id="slGaugeNeedle" style="transform: rotate(-90deg);"></div>
              <div class="sl-gauge-center"></div>
            </div>
            <div class="sl-gauge-meta">
              <h2>市场温度计 <span class="hint">高度25% + 晋级30% + 炸板30% + 涨跌停15%</span></h2>
              <div class="sl-gauge-score">
                <span class="num">${s.score}</span>
                <span class="lvl sl-lvl-${s.level}">${levelLabel(s.level)}</span>
              </div>
              <div class="sl-gauge-scale"><span>0 冰点</span><span>50</span><span>100 亢奋</span></div>
            </div>
          </div>
          <div class="sl-metric-grid">
            <div class="sl-metric-card"><span class="label">最高连板</span>
              <span class="value sl-up">${s.max_board} 板</span>
              <span class="sub">${esc(topBoardName(data.ladder) || '—')}</span></div>
            <div class="sl-metric-card"><span class="label">涨停 / 跌停</span>
              <span class="value"><span class="sl-up">${s.limit_up_count}</span> / <span class="sl-down">${s.limit_down_count}</span></span>
              <span class="sub">${s.limit_down_count <= 10 ? '跌停可控 ✓' : '跌停偏多 ⚠'}</span></div>
            <div class="sl-metric-card"><span class="label">炸板率</span>
              <span class="value ${s.break_rate > 0.35 ? 'sl-warn' : ''}">${(s.break_rate * 100).toFixed(1)}%</span>
              <span class="sub">炸板 ${s.broken_count} / 涨停 ${s.limit_up_count}</span></div>
            <div class="sl-metric-card"><span class="label">总晋级率</span>
              <span class="value ${s.promotion_rate >= 0.6 ? 'sl-up' : (s.promotion_rate < 0.4 ? 'sl-down' : 'sl-warn')}">${(s.promotion_rate * 100).toFixed(0)}%</span>
              <span class="sub">≥60% 强化 · &lt;40% 警惕</span></div>
            <div class="sl-metric-card"><span class="label">昨梯队溢价</span>
              <span class="value ${(s.yesterday_premium?.mean || 0) >= 0 ? 'sl-up' : 'sl-down'}">${fmtPct(s.yesterday_premium?.mean)}</span>
              <span class="sub">n=${s.yesterday_premium?.n || 0} · 中位 ${fmtPct(s.yesterday_premium?.median)}</span></div>
          </div>
        </div>
      </div>

      <div class="sl-row-main">
        <div class="sl-card sl-card-ladder">
          <h2>连板天梯 <span class="hint">按板数降序 · 卡片按题材着色 · 角标为次日晋级结果</span></h2>
          <div class="sl-ladder-wrap" id="slLadder"></div>
        </div>

        <div class="sl-stack">
          <div class="sl-card sl-card-first">
            <h2>首板墙 <span class="hint">梯子地基 · 今日首板全部标的</span></h2>
            <div class="sl-scroll" id="slFirstBoards"></div>
          </div>

          <div class="sl-card sl-card-themes">
            <h2>题材聚合 <span class="hint" id="slThemesHint">涨停原因按 "+" 切分</span></h2>
            <div class="sl-scroll" id="slThemes"></div>
          </div>
        </div>
      </div>

      <div class="sl-row-foot">
        <div class="sl-card sl-card-promo">
          <h2>晋级率矩阵 <span class="hint">昨日 k 板 → 今日 k+1 板</span></h2>
          <div class="sl-scroll" id="slPromoWrap">
            <div class="sl-promo-grid" id="slPromo"></div>
            <div class="sl-promo-hint" id="slPromoHint"></div>
          </div>
          <div class="sl-subhead">封板质量 &amp; 分歧区 <span class="hint" id="slQualityHint">封单额 / 炸板次数 / 跌停时间</span></div>
          <div class="sl-scroll" id="slQualityHost">
            <div class="sl-quality-wrap" id="slQuality"></div>
          </div>
        </div>
      </div>

      <div class="sl-card sl-card-hist" id="slHistCard" style="display:none;">
        <h2>情绪温度历史 <span class="hint">近 30 交易日（每次访问自动固化当日快照）</span></h2>
        <div class="sl-hist-wrap"><canvas id="slHistChart"></canvas></div>
      </div>`;

    renderGauge(s.score);
    renderLadder(data.ladder);
    renderFirstBoards(data.first_boards);
    renderThemes(data.themes);
    renderPromo(data.promo);
    renderQuality(data.quality);
    // 布局交给 CSS：这里不需要任何适配计算
  }

  function renderGauge(score) {
    const needle = document.getElementById('slGaugeNeedle');
    const fill = document.getElementById('slGaugeFill');
    if (!needle || !fill) return;
    // 表盘 -90°(0分) → +90°(100分)
    const deg = -90 + (Math.max(0, Math.min(100, score)) / 100) * 180;
    requestAnimationFrame(() => { needle.style.transform = `rotate(${deg}deg)`; });
    const total = 283; // 半圆弧长
    fill.style.strokeDashoffset = String(total * (1 - score / 100));
    fill.style.stroke = score >= 70 ? '#e74c3c' : (score >= 50 ? '#f59e0b' : '#16a34a');
  }

  function topBoardName(ladder) {
    const boards = (ladder && ladder[0] && ladder[0].boards) || [];
    if (!boards.length) return '';
    const max = Math.max(...boards.map(b => b.board_num || 0));
    const top = boards.filter(b => b.board_num === max).slice(0, 2).map(b => b.name).join('、');
    return `${top} · ${(boards.find(b => b.board_num === max) || {}).theme || ''}`;
  }

  function renderLadder(ladder) {
    const el = document.getElementById('slLadder');
    if (!el) return;
    const boards = (ladder && ladder[0] && ladder[0].boards) || [];
    if (!boards.length) { el.innerHTML = '<div class="sl-empty" style="grid-column:1/-1;">今日暂无连板梯队（或非交易日）</div>'; return; }
    const byNum = {};
    for (const b of boards) (byNum[b.board_num] = byNum[b.board_num] || []).push(b);
    const nums = Object.keys(byNum).map(Number).sort((a, b) => b - a);

    el.innerHTML = nums.map(n => {
      const color = n >= 5 ? '#e74c3c' : (n >= 3 ? '#f59e0b' : '#176b5b');
      const cards = byNum[n].map(s => `
        <div class="sl-stock-card" style="--theme-color:${themeColor(s.theme)}">
          <div class="nm"><span>${esc(s.name)}</span><span class="code">${esc(s.code)}</span></div>
          <div class="meta">
            ${s.seal_amount ? `<span>封 ${fmtYi(s.seal_amount)}</span>` : ''}
            ${s.first_time ? `<span>首封 ${esc(s.first_time)}</span>` : ''}
            ${s.seal_nextday === true ? '<span class="sl-next-tag sl-next-seal">次日续板</span>' : ''}
            ${s.seal_nextday === false ? '<span class="sl-next-tag sl-next-break">次日断板</span>' : ''}
          </div>
          ${s.theme ? `<span class="theme">${esc(s.theme)}</span>` : ''}
        </div>`).join('');
      // 列壳内含独立滚动体：长列不撑破行、短列不留死白
      return `<div class="sl-ladder-col">
        <div class="sl-col-head" style="background:${color};">${n} 连板 · ${byNum[n].length} 只</div>
        <div class="sl-scroll">${cards}</div></div>`;
    }).join('');
  }

  function renderFirstBoards(list) {
    const el = document.getElementById('slFirstBoards');
    if (!el) return;
    const fb = list || [];
    if (!fb.length) { el.innerHTML = '<div class="sl-empty">暂无首板</div>'; return; }
    el.innerHTML = `<div class="sl-fb-grid">${fb.map(s => `
      <div class="sl-fb-card" style="--theme-color:${themeColor(s.theme)}">
        <div class="nm">${esc(s.name)}</div>
        <span class="t">${esc(s.theme || '')}</span>
      </div>`).join('')}</div>`;
  }

  function renderThemes(themes) {
    const el = document.getElementById('slThemes');
    if (!el) return;
    const total = (themes || []).length;
    const hint = document.getElementById('slThemesHint');
    if (!total) {
      if (hint) hint.textContent = '涨停原因按 "+" 切分';
      el.innerHTML = '<div class="sl-empty">暂无题材数据</div>';
      return;
    }
    // 全量渲染：不再 slice 截断，超出部分由卡片内细滚动条浏览
    if (hint) hint.textContent = `共 ${total} 个题材 · 按热度降序 · 涨停原因按 "+" 切分`;
    const maxCount = Math.max(...themes.map(t => t.count));
    el.innerHTML = themes.map(t => {
      const color = themeColor(t.name);
      const segs = `<div class="seg" style="width:${Math.max(t.count / maxCount * 100, 3)}%;background:${color};"></div>`;
      const status = t.ladder_ok
        ? (t.max_board >= 3 ? '<span class="sl-pill sl-pill-yes">主线</span>' : '<span class="sl-pill sl-pill-probe">发酵</span>')
        : '<span class="sl-pill sl-pill-fade">试探</span>';
      return `<div class="sl-theme-row">
        <span class="sl-theme-name"><span class="sl-theme-dot" style="background:${color};"></span>${esc(t.name)} ${status}</span>
        <div class="sl-theme-bar-wrap"><div class="sl-theme-bar">${segs}</div></div>
        <span class="sl-theme-stats">高度 <b>${t.max_board}板</b> · 广度 <b>${t.count}</b> · 梯队${t.ladder_ok ? '<span class="sl-up">完整</span>' : '断层'}</span>
      </div>`;
    }).join('');
  }

  function renderPromo(promo) {
    const el = document.getElementById('slPromo');
    if (!el) return;
    if (!promo || !promo.length) { el.innerHTML = '<div class="sl-empty">暂无晋级数据（需 ≥2 个交易日天梯）</div>'; return; }
    el.innerHTML = promo.map(p => {
      const cls = p.rate >= 0.6 ? 'sl-p-good' : (p.rate < 0.4 ? 'sl-p-bad' : 'sl-p-mid');
      return `<div class="sl-promo-cell ${cls}">
        <div class="node">${esc(p.node)}</div>
        <div class="pct">${(p.rate * 100).toFixed(0)}%</div>
        <div class="ratio">${p.hit}/${p.base}</div></div>`;
    }).join('');
    const best = promo.filter(p => p.base >= 2).sort((a, b) => b.rate - a.rate)[0];
    const hint = document.getElementById('slPromoHint');
    if (hint && best) hint.innerHTML = `核心节点：<b>${esc(best.node)}</b>（${(best.rate * 100).toFixed(0)}%，${best.hit}/${best.base}）`;
  }

  function renderQuality(q) {
    const el = document.getElementById('slQuality');
    if (!el) return;
    const rows = [];
    // 全量渲染：不再 slice(6/5/4) 丢弃数据，超出部分由卡片内细滚动条浏览
    (q?.top_seal || []).forEach(x => rows.push(
      `<div class="sl-q-row"><span>${esc(x.name)} <span class="r mono">${esc(x.code)}</span></span>
       <span class="r">封 ${fmtYi(x.seal_amount)} · 首封 ${esc(x.first_time || '—')}</span></div>`));
    (q?.broken || []).forEach(x => rows.push(
      `<div class="sl-q-row"><span>${esc(x.name)} <span class="r mono">${esc(x.code)}</span>
        <span class="sl-tag sl-tag-broken">炸板</span></span>
       <span class="r">开板 ${x.open_times ?? '—'} 次</span></div>`));
    (q?.down || []).forEach(x => rows.push(
      `<div class="sl-q-row"><span>${esc(x.name)} <span class="r mono">${esc(x.code)}</span>
        <span class="sl-tag sl-tag-down">跌停</span></span>
       <span class="r">${esc(x.last_limit_time || '—')}</span></div>`));
    const qHint = document.getElementById('slQualityHint');
    if (qHint) {
      qHint.textContent = rows.length
        ? `封单额 / 炸板次数 / 跌停时间 · 共 ${rows.length} 条（封单 ${(q?.top_seal || []).length} · 炸板 ${(q?.broken || []).length} · 跌停 ${(q?.down || []).length}）`
        : '封单额 / 炸板次数 / 跌停时间';
    }
    el.innerHTML = rows.length ? rows.join('') : '<div class="sl-empty">暂无数据</div>';
  }

  function renderHistory(items) {
    const card = document.getElementById('slHistCard');
    const canvas = document.getElementById('slHistChart');
    if (!card || !canvas || !window.Chart) return;
    card.style.display = '';
    $body()?.classList.add('has-hist');
    if (_histChart) { _histChart.destroy(); _histChart = null; }
    const labels = items.map(x => String(x.trade_date).slice(5));
    const data = items.map(x => x.score);
    _histChart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { labels, datasets: [{
        label: '情绪温度', data, borderColor: 'rgba(231,76,60,.85)',
        backgroundColor: 'rgba(231,76,60,.08)', fill: true, tension: .35,
        pointRadius: 3, pointHoverRadius: 5,
      }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          y: { min: 0, max: 100, ticks: { font: { size: 10 } }, grid: { color: '#f0f0f0' } },
          x: { ticks: { font: { size: 10 }, maxTicksLimit: 14 }, grid: { display: false } },
        },
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: c => `温度 ${c.raw} 分` } } },
        animation: { duration: 400 },
      },
    });
  }

  /* ── 渲染整页为 PNG Blob（固定 2560×1279，一屏全貌） ──
   * 要点：
   * 1) 临时把页面壳设为目标像素尺寸 → 容器查询按 2560 宽走桌面分支（媒体查询做不到）；
   * 2) 导出期间隐藏滚动条与操作按钮（.sl-exporting），并放开所有滚动体限高；
   * 3) 等两帧 rAF 让重排落地再截，finally 里还原一切。
   */
  async function renderPngBlob() {
    const page = $page();
    if (!page) throw new Error('页面未打开');
    const prev = { w: page.style.width, h: page.style.height };
    try {
      await ensureHtmlToImage();
      page.classList.add('sl-exporting');
      page.style.width = EXPORT_W + 'px';
      page.style.height = EXPORT_H + 'px';
      await nextFrames(2);

      const blob = await htmlToImage.toBlob(page, {
        width: EXPORT_W,
        height: EXPORT_H,
        pixelRatio: 1,
        backgroundColor: '#ffffff',
        cacheBust: true,
        filter: (n) => !(n.id === 'slToast') && !(n.classList && n.classList.contains('sl-toast')),
      });
      if (!blob) throw new Error('图片生成失败');
      return blob;
    } finally {
      page.classList.remove('sl-exporting');
      page.style.width = prev.w;
      page.style.height = prev.h;
    }
  }

  /* ── 导出入口：mode = 'copy' 复制到剪贴板 / 'download' 下载到本地 ──
   * 复制必须在**点击手势内**就发起 clipboard.write（长耗时渲染后再 write 会因手势失效被拒），
   * 所以先把 renderPngBlob() 的 promise 交给 ClipboardItem，写入与渲染并行。 */
  function exportImage(btn, mode) {
    if (_exporting) return;
    _exporting = true;
    const label = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = mode === 'copy' ? '复制中…' : '导出中…'; }

    const pngPromise = renderPngBlob();
    const filename = `短线情绪观测_${(document.getElementById('slDate')?.textContent || '').trim() || 'today'}.png`;

    const done = (msg, ms) => { if (btn) { btn.disabled = false; btn.textContent = label; } _exporting = false; toast(msg, ms); };
    const fail = (e) => {
      console.warn('[shortLine] 导出失败:', e);
      if (btn) { btn.disabled = false; btn.textContent = label; }
      _exporting = false;
      toast('导出失败：' + (e?.message || e), 2600);
    };

    if (mode === 'copy') {
      toast('正在生成图片…', 0);
      const canClip = navigator.clipboard && navigator.clipboard.write && typeof ClipboardItem !== 'undefined';
      if (canClip) {
        // 关键：立刻在手势内构造 ClipboardItem（blob 位置传 Promise）
        navigator.clipboard.write([new ClipboardItem({ 'image/png': pngPromise })])
          .then(() => done('已复制到剪贴板', 1600))
          .catch((err) => {
            console.warn('[shortLine] 剪贴板写入失败，降级为下载:', err);
            return pngPromise.then(b => { downloadBlob(b, filename); done('剪贴板不可用，已改为下载', 2200); });
          });
        return;
      }
      // 无剪贴板能力：降级为下载
      pngPromise.then(b => { downloadBlob(b, filename); done('当前环境不支持复制，已下载 PNG', 2200); }).catch(fail);
      return;
    }

    toast('正在生成图片…', 0);
    pngPromise
      .then(b => { downloadBlob(b, filename); done(`已下载 PNG（${EXPORT_W}×${EXPORT_H}）`, 1800); })
      .catch(fail);
  }

  async function ensureHtmlToImage() {
    if (typeof htmlToImage !== 'undefined' && htmlToImage.toBlob) return;
    const loader = window.StockRadar?.utils?.loadScriptOnce;
    if (!loader) throw new Error('导出依赖未就绪');
    await loader('/static/lib/html-to-image.js?v=1', 'htmlToImage');
    if (typeof htmlToImage === 'undefined' || !htmlToImage.toBlob) throw new Error('html-to-image 未加载');
  }

  function nextFrames(n) {
    return new Promise((resolve) => {
      let left = Math.max(1, n);
      const tick = () => (--left <= 0 ? resolve() : requestAnimationFrame(tick));
      requestAnimationFrame(tick);
    });
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  let _toastTimer = null;
  function toast(msg, ms) {
    let el = document.getElementById('slToast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'slToast';
      el.className = 'sl-toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('on');
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = ms ? setTimeout(() => el.classList.remove('on'), ms) : null;
  }

  /* ── 工具 ── */
  const THEME_COLORS = ['#e74c3c', '#8b5cf6', '#f59e0b', '#0ea5e9', '#10b981', '#ec4899', '#64748b', '#84cc16'];
  const _themeColorCache = {};
  function themeColor(name) {
    if (!name) return '#176b5b';
    if (_themeColorCache[name]) return _themeColorCache[name];
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return (_themeColorCache[name] = THEME_COLORS[h % THEME_COLORS.length]);
  }
  function esc(s) { return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function fmtYi(v) { return v == null ? '—' : (Number(v) / 1e8).toFixed(1) + '亿'; }
  function fmtPct(v) { return v == null ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%'; }
  function levelLabel(lv) {
    return { ice: '冰点 · 空仓观望', ebb: '退潮 · 只做最高板', warm: '回暖 · 做二进三', hot: '亢奋 · 梯队接力' }[lv] || lv;
  }

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.shortLine = { openPage, closePage, load, exportImage, renderPngBlob };
})();
