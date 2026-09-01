/* AI 信号评测 —— 回测面板子页签渲染（对齐原型 docs/prototypes/ai-signal-eval.html） */
(function () {
  "use strict";

  const CYCLES = [5, 10, 20, 30, 60];
  const HOLD = "now";
  const $ = (s) => document.querySelector(s);

  const SignalEval = {
    filter: "all",
    _data: { overview: null, matrix: null, signals: null, top: null },

    /* ── 打开/刷新 ── */
    async open() {
      const view = $("#btSignalEvalView");
      view.style.display = "flex";
      view.innerHTML = '<div class="bt-signal-eval-loading">加载中...</div>';
      try {
        await Promise.all([
          window.StockRadar.api.fetchJSON("/api/signal-eval/overview").then((d) => (this._data.overview = d.data)),
          window.StockRadar.api.fetchJSON("/api/signal-eval/matrix").then((d) => (this._data.matrix = d.data)),
          window.StockRadar.api.fetchJSON("/api/signal-eval/signals?size=200").then((d) => (this._data.signals = d.data)),
          window.StockRadar.api.fetchJSON("/api/signal-eval/top").then((d) => (this._data.top = d.data)),
        ]);
        this.render();
      } catch (e) {
        view.innerHTML = `<div class="se-empty">加载失败：${e.message}<br><br>
          <button class="se-fb-btn" onclick="window._SignalEval?.open()">重试</button></div>`;
      }
    },

    render() {
      const view = $("#btSignalEvalView");
      view.innerHTML = `
        <div class="se-page-hint">统计维度 = 标的 · 数据来自历史 dashboard 决策</div>
        <div class="se-grid" id="seOverview"></div>
        <div class="se-section">
          <div class="se-section-title">胜率矩阵<span class="tag">方向 × 周期 · 样本≥5 显示 95%CI</span></div>
          <div class="se-body se-matrix-wrap"><table class="se-matrix" id="seMatrix"></table></div>
        </div>
        <div class="se-section">
          <div class="se-section-title">基准对比 · alpha<span class="tag">信号收益 − 同期沪深300</span></div>
          <div class="se-body se-tbl-wrap"><table class="se-samples" id="seBench"></table>
            <div id="seBenchSummary" style="margin-top:10px;font-size:12px;color:var(--text-secondary)"></div></div>
        </div>
        <div class="se-section">
          <div class="se-section-title">信号样本<span class="tag">点行展开事件流 · 点标的/周期跳 K 线</span></div>
          <div class="se-filterbar" id="seFilterBar"></div>
          <div class="se-body se-tbl-wrap"><table class="se-samples" id="seSamples"></table></div>
        </div>
        <div class="se-section">
          <div class="se-section-title">极端案例 TOP5<span class="tag">buy 正向 · sell 反向（卖飞/卖对）</span></div>
          <div class="se-top-grid" id="seTop"></div>
        </div>`;
      this.renderOverview();
      this.renderMatrix();
      this.renderBench();
      this.renderFilterBar();
      this.renderSamples();
      this.renderTop();
    },

    /* ── 概览卡 ── */
    renderOverview() {
      const d = this._data.overview || {};
      const fmt = (v, s = "") => (v == null ? "—" : v.toFixed(0) + "%");
      const ciTxt = d.buy_ci ? `95%CI ${fmt(d.buy_ci[0])}~${fmt(d.buy_ci[1])}` : "";
      const cards = [
        { l: "buy 信号", v: String(d.buy_count || 0), f: "buy", hint: "点击筛选" },
        { l: "sell 信号", v: String(d.sell_count || 0), f: "sell", hint: "点击筛选" },
        { l: "配对平仓", v: String(d.paired_count || 0), f: "paired", hint: "点击筛选" },
        { l: "持仓中", v: String(d.hold_count || 0), f: "holding", hint: "点击筛选" },
        { l: "buy 至今胜率", v: fmt(d.buy_winrate), cls: (d.buy_winrate || 0) >= 0.6 ? "up" : "down", hint: ciTxt },
        { l: "每笔期望", v: d.expectancy == null ? "—" : (d.expectancy > 0 ? "+" : "") + d.expectancy.toFixed(2) + "%",
          cls: (d.expectancy || 0) > 0 ? "up" : "down", hint: "均盈/均亏差值" },
        { l: "盈亏比", v: d.payoff == null ? "—" : d.payoff.toFixed(2), hint: ">1 才划算" },
        { l: "sell 反向胜率", v: fmt(d.sell_winrate_reverse), hint: "卖出后跌=卖对" },
        { l: "平均 alpha", v: d.avg_alpha == null ? "—" : (d.avg_alpha > 0 ? "+" : "") + d.avg_alpha.toFixed(1) + "%",
          cls: (d.avg_alpha || 0) > 0 ? "up" : "down",
          hint: d.beat_total ? `跑赢 ${d.beat_count}/${d.beat_total}` : "" },
      ];
      $("#seOverview").innerHTML = cards.map((c) => `
        <div class="se-card ${c.f ? "clickable" : ""}" ${c.f ? `data-f="${c.f}"` : ""}>
          <div class="lbl">${c.l}</div><div class="val ${c.cls || ""}">${c.v}</div>
          <div class="hint">${c.hint || ""}</div></div>`).join("");
      this._bindCards();
    },

    _bindCards() {
      document.querySelectorAll("#seOverview .se-card[data-f]").forEach((card) => {
        card.addEventListener("click", () => this.setFilter(card.dataset.f));
      });
    },

    /* ── 胜率矩阵 ── */
    renderMatrix() {
      const d = this._data.matrix || { cols: CYCLES.map(String).concat(HOLD), rows: [] };
      const cols = d.cols || [];
      const fmtC = (c) => (c === "now" ? "至今" : c + "日");
      let html = `<thead><tr><th>方向</th>${cols.map((c) => `<th>${fmtC(c)}</th>`).join("")}<th>样本</th></tr></thead><tbody>`;
      d.rows.forEach((r) => {
        html += `<tr class="${r.key === "conf" ? "conf-row" : ""}">
          <td class="rl">${r.label}</td>`;
        r.cells.forEach((cell) => {
          const wr = cell.winrate;
          const cls = wr == null ? "neutral" : (wr >= 0.6 ? "good" : (wr <= 0.4 ? "bad" : "neutral"));
          const sub = cell.ci ? `${cell.n} · ${(cell.ci[0] * 100).toFixed(0)}~${(cell.ci[1] * 100).toFixed(0)}%`
                              : (cell.n > 0 ? String(cell.n) : "—");
          html += `<td><span class="se-wr ${cls}">${wr == null ? "—" : (wr * 100).toFixed(0) + "%"}</span>
            <div class="se-wr-sub">${sub}</div></td>`;
        });
        html += `<td>${r.n}</td></tr>`;
      });
      html += "</tbody>";
      $("#seMatrix").innerHTML = html;
    },

    /* ── 基准对比 ── */
    renderBench() {
      const items = (this._data.signals || {}).items || [];
      const rows = items.filter((s) => s.alpha != null);
      const cls = (v) => (v == null ? "neutral" : (v > 0 ? "up" : (v < 0 ? "down" : "neutral")));
      const fmtPct = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(1) + "%");
      let html = `<thead><tr><th>标的</th><th>信号</th><th>信号日</th><th>至今</th><th>沪深300</th><th>alpha</th><th>跑赢</th></tr></thead><tbody>`;
      rows.slice(0, 30).forEach((s) => {
        html += `<tr>
          <td><span class="se-sym">${s.symbol_name || s.symbol_key}<span class="code">${s.symbol_key.slice(2)}</span></span></td>
          <td><span class="se-chip ${s.decision === "buy" ? "buy" : "sell"}">${s.decision}</span></td>
          <td style="font-family:var(--font-mono);font-size:11px">${(s.signal_time || "").slice(0, 10)}</td>
          <td><span class="se-ret ${cls(s.returns?.now)}">${fmtPct(s.returns?.now)}</span></td>
          <td><span class="se-ret ${cls(s.bench)}">${fmtPct(s.bench)}</span></td>
          <td><span class="se-ret ${cls(s.alpha)}">${s.alpha > 0 ? "+" : ""}${s.alpha.toFixed(1)}%</span></td>
          <td>${s.alpha > 0 ? "<span class='se-chip buy'>跑赢</span>" : "<span class='se-chip sell'>跑输</span>"}</td></tr>`;
      });
      html += "</tbody>";
      $("#seBench").innerHTML = rows.length ? html : '<tr><td colspan="7" class="se-empty">暂无 alpha 数据</td></tr>';
      const alphas = rows.map((s) => s.alpha);
      if (alphas.length) {
        const avg = alphas.reduce((a, b) => a + b, 0) / alphas.length;
        const beat = alphas.filter((a) => a > 0).length;
        $("#seBenchSummary").innerHTML =
          `平均 alpha：<strong style="color:${avg >= 0 ? "var(--up)" : "var(--down)"}">${avg >= 0 ? "+" : ""}${avg.toFixed(1)}%</strong>
           · 跑赢基准 ${beat}/${alphas.length}（${((beat / alphas.length) * 100).toFixed(0)}%）`;
      }
    },

    /* ── 样本表 ── */
    renderFilterBar() {
      const items = (this._data.signals || {}).items || [];
      const cnt = (fn) => items.filter(fn).length;
      const btns = [
        { k: "all", l: "全部" }, { k: "buy", l: "buy" }, { k: "sell", l: "sell" },
        { k: "holding", l: "持仓中" }, { k: "paired", l: "配对平仓" },
      ];
      $("#seFilterBar").innerHTML =
        '<span style="font-size:11.5px;color:var(--text-secondary)">筛选：</span>' +
        btns.map((b) => {
          const n = { all: items.length, buy: cnt((s) => s.decision === "buy"),
                      sell: cnt((s) => s.decision === "sell"),
                      holding: cnt((s) => s.decision === "buy" && !s.paired),
                      paired: cnt((s) => s.decision === "buy" && s.paired) }[b.k];
          return `<button class="se-fb-btn ${this.filter === b.k ? "active" : ""}" data-f="${b.k}">${b.l} <span class="cnt">${n}</span></button>`;
        }).join("");
      document.querySelectorAll("#seFilterBar .se-fb-btn").forEach((b) =>
        b.addEventListener("click", () => this.setFilter(b.dataset.f)));
    },

    setFilter(f) {
      this.filter = f;
      if (this._data.signals) {
        // 重渲染筛选条 + 样本表
        this.renderFilterBar();
        this.renderSamples();
      }
    },

    renderSamples() {
      const items = ((this._data.signals || {}).items || []).filter((s) => {
        if (this.filter === "buy") return s.decision === "buy";
        if (this.filter === "sell") return s.decision === "sell";
        if (this.filter === "holding") return s.decision === "buy" && !s.paired;
        if (this.filter === "paired") return s.decision === "buy" && s.paired;
        return true;
      });
      const cls = (v) => (v == null ? "neutral" : (v > 0 ? "up" : (v < 0 ? "down" : "neutral")));
      const fmtPct = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(1) + "%");
      const confChip = (c) => {
        if (c == null) return '<span class="se-chip flat">—</span>';
        return c >= 0.75 ? `<span class="se-chip conf-h">高 ${c.toFixed(2)}</span>`
             : c >= 0.6 ? `<span class="se-chip conf-m">中 ${c.toFixed(2)}</span>`
             : `<span class="se-chip conf-l">低 ${c.toFixed(2)}</span>`;
      };
      const cycCell = (s, c) => {
        const v = (s.returns || {})[c];
        if (v == null) return "<td><span class='se-ret neutral'>—</span></td>";
        return `<td><span class="se-ret ${cls(v)} jump" title="打开 K 线，标记信号日→+${c}日区间"
          onclick="event.stopPropagation(); window._SignalEval.jumpKline('${s.symbol_key.slice(2)}', '${s.symbol_name || ""}', {from:'${(s.signal_time || "").slice(0, 10)}', to:'+${c}D'})">${fmtPct(v)}</span></td>`;
      };
      const statusChip = (s) => (s.decision === "buy" && !s.paired
        ? '<span class="se-chip holding">持仓中</span>' : '<span class="se-chip flat">已了结</span>');
      let html = `<thead><tr>
        <th>标的</th><th>信号</th><th>信号日</th><th>成本价</th><th>置信度</th>
        ${CYCLES.map((c) => `<th>${c}日</th>`).join("")}<th>至今</th>
        <th>最高利润</th><th>最大亏损</th><th>状态</th></tr></thead><tbody>`;
      items.forEach((s, idx) => {
        html += `<tr class="sample-row" data-i="${idx}">
          <td><span class="se-sym jump" title="打开 ${s.symbol_name || s.symbol_key} K 线"
            onclick="event.stopPropagation(); window._SignalEval.jumpKline('${s.symbol_key.slice(2)}', '${s.symbol_name || ""}')">
            ${s.symbol_name || s.symbol_key}<span class="code">${s.symbol_key.slice(2)}</span></span></td>
          <td><span class="se-chip ${s.decision === "buy" ? "buy" : "sell"}">${s.decision === "buy" ? "buy 买入" : "sell 卖出"}</span></td>
          <td style="font-family:var(--font-mono);font-size:11px">${(s.signal_time || "").slice(0, 10)}</td>
          <td style="font-family:var(--font-mono)">${s.entry_price != null ? s.entry_price.toFixed(2) : "—"}</td>
          <td>${confChip(s.confidence)}</td>
          ${CYCLES.map((c) => cycCell(s, c)).join("")}
          <td><span class="se-ret ${cls(s.returns?.now)}">${fmtPct(s.returns?.now)}</span></td>
          <td><span class="se-ret up">${fmtPct(s.peak)}</span></td>
          <td><span class="se-ret down">${fmtPct(s.maxDD)}</span></td>
          <td>${statusChip(s)}</td></tr>`;
      });
      html += "</tbody>";
      const el = $("#seSamples");
      el.innerHTML = items.length ? html : '<tr><td colspan="13" class="se-empty">暂无信号样本</td></tr>';
      // 行展开事件流（按标的分组：时间线 + 三通道 + 标的TOP5）
      el.querySelectorAll(".sample-row").forEach((row) => {
        row.addEventListener("click", () => {
          const s = items[+row.dataset.i];
          const open = row.classList.contains("open");
          el.querySelectorAll(".sample-row.open").forEach((r) => { r.classList.remove("open"); r.nextElementSibling?.remove(); });
          if (open) return;
          row.classList.add("open");
          const tr = document.createElement("tr");
          tr.innerHTML = `<td colspan="13">${this._eventDetailHtml(s, cls, fmtPct)}</td>`;
          row.after(tr);
          tr.querySelector(".close-x").addEventListener("click", () => { row.classList.remove("open"); tr.remove(); });
        });
      });
    },

    /* ── 事件流详情（对齐原型：时间线 + 三通道 + 标的TOP5） ── */
    _eventDetailHtml(sig, cls, fmtPct) {
      const allItems = (this._data.signals || {}).items || [];
      const symSigs = allItems
        .filter((s) => s.symbol_key === sig.symbol_key)
        .sort((a, b) => (a.signal_time || "").localeCompare(b.signal_time || ""));
      const fmtPrice = (v) => (v == null ? "—" : "¥" + v.toFixed(2));
      const code = sig.symbol_key.slice(2);
      const name = sig.symbol_name || sig.symbol_key;

      /* 时间线 */
      const arrows = symSigs.length - 1;
      let tl = "";
      symSigs.forEach((s, i) => {
        const isBuy = s.decision === "buy";
        tl += `<div class="se-tl-node ${isBuy ? "buy" : "sell"}">
          <div class="dot"></div>
          <div class="date">${(s.signal_time || "").slice(0, 10)}</div>
          <div class="act">${isBuy ? "BUY 买入" : "SELL 卖出"}</div>
          <div class="price">${fmtPrice(s.entry_price)}</div></div>`;
        if (i < arrows) tl += `<div class="se-tl-arrow"></div>`;
      });

      /* 三通道 */
      const chan = (cn, val, note, valCls) => `
        <div class="se-chan"><div class="cn">${cn}</div>
        <div class="cv ${valCls}">${fmtPct(val)}</div><div class="note">${note}</div></div>`;

      /* 通道①配对平仓（已实现） */
      const pairedBuys = symSigs.filter((s) => s.decision === "buy" && s.paired);
      const lastPaired = pairedBuys[pairedBuys.length - 1];
      const chanPaired = lastPaired
        ? (lastPaired.paired_ret != null
            ? chan("通道① 配对平仓（已实现）", lastPaired.paired_ret,
                lastPaired.paired_ret > 0 ? `已实现 +${lastPaired.paired_ret.toFixed(1)}%` : `已实现 ${lastPaired.paired_ret.toFixed(1)}%`,
                lastPaired.paired_ret > 0 ? "up" : "down")
            : chan("通道① 配对平仓", null, "配对收益数据不足", ""))
        : chan("通道① 配对平仓", null, "暂无 buy→sell 配对", "");

      /* 通道②独立sell（反向） */
      const sells = symSigs.filter((s) => s.decision === "sell");
      const lastSell = sells[sells.length - 1];
      const chanSell = lastSell
        ? (lastSell.returns?.now != null
            ? chan("通道② 独立 sell（反向）", lastSell.returns.now,
                lastSell.returns.now > 0 ? "卖出后涨 → 卖飞（反面）" : "卖出后跌 → 卖对（正面）",
                lastSell.returns.now > 0 ? "down" : "up")
            : chan("通道② 独立 sell", null, "收益数据不足", ""))
        : chan("通道② 独立 sell", null, "暂无独立 sell", "");

      /* 通道③持仓buy（浮动） */
      const buys = symSigs.filter((s) => s.decision === "buy");
      const lastBuy = buys[buys.length - 1];
      const isHolding = lastBuy && !lastBuy.paired;
      const chanBuy = lastBuy
        ? (lastBuy.returns?.now != null
            ? chan("通道③ 持仓 buy（浮动）", lastBuy.returns.now,
                isHolding ? "持仓中 · 按当前价" : "已平仓",
                lastBuy.returns.now > 0 ? "up" : "down")
            : chan("通道③ 持仓 buy", null, isHolding ? "持仓中 · 收益数据不足" : "已平仓", ""))
        : chan("通道③ 持仓 buy", null, "暂无 buy", "");

      /* 标的维度：利润/亏损 TOP5 */
      const ranked = symSigs.filter((s) => s.returns?.now != null)
        .sort((a, b) => b.returns.now - a.returns.now);
      const profitTop = ranked.filter((s) => s.returns.now > 0).slice(0, 5);
      const lossTop = ranked.filter((s) => s.returns.now < 0).slice(0, 5);
      const sigRow = (x) => `
        <div style="display:flex;align-items:center;gap:8px;padding:5px 10px;border-radius:6px;background:var(--bg);margin-bottom:3px">
          <span class="se-chip ${x.decision === "buy" ? "buy" : "sell"}">${x.decision}</span>
          <span style="font-family:var(--font-mono);font-size:11px;color:var(--muted)">${(x.signal_time || "").slice(0, 10)}</span>
          <span class="se-ret ${cls(x.returns?.now)}" style="margin-left:auto">${fmtPct(x.returns?.now)}</span>
        </div>`;

      return `<div class="se-detail">
        <div class="se-detail-title">📈 ${name}
          <span style="color:var(--muted);font-weight:400">${code}</span>
          <button class="se-fb-btn" style="margin-left:8px;background:var(--brand);color:#fff;border-color:var(--brand)"
            onclick="event.stopPropagation(); window._SignalEval.jumpKline('${code}', '${name}')">📊 打开 K 线图</button>
          <span class="close-x">✕</span></div>
        <div class="se-timeline">${tl}</div>
        <div class="se-chan-grid">${chanPaired}${chanSell}${chanBuy}</div>
        ${(profitTop.length || lossTop.length) ? `
        <div style="margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div><div style="font-size:12px;font-weight:600;margin-bottom:6px">🟢 ${name} 利润信号 TOP${profitTop.length || "—"}</div>
            ${profitTop.length ? profitTop.map(sigRow).join("") : '<div style="color:var(--muted);font-size:11.5px">无盈利信号</div>'}</div>
          <div><div style="font-size:12px;font-weight:600;margin-bottom:6px">🔴 ${name} 亏损信号 TOP${lossTop.length || "—"}</div>
            ${lossTop.length ? lossTop.map(sigRow).join("") : '<div style="color:var(--muted);font-size:11.5px">无亏损信号</div>'}</div>
        </div>` : ""}
      </div>`;
    },

    /* ── TOP5 ── */
    renderTop() {
      const d = this._data.top || {};
      const fmtPct = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(1) + "%");
      const item = (rank, s, pctCls, note) => `
        <div class="se-top-item"><span class="rank">${rank}</span>
          <span class="name jump" onclick="event.stopPropagation(); window._SignalEval.jumpKline('${s.symbol_key.slice(2)}', '${s.symbol_name || ""}')">${s.symbol_name || s.symbol_key}</span>
          <span class="detail">${(s.signal_time || "").slice(0, 10)}</span>
          <span class="pct ${pctCls}">${fmtPct(s.now_ret)}</span></div>
        <div style="font-size:10.5px;color:var(--muted);padding:0 9px 4px 29px">${note}</div>`;
      const col = (title, list, pctCls, noteFn) => `
        <div class="se-top-col"><h4>${title}</h4>
          ${list.length ? list.map((s, i) => item(i + 1, s, pctCls, noteFn(s))).join("") : '<div style="color:var(--muted);font-size:11.5px">暂无</div>'}</div>`;
      $("#seTop").innerHTML =
        col("🟢 buy TOP5 利润", d.buy_profit_top || [], "up", (s) => `买入至今 ${fmtPct(s.now_ret)}`) +
        col("🔴 buy TOP5 亏损", d.buy_loss_top || [], "down", (s) => `买入至今 ${fmtPct(s.now_ret)}`) +
        col("💸 sell TOP5 卖飞", d.sell_fly_top || [], "up", (s) => `卖出后 ${fmtPct(s.now_ret)} → 卖飞`) +
        col("🛡️ sell TOP5 卖对", d.sell_right_top || [], "down", (s) => `卖出后 ${fmtPct(s.now_ret)} → 卖对`);
    },

    jumpKline(code, name, opts) {
      // 扩展 openKlineChart 支持 markRange（chart-kline.js 实现）
      const mark = opts?.from ? { from: opts.from, to: opts.to || null } : null;
      window.openKlineChart(code, mark ? { markRange: mark } : undefined);
    },

    backToStrategy() {
      // 返回策略回测视图（切顶部 tab + 恢复左右布局）
      const ts = $("#btTabStrategy");
      if (ts) ts.click();
    },
  };

  window._SignalEval = SignalEval;

  /* ── tab 切换 ── */
  function bindTabs() {
    const tabStrategy = $("#btTabStrategy");
    const tabSignal = $("#btTabSignalEval");
    const rightTabSignal = $("#btRightTabSignalEval");
    if (!tabStrategy || !tabSignal) return;
    tabStrategy.addEventListener("click", () => switchTab(false));
    tabSignal.addEventListener("click", () => switchTab(true));
    // 右侧 tab 区入口（用户习惯位置）
    if (rightTabSignal) rightTabSignal.addEventListener("click", () => switchTab(true));
  }

  function switchTab(signalEval) {
    const tv = $("#btStrategyView");
    const sv = $("#btSignalEvalView");
    const rv = document.querySelector(".backtest-modal .bt-right");
    const ts = $("#btTabStrategy");
    const se = $("#btTabSignalEval");
    const rightBtn = $("#btRightTabSignalEval");
    // 顶部选择页签容器（CSS 兜底 + JS 兜底，确保即使 CSS 缓存未刷新也能正确隐藏）
    const tbLeft = document.querySelector(".backtest-modal .bt-tabbar-left");
    if (signalEval) {
      tv.style.display = "none";
      if (rv) rv.style.display = "none";
      sv.style.display = "flex";
      ts.classList.remove("active");
      se.classList.add("active");
      // 右侧入口高亮，其他右侧 tab 取消高亮
      document.querySelectorAll(".bt-tabs .bt-tab").forEach((b) => {
        if (b === rightBtn) b.classList.add("active");
        else b.classList.remove("active");
      });
      // 进入 AI 信号评测自动最大化弹窗，避免内容被压缩
      window._btPanel?.maximize?.();
      document.querySelector('.backtest-modal')?.classList.add('eval-mode');
      if (tbLeft) tbLeft.style.display = "flex";   // JS 兜底：显示选择页签（也兼作「返回」入口）
      SignalEval.open();
    } else {
      sv.style.display = "none";
      if (rv) rv.style.display = "flex";
      tv.style.display = "flex";
      se.classList.remove("active");
      ts.classList.add("active");
      if (rightBtn) rightBtn.classList.remove("active");
      // 刷新右侧 tab 状态：回到默认的回测结果 tab，避免返回后残留 AI 信号评测高亮
      document.querySelectorAll(".bt-tabs .bt-tab").forEach((b) => {
        if (b.dataset && b.dataset.btab === "btResultPanel") b.classList.add("active");
        else b.classList.remove("active");
      });
      document.querySelectorAll(".bt-tab-panel").forEach((p) => {
        if (p.id === "btResultPanel") p.classList.add("active");
        else p.classList.remove("active");
      });
      // 返回策略回测恢复默认窗口尺寸
      document.querySelector('.backtest-modal')?.classList.remove('eval-mode');
      window._btPanel?.restore?.();
      if (tbLeft) tbLeft.style.display = "none";    // JS 兜底：策略回测模式下隐藏顶部选择页签
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindTabs);
  } else {
    bindTabs();
  }
})();
