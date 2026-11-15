/*
   选股雷达 Web - 持仓账本面板
   结构：摘要卡（市值/浮盈/已实现/持仓数 + 东财账户总资产/可用资金）
        + 持仓表（可用/类型/来源列 + 盯盘/看K线操作）+ 交易流水（可折叠）+ 记交易表单
   后端：流水为 source of truth（加权平均成本），持仓为重算快照；
        东财同步快照行（source=eastmoney）以券商为事实源（§3.7）
*/

window.StockRadar = window.StockRadar || {};

// 持仓/流水来源标签（东财同步新增 eastmoney）
const SRC_LABELS = {
  manual: '手动',
  signal_eval: '信号',
  backtest: '回测',
  mx_moni: '模拟盘',
  eastmoney: '东财',
};

window.StockRadar.PortfolioPanel = class PortfolioPanel {
  constructor() {
    this.overlayEl = document.getElementById('pfOverlay');
    this.modalEl = document.getElementById('pfModal');
    this.closeBtn = document.getElementById('pfCloseBtn');
    this.summaryEl = document.getElementById('pfSummary');
    this.positionsBodyEl = document.getElementById('pfPositionsBody');
    this.tradesBodyEl = document.getElementById('pfTradesBody');
    this.tradesToggleEl = document.getElementById('pfTradesToggle');
    this.tradesSectionEl = document.getElementById('pfTradesSection');
    this.refreshBtn = document.getElementById('pfRefreshBtn');
    this.syncBtn = document.getElementById('pfSyncBtn');
    this.scanBtn = document.getElementById('pfScanBtn');
    this.accountEl = document.getElementById('pfAccount');
    this.sourceSelectorEl = document.getElementById('pfSourceSelector');
    this.sourceTrigger = document.getElementById('pfSourceTrigger');
    this.sourceDropdown = document.getElementById('pfSourceDropdown');
    this.sourceLabel = document.getElementById('pfSourceLabel');

    this.positions = [];
    this.trades = [];
    this.tradesPage = 1;
    this.tradesTotal = 0;
    this.quoteMeta = null;      // {source, time} 行情来源与时间
    this.source = localStorage.getItem('pf_quote_source') || 'sina';
    this._preFill = null; // 行内「记交易」预填 {code,name,side}

    this.bind();
  }

  open() {
    this.overlayEl.classList.add('open');
    this.modalEl.classList.add('open');
    this._loadAll();
  }

  close() {
    this.overlayEl.classList.remove('open');
    this.modalEl.classList.remove('open');
  }

  bind() {
    this.closeBtn.onclick = () => this.close();
    this.overlayEl.onclick = () => this.close();
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.modalEl.classList.contains('open')) this.close();
    });
    document.getElementById('pfAddBtn').onclick = () => this._submitTrade();
    this.refreshBtn.onclick = () => this._loadAll();
    if (this.syncBtn) this.syncBtn.onclick = () => this._openEastmoneySync();
    if (this.scanBtn) this.scanBtn.onclick = () => this._openScan && this._openScan();
    const histBtn = document.getElementById('pfHistoryBtn');
    if (histBtn) histBtn.onclick = () => this._openAccountHistory();
    // 行情数据源下拉（K线图同款交互）
    if (this.sourceTrigger) {
      this.sourceTrigger.onclick = () => this._toggleSourceDropdown();
      this.sourceTrigger.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this._toggleSourceDropdown(); }
      });
    }
    document.addEventListener('click', (e) => {
      if (this.sourceSelectorEl && !this.sourceSelectorEl.contains(e.target)) this._closeSourceDropdown();
    });
    this._initSourceOptions();
    this.tradesToggleEl.onclick = () => {
      const collapsed = this.tradesSectionEl.classList.toggle('collapsed');
      this.tradesToggleEl.textContent = collapsed ? '交易流水 ▸' : '交易流水 ▾';
    };
    document.getElementById('pfMoreTradesBtn').onclick = () => {
      this.tradesPage += 1;
      this._loadTrades();
    };
    document.getElementById('pfTradeDate').value = new Date().toISOString().slice(0, 10);
  }

  // ── 东财同步弹窗（独立模块 eastmoney-sync.js，弹窗自建 DOM） ──────────────

  _openEastmoneySync() {
    if (!window.StockRadar.EastmoneySyncDialog) {
      this._toast('同步模块未加载（eastmoney-sync.js）', 'error');
      return;
    }
    if (!this._emDialog) {
      this._emDialog = new window.StockRadar.EastmoneySyncDialog(() => this._loadAll());
    }
    this._emDialog.open();
  }

  // ── 批量策略扫描（跳转回测面板：多标的批量回测，参数预填） ───────────────

  _openScan() {
    const bt = typeof window.initBacktestPanel === 'function' && window.initBacktestPanel();
    if (!bt || !bt.openScanWithCodes) {
      this._toast('回测面板未加载（backtest.js）', 'error');
      return;
    }
    // 用当前持仓（qty>0）批量跑回测
    const codes = (this.positions || [])
      .filter(p => (p.qty || 0) > 0)
      .map(p => p.stock_code);
    if (!codes.length) { this._toast('没有可扫描的持仓', 'error'); return; }
    bt.openScanWithCodes(codes);
  }

  // ── 数据加载 ────────────────────────────────────────────────────────────

  async _loadAll() {
    await Promise.all([this._loadPositions(), this._loadTrades()]);
    this.account = this.account || {};
    await this._loadAccount();
  }

  async _loadPositions() {
    try {
      const data = await window.StockRadar.api.getPortfolioPositions(this.source);
      this.positions = data.items || [];
      this.quoteMeta = data.quote || null;
      this._renderSummary(data.summary || {});
      this._renderQuoteMeta();
      this._renderAccount();
      this._renderPositions();
      this._loadEvents();
    } catch (e) {
      this.summaryEl.innerHTML = `<span class="pf-error">持仓加载失败：${this._esc(e.message || e)}</span>`;
    }
  }

  // 未来 30 天公司事件（解禁/除权/披露/股东大会/预告），只显示当前持仓标的
  async _loadEvents() {
    const el = document.getElementById('pfEventsSection');
    if (!el) return;
    const codes = [...new Set((this.positions || []).map(p => p.stock_code).filter(Boolean))];
    if (!codes.length) { el.hidden = true; return; }
    const today = new Date().toISOString().slice(0, 10);
    const to = new Date(Date.now() + 30 * 86400000).toISOString().slice(0, 10);
    try {
      const r = await fetch(`/api/events?codes=${codes.join(',')}&from=${today}&to=${to}&limit=100`);
      const body = await r.json();
      const events = (body && body.data && body.data.events) || [];
      if (!events.length) { el.hidden = true; return; }
      el.innerHTML = this._renderEventsHtml(events);
      el.hidden = false;
    } catch (e) {
      el.hidden = true;
    }
  }

  _renderEventsHtml(events) {
    const meta = {
      unlock: ['解禁', 'unlock'], dividend: ['分红除权', 'dividend'],
      disclosure: ['预约披露', 'disclosure'], meeting: ['股东大会', 'meeting'],
      forecast: ['业绩预告', 'forecast'],
    };
    const rows = events.map((ev) => {
      const [label, cls] = meta[ev.event_type] || [ev.type_label || ev.event_type, 'disclosure'];
      const d = String(ev.event_date || '').slice(5);
      return `<div class="pf-event-row">
        <span class="pf-event-date">${this._esc(d)}</span>
        <span class="pf-event-type ${cls}">${this._esc(label)}</span>
        <span class="pf-event-main"><b>${this._esc(ev.code || '')}</b> ${this._esc(ev.title || '')}</span>
      </div>`;
    }).join('');
    return `<div class="pf-events-head">未来 30 天事件 <span class="pf-events-count">${events.length}</span></div>
      <div class="pf-events-rows">${rows}</div>`;
  }

  // 行情数据源下拉：新浪·快（默认）+ /api/datasources 可用源（全市场快照，较慢）
  async _initSourceOptions() {
    if (!this.sourceDropdown) return;
    this._renderSourceDropdown([{ name: 'sina', label: '新浪', description: '实时行情，批量拉取，速度快' }]);
    try {
      const data = await window.StockRadar.api.getDataSources();
      const items = (data.sources || []).filter(
        (ds) => ds.available && ds.name !== 'sina' && ds.name !== 'sina_direct');
      if (items.length) this._renderSourceDropdown(items, true);
    } catch { /* 保底仅新浪 */ }
    this._setSourceLabel(this.source);
  }

  _renderSourceDropdown(items, append) {
    if (!append) this.sourceDropdown.innerHTML = '';
    for (const ds of items) {
      const opt = document.createElement('div');
      opt.className = 'kline-source-option' + (ds.name === this.source ? ' active' : '');
      opt.innerHTML = `
        <div class="kline-source-option-main">
          <div class="kline-source-option-name">${this._esc(ds.label || ds.name)}</div>
          <div class="kline-source-option-desc">${this._esc(ds.description || '')}</div>
        </div>
        <svg class="kline-source-option-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
      opt.addEventListener('click', () => this._selectSource(ds.name, ds.label || ds.name));
      this.sourceDropdown.appendChild(opt);
    }
  }

  _selectSource(name, label) {
    if (name === this.source) { this._closeSourceDropdown(); return; }
    this.source = name;
    localStorage.setItem('pf_quote_source', name);
    this._setSourceLabel(label);
    this._closeSourceDropdown();
    this._loadAll();
  }

  _setSourceLabel(label) {
    if (this.sourceLabel) this.sourceLabel.textContent = label || '新浪';
  }

  _toggleSourceDropdown() {
    if (!this.sourceDropdown) return;
    const open = this.sourceDropdown.classList.contains('open');
    open ? this._closeSourceDropdown() : this._openSourceDropdown();
  }

  _openSourceDropdown() {
    this.sourceDropdown.classList.add('open');
    if (this.sourceTrigger) this.sourceTrigger.setAttribute('aria-expanded', 'true');
  }

  _closeSourceDropdown() {
    if (this.sourceDropdown) this.sourceDropdown.classList.remove('open');
    if (this.sourceTrigger) this.sourceTrigger.setAttribute('aria-expanded', 'false');
  }

  // 现价来源与行情时间反馈（让用户确认刷新确实走了数据源）
  _renderQuoteMeta() {
    const el = document.getElementById('pfQuoteMeta');
    if (!el) return;
    const q = this.quoteMeta || {};
    const label = this._sourceLabel(q.source || this.source || 'sina');
    el.textContent = q.time ? `现价来源：${label} · 行情时间 ${q.time}` : `现价来源：${label}`;
  }

  _sourceLabel(source) {
    const map = { sina: '新浪', auto: '自动' };
    return map[source] || source;
  }

  async _loadAccount() {
    try {
      const result = await window.StockRadar.api.getEastmoneySession();
      this.account = result.code === 0 ? (result.data || {}) : {};
    } catch {
      this.account = {};
    }
    this._renderAccount();
  }

  // 账户总资产/可用资金/今日盈亏（东财同步数据；手动记账给不了）——渲染 + 后台刷新
  _renderAccount() {
    if (!this.accountEl) return;
    const a = this.account || {};
    const acct = a.account || {};
    const fmt = (v) => v == null ? '—' :
      Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
    const pnlCls = (v) => v == null ? '' : v >= 0 ? 'pf-up' : 'pf-down';
    const fmtPnl = (v) => v == null ? '—' :
      `${v >= 0 ? '+' : ''}${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`;
    if (acct.total_asset == null) {
      this.accountEl.innerHTML = a.last_synced_at
        ? `<span class="pf-account-hint">上次东财同步 ${this._esc(a.last_synced_at)} · 账户资金暂缺（重新同步后显示）</span>`
        : '<span class="pf-account-hint">尚未同步东方财富：点右上「同步东方财富」导入真实持仓与账户资金</span>';
      return;
    }
    // 盘中快照语义标注：交易时段内最新价为实时快照会波动，仅买入价/数量确定
    let stamp = '';
    if (acct.snapshot_at && acct.market_status) {
      const warn = acct.market_status === '盘中' ? ' · 最新价为盘中快照，会波动' : '';
      stamp = `<span class="pf-account-hint">同步于 ${this._esc(acct.snapshot_at)}（${this._esc(acct.market_status)}${warn}）</span>`;
    }
    this.accountEl.innerHTML = `
      <span>账户总资产 <b>${fmt(acct.total_asset)}</b></span>
      <span>可用资金 <b>${fmt(acct.avail_cash)}</b></span>
      <span>持仓市值 <b>${fmt(acct.positions_market_value)}</b></span>
      <span>今日盈亏 <b class="${pnlCls(acct.today_pnl)}">${fmtPnl(acct.today_pnl)}</b></span>
      ${stamp}`;
  }

  async _loadTrades() {
    try {
      const data = await window.StockRadar.api.getPortfolioTrades(null, this.tradesPage, 50);
      this.tradesTotal = data.total || 0;
      const items = data.items || [];
      const seen = new Set(this.trades.map((t) => t.id));
      this.trades = this.trades.concat(items.filter((t) => !seen.has(t.id)));
      this._renderTrades();
    } catch { /* 保留旧数据 */ }
  }

  // ── 渲染 ────────────────────────────────────────────────────────────────

  _renderSummary(s) {
    const fmt = (v, digits = 2, suffix = '') => v == null ? '—' :
      `${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits })}${suffix}`;
    const pnl = (v) => v == null ? '—' :
      `<span class="${v >= 0 ? 'pf-up' : 'pf-down'}">${v >= 0 ? '+' : ''}${fmt(v)}</span>`;
    this.summaryEl.innerHTML = `
      <div class="pf-stat"><div class="pf-stat-label">总市值</div><div class="pf-stat-value">${fmt(s.total_market_value)}</div></div>
      <div class="pf-stat"><div class="pf-stat-label">总成本</div><div class="pf-stat-value">${fmt(s.total_cost)}</div></div>
      <div class="pf-stat"><div class="pf-stat-label">浮动盈亏</div><div class="pf-stat-value">${pnl(s.total_float_pnl)}</div></div>
      <div class="pf-stat"><div class="pf-stat-label">已实现盈亏</div><div class="pf-stat-value">${pnl(s.total_realized_pnl)}</div></div>
      <div class="pf-stat"><div class="pf-stat-label">持仓数</div><div class="pf-stat-value">${this._esc(s.position_count ?? 0)}</div></div>`;
  }

  // 价格精度：<1 元的低价 ETF/基金显示 3 位小数，其余 2 位
  _fmtPrice(v) {
    if (v == null) return '—';
    const n = Number(v);
    return n.toFixed(n < 1 ? 3 : 2);
  }

  _renderPositions() {
    if (!this.positions.length) {
      this.positionsBodyEl.innerHTML = '<div class="pf-empty">暂无持仓：从下方记一笔买入，或点右上「同步东方财富」导入真实持仓</div>';
      return;
    }
    // 表头排序：默认按写入顺序；点表头切换 asc/desc
    if (this.sortKey && this.sortDir) {
      const dir = this.sortDir;
      this._sortedPositions = [...this.positions].sort((a, b) => {
        const va = a[this.sortKey];
        const vb = b[this.sortKey];
        if (va == null) return 1;
        if (vb == null) return -1;
        if (typeof va === 'string' && typeof vb === 'string') {
          return va.localeCompare(vb, 'zh-CN') * dir;
        }
        return ((Number(va) || 0) - (Number(vb) || 0)) * dir || 0;
      });
    } else {
      this._sortedPositions = this.positions;
    }
    // 行内「距成本」迷你条用的量程：取全表最大绝对盈亏率，用来归一化每行的条
    const maxAbs = Math.max(10, ...this._sortedPositions.map(
      (p) => Math.abs(Number(p.float_pnl_pct) || 0)));
    const rows = this._sortedPositions.map((p) => {
      const holding = p.qty > 0;
      const priceCell = this._fmtPrice(p.price);
      const mvCell = p.market_value != null
        ? Number(p.market_value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
        : '—';
      const pnlCell = p.float_pnl != null
        ? `<span class="${p.float_pnl >= 0 ? 'pf-up' : 'pf-down'}">${p.float_pnl >= 0 ? '+' : ''}${p.float_pnl.toFixed(0)}</span>`
        : '—';
      const pctCell = p.float_pnl_pct != null
        ? `<div class="${p.float_pnl_pct >= 0 ? 'pf-up' : 'pf-down'}" style="line-height:1.25;text-align:center">`
          + `${p.float_pnl_pct >= 0 ? '+' : ''}${p.float_pnl_pct.toFixed(2)}%</div>`
          + this._costBarSvg(p.float_pnl_pct, maxAbs)
        : '—';
      const dayPnl = p.today_pnl != null
        ? `<span class="${p.today_pnl >= 0 ? 'pf-up' : 'pf-down'}">${p.today_pnl >= 0 ? '+' : ''}${p.today_pnl.toFixed(2)}</span>`
        : '—';
      const availCell = holding
        ? (p.avail_qty != null ? p.avail_qty : p.qty)
        : '—';
      const typeCell = p.etf_flag ? '<span class="pf-badge">ETF</span>' : '<span class="pf-badge-plain">股</span>';
      const srcCell = `<span class="pf-trade-src">${this._esc(SRC_LABELS[p.source] || p.source || '手动')}</span>`;
      const fmtDate = (iso) => (iso ? String(iso).slice(0, 10) : '-');
      return `
        <div class="pf-pos-row${holding ? '' : ' pf-pos-closed'}">
          <div class="pf-pos-stock">
            <div class="pf-pos-name">${this._esc(p.stock_name || p.stock_code)}<span class="pf-pos-code">${this._esc(p.stock_code)}</span></div>
            <div class="pf-pos-code">开仓 ${this._esc(fmtDate(p.opened_at))}${p.etf_flag ? ' · 场内' : ''}</div>
          </div>
          <div>${p.qty}</div>
          <div>${availCell}</div>
          <div>${typeCell}</div>
          <div>${p.avg_cost.toFixed(2)}</div>
          <div>${priceCell}</div>
          <div>${mvCell}</div>
          <div>${pnlCell}</div>
          <div>${pctCell}</div>
          <div>${dayPnl}</div>
          <div>${p.realized_pnl ? `<span class="${p.realized_pnl >= 0 ? 'pf-up' : 'pf-down'}">${p.realized_pnl >= 0 ? '+' : ''}${p.realized_pnl.toFixed(0)}</span>` : '—'}</div>
          <div>${srcCell}</div>
          <div class="pf-pos-ops">
            <button class="pf-btn" data-act="buy" data-code="${p.stock_code}" data-name="${this._esc(p.stock_name || '')}">买入</button>
            <button class="pf-btn" data-act="sell" data-code="${p.stock_code}" data-name="${this._esc(p.stock_name || '')}" ${holding ? '' : 'disabled'}>卖出</button>
            <button class="pf-btn" data-act="watch" data-code="${p.stock_code}" data-name="${this._esc(p.stock_name || '')}">加自选</button>
            <button class="pf-btn" data-act="monitor" data-code="${p.stock_code}" data-name="${this._esc(p.stock_name || '')}">盯盘</button>
            <button class="pf-btn" data-act="kline" data-code="${p.stock_code}" data-cost="${p.avg_cost ?? ''}">看K线</button>
          </div>
        </div>`;
    }).join('');

    const col = (label, key) => {
      const mark = this.sortKey === key ? (this.sortDir > 0 ? ' ↑' : ' ↓') : '';
      return `<span data-sort="${key}">${label}${mark}</span>`;
    };
    this.positionsBodyEl.innerHTML = `<div class="pf-pos-head">
        <span>股票</span>${col('数量', 'qty')}${col('可用', 'avail_qty')}<span>类型</span>${col('成本价', 'avg_cost')}${col('现价', 'price')}${col('市值', 'market_value')}${col('浮盈亏', 'float_pnl')}${col('盈亏率', 'float_pnl_pct')}${col('今日盈亏', 'today_pnl')}${col('已实现', 'realized_pnl')}<span>来源</span><span></span>
      </div>${rows}
      <div class="pf-pos-foot" style="grid-column:1/-1;display:flex;justify-content:flex-end;padding:10px 4px 2px"><button class="pf-btn" id="pfCostDistBtn" type="button">看全貌 · 成本分布</button></div>`;
    this.positionsBodyEl.querySelectorAll('.pf-btn').forEach((b) => {
      if (b.id === 'pfCostDistBtn') return;
      b.onclick = () => this._rowAction(b.dataset.act, b.dataset.code, b.dataset.name, b.dataset.cost);
    });
    const distBtn = this.positionsBodyEl.querySelector('#pfCostDistBtn');
    if (distBtn) distBtn.onclick = () => this._openCostDistribution();
    this.positionsBodyEl.querySelectorAll('.pf-pos-head span[data-sort]').forEach((h) => {
      h.onclick = () => this._setSort(h.dataset.sort);
    });
  }

  /** 行内迷你条：中点=成本，圆点=现价，连线长度=盈亏幅度 */
  _costBarSvg(ret, maxAbs) {
    // 宽度跟盈亏率列（150px）配套：54px 时只能看出方向，读不出幅度
    const w = 130, h = 14, cx = w / 2, mid = h / 2;
    const lim = Math.max(10, Math.ceil(maxAbs / 10) * 10);
    const x = cx + Math.max(-1, Math.min(1, ret / lim)) * (cx - 4);
    const col = ret >= 0 ? '#e74c3c' : '#176b5b';
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true" style="display:block;margin:0 auto">`
      + `<line x1="3" y1="${mid}" x2="${w - 3}" y2="${mid}" stroke="#e6e5df" stroke-width="1"/>`
      + `<line x1="${cx}" y1="2" x2="${cx}" y2="${h - 2}" stroke="#d8d7d1" stroke-width="1"/>`
      + `<line x1="${cx}" y1="${mid}" x2="${x}" y2="${mid}" stroke="${col}" stroke-width="2.5" opacity=".45"/>`
      + `<circle cx="${x}" cy="${mid}" r="3.6" fill="${col}"/></svg>`;
  }

  /** 全貌：成本 → 现价的哑铃图（空心=成本，实心=现价，一珠=1%/2%/5%） */
  _openCostDistribution() {
    const rows = (this._sortedPositions || [])
      .filter((p) => p.qty > 0 && p.float_pnl_pct != null && p.avg_cost)
      .map((p) => ({
        name: p.stock_name || p.stock_code,
        code: p.stock_code,
        ret: Number(p.float_pnl_pct),
      }))
      .sort((a, b) => a.ret - b.ret);
    if (!rows.length) return;

    const W = 900, X0 = 140, X1 = 720, TOP = 22, ROW = 26;
    const lo = Math.min(-2, ...rows.map((r) => r.ret));
    const hi = Math.max(2, ...rows.map((r) => r.ret));
    const pad = (hi - lo) * 0.06;
    const X = (v) => X0 + (v - lo + pad) / (hi - lo + pad * 2) * (X1 - X0);
    const maxAbs = Math.max(...rows.map((r) => Math.abs(r.ret)));
    // 珠子单位随跨度自动选：跨度大时一珠=1% 会串上百颗，珠距也不等值
    const unit = maxAbs <= 25 ? 1 : maxAbs <= 60 ? 2 : 5;
    const UP = '#e74c3c', DOWN = '#176b5b', INK = '#1c1c1a';

    let s = `<line x1="${X(0)}" y1="8" x2="${X(0)}" y2="${TOP + rows.length * ROW}"
      stroke="#d8d7d1" stroke-width="1" stroke-dasharray="3 3"/>`;
    rows.forEach((r, i) => {
      const y = TOP + i * ROW, xa = X(0), xb = X(r.ret);
      const col = r.ret >= 0 ? UP : DOWN;
      s += `<line x1="${X0 - 8}" y1="${y}" x2="${X1 + 8}" y2="${y}" stroke="#f5f4f0" stroke-width="1"/>`;
      const n = Math.round(Math.abs(r.ret) / unit);
      for (let k = 0; k < n; k++) {
        const t = (k + 0.5) / n;
        s += `<circle cx="${(xa + t * (xb - xa)).toFixed(1)}" cy="${y}" r="1.4" fill="${col}" opacity=".45"/>`;
      }
      s += `<circle cx="${xa}" cy="${y}" r="3.8" fill="#fff" stroke="${INK}" stroke-width="1.2"/>`;
      s += `<circle cx="${xb}" cy="${y}" r="4" fill="${col}"/>`;
      s += `<text x="${X0 - 16}" y="${y + 3.5}" font-size="10" fill="#4b5563" text-anchor="end">${this._esc(r.name)}</text>`;
      s += `<text x="${(Math.max(xa, xb) + 12).toFixed(1)}" y="${y + 3.5}" font-size="10" font-weight="700" fill="${col}">`
        + `${r.ret >= 0 ? '+' : ''}${r.ret.toFixed(2)}%</text>`;
    });
    const H = TOP + rows.length * ROW + 16;

    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.42);z-index:1300;'
      + 'display:flex;align-items:center;justify-content:center;padding:24px';
    ov.innerHTML = `<div style="background:#fff;border-radius:12px;max-width:960px;width:100%;`
      + `max-height:88vh;overflow:auto;padding:18px 22px 14px;box-shadow:0 12px 40px rgba(0,0,0,.18)">`
      + `<div style="display:flex;align-items:center;margin-bottom:6px">`
      + `<div style="font-size:15px;font-weight:700">持仓成本分布</div>`
      + `<div style="margin-left:auto;font-size:11px;color:#9a9b96">共 ${rows.length} 只 · 按盈亏率排序</div>`
      + `<button type="button" data-act="close" style="margin-left:14px;border:1px solid #e3e2dd;`
      + `background:#fff;border-radius:6px;width:26px;height:26px;cursor:pointer;color:#6b7280">×</button></div>`
      + `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${s}</svg>`
      + `<div style="font-size:10px;color:#9a9b96;letter-spacing:.06em;margin-top:6px">`
      + `HOLLOW = 成本价 · INK = 现价 · ONE BEAD = ${unit}% 盈亏</div>`
      + `<div style="border-top:1px solid #eeede9;margin:16px 0 12px"></div>`
      + `<div style="display:flex;align-items:center;margin-bottom:10px">`
      + `<div style="font-size:15px;font-weight:700">持仓构成</div>`
      + `<div id="pfShareTabs" style="margin-left:auto;display:flex;gap:4px">`
      + ['waffle|点阵', 'treemap|树图', 'bars|横条', 'corr|相关性', 'profile|画像'].map((x) => {
        const [k, label] = x.split('|');
        return `<button type="button" data-kind="${k}" style="font-size:11px;padding:3px 12px;`
          + `border:1px solid #e3e2dd;background:#fff;border-radius:6px;cursor:pointer;color:#6b7280">${label}</button>`;
      }).join('')
      + `</div></div><div id="pfShareBody" style="min-height:120px"></div></div>`;

    const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
    ov.querySelector('button[data-act="close"]').onclick = close;
    document.addEventListener('keydown', onKey);
    document.body.appendChild(ov);

    // 构成视图：多种样式可切换，选择记在 localStorage
    const shares = (this._sortedPositions || [])
      .filter((p) => p.qty > 0 && Number(p.market_value) > 0)
      .map((p) => ({ name: p.stock_name || p.stock_code, mv: Number(p.market_value) }))
      .sort((a, b) => b.mv - a.mv);
    const holdings = (this._sortedPositions || [])
      .filter((p) => p.qty > 0 && Number(p.market_value) > 0)
      .map((p) => ({ code: p.stock_code, name: p.stock_name || p.stock_code }))
      .sort((a, b) => 0);
    const body = ov.querySelector('#pfShareBody');
    const tabs = ov.querySelector('#pfShareTabs');
    let kind = 'waffle';
    try { kind = localStorage.getItem('pf_share_kind') || 'waffle'; } catch (e) { /* 隐私模式 */ }
    const paint = (k) => {
      kind = k;
      try { localStorage.setItem('pf_share_kind', k); } catch (e) { /* ignore */ }
      tabs.querySelectorAll('button').forEach((b) => {
        const on = b.dataset.kind === k;
        b.style.background = on ? '#176b5b' : '#fff';
        b.style.borderColor = on ? '#176b5b' : '#e3e2dd';
        b.style.color = on ? '#fff' : '#6b7280';
      });
      if (k === 'corr') {
        body.innerHTML = '<div style="padding:24px;color:#9a9b96;font-size:12px">'
          + '正在拉取各标的近 120 日行情并计算相关系数…</div>';
        this._renderCorrelation(body, holdings);
        return;
      }
      if (k === 'profile') {
        body.innerHTML = '<div style="padding:24px;color:#9a9b96;font-size:12px">'
          + '正在拉取各标的近 90 日行情并计算画像指标…</div>';
        this._renderProfile(body, holdings);
        return;
      }
      body.innerHTML = k === 'treemap' ? this._shareTreemap(shares)
        : k === 'bars' ? this._shareBars(shares) : this._shareWaffle(shares);
    };
    tabs.querySelectorAll('button').forEach((b) => { b.onclick = () => paint(b.dataset.kind); });
    paint(kind);
  }

  /** 构成配色：占比是分类编码（哪一只），所以用色相而不是明度。
   *  刻意避开纯红/纯绿——那两个色在站内固定表示涨跌，掺进来会误导。
   *  只保留 6 档：分类色超过 6 个就开始互相认不出（lieflat 的容量规则）。 */
  _shareColor(i, n) {
    const cats = ['#2f6f9f', '#6d4bc4', '#b45309', '#0891b2', '#a3556b', '#5b7db1'];
    return i === n - 1 && n > 1 ? '#c2c1ba' : cats[Math.min(i, cats.length - 1)];
  }

  /** 把 <2% 的零碎仓位合并成「其他 N 只」：
   *  一是避免小仓位在点阵里凭空消失，二是把单独上色的组数压到 6 个以内。 */
  _shareGroups(rows) {
    const total = rows.reduce((s, r) => s + r.mv, 0) || 1;
    const big = [], tail = [];
    rows.forEach((r) => {
      const pct = r.mv / total * 100;
      if (pct >= 2 && big.length < 6) big.push({ name: r.name, pct });
      else tail.push({ name: r.name, pct });
    });
    if (tail.length) {
      big.push({
        name: `其他 ${tail.length} 只`,
        pct: tail.reduce((s, r) => s + r.pct, 0),
        detail: tail.map((r) => `${r.name} ${r.pct.toFixed(1)}%`).join(' · '),
      });
    }
    return big;
  }

  /** 点阵：一格 = 1%，总数固定 100 */
  _shareWaffle(rows) {
    const g = this._shareGroups(rows);
    const cell = 18, gap = 4, cols = 20;
    const W = cols * (cell + gap), H = Math.ceil(100 / cols) * (cell + gap);
    let idx = 0, s = '';
    g.forEach((b, i) => {
      const n = i === g.length - 1 ? 100 - idx : Math.round(b.pct);
      const col = this._shareColor(i, g.length);
      for (let k = 0; k < n && idx < 100; k++, idx++) {
        const x = (idx % cols) * (cell + gap), y = Math.floor(idx / cols) * (cell + gap);
        s += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" rx="3" fill="${col}"/>`;
      }
    });
    const legend = g.map((b, i) =>
      `<span style="display:inline-flex;align-items:center;gap:5px;margin:0 14px 6px 0;`
      + `font-size:11px;color:#4b5563"><i style="display:inline-block;width:9px;height:9px;`
      + `border-radius:2px;background:${this._shareColor(i, g.length)}"></i>`
      + `${this._esc(b.name)} <b style="color:#1c1c1a">${b.pct.toFixed(1)}%</b></span>`).join('');
    const tailNote = g[g.length - 1].detail
      ? `<div style="font-size:10px;color:#9a9b96;margin-top:2px">`
        + `其他含：${this._esc(g[g.length - 1].detail)}</div>` : '';
    return `<svg viewBox="0 0 ${W} ${H}" style="width:${W}px;height:${H}px">${s}</svg>`
      + `<div style="margin-top:10px;line-height:1.9">${legend}</div>${tailNote}`
      + `<div style="font-size:10px;color:#9a9b96;letter-spacing:.06em;margin-top:4px">`
      + `ONE SQUARE = 1% 市值 · 不足 2% 的仓位并入「其他」</div>`;
  }

  /** 树图：slice-and-dice，面积 = 市值占比 */
  _shareTreemap(rows) {
    const g = this._shareGroups(rows);
    const W = 900, H = 300;
    let s = '';
    const walk = (items, x, y, w, h, horiz) => {
      if (!items.length) return;
      if (items.length === 1) {
        const b = items[0];
        s += `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${b.col}" stroke="#fff" stroke-width="2"/>`;
        if (w > 54 && h > 26) {
          s += `<text x="${x + 8}" y="${y + 18}" font-size="11" fill="#fff">${this._esc(b.name)}</text>`;
          s += `<text x="${x + 8}" y="${y + 33}" font-size="12" font-weight="700" fill="#fff">${b.pct.toFixed(0)}%</text>`;
        }
        return;
      }
      const total = items.reduce((a, b) => a + b.pct, 0) || 1;
      let acc = 0, cut = 1;
      for (let k = 0; k < items.length; k++) {
        acc += items[k].pct;
        if (acc >= total / 2) { cut = k + 1; break; }
      }
      // 必须真的切成两段：切到末尾会让 a 等于原数组，递归原地打转（爆栈）
      cut = Math.max(1, Math.min(cut, items.length - 1));
      const a = items.slice(0, cut), b = items.slice(cut);
      const share = a.reduce((p, q) => p + q.pct, 0) / total;
      if (horiz) {
        walk(a, x, y, w * share, h, !horiz);
        walk(b, x + w * share, y, w * (1 - share), h, !horiz);
      } else {
        walk(a, x, y, w, h * share, !horiz);
        walk(b, x, y + h * share, w, h * (1 - share), !horiz);
      }
    };
    walk(g.map((b, i) => ({ ...b, col: this._shareColor(i, g.length) })), 0, 0, W, H, false);
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${s}</svg>`
      + `<div style="font-size:10px;color:#9a9b96;letter-spacing:.06em;margin-top:6px">`
      + `AREA = 市值占比 · 小块放不下字时看横条视图</div>`;
  }

  /** 横条：前 8 大，其余合并 */
  _shareBars(rows) {
    const g = this._shareGroups(rows);
    const top = g.slice(0, 8);
    const rest = g.slice(8);
    const list = top.slice();
    if (rest.length) {
      list.push({ name: `其余 ${rest.length} 组`, pct: rest.reduce((s, r) => s + r.pct, 0) });
    }
    const max = Math.max(...list.map((r) => r.pct)) || 1;
    return list.map((r, i) => {
      const w = (r.pct / max) * 100;
      return `<div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-size:11.5px">`
        + `<div style="width:150px;text-align:right;color:#4b5563;flex:none;overflow:hidden;`
        + `text-overflow:ellipsis;white-space:nowrap">${this._esc(r.name)}</div>`
        + `<div style="flex:1;height:14px"><div style="width:${w.toFixed(1)}%;height:14px;`
        + `background:${this._shareColor(i, list.length)};border-radius:0 7px 7px 0"></div></div>`
        + `<div style="width:56px;text-align:right;font-weight:600;color:#1c1c1a;flex:none">`
        + `${r.pct.toFixed(1)}%</div></div>`;
    }).join('') + `<div style="font-size:10px;color:#9a9b96;letter-spacing:.06em;margin-top:6px">`
      + `BAR = 市值占比</div>`;
  }

  /** 相关性：并发拉各标的最新 120 日行情 → Pearson 相关矩阵 + 强相关对 */
  _renderCorrelation(body, holdings) {
    const api = window.StockRadar && window.StockRadar.api;
    if (!api || !api.getKline || holdings.length < 2) {
      body.innerHTML = '<div style="padding:24px;color:#9a9b96;font-size:12px">'
        + '持仓不足两只，不用看相关性</div>';
      return;
    }
    Promise.all(holdings.map((h) => api.getKline(h.code, 120).catch(() => null)))
      .then((res) => {
        const maps = [], labels = [];
        res.forEach((r, i) => {
          const k = (r && r.kline) || [];
          const mm = {};
          for (let j = 1; j < k.length; j++) {
            const prev = Number(k[j - 1].close), cur = Number(k[j].close);
            if (prev > 0 && Number.isFinite(cur)) mm[k[j].date] = cur / prev - 1;
          }
          maps.push(mm);
          labels.push(holdings[i].name);
        });
        const dates = Object.keys(maps[0] || {}).filter((d) => maps.every((mm) => mm[d] != null));
        if (dates.length < 20) {
          body.innerHTML = '<div style="padding:24px;color:#9a9b96;font-size:12px">'
            + '共同交易日不足 20 天，算不出可靠的相关性</div>';
          return;
        }
        const n = maps.length, m = [];
        for (let i = 0; i < n; i++) {
          m.push([]);
          for (let j = 0; j < n; j++) {
            m[i].push(i === j ? 1 : this._pearson(
              dates.map((d) => maps[i][d]), dates.map((d) => maps[j][d])));
          }
        }
        body.innerHTML = this._corrMatrixSvg(labels, m) + this._corrStrongList(labels, m, dates.length);
      })
      .catch(() => {
        body.innerHTML = '<div style="padding:24px;color:#9a9b96;font-size:12px">相关性计算失败</div>';
      });
  }

  _pearson(a, b) {
    const n = a.length;
    if (n < 10) return null;
    let ma = 0, mb = 0;
    for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
    ma /= n; mb /= n;
    let num = 0, da = 0, db = 0;
    for (let i = 0; i < n; i++) {
      const x = a[i] - ma, y = b[i] - mb;
      num += x * y; da += x * x; db += y * y;
    }
    const den = Math.sqrt(da * db);
    return den > 0 ? num / den : null;
  }

  /** 双极色阶：蓝(负) ↔ 白(0) ↔ 琥珀(正)。不用红绿——那是涨跌语义。 */
  _corrColor(v) {
    const t = Math.max(-1, Math.min(1, v));
    const mix = (a, b, k) => a.map((c, i) => Math.round(c + (b[i] - c) * k));
    const NEG = [47, 111, 159], ZERO = [255, 255, 255], POS = [180, 83, 9];
    const c = t < 0 ? mix(ZERO, NEG, -t) : mix(ZERO, POS, t);
    return 'rgb(' + c.join(',') + ')';
  }

  /** 画像：四象限散点 + 平行坐标（指标全部由日线推导）。
   *  换手率字段本地只有 6.6% 有值，所以轴只用 OHLCV 能算出来的东西。 */
  _renderProfile(body, holdings) {
    const api = window.StockRadar && window.StockRadar.api;
    if (!api || !api.getKline || holdings.length < 2) {
      body.innerHTML = '<div style="padding:24px;color:#9a9b96;font-size:12px">'
        + '持仓不足两只，不用看画像</div>';
      return;
    }
    Promise.all(holdings.map((h) => api.getKline(h.code, 90).catch(() => null)))
      .then((res) => {
        const rows = [];
        res.forEach((r, i) => {
          const k = (r && r.kline) || [];
          if (k.length < 40) return;
          const closes = k.map((d) => Number(d.close)).filter((v) => Number.isFinite(v));
          if (closes.length < 40) return;
          const last = closes[closes.length - 1];
          const ret = last / closes[Math.max(0, closes.length - 21)] - 1;
          const daily = [];
          for (let j = 1; j < closes.length; j++) daily.push(closes[j] / closes[j - 1] - 1);
          const tail = daily.slice(-60);
          const mean = tail.reduce((a, b) => a + b, 0) / (tail.length || 1);
          const varc = tail.reduce((a, b) => a + (b - mean) * (b - mean), 0) / (tail.length || 1);
          const vol = Math.sqrt(varc) * Math.sqrt(244);
          const h60 = k.slice(-60).map((d) => Number(d.high)).filter(Number.isFinite);
          const l60 = k.slice(-60).map((d) => Number(d.low)).filter(Number.isFinite);
          const hi = Math.max(...h60), lo = Math.min(...l60);
          const pos = hi > lo ? (last - lo) / (hi - lo) : 0.5;
          const vols = k.map((d) => Number(d.volume)).filter(Number.isFinite);
          const v5 = vols.slice(-5).reduce((a, b) => a + b, 0) / 5;
          const v20 = vols.slice(-20).reduce((a, b) => a + b, 0) / 20;
          const vratio = v20 ? v5 / v20 : 1;
          let peak = -Infinity, mdd = 0;
          closes.slice(-60).forEach((c) => {
            peak = Math.max(peak, c);
            mdd = Math.min(mdd, (c - peak) / peak);
          });
          rows.push({
            code: holdings[i].code, name: holdings[i].name,
            mv: Number((this._sortedPositions || [])
              .find((p) => p.stock_code === holdings[i].code)?.market_value) || 0,
            '涨跌幅': ret, '波动率': vol, '价格位置': pos, '量能变化': vratio, '最大回撤': mdd,
          });
        });
        if (rows.length < 2) {
          body.innerHTML = '<div style="padding:24px;color:#9a9b96;font-size:12px">'
            + '行情不足，算不出画像</div>';
          return;
        }
        body.innerHTML = this._profileScatter(rows) + this._profileParallel(rows);
      })
      .catch(() => {
        body.innerHTML = '<div style="padding:24px;color:#9a9b96;font-size:12px">画像计算失败</div>';
      });
  }

  _fmtPct(v) { return (v * 100).toFixed(1) + '%'; }

  /** 四象限散点：涨跌幅 × 波动率，气泡大小 = 市值 */
  _profileScatter(rows) {
    const AX = '涨跌幅', AY = '波动率';
    const W = 900, H = 340, L = 84, R = 40, TOP = 36, BASE = 274;
    const xs = rows.map((r) => r[AX]), ys = rows.map((r) => r[AY]);
    const xmin = Math.min(...xs) * 1.18, xmax = Math.max(...xs) * 1.18;
    const ymin = Math.min(...ys) * 0.85, ymax = Math.max(...ys) * 1.12;
    const X = (v) => L + (v - xmin) / ((xmax - xmin) || 1) * (W - L - R);
    const Y = (v) => BASE - (v - ymin) / ((ymax - ymin) || 1) * (BASE - TOP);
    const med = (arr) => arr.slice().sort((a, b) => a - b)[Math.floor(arr.length / 2)];
    const mx = med(xs), my = med(ys);
    const maxMv = Math.max(...rows.map((r) => r.mv)) || 1;

    let s = `<line x1="${L}" y1="${BASE}" x2="${W - R}" y2="${BASE}" stroke="#B0AFA9"/>`;
    s += `<line x1="${L}" y1="${TOP}" x2="${L}" y2="${BASE}" stroke="#B0AFA9"/>`;
    s += `<line x1="${X(mx).toFixed(1)}" y1="${TOP}" x2="${X(mx).toFixed(1)}" y2="${BASE}"
      stroke="#E3E2DB" stroke-dasharray="4 4"/>`;
    s += `<line x1="${L}" y1="${Y(my).toFixed(1)}" x2="${W - R}" y2="${Y(my).toFixed(1)}"
      stroke="#E3E2DB" stroke-dasharray="4 4"/>`;
    s += `<text x="${L + 10}" y="${TOP + 16}" font-size="9.5" fill="#B45309" font-weight="700">跌 · 波动高</text>`;
    s += `<text x="${W - R - 10}" y="${TOP + 16}" font-size="9.5" fill="#9A9B96" text-anchor="end">涨得多 · 波动高</text>`;
    s += `<text x="${L + 10}" y="${BASE - 10}" font-size="9.5" fill="#9A9B96">跌 · 波动低</text>`;
    s += `<text x="${W - R - 10}" y="${BASE - 10}" font-size="9.5" fill="#176B5B"
      text-anchor="end" font-weight="700">涨得多 · 波动低（理想）</text>`;
    s += `<text x="${(L + W - R) / 2}" y="${BASE + 28}" font-size="10" fill="#6B7280"
      text-anchor="middle">近 20 日涨跌幅</text>`;
    s += `<text x="${L - 50}" y="${(TOP + BASE) / 2}" font-size="10" fill="#6B7280"
      text-anchor="middle" transform="rotate(-90 ${L - 50} ${(TOP + BASE) / 2})">年化波动率</text>`;

    rows.forEach((r) => {
      const cx = X(r[AX]), cy = Y(r[AY]);
      const rad = 5 + Math.sqrt(r.mv / maxMv) * 11;
      const good = r[AX] >= mx && r[AY] <= my;
      const bad = r[AX] < mx && r[AY] > my;
      const col = good ? '#176B5B' : bad ? '#B45309' : '#8F8E88';
      s += `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${rad.toFixed(1)}"
        fill="${col}" opacity=".26"/>`;
      s += `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${(rad * 0.42).toFixed(1)}"
        fill="${col}"/>`;
      s += `<text x="${cx.toFixed(1)}" y="${(cy - rad - 5).toFixed(1)}" font-size="9"
        fill="#4B5563" text-anchor="middle">${this._esc(r.name.slice(0, 7))}</text>`;
    });
    return `<div style="font-size:13px;font-weight:700;margin:6px 0 2px">四象限：谁涨得稳、谁跌得凶</div>`
      + `<div style="font-size:11px;color:#8a8b86;margin-bottom:6px">`
      + `虚线 = 横纵中位数 · 气泡大小 = 市值</div>`
      + `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${s}</svg>`;
  }

  /** 平行坐标：5 根轴，按横截面百分位归一；市值前三高亮 */
  _profileParallel(rows) {
    const AXES = ['涨跌幅', '波动率', '价格位置', '量能变化', '最大回撤'];
    const W = 900, H = 300, TOP = 46, BASE = 220, L = 60, R = 56;
    const xs = AXES.map((_, i) => L + i * ((W - L - R) / (AXES.length - 1)));
    const ranks = (vals) => {
      const idx = vals.map((v, i) => [v, i]).sort((a, b) => a[0] - b[0]);
      const out = new Array(vals.length);
      idx.forEach(([, i], k) => { out[i] = vals.length > 1 ? k / (vals.length - 1) : 0.5; });
      return out;
    };
    const norm = {};
    AXES.forEach((a) => { norm[a] = ranks(rows.map((r) => r[a])); });
    const top = rows.slice().sort((a, b) => b.mv - a.mv).slice(0, 3).map((r) => r.code);
    const COLORS = ['#176B5B', '#B45309', '#6D4BC4'];

    let s = '';
    AXES.forEach((a, i) => {
      s += `<line x1="${xs[i]}" y1="${TOP}" x2="${xs[i]}" y2="${BASE}" stroke="#DEDDD6"/>`;
      s += `<text x="${xs[i]}" y="${TOP - 22}" font-size="10" font-weight="700"
        fill="#1C1C1A" text-anchor="middle">${a}</text>`;
      const lo = Math.min(...rows.map((r) => r[a])), hi = Math.max(...rows.map((r) => r[a]));
      // 量能变化是倍数，不能和百分比用同一个格式化
      const f = a === '量能变化' ? ((v) => v.toFixed(2) + 'x') : ((v) => (v * 100).toFixed(1) + '%');
      s += `<text x="${xs[i]}" y="${TOP - 8}" font-size="8" fill="#9A9B96"
        text-anchor="middle">${f(hi)}</text>`;
      s += `<text x="${xs[i]}" y="${BASE + 14}" font-size="8" fill="#9A9B96"
        text-anchor="middle">${f(lo)}</text>`;
    });
    let ci = 0;
    rows.forEach((r) => {
      const pts = AXES.map((a, i) =>
        `${xs[i].toFixed(1)},${(BASE - norm[a][rows.indexOf(r)] * (BASE - TOP)).toFixed(1)}`).join(' ');
      const isTop = top.indexOf(r.code) >= 0;
      const col = isTop ? COLORS[ci++ % COLORS.length] : '#CFCEC7';
      s += `<polyline points="${pts}" fill="none" stroke="${col}"
        stroke-width="${isTop ? 2 : 1}" opacity="${isTop ? 0.95 : 0.72}"/>`;
      if (isTop) {
        const ly = BASE - norm[AXES[AXES.length - 1]][rows.indexOf(r)] * (BASE - TOP);
        s += `<text x="${xs[AXES.length - 1] + 6}" y="${(ly + 3).toFixed(1)}" font-size="9"
          font-weight="700" fill="${col}">${this._esc(r.name.slice(0, 6))}</text>`;
      }
    });
    return `<div style="border-top:1px solid #eeede9;margin:16px 0 8px"></div>`
      + `<div style="font-size:13px;font-weight:700;margin-bottom:2px">平行坐标：5 维一次看</div>`
      + `<div style="font-size:11px;color:#8a8b86;margin-bottom:6px">`
      + `每根轴按 ${rows.length} 只的百分位归一 · 深色 = 市值前三 · 其余浅灰</div>`
      + `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${s}</svg>`;
  }

  _corrMatrixSvg(labels, m) {
    const n = labels.length;
    const cell = Math.max(26, Math.min(40, Math.floor(620 / n)));
    const pad = 108;
    const W = pad + n * cell + 6, H = pad + n * cell + 6;
    let s = '';
    labels.forEach((t, j) => {
      const x = pad + j * cell + cell / 2;
      s += `<text x="${x}" y="${pad - 18}" font-size="8.5" fill="#6b7280" text-anchor="end"
        transform="rotate(-45 ${x} ${pad - 18})">${this._esc(t.slice(0, 7))}</text>`;
    });
    labels.forEach((t, i) => {
      s += `<text x="${pad - 8}" y="${pad + i * cell + cell / 2 + 3}" font-size="9"
        fill="#4b5563" text-anchor="end">${this._esc(t.length > 7 ? t.slice(0, 7) + '…' : t)}</text>`;
      for (let j = 0; j < n; j++) {
        const x = pad + j * cell, y = pad + i * cell;
        const v = m[i][j];
        if (i === j || v == null) {
          s += `<rect x="${x}" y="${y}" width="${cell - 2}" height="${cell - 2}" rx="2" fill="#efeeea"/>`;
          continue;
        }
        s += `<rect x="${x}" y="${y}" width="${cell - 2}" height="${cell - 2}" rx="2"
          fill="${this._corrColor(v)}"/>`;
        if (cell >= 30) {
          s += `<text x="${x + cell / 2 - 1}" y="${y + cell / 2 + 3}" font-size="8.5"
            fill="${Math.abs(v) > 0.55 ? '#fff' : '#3f3b33'}" text-anchor="middle">${v.toFixed(2).replace('0.', '.')}</text>`;
        }
      }
    });
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px;height:auto">${s}</svg>`
      + `<div style="font-size:10px;color:#9a9b96;letter-spacing:.06em;margin-top:6px">`
      + `蓝 = 负相关 · 琥珀 = 正相关 · 对角是自身</div>`;
  }

  _corrStrongList(labels, m, days) {
    const TH = 0.7, pairs = [];
    for (let i = 0; i < labels.length; i++) {
      for (let j = i + 1; j < labels.length; j++) {
        const v = m[i][j];
        if (v != null && Math.abs(v) >= TH) pairs.push({ v, a: labels[i], b: labels[j] });
      }
    }
    pairs.sort((x, y) => y.v - x.v);
    const head = `<div style="margin:16px 0 8px;font-size:12.5px;font-weight:700">`
      + `|r| ≥ ${TH} 的持仓对<span style="font-weight:400;color:#9a9b96;font-size:11px;margin-left:8px">`
      + `近 ${days} 个交易日 · 这些等于同一份风险买了两遍</span></div>`;
    if (!pairs.length) {
      return head + `<div style="font-size:11.5px;color:#9a9b96">没有 |r| ≥ ${TH} 的持仓对</div>`;
    }
    const rows = pairs.map((p) => {
      const w = (Math.abs(p.v) - TH) / (1 - TH) * 100;
      return `<div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-size:11px">`
        + `<div style="width:190px;text-align:right;color:#4b5563;flex:none;overflow:hidden;`
        + `text-overflow:ellipsis;white-space:nowrap">${this._esc(p.a)}</div>`
        + `<div style="flex:1;height:12px"><div style="width:${Math.max(4, w).toFixed(0)}%;`
        + `height:12px;background:#b45309;border-radius:0 6px 6px 0"></div></div>`
        + `<div style="width:190px;color:#4b5563;flex:none;overflow:hidden;`
        + `text-overflow:ellipsis;white-space:nowrap">${this._esc(p.b)}</div>`
        + `<div style="width:42px;text-align:right;font-weight:700;color:#1c1c1a;flex:none">`
        + `${p.v.toFixed(2)}</div></div>`;
    }).join('');
    return head + rows;
  }


  // ── 账户历史（ADR-008）：日历热力 + 构成流 ──────────────────────────────

  async _openAccountHistory(days = 90) {
    const api = window.StockRadar && window.StockRadar.api;
    if (!api || !api.getPortfolioSnapshots) return;
    let snap = { items: [] }, comp = { items: [] };
    try {
      const [a, b] = await Promise.all([
        api.getPortfolioSnapshots(days),
        api.getPortfolioSnapshotComposition(days).catch(() => ({ items: [] })),
      ]);
      snap = a || snap;
      comp = b || comp;
    } catch (e) {
      console.error('[账户历史] 读取失败', e);
    }
    const items = snap.items || [];
    const rows = comp.items || [];

    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.42);z-index:1350;'
      + 'display:flex;align-items:center;justify-content:center;padding:24px';
    const last = items.length ? items[items.length - 1] : null;
    const meta = last
      ? `已记录 ${items.length} 天 · 最后一条 ${this._esc(last.recorded_at)}`
        + `（${last.source === 'sync' ? '同步' : '打开'}时记录）`
      : '还没有任何记录';

    const rangeBtn = (d, label) => {
      const on = d === days;
      return `<button type="button" data-days="${d}" style="font-size:11px;padding:3px 10px;`
        + `border:1px solid ${on ? '#176b5b' : '#e3e2dd'};background:${on ? '#176b5b' : '#fff'};`
        + `border-radius:6px;color:${on ? '#fff' : '#6b7280'};cursor:pointer">${label}</button>`;
    };

    let bodyHtml;
    if (!items.length) {
      bodyHtml = `<div style="padding:34px 10px;text-align:center;color:#9a9b96;font-size:12.5px;`
        + `line-height:1.9">还没有记录。<br>打开持仓页或同步一次东方财富，就会开始记录当天快照。`
        + `<br><span style="font-size:11px">登录东财需要交易密码，无法自动同步，所以只能这样攒。</span></div>`;
    } else if (items.length < 2) {
      bodyHtml = this._ahCalendarSvg(items)
        + `<div style="margin-top:14px;padding:12px;background:#FAFAF8;border-radius:8px;`
        + `font-size:11.5px;color:#8a8b86">构成流至少要 2 天数据才能看出变化——现在有 1 天。</div>`;
    } else {
      bodyHtml = `<div data-p="cal">${this._ahCalendarSvg(items)}</div>`
        + `<div data-p="flow" style="display:none">${this._ahFlowSvg(rows)}</div>`;
    }

    ov.innerHTML = `<div style="background:#fff;border-radius:12px;width:min(960px,96vw);`
      + `max-height:88vh;overflow:auto;padding:18px 22px 16px;box-shadow:0 12px 40px rgba(0,0,0,.18)">`
      + `<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;flex-wrap:wrap">`
      + `<div style="font-size:16px;font-weight:700">账户历史</div>`
      + `<div style="margin-left:auto;display:flex;gap:4px">${rangeBtn(30, '近 30 天')}`
      + `${rangeBtn(90, '近 90 天')}${rangeBtn(3650, '全部')}</div>`
      + `<button type="button" data-act="close" style="border:1px solid #e3e2dd;background:#fff;`
      + `border-radius:6px;width:26px;height:26px;cursor:pointer;color:#6b7280;margin-left:6px">×</button></div>`
      + `<div style="font-size:11px;color:#8a8b86;margin-bottom:10px">${meta}</div>`
      + (items.length >= 2
        ? `<div id="pfHistTabs" style="display:flex;gap:4px;margin-bottom:12px">`
          + `<button type="button" data-k="cal" style="font-size:11.5px;padding:4px 14px;`
          + `border:1px solid #176b5b;background:#176b5b;border-radius:6px;color:#fff;cursor:pointer">日历热力</button>`
          + `<button type="button" data-k="flow" style="font-size:11.5px;padding:4px 14px;`
          + `border:1px solid #e3e2dd;background:#fff;border-radius:6px;color:#6b7280;cursor:pointer">构成流</button>`
          + `</div>`
        : '')
      + `<div id="pfHistBody">${bodyHtml}</div></div>`;

    const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
    ov.querySelector('button[data-act="close"]').onclick = close;
    document.addEventListener('keydown', onKey);
    ov.querySelectorAll('[data-days]').forEach((b) => {
      b.onclick = () => { close(); this._openAccountHistory(Number(b.dataset.days)); };
    });
    const tabs = ov.querySelector('#pfHistTabs');
    if (tabs) {
      tabs.querySelectorAll('button').forEach((b) => {
        b.onclick = () => {
          tabs.querySelectorAll('button').forEach((x) => {
            const on = x === b;
            x.style.background = on ? '#176b5b' : '#fff';
            x.style.borderColor = on ? '#176b5b' : '#e3e2dd';
            x.style.color = on ? '#fff' : '#6b7280';
          });
          ov.querySelectorAll('[data-p]').forEach((d) => {
            d.style.display = d.dataset.p === b.dataset.k ? '' : 'none';
          });
        };
      });
    }
    document.body.appendChild(ov);
  }

  /** 日历热力：一格一天，红赚绿亏，透明度表示幅度 */
  _ahCalendarSvg(items) {
    const byDate = {};
    items.forEach((it) => { byDate[it.snapshot_date] = it; });
    const dates = items.map((it) => it.snapshot_date).sort();
    const first = new Date(dates[0] + 'T00:00:00');
    const lastDate = new Date(dates[dates.length - 1] + 'T00:00:00');
    const dow0 = (first.getDay() + 6) % 7;
    const totalDays = Math.round((lastDate - first) / 86400000) + 1;
    const weeks = Math.ceil((totalDays + dow0) / 7);
    const chgs = items.map((it) => it.chg_pct).filter((v) => v != null);
    const maxAbs = Math.max(0.005, ...chgs.map(Math.abs));
    const cum = items.reduce((acc, it) => acc * (1 + (it.chg_pct || 0)), 1) - 1;

    const cell = 13, gap = 3, TOP = 56, L = 30;
    const W = Math.max(880, L + weeks * (cell + gap) + 20);
    const H = TOP + 7 * (cell + gap) + 22;
    const UP = '#E74C3C', DOWN = '#176B5B';
    let s = '';
    s += `<text x="${L}" y="26" font-size="26" font-weight="800" fill="${cum >= 0 ? UP : DOWN}">`
      + `${cum >= 0 ? '+' : ''}${(cum * 100).toFixed(1)}%</text>`;
    s += `<text x="${L + 96}" y="26" font-size="11" fill="#8a8b86">区间累计</text>`;
    const win = items.filter((it) => (it.chg_pct || 0) > 0).length;
    const lose = items.filter((it) => it.chg_pct != null && it.chg_pct < 0).length;
    const withPct = items.filter((it) => it.chg_pct != null).length;
    s += `<text x="${L + 430}" y="20" font-size="11" fill="#4b5563">赚钱 `
      + `<tspan font-weight="800" fill="${UP}">${win}</tspan> 天 / 共 ${withPct} 天</text>`;
    // 亏钱天数必须真的按 chg<0 数：首日没有前值（chg=null），不能算成亏钱
    s += `<text x="${L + 430}" y="37" font-size="11" fill="#4b5563">亏钱 `
      + `<tspan font-weight="800" fill="${DOWN}">${lose}</tspan> 天`
      + (withPct ? ` · 胜率 <tspan font-weight="800" fill="#1c1c1a">`
        + `${Math.round(win / withPct * 100)}%</tspan>` : '') + `</text>`;
    let best = null, worst = null;
    items.forEach((it) => {
      if (it.chg_pct == null) return;
      if (!best || it.chg_pct > best.chg_pct) best = it;
      if (!worst || it.chg_pct < worst.chg_pct) worst = it;
    });
    [['最好', best], ['最差', worst]].forEach(([lab, it], k) => {
      if (!it) return;
      const col = it.chg_pct >= 0 ? UP : DOWN;
      s += `<text x="${L + 640}" y="${20 + k * 17}" font-size="11" fill="#4b5563">${lab} `
        + `<tspan font-weight="800" fill="${col}">${(it.chg_pct * 100).toFixed(1)}%</tspan> `
        + `<tspan fill="#9a9b96">${this._esc(it.snapshot_date.slice(5))}</tspan></text>`;
    });
    let lastM = -1;
    for (let i = 0; i < totalDays; i++) {
      const d = new Date(first.getTime() + i * 86400000);
      const col = Math.floor((i + dow0) / 7);
      if (d.getMonth() !== lastM) {
        lastM = d.getMonth();
        s += `<text x="${L + col * (cell + gap)}" y="${TOP - 8}" font-size="8.5" fill="#9a9b96">`
          + `${d.getMonth() + 1}月</text>`;
      }
    }
    ['一', '三', '五'].forEach((w, k) => {
      s += `<text x="${L - 8}" y="${TOP + k * 2 * (cell + gap) + cell - 2}" font-size="8"`
        + ` fill="#b0afa9" text-anchor="end">${w}</text>`;
    });
    for (let i = 0; i < totalDays; i++) {
      const d = new Date(first.getTime() + i * 86400000);
      const key = d.toISOString().slice(0, 10);
      const it = byDate[key];
      const col = Math.floor((i + dow0) / 7), row = (i + dow0) % 7;
      const x = L + col * (cell + gap), y = TOP + row * (cell + gap);
      if (!it) {
        s += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" rx="2.5" fill="#F2F3F0"/>`;
        continue;
      }
      const pct = it.chg_pct;
      if (pct == null) {
        s += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" rx="2.5" fill="#EFEEEA"/>`;
        continue;
      }
      const a = Math.min(1, Math.abs(pct) / maxAbs * 1.15);
      s += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" rx="2.5"`
        + ` fill="${pct >= 0 ? UP : DOWN}" opacity="${(0.12 + a * 0.88).toFixed(2)}"/>`;
      if (best && it.snapshot_date === best.snapshot_date) {
        s += `<circle cx="${x + cell / 2}" cy="${y + cell / 2}" r="7" fill="none"`
          + ` stroke="${pct >= 0 ? UP : DOWN}" stroke-width="1.2"/>`;
      }
      if (worst && it.snapshot_date === worst.snapshot_date) {
        s += `<circle cx="${x + cell / 2}" cy="${y + cell / 2}" r="7" fill="none"`
          + ` stroke="${pct >= 0 ? UP : DOWN}" stroke-width="1.2"/>`;
      }
    }
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px;height:auto">${s}</svg>`
      + `<div style="font-size:10px;color:#9a9b96;letter-spacing:.05em;margin-top:8px">`
      + `RED = 赚钱 · GREEN = 亏钱 · 深 = 幅度大 · 空心圈 = 最好/最差单日 · 灰格 = 那天没记录</div>`;
  }

  /** 构成流：100% 堆叠，看结构不看规模 */
  _ahFlowSvg(rows) {
    if (!rows || !rows.length) {
      return '<div style="padding:24px;color:#9a9b96;font-size:12px">没有持仓明细</div>';
    }
    const dates = [...new Set(rows.map((r) => r.snapshot_date))].sort();
    if (dates.length < 2) {
      return '<div style="padding:24px;color:#9a9b96;font-size:12px">至少需要 2 天数据</div>';
    }
    // 每只标的的峰值，用来决定谁上色、谁并入「其他」
    const peak = {};
    rows.forEach((r) => {
      peak[r.stock_code] = Math.max(peak[r.stock_code] || 0, r.market_value || 0,
        peak[r.stock_code] ? 0 : (r.stock_name ? 1 : 0));
      peak[r.stock_code + '|n'] = r.stock_name || r.stock_code;
    });
    const ranked = Object.keys(peak).filter((k) => !k.includes('|'))
      .sort((a, b) => peak[b] - peak[a]);
    const top = ranked.slice(0, 6);
    const colOf = {};
    const CATS = ['#2f6f9f', '#6d4bc4', '#b45309', '#0891b2', '#a3556b', '#5b7db1'];
    top.forEach((c, i) => { colOf[c] = CATS[i]; });
    const nameOf = {};
    rows.forEach((r) => { nameOf[r.stock_code] = r.stock_name || r.stock_code; });

    // 每天：top6 各自占比 + 其他
    const series = {};
    top.forEach((c) => { series[c] = []; });
    series.__other = [];
    dates.forEach((d) => {
      const dayRows = rows.filter((r) => r.snapshot_date === d);
      const total = dayRows.reduce((a, r) => a + (r.market_value || 0), 0) || 1;
      const acc = {};
      dayRows.forEach((r) => {
        if (top.indexOf(r.stock_code) >= 0) {
          acc[r.stock_code] = (acc[r.stock_code] || 0) + (r.market_value || 0);
        } else {
          acc.__other = (acc.__other || 0) + (r.market_value || 0);
        }
      });
      top.forEach((c) => { series[c].push((acc[c] || 0) / total); });
      series.__other.push((acc.__other || 0) / total);
    });

    const W = 880, H = 250, L = 20, R = 130, TOP = 12, BASE = H - 26;
    const n = W - L - R, m = dates.length;
    const X = (i) => L + (i / (m - 1)) * n;
    const Y = (v) => BASE - v * (BASE - TOP);
    const order = top.slice();
    const stacks = [...order.map((c) => ({ key: c, vals: series[c], col: colOf[c] })),
      { key: '__other', vals: series.__other, col: '#c2c1ba' }];
    let s = '';
    const base = new Array(m).fill(0);
    stacks.forEach((st) => {
      const topArr = st.vals.map((v, i) => base[i] + v);
      const up = topArr.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
      const down = base.slice().reverse()
        .map((v, i) => `${X(m - 1 - i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
      s += `<polygon points="${up} ${down}" fill="${st.col}" opacity=".92"/>`;
      for (let i = 0; i < m; i++) base[i] = topArr[i];
    });
    // 图例：按最后一天占比降序
    const lastVals = stacks.map((st) => ({ st, v: st.vals[m - 1] }))
      .sort((a, b) => b.v - a.v);
    lastVals.forEach((o, j) => {
      const y = TOP + 14 + j * 20;
      s += `<rect x="${W - R + 10}" y="${y - 7}" width="9" height="9" rx="2" fill="${o.st.col}"/>`;
      s += `<text x="${W - R + 24}" y="${y}" font-size="9.5" fill="#4b5563">`
        + `${this._esc(o.st.key === '__other' ? '其他'
          : (nameOf[o.st.key] || o.st.key).slice(0, 7))}</text>`;
      s += `<text x="${W - 8}" y="${y}" font-size="9.5" font-weight="700" fill="#1c1c1a"`
        + ` text-anchor="end">${(o.v * 100).toFixed(0)}%</text>`;
    });
    s += `<text x="${L}" y="${BASE + 18}" font-size="9" fill="#9a9b96">${this._esc(dates[0].slice(5))}</text>`;
    s += `<text x="${L + n}" y="${BASE + 18}" font-size="9" fill="#9a9b96" text-anchor="end">`
      + `${this._esc(dates[m - 1].slice(5))}</text>`;
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${s}</svg>`
      + `<div style="font-size:10px;color:#9a9b96;letter-spacing:.05em;margin-top:6px">`
      + `100% = 当天总持仓 · 只看结构不看规模 · 占比最小的并入「其他」</div>`;
  }

  // 表头点击排序（数值列数字优先；文本列中文排序）
  _setSort(key) {
    if (this.sortKey === key) {
      this.sortDir = this.sortDir > 0 ? -1 : 0;
      if (this.sortDir === 0) { this.sortKey = ''; }
    } else {
      this.sortKey = key;
      this.sortDir = 1;
    }
    this._renderPositions();
  }

  _renderTrades() {
    const moreBtn = document.getElementById('pfMoreTradesBtn');
    moreBtn.style.display = this.trades.length < this.tradesTotal ? '' : 'none';
    if (!this.trades.length) {
      this.tradesBodyEl.innerHTML = '<div class="pf-empty">暂无交易流水</div>';
      return;
    }
    this.tradesBodyEl.innerHTML = this.trades.map((t) => `
      <div class="pf-trade-row">
        <span class="pf-trade-date">${this._esc(t.trade_date)}</span>
        <span>${this._esc(t.stock_name || t.stock_code)}<span class="pf-trade-code">${this._esc(t.stock_code)}</span></span>
        <span class="pf-trade-side ${t.side}">${t.side === 'buy' ? '买入' : '卖出'}</span>
        <span>${t.qty} 股</span>
        <span>@ ${this._fmtPrice(t.price)}</span>
        <span class="pf-trade-src">${this._esc(SRC_LABELS[t.source] || t.source)}</span>
        <button class="pf-btn pf-btn-del" data-uuid="${t.trade_uuid}" title="删除该笔并重算持仓">删</button>
      </div>`).join('');
    this.tradesBodyEl.querySelectorAll('.pf-btn-del').forEach((b) => {
      b.onclick = () => this._deleteTrade(b.dataset.uuid);
    });
  }

  // ── 操作 ────────────────────────────────────────────────────────────────

  _rowAction(act, code, name, cost) {
    if (act === 'watch') {
      window.StockRadar.api.addWatchlist({ stock_code: code, stock_name: name, market: 'cn' })
        .then(() => this._toast('已加入自选', 'ok'))
        .catch((e) => this._toast(`加自选失败：${e.message || e}`, 'error'));
      return;
    }
    if (act === 'monitor') {
      // 盯盘联动（链路 4）：直接预填监控规则（现价上穿 5% 涨幅提醒），一次请求创建
      window.StockRadar.api.addMonitorRule({
        stock_code: code, stock_name: name,
        rule_type: 'pct_above', threshold: 5, once_only: true,
      })
        .then(() => this._toast(`已创建盯盘规则：${name || code} 涨幅超 5% 提醒`, 'ok'))
        .catch((e) => this._toast(`创建盯盘规则失败：${e.message || e}`, 'error'));
      return;
    }
    if (act === 'kline') {
      // 链路 5：K 线图叠加成本线（紫色虚线；买卖点不做——流水不全，标了误导）
      if (window.openKlineChart) {
        const c = parseFloat(cost);
        window.openKlineChart(code, isFinite(c) && c > 0 ? { costLine: c } : undefined);
      } else {
        this._toast('K 线图未初始化', 'error');
      }
      return;
    }
    // buy/sell → 预填表单滚动到底部
    this._preFill = { code, name, side: act };
    document.getElementById('pfStockInput').value = code;
    document.getElementById('pfSideSelect').value = act;
    document.getElementById('pfQtyInput').focus();
    this.modalEl.querySelector('.pf-add-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  async _submitTrade() {
    const code = document.getElementById('pfStockInput').value.trim();
    const side = document.getElementById('pfSideSelect').value;
    const qty = parseFloat(document.getElementById('pfQtyInput').value);
    const price = parseFloat(document.getElementById('pfPriceInput').value);
    const tradeDate = document.getElementById('pfTradeDate').value;
    const fee = parseFloat(document.getElementById('pfFeeInput').value) || 0;
    const name = this._preFill && this._preFill.code === code ? this._preFill.name : '';

    if (!/^\d{6}$/.test(code)) { this._toast('请输入 6 位股票代码', 'warn'); return; }
    if (!isFinite(qty) || qty <= 0) { this._toast('请输入有效数量', 'warn'); return; }
    if (!isFinite(price) || price <= 0) { this._toast('请输入有效价格', 'warn'); return; }
    if (!tradeDate) { this._toast('请选择交易日期', 'warn'); return; }

    try {
      await window.StockRadar.api.addPortfolioTrade({
        stock_code: code, stock_name: name, side, qty, price,
        trade_date: tradeDate, fee,
      });
      this._toast(side === 'buy' ? '买入已记账' : '卖出已记账', 'ok');
      document.getElementById('pfQtyInput').value = '';
      document.getElementById('pfPriceInput').value = '';
      document.getElementById('pfFeeInput').value = '';
      this._preFill = null;
      // 回到第一页重新拉取：不重置的话分页 >1 时新流水看不到（或被 append 到尾部错序）
      this.trades = [];
      this.tradesPage = 1;
      await this._loadAll();
    } catch (e) {
      this._toast(`记账失败：${e.message || e}`, 'error');
    }
  }

  async _deleteTrade(uuid) {
    if (!confirm('删除该笔流水并重算持仓？（记错单更正用）')) return;
    try {
      await window.StockRadar.api.deletePortfolioTrade(uuid);
      this._toast('已删除并重算', 'ok');
      this.trades = [];
      this.tradesPage = 1;
      await this._loadAll();
    } catch (e) {
      this._toast(`删除失败：${e.message || e}`, 'error');
    }
  }

  // ── toast（复用盯盘面板的浮层宿主） ─────────────────────────────────────

  _toast(text, kind = 'info') {
    let host = document.getElementById('mmToastHost');
    if (!host) {
      host = document.createElement('div');
      host.id = 'mmToastHost';
      document.body.appendChild(host);
    }
    const t = document.createElement('div');
    t.className = `mm-toast mm-toast-${kind}`;
    t.textContent = text;
    host.appendChild(t);
    setTimeout(() => t.classList.add('show'), 10);
    setTimeout(() => {
      t.classList.remove('show');
      setTimeout(() => t.remove(), 300);
    }, 3000);
  }

  _esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
};
