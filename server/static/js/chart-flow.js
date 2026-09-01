/**
 * 资金流向面板
 *
 * 板块资金流热力图 + 表格、个股资金流详情、北向资金。
 * 数据来源：同花顺问财（通过后端 API 代理）。
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const api = () => window.StockRadar?.api;

  class FlowPanel {
    constructor() {
      this.overlay = $('flowOverlay');
      this.modal = $('flowModal');
      this.body = $('flowBody');
      this.chart = null;
      this.currentTab = 'sector';
      this._bind();
    }

    _bind() {
      $('flowCloseBtn')?.addEventListener('click', (e) => { e.stopPropagation(); this.hide(); });
      this.overlay?.addEventListener('click', () => this.hide());
      $('flowRefreshBtn')?.addEventListener('click', (e) => { e.stopPropagation(); this._load(); });
      $('flowTabs')?.addEventListener('click', (e) => {
        const tab = e.target.closest('.flow-tab');
        if (!tab) return;
        this.currentTab = tab.dataset.tab;
        this._setActiveTab();
        this._load();
      });
    }

    show() {
      this.overlay?.classList.add('open');
      this.modal?.classList.add('open');
      this._load();
    }

    hide() {
      this.overlay?.classList.remove('open');
      this.modal?.classList.remove('open');
    }

    _setActiveTab() {
      $('flowTabs')?.querySelectorAll('.flow-tab').forEach((t) => {
        t.classList.toggle('active', t.dataset.tab === this.currentTab);
      });
    }

    async _load() {
      if (!this.body) return;
      this.body.innerHTML = '<div class="flow-loading">加载中…</div>';
      try {
        const _api = api();
        if (!_api) { this.body.innerHTML = '<div class="flow-empty">API 未就绪</div>'; return; }

        if (this.currentTab === 'sector') {
          const data = await _api.getSectorFlow(30, '今日');
          this._renderSector(data.items || []);
        } else if (this.currentTab === 'stock') {
          this._renderStockSearch();
        } else if (this.currentTab === 'north') {
          const data = await _api.getNorthFlow(30);
          this._renderNorth(data.items || []);
        }
      } catch (e) {
        console.error('加载资金流向失败:', e);
        this.body.innerHTML = '<div class="flow-empty">加载失败，请稍后重试</div>';
      }
    }

    /* ── 板块资金流 ── */

    _renderSector(items) {
      if (!items.length) {
        this.body.innerHTML = '<div class="flow-empty">暂无板块资金流数据</div>';
        return;
      }
      const inflows = items.filter(it => it.direction === 'inflow');
      const outflows = items.filter(it => it.direction === 'outflow');

      this.body.innerHTML = `
        <div id="flowTreemap" class="flow-chart"></div>
        <div style="display:flex;gap:20px;margin-top:4px;">
          <div style="flex:1;">
            <div style="font-size:13px;font-weight:600;color:#ef4444;margin-bottom:6px;">主力净流入 TOP${inflows.length}</div>
            ${this._renderTable(inflows, true)}
          </div>
          <div style="flex:1;">
            <div style="font-size:13px;font-weight:600;color:#22c55e;margin-bottom:6px;">主力净流出 TOP${outflows.length}</div>
            ${this._renderTable(outflows, false)}
          </div>
        </div>`;
      this._drawTreemap(items);
    }

    _renderTable(items, isInflow) {
      return `<table class="flow-table"><tbody>
        ${items.map((it, i) => `<tr>
          <td style="width:24px;color:var(--text-muted);">${i + 1}</td>
          <td>${this._esc(it.name)}</td>
          <td class="${isInflow ? 'positive' : 'negative'}" style="text-align:right;font-family:var(--font-mono);">${this._fmtAmount(it.net_inflow)}</td>
        </tr>`).join('')}
      </tbody></table>`;
    }

    async _drawTreemap(items) {
      const el = document.getElementById('flowTreemap');
      if (!el) return;

      // 动态加载 ECharts（复用 chart-kline.js 的 CDN）
      if (!window.echarts) {
        await new Promise((resolve, reject) => {
          const s = document.createElement('script');
          s.src = 'https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js';
          s.onload = () => resolve();
          s.onerror = () => reject(new Error('ECharts 加载失败'));
          document.head.appendChild(s);
        });
      }

      if (this.chart) this.chart.dispose();
      this.chart = echarts.init(el);

      // 按绝对值排序，取有意义的数据
      const sorted = items
        .filter(it => Math.abs(it.net_inflow) > 0)
        .sort((a, b) => Math.abs(b.net_inflow) - Math.abs(a.net_inflow));

      const maxAbs = Math.abs(sorted[0]?.net_inflow || 1);

      const data = sorted.map((it) => {
        const v = it.net_inflow;
        const abs = Math.abs(v);
        const intensity = Math.min(abs / maxAbs, 1);
        return {
          name: it.name || '未知',
          value: abs || 1,
          netInflow: v,
          change: it.change_pct,
          itemStyle: {
            color: v >= 0
              ? `rgb(${180 + Math.round(75 * intensity)}, ${70 - Math.round(40 * intensity)}, ${60 - Math.round(30 * intensity)})`
              : `rgb(${50 - Math.round(30 * intensity)}, ${150 + Math.round(80 * intensity)}, ${70 + Math.round(30 * intensity)})`,
          },
        };
      });

      this.chart.setOption({
        tooltip: {
          formatter: (p) => {
            const d = p.data;
            return `<b>${d.name}</b><br/>主力净流入: ${this._fmtAmount(d.netInflow)}`;
          },
        },
        series: [{
          type: 'treemap',
          roam: false,
          nodeClick: false,
          width: '100%',
          height: '100%',
          breadcrumb: { show: false },
          label: {
            show: true,
            formatter: (p) => `${p.data.name}\n${this._fmtAmount(p.data.netInflow)}`,
            fontSize: 13,
            lineHeight: 18,
            color: '#fff',
          },
          data,
        }],
      });

      const ro = new ResizeObserver(() => this.chart?.resize());
      ro.observe(el);
    }

    /* ── 个股资金流 ── */

    _renderStockSearch() {
      this.body.innerHTML = `
        <div class="flow-stock-search">
          <input id="flowStockInput" type="text" placeholder="输入股票代码，如 600519" />
          <button id="flowStockBtn">查询</button>
        </div>
        <div id="flowStockResult"></div>`;

      const input = $('flowStockInput');
      const btn = $('flowStockBtn');
      const go = () => {
        const code = input?.value?.trim();
        if (code) this._loadStockFlow(code);
      };
      btn?.addEventListener('click', go);
      input?.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    }

    async _loadStockFlow(code) {
      const result = $('flowStockResult');
      if (!result) return;
      result.innerHTML = '<div class="flow-loading">查询中…</div>';
      try {
        const _api = api();
        const data = await _api.getStockFlow(code);
        if (!data.item) {
          result.innerHTML = '<div class="flow-empty">未找到该股票的资金流数据</div>';
          return;
        }
        const it = data.item;
        result.innerHTML = `
          <div style="margin-bottom:8px;font-size:14px;font-weight:600;">${this._esc(it.name)} (${this._esc(it.code)})</div>
          <div class="flow-stock-detail">
            <div class="flow-stock-card"><div class="label">最新价</div><div class="value">${it.price || '—'}</div></div>
            <div class="flow-stock-card"><div class="label">涨跌幅</div><div class="value ${this._signClass(it.change_pct)}">${this._fmt(it.change_pct)}%</div></div>
            <div class="flow-stock-card"><div class="label">主力净流入</div><div class="value ${this._signClass(it.main_net)}">${this._fmtAmount(it.main_net)}</div></div>
          </div>`;
      } catch (e) {
        console.error('查询个股资金流失败:', e);
        result.innerHTML = '<div class="flow-empty">查询失败，请稍后重试</div>';
      }
    }

    /* ── 北向资金 ── */

    _renderNorth(items) {
      if (!items.length) {
        this.body.innerHTML = '<div class="flow-empty">暂无北向资金数据</div>';
        return;
      }
      this.body.innerHTML = `
        <table class="flow-table">
          <thead><tr>
            <th>排名</th><th>板块</th><th>北向净流入</th>
          </tr></thead>
          <tbody>
            ${items.map((it, i) => `<tr>
              <td>${i + 1}</td>
              <td>${this._esc(it.name)}</td>
              <td class="${this._signClass(it.north_net)}" style="font-family:var(--font-mono);">${this._fmtAmount(it.north_net)}</td>
            </tr>`).join('')}
          </tbody>
        </table>`;
    }

    /* ── 工具函数 ── */

    _fmt(v) {
      if (v == null || v === '') return '—';
      const n = parseFloat(String(v).replace(/[亿万千百%,\s]/g, ''));
      return isNaN(n) ? String(v) : n.toFixed(2);
    }

    _fmtAmount(v) {
      if (v == null || v === 0) return '—';
      const n = typeof v === 'number' ? v : parseFloat(String(v).replace(/[,\s]/g, ''));
      if (isNaN(n)) return String(v);
      const abs = Math.abs(n);
      const sign = n >= 0 ? '' : '-';
      if (abs >= 1e8) return sign + (abs / 1e8).toFixed(2) + '亿';
      if (abs >= 1e4) return sign + (abs / 1e4).toFixed(2) + '万';
      return sign + abs.toFixed(0);
    }

    _signClass(v) {
      const n = typeof v === 'number' ? v : parseFloat(String(v).replace(/[,\s]/g, ''));
      if (n > 0) return 'positive';
      if (n < 0) return 'negative';
      return '';
    }

    _esc(s) {
      if (!s) return '';
      const d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }
  }

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.flowPanel = new FlowPanel();
})();
