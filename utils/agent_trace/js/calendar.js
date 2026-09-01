/* ── trace calendar ── */

const TraceCalendar = (() => {
  let overlayEl, modalEl, bodyEl, yearTitleEl, monthTitleEl;
  let year, month;
  let calData = null;
  let popoverEl = null;
  let onDocClick = null;

  function init() {
    overlayEl = document.getElementById('traceCalOverlay');
    modalEl = document.getElementById('traceCalModal');
    bodyEl = document.getElementById('traceCalBody');
    yearTitleEl = document.getElementById('traceCalYear');
    monthTitleEl = document.getElementById('traceCalMonth');

    const now = new Date();
    year = now.getFullYear();
    month = now.getMonth() + 1;

    document.getElementById('traceCalBtn').onclick = open;
    document.getElementById('traceCalClose').onclick = close;
    overlayEl.onclick = close;
    document.getElementById('traceCalPrevYear').onclick = () => navigate(-1, 0);
    document.getElementById('traceCalNextYear').onclick = () => navigate(1, 0);
    document.getElementById('traceCalPrevMonth').onclick = () => navigate(0, -1);
    document.getElementById('traceCalNextMonth').onclick = () => navigate(0, 1);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && modalEl.classList.contains('open')) close();
    });
  }

  function open() {
    const now = new Date();
    year = now.getFullYear();
    month = now.getMonth() + 1;
    overlayEl.classList.add('open');
    modalEl.classList.add('open');
    loadMonth();
  }

  function close() {
    overlayEl.classList.remove('open');
    modalEl.classList.remove('open');
    hidePopover();
  }

  function navigate(dy, dm) {
    month += dm;
    year += dy;
    if (month < 1) { month = 12; year--; }
    if (month > 12) { month = 1; year++; }
    loadMonth();
  }

  async function loadMonth() {
    yearTitleEl.textContent = year + '年';
    monthTitleEl.textContent = month + '月';
    bodyEl.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">加载中...</div>';

    if (!S.apiBase) {
      bodyEl.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted)">仅 API 模式可用</div>';
      return;
    }

    try {
      const res = await fetch(`${S.apiBase}/api/calendar?year=${year}&month=${month}`);
      calData = await res.json();
    } catch (e) {
      calData = { run_days: {} };
    }
    renderCalendar();
  }

  function renderCalendar() {
    hidePopover();
    bodyEl.innerHTML = '';

    const runDays = calData.run_days || {};

    // 月度统计
    let totalRuns = 0, okRuns = 0, errRuns = 0;
    let totalIn = 0, totalOut = 0, totalTok = 0, totalCached = 0, totalReasoning = 0;
    let allDurations = [];
    for (const [day, runs] of Object.entries(runDays)) {
      for (const r of runs) {
        totalRuns++;
        if (r.status === 'success') okRuns++;
        if (r.status === 'error') errRuns++;
        totalIn += r.input_tokens || 0;
        totalOut += r.output_tokens || 0;
        totalTok += r.total_tokens || 0;
        totalCached += r.cached_tokens || 0;
        totalReasoning += r.reasoning_tokens || 0;
        if (r.duration_ms) allDurations.push(r.duration_ms);
      }
    }
    const avgDur = allDurations.length ? allDurations.reduce((a, b) => a + b, 0) / allDurations.length : 0;
    const cacheHit = totalIn > 0 ? (totalCached / totalIn * 100).toFixed(0) : 0;

    // 摘要栏
    const fmt = (n) => n > 0 ? fmtCompact(n) : '—';
    const fmtD = (ms) => ms > 0 ? fmtDur(ms) : '—';
    const totalDurMs = allDurations.reduce((a, b) => a + b, 0);

    const summary = document.createElement('div');
    summary.className = 'trace-cal-summary';
    summary.innerHTML = `
      <div class="trace-cal-stat"><div class="trace-cal-stat-label">Runs</div><div class="trace-cal-stat-value accent">${totalRuns}</div></div>
      <div class="trace-cal-stat"><div class="trace-cal-stat-label">成功</div><div class="trace-cal-stat-value success">${okRuns}</div></div>
      <div class="trace-cal-stat"><div class="trace-cal-stat-label">失败</div><div class="trace-cal-stat-value${errRuns > 0 ? ' error' : ''}">${errRuns}</div></div>
      <div class="trace-cal-stat"><div class="trace-cal-stat-label">Token 总计</div><div class="trace-cal-stat-value">${fmt(totalTok)}</div></div>
      <div class="trace-cal-stat"><div class="trace-cal-stat-label">输入</div><div class="trace-cal-stat-value success">${fmt(totalIn)}</div></div>
      <div class="trace-cal-stat"><div class="trace-cal-stat-label">输出</div><div class="trace-cal-stat-value purple">${fmt(totalOut)}</div></div>
      ${totalCached ? `<div class="trace-cal-stat"><div class="trace-cal-stat-label">缓存命中</div><div class="trace-cal-stat-value" style="color:#10b981">${fmt(totalCached)} (${cacheHit}%)</div></div>` : ""}
      ${totalReasoning ? `<div class="trace-cal-stat"><div class="trace-cal-stat-label">思考</div><div class="trace-cal-stat-value" style="color:#8b5cf6">${fmt(totalReasoning)}</div></div>` : ""}
      <div class="trace-cal-stat"><div class="trace-cal-stat-label">总耗时</div><div class="trace-cal-stat-value">${fmtD(totalDurMs)}</div></div>
      <div class="trace-cal-stat"><div class="trace-cal-stat-label">平均耗时</div><div class="trace-cal-stat-value">${fmtD(avgDur)}</div></div>
    `;
    bodyEl.appendChild(summary);

    // 星期头
    const weekdays = document.createElement('div');
    weekdays.className = 'trace-cal-weekdays';
    ['一', '二', '三', '四', '五', '六', '日'].forEach((d, i) => {
      const el = document.createElement('div');
      el.className = 'trace-cal-weekday';
      if (i >= 5) el.style.color = 'var(--error)';
      el.textContent = d;
      weekdays.appendChild(el);
    });
    bodyEl.appendChild(weekdays);

    // 网格
    const grid = document.createElement('div');
    grid.className = 'trace-cal-grid';

    const today = new Date();
    const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

    const firstDay = new Date(year, month - 1, 1);
    const lastDay = new Date(year, month, 0);
    const startOffset = (firstDay.getDay() + 6) % 7;

    // 上月补位
    const prevLast = new Date(year, month - 1, 0);
    for (let i = startOffset - 1; i >= 0; i--) {
      const d = prevLast.getDate() - i;
      const cell = createDayCell(d, true, false, [], null);
      grid.appendChild(cell);
    }

    // 本月日期
    for (let d = 1; d <= lastDay.getDate(); d++) {
      const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      const runs = runDays[dateStr] || [];
      const isToday = dateStr === todayStr;
      const cell = createDayCell(d, false, isToday, runs, dateStr);
      grid.appendChild(cell);
    }

    // 下月补位
    const totalCells = startOffset + lastDay.getDate();
    const remainder = totalCells % 7;
    if (remainder > 0) {
      for (let d = 1; d <= 7 - remainder; d++) {
        const cell = createDayCell(d, true, false, [], null);
        grid.appendChild(cell);
      }
    }

    bodyEl.appendChild(grid);
  }

  function createDayCell(num, otherMonth, isToday, runs, dateStr) {
    const cell = document.createElement('div');
    let cls = 'trace-cal-day';
    if (otherMonth) cls += ' other-month';
    if (isToday) cls += ' today';
    if (runs.length > 0) cls += ' has-runs';
    cell.className = cls;

    const numEl = document.createElement('span');
    numEl.className = 'trace-cal-day-num';
    numEl.textContent = num;
    cell.appendChild(numEl);

    if (runs.length > 0 && !otherMonth) {
      // 状态点
      const dots = document.createElement('div');
      dots.className = 'trace-cal-day-dots';
      const hasOk = runs.some(r => r.status === 'success');
      const hasErr = runs.some(r => r.status === 'error');
      if (hasOk) {
        const dot = document.createElement('span');
        dot.className = 'trace-cal-day-dot success';
        dots.appendChild(dot);
      }
      if (hasErr) {
        const dot = document.createElement('span');
        dot.className = 'trace-cal-day-dot error';
        dots.appendChild(dot);
      }
      cell.appendChild(dots);

      // token 摘要
      let dayTotal = 0;
      runs.forEach(r => dayTotal += r.total_tokens || 0);
      if (dayTotal > 0) {
        const tokEl = document.createElement('span');
        tokEl.className = 'trace-cal-day-token';
        tokEl.textContent = fmtCompact(dayTotal);
        cell.appendChild(tokEl);
      }

      cell.addEventListener('click', (e) => {
        e.stopPropagation();
        showPopover(cell, dateStr, runs);
      });
    }

    return cell;
  }

  function showPopover(anchor, dateStr, runs) {
    hidePopover();

    const pop = document.createElement('div');
    pop.className = 'trace-cal-popover';

    const dateLabel = document.createElement('div');
    dateLabel.className = 'trace-cal-popover-date';
    const [y, m, d] = dateStr.split('-');
    dateLabel.textContent = `${parseInt(m)}月${parseInt(d)}日  ·  ${runs.length} runs`;
    pop.appendChild(dateLabel);

    const sorted = [...runs].sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
    sorted.forEach(run => {
      const item = document.createElement('div');
      item.className = 'trace-cal-popover-item';

      const icon = document.createElement('span');
      icon.className = 'trace-cal-popover-icon';
      icon.textContent = { success: '✅', error: '❌', running: '⏳' }[run.status] || '❓';
      item.appendChild(icon);

      const info = document.createElement('div');
      info.className = 'trace-cal-popover-info';

      const agentLine = document.createElement('div');
      agentLine.className = 'trace-cal-popover-agent';
      agentLine.textContent = run.agent_name || 'unknown';
      const badge = document.createElement('span');
      badge.className = `badge badge-${run.status}`;
      badge.textContent = run.status;
      agentLine.appendChild(badge);
      info.appendChild(agentLine);

      const timeLine = document.createElement('div');
      timeLine.className = 'trace-cal-popover-time';
      timeLine.textContent = (run.id || '').slice(0, 8) + '  ' + fmtTime(run.created_at);
      info.appendChild(timeLine);

      // token 统计 — 完整多维度
      if (run.total_tokens || run.duration_ms) {
        const tokLine = document.createElement('div');
        tokLine.className = 'trace-cal-popover-tokens';
        if (run.total_tokens) {
          const t = document.createElement('span');
          t.className = 'trace-cal-popover-tok total';
          t.textContent = `总 ${fmtCompact(run.total_tokens)}`;
          tokLine.appendChild(t);
        }
        if (run.input_tokens) {
          const t = document.createElement('span');
          t.className = 'trace-cal-popover-tok in';
          t.textContent = `入 ${fmtCompact(run.input_tokens)}`;
          tokLine.appendChild(t);
        }
        if (run.output_tokens) {
          const t = document.createElement('span');
          t.className = 'trace-cal-popover-tok out';
          t.textContent = `出 ${fmtCompact(run.output_tokens)}`;
          tokLine.appendChild(t);
        }
        if (run.cached_tokens) {
          const t = document.createElement('span');
          t.className = 'trace-cal-popover-tok';
          t.style.color = '#10b981';
          const hitRate = run.input_tokens > 0 ? (run.cached_tokens / run.input_tokens * 100).toFixed(0) : 0;
          t.textContent = `缓 ${fmtCompact(run.cached_tokens)} (${hitRate}%)`;
          tokLine.appendChild(t);
        }
        if (run.reasoning_tokens) {
          const t = document.createElement('span');
          t.className = 'trace-cal-popover-tok';
          t.style.color = '#8b5cf6';
          t.textContent = `思 ${fmtCompact(run.reasoning_tokens)}`;
          tokLine.appendChild(t);
        }
        if (run.duration_ms) {
          const t = document.createElement('span');
          t.className = 'trace-cal-popover-tok dur';
          t.textContent = `耗时 ${fmtDur(run.duration_ms)}`;
          tokLine.appendChild(t);
        }
        info.appendChild(tokLine);
      }

      item.appendChild(info);
      item.addEventListener('click', () => {
        hidePopover();
        close();
        selectRun(run.id);
      });

      pop.appendChild(item);
    });

    document.body.appendChild(pop);
    popoverEl = pop;

    // 定位
    const rect = anchor.getBoundingClientRect();
    const popW = pop.offsetWidth;
    const popH = pop.offsetHeight;
    let left = rect.left + rect.width / 2 - popW / 2;
    let top = rect.bottom + 6;
    if (left < 8) left = 8;
    if (left + popW > window.innerWidth - 8) left = window.innerWidth - popW - 8;
    if (top + popH > window.innerHeight - 8) top = rect.top - popH - 6;
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';

    setTimeout(() => {
      document.addEventListener('click', onDocClick = (e) => {
        if (!pop.contains(e.target) && !anchor.contains(e.target)) hidePopover();
      });
    }, 0);
  }

  function hidePopover() {
    if (popoverEl) { popoverEl.remove(); popoverEl = null; }
    if (onDocClick) { document.removeEventListener('click', onDocClick); onDocClick = null; }
  }

  // DOMContentLoaded 后初始化
  document.addEventListener('DOMContentLoaded', init);

  return { open, close };
})();
