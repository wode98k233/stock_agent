/* AI 信号评测 —— 回测面板子页签渲染
 * v2（2026-09-07）：整体布局重构
 *   · 12 栅格分区：净值曲线 / 校准 / 矩阵 / TOP5 / 样本 / 基准 / 未识别
 *   · 主 KPI（4 张强调卡）+ 次指标筛选条（点击即筛样本表，单一筛选入口）
 *   · sticky 顶部工具条；长表内部滚动 + 表头吸顶
 *   · 修复：--up-bg/--down-bg 未定义导致 chip 无底色；补标交易所前缀判断写错
 * v2.1（2026-09-08）：未识别清单降噪（后端配套）
 *   · 大盘日评（"今日大盘总结"类）自动归档 is_market_wide=1，不再堆进未识别
 *   · 识别成功后自动清理同 message 的 NULL 残留行
 *   · 分区说明文案更新：未识别清单只留个股/ETF 可补标条目
 */
(function () {
  "use strict";

  const CYCLES = [5, 10, 20, 30, 60];
  const COSTS = [0, 0.05, 0.1];
  const $ = (s) => document.querySelector(s);
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = (v, d = 1) => (v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(d) + "%");
  const tone = (v) => (v == null ? "neutral" : v > 0 ? "up" : v < 0 ? "down" : "neutral");

  const SignalEval = {
    filter: "all",
    cost: 0, // 单边交易成本%（0 = 毛口径）
    _data: { overview: null, matrix: null, signals: null, top: null, unrecognized: null, portfolio: null, calibration: null },
    _rescanTimer: null,

    /* ── 打开/刷新 ── */
    async open() {
      const view = $("#btSignalEvalView");
      if (!view) return;
      view.style.display = "flex";
      view.innerHTML = '<div class="bt-signal-eval-loading">加载中...</div>';
      try {
        const q = this.cost ? `?cost=${this.cost}` : "";
        await Promise.all([
          window.StockRadar.api.fetchJSON(`/api/signal-eval/overview${q}`).then((d) => (this._data.overview = d.data)),
          window.StockRadar.api.fetchJSON(`/api/signal-eval/matrix${q}`).then((d) => (this._data.matrix = d.data)),
          window.StockRadar.api.fetchJSON(`/api/signal-eval/signals?size=200${this.cost ? "&cost=" + this.cost : ""}`).then((d) => (this._data.signals = d.data)),
          window.StockRadar.api.fetchJSON(`/api/signal-eval/top${q}`).then((d) => (this._data.top = d.data)),
          window.StockRadar.api.fetchJSON(`/api/signal-eval/portfolio${q}`).then((d) => (this._data.portfolio = d.data)).catch(() => (this._data.portfolio = null)),
          window.StockRadar.api.fetchJSON("/api/signal-eval/unrecognized?size=50").then((d) => (this._data.unrecognized = d.data)).catch(() => (this._data.unrecognized = null)),
          window.StockRadar.api.fetchJSON(`/api/signal-eval/calibration${q}`).then((d) => (this._data.calibration = d.data)).catch(() => (this._data.calibration = null)),
        ]);
        this.render();
      } catch (e) {
        view.innerHTML = `<div class="se-empty">加载失败：${esc(e.message)}<br>
          <button class="se-btn primary" id="seRetryBtn">重试</button></div>`;
        $("#seRetryBtn")?.addEventListener("click", () => this.open());
      }
    },

    /* ── 成本开关：切换后重拉 ── */
    setCost(v) {
      if (this.cost === v) return;
      this.cost = v;
      this.open();
    },

    /* ── 重扫消息库：异步任务 + 进度轮询 ── */
    async rescan() {
      const btn = $("#seRescanBtn");
      const bar = $("#seRescanBar");
      const fill = $("#seRescanFill");
      const txt = $("#seRescanTxt");
      if (btn) btn.disabled = true;
      try {
        const d = await window.StockRadar.api.fetchJSON("/api/signal-eval/rescan", { method: "POST" });
        const taskId = d.data && d.data.task_id;
        if (!taskId) throw new Error("未返回任务ID");
        if (bar) bar.style.display = "flex";
        const poll = async () => {
          try {
            const s = await window.StockRadar.api.fetchJSON(`/api/signal-eval/rescan/${taskId}`);
            const p = s.data || {};
            if (fill) fill.style.width = `${Math.round((p.pct || 0) * 100)}%`;
            if (txt) txt.textContent = p.status === "completed"
              ? `完成 · 新增 ${p.added} 条`
              : p.status === "failed" ? `失败：${p.error || "未知错误"}` : "扫描中...";
            if (p.status === "running") {
              this._rescanTimer = setTimeout(poll, 1000);
            } else {
              if (btn) btn.disabled = false;
              setTimeout(() => { if (bar) bar.style.display = "none"; }, 2000);
              if (p.status === "completed" && p.added > 0) this.open();
            }
          } catch (e) {
            if (txt) txt.textContent = `进度查询失败：${e.message}`;
            if (btn) btn.disabled = false;
          }
        };
        poll();
      } catch (e) {
        if (btn) btn.disabled = false;
        if (txt) { if (bar) bar.style.display = "flex"; txt.textContent = `启动失败：${e.message}`; }
      }
    },

    /* ══════════ 骨架 ══════════ */
    render() {
      const view = $("#btSignalEvalView");
      const costSeg = COSTS.map((v) =>
        `<button class="se-seg-btn${this.cost === v ? " active" : ""}" data-cost="${v}">${v === 0 ? "不计费" : v + "%"}</button>`
      ).join("");
      view.innerHTML = `
        <div class="se-page">
          <header class="se-head">
            <div class="se-head-txt">
              <h2>AI 信号评测</h2>
              <p>统计维度 = 标的 · 数据来自历史 dashboard 决策</p>
            </div>
            <div class="se-head-act">
              <div class="se-field">
                <span class="se-field-lbl">交易成本</span>
                <span class="se-seg" title="单边交易成本，买卖各扣一次">${costSeg}</span>
              </div>
              <button class="se-btn" id="seRescanBtn">↻ 重扫消息库</button>
              <div class="se-rescan-bar" id="seRescanBar" style="display:none">
                <div class="se-rescan-track"><div class="se-rescan-fill" id="seRescanFill"></div></div>
                <span id="seRescanTxt">扫描中...</span>
              </div>
            </div>
          </header>

          <div class="se-kpi" id="seOverview"></div>
          <div class="se-stats" id="seStats"></div>

          <div class="se-grid">
            <section class="se-section se-span-6">
              <div class="se-hd">
                <h3>组合净值曲线</h3>
                <span class="tag">buy 收盘建仓 / sell 收盘平仓 · 等权独立子仓位 · 净值=1 起</span>
              </div>
              <div class="se-bd" id="sePortfolio"></div>
            </section>

            <section class="se-section se-span-6">
              <div class="se-hd">
                <h3>胜率矩阵</h3>
                <span class="tag">方向 × 周期 · 样本≥5 显示 95%CI</span>
              </div>
              <div class="se-bd se-matrix-wrap"><table class="se-matrix" id="seMatrix"></table></div>
            </section>

            <section class="se-section se-span-12">
              <div class="se-hd">
                <h3>校准 · 预测置信度 vs 实现胜率</h3>
                <span class="tag">A2 · 分桶统计 · 柱状=实现胜率，竖线=平均预测</span>
              </div>
              <div class="se-bd" id="seCalibration"></div>
            </section>

            <section class="se-section se-span-12">
              <div class="se-hd">
                <h3>极端案例 TOP5</h3>
                <span class="tag">buy 正向 · sell 反向（卖飞 / 卖对）</span>
              </div>
              <div class="se-top-grid" id="seTop"></div>
            </section>

            <section class="se-section se-span-12">
              <div class="se-hd">
                <h3>信号样本</h3>
                <span class="tag">点行展开事件流 · 点标的 / 周期跳 K 线</span>
                <span class="spacer"></span>
                <span class="se-filterbar"><span id="seFilterInfo"></span></span>
              </div>
              <div class="se-bd se-tbl-wrap scroll-y"><table class="se-samples" id="seSamples"></table></div>
            </section>

            <section class="se-section se-span-12">
              <div class="se-hd">
                <h3>基准对比 · alpha</h3>
                <span class="tag">信号收益 − 同期行业基准/沪深300 · 未识别与大盘类不进本表</span>
              </div>
              <div class="se-bd">
                <div id="seBenchSummary"></div>
                <div class="se-tbl-wrap scroll-y"><table class="se-samples" id="seBench"></table></div>
              </div>
            </section>

            <section class="se-section se-span-12" id="seUnrecSection" style="display:none">
              <div class="se-hd">
                <h3>未识别标的标注</h3>
                <span class="tag">个股/ETF 提问但名称未识别 · 补标后立即参与统计 · 大盘日评已自动归档不在此列</span>
              </div>
              <div class="se-bd se-tbl-wrap" id="seUnrecognized"></div>
            </section>
          </div>
        </div>`;

      document.querySelectorAll("#btSignalEvalView .se-seg-btn").forEach((b) =>
        b.addEventListener("click", () => this.setCost(parseFloat(b.dataset.cost))));
      $("#seRescanBtn")?.addEventListener("click", () => this.rescan());

      this.renderKpi();
      this.renderStats();
      this.renderPortfolio();
      this.renderMatrix();
      this.renderCalibration();
      this.renderTop();
      this.renderSamples();
      this.renderBench();
      this.renderUnrecognized();
    },

    /* ══════════ 主 KPI（4 张） ══════════ */
    renderKpi() {
      const d = this._data.overview || {};
      const w = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
      const ciTxt = d.buy_ci ? `95%CI ${Math.round(d.buy_ci[0] * 100)}~${Math.round(d.buy_ci[1] * 100)}%` : "样本不足";
      const cards = [
        {
          l: "buy 至今胜率", v: w(d.buy_winrate),
          cls: (d.buy_winrate || 0) >= 0.6 ? "up" : "down", hint: ciTxt, accent: "var(--up)",
        },
        {
          l: "每笔期望", v: d.expectancy == null ? "—" : (d.expectancy > 0 ? "+" : "") + d.expectancy.toFixed(2) + "%",
          cls: (d.expectancy || 0) > 0 ? "up" : "down", hint: "均盈 / 均亏差值", accent: "var(--up)",
        },
        {
          l: "盈亏比", v: d.payoff == null ? "—" : d.payoff.toFixed(2),
          cls: (d.payoff || 0) >= 1 ? "up" : "down", hint: "> 1 才划算", accent: "var(--brand)",
        },
        {
          l: "平均 alpha", v: d.avg_alpha == null ? "—" : (d.avg_alpha > 0 ? "+" : "") + d.avg_alpha.toFixed(1) + "%",
          cls: (d.avg_alpha || 0) > 0 ? "up" : "down",
          hint: d.beat_total ? `跑赢沪深300 ${d.beat_count}/${d.beat_total}` : "暂无基准数据",
          accent: "var(--brand)",
        },
      ];
      $("#seOverview").innerHTML = cards.map((c) => `
        <div class="se-kpi-card" style="--kpi-accent:${c.accent}">
          <div class="lbl">${c.l}</div>
          <div class="val ${c.cls}">${c.v}</div>
          <div class="hint">${c.hint}</div>
        </div>`).join("");
    },

    /* ══════════ 次指标筛选条（点击筛选样本表） ══════════ */
    renderStats() {
      const items = (this._data.signals || {}).items || [];
      const n = {
        all: items.length,
        buy: items.filter((s) => s.decision === "buy").length,
        sell: items.filter((s) => s.decision === "sell").length,
        holding: items.filter((s) => s.decision === "buy" && !s.paired).length,
        paired: items.filter((s) => s.decision === "buy" && s.paired).length,
      };
      const ov = this._data.overview || {};
      const btns = [
        { k: "all", l: "全部信号" },
        { k: "buy", l: "buy 买入" },
        { k: "sell", l: "sell 卖出" },
        { k: "holding", l: "持仓中" },
        { k: "paired", l: "配对平仓" },
      ];
      const extra = this.filter === "sell" && ov.sell_winrate_reverse != null
        ? `<span class="se-stat" style="cursor:default"><span class="k">sell 反向胜率</span>
             <span class="v">${Math.round(ov.sell_winrate_reverse * 100)}%</span></span>`
        : "";
      $("#seStats").innerHTML = btns.map((b) => `
        <div class="se-stat ${this.filter === b.k ? "active" : ""}" data-f="${b.k}">
          <span class="k">${b.l}</span><span class="v">${n[b.k]}</span>
        </div>`).join("") + extra;
      document.querySelectorAll("#seStats .se-stat[data-f]").forEach((el) =>
        el.addEventListener("click", () => this.setFilter(el.dataset.f)));
    },

    setFilter(f) {
      this.filter = f;
      if (!this._data.signals) return;
      this.renderStats();
      this.renderSamples();
      const info = $("#seFilterInfo");
      if (info) {
        const label = { all: "全部", buy: "buy 买入", sell: "sell 卖出", holding: "持仓中", paired: "配对平仓" }[f] || f;
        const total = this._filterSignals().length;
        info.innerHTML = `当前：<b>${label}</b> · ${total} 条` +
          (f === "all" ? "" : ` <button class="se-fb-btn" id="seFilterReset" style="margin-left:4px">清除筛选</button>`);
        $("#seFilterReset")?.addEventListener("click", () => this.setFilter("all"));
      }
    },

    _filterSignals() {
      const items = (this._data.signals || {}).items || [];
      const f = this.filter;
      return items.filter((s) => {
        if (f === "buy") return s.decision === "buy";
        if (f === "sell") return s.decision === "sell";
        if (f === "holding") return s.decision === "buy" && !s.paired;
        if (f === "paired") return s.decision === "buy" && s.paired;
        return true;
      });
    },

    /* ══════════ 组合净值曲线 ══════════ */
    renderPortfolio() {
      const el = $("#sePortfolio");
      if (!el) return;
      const d = this._data.portfolio;
      if (!d || !d.dates || d.dates.length < 2) {
        el.innerHTML = '<div class="se-empty">可统计样本不足，暂无组合净值<span class="se-empty-sub">需至少一个 buy / sell 信号 + K 线</span></div>';
        return;
      }
      const W = 760, H = 250, PL = 46, PR = 16, PT = 14, PB = 26;
      const hasBench = (d.bench || []).some((v) => v != null);
      const vals = d.equity.concat(hasBench ? d.bench.filter((v) => v != null) : [], [1.0]);
      let lo = Math.min(...vals), hi = Math.max(...vals);
      const pad = (hi - lo) * 0.08 || 0.05;
      lo = Math.max(0, lo - pad); hi = hi + pad;
      const n = d.dates.length;
      const x = (i) => PL + (W - PL - PR) * (i / (n - 1));
      const y = (v) => PT + (H - PT - PB) * (1 - (v - lo) / (hi - lo));
      const segs = (arr) => {
        const out = [];
        let cur = [];
        arr.forEach((v, i) => {
          if (v == null) { if (cur.length > 1) out.push(cur); cur = []; }
          else cur.push([x(i), y(v)]);
        });
        if (cur.length > 1) out.push(cur);
        return out;
      };
      const toPts = (seg) => seg.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(" ");

      let grid = "";
      for (let g = 0; g <= 4; g++) {
        const v = lo + (hi - lo) * g / 4;
        grid += `<line x1="${PL}" y1="${y(v).toFixed(1)}" x2="${W - PR}" y2="${y(v).toFixed(1)}" stroke="var(--border-light)" stroke-width="1"/>
          <text x="6" y="${(y(v) + 3.5).toFixed(1)}" font-size="10" fill="var(--muted)" font-family="var(--font-mono)">${v.toFixed(2)}</text>`;
      }
      if (1.0 > lo && 1.0 < hi) {
        grid += `<line x1="${PL}" y1="${y(1.0).toFixed(1)}" x2="${W - PR}" y2="${y(1.0).toFixed(1)}" stroke="var(--line-strong, #cbd3dc)" stroke-width="1" stroke-dasharray="3 3"/>`;
      }
      const xIdx = [...new Set([0, Math.floor((n - 1) / 3), Math.floor((2 * (n - 1)) / 3), n - 1])];
      const xLbl = xIdx.map((i) =>
        `<text x="${x(i).toFixed(1)}" y="${H - 8}" font-size="10" fill="var(--muted)" font-family="var(--font-mono)" text-anchor="middle">${d.dates[i].slice(5)}</text>`).join("");

      const eqSegs = segs(d.equity);
      const eqLines = eqSegs.map((s) => `<polyline fill="none" stroke="var(--up)" stroke-width="2.2" stroke-linejoin="round" points="${toPts(s)}"/>`).join("");
      const eqArea = eqSegs.map((s) =>
        `<polygon fill="url(#seEqGrad)" points="${toPts(s)} ${s[s.length - 1][0].toFixed(1)},${y(lo).toFixed(1)} ${s[0][0].toFixed(1)},${y(lo).toFixed(1)}"/>`).join("");
      const benchSegs = hasBench ? segs(d.bench).map((s) =>
        `<polyline fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="5 4" points="${toPts(s)}"/>`).join("") : "";

      const lastEq = d.equity[d.equity.length - 1];
      const benchRet = hasBench && d.bench[d.bench.length - 1] != null ? (d.bench[d.bench.length - 1] - 1) * 100 : null;
      const alpha = benchRet != null ? d.return - benchRet : null;
      const stat = (k, v, cls) => `<span class="se-eq-stat"><span class="k">${k}</span><span class="v ${cls}">${v}</span></span>`;
      const lastX = x(n - 1).toFixed(1), lastY = y(lastEq).toFixed(1);
      const anchor = lastX > W * 0.75 ? "end" : "start";
      const dx = anchor === "end" ? -9 : 9;

      el.innerHTML = `
        <div class="se-eq-legend">
          <span><i class="eq"></i>AI 信号组合</span>
          ${hasBench ? '<span><i class="bench"></i>沪深300</span>' : ""}
          <span><i class="one"></i>净值 1.0</span>
        </div>
        <svg class="se-eq-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
          <defs>
            <linearGradient id="seEqGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="var(--up)" stop-opacity="0.18"/>
              <stop offset="100%" stop-color="var(--up)" stop-opacity="0"/>
            </linearGradient>
          </defs>
          ${grid}${eqArea}${benchSegs}${eqLines}${xLbl}
          <circle cx="${lastX}" cy="${lastY}" r="3.5" fill="var(--up)"/>
          <text x="${(+lastX + dx).toFixed(1)}" y="${(+lastY - 9).toFixed(1)}" font-size="11" fill="var(--up)" font-weight="700"
            font-family="var(--font-mono)" text-anchor="${anchor}">${pct((lastEq - 1) * 100)}</text>
        </svg>
        <div class="se-eq-stats">
          ${stat("区间收益", pct(d.return), d.return >= 0 ? "up" : "down")}
          ${stat("最大回撤", "-" + (d.max_dd ?? 0).toFixed(2) + "%", "down")}
          ${benchRet != null ? stat("同期沪深300", pct(benchRet), benchRet >= 0 ? "up" : "down") + stat("超额 alpha", pct(alpha), alpha >= 0 ? "up" : "down") : ""}
          ${stat("持仓中子仓位", d.open_positions, "")}
        </div>`;
    },

    /* ══════════ 胜率矩阵 ══════════ */
    renderMatrix() {
      const d = this._data.matrix || { cols: CYCLES.map(String).concat("now"), rows: [] };
      const cols = d.cols || [];
      const fmtC = (c) => (c === "now" ? "至今" : c + "日");
      let html = `<thead><tr><th>方向</th>${cols.map((c) => `<th>${fmtC(c)}</th>`).join("")}<th>样本</th></tr></thead><tbody>`;
      d.rows.forEach((r) => {
        html += `<tr><td class="rl">${esc(r.label)}</td>`;
        r.cells.forEach((cell) => {
          const wr = cell.winrate;
          const heat = wr == null ? "" : (wr >= 0.6 ? "good" : (wr <= 0.4 ? "bad" : ""));
          const cls = wr == null ? "neutral" : (wr >= 0.6 ? "good" : (wr <= 0.4 ? "bad" : "neutral"));
          const sub = cell.ci
            ? `${cell.n} · ${Math.round(cell.ci[0] * 100)}~${Math.round(cell.ci[1] * 100)}%`
            : (cell.n > 0 ? String(cell.n) : "—");
          html += `<td class="se-hm ${heat}"><span class="se-wr ${cls}">${wr == null ? "—" : Math.round(wr * 100) + "%"}</span>
            <div class="se-wr-sub">${sub}</div></td>`;
        });
        html += `<td class="mono">${r.n}</td></tr>`;
      });
      html += "</tbody>";
      $("#seMatrix").innerHTML = html;
    },

    /* ══════════ 校准（A2） ══════════ */
    renderCalibration() {
      const el = $("#seCalibration");
      if (!el) return;
      const d = this._data.calibration;
      if (!d) {
        el.innerHTML = '<div class="se-empty">校准数据加载失败，请稍后重试</div>';
        return;
      }
      const total = d.sample_total || 0;
      if (total < 20) {
        el.innerHTML = `<div class="se-empty">本功能需要 ≥20 条带置信度的已实现信号，当前 ${total} 条
          <span class="se-empty-sub">新报告已带置信度评分，样本随使用逐步积累</span></div>`;
        return;
      }
      const P = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
      const statusChip = (b) => {
        if (b.status === "over") return '<span class="se-cal-flag bad">显著高估</span>';
        if (b.status === "under") return '<span class="se-cal-flag bad">显著低估</span>';
        if (b.status === "insufficient") return '<span class="se-cal-flag thin">样本不足</span>';
        return '<span class="se-cal-flag ok">健康</span>';
      };
      let rows = "";
      (d.buckets || []).forEach((b) => {
        const dev = b.status === "insufficient" || b.deviation_pp == null
          ? '<span style="color:var(--muted)">样本不足</span>'
          : (b.deviation_pp > 0 ? "+" : "") + b.deviation_pp + "pp";
        const devCls = b.deviation_pp != null && Math.abs(b.deviation_pp) > 15 ? "dev-bad" : "dev-ok";
        const bar = b.realized == null
          ? '<span style="color:var(--muted)">—</span>'
          : `<div class="se-cal-bar" title="柱状=实现胜率 ${P(b.realized)} · 竖线=平均预测 ${P(b.avg_pred)}">` +
            `<div class="se-cal-fill" style="width:${Math.round(b.realized * 100)}%"></div>` +
            `<div class="se-cal-pred" style="left:${Math.round(b.avg_pred * 100)}%"></div></div>`;
        rows += `<tr class="${b.status === "over" || b.status === "under" ? "se-cal-bad" : ""}">
          <td class="mono">${esc(b.range)}</td>
          <td class="mono">${b.n || "—"}</td>
          <td class="mono">${b.n ? P(b.avg_pred) : "—"}</td>
          <td class="mono">${b.n ? P(b.realized) : "—"}</td>
          <td>${bar}</td>
          <td class="mono ${devCls}">${dev}</td>
          <td>${statusChip(b)}</td></tr>`;
      });
      const excl = d.excluded || {};
      el.innerHTML = `
        <div class="se-cal-wrap">
          <div class="se-cal-main">
            <table class="se-cal"><thead><tr>
              <th>预测置信度桶</th><th>样本</th><th>平均预测</th><th>实现胜率</th><th>对比</th><th>偏差</th><th>状态</th>
            </tr></thead><tbody>${rows}</tbody></table>
          </div>
          <aside class="se-cal-side">
            <div class="se-cal-ece">
              <span class="k">ECE 校准总误差</span>
              <b>${d.ece == null ? "—" : (d.ece * 100).toFixed(1) + "pp"}</b>
            </div>
            <ul class="se-cal-ex">
              <li><span>参与样本</span><b>${total}</b></li>
              <li><span>hold 类（无方向）</span><b>${excl.hold_count || 0}</b></li>
              <li><span>未评级</span><b>${excl.no_confidence_count || 0}</b></li>
              <li><span>无收益窗口</span><b>${excl.no_return_count || 0}</b></li>
            </ul>
            <p class="se-cal-rule">判定规则：样本 n&lt;10 或 Wilson 区间宽度 &gt;40pp → 不出偏差结论；|偏差| &gt;15pp → 标红（over=预测高于实现 / under=反之）。</p>
          </aside>
        </div>`;
    },

    /* ══════════ 极端案例 TOP5 ══════════ */
    renderTop() {
      const d = this._data.top || {};
      const item = (rank, s, cls, note) => {
        const code = (s.symbol_key || "").slice(2);
        return `<div class="se-top-item">
            <span class="rank">${rank}</span>
            <span class="name jump" data-jump="${esc(code)}" data-jname="${esc(s.symbol_name || "")}"
              title="${esc(s.symbol_name || s.symbol_key)}">${esc(s.symbol_name || s.symbol_key)}</span>
            <span class="pct ${cls}">${pct(s.now_ret)}</span>
          </div>
          <div class="se-top-note">${(s.signal_time || "").slice(0, 10)} · ${note}</div>`;
      };
      const col = (title, list, cls, toneCls, noteFn) => `
        <div class="se-top-col ${toneCls}">
          <h4><span class="dot"></span>${title}</h4>
          <div class="se-top-list">
            ${list.length ? list.map((s, i) => item(i + 1, s, cls, noteFn(s))).join("")
              : '<div style="color:var(--muted);font-size:11.5px;padding:8px 6px">暂无</div>'}
          </div>
        </div>`;
      $("#seTop").innerHTML =
        col("buy TOP5 利润", d.buy_profit_top || [], "up", "up", () => "买入至今") +
        col("buy TOP5 亏损", d.buy_loss_top || [], "down", "down", () => "买入至今") +
        col("sell TOP5 卖飞", d.sell_fly_top || [], "up", "up", () => "卖出后涨 → 卖飞") +
        col("sell TOP5 卖对", d.sell_right_top || [], "down", "down", () => "卖出后跌 → 卖对");
    },

    /* ══════════ 信号样本表 ══════════ */
    renderSamples() {
      const items = this._filterSignals();
      const confChip = (c) => {
        if (c == null) return '<span class="se-chip flat">未评级</span>';
        const lv = c >= 0.75 ? "conf-h" : c >= 0.6 ? "conf-m" : "conf-l";
        const txt = c >= 0.75 ? "高" : c >= 0.6 ? "中" : "低";
        return `<span class="se-chip ${lv}">${txt} ${c.toFixed(2)}</span>`;
      };
      const cycCell = (s, c) => {
        const v = (s.returns || {})[c];
        if (v == null) return `<td><span class="se-ret neutral">—</span></td>`;
        return `<td><span class="se-ret ${tone(v)} jump" title="打开 K 线，标记信号日 → +${c}日区间"
          data-jump="${esc((s.symbol_key || "").slice(2))}" data-jname="${esc(s.symbol_name || "")}"
          data-jfrom="${(s.signal_time || "").slice(0, 10)}" data-jto="+${c}D">${pct(v)}</span></td>`;
      };
      const statusChip = (s) => (s.decision === "buy" && !s.paired
        ? '<span class="se-chip holding">持仓中</span>' : '<span class="se-chip flat">已了结</span>');

      let html = `<thead><tr>
        <th>标的</th><th>信号</th><th>信号日</th><th>成本价</th><th>置信度</th>
        ${CYCLES.map((c) => `<th>${c}日</th>`).join("")}<th>至今</th>
        <th>最高利润</th><th>最大亏损</th><th>状态</th><th>操作</th></tr></thead><tbody>`;
      items.forEach((s, idx) => {
        const code = esc((s.symbol_key || "").slice(2));
        html += `<tr class="sample-row" data-i="${idx}">
          <td><span class="se-sym jump" title="打开 ${esc(s.symbol_name || s.symbol_key)} K 线"
            data-jump="${code}" data-jname="${esc(s.symbol_name || "")}">
            ${esc(s.symbol_name || s.symbol_key)}<span class="code">${code}</span></span></td>
          <td><span class="se-chip ${s.decision === "buy" ? "buy" : "sell"}">${s.decision === "buy" ? "buy 买入" : "sell 卖出"}</span></td>
          <td class="num">${(s.signal_time || "").slice(0, 10)}</td>
          <td class="num">${s.entry_price != null ? s.entry_price.toFixed(2) : "—"}</td>
          <td>${confChip(s.confidence)}</td>
          ${CYCLES.map((c) => cycCell(s, c)).join("")}
          <td><span class="se-ret ${tone(s.returns?.now)}">${pct(s.returns?.now)}</span></td>
          <td><span class="se-ret up">${pct(s.peak)}</span></td>
          <td><span class="se-ret down">${pct(s.maxDD)}</span></td>
          <td>${statusChip(s)}</td>
          <td><button class="se-fb-btn" data-rec="${s.id}" data-name="${esc(s.symbol_name || "")}"
            data-price="${s.entry_price != null ? s.entry_price : ""}"
            title="按信号成本价记入持仓账本">记持仓</button></td></tr>`;
      });
      html += "</tbody>";

      const el = $("#seSamples");
      el.innerHTML = items.length
        ? html
        : `<tbody><tr><td colspan="15" class="se-empty">当前筛选下暂无信号样本</td></tr></tbody>`;

      const info = $("#seFilterInfo");
      if (info) {
        const label = { all: "全部", buy: "buy 买入", sell: "sell 卖出", holding: "持仓中", paired: "配对平仓" }[this.filter] || this.filter;
        info.innerHTML = `当前：<b>${label}</b> · ${items.length} 条` +
          (this.filter === "all" ? "" : ` <button class="se-fb-btn" id="seFilterReset" style="margin-left:4px">清除筛选</button>`);
        $("#seFilterReset")?.addEventListener("click", () => this.setFilter("all"));
      }

      // 一键记入持仓
      el.querySelectorAll("button[data-rec]").forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          const id = +btn.dataset.rec;
          const name = btn.dataset.name || "该标的";
          const qtyRaw = prompt(`记入持仓：${name}\n输入数量（股）：`, "100");
          if (qtyRaw == null) return;
          const qty = parseFloat(qtyRaw);
          if (!isFinite(qty) || qty <= 0) { alert("数量无效"); return; }
          let price = parseFloat(btn.dataset.price);
          if (!isFinite(price) || price <= 0) {
            const pRaw = prompt("该信号无成本价记录，请输入成交价：");
            if (pRaw == null) return;
            price = parseFloat(pRaw);
            if (!isFinite(price) || price <= 0) { alert("价格无效"); return; }
          }
          try {
            await window.StockRadar.api.portfolioFromSignal({ signal_id: id, qty, price });
            btn.textContent = "已记入";
            btn.disabled = true;
            alert(`已记入持仓（${name} ${qty}股 @${price.toFixed(2)}），可在「持仓」面板查看`);
          } catch (e) {
            alert(`记入失败：${e.message || e}`);
          }
        });
      });
      // 行展开事件流
      el.querySelectorAll("tr.sample-row").forEach((row) => {
        row.addEventListener("click", () => {
          const s = items[+row.dataset.i];
          const open = row.classList.contains("open");
          el.querySelectorAll("tr.sample-row.open").forEach((r) => {
            r.classList.remove("open");
            r.nextElementSibling?.remove();
          });
          if (open) return;
          row.classList.add("open");
          const tr = document.createElement("tr");
          tr.innerHTML = `<td colspan="15">${this._eventDetailHtml(s)}</td>`;
          row.after(tr);
          tr.querySelector(".close-x").addEventListener("click", () => {
            row.classList.remove("open");
            tr.remove();
          });
        });
      });
    },

    /* ── 事件流详情：时间线 + 三通道 + 标的 TOP5 ── */
    _eventDetailHtml(sig) {
      const allItems = (this._data.signals || {}).items || [];
      const symSigs = allItems
        .filter((s) => s.symbol_key === sig.symbol_key)
        .sort((a, b) => (a.signal_time || "").localeCompare(b.signal_time || ""));
      const fmtPrice = (v) => (v == null ? "—" : "¥" + v.toFixed(2));
      const code = esc((sig.symbol_key || "").slice(2));
      const name = esc(sig.symbol_name || sig.symbol_key);

      let tl = "";
      symSigs.forEach((s, i) => {
        const isBuy = s.decision === "buy";
        tl += `<div class="se-tl-node ${isBuy ? "buy" : "sell"}">
            <div class="dot"></div>
            <div class="date">${(s.signal_time || "").slice(0, 10)}</div>
            <div class="act">${isBuy ? "BUY 买入" : "SELL 卖出"}</div>
            <div class="price">${fmtPrice(s.entry_price)}</div>
          </div>`;
        if (i < symSigs.length - 1) tl += `<div class="se-tl-arrow"></div>`;
      });

      const chan = (cn, val, note, valCls) => `
        <div class="se-chan"><div class="cn">${cn}</div>
          <div class="cv ${valCls}">${pct(val)}</div><div class="note">${note}</div></div>`;

      const pairedBuys = symSigs.filter((s) => s.decision === "buy" && s.paired);
      const lastPaired = pairedBuys[pairedBuys.length - 1];
      const chanPaired = lastPaired
        ? (lastPaired.paired_ret != null
          ? chan("通道① 配对平仓（已实现）", lastPaired.paired_ret,
            lastPaired.paired_ret > 0 ? `已实现 ${pct(lastPaired.paired_ret)}` : `已实现 ${pct(lastPaired.paired_ret)}`,
            lastPaired.paired_ret > 0 ? "up" : "down")
          : chan("通道① 配对平仓", null, "配对收益数据不足", ""))
        : chan("通道① 配对平仓", null, "暂无 buy→sell 配对", "");

      const sells = symSigs.filter((s) => s.decision === "sell");
      const lastSell = sells[sells.length - 1];
      const chanSell = lastSell
        ? (lastSell.returns?.now != null
          ? chan("通道② 独立 sell（反向）", lastSell.returns.now,
            lastSell.returns.now > 0 ? "卖出后涨 → 卖飞（反面）" : "卖出后跌 → 卖对（正面）",
            lastSell.returns.now > 0 ? "down" : "up")
          : chan("通道② 独立 sell", null, "收益数据不足", ""))
        : chan("通道② 独立 sell", null, "暂无独立 sell", "");

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

      const ranked = symSigs.filter((s) => s.returns?.now != null).sort((a, b) => b.returns.now - a.returns.now);
      const profitTop = ranked.filter((s) => s.returns.now > 0).slice(0, 5);
      const lossTop = ranked.filter((s) => s.returns.now < 0).slice(0, 5);
      const sigRow = (x) => `
        <div class="se-detail-row">
          <span class="se-chip ${x.decision === "buy" ? "buy" : "sell"}">${x.decision}</span>
          <span style="font-family:var(--font-mono);font-size:11px;color:var(--muted)">${(x.signal_time || "").slice(0, 10)}</span>
          <span class="se-ret ${tone(x.returns?.now)}" style="margin-left:auto">${pct(x.returns?.now)}</span>
        </div>`;

      return `<div class="se-detail">
        <div class="se-detail-title">${name}
          <span style="color:var(--muted);font-weight:400">${code}</span>
          <button class="se-btn primary" style="margin-left:8px"
            data-jump="${code}" data-jname="${name}">打开 K 线图</button>
          <span class="close-x">✕</span>
        </div>
        <div class="se-timeline">${tl}</div>
        <div class="se-chan-grid">${chanPaired}${chanSell}${chanBuy}</div>
        ${(profitTop.length || lossTop.length) ? `
        <div class="se-detail-cols">
          <div><div class="se-detail-col-h">利润信号 TOP${profitTop.length || "—"}</div>
            ${profitTop.length ? profitTop.map(sigRow).join("") : '<div style="color:var(--muted);font-size:11.5px">无盈利信号</div>'}</div>
          <div><div class="se-detail-col-h">亏损信号 TOP${lossTop.length || "—"}</div>
            ${lossTop.length ? lossTop.map(sigRow).join("") : '<div style="color:var(--muted);font-size:11.5px">无亏损信号</div>'}</div>
        </div>` : ""}
      </div>`;
    },

    /* ══════════ 基准对比 alpha ══════════ */
    renderBench() {
      const rows = ((this._data.signals || {}).items || []).filter((s) => s.alpha != null);
      let html = `<thead><tr><th>标的</th><th>信号</th><th>信号日</th><th>至今</th><th>沪深300</th><th>alpha</th><th>跑赢</th></tr></thead><tbody>`;
      rows.slice(0, 30).forEach((s) => {
        const code = esc((s.symbol_key || "").slice(2));
        html += `<tr>
          <td><span class="se-sym jump" data-jump="${code}" data-jname="${esc(s.symbol_name || "")}">${esc(s.symbol_name || s.symbol_key)}<span class="code">${code}</span></span></td>
          <td><span class="se-chip ${s.decision === "buy" ? "buy" : "sell"}">${s.decision}</span></td>
          <td class="num">${(s.signal_time || "").slice(0, 10)}</td>
          <td><span class="se-ret ${tone(s.returns?.now)}">${pct(s.returns?.now)}</span></td>
          <td><span class="se-ret ${tone(s.bench)}">${pct(s.bench)}</span></td>
          <td><span class="se-ret ${tone(s.alpha)}">${pct(s.alpha)}</span></td>
          <td>${s.alpha > 0 ? '<span class="se-chip buy">跑赢</span>' : '<span class="se-chip sell">跑输</span>'}</td></tr>`;
      });
      html += "</tbody>";
      $("#seBench").innerHTML = rows.length
        ? html
        : `<tbody><tr><td colspan="7" class="se-empty">暂无 alpha 数据</td></tr></tbody>`;

      const sum = $("#seBenchSummary");
      if (!sum) return;
      const alphas = rows.map((s) => s.alpha);
      if (!alphas.length) { sum.innerHTML = ""; return; }
      const avg = alphas.reduce((a, b) => a + b, 0) / alphas.length;
      const beat = alphas.filter((a) => a > 0).length;
      sum.innerHTML = `<div class="se-bench-sum">
        <span>平均 alpha <b class="${avg >= 0 ? "up" : "down"}">${pct(avg)}</b></span>
        <span>跑赢基准 <b>${beat}/${alphas.length}</b></span>
        <span>跑赢占比 <b>${Math.round((beat / alphas.length) * 100)}%</b></span>
        <span>样本 <b>${alphas.length}</b> 条</span>
      </div>`;
    },

    /* ══════════ 未识别标的标注（P3） ══════════ */
    renderUnrecognized() {
      const el = $("#seUnrecognized");
      const section = $("#seUnrecSection");
      if (!el || !section) return;
      const data = this._data.unrecognized;
      if (!data || !data.total) { section.style.display = "none"; return; }
      section.style.display = "";
      const decChip = (d) => `<span class="se-chip ${d === "buy" ? "buy" : d === "sell" ? "sell" : "flat"}">${esc(d)}</span>`;
      let html = `<table><thead><tr><th>时间</th><th>决策</th><th>对话上下文</th><th>核心结论</th><th>标注</th></tr></thead><tbody>`;
      data.items.forEach((s) => {
        const ctx = esc(s.context || "");
        const verdict = esc((s.core_verdict || "").slice(0, 40));
        html += `<tr data-id="${s.id}">
          <td class="num" style="font-size:11px">${(s.signal_time || "").slice(0, 16).replace("T", " ")}</td>
          <td>${decChip(s.decision)}</td>
          <td style="color:var(--text-secondary);font-size:11.5px" title="${ctx}">${ctx || "—"}</td>
          <td style="color:var(--muted);font-size:11.5px" title="${verdict}">${verdict || "—"}</td>
          <td class="se-mark-cell">
            <input class="se-mark-input" placeholder="代码，如 600519" maxlength="6" inputmode="numeric">
            <button class="se-fb-btn se-mark-btn" data-act="mark">补标</button>
            <button class="se-fb-btn se-mark-btn" data-act="mw" title="大盘/板块类决策，不计入胜率统计">大盘类</button>
          </td></tr>`;
      });
      html += "</tbody></table>";
      el.innerHTML = html;

      el.querySelectorAll("tr[data-id]").forEach((tr) => {
        const id = tr.dataset.id;
        const input = tr.querySelector(".se-mark-input");
        const onAct = async (act) => {
          try {
            if (act === "mw") {
              await window.StockRadar.api.fetchJSON(`/api/signal-eval/signals/${id}/market-wide`,
                { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ is_market_wide: true }) });
            } else {
              const code = (input.value || "").trim();
              if (!/^\d{6}$/.test(code)) { input.style.borderColor = "var(--danger)"; return; }
              // 交易所前缀：6/9/5 开头 → 沪市，其余（0/3/4/8）→ 深/北
              const prefix = /^[695]/.test(code) ? "sh" : "sz";
              await window.StockRadar.api.fetchJSON(`/api/signal-eval/signals/${id}`,
                { method: "PATCH", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ symbol_key: prefix + code }) });
            }
            this.open(); // 刷新全部统计（补标后立即参与）
          } catch (e) {
            input && (input.title = e.message);
          }
        };
        tr.querySelector('[data-act="mark"]').addEventListener("click", () => onAct("mark"));
        tr.querySelector('[data-act="mw"]').addEventListener("click", () => onAct("mw"));
        input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") onAct("mark"); });
      });
    },

    /* ══════════ 跳转 K 线 ══════════ */
    jumpKline(code, name, opts) {
      const mark = opts && opts.from ? { from: opts.from, to: opts.to || null } : null;
      window.openKlineChart(code, mark ? { markRange: mark } : undefined);
    },

    backToStrategy() {
      const ts = $("#btTabStrategy");
      if (ts) ts.click();
    },
  };

  window._SignalEval = SignalEval;

  /* ── 统一事件委托：data-jump → 打开 K 线 ──
     用捕获阶段，保证 stopPropagation 早于行展开（tr.sample-row）监听器生效 */
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-jump]");
    if (!t) return;
    const code = t.dataset.jump;
    if (!code) return;
    e.stopPropagation();
    e.preventDefault();
    SignalEval.jumpKline(code, t.dataset.jname || "",
      t.dataset.jfrom ? { from: t.dataset.jfrom, to: t.dataset.jto || null } : null);
  }, true);

  /* ── tab 切换 ── */
  function bindTabs() {
    const tabStrategy = $("#btTabStrategy");
    const tabSignal = $("#btTabSignalEval");
    const rightTabSignal = $("#btRightTabSignalEval");
    if (!tabStrategy || !tabSignal) return;
    tabStrategy.addEventListener("click", () => switchTab(false));
    tabSignal.addEventListener("click", () => switchTab(true));
    if (rightTabSignal) rightTabSignal.addEventListener("click", () => switchTab(true));
  }

  function switchTab(signalEval) {
    const tv = $("#btStrategyView");
    const sv = $("#btSignalEvalView");
    const rv = document.querySelector(".backtest-modal .bt-right");
    const ts = $("#btTabStrategy");
    const se = $("#btTabSignalEval");
    const rightBtn = $("#btRightTabSignalEval");
    const tbLeft = document.querySelector(".backtest-modal .bt-tabbar-left");
    if (signalEval) {
      tv.style.display = "none";
      if (rv) rv.style.display = "none";
      sv.style.display = "flex";
      ts.classList.remove("active");
      se.classList.add("active");
      document.querySelectorAll(".bt-tabs .bt-tab").forEach((b) => {
        if (b === rightBtn) b.classList.add("active");
        else b.classList.remove("active");
      });
      // 进入 AI 信号评测自动最大化弹窗，避免内容被压缩
      window._btPanel?.maximize?.();
      document.querySelector(".backtest-modal")?.classList.add("eval-mode");
      if (tbLeft) tbLeft.style.display = "flex";
      SignalEval.open();
      requestAnimationFrame(() => { sv.scrollTop = 0; });
    } else {
      sv.style.display = "none";
      if (rv) rv.style.display = "flex";
      tv.style.display = "flex";
      se.classList.remove("active");
      ts.classList.add("active");
      if (rightBtn) rightBtn.classList.remove("active");
      document.querySelectorAll(".bt-tabs .bt-tab").forEach((b) => {
        if (b.dataset && b.dataset.btab === "btResultPanel") b.classList.add("active");
        else b.classList.remove("active");
      });
      document.querySelectorAll(".bt-tab-panel").forEach((p) => {
        if (p.id === "btResultPanel") p.classList.add("active");
        else p.classList.remove("active");
      });
      document.querySelector(".backtest-modal")?.classList.remove("eval-mode");
      window._btPanel?.restore?.();
      if (tbLeft) tbLeft.style.display = "none";
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindTabs);
  } else {
    bindTabs();
  }
})();
