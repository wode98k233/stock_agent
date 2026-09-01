/*
   选股雷达 Web - 自选股管理组件
*/

window.StockRadar = window.StockRadar || {};

window.StockRadar.WatchlistPanel = class WatchlistPanel {
  constructor() {
    this.overlayEl = document.getElementById('watchlistOverlay');
    this.modalEl = document.getElementById('watchlistModal');
    this.bodyEl = document.getElementById('watchlistBody');
    this.countEl = document.getElementById('watchlistCount');
    this.addInput = document.getElementById('watchlistAddInput');
    this.addBtn = document.getElementById('watchlistAddBtn');
    this.syncBtn = document.getElementById('watchlistSyncBtn');
    this.refreshBtn = document.getElementById('watchlistRefreshBtn');
    this.sourceDropdown = document.getElementById('wlSourceDropdown');
    this.sourceTrigger = document.getElementById('wlSourceTrigger');
    this.sourceLabel = document.getElementById('wlSourceLabel');

    this.items = [];
    this.quotes = {};
    this.selectedSource = 'auto';
    this.selectedFilter = 'all';
    this.dataSources = [];
    this.lastRefreshAt = null;

    this._bindEvents();
    this._loadDataSources();
  }

  open() {
    this.overlayEl.classList.add('open');
    this.modalEl.classList.add('open');
    this._load();
  }

  close() {
    this.overlayEl.classList.remove('open');
    this.modalEl.classList.remove('open');
    this._closeSourceDropdown();
  }

  _bindEvents() {
    document.getElementById('watchlistCloseBtn').onclick = () => this.close();
    this.overlayEl.onclick = () => this.close();
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.modalEl.classList.contains('open')) this.close();
    });

    this.addBtn.onclick = () => this._addStock();
    this.addInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') this._addStock();
    });
    this.syncBtn.onclick = () => this._syncFromMX();
    this.refreshBtn.onclick = () => this._refreshAll();

    this.sourceTrigger.addEventListener('click', () => this._toggleSourceDropdown());
    this.sourceTrigger.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        this._toggleSourceDropdown();
      }
    });
    document.addEventListener('click', (e) => {
      if (!document.getElementById('wlSourceSelector').contains(e.target)) this._closeSourceDropdown();
    });
  }

  async _loadDataSources() {
    try {
      const data = await window.StockRadar.api.getDataSources();
      this.dataSources = data.sources || [];
    } catch {
      this.dataSources = [];
    }
    this._renderSourceDropdown();
  }

  _renderSourceDropdown() {
    this.sourceDropdown.innerHTML = '';
    this.sourceDropdown.appendChild(this._createSourceOption('auto', '自动', '按优先级自动选择数据源'));
    for (const ds of this.dataSources) {
      if (!ds.available) continue;
      this.sourceDropdown.appendChild(this._createSourceOption(ds.name, ds.label || ds.name, ds.description || ''));
    }
  }

  _createSourceOption(value, label, desc) {
    const opt = document.createElement('div');
    opt.className = 'wl-source-option' + (value === this.selectedSource ? ' active' : '');
    opt.innerHTML = `
      <div class="wl-source-option-main">
        <div class="wl-source-option-name">${this._esc(label)}</div>
        ${desc ? `<div class="wl-source-option-desc">${this._esc(desc)}</div>` : ''}
      </div>
      <svg class="wl-source-option-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
    opt.addEventListener('click', () => {
      this.selectedSource = value;
      this.sourceLabel.textContent = label;
      this._closeSourceDropdown();
      this._renderSourceDropdown();
    });
    return opt;
  }

  _toggleSourceDropdown() {
    this.sourceDropdown.classList.contains('open') ? this._closeSourceDropdown() : this._openSourceDropdown();
  }

  _openSourceDropdown() {
    this.sourceDropdown.classList.add('open');
    this.sourceTrigger.setAttribute('aria-expanded', 'true');
  }

  _closeSourceDropdown() {
    this.sourceDropdown.classList.remove('open');
    this.sourceTrigger.setAttribute('aria-expanded', 'false');
  }

  async _load() {
    try {
      this.items = await window.StockRadar.api.getWatchlist();
    } catch {
      this.items = [];
    }
    this._render();
  }

  _render() {
    this.bodyEl.innerHTML = '';
    this.bodyEl.className = 'watchlist-body watchlist-workspace';
    this.countEl.textContent = this.items.length;

    this.bodyEl.appendChild(this._renderSummary());
    this.bodyEl.appendChild(this._renderFilters());

    if (this.items.length === 0) {
      this.bodyEl.appendChild(this._renderEmpty());
      return;
    }

    this.bodyEl.appendChild(this._renderTableHead());
    const list = document.createElement('div');
    list.className = 'watchlist-list';
    for (const item of this._filteredItems()) {
      list.appendChild(this._renderItem(item));
    }
    if (list.children.length === 0) {
      list.appendChild(this._renderEmpty('当前筛选下暂无股票'));
    }
    this.bodyEl.appendChild(list);
  }

  _renderSummary() {
    const summary = this._portfolioStats();
    const el = document.createElement('div');
    el.className = 'watchlist-summary';
    el.innerHTML = `
      <div class="watchlist-summary-card">
        <span>自选数量</span><strong>${this.items.length}</strong>
      </div>
      <div class="watchlist-summary-card up">
        <span>上涨</span><strong>${summary.up}</strong>
      </div>
      <div class="watchlist-summary-card down">
        <span>下跌</span><strong>${summary.down}</strong>
      </div>
      <div class="watchlist-summary-card ${summary.avg >= 0 ? 'up' : 'down'}">
        <span>平均</span><strong>${summary.avg == null ? '-' : this._signed(summary.avg) + '%'}</strong>
      </div>
      <div class="watchlist-summary-card">
        <span>最近刷新</span><strong>${this.lastRefreshAt || '-'}</strong>
      </div>`;
    return el;
  }

  _renderFilters() {
    const filters = [
      ['all', '全部'],
      ['up', '上涨'],
      ['down', '下跌'],
      ['high_turnover', '高换手'],
      ['low_pe', '低估值'],
      ['no_quote', '无行情'],
    ];
    const wrap = document.createElement('div');
    wrap.className = 'watchlist-filter-tabs';
    filters.forEach(([value, label]) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'watchlist-filter-chip' + (this.selectedFilter === value ? ' active' : '');
      btn.textContent = label;
      btn.addEventListener('click', () => {
        this.selectedFilter = value;
        this._render();
      });
      wrap.appendChild(btn);
    });
    return wrap;
  }

  _renderTableHead() {
    const head = document.createElement('div');
    head.className = 'watchlist-table-head';
    head.innerHTML = `
      <span>代码</span>
      <span>名称</span>
      <span class="num">最新价</span>
      <span class="num">涨跌幅</span>
      <span class="num optional">PE</span>
      <span class="num optional">PB</span>
      <span class="num">操作</span>`;
    return head;
  }

  _renderEmpty(text) {
    const empty = document.createElement('div');
    empty.className = 'watchlist-empty';
    empty.innerHTML = `
      <div class="watchlist-empty-icon">□</div>
      <div class="watchlist-empty-text">${this._esc(text || '暂无自选股')}<br>在下方输入股票代码添加，或从 MX 同步</div>`;
    return empty;
  }

  _filteredItems() {
    return this.items.filter((item) => {
      const q = this.quotes[item.stock_code];
      const change = this._num(q ? q.change_pct : item.change_pct);
      const pe = this._num(item.pe || (q && q.pe));
      const turnover = this._num(item.turnover_rate || (q && q.turnover_rate));
      const hasQuote = (q && q.price != null) || item.price != null;
      if (this.selectedFilter === 'up') return change > 0;
      if (this.selectedFilter === 'down') return change < 0;
      if (this.selectedFilter === 'high_turnover') return turnover >= 5;
      if (this.selectedFilter === 'low_pe') return pe > 0 && pe <= 15;
      if (this.selectedFilter === 'no_quote') return !hasQuote;
      return true;
    });
  }

  _renderItem(item) {
    const row = document.createElement('div');
    row.className = 'watchlist-item';
    row.dataset.code = item.stock_code;

    const q = this.quotes[item.stock_code];
    const price = q ? q.price : item.price;
    const chgPct = q ? q.change_pct : item.change_pct;
    const chgAmt = q ? q.change_amt : item.change_amt;
    const pe = this._fmtNum(item.pe || (q && q.pe));
    const pb = this._fmtNum(item.pb || (q && q.pb));

    // 股票代码 - 点击打开 K 线图
    const codeEl = Object.assign(document.createElement('div'), {
      className: 'watchlist-item-code watchlist-clickable',
      textContent: item.stock_code,
    });
    codeEl.title = '点击查看 K 线图';
    codeEl.onclick = (e) => {
      e.stopPropagation();
      this.close();
      if (window.openKlineChart) {
        window.openKlineChart(item.stock_code);
      }
    };
    row.appendChild(codeEl);

    const info = document.createElement('div');
    info.className = 'watchlist-item-info';
    const name = document.createElement('div');
    name.className = 'watchlist-item-name watchlist-clickable';
    name.textContent = item.stock_name || (q && q.name) || '-';
    name.title = `${name.textContent} - 点击查看 K 线图`;
    name.onclick = (e) => {
      e.stopPropagation();
      this.close();
      if (window.openKlineChart) {
        window.openKlineChart(item.stock_code);
      }
    };
    const tags = document.createElement('div');
    tags.className = 'watchlist-stock-tags';
    if (item.market_short) tags.appendChild(this._tag(item.market_short));
    tags.appendChild(this._tag(item.source === 'mx' ? '妙想' : '自定义', item.source === 'mx' ? 'mx' : 'local'));
    const turnover = item.turnover_rate || (q && q.turnover_rate);
    if (turnover != null) tags.appendChild(this._tag(`换手 ${this._fmtPct(turnover)}`));
    info.appendChild(name);
    info.appendChild(tags);
    row.appendChild(info);

    row.appendChild(this._numberCell(price != null ? Number(price).toFixed(2) : '-', chgAmt != null ? this._signed(chgAmt) : null));
    row.appendChild(this._changeCell(chgPct));
    row.appendChild(this._plainNumberCell(pe || '-'));
    row.appendChild(this._plainNumberCell(pb || '-'));

    const actions = document.createElement('div');
    actions.className = 'watchlist-row-actions';
    const refBtn = document.createElement('button');
    refBtn.type = 'button';
    refBtn.title = '刷新行情';
    refBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';
    refBtn.onclick = () => this._refreshOne(item.stock_code, refBtn);
    actions.appendChild(refBtn);

    const del = document.createElement('button');
    del.type = 'button';
    del.title = '删除';
    del.textContent = '×';
    del.onclick = () => this._removeStock(item.stock_code, item.market || 'cn');
    actions.appendChild(del);
    row.appendChild(actions);
    return row;
  }

  _tag(text, type) {
    const tag = document.createElement('span');
    tag.className = 'watchlist-mini-tag' + (type ? ` ${type}` : '');
    tag.textContent = text;
    return tag;
  }

  _numberCell(main, sub) {
    const cell = document.createElement('div');
    cell.className = 'watchlist-number-cell';
    cell.innerHTML = `<span>${this._esc(main)}</span>${sub ? `<small>${this._esc(sub)}</small>` : ''}`;
    return cell;
  }

  _plainNumberCell(value) {
    const cell = document.createElement('div');
    cell.className = 'watchlist-number-cell optional';
    cell.innerHTML = `<span>${this._esc(value)}</span>`;
    return cell;
  }

  _changeCell(chgPct) {
    const cell = document.createElement('div');
    const value = this._num(chgPct);
    cell.className = 'watchlist-change-cell';
    if (value > 0) cell.classList.add('up');
    if (value < 0) cell.classList.add('down');
    cell.textContent = value == null ? '-' : `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
    return cell;
  }

  async _refreshOne(code, btn) {
    if (btn) btn.classList.add('spinning');
    try {
      const result = await window.StockRadar.api.getWatchlistQuote(code, this.selectedSource);
      if (result.quotes) {
        const q = result.quotes[code] || Object.values(result.quotes)[0];
        if (q) {
          this.quotes[code] = q;
          this.lastRefreshAt = this._nowTime();
          this._render();
        }
      }
    } catch (e) {
      console.warn('刷新失败:', code, e);
    }
    if (btn) btn.classList.remove('spinning');
  }

  async _refreshAll() {
    if (this.items.length === 0) return;
    this.refreshBtn.classList.add('refreshing');
    try {
      const result = await window.StockRadar.api.getWatchlistQuotes(
        this.items.map(i => i.stock_code), this.selectedSource
      );
      if (result.quotes) {
        Object.assign(this.quotes, result.quotes);
        this.lastRefreshAt = this._nowTime();
        this._render();
      }
    } catch (e) {
      console.warn('刷新行情失败:', e);
    }
    this.refreshBtn.classList.remove('refreshing');
  }

  async _addStock() {
    const code = this.addInput.value.trim();
    if (!code) return;
    this.addBtn.disabled = true;
    try {
      await window.StockRadar.api.addWatchlist(code, '', 'cn');
      this.addInput.value = '';
      await this._load();
      await this._refreshOne(code, this.refreshBtn);
    } catch (e) {
      alert('添加失败: ' + e.message);
    }
    this.addBtn.disabled = false;
  }

  async _removeStock(code, market) {
    try {
      await window.StockRadar.api.deleteWatchlist(code, market);
      delete this.quotes[code];
      await this._load();
    } catch (e) {
      alert('删除失败: ' + e.message);
    }
  }

  async _syncFromMX() {
    this.syncBtn.classList.add('syncing');
    try {
      const result = await window.StockRadar.api.syncWatchlist();
      if (result.success) {
        await this._load();
      } else {
        alert('同步失败: ' + (result.error || '未知错误'));
      }
    } catch (e) {
      alert('同步失败: ' + e.message);
    }
    this.syncBtn.classList.remove('syncing');
  }

  _portfolioStats() {
    let up = 0;
    let down = 0;
    let sum = 0;
    let count = 0;
    for (const item of this.items) {
      const q = this.quotes[item.stock_code];
      const chg = this._num(q ? q.change_pct : item.change_pct);
      if (chg == null) continue;
      if (chg > 0) up++;
      if (chg < 0) down++;
      sum += chg;
      count++;
    }
    return { up, down, avg: count > 0 ? sum / count : null };
  }

  _nowTime() {
    const d = new Date();
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  _num(v) {
    if (v == null || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  _signed(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return '-';
    return `${n > 0 ? '+' : ''}${n.toFixed(2)}`;
  }

  _esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  _fmtNum(v) { return v != null && v !== '' ? Number(v).toFixed(2) : null; }
  _fmtPct(v) { return v != null && v !== '' ? Number(v).toFixed(2) + '%' : null; }
};
