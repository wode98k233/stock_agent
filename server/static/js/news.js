/**
 * 热点新闻面板
 *
 * 市场热点新闻浏览面板，支持分类筛选、刷新、跳转原文。
 * 个股新闻可从数据中心跳转。
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  // 延迟获取 api，避免加载顺序问题
  const api = () => window.StockRadar?.api;

  class NewsPanel {
    constructor() {
      this.overlay = $('newsOverlay');
      this.modal = $('newsModal');
      this.body = $('newsBody');
      this.items = [];
      this.currentCategory = '全部';
      console.log('[NewsPanel] 构造完成, overlay:', !!this.overlay, 'modal:', !!this.modal, 'body:', !!this.body);
      this._bind();
    }

    _bind() {
      // 关闭
      $('newsCloseBtn')?.addEventListener('click', (e) => { e.stopPropagation(); this.hide(); });
      this.overlay?.addEventListener('click', () => this.hide());
      // 刷新
      $('newsRefreshBtn')?.addEventListener('click', (e) => { e.stopPropagation(); this._load(); });
      // 分类 Tab
      $('newsTabs')?.addEventListener('click', (e) => {
        const tab = e.target.closest('.news-tab');
        if (!tab) return;
        this.currentCategory = tab.dataset.category || '全部';
        this._setActiveTab();
        this._load();
      });
    }

    show() {
      console.log('[NewsPanel] show() 被调用');
      this.overlay?.classList.add('open');
      this.modal?.classList.add('open');
      this._load();
    }

    hide() {
      this.overlay?.classList.remove('open');
      this.modal?.classList.remove('open');
    }

    /** 从数据中心跳转，加载个股新闻 */
    showStockNews(code, name) {
      this.overlay?.classList.add('open');
      this.modal?.classList.add('open');
      this._loadStockNews(code, name);
    }

    _setActiveTab() {
      const tabs = $('newsTabs')?.querySelectorAll('.news-tab');
      tabs?.forEach((t) => {
        t.classList.toggle('active', (t.dataset.category || '全部') === this.currentCategory);
      });
    }

    async _load() {
      if (!this.body) return;
      this.body.innerHTML = '<div class="news-loading">加载中…</div>';
      try {
        const _api = api();
        if (!_api) { this.body.innerHTML = '<div class="news-empty">API 未就绪</div>'; return; }
        const data = await _api.getHotNews(this.currentCategory, 50);
        this.items = data.items || [];
        this._render();
      } catch (e) {
        console.error('加载热点新闻失败:', e);
        this.body.innerHTML = '<div class="news-empty">加载失败，请稍后重试</div>';
      }
    }

    async _loadStockNews(code, name) {
      if (!this.body) return;
      this.body.innerHTML = `<div class="news-loading">加载 ${name || code} 相关新闻…</div>`;
      // 隐藏分类 Tab（个股新闻不分类）
      $('newsTabs')?.classList.add('hidden');
      try {
        const _api = api();
        if (!_api) { this.body.innerHTML = '<div class="news-empty">API 未就绪</div>'; return; }
        const data = await _api.getStockNews(code, 20);
        this.items = data.items || [];
        this._render(name);
      } catch (e) {
        console.error('加载个股新闻失败:', e);
        this.body.innerHTML = '<div class="news-empty">加载失败，请稍后重试</div>';
      }
    }

    _render(stockName) {
      if (!this.items.length) {
        this.body.innerHTML = '<div class="news-empty">暂无相关新闻</div>';
        return;
      }
      const title = stockName ? `${stockName} 相关新闻` : '热点新闻';
      $('newsTitle').textContent = title;
      // 恢复分类 Tab（非个股模式）
      if (!stockName) $('newsTabs')?.classList.remove('hidden');

      this.body.innerHTML = this.items
        .map(
          (item, i) => `
        <div class="news-card" data-idx="${i}">
          <div class="news-card-rank">${i + 1}</div>
          <div class="news-card-body">
            <div class="news-card-title">
              ${item.url ? `<a href="${this._esc(item.url)}" target="_blank" rel="noopener">${this._esc(item.title)}</a>` : this._esc(item.title)}
            </div>
            <div class="news-card-summary" title="${this._esc(item.summary)}">${this._esc(item.summary)}</div>
            <div class="news-card-meta">
              <span class="news-card-source">${this._esc(item.source)}</span>
              <span class="news-card-time">${this._esc(item.publish_time)}</span>
              ${item.url ? `<a class="news-card-link" href="${this._esc(item.url)}" target="_blank" rel="noopener">查看原文 ↗</a>` : ''}
            </div>
          </div>
        </div>`
        )
        .join('');
    }

    _esc(s) {
      if (!s) return '';
      const d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }
  }

  // 暴露（按钮绑定在 app.js 中统一处理）
  window.StockRadar = window.StockRadar || {};
  window.StockRadar.newsPanel = new NewsPanel();
})();
