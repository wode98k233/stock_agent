/* ─────────────────────────────────────────────
   市场情绪面板（方案 C 二期）
   - 温度计 + 近 10 日 sparkline
   - 异动/新闻双 Tab + 未读徽标（localStorage）
   - 异动点击 → K 线；＋自选；自选/持仓置顶高亮
   - 窄屏（≤1100px）右滑抽屉；10s 轮询（页面隐藏暂停）
   数据：/api/market-dashboard/snapshot、/api/news/hot、/api/short-line/history
   ───────────────────────────────────────────── */
(() => {
  "use strict";

  window.StockRadar = window.StockRadar || {};

  const API = "/api/market-dashboard/snapshot";
  const NEWS_API = "/api/news/hot";
  const HISTORY_API = "/api/short-line/history?days=10";
  const WATCHLIST_API = "/api/watchlist";
  const PORTFOLIO_CODES_API = "/api/portfolio/codes";
  const READ_KEY_ALERTS = "sr_market_read_alerts";
  const READ_KEY_NEWS = "sr_market_read_news";
  const READ_LIMIT = 400;
  const AUX_REFRESH_MS = 60000;
  const SPARK_REFRESH_MS = 300000;
  const NEWS_REFRESH_MS = 120000;

  const LEVEL_CLASS = { ice: "ice", ebb: "ebb", warm: "warm", hot: "hot" };

  function fmtPercent(value, digits = 1) {
    if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) return "-";
    return `${(Number(value) * 100).toFixed(digits)}%`;
  }

  function fmtNumber(value, digits = 2) {
    if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) return "-";
    return Number(value).toFixed(digits);
  }

  function fmtTime(iso) {
    if (!iso) return "--:--:--";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "--:--:--";
    return d.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function hashId(text) {
    let hash = 0;
    const value = String(text || "");
    for (let i = 0; i < value.length; i += 1) {
      hash = (hash * 31 + value.charCodeAt(i)) | 0;
    }
    return String(hash);
  }

  function loadRead(key) {
    try {
      return new Set(JSON.parse(localStorage.getItem(key) || "[]"));
    } catch {
      return new Set();
    }
  }

  function saveRead(key, set) {
    try {
      localStorage.setItem(key, JSON.stringify([...set].slice(-READ_LIMIT)));
    } catch {}
  }

  function isAShareCode(code) {
    return /^\d{6}$/.test(code || "");
  }

  class MarketDashboard {
    constructor() {
      this.timer = null;
      this.auxTimer = null;
      this.sparkTimer = null;
      this.newsTimer = null;
      this.dom = {};
      this.trading = false;
      this.activeTab = "alerts";
      this.alerts = [];
      this.news = [];
      this.newsLoadedAt = 0;
      this.watchlist = new Set();
      this.portfolio = new Set();
      this.readAlerts = loadRead(READ_KEY_ALERTS);
      this.readNews = loadRead(READ_KEY_NEWS);
      this.config = { enabled: true, refresh_s: 10, news_limit: 20 };
    }

    init() {
      [
        "marketUpdatedAt", "marketTradingTag", "marketTradingDot",
        "mScore", "mLevel", "mLevelFill", "mLevelMark", "mTradeDate",
        "mLimitUp", "mLimitDown", "mBreakRate", "mMaxBoard", "mPromotion", "mPremium",
        "marketDegraded", "marketAlertCount", "marketAlertList", "mSparkline", "mSparkDates",
        "mSparkLatest", "mSparkRange",
        "marketTabAlerts", "marketTabNews", "marketAlertBadge", "marketNewsBadge",
        "marketPaneClose", "marketToggleBtn", "marketDrawerMask", "marketPane", "marketBreadth",
      ].forEach((id) => { this.dom[id] = document.getElementById(id); });
      if (!this.dom.marketAlertList) return;

      this._bindEvents();
      this.refresh();
      this.loadAux();
      this.loadSparkline();
      this._scheduleRefresh(this.config.refresh_s);
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) this.refresh();
      });
    }

    /* ── 定时器 ── */

    _scheduleRefresh(seconds) {
      if (this.timer) clearInterval(this.timer);
      const intervalMs = Math.max(3, Number(seconds) || 10) * 1000;
      this.timer = setInterval(() => {
        if (!document.hidden) this.refresh();
      }, intervalMs);
    }

    _ensureAuxTimer() {
      if (this.auxTimer) return;
      this.auxTimer = setInterval(() => {
        if (!document.hidden) this.loadAux();
      }, AUX_REFRESH_MS);
    }

    _ensureSparkTimer() {
      if (this.sparkTimer) return;
      this.sparkTimer = setInterval(() => {
        if (!document.hidden) this.loadSparkline();
      }, SPARK_REFRESH_MS);
    }

    _ensureNewsTimer() {
      if (this.newsTimer) return;
      this.newsTimer = setInterval(() => {
        if (!document.hidden && this.activeTab === "news") this.loadNews(false);
      }, NEWS_REFRESH_MS);
    }

    /* ── 事件绑定 ── */

    _bindEvents() {
      if (this.dom.marketTabAlerts) {
        this.dom.marketTabAlerts.onclick = () => this.switchTab("alerts");
      }
      if (this.dom.marketTabNews) {
        this.dom.marketTabNews.onclick = () => this.switchTab("news");
      }
      if (this.dom.marketAlertList) {
        this.dom.marketAlertList.addEventListener("click", (e) => this._onListClick(e));
      }
      if (this.dom.marketPaneClose) {
        this.dom.marketPaneClose.onclick = () => this.closePane();
      }
      if (this.dom.marketToggleBtn) {
        this.dom.marketToggleBtn.onclick = () => this.togglePane();
      }
      if (this.dom.marketDrawerMask) {
        this.dom.marketDrawerMask.onclick = () => this.closePane();
      }
    }

    _onListClick(e) {
      const addBtn = e.target.closest(".ma-add");
      if (addBtn) {
        e.stopPropagation();
        this._addWatchlist(addBtn.dataset.code, addBtn.dataset.name);
        return;
      }
      const newsItem = e.target.closest(".market-news-item");
      if (newsItem) {
        const id = newsItem.dataset.id;
        const url = newsItem.dataset.url;
        this.readNews.add(id);
        saveRead(READ_KEY_NEWS, this.readNews);
        this._renderNews();
        if (url) window.open(url, "_blank", "noopener");
        return;
      }
      const alertItem = e.target.closest(".market-alert-item");
      if (alertItem) {
        const id = alertItem.dataset.id;
        const code = alertItem.dataset.code;
        if (id) {
          this.readAlerts.add(id);
          saveRead(READ_KEY_ALERTS, this.readAlerts);
          this._renderAlerts();
        }
        if (code && window.openKlineChart) window.openKlineChart(code);
      }
    }

    /* ── 窄屏抽屉 ── */

    openPane() {
      if (!this.dom.marketPane) return;
      if (window.StockRadar.execDrawer) window.StockRadar.execDrawer.close();
      this.dom.marketPane.classList.add("open");
      if (this.dom.marketDrawerMask) this.dom.marketDrawerMask.classList.add("visible");
    }

    closePane() {
      if (!this.dom.marketPane) return;
      this.dom.marketPane.classList.remove("open");
      if (this.dom.marketDrawerMask) this.dom.marketDrawerMask.classList.remove("visible");
    }

    togglePane() {
      if (!this.dom.marketPane) return;
      if (this.dom.marketPane.classList.contains("open")) this.closePane();
      else this.openPane();
    }

    /* ── 快照 ── */

    async refresh() {
      try {
        const res = await fetch(API);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const payload = await res.json();
        this.render((payload && payload.data) || {});
      } catch (e) {
        this.renderError();
      }
    }

    render(data) {
      this.trading = !!data.trading;
      this.config = {
        enabled: data.enabled !== false,
        refresh_s: Number(data.refresh_s) || 10,
        news_limit: Number(data.news_limit) || 20,
      };

      if (this.dom.marketTradingTag) {
        this.dom.marketTradingTag.textContent = this.trading ? "交易中" : "非交易时段";
        this.dom.marketTradingTag.classList.toggle("open", this.trading);
      }
      if (this.dom.marketTradingDot) {
        this.dom.marketTradingDot.classList.toggle("live", this.trading);
      }
      if (this.dom.marketUpdatedAt) {
        const stale = data.stale ? `（回退 ${data.stale_date || "最近快照"}）` : "";
        const disabled = data.enabled === false ? " · 已关闭" : "";
        this.dom.marketUpdatedAt.textContent = `更新 ${fmtTime(data.updated_at)}${stale}${disabled}`;
      }
      this._scheduleRefresh(this.config.refresh_s);

      const sentiment = data.sentiment || null;
      if (sentiment) {
        const score = Number(sentiment.score);
        if (this.dom.mScore) {
          this.dom.mScore.textContent = Number.isNaN(score) ? "--" : score;
          this.dom.mScore.className = `m-score ${LEVEL_CLASS[sentiment.level] || ""}`;
        }
        if (this.dom.mLevel) this.dom.mLevel.textContent = sentiment.level_label || "--";
        if (this.dom.mTradeDate && sentiment.trade_date) {
          this.dom.mTradeDate.textContent = `交易日 ${String(sentiment.trade_date).slice(5)}`;
        }
        const pct = Math.max(0, Math.min(100, Number.isNaN(score) ? 0 : score));
        if (this.dom.mLevelFill) this.dom.mLevelFill.style.width = `${pct}%`;
        if (this.dom.mLevelMark) this.dom.mLevelMark.style.left = `${pct}%`;

        const limitUp = Number(sentiment.limit_up_count) || 0;
        const limitDown = Number(sentiment.limit_down_count) || 0;
        const breakRate = Number(sentiment.break_rate) || 0;
        const promotion = Number(sentiment.promotion_rate) || 0;
        const premium = sentiment.yesterday_premium || {};
        const premiumMean = premium.mean === null || premium.mean === undefined ? null : Number(premium.mean);

        this._setVal("mLimitUp", sentiment.limit_up_count ?? "-", limitUp > 0 ? "val-up" : "");
        this._setVal("mLimitDown", sentiment.limit_down_count ?? "-", limitDown > 0 ? "val-down" : "");
        this._setVal("mBreakRate", fmtPercent(sentiment.break_rate, 0), breakRate >= 0.3 ? "val-warn" : "");
        this._setVal("mMaxBoard", sentiment.max_board ?? "-", "");
        this._setVal("mPromotion", fmtPercent(sentiment.promotion_rate, 0), promotion >= 0.3 ? "val-up" : "");
        this._setVal("mPremium", premiumMean === null ? "-" : `${fmtNumber(premiumMean)}%`,
          premiumMean === null ? "" : (premiumMean >= 0 ? "val-up" : "val-down"));
      } else {
        this._setVal("mScore", "--", "");
        this._setVal("mLevel", data.enabled === false ? "已关闭" : "暂无数据", "");
        if (this.dom.mLevelMark) this.dom.mLevelMark.style.left = "0%";
      }

      this._renderBreadth(data.breadth || null, sentiment);

      this.alerts = Array.isArray(data.alerts) ? data.alerts : [];
      this._renderAlerts();
      this._updateBadges();

      const degraded = (Array.isArray(data.degraded) ? data.degraded : []).filter(Boolean);
      if (this.dom.marketDegraded) {
        this.dom.marketDegraded.hidden = degraded.length === 0;
        if (degraded.length) {
          const text = degraded.includes("disabled")
            ? "面板已在配置中关闭"
            : `数据降级：${degraded.join("、")}（显示最近一次结果）`;
          this.dom.marketDegraded.textContent = text;
        }
      }
    }

    renderError() {
      if (this.dom.marketUpdatedAt) this.dom.marketUpdatedAt.textContent = "刷新失败，重试中…";
    }

    _set(id, value) {
      const el = this.dom[id];
      if (el) el.textContent = value;
    }

    _setVal(id, value, cls) {
      const el = this.dom[id];
      if (!el) return;
      el.textContent = value;
      if (el.classList && el.classList.contains("m-v")) {
        el.classList.remove("val-up", "val-down", "val-warn");
        if (cls) el.classList.add(cls);
      }
    }

    /* ── Tab ── */

    switchTab(tab) {
      this.activeTab = tab === "news" ? "news" : "alerts";
      if (this.dom.marketTabAlerts) this.dom.marketTabAlerts.classList.toggle("active", this.activeTab === "alerts");
      if (this.dom.marketTabNews) this.dom.marketTabNews.classList.toggle("active", this.activeTab === "news");
      if (this.activeTab === "news") {
        this._ensureNewsTimer();
        this.loadNews(false);
      } else {
        this._renderAlerts();
      }
    }

    /* ── 异动列表 ── */

    _alertId(item) {
      return hashId(`${item.code}|${item.tag_name}|${(item.analysis_content || "").slice(0, 40)}`);
    }

    _renderAlerts() {
      const list = this.dom.marketAlertList;
      if (!list) return;
      if (this.activeTab !== "alerts") return;

      if (!this.alerts.length) {
        list.replaceChildren(this._emptyNode(
          this.config.enabled === false ? "面板已关闭" : (this.trading ? "暂无异动" : "非交易时段，暂无新异动")
        ));
      } else {
        const ordered = [...this.alerts].sort((a, b) => this._rank(b) - this._rank(a));
        const frag = document.createDocumentFragment();
        ordered.forEach((item) => frag.appendChild(this._alertNode(item)));
        list.replaceChildren(frag);
      }
      this._set("marketAlertCount", `${this.alerts.length} 条`);
    }

    _rank(item) {
      if (this.portfolio.has(item.code)) return 2;
      if (this.watchlist.has(item.code)) return 1;
      return 0;
    }

    _alertNode(item) {
      const row = document.createElement("div");
      row.className = "market-alert-item";
      const id = this._alertId(item);
      row.dataset.id = id;
      row.dataset.code = item.code || "";
      if (!this.readAlerts.has(id)) row.classList.add("unread");

      const head = document.createElement("div");
      head.className = "ma-head";
      if (item.tag_name) {
        const tag = document.createElement("span");
        tag.className = "ma-tag";
        tag.textContent = item.tag_name;
        head.appendChild(tag);
      }
      const name = document.createElement("span");
      name.className = "ma-name";
      name.textContent = item.name || "";
      head.appendChild(name);
      const code = document.createElement("span");
      code.className = "ma-code";
      code.textContent = item.code || "";
      head.appendChild(code);

      const flags = document.createElement("span");
      flags.className = "ma-flags";
      if (this.portfolio.has(item.code)) {
        const flag = document.createElement("span");
        flag.className = "ma-flag portfolio";
        flag.textContent = "持仓";
        flags.appendChild(flag);
      } else if (this.watchlist.has(item.code)) {
        const flag = document.createElement("span");
        flag.className = "ma-flag";
        flag.textContent = "自选";
        flags.appendChild(flag);
      }
      head.appendChild(flags);

      if (isAShareCode(item.code) && !this.watchlist.has(item.code)) {
        const add = document.createElement("button");
        add.type = "button";
        add.className = "ma-add";
        add.dataset.code = item.code;
        add.dataset.name = item.name || "";
        add.title = "加入自选";
        add.textContent = "＋";
        head.appendChild(add);
      }
      row.appendChild(head);

      const body = document.createElement("div");
      body.className = "ma-body";
      body.textContent = item.analysis_content || "";
      row.appendChild(body);

      return row;
    }

    /* ── 新闻 Tab ── */

    async loadNews(force) {
      const now = Date.now();
      if (!force && this.news.length && now - this.newsLoadedAt < NEWS_REFRESH_MS) {
        this._renderNews();
        return;
      }
      try {
        const res = await fetch(`${NEWS_API}?category=${encodeURIComponent("全部")}&limit=${this.config.news_limit}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const payload = await res.json();
        this.news = Array.isArray(payload.items) ? payload.items : [];
        this.newsLoadedAt = now;
      } catch (e) {
        this.news = [];
      }
      this._renderNews();
      this._updateBadges();
    }

    _newsId(item) {
      return hashId(item.url || item.title || "");
    }

    _renderNews() {
      const list = this.dom.marketAlertList;
      if (!list || this.activeTab !== "news") return;

      if (!this.news.length) {
        list.replaceChildren(this._emptyNode("暂无新闻"));
      } else {
        const frag = document.createDocumentFragment();
        this.news.forEach((item) => {
          const id = this._newsId(item);
          const row = document.createElement("div");
          row.className = "market-news-item";
          row.dataset.id = id;
          row.dataset.url = item.url || "";
          if (!this.readNews.has(id)) row.classList.add("unread");

          const title = document.createElement("div");
          title.className = "mn-title";
          title.textContent = item.title || "";
          row.appendChild(title);

          const summary = document.createElement("div");
          summary.className = "mn-summary";
          summary.textContent = item.summary || "";
          row.appendChild(summary);

          const meta = document.createElement("div");
          meta.className = "mn-meta";
          meta.textContent = [item.source, item.publish_time].filter(Boolean).join(" · ");
          row.appendChild(meta);

          frag.appendChild(row);
        });
        list.replaceChildren(frag);
      }
      this._set("marketAlertCount", `${this.news.length} 条`);
    }

    /* ── 自选 / 持仓 ── */

    async loadAux() {
      this._ensureAuxTimer();
      const tasks = [
        fetch(WATCHLIST_API).then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
        fetch(PORTFOLIO_CODES_API).then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
      ];
      const [watchlist, portfolio] = await Promise.all(tasks);
      this.watchlist = new Set((watchlist.items || []).map((it) => it.stock_code || it.code).filter(Boolean));
      this.portfolio = new Set((portfolio.items || []).filter(Boolean));
      if (this.activeTab === "alerts") this._renderAlerts();
    }

    async _addWatchlist(code, name) {
      if (!code) return;
      try {
        const res = await fetch(WATCHLIST_API, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ stock_code: code, stock_name: name || "", market: "cn" }),
        });
        if (res.ok) {
          this.watchlist.add(code);
          this._renderAlerts();
          return;
        }
        const detail = await res.json().catch(() => ({}));
        this._toast(`加自选失败：${detail.detail || "HTTP " + res.status}`, "error");
      } catch (e) {
        this._toast(`加自选失败：${(e && e.message) || e}`, "error");
      }
    }

    // 轻量提示：复用盯盘面板的浮层宿主（mm-toast 样式随 monitor.css 全局加载）
    _toast(text, kind = "info") {
      const u = window.StockRadar && window.StockRadar.utils;
      if (u && typeof u.toast === "function") u.toast(text, kind);
    }

    /* ── 温度历史 Sparkline ── */

    async loadSparkline() {
      this._ensureSparkTimer();
      try {
        const res = await fetch(HISTORY_API);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const payload = await res.json();
        const items = (payload.data && payload.data.items) || [];
        this._renderSparkline(items);
      } catch {}
    }

    _renderSparkline(items) {
      const svg = this.dom.mSparkline;
      if (!svg) return;
      const points = (items || [])
        .map((item) => ({ date: String(item.trade_date || ""), score: Number(item.score) }))
        .filter((point) => !Number.isNaN(point.score));
      if (points.length < 2) {
        svg.replaceChildren();
        this._set("mSparkDates", "--");
        this._set("mSparkRange", "");
        if (this.dom.mSparkLatest) {
          this.dom.mSparkLatest.textContent = "历史不足";
          this.dom.mSparkLatest.className = "m-spark-latest";
        }
        return;
      }

      const scores = points.map((point) => point.score);
      const latest = scores[scores.length - 1];
      const prev = scores[scores.length - 2];
      const min = Math.min(...scores);
      const max = Math.max(...scores);

      const fmtDay = (date) => (date.length >= 10 ? date.slice(5) : date);
      this._set("mSparkDates", `${fmtDay(points[0].date)} ~ ${fmtDay(points[points.length - 1].date)} · ${points.length}日`);

      const latestEl = this.dom.mSparkLatest;
      if (latestEl) {
        latestEl.textContent = `最新 ${latest}`;
        latestEl.className = "m-spark-latest";
        if (prev !== undefined) {
          const delta = latest - prev;
          const deltaEl = document.createElement("span");
          deltaEl.className = `m-spark-delta ${delta >= 0 ? "up" : "down"}`;
          deltaEl.textContent = ` ${delta >= 0 ? "▲" : "▼"}${Math.abs(delta)}`;
          latestEl.appendChild(deltaEl);
        }
      }
      this._set("mSparkRange", `最高 ${max} · 最低 ${min}`);

      const width = 200;
      const height = 40;
      const pad = 4;
      const span = max - min || 1;
      const xy = scores.map((score, index) => {
        const x = pad + (index / (scores.length - 1)) * (width - pad * 2);
        const y = height - pad - ((score - min) / span) * (height - pad * 2);
        return [Number(x.toFixed(1)), Number(y.toFixed(1))];
      });

      const ns = "http://www.w3.org/2000/svg";
      const area = document.createElementNS(ns, "polygon");
      area.setAttribute(
        "points",
        `${pad},${height} ${xy.map((point) => point.join(",")).join(" ")} ${width - pad},${height}`
      );
      area.setAttribute("class", "m-spark-area");

      const polyline = document.createElementNS(ns, "polyline");
      polyline.setAttribute("points", xy.map((point) => point.join(",")).join(" "));

      const last = xy[xy.length - 1];
      const dot = document.createElementNS(ns, "circle");
      dot.setAttribute("cx", last[0]);
      dot.setAttribute("cy", last[1]);
      dot.setAttribute("r", "2.6");
      dot.setAttribute("class", "m-spark-dot");

      svg.replaceChildren(area, polyline, dot);
    }

    /* ── 徽标 ── */

    _updateBadges() {
      const unreadAlerts = this.alerts.filter((item) => !this.readAlerts.has(this._alertId(item))).length;
      const unreadNews = this.news.filter((item) => !this.readNews.has(this._newsId(item))).length;
      this._setBadge(this.dom.marketAlertBadge, unreadAlerts);
      this._setBadge(this.dom.marketNewsBadge, unreadNews);
    }

    _setBadge(el, count) {
      if (!el) return;
      if (count > 0) {
        el.hidden = false;
        el.textContent = count > 99 ? "99+" : String(count);
      } else {
        el.hidden = true;
      }
    }

    _emptyNode(text) {
      const node = document.createElement("div");
      node.className = "market-alert-empty";
      node.textContent = text;
      return node;
    }

    /* ── 涨跌分布 / 三市成交额 / 主力净流入 ── */

    _fmtAmount(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      const v = Number(value);
      if (Math.abs(v) >= 1e12) return `${(v / 1e12).toFixed(2)}万亿`;
      if (Math.abs(v) >= 1e8) return `${(v / 1e8).toFixed(1)}亿`;
      if (Math.abs(v) >= 1e4) return `${(v / 1e4).toFixed(0)}万`;
      return String(v);
    }

    _renderBreadth(breadth, sentiment) {
      const wrap = this.dom.marketBreadth;
      if (!wrap) return;
      wrap.replaceChildren();
      wrap.hidden = !breadth;
      if (!breadth) return;

      const addRow = (label, valueEl) => {
        const row = document.createElement("div");
        row.className = "m-b-row";
        const labelEl = document.createElement("span");
        labelEl.textContent = label;
        row.appendChild(labelEl);
        row.appendChild(valueEl);
        wrap.appendChild(row);
      };

      // 三市成交额 + 放量/缩量（放量红、缩量绿）
      const amountWrap = document.createElement("span");
      const amountSpan = document.createElement("span");
      amountSpan.className = "m-b-value";
      amountSpan.textContent = this._fmtAmount(breadth.amount);
      amountWrap.appendChild(amountSpan);
      if (breadth.prev_amount && breadth.amount) {
        const ratio = breadth.amount / breadth.prev_amount - 1;
        const delta = document.createElement("span");
        delta.className = `m-b-delta ${ratio >= 0 ? "up" : "down"}`;
        delta.textContent = ` 较上日${ratio >= 0 ? "放量" : "缩量"}${Math.abs(ratio * 100).toFixed(1)}%`;
        amountWrap.appendChild(delta);
      }
      addRow("三市成交额", amountWrap);

      // 主力净流入（正红负绿）
      if (breadth.main_net_inflow !== null && breadth.main_net_inflow !== undefined) {
        const flow = document.createElement("span");
        flow.className = `m-b-value ${breadth.main_net_inflow >= 0 ? "up" : "down"}`;
        flow.textContent = `${breadth.main_net_inflow >= 0 ? "+" : ""}${this._fmtAmount(breadth.main_net_inflow)}`;
        addRow("主力净流入", flow);
      }

      // 涨跌共用比例条（红涨 / 灰平 / 绿跌）
      const total = Number(breadth.total)
        || ((breadth.up || 0) + (breadth.down || 0) + (breadth.flat || 0))
        || 1;
      const upPct = ((breadth.up || 0) / total) * 100;
      const flatPct = ((breadth.flat || 0) / total) * 100;

      const udRow = document.createElement("div");
      udRow.className = "m-updown";
      const upLabel = document.createElement("span");
      upLabel.className = "m-ud-label up";
      upLabel.textContent = `涨 ${breadth.up ?? "-"}`;
      const udBar = document.createElement("div");
      udBar.className = "m-ud-bar";
      const segUp = document.createElement("i");
      segUp.className = "up";
      segUp.style.width = `${upPct.toFixed(2)}%`;
      segUp.title = `上涨 ${breadth.up ?? 0} 家`;
      const segFlat = document.createElement("i");
      segFlat.className = "flat";
      segFlat.style.width = `${flatPct.toFixed(2)}%`;
      segFlat.title = `平盘 ${breadth.flat ?? 0} 家`;
      const segDown = document.createElement("i");
      segDown.className = "down";
      segDown.style.width = `${Math.max(0, 100 - upPct - flatPct).toFixed(2)}%`;
      segDown.title = `下跌 ${breadth.down ?? 0} 家`;
      udBar.append(segUp, segFlat, segDown);
      const downLabel = document.createElement("span");
      downLabel.className = "m-ud-label down";
      downLabel.textContent = `跌 ${breadth.down ?? "-"}`;
      udRow.append(upLabel, udBar, downLabel);
      wrap.appendChild(udRow);

      const flatLine = document.createElement("div");
      flatLine.className = "m-ud-flat";
      flatLine.textContent = `平盘 ${breadth.flat ?? 0}`;
      wrap.appendChild(flatLine);

      // 九段涨跌分布柱状图（涨停/跌停取涨停池口径，与上方指标一致）
      const dist = breadth.dist || {};
      const poolUp = sentiment && sentiment.limit_up_count !== null && sentiment.limit_up_count !== undefined
        ? sentiment.limit_up_count : (dist.limit_up || 0);
      const poolDown = sentiment && sentiment.limit_down_count !== null && sentiment.limit_down_count !== undefined
        ? sentiment.limit_down_count : (dist.limit_down || 0);
      const buckets = [
        { label: "涨停", color: "#b42318", value: poolUp, pool: true },
        { label: "≥5", color: "#e74c3c", value: dist.up_5 || 0 },
        { label: "1~5", color: "#f0705a", value: dist.up_1_5 || 0 },
        { label: "0~1", color: "#f5a09a", value: dist.up_0_1 || 0 },
        { label: "平盘", color: "#cbd3dc", value: dist.flat || 0 },
        { label: "0~-1", color: "#a7d8c9", value: dist.down_0_1 || 0 },
        { label: "-1~-5", color: "#6cbfa5", value: dist.down_1_5 || 0 },
        { label: "≤-5", color: "#2f8f74", value: dist.down_5 || 0 },
        { label: "跌停", color: "#176b5b", value: poolDown, pool: true },
      ];
      const maxValue = Math.max(1, ...buckets.map((bucket) => Number(bucket.value) || 0));
      const hist = document.createElement("div");
      hist.className = "m-hist";
      buckets.forEach((bucket) => {
        const col = document.createElement("div");
        col.className = "m-hist-col";
        const count = document.createElement("span");
        count.className = "m-hist-count";
        count.textContent = bucket.value;
        const slot = document.createElement("div");
        slot.className = "m-hist-slot";
        const bar = document.createElement("i");
        bar.className = "m-hist-bar";
        bar.style.height = `${Math.max(4, Math.sqrt((Number(bucket.value) || 0) / maxValue) * 100)}%`;
        bar.style.background = bucket.color;
        bar.title = `${bucket.label} ${bucket.value} 家${bucket.pool ? "（涨停池口径）" : ""}`;
        slot.appendChild(bar);
        const label = document.createElement("span");
        label.className = "m-hist-label";
        label.textContent = bucket.label;
        col.append(count, slot, label);
        hist.appendChild(col);
      });
      wrap.appendChild(hist);
    }
  }

  window.StockRadar.MarketDashboard = MarketDashboard;
  window.StockRadar.marketDashboard = new MarketDashboard();
})();
