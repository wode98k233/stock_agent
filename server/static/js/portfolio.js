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
    } catch (e) {
      this.summaryEl.innerHTML = `<span class="pf-error">持仓加载失败：${this._esc(e.message || e)}</span>`;
    }
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
        ? `<span class="${p.float_pnl_pct >= 0 ? 'pf-up' : 'pf-down'}">${p.float_pnl_pct >= 0 ? '+' : ''}${p.float_pnl_pct.toFixed(2)}%</span>`
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
      </div>${rows}`;
    this.positionsBodyEl.querySelectorAll('.pf-btn').forEach((b) => {
      b.onclick = () => this._rowAction(b.dataset.act, b.dataset.code, b.dataset.name, b.dataset.cost);
    });
    this.positionsBodyEl.querySelectorAll('.pf-pos-head span[data-sort]').forEach((h) => {
      h.onclick = () => this._setSort(h.dataset.sort);
    });
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
