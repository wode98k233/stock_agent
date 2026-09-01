/* ─────────────────────────────────────────────
   选股雷达 Web — Dashboard 仪表盘渲染（数据驱动版）
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.chatDashboard = (() => {
  const { esc } = window.StockRadar.utils;

  const DASH_COLORS = { buy: "#059669", hold: "#d97706", sell: "#dc2626" };
  const DASH_ICONS = { buy: "🟢", hold: "🟡", sell: "🔴" };
  const DASH_LABELS = { buy: "BUY", hold: "HOLD", sell: "SELL" };
  const DASH_SUBS = { buy: "可参与", hold: "观望", sell: "卖出" };
  const RISK_LABELS = { high: "高", medium: "中", low: "低" };
  const CHECK_ICONS = { positive: "✅", warning: "⚠️", negative: "❌" };

  /* ── 缓存：dashboard_id → JSON 定义 ── */
  const _defCache = {};
  let _defsLoaded = false;

  /* 同步读缓存，未加载返回 null */
  function _getDashboardDef(dashboardId) {
    return _defCache[dashboardId] || null;
  }

  /* 启动时预加载全部仪表盘定义 */
  (function _preloadDefs() {
    fetch("/api/dashboards")
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data || !data.dashboards) return;
        for (const def of data.dashboards) {
          if (def && def.id) _defCache[def.id] = def;
        }
        _defsLoaded = true;
      })
      .catch(() => {});
  })();

  /* ── 工具 ── */
  function _el(tag, cls, html) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html !== undefined) el.innerHTML = html;
    return el;
  }

  function _stageInfo(trend) {
    const map = {
      bullish:  { pos: 65, label: "主升浪中段", arrow: "⬆" },
      neutral:  { pos: 40, label: "震荡整理",   arrow: "→" },
      bearish:  { pos: 15, label: "退潮调整",   arrow: "⬇" },
    };
    return map[trend] || map.neutral;
  }

  /* ── 判断数据中是否有有效字段值 ── */
  function _hasFieldData(data, fields) {
    if (!fields) return false;
    return fields.some(f => {
      const v = data[f];
      if (v == null) return false;
      if (Array.isArray(v) && v.length === 0) return false;
      if (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0) return false;
      return true;
    });
  }

  /* ── 渲染器注册表 ── */
  const SECTION_RENDERERS = {};

  /* signal_row: 决策信号行 */
  SECTION_RENDERERS.signal_row = (data, _section, dt, color) => {
    const row = _el("div", "dash-signal-row");
    row.appendChild(_el("div", `dash-signal-box decision ${dt}`,
      `<div class="dash-sig-label">决策信号</div>` +
      `<div class="dash-sig-value" style="color:${color}">${DASH_ICONS[dt]} ${DASH_LABELS[dt]}</div>` +
      `<div class="dash-sig-sub">${DASH_SUBS[dt]}</div>`
    ));
    const confPct = data.confidence_level != null ? Math.round(data.confidence_level * 100) : null;
    row.appendChild(_el("div", "dash-signal-box",
      `<div class="dash-sig-label">置信度</div>` +
      (confPct != null
        ? `<div class="dash-sig-value" style="color:${color}">${confPct}%</div>` +
          `<div class="dash-confidence-bar"><div class="dash-confidence-fill ${dt}" style="width:${confPct}%"></div></div>`
        : `<div class="dash-sig-value" style="color:#64748b">—</div>`)
    ));
    const sentScore = data.sentiment_score != null ? data.sentiment_score : null;
    const sentLabelMap = { bullish: "偏积极", neutral: "中性", bearish: "偏消极" };
    row.appendChild(_el("div", "dash-signal-box",
      `<div class="dash-sig-label">情绪分数</div>` +
      (sentScore != null
        ? `<div class="dash-sig-value" style="color:${color}">${sentScore}</div>` +
          (data.sentiment_label || sentLabelMap[data.trend_prediction]
            ? `<div class="dash-sig-sub">${esc(data.sentiment_label || sentLabelMap[data.trend_prediction] || "")}</div>` : "")
        : `<div class="dash-sig-value" style="color:#64748b">—</div>`)
    ));
    return row;
  };

  /* verdict_text: 核心结论 */
  SECTION_RENDERERS.verdict_text = (data) => {
    if (!data.core_verdict) return null;
    return _el("div", "dash-verdict-row",
      `<span class="dash-verdict-icon">💬</span>` +
      `<span class="dash-verdict-text">${esc(data.core_verdict)}</span>`
    );
  };

  /* price_grid: 买卖点位 */
  SECTION_RENDERERS.price_grid = (data) => {
    const levels = data.price_levels;
    if (!levels || Object.keys(levels).length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">📍</span> 买卖点位`));
    const grid = _el("div", "dash-price-levels");
    for (const [k, v] of Object.entries(levels)) {
      grid.appendChild(_el("div", "dash-price-level",
        `<div class="dash-price-label">${esc(k)}</div>` +
        `<div class="dash-price-value">${esc(String(v))}</div>`
      ));
    }
    section.appendChild(grid);
    return section;
  };

  /* checklist: 维度检查 */
  SECTION_RENDERERS.checklist = (data) => {
    if (!data.checklist || data.checklist.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">📋</span> 维度检查`));
    const list = _el("div", "dash-checklist");
    for (const item of data.checklist) {
      const status = (item.status || "warning").toLowerCase();
      const icon = CHECK_ICONS[status] || "⚠️";
      list.appendChild(_el("div", `dash-check-item ${status}`,
        `<span class="dash-check-icon">${icon}</span>` +
        `<div class="dash-check-detail">` +
          `<div class="dash-check-dim">${esc(item.dimension || "")}</div>` +
          (item.detail ? `<div class="dash-check-desc">${esc(item.detail)}</div>` : "") +
        `</div>`
      ));
    }
    section.appendChild(list);
    return section;
  };

  /* advice_cards: 分视角操作建议 */
  SECTION_RENDERERS.advice_cards = (data) => {
    const adv = data.split_advice;
    if (!adv || Object.keys(adv).length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">👤</span> 操作建议`));
    const wrap = _el("div", "dash-split-advice");
    for (const [k, v] of Object.entries(adv)) {
      wrap.appendChild(_el("div", "dash-advice-card",
        `<div class="dash-advice-label">${esc(k)}</div>` +
        `<div class="dash-advice-text">${esc(String(v))}</div>`
      ));
    }
    section.appendChild(wrap);
    return section;
  };

  /* risk_list: 风险优先级 */
  SECTION_RENDERERS.risk_list = (data) => {
    if (!data.risk_priority || data.risk_priority.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">🚨</span> 风险优先级`));
    const list = _el("div", "dash-risk-list");
    for (const r of data.risk_priority) {
      const level = (r.level || "medium").toLowerCase();
      list.appendChild(_el("div", `dash-risk-item ${level}`,
        `<span class="dash-risk-level ${level}">${RISK_LABELS[level] || "中"}</span>` +
        `<div class="dash-risk-content">` +
          `<div class="dash-risk-title">${esc(r.category || "")}</div>` +
          (r.action ? `<div class="dash-risk-action">→ ${esc(r.action)}</div>` : "") +
        `</div>`
      ));
    }
    section.appendChild(list);
    return section;
  };

  /* watch_chips: 后续观察 */
  SECTION_RENDERERS.watch_chips = (data) => {
    if (!data.next_watch || data.next_watch.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">👀</span> 后续观察`));
    const chips = _el("div", "dash-watch-chips");
    for (const w of data.next_watch) {
      chips.appendChild(_el("span", "dash-watch-chip", esc(w)));
    }
    section.appendChild(chips);
    return section;
  };

  /* sector_tabs: 板块轮动 */
  SECTION_RENDERERS.sector_tabs = (data) => {
    const rotation = data.sector_rotation;
    if (!rotation || rotation.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">🔄</span> 板块轮动`));
    const tabs = _el("div", "dash-sector-tabs");
    for (const s of rotation) {
      const name = typeof s === "string" ? s : (s.name || s.sector || "");
      const trend = typeof s === "object" ? (s.trend || "neutral") : "neutral";
      let tabCls = "dash-sector-tab";
      if (trend === "bullish" || trend === "up") tabCls += " up";
      if (trend === "bearish" || trend === "down") tabCls += " down";
      tabs.appendChild(_el("span", tabCls, esc(name)));
    }
    section.appendChild(tabs);
    return section;
  };

  /* index_cards: 主要指数 */
  SECTION_RENDERERS.index_cards = (data) => {
    const idxData = data.index_data;
    if (!idxData || idxData.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">📊</span> 主要指数`));
    const wrap = _el("div", "dash-index-wrap");
    for (const idx of idxData) {
      const pct = idx.change_pct != null ? idx.change_pct : 0;
      const cls = pct > 0 ? "up" : (pct < 0 ? "down" : "flat");
      const sign = pct > 0 ? "+" : "";
      wrap.innerHTML +=
        `<div class="dash-index-item ${cls}">` +
          `<div class="dash-index-name">${esc(idx.name || "")}</div>` +
          `<div class="dash-index-pct">${sign}${pct.toFixed(2)}%</div>` +
        `</div>`;
    }
    section.appendChild(wrap);
    return section;
  };

  /* updown_grid: 涨跌家数 */
  SECTION_RENDERERS.updown_grid = (data) => {
    const breadth = data.market_breadth || {};
    if (data.market_temperature == null && Object.keys(breadth).length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">📈</span> 涨跌家数`));
    const wrap = _el("div", "dash-updown-wrap");
    const up = breadth.advance != null ? breadth.advance : (breadth.up != null ? breadth.up : "—");
    const down = breadth.decline != null ? breadth.decline : (breadth.down != null ? breadth.down : "—");
    const limitUp = breadth.limit_up;
    const limitDown = breadth.limit_down;
    wrap.innerHTML =
      `<div class="dash-updown-col up"><div class="dash-updown-num">${esc(String(up))}</div><div class="dash-updown-label">上涨</div></div>` +
      (limitUp != null ? `<div class="dash-updown-col up"><div class="dash-updown-num">${esc(String(limitUp))}</div><div class="dash-updown-label">涨停</div></div>` : "") +
      `<div class="dash-updown-col down"><div class="dash-updown-num">${esc(String(down))}</div><div class="dash-updown-label">下跌</div></div>` +
      (limitDown != null ? `<div class="dash-updown-col down"><div class="dash-updown-num">${esc(String(limitDown))}</div><div class="dash-updown-label">跌停</div></div>` : "");
    section.appendChild(wrap);
    return section;
  };

  /* text_block: 通用文本块（成交量描述、资金行为等） */
  SECTION_RENDERERS.text_block = (data, section) => {
    const field = (section.fields || []).find(f => data[f] != null);
    if (!field) return null;
    const val = data[field];
    if (typeof val === "string" && val.length === 0) return null;
    const s = _el("div", "dash-section");
    const icon = section.icon || "💰";
    const titleMap = { volume_summary: "成交量", capital_behavior: "资金行为" };
    const title = section.title || titleMap[field] || field;
    s.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">${icon}</span> ${esc(title)}`));
    s.appendChild(_el("div", "dash-volume-text", esc(String(val))));
    return s;
  };

  /* sector_tags: 板块强弱标签 */
  SECTION_RENDERERS.sector_tags = (data) => {
    const strong = data.strong_sectors || [];
    const weak = data.weak_sectors || [];
    if (strong.length === 0 && weak.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">🏷️</span> 板块强弱`));
    const wrap = _el("div", "dash-sector-strength-wrap");
    if (strong.length > 0) {
      const row = _el("div", "dash-sector-strength-row");
      row.innerHTML = `<span class="dash-strength-label up">强势</span>` +
        strong.map(s => `<span class="dash-sector-tag up">${esc(typeof s === "string" ? s : s.name || "")}</span>`).join("");
      wrap.appendChild(row);
    }
    if (weak.length > 0) {
      const row = _el("div", "dash-sector-strength-row");
      row.innerHTML = `<span class="dash-strength-label down">弱势</span>` +
        weak.map(s => `<span class="dash-sector-tag down">${esc(typeof s === "string" ? s : s.name || "")}</span>`).join("");
      wrap.appendChild(row);
    }
    section.appendChild(wrap);
    return section;
  };

  /* stage_bar: 阶段进度条（板块阶段 / 热点生命周期） */
  SECTION_RENDERERS.stage_bar = (data, section) => {
    const field = (section.fields || []).find(f => data[f] != null);
    if (!field) return null;
    const val = data[field];
    if (!val) return null;
    const dt = (data.decision_type || "hold").toLowerCase();
    let pos = 40, label = String(val), arrow = "→";
    const stageMap = {
      "启动": { pos: 20, arrow: "⬆" }, "主升": { pos: 65, arrow: "⬆" },
      "分化": { pos: 50, arrow: "→" }, "退潮": { pos: 15, arrow: "⬇" },
      "爆发期": { pos: 20, arrow: "⬆" }, "分化期": { pos: 55, arrow: "→" }, "退潮期": { pos: 15, arrow: "⬇" },
      "蓄势": { pos: 30, arrow: "→" },
    };
    const mapped = stageMap[val];
    if (mapped) { pos = mapped.pos; arrow = mapped.arrow; }
    const trendPosMap = { bullish: 65, neutral: 40, bearish: 15 };
    if (field === "trend_prediction" && trendPosMap[val] != null) {
      pos = trendPosMap[val];
      const info = _stageInfo(val);
      label = info.label;
      arrow = info.arrow;
    }
    const s = _el("div", "dash-section");
    s.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">📍</span> ${section.title || "阶段"}`));
    const wrap = _el("div", "dash-stage-wrap");
    wrap.innerHTML =
      `<span class="dash-stage-end">蓄势</span>` +
      `<div class="dash-stage-track"><div class="dash-stage-fill ${dt}" style="width:${pos}%"></div><div class="dash-stage-dot ${dt}" style="left:${pos}%"></div></div>` +
      `<span class="dash-stage-end dash-stage-end-right">退潮</span>`;
    wrap.appendChild(_el("div", `dash-stage-indicator ${dt}`,
      `<span class="dash-stage-arrow">${arrow}</span>` +
      `<span class="dash-stage-text">${esc(label)}</span>`
    ));
    s.appendChild(wrap);
    return s;
  };

  /* leader_cards: 龙头辨识 */
  SECTION_RENDERERS.leader_cards = (data) => {
    const stocks = data.leading_stocks;
    if (!stocks || stocks.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">🏆</span> 龙头辨识`));
    const list = _el("div", "dash-leader-list");
    for (const s of stocks) {
      const role = (s.role || s.type || "leader").toLowerCase();
      let roleCls = "leader"; let roleLabel = "龙头";
      if (role === "follower" || role === "跟风") { roleCls = "follower"; roleLabel = "跟风"; }
      if (role === "laggard" || role === "补涨") { roleCls = "laggard"; roleLabel = "补涨"; }
      list.appendChild(_el("div", `dash-leader-card ${roleCls}`,
        `<span class="dash-leader-role ${roleCls}">${esc(roleLabel)}</span>` +
        `<span class="dash-leader-name">${esc(s.name || "")}</span>` +
        `<span class="dash-leader-detail">${esc(s.detail || s.reason || "")}</span>`
      ));
    }
    section.appendChild(list);
    return section;
  };

  /* candidate_table: 候选标的 */
  SECTION_RENDERERS.candidate_table = (data, section) => {
    const field = (section.fields || []).find(f => Array.isArray(data[f]) && data[f].length > 0);
    if (!field) return null;
    const stocks = data[field];
    const sectionEl = _el("div", "dash-section");
    sectionEl.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">🏆</span> ${section.title || "候选标的"}`));
    const table = _el("div", "dash-candidate-table");
    for (const s of stocks) {
      const row = _el("div", "dash-candidate-row");
      row.innerHTML =
        `<div class="dash-candidate-rank">${esc(String(s.rank || ""))}</div>` +
        `<div class="dash-candidate-info">` +
          `<div class="dash-candidate-name">${esc(s.name || "")}${s.ticker ? ' <span class="dash-candidate-ticker">' + esc(s.ticker) + '</span>' : ""}</div>` +
          (s.reason ? `<div class="dash-candidate-reason">${esc(s.reason)}</div>` : "") +
          `<div class="dash-candidate-tags">` +
            (s.strength ? `<span class="dash-ctag strength">${esc(s.strength)}</span>` : "") +
            (s.weakness ? `<span class="dash-ctag weakness">${esc(s.weakness)}</span>` : "") +
            (s.suitability ? `<span class="dash-ctag suit">${esc(s.suitability)}</span>` : "") +
          `</div>` +
        `</div>`;
      table.appendChild(row);
    }
    sectionEl.appendChild(table);
    return sectionEl;
  };

  /* action_text: 操作建议文本 */
  SECTION_RENDERERS.action_text = (data) => {
    const val = data.portfolio_action;
    if (!val) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">💼</span> 操作建议`));
    section.appendChild(_el("div", "dash-action-text", esc(val)));
    return section;
  };

  /* key_points: 关键要点 */
  SECTION_RENDERERS.key_points = (data) => {
    const pts = data.key_points;
    if (!pts || pts.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">💡</span> 关键要点`));
    const list = _el("ul", "dash-key-points");
    for (const p of pts) {
      if (typeof p === "string" && p.trim()) list.appendChild(_el("li", "dash-key-point", esc(p)));
    }
    section.appendChild(list);
    return section;
  };

  /* action_items: 行动项 */
  SECTION_RENDERERS.action_items = (data) => {
    const items = data.action_items;
    if (!items || items.length === 0) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">✅</span> 行动项`));
    const list = _el("ul", "dash-action-items");
    for (const it of items) {
      if (typeof it === "string" && it.trim()) {
        list.appendChild(_el("li", "dash-action-item", esc(it)));
      } else if (it && typeof it === "object" && it.action) {
        list.appendChild(_el("li", "dash-action-item", esc(it.action)));
      }
    }
    section.appendChild(list);
    return section;
  };

  /* position_guidance: 仓位指引 */
  SECTION_RENDERERS.position_guidance = (data) => {
    const g = data.position_guidance;
    if (g == null || (typeof g === "object" && Object.keys(g).length === 0)) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">⚖️</span> 仓位指引`));
    if (typeof g === "string") {
      section.appendChild(_el("div", "dash-position-text", esc(g)));
    } else {
      const pos = g.仓位 || g.position || "";
      const reason = g.理由 || g.reason || "";
      section.appendChild(_el("div", "dash-position-text",
        (pos ? `<span class="dash-position-badge">${esc(pos)}</span>` : "") +
        (reason ? `<span class="dash-position-reason">${esc(reason)}</span>` : "")
      ));
    }
    return section;
  };

  /* falsification_signal: 证伪信号 */
  SECTION_RENDERERS.falsification_signal = (data) => {
    const sig = data.falsification_signal;
    if (sig == null || !String(sig).trim()) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">🚫</span> 证伪信号`));
    section.appendChild(_el("div", "dash-falsification", esc(String(sig))));
    return section;
  };

  /* event_card: 事件影响卡片（热点事件专用） */
  SECTION_RENDERERS.event_card = (data) => {
    const val = data.event_impact;
    if (!val) return null;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">📰</span> 事件影响`));
    section.appendChild(_el("div", "dash-action-text", esc(val)));
    return section;
  };

  /* heat_gauge: 情绪热度仪表盘（热点事件专用） */
  SECTION_RENDERERS.heat_gauge = (data) => {
    const score = data.sentiment_heat;
    if (score == null) return null;
    const dt = (data.decision_type || "hold").toLowerCase();
    const color = DASH_COLORS[dt] || DASH_COLORS.hold;
    const section = _el("div", "dash-section");
    section.appendChild(_el("div", "dash-section-title", `<span class="dash-section-icon">🌡️</span> 情绪热度`));
    const wrap = _el("div", "dash-heat-gauge");
    wrap.innerHTML =
      `<div class="dash-heat-bar">` +
        `<div class="dash-heat-fill" style="width:${score}%;background:${color}"></div>` +
      `</div>` +
      `<div class="dash-heat-score">${score}/100</div>`;
    section.appendChild(wrap);
    return section;
  };

  /* ── 默认 sections（向后兼容：没有 dashboard_id 时的降级方案） ── */
  const _DEFAULT_SECTIONS = [
    { renderer: "signal_row", required: true },
    { renderer: "verdict_text", required: true },
    { renderer: "price_grid" },
    { renderer: "checklist" },
    { renderer: "advice_cards" },
    { renderer: "risk_list" },
    { renderer: "watch_chips" },
  ];

  /* ── scenario_tag → dashboard_id 映射（向后兼容） ── */
  const _TAG_TO_DASHBOARD = {
    "市场": "market",
    "板块": "sector",
    "筛选": "screening",
    "热点": "hot_events",
    "个股": "stock",
  };

  /* ── 主渲染函数 ── */
  function renderDashboardCard(data) {
    const dt = (data.decision_type || "hold").toLowerCase();
    const color = DASH_COLORS[dt] || DASH_COLORS.hold;
    const card = _el("div", "dashboard-card");

    /* 确定仪表盘定义 */
    let sections = null;
    let displayName = data.scenario_tag || "决策仪表盘";
    const dashboardId = data.dashboard_id || "";

    if (dashboardId) {
      const defn = _getDashboardDef(dashboardId);
      if (defn && defn.sections) {
        sections = defn.sections;
        displayName = defn.display_name || defn.scenario_tag || displayName;
      }
    }

    if (!sections) {
      /* 向后兼容：根据 scenario_tag 推断 dashboard_id */
      const tag = data.scenario_tag || "";
      for (const [keyword, did] of Object.entries(_TAG_TO_DASHBOARD)) {
        if (tag.includes(keyword)) {
          const defn = _getDashboardDef(did);
          if (defn && defn.sections) {
            sections = defn.sections;
            displayName = defn.display_name || defn.scenario_tag || displayName;
          }
          break;
        }
      }
      if (!sections) sections = _DEFAULT_SECTIONS;
    }

    /* Header */
    const headerTag = data.stock_name || data.sector_name || data.scenario_tag || "";
    card.appendChild(_el("div", "dash-header",
      `<div class="dash-header-icon">📊</div>` +
      `<h2>${esc(displayName)}</h2>` +
      (headerTag ? `<span class="dash-stock-tag">${esc(headerTag)}</span>` : "")
    ));

    /* 按 sections 顺序渲染 */
    for (const section of sections) {
      const renderer = SECTION_RENDERERS[section.renderer];
      if (!renderer) continue;

      /* 检查是否有数据 */
      if (!_hasFieldData(data, section.fields)) {
        if (section.required) {
          /* required section: 仍渲染（会显示 ——） */
        } else {
          continue;
        }
      }

      const el = renderer(data, section, dt, color);
      if (el) card.appendChild(el);
    }

    /* Footer */
    card.appendChild(_el("div", "dash-footer",
      `<span class="dash-disclaimer">⚠️ 仅供参考，不构成投资建议</span>` +
      (data.generated_at ? `<span>${esc(data.generated_at)}</span>` :
       `<span>${new Date().toLocaleString("zh-CN")} 生成</span>`)
    ));

    return card;
  }

  return { renderDashboardCard };
})();
