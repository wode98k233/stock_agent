/*
   选股雷达 Web - 交易日历组件
*/

window.StockRadar = window.StockRadar || {};

window.StockRadar.TradingCalendar = class TradingCalendar {
  constructor() {
    this.overlayEl = document.getElementById('calendarOverlay');
    this.modalEl = document.getElementById('calendarModal');
    this.bodyEl = document.getElementById('calendarBody');
    this.titleEl = document.getElementById('calYearTitle');
    this.monthTitleEl = document.getElementById('calMonthTitle');

    const now = new Date();
    this.year = now.getFullYear();
    this.month = now.getMonth() + 1;
    this.selectedDate = null;
    this.data = null;
    this.loading = false;

    this._bindNav();
    this._bindClose();
  }

  open() {
    const now = new Date();
    this.year = now.getFullYear();
    this.month = now.getMonth() + 1;
    this.selectedDate = this._dateString(now);
    this.overlayEl.classList.add('open');
    this.modalEl.classList.add('open');
    this._loadMonth();
  }

  close() {
    this.overlayEl.classList.remove('open');
    this.modalEl.classList.remove('open');
  }

  _bindNav() {
    document.getElementById('calPrevYear').onclick = () => this._navigate(-1, 0);
    document.getElementById('calNextYear').onclick = () => this._navigate(1, 0);
    document.getElementById('calPrevMonth').onclick = () => this._navigate(0, -1);
    document.getElementById('calNextMonth').onclick = () => this._navigate(0, 1);
  }

  _bindClose() {
    document.getElementById('calendarCloseBtn').onclick = () => this.close();
    this.overlayEl.onclick = () => this.close();
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.modalEl.classList.contains('open')) this.close();
    });
  }

  _navigate(yearDelta, monthDelta) {
    this.month += monthDelta;
    this.year += yearDelta;
    if (this.month < 1) { this.month = 12; this.year--; }
    if (this.month > 12) { this.month = 1; this.year++; }
    this.selectedDate = `${this.year}-${String(this.month).padStart(2, '0')}-01`;
    this._loadMonth();
  }

  async _loadMonth() {
    this._updateTitle();
    this._showLoading();
    this.loading = true;
    try {
      this.data = await window.StockRadar.api.getCalendar(this.year, this.month);
    } catch (err) {
      this.data = { trading_days: [], dialog_days: {} };
    }
    this.loading = false;
    this._selectDefaultDate();
    this._render();
  }

  _updateTitle() {
    this.titleEl.textContent = `${this.year}年`;
    this.monthTitleEl.textContent = `${this.month}月`;
  }

  _showLoading() {
    this.bodyEl.innerHTML = '<div class="calendar-loading"><div class="calendar-loading-spinner"></div>加载中...</div>';
  }

  _selectDefaultDate() {
    const dialogDays = this.data.dialog_days || {};
    if (this.selectedDate && this.selectedDate.startsWith(`${this.year}-${String(this.month).padStart(2, '0')}`)) {
      return;
    }
    const days = Object.keys(dialogDays).sort();
    this.selectedDate = days.length > 0 ? days[days.length - 1] : `${this.year}-${String(this.month).padStart(2, '0')}-01`;
  }

  _render() {
    this.bodyEl.innerHTML = '';
    this.bodyEl.className = 'calendar-modal-body calendar-workspace';

    const stats = this._renderStats();
    const main = document.createElement('div');
    main.className = 'calendar-main-grid';

    const monthPane = document.createElement('div');
    monthPane.className = 'calendar-month-pane';
    monthPane.appendChild(this._renderWeekdays());
    monthPane.appendChild(this._renderGrid());

    const dayPane = document.createElement('aside');
    dayPane.className = 'calendar-day-pane';
    dayPane.appendChild(this._renderDayDetail());

    main.appendChild(monthPane);
    main.appendChild(dayPane);

    const legend = this._renderLegend();
    this.bodyEl.appendChild(stats);
    this.bodyEl.appendChild(main);
    this.bodyEl.appendChild(legend);
  }

  _renderStats() {
    const tradingDays = this.data.trading_days || [];
    const dialogDays = this.data.dialog_days || {};
    const dialogDates = Object.keys(dialogDays);
    const dialogCount = dialogDates.reduce((acc, date) => acc + (dialogDays[date] || []).length, 0);

    // 聚合 token 统计
    let totalIn = 0, totalOut = 0, totalTok = 0, totalCached = 0, totalReasoning = 0, totalDur = 0, durCount = 0;
    for (const dialogs of Object.values(dialogDays)) {
      for (const dlg of dialogs) {
        totalIn += dlg.input_tokens || 0;
        totalOut += dlg.output_tokens || 0;
        totalTok += dlg.total_tokens || 0;
        totalCached += dlg.cached_tokens || 0;
        totalReasoning += dlg.reasoning_tokens || 0;
        if (dlg.duration_ms) { totalDur += dlg.duration_ms; durCount++; }
      }
    }
    const avgDur = durCount > 0 ? totalDur / durCount : 0;
    const totalDurSec = totalDur / 1000;
    const cacheHit = totalIn > 0 ? (totalCached / totalIn * 100).toFixed(0) : 0;
    const fmt = (n) => n > 0 ? this._fmtCompact(n) : '—';
    const fmtDur = (ms) => ms > 0 ? this._fmtDur(ms) : '—';

    const stats = document.createElement('div');
    stats.className = 'calendar-stats';
    stats.innerHTML = `
      <div class="cal-stat"><span class="cal-stat-label">交易日</span><span class="cal-stat-value">${tradingDays.length}</span></div>
      <div class="cal-stat"><span class="cal-stat-label">有记录</span><span class="cal-stat-value">${dialogDates.length} 天</span></div>
      <div class="cal-stat"><span class="cal-stat-label">记录数</span><span class="cal-stat-value">${dialogCount}</span></div>
      <div class="cal-stat"><span class="cal-stat-label">Token 总计</span><span class="cal-stat-value tok-total">${fmt(totalTok)}</span></div>
      <div class="cal-stat"><span class="cal-stat-label">输入</span><span class="cal-stat-value tok-in">${fmt(totalIn)}</span></div>
      <div class="cal-stat"><span class="cal-stat-label">输出</span><span class="cal-stat-value tok-out">${fmt(totalOut)}</span></div>
      ${totalCached ? `<div class="cal-stat"><span class="cal-stat-label">缓存命中</span><span class="cal-stat-value" style="color:#10b981">${fmt(totalCached)} (${cacheHit}%)</span></div>` : ""}
      ${totalReasoning ? `<div class="cal-stat"><span class="cal-stat-label">思考</span><span class="cal-stat-value" style="color:#8b5cf6">${fmt(totalReasoning)}</span></div>` : ""}
      <div class="cal-stat"><span class="cal-stat-label">总耗时</span><span class="cal-stat-value">${fmtDur(totalDur)}</span></div>
      <div class="cal-stat"><span class="cal-stat-label">平均耗时</span><span class="cal-stat-value">${fmtDur(avgDur)}</span></div>`;
    return stats;
  }

  _renderWeekdays() {
    const weekdays = document.createElement('div');
    weekdays.className = 'calendar-weekdays';
    ['一', '二', '三', '四', '五', '六', '日'].forEach((d, i) => {
      const el = document.createElement('div');
      el.className = 'calendar-weekday' + (i >= 5 ? ' weekend' : '');
      el.textContent = d;
      weekdays.appendChild(el);
    });
    return weekdays;
  }

  _renderGrid() {
    const grid = document.createElement('div');
    grid.className = 'calendar-grid';

    const tradingSet = new Set(this.data.trading_days || []);
    const dialogDays = this.data.dialog_days || {};
    const todayStr = this._dateString(new Date());

    const firstDay = new Date(this.year, this.month - 1, 1);
    const lastDay = new Date(this.year, this.month, 0);
    const startOffset = (firstDay.getDay() + 6) % 7;
    const prevLast = new Date(this.year, this.month - 1, 0);

    for (let i = startOffset - 1; i >= 0; i--) {
      grid.appendChild(this._createDayCell(prevLast.getDate() - i, null, true, false, false, false, []));
    }

    for (let d = 1; d <= lastDay.getDate(); d++) {
      const dateStr = `${this.year}-${String(this.month).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      const dow = new Date(this.year, this.month - 1, d).getDay();
      const dialogs = dialogDays[dateStr] || [];
      grid.appendChild(this._createDayCell(
        d,
        dateStr,
        false,
        dow === 0 || dow === 6,
        tradingSet.has(dateStr),
        dateStr === todayStr,
        dialogs
      ));
    }

    const totalCells = startOffset + lastDay.getDate();
    const remainder = totalCells % 7;
    if (remainder > 0) {
      for (let d = 1; d <= 7 - remainder; d++) {
        grid.appendChild(this._createDayCell(d, null, true, false, false, false, []));
      }
    }
    return grid;
  }

  _createDayCell(num, dateStr, otherMonth, isWeekend, isTrading, isToday, dialogs) {
    const cell = document.createElement('button');
    cell.type = 'button';
    let cls = 'calendar-day';
    if (otherMonth) cls += ' other-month';
    if (isWeekend && !otherMonth) cls += ' weekend';
    if (isTrading) cls += ' trading';
    if (isToday) cls += ' today';
    if (dateStr === this.selectedDate) cls += ' selected';
    if (dialogs.length > 0) cls += ' has-dialogs';
    cell.className = cls;
    if (!dateStr) cell.disabled = true;

    const numEl = document.createElement('span');
    numEl.className = 'calendar-day-num';
    numEl.textContent = num;
    cell.appendChild(numEl);

    const meta = document.createElement('span');
    meta.className = 'calendar-day-meta';
    if (dialogs.length > 0 && !otherMonth) {
      const count = document.createElement('span');
      count.textContent = `${dialogs.length}条`;
      meta.appendChild(count);
      const dots = document.createElement('span');
      dots.className = 'calendar-dots';
      const uniqueModes = [...new Set(dialogs.map(d => d.mode))].slice(0, 4);
      uniqueModes.forEach(mode => {
        const dot = document.createElement('span');
        dot.className = `calendar-dot mode-${mode || 'react_stock'}`;
        dots.appendChild(dot);
      });
      meta.appendChild(dots);
    } else if (isTrading) {
      meta.textContent = '交易日';
    } else if (isToday) {
      meta.textContent = '今天';
    }
    cell.appendChild(meta);

    if (dateStr) {
      cell.addEventListener('click', () => {
        this.selectedDate = dateStr;
        this._render();
      });
    }
    return cell;
  }

  _renderDayDetail() {
    const dialogDays = this.data.dialog_days || {};
    const dialogs = dialogDays[this.selectedDate] || [];
    const tradingSet = new Set(this.data.trading_days || []);
    const card = document.createElement('div');
    card.className = 'calendar-day-card';

    const [y, m, d] = (this.selectedDate || '').split('-');
    const dateObj = this.selectedDate ? new Date(Number(y), Number(m) - 1, Number(d)) : null;
    const week = dateObj ? ['日', '一', '二', '三', '四', '五', '六'][dateObj.getDay()] : '';
    const isTrading = tradingSet.has(this.selectedDate);
    card.innerHTML = `
      <div class="calendar-day-head">
        <div class="calendar-day-title">${m ? `${parseInt(m)}月${parseInt(d)}日 周${week}` : '未选择日期'}</div>
        <div class="calendar-day-sub">${isTrading ? '交易日' : '非交易日'} · ${dialogs.length} 条研究记录</div>
      </div>`;

    const list = document.createElement('div');
    list.className = 'calendar-record-list';
    if (dialogs.length === 0) {
      list.innerHTML = '<div class="calendar-empty">当天暂无研究记录</div>';
    } else {
      const sorted = [...dialogs].sort((a, b) => b.created_at.localeCompare(a.created_at));
      sorted.forEach((dlg, idx) => list.appendChild(this._renderRecordItem(dlg, idx === 0)));
    }
    card.appendChild(list);
    return card;
  }

  _renderRecordItem(dlg, active) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'calendar-record-item' + (active ? ' active' : '');

    const hasTokens = (dlg.total_tokens || 0) > 0;
    const hasDur = dlg.duration_ms != null;
    const fmt = (n) => this._fmtCompact(n || 0);

    // token 行：完整的多维度统计
    let tokenHtml = '';
    if (hasTokens || hasDur) {
      const bits = [];
      if (hasTokens) {
        bits.push(`<span class="rec-tok rec-tok-total">总 ${fmt(dlg.total_tokens)}</span>`);
        bits.push(`<span class="rec-tok rec-tok-in">入 ${fmt(dlg.input_tokens)}</span>`);
        bits.push(`<span class="rec-tok rec-tok-out">出 ${fmt(dlg.output_tokens)}</span>`);
        if (dlg.cached_tokens) {
          const hitRate = dlg.input_tokens > 0 ? (dlg.cached_tokens / dlg.input_tokens * 100).toFixed(0) : 0;
          bits.push(`<span class="rec-tok" style="color:#10b981">缓 ${fmt(dlg.cached_tokens)} (${hitRate}%)</span>`);
        }
        if (dlg.reasoning_tokens) {
          bits.push(`<span class="rec-tok" style="color:#8b5cf6">思 ${fmt(dlg.reasoning_tokens)}</span>`);
        }
      }
      if (hasDur) {
        bits.push(`<span class="rec-tok rec-tok-dur">耗时 ${this._fmtDur(dlg.duration_ms)}</span>`);
      }
      tokenHtml = `<span class="calendar-record-tokens">${bits.join('')}</span>`;
    }

    item.innerHTML = `
      <span class="calendar-record-title">${this._esc(dlg.title || '新对话')}</span>
      <span class="calendar-record-meta">
        <span>${this._timeLabel(dlg.created_at)}</span>
        <span class="calendar-record-mode mode-${dlg.mode || 'react_stock'}">${this._modeLabel(dlg.mode)}</span>
      </span>
      ${tokenHtml}`;
    item.addEventListener('click', () => {
      this.close();
      if (window.StockRadar._selectDialog) window.StockRadar._selectDialog(dlg.dialog_uuid);
    });
    return item;
  }

  _renderLegend() {
    const legend = document.createElement('div');
    legend.className = 'calendar-legend';
    legend.innerHTML = `
      <span><i class="calendar-dot mode-react_stock"></i> ReAct</span>
      <span><i class="calendar-dot mode-plan_solve"></i> Plan</span>
      <span><i class="calendar-dot mode-unified_plan"></i> Unified</span>
      <span><i class="calendar-dot mode-scenario"></i> Scenario</span>
      <span><i class="calendar-dot mode-pdor"></i> PDOR</span>`;
    return legend;
  }

  _latestDialog(dialogDays) {
    const all = Object.values(dialogDays).flat();
    if (all.length === 0) return null;
    return all.sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  }

  _dateString(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }

  _timeLabel(value) {
    try {
      const dt = new Date(value);
      return `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`;
    } catch {
      return '';
    }
  }

  _modeLabel(mode) {
    const map = {
      react_stock: 'ReAct',
      plan_solve: 'Plan',
      unified_plan: 'Unified',
      scenario: 'Scenario',
      pdor: 'PDOR',
    };
    return map[mode] || mode || 'ReAct';
  }

  _esc(value) {
    const d = document.createElement('div');
    d.textContent = value == null ? '' : String(value);
    return d.innerHTML;
  }

  _fmtCompact(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
  }

  _fmtDur(ms) {
    if (ms == null) return '...';
    if (ms < 1000) return Math.round(ms) + 'ms';
    return (ms / 1000).toFixed(1) + 's';
  }
};
