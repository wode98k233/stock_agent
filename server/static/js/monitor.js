/*
   选股雷达 Web - 盯盘告警面板
   结构：状态条（运行状态/轮询信息/通知引导）+ 规则管理 + 告警历史
   后台：MARKET_MONITOR_ENABLED 开启后调度器盘中轮询自选股，命中规则经通知渠道推送
*/

window.StockRadar = window.StockRadar || {};

window.StockRadar.MarketMonitorPanel = class MarketMonitorPanel {
  constructor() {
    this.overlayEl = document.getElementById('mmOverlay');
    this.modalEl = document.getElementById('mmModal');
    this.closeBtn = document.getElementById('mmCloseBtn');
    this.statusBarEl = document.getElementById('mmStatusBar');
    this.rulesBodyEl = document.getElementById('mmRulesBody');
    this.alertsBodyEl = document.getElementById('mmAlertsBody');
    this.moreBtn = document.getElementById('mmMoreBtn');

    this.rules = [];
    this.alerts = [];
    this.alertsPage = 1;
    this.alertsTotal = 0;
    this.watchlist = [];

    // 未读告警轮询（toast）状态
    this._toastSeenMaxId = 0;
    this._toastTimer = null;

    this._bindEvents();
    this._startUnreadPolling();
  }

  open() {
    this.overlayEl.classList.add('open');
    this.modalEl.classList.add('open');
    this._refreshAll();
  }

  close() {
    this.overlayEl.classList.remove('open');
    this.modalEl.classList.remove('open');
  }

  _bindEvents() {
    this.closeBtn.onclick = () => this.close();
    this.overlayEl.onclick = () => this.close();
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.modalEl.classList.contains('open')) this.close();
    });
    document.getElementById('mmAddBtn').onclick = () => this._addRule();
    document.getElementById('mmTestBtn').onclick = () => this._sendTest();
    document.getElementById('mmReadAllBtn').onclick = () => this._markAllRead();
    const clearBtn = document.getElementById('mmClearAlertsBtn');
    if (clearBtn) clearBtn.onclick = () => this._clearAlerts();
    this.moreBtn.onclick = () => {
      this.alertsPage += 1;
      this._loadAlerts();
    };
  }

  // ── 数据加载 ────────────────────────────────────────────────────────────

  async _refreshAll() {
    await Promise.all([this._loadStatus(), this._loadRules(), this._loadWatchlist()]);
    this.alertsPage = 1;
    this.alerts = [];
    await this._loadAlerts();
  }

  async _loadStatus() {
    try {
      const s = await window.StockRadar.api.getMonitorStatus();
      this._renderStatus(s);
    } catch {
      this.statusBarEl.innerHTML = '<span class="mm-status-error">状态获取失败</span>';
    }
  }

  async _loadWatchlist() {
    try {
      const items = await window.StockRadar.api.getWatchlist();
      this.watchlist = (items || []).filter((s) => (s.market || 'cn') === 'cn');
    } catch {
      this.watchlist = [];
    }
    this._renderStockOptions();
  }

  async _loadRules() {
    try {
      const data = await window.StockRadar.api.getMonitorRules();
      this.rules = data.items || [];
    } catch {
      this.rules = [];
    }
    this._renderRules();
  }

  async _loadAlerts() {
    try {
      const data = await window.StockRadar.api.getMonitorAlerts(this.alertsPage, 20);
      const items = data.items || [];
      this.alertsTotal = data.total || 0;
      // 追加去重（翻页时旧数据保留）
      const seen = new Set(this.alerts.map((a) => a.id));
      this.alerts = this.alerts.concat(items.filter((a) => !seen.has(a.id)));
      // 面板打开即视为已查看：记录当前最大 id 供 toast 去重
      for (const a of items) this._toastSeenMaxId = Math.max(this._toastSeenMaxId, a.id);
    } catch {
      /* 保留旧数据 */
    }
    this._renderAlerts();
  }

  // ── 渲染 ────────────────────────────────────────────────────────────────

  _renderStatus(s) {
    const runBadge = s.running
      ? '<span class="mm-badge mm-badge-on">运行中</span>'
      : '<span class="mm-badge mm-badge-off">未启动</span>';
    const parts = [
      runBadge,
      `<span>轮询间隔 ${this._esc(s.interval_s ?? '-')}s</span>`,
      `<span>盯盘标的 ${this._esc(s.watchlist_count ?? 0)} 只</span>`,
      `<span>启用规则 ${this._esc(s.enabled_rules ?? 0)} 条</span>`,
    ];
    if (s.last_poll_at) parts.push(`<span>上次轮询 ${this._esc(s.last_poll_at)}</span>`);
    if (s.running && s.fail_streak > 0) parts.push(`<span class="mm-warn">连续失败 ${s.fail_streak} 轮</span>`);
    let html = parts.join('<span class="mm-dot">·</span>');
    if (!s.notification_enabled) {
      html += `<div class="mm-notice">未开启通知渠道（NOTIFICATION_ENABLED=false）：告警仅落库，不会推送。可到「系统配置 → 通知」开启后点「测试通知」验证。</div>`;
    }
    this.statusBarEl.innerHTML = html;
  }

  _renderStockOptions() {
    const sel = document.getElementById('mmStockSelect');
    sel.innerHTML = '<option value="">— 选择自选股 —</option>';
    for (const s of this.watchlist) {
      const opt = document.createElement('option');
      opt.value = s.stock_code;
      opt.textContent = `${s.stock_name || s.stock_code}（${s.stock_code}）`;
      sel.appendChild(opt);
    }
    // 支持手动输入不在自选股的代码
    const manual = document.createElement('option');
    manual.value = '__manual__';
    manual.textContent = '手动输入代码…';
    sel.appendChild(manual);
  }

  _renderRules() {
    if (!this.rules.length) {
      this.rulesBodyEl.innerHTML = '<div class="mm-empty">暂无规则：从上方选择自选股并添加盯盘规则</div>';
      return;
    }
    const rows = this.rules.map((r) => {
      const typeLabel = this.constructor.TYPE_LABELS[r.rule_type] || r.rule_type;
      const unit = r.rule_type.startsWith('pct') ? '%' : ' 元';
      const priceCell = r.price != null ? r.price.toFixed(2) : '-';
      let distCell = '-';
      if (r.distance != null) {
        distCell = r.distance > 0
          ? `距触发 ${r.distance.toFixed(2)}${r.rule_type.startsWith('pct') ? '%' : ''}`
          : '<span class="mm-crossed">已越线</span>';
      }
      return `
        <div class="mm-rule-row${r.enabled ? '' : ' mm-rule-disabled'}" data-id="${r.id}">
          <div class="mm-rule-stock">
            <div class="mm-rule-name">${this._esc(r.stock_name || r.stock_code)}</div>
            <div class="mm-rule-code">${this._esc(r.stock_code)}</div>
          </div>
          <div class="mm-rule-type">${this._esc(typeLabel)}<span class="mm-rule-th">${r.threshold}${unit}</span></div>
          <div class="mm-rule-price">${priceCell}</div>
          <div class="mm-rule-dist">${distCell}</div>
          <div class="mm-rule-once">${r.once_only ? '一次性' : '持续'}</div>
          <div class="mm-rule-ops">
            <button class="mm-btn mm-btn-toggle" data-id="${r.id}" data-enabled="${r.enabled}" title="${r.enabled ? '停用' : '启用'}">${r.enabled ? '停用' : '启用'}</button>
            <button class="mm-btn mm-btn-del" data-id="${r.id}" title="删除">删除</button>
          </div>
        </div>`;
    }).join('');
    this.rulesBodyEl.innerHTML = `<div class="mm-rule-head">
        <span>股票</span><span>规则</span><span>现价</span><span>状态</span><span>模式</span><span></span>
      </div>${rows}`;
    this.rulesBodyEl.querySelectorAll('.mm-btn-toggle').forEach((b) => {
      b.onclick = () => this._toggleRule(Number(b.dataset.id), b.dataset.enabled === '1');
    });
    this.rulesBodyEl.querySelectorAll('.mm-btn-del').forEach((b) => {
      b.onclick = () => this._deleteRule(Number(b.dataset.id));
    });
  }

  _renderAlerts() {
    this.moreBtn.style.display = this.alerts.length < this.alertsTotal ? '' : 'none';
    if (!this.alerts.length) {
      this.alertsBodyEl.innerHTML = '<div class="mm-empty">暂无告警记录</div>';
      return;
    }
    this.alertsBodyEl.innerHTML = this.alerts.map((a) => {
      const pct = a.pct != null ? `${a.pct > 0 ? '+' : ''}${a.pct.toFixed(2)}%` : '';
      const pctCls = a.pct > 0 ? 'mm-up' : (a.pct < 0 ? 'mm-down' : '');
      return `
        <div class="mm-alert-row${a.read ? '' : ' mm-alert-unread'}">
          <div class="mm-alert-main">
            <div class="mm-alert-desc">${this._esc(a.rule_desc)}</div>
            <div class="mm-alert-meta">${this._esc(a.triggered_at)} · 现价 ${a.price ?? '-'} ${pct ? `<span class="${pctCls}">${pct}</span>` : ''}</div>
          </div>
          ${a.channels_ok ? `<span class="mm-alert-ch" title="已推送渠道">已推送 ${this._esc(a.channels_ok)}</span>` : '<span class="mm-alert-ch mm-alert-nopush" title="未推送（通知未开启或全部失败）">未推送</span>'}
        </div>`;
    }).join('');
  }

  // ── 操作 ────────────────────────────────────────────────────────────────

  async _addRule() {
    const sel = document.getElementById('mmStockSelect');
    const typeSel = document.getElementById('mmTypeSelect');
    const thresholdInput = document.getElementById('mmThresholdInput');
    const onceSel = document.getElementById('mmOnceSelect');

    let code = sel.value;
    let name = '';
    if (code === '__manual__') {
      code = (prompt('输入 6 位股票代码：') || '').trim();
    } else if (code) {
      const stock = this.watchlist.find((s) => s.stock_code === code);
      name = stock ? (stock.stock_name || '') : '';
    }
    if (!/^\d{6}$/.test(code)) { this._toast('请选择自选股或输入 6 位代码', 'warn'); return; }
    const threshold = parseFloat(thresholdInput.value);
    if (!isFinite(threshold)) { this._toast('请输入有效阈值', 'warn'); return; }

    try {
      await window.StockRadar.api.addMonitorRule({
        stock_code: code, stock_name: name,
        rule_type: typeSel.value, threshold,
        once_only: onceSel.value === 'once',
      });
      thresholdInput.value = '';
      this._toast('规则已添加', 'ok');
      this._loadRules();
      this._loadStatus();
    } catch (e) {
      this._toast(`添加失败：${e.message || e}`, 'error');
    }
  }

  async _toggleRule(id, enabled) {
    try {
      await window.StockRadar.api.patchMonitorRule(id, { enabled: !enabled });
      this._loadRules();
      this._loadStatus();
    } catch (e) {
      this._toast(`操作失败：${e.message || e}`, 'error');
    }
  }

  async _deleteRule(id) {
    if (!confirm('删除该规则？')) return;
    try {
      await window.StockRadar.api.deleteMonitorRule(id);
      this._toast('规则已删除', 'ok');
      this._loadRules();
      this._loadStatus();
    } catch (e) {
      this._toast(`删除失败：${e.message || e}`, 'error');
    }
  }

  async _markAllRead() {
    try {
      await window.StockRadar.api.markMonitorAlertsRead({ ids: [], all: true });
      this.alerts = this.alerts.map((a) => ({ ...a, read: 1 }));
      this._renderAlerts();
    } catch { /* 静默 */ }
  }

  async _clearAlerts() {
    if (!this.alerts.length && !this.alertsTotal) return;
    if (!confirm('清空全部告警历史？（不可恢复，不影响已有规则）')) return;
    try {
      await window.StockRadar.api.clearMonitorAlerts();
      this.alerts = [];
      this.alertsPage = 1;
      this.alertsTotal = 0;
      // 清空后自增 id 从 1 重新计，未读轮询的基线同步归零，否则新告警不再弹
      this._toastSeenMaxId = 0;
      this._renderAlerts();
      this._loadStatus();
      this._toast('告警历史已清空', 'ok');
    } catch (e) {
      this._toast(`清空失败：${e.message || e}`, 'error');
    }
  }

  async _sendTest() {
    this._toast('测试通知发送中…');
    try {
      const r = await window.StockRadar.api.sendMonitorTest();
      if (r.success) {
        this._toast(r.channels_ok?.length ? `测试成功：${r.channels_ok.join('、')}` : '已发送（无渠道成功，请检查配置）', r.channels_ok?.length ? 'ok' : 'warn');
      } else {
        this._toast(`测试失败：${r.error || '未知错误'}`, 'error');
      }
    } catch (e) {
      this._toast(`测试失败：${e.message || e}`, 'error');
    }
  }

  // ── 未读告警轮询 + toast ────────────────────────────────────────────────

  _startUnreadPolling() {
    const poll = async () => {
      // 页面不可见（后台标签页）时跳过轮询，避免无意义请求与日志噪音
      if (document.hidden) return;
      try {
        const data = await window.StockRadar.api.getMonitorAlerts(1, 10, true);
        const items = data.items || [];
        const fresh = items.filter((a) => a.id > this._toastSeenMaxId);
        for (const a of fresh) {
          this._toastSeenMaxId = Math.max(this._toastSeenMaxId, a.id);
          this._toast(`盯盘提醒：${a.rule_desc}`, 'alert');
        }
        // 首次运行只记录基线，不弹历史告警
      } catch { /* 网络异常静默 */ }
    };
    this._toastTimer = setInterval(poll, 60000);
  }

  // ── 轻量 toast（面板内操作反馈 + 告警提醒共用） ──────────────────────────

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
    }, kind === 'alert' ? 8000 : 3000);
  }

  _esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
};

// 规则类型 → 中文标签（与后端 rules.py RULE_TYPE_LABELS 对齐）
window.StockRadar.MarketMonitorPanel.TYPE_LABELS = {
  price_above: '价格上穿',
  price_below: '价格下穿',
  pct_above: '涨幅超过',
  pct_below: '跌幅超过',
  pct_change: '振幅超过',
};
