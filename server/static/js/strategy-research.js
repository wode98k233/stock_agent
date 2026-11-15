/*
   策略研究面板 —— 想法 → 样本外验证（对标量化平台的回测报告页）
   结构：发起条 → 结论条 hero（判定 + 人话结论 + IS/OOS 两列对照 + 落地四动作）
        → 净值曲线（IS / OOS 两段 + 回撤小图）→ K 线 · 买卖点（跟随选中轮次）
        → 迭代时间线（点节点换轮次）→ 明细表（折叠）→ 历史研究（折叠）
   形态：默认全屏（研究内容多，浮窗装不下），右上角按钮切「全屏 ↔ 浮窗」
   数据：POST /api/strategy-research/run · GET runs · GET runs/{uuid} · GET runs/{uuid}/iters
        GET runs/{uuid}/iters/{round}/curve · GET runs/{uuid}/events (SSE)
        GET /api/backtest/runs/{run_id}/kline（该轮样本内账本的 K 线 + 买卖点）
        POST cancel · POST adopt（{code,data,msg} 壳）
   图表：/static/vendor/echarts.min.js（本地 vendor；jsdelivr 在部分网络不可达会让图表静默不渲染）
        K 线配置复用 chart-backtest-kline.js 的 renderBacktestKline（不复制第二份）
   入口：侧边栏「策略研究」按钮（index.html 内联 onclick，不依赖 app.js）
*/
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const API = "/api/strategy-research";
  const ECHARTS_SRC = "/static/vendor/echarts.min.js";
  const MIN_SAMPLE = 5;        // 少于 5 笔成交，胜率与夏普都不具统计意义
  // 颜色令牌统一放 variables.css（--chart-is 为样本内浅色段；样本外用 --brand）
  function cssVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }
  const COLOR_IS = cssVar("--chart-is", "#7fb3a8");
  const COLOR_OOS = "#176b5b"; // 样本外：深品牌绿（结论以它为准）
  const COLOR_DD = cssVar("--up", "#e74c3c");
  const COLOR_MUTED = cssVar("--muted", "#667085");
  const COLOR_AXIS = cssVar("--line", "#dfe4ea");
  const COLOR_SPLIT = cssVar("--border-light", "#f0f0f0");
  const COLOR_MARK = cssVar("--border-main", "#cbd3dc");
  function rgba(hex, a) {
    const m = String(hex).replace("#", "");
    const v = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
    const i = parseInt(v.slice(0, 6) || "e74c3c", 16);
    return "rgba(" + ((i >> 16) & 255) + "," + ((i >> 8) & 255) + "," + (i & 255) + "," + a + ")";
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"\x27]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "\x27": "&#39;" }[c];
    });
  }
  function pct(v) { if (v == null || isNaN(v)) return "—"; return (Number(v) * 100).toFixed(2) + "%"; }
  function num(v) { if (v == null || isNaN(v)) return "—"; return Number(v).toFixed(2); }
  function metricCls(v) { return v != null && !isNaN(v) ? (v >= 0 ? "sr-pos" : "sr-neg") : ""; }
  function day(s) { return String(s == null ? "" : s).slice(0, 10); }
  function tradesOf(m) { return m && m.trades != null && !isNaN(m.trades) ? Number(m.trades) : null; }
  function nOr(v, alt) { return v == null ? alt : v; }

  /* 成交口径中文文案：与后端 backtest/strategy_base.py 的封闭枚举一致。
     entry/exit 都由后端归一化后给出（exit 空串已被解析成跟随 entry 的实际值），
     这里只负责翻译，不再做回落判断。 */
  const TIMING_LABEL = { next_open: "次日开盘", same_close: "尾盘", next_close: "次日收盘" };
  function timingText(entry, exitT) {
    const e = TIMING_LABEL[String(entry || "").toLowerCase()] || "";
    const x = TIMING_LABEL[String(exitT || "").toLowerCase()] || "";
    if (!e && !x) return "";
    if (e && x) return e + "买入 → " + x + "卖出";
    return e ? e + "买入" : x + "卖出";
  }

  // 状态中文化：原始枚举（done/failed + stop_reason）留在 title 里备查，界面只说人话
  function statusText(status, stopReason) {
    const s = String(status || "");
    const sr = String(stopReason || "");
    if (s === "done") {
      if (sr === "converged") return "已完成 · 已收敛";
      if (sr === "max_rounds") return "已完成 · 达最大轮数";
      return "已完成";
    }
    if (s === "failed") return sr === "cancelled" ? "已取消" : "失败";
    if (s === "interrupted") return "已中断";
    if (s === "running") return "研究中";
    if (s === "pending" || s === "queued") return "排队中";
    return s || "—";
  }
  function statusCls(status, stopReason) {
    const s = String(status || "");
    if (s === "done") return "sr-st-ok";
    if (s === "failed") return String(stopReason || "") === "cancelled" ? "sr-st-mute" : "sr-st-bad";
    if (s === "interrupted") return "sr-st-warn";
    return "sr-st-run";
  }

  // 图表库按需加载（本地 vendor；其它面板也懒加载同一份，故先查 window.echarts）
  function ensureECharts() {
    return new Promise(function (resolve) {
      if (window.echarts) return resolve(true);
      const s = document.createElement("script");
      s.src = ECHARTS_SRC;
      s.onload = function () { resolve(!!window.echarts); };
      s.onerror = function () { resolve(false); };
      document.head.appendChild(s);
    });
  }

  /* 结论判定：一句话说清「这个策略到底行不行」，口径与引擎红线一致
     （OOS 为默认口径；IS/OOS 夏普比 > 1.3 或 OOS 笔数 < 5 都不得当选最优） */
  function verdictOf(card, run, resumeFrom) {
    const st = String((run && run.status) || "");
    if (!card || !card.exists) {
      if (st === "failed") {
        return { key: "failed", label: "生成失败", cls: "bad",
                 text: "这次研究没跑出可用策略：想法太模糊或 schema 约束过严，换个更具体、可量化的说法再试。" };
      }
      if (st === "interrupted") {
        return { key: "interrupted", label: "已中断", cls: "warn",
                 text: "任务跑到第 " + Math.max(1, (resumeFrom || 2) - 1) + " 轮时中断（多为服务重启）。"
                   + "已跑完的轮次都在，点「从第 " + (resumeFrom || "N") + " 轮续跑」接着跑，不用从头再来。" };
      }
      if (st === "running" || st === "pending" || st === "queued") {
        return { key: "running", label: "研究中", cls: "run",
                 text: "还在跑：每轮都做样本内 / 样本外双段回测，跑完这里会给出结论。" };
      }
      return { key: "none", label: "没有可用策略", cls: "mute",
               text: "所有轮次都没过样本外红线（IS/OOS 夏普比 ≤ 1.3 且样本笔数足够），这个想法当前形态站不住。" };
    }
    const oos = card.oos_metrics || {};
    const t = tradesOf(oos);
    const sharpe = oos.sharpe != null && !isNaN(oos.sharpe) ? Number(oos.sharpe) : null;
    const head = "第 " + card.round + " 轮 · " + (card.strategy_name || "未命名策略");
    if (card.overfit_flag) {
      return { key: "overfit", label: "过拟合嫌疑", cls: "bad", name: head,
               text: "样本内好、样本外垮，IS/OOS 夏普比越过 1.3 红线 —— 这条不能当选最优。" };
    }
    if (t != null && t < MIN_SAMPLE) {
      return { key: "lowsample", label: "样本不足", cls: "warn", name: head,
               text: "样本外只有 " + t + " 笔成交（少于 " + MIN_SAMPLE + " 笔），胜率和夏普都还说明不了问题。" };
    }
    if (sharpe != null && sharpe > 0) {
      return { key: "pass", label: "样本外验证通过", cls: "ok", name: head,
               text: "样本外夏普 " + num(sharpe) + "、年化 " + pct(oos.cagr) + "、" + nOr(t, "—") + " 笔成交 —— 过了红线，可以往下落地。" };
    }
    return { key: "negative", label: "样本外为负", cls: "bad", name: head,
             text: "样本外夏普 " + num(sharpe) + "、年化 " + pct(oos.cagr) + "、" + nOr(t, "—") + " 笔成交 —— 拿真金白银跑大概率是亏的。" };
  }

  // 时间线节点判定（比表里的徽章更短，只够放两个字）
  function nodeVerdict(it) {
    if (it.running) return { cls: "run", label: "进行中" };
    if (it.verdict === "failed") return { cls: "bad", label: "失败" };
    if (it.verdict === "best") return { cls: "ok", label: "最优" };
    if (it.verdict === "degenerate") return { cls: "warn", label: "无效" };
    if (it.overfit_flag) return { cls: "bad", label: "过拟合" };
    const t = tradesOf(it.oos_metrics);
    if (it.compile_ok && t != null && t < MIN_SAMPLE) return { cls: "warn", label: "样本少" };
    if (it.verdict === "keep" || it.verdict === "reject") return { cls: "keep", label: "保留" };
    return { cls: "mute", label: "—" };
  }

  const StrategyResearchPanel = class StrategyResearchPanel {
    constructor() {
      this.currentRun = null;      // 当前查看的 run_uuid
      this.es = null;              // EventSource
      this._iterRows = {};         // round -> iter
      this._iters = [];
      this._round = null;          // 当前选中的轮次（曲线、K 线与详情跟着它走）
      this._chart = null;
      this._ddChart = null;
      this._klineChart = null;
      this._buildDom();
      window.addEventListener("resize", () => this._resizeCharts());
    }

    /* ── DOM 构建（弹窗自建，index.html 只挂脚本与按钮） ── */
    _buildDom() {
      this.overlayEl = document.createElement("div");
      this.overlayEl.className = "sr-overlay";
      this.modalEl = document.createElement("div");
      this.modalEl.className = "sr-modal";
      this.modalEl.setAttribute("role", "dialog");
      this.modalEl.setAttribute("aria-modal", "true");
      this.modalEl.setAttribute("aria-label", "策略研究");
      this.modalEl.innerHTML = [
        "<div class='sr-header'>",
        "  <div class='sr-title'>策略研究 <span class='sr-sub'>想法 → 样本外验证 · 历史表现不代表未来收益</span></div>",
        "  <div class='sr-header-actions'>",
        "    <button class='sr-max-btn' type='button' aria-label='切回浮窗' title='切回浮窗'>"
        + "<svg class='sr-ic sr-ic-expand' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        + "<path d='M15 3h6v6'/><path d='M9 21H3v-6'/><path d='M21 3l-7 7'/><path d='M3 21l7-7'/></svg>"
        + "<svg class='sr-ic sr-ic-collapse' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        + "<path d='M4 14h6v6'/><path d='M20 10h-6V4'/><path d='M14 10l7-7'/><path d='M3 21l7-7'/></svg>"
        + "</button>",
        "    <button class='sr-close-btn' type='button' aria-label='关闭'>×</button>",
        "  </div>",
        "</div>",
        "<div class='sr-body'>",
        "  <div class='sr-launch'>",
        "    <textarea id='srIdea' rows='1' placeholder='用一句话说想法：例 尾盘买入第二天卖出 / DPO 底部上穿 MADPO + KDJ 低位共振'></textarea>",
        "    <label class='sr-field'><span>标的</span><input id='srUniverse' value='600519' /></label>",
        "    <label class='sr-field sr-field-period'><span>区间</span><input id='srPeriodStart' type='date' /><em>~</em><input id='srPeriodEnd' type='date' /></label>",
        /* 成交口径（买卖各自独立）：「尾盘买入、次日开盘卖出」这类需求必须能直接表达，
           之前面板只能表达「次日开盘买」，用户勾的口径在接口层被静默丢掉。 */
        "    <label class='sr-field'><span>买入口径</span>"
        + "<select id='srEntryTiming' title='买入成交价取哪根 bar 的哪个价'"
        + " style='height:30px;padding:0 6px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--detail-bg);color:var(--text);font-size:12.5px'>"
        + "<option value='next_open'>次日开盘</option>"
        + "<option value='same_close'>当日收盘（尾盘）</option>"
        + "<option value='next_close'>次日收盘</option></select></label>",
        "    <label class='sr-field'><span>卖出口径</span>"
        + "<select id='srExitTiming' title='卖出成交价取哪根 bar 的哪个价；跟随买入 = 买卖同口径'"
        + " style='height:30px;padding:0 6px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--detail-bg);color:var(--text);font-size:12.5px'>"
        + "<option value=''>跟随买入</option>"
        + "<option value='next_open'>次日开盘</option>"
        + "<option value='same_close'>当日收盘（尾盘）</option>"
        + "<option value='next_close'>次日收盘</option></select></label>",
        "    <label class='sr-field'><span>池子</span><input id='srPool' value='POOL:default' title='POOL:default 或 POOL:600519,510300' /></label>",
        "    <label class='sr-field'><span>池子模式</span>"
        + "<select id='srPoolMode' title='验证=逐只独立；组合=共享资金；两者=同一张报告对照'"
        + " style='height:30px;padding:0 6px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--detail-bg);color:var(--text);font-size:12.5px'>"
        + "<option value='both'>验证 + 组合</option>"
        + "<option value='validate'>只验证（逐只独立）</option>"
        + "<option value='portfolio'>只组合（共享资金）</option></select></label>",
        "    <button id='srStartBtn' class='sr-start-btn' type='button'>发起研究</button>",
        "    <button id='srPoolBtn' class='sr-action-btn' type='button' title='默认池逐只验证 + 可选组合回测'>池子验证</button>",
        "  </div>",
        "  <div id='srRunningBar' class='sr-running-bar' style='display:none;'>",
        "    <span class='sr-spinner'></span><span id='srRunningText'>研究中…</span>",
        "    <button id='srCancelBtn' class='sr-action-btn' type='button'>取消</button>",
        "  </div>",
        "  <div id='srPoolResult' class='sr-section' style='display:none;'></div>",
        "  <div id='srBestCard' class='sr-hero'><div class='sr-empty'>打开面板会自动载入最近一次研究</div></div>",
        "  <div id='srPrior' class='sr-prior' style='display:none;'></div>",
        "  <div id='srKpiSection' class='sr-section' style='display:none;'>",
        "    <div class='sr-section-title'>关键指标 <span class='sr-tag'>大数是样本外（结论以此为准），小字是样本内对照</span></div>",
        "    <div id='srKpis' class='sr-kpis'></div>",
        "  </div>",
        "  <div class='sr-section'>",
        "    <div class='sr-section-title'>净值曲线 <span class='sr-tag' id='srCurveTag'>样本内 + 样本外两段独立回测，都从 100 起算</span></div>",
        "    <div id='srCurveWarn' class='sr-curve-warn' style='display:none;'></div>",
        "    <div id='srCurve' class='sr-curve'><div class='sr-empty'>暂无曲线</div></div>",
        "    <div id='srCurveFoot' class='sr-curve-foot'></div>",
        "  </div>",
        "  <div class='sr-section'>",
        "    <div class='sr-section-title'>回撤 <span class='sr-tag' id='srDdTag'></span></div>",
        "    <div id='srDd' class='sr-dd'><div class='sr-empty'>暂无回撤</div></div>",
        "    <div id='srDdNote' class='sr-dd-note'></div>",
        "  </div>",
        "  <div class='sr-section'>",
        "    <div class='sr-section-title'>K 线 · 买卖点 <span class='sr-tag' id='srKlineTag'>跟随当前轮次（该轮样本内回测账本）</span></div>",
        "    <div id='srKlineSummary' class='sr-kline-summary'></div>",
        "    <div id='srKline' class='sr-kline'><div class='sr-empty'>暂无 K 线</div></div>",
        "  </div>",
        "  <div class='sr-section'>",
        "    <div class='sr-section-title'>迭代轮次 <span class='sr-tag'>点节点看那一轮做了什么，曲线跟着切</span>",
        "      <button id='srTableToggle' class='sr-mini-btn' type='button'>明细表</button></div>",
        "    <div id='srTimeline' class='sr-timeline'></div>",
        "    <div id='srIterDetail'></div>",
        "    <div id='srIterTableWrap' class='sr-iter-wrap' style='display:none;'>",
        "      <table class='sr-iter-table'><thead><tr>",
        "        <th>轮次</th><th>假设</th><th>IS 夏普</th><th>OOS 夏普</th>",
        "        <th>OOS 年化</th><th>OOS 回撤</th><th>OOS 笔数</th><th>判定</th>",
        "      </tr></thead><tbody id='srIterBody'><tr><td colspan='8' class='sr-empty'>发起研究或从下方历史研究选择</td></tr></tbody></table>",
        "    </div>",
        "  </div>",
        "  <div class='sr-section'>",
        "    <div class='sr-section-title'>本轮改了什么 <span class='sr-tag' id='srChangeTag'>点轮次节点切换</span></div>",
        "    <div id='srChange' class='sr-change'></div>",
        "  </div>",
        "  <div class='sr-section'>",
        "    <div class='sr-section-title sr-clickable' id='srRunsToggle'>历史研究 <span class='sr-tag' id='srRunsTag'>展开</span></div>",
        "    <div id='srRunsWrap' class='sr-iter-wrap' style='display:none;'>",
        "      <table class='sr-runs-table'><thead><tr><th>任务</th><th>标的</th><th>区间</th><th>状态</th><th>轮数</th><th>最优 OOS 夏普</th><th>创建时间</th></tr></thead>",
        "      <tbody id='srRunsBody'><tr><td colspan='7' class='sr-empty'>加载中…</td></tr></tbody></table>",
        "    </div>",
        "  </div>",
        "</div>",
      ].join("");
      document.body.appendChild(this.overlayEl);
      document.body.appendChild(this.modalEl);

      // 默认区间：近 3 年
      const end = new Date();
      const start = new Date(end.getTime() - 365 * 3 * 86400 * 1000);
      const fmt = (d) => d.toISOString().slice(0, 10);
      $("#srPeriodStart", this.modalEl).value = fmt(start);
      $("#srPeriodEnd", this.modalEl).value = fmt(end);

      $(".sr-close-btn", this.modalEl).onclick = () => this.close();
      /* 全屏 ↔ 浮窗切换（默认全屏：研究内容多，浮窗展示不下） */
      $(".sr-max-btn", this.modalEl).onclick = () => this._toggleFull();
      this.overlayEl.onclick = () => this.close();
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && this.modalEl.classList.contains("open")) this.close();
      });
      $("#srStartBtn", this.modalEl).onclick = () => this.startRun();
      $("#srPoolBtn", this.modalEl).onclick = () => this.startPoolRun();
      $("#srCancelBtn", this.modalEl).onclick = () => this.cancelRun();
      $("#srTableToggle", this.modalEl).onclick = () => this._toggleTable();
      $("#srRunsToggle", this.modalEl).onclick = () => this._toggleRuns();
    }

    _toggleTable() {
      const wrap = $("#srIterTableWrap", this.modalEl);
      const btn = $("#srTableToggle", this.modalEl);
      const show = wrap.style.display === "none";
      wrap.style.display = show ? "block" : "none";
      btn.textContent = show ? "收起明细表" : "明细表";
    }

    _toggleRuns() {
      const wrap = $("#srRunsWrap", this.modalEl);
      const tag = $("#srRunsTag", this.modalEl);
      const show = wrap.style.display === "none";
      wrap.style.display = show ? "block" : "none";
      if (tag) tag.textContent = (show ? "收起" : "展开") + (tag.dataset.count ? " · 共 " + tag.dataset.count + " 次" : "");
    }

    open() {
      this.modalEl.classList.add("sr-full");   // 默认全屏：K 线 + 曲线 + 时间线浮窗装不下
      this.overlayEl.classList.add("open");
      this.modalEl.classList.add("open");
      this._syncMaxBtn();
      this.loadRuns();
    }

    /* 统一入口（与其它面板一致）：对话里的「看研发过程」走这里。
       为什么必须有 openPage：卡片按 typeof rp.openPage === 'function' 判定，
       只有 open() 时判定为假 → 按钮点了静默无反应（实测 2026-10-18）。
       run_uuid 可选：传了就定位到那一次研究，不传按「最近一次」载入。 */
    openPage(runUuid) {
      this.open();
      const uuid = String(runUuid == null ? "" : runUuid).trim();
      if (uuid && uuid !== this.currentRun) this.loadRun(uuid, { subscribe: true });
      return true;
    }

    close() {
      this.overlayEl.classList.remove("open");
      this.modalEl.classList.remove("open");
      this._closeSse();
      this._disposeChart();
      this._disposeDdChart();
      this._disposeKline();
    }

    /* 全屏 ↔ 浮窗：图标由 CSS 按 .sr-full 切换，这里只同步可用性与 tooltip 文案 */
    _toggleFull() {
      this.modalEl.classList.toggle("sr-full");
      this._syncMaxBtn();
      this._resizeCharts();
    }

    _syncMaxBtn() {
      const btn = $(".sr-max-btn", this.modalEl);
      if (!btn) return;
      const label = this.modalEl.classList.contains("sr-full") ? "切回浮窗" : "切到全屏";
      btn.title = label;
      btn.setAttribute("aria-label", label);
    }

    _resizeCharts() {
      if (this._chart) { try { this._chart.resize(); } catch (e) { /* noop */ } }
      if (this._ddChart) { try { this._ddChart.resize(); } catch (e) { /* noop */ } }
      if (this._klineChart) { try { this._klineChart.resize(); } catch (e) { /* noop */ } }
    }

    _fetch(url, options) { return window.StockRadar.api.fetchJSON(url, options); }

    _disposeChart() {
      if (this._chart) { try { this._chart.dispose(); } catch (e) { /* noop */ } this._chart = null; }
    }

    _disposeDdChart() {
      if (this._ddChart) { try { this._ddChart.dispose(); } catch (e) { /* noop */ } this._ddChart = null; }
    }

    /* K 线用 renderBacktestKline 渲染，实例挂在同一个 DOM 节点上；
       换轮次前必须 dispose，否则 innerHTML 清空后 ECharts 拿不到自己的 canvas。 */
    _disposeKline() {
      const box = $("#srKline", this.modalEl);
      if (box && window.echarts) {
        try {
          const inst = window.echarts.getInstanceByDom(box);
          if (inst) inst.dispose();
        } catch (e) { /* noop */ }
      }
      this._klineChart = null;
    }

    /* ── 池子验证 / 组合（同步端点；后续再包 SSE） ── */
    async startPoolRun() {
      const idea = $("#srIdea", this.modalEl).value.trim();
      const pool = ($("#srPool", this.modalEl).value || "POOL:default").trim();
      const mode = $("#srPoolMode", this.modalEl).value || "both";
      const start = $("#srPeriodStart", this.modalEl).value;
      const end = $("#srPeriodEnd", this.modalEl).value;
      const entryTiming = $("#srEntryTiming", this.modalEl).value;
      const exitTiming = $("#srExitTiming", this.modalEl).value;
      const btn = $("#srPoolBtn", this.modalEl);
      const bar = $("#srRunningBar", this.modalEl);
      const barText = $("#srRunningText", this.modalEl);
      if (!idea || idea.length < 4) { alert("请先填写研究想法（至少 4 个字）"); return; }
      btn.disabled = true;
      const oldText = btn.textContent;
      btn.textContent = "池子跑…";
      bar.style.display = "";
      barText.textContent = "池子验证中（生成策略 + 逐只 + 可选组合）…";
      const done = () => {
        btn.disabled = false;
        btn.textContent = oldText;
        bar.style.display = "none";
      };
      try {
        const res = await this._fetch(API + "/pool-runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            idea: idea, pool: pool, start: start, end: end,
            entry_timing: entryTiming, exit_timing: exitTiming,
            mode: mode, max_positions: 10,
          }),
        });
        if (res.code !== 0) { alert(res.msg || "池子验证失败"); done(); return; }
        const runId = res.data.run_id;
        const es = new EventSource(API + "/pool-runs/" + runId + "/events");
        es.addEventListener("pool_phase", (ev) => {
          try {
            const d = JSON.parse(ev.data);
            barText.textContent = d.message || ("池子阶段：" + (d.phase || ""));
          } catch (e) {}
        });
        es.addEventListener("pool_progress", (ev) => {
          try {
            const d = JSON.parse(ev.data);
            barText.textContent = "逐只回测 " + (d.done || 0) + "/" + (d.total || 0)
              + " · 成功 " + (d.ok || 0) + " · 失败 " + (d.fail || 0);
          } catch (e) {}
        });
        es.addEventListener("final", (ev) => {
          try {
            const d = JSON.parse(ev.data);
            if (d.result) this._renderPoolResult(d.result);
            else alert("池子完成但没有结果");
          } catch (e) { alert("结果解析失败：" + e); }
          es.close();
          done();
        });
        es.addEventListener("task_error", (ev) => {
          let msg = "池子验证失败";
          try { msg = JSON.parse(ev.data).message || msg; } catch (e) {}
          alert(msg);
          es.close();
          done();
        });
        es.onerror = () => {
          es.close();
          this._fetch(API + "/pool-runs/" + runId).then((snap) => {
            if (snap && snap.code === 0 && snap.data && snap.data.status === "done") {
              this._renderPoolResult(snap.data.result);
            } else {
              alert("池子事件流断开，任务可能仍在跑；稍后再打开面板看结果。");
            }
            done();
          }).catch(() => done());
        };
        return;
      } catch (e) {
        alert("池子验证失败：" + (e.message || e));
        done();
      }
    }

    _renderPoolResult(data) {
      const el = $("#srPoolResult", this.modalEl);
      if (!el || !data) return;
      const pool = data.pool || {};
      const cs = data.cross_section || {};
      const ov = cs.overall || {};
      const vd = cs.verdict || {};
      const pf = data.portfolio;
      const assets = pool.assets || {};
      const fmtPct = (v, d) => (v == null || isNaN(v)) ? "—" : (Number(v) * 100).toFixed(d == null ? 2 : d) + "%";
      const rows = (cs.results || []).slice().sort((a, b) => (b.total_return || 0) - (a.total_return || 0));
      let html = "<div class='sr-section-title'>池子验证 · " + esc(pool.n || ov.total || 0)
        + " 只（" + esc(assets.stock || 0) + " 股票 + " + esc(assets.etf || 0) + " ETF）"
        + "<span class='sr-tag'>横截面=逐只独立、不共享资金</span></div>";
      html += "<div class='sr-kpis'>"
        + "<div class='sr-kpi'><span>可评判</span><b>" + esc(ov.scored || 0) + "</b></div>"
        + "<div class='sr-kpi'><span>有效（胜率>50%）</span><b>" + esc(ov.valid || 0) + "</b></div>"
        + "<div class='sr-kpi'><span>有效占比</span><b>" + fmtPct(ov.valid_ratio, 1) + "</b></div>"
        + "<div class='sr-kpi'><span>胜率中位</span><b>" + fmtPct(ov.win_rate_median, 1) + "</b></div>"
        + "<div class='sr-kpi'><span>收益中位</span><b class='" + metricCls(ov.total_return_median) + "'>" + fmtPct(ov.total_return_median) + "</b></div>"
        + "</div>";
      html += "<div class='sr-prior' style='display:block;margin:8px 0;'><b>" + esc(vd.label || "—") + "</b> · " + esc(vd.headline || "") + "</div>";
      if (pf) {
        html += "<div class='sr-section-title'>组合（共享资金 · " + esc(pf.max_positions || "?") + " 仓等权）</div>"
          + "<div class='sr-kpis'>"
          + "<div class='sr-kpi'><span>期末权益</span><b>" + Number(pf.final_equity || 0).toLocaleString() + "</b></div>"
          + "<div class='sr-kpi'><span>总收益</span><b class='" + metricCls(pf.total_return) + "'>" + fmtPct(pf.total_return) + "</b></div>"
          + "<div class='sr-kpi'><span>年化</span><b class='" + metricCls(pf.cagr) + "'>" + fmtPct(pf.cagr) + "</b></div>"
          + "<div class='sr-kpi'><span>最大回撤</span><b class='sr-neg'>" + fmtPct(pf.max_drawdown, 1) + "</b></div>"
          + "<div class='sr-kpi'><span>成交笔数</span><b>" + esc(pf.buys || 0) + "</b></div>"
          + "<div class='sr-kpi'><span>胜率</span><b>" + fmtPct(pf.win_rate, 1) + "</b></div>"
          + "</div>";
      }
      html += "<div style='margin-top:8px'><b>边界</b><ul style='margin:4px 0 0 18px;color:var(--text-dim);font-size:12px'>"
        + (cs.caveats || []).map((c) => "<li>" + esc(c) + "</li>").join("") + "</ul></div>";
      if (rows.length) {
        html += "<div class='sr-section-title' style='margin-top:10px'>逐只明细（按收益排序，前 8）</div>"
          + "<table class='sr-table'><thead><tr><th>代码</th><th>类型</th><th>健康</th><th>笔数</th><th>胜率</th><th>收益</th></tr></thead><tbody>"
          + rows.slice(0, 8).map((r) => "<tr><td>" + esc(r.code) + "</td><td>" + esc(r.asset === "etf" ? "ETF" : "股票")
            + "</td><td>" + esc(r.health || "") + "</td><td>" + esc(r.trades == null ? "—" : r.trades)
            + "</td><td>" + fmtPct(r.win_rate, 1) + "</td><td class='" + metricCls(r.total_return) + "'>" + fmtPct(r.total_return) + "</td></tr>").join("")
          + "</tbody></table>";
      }
      el.innerHTML = html;
      el.style.display = "block";
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    /* ── 发起研究 ── */
    async startRun() {
      const idea = $("#srIdea", this.modalEl).value.trim();
      const universe = $("#srUniverse", this.modalEl).value.trim();
      const periodStart = $("#srPeriodStart", this.modalEl).value;
      const periodEnd = $("#srPeriodEnd", this.modalEl).value;
      const entryTiming = $("#srEntryTiming", this.modalEl).value;
      const exitTiming = $("#srExitTiming", this.modalEl).value;   // 空串 = 跟随买入
      const btn = $("#srStartBtn", this.modalEl);
      if (!idea || idea.length < 4) { alert("请先填写研究想法（至少 4 个字）"); return; }
      btn.disabled = true;
      try {
        const res = await this._fetch(API + "/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          /* 口径必须随请求体一起发：只在后端线程里用、不落库的话，
             面板起的任务续跑/结果卡都会退回默认口径（用户看不到也纠不了）。 */
          body: JSON.stringify({ idea: idea, universe: universe, period_start: periodStart, period_end: periodEnd, source: "panel", entry_timing: entryTiming, exit_timing: exitTiming }),
        });
        if (res.code !== 0) { alert(res.msg || "创建失败"); return; }
        this.loadRun(res.data.run_uuid, { subscribe: true });
      } catch (e) {
        alert("创建失败：" + (e.message || e));
      } finally {
        btn.disabled = false;
      }
    }

    /* ── 取消 ── */
    async cancelRun() {
      if (!this.currentRun) return;
      try {
        const res = await this._fetch(API + "/runs/" + this.currentRun + "/cancel", { method: "POST" });
        if (res.code === 0) $("#srRunningText", this.modalEl).textContent = "已请求取消，等待当前轮次结束…";
        else alert(res.msg || "取消失败");
      } catch (e) { alert("取消失败：" + (e.message || e)); }
    }

    /* ── 断点续跑：从第 N 轮接着跑**同一个 run**（不新建任务） ── */
    async _resumeRun(runUuid, btn) {
      const label = btn ? btn.textContent : "";
      if (btn) { btn.disabled = true; btn.textContent = "续跑中…"; }
      try {
        const res = await this._fetch(API + "/runs/" + encodeURIComponent(runUuid) + "/resume", { method: "POST" });
        if (!res || res.code !== 0) { alert((res && res.msg) || "续跑失败"); return; }
        const from = res.data && res.data.resume_from_round;
        // 切到该 run 并订阅同一个 events_url（loadRun 见 status=running 会自动订）
        this.loadRun(runUuid, { subscribe: true });
        this.loadRuns();
        const txt = $("#srRunningText", this.modalEl);
        if (txt) txt.textContent = from ? ("已从第 " + from + " 轮续跑…") : "已续跑…";
      } catch (e) {
        alert("续跑失败：" + (e.message || e));
      } finally {
        if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = label; }
      }
    }

    /* ── 历史研究（默认折叠，只露次数） ── */
    async loadRuns() {
      const body = $("#srRunsBody", this.modalEl);
      const tag = $("#srRunsTag", this.modalEl);
      try {
        const res = await this._fetch(API + "/runs");
        const runs = (res.data && res.data.runs) || [];
        if (tag) { tag.dataset.count = String(runs.length); tag.textContent = (runs.length ? "共 " + runs.length + " 次 · " : "") + "展开"; }
        if (!runs.length) {
          body.innerHTML = "<tr><td colspan='7' class='sr-empty'>暂无历史研究</td></tr>";
          return;
        }
        body.innerHTML = runs.map((r) => {
          const oos = r.best_oos_metrics || {};
          const interrupted = String(r.status || "") === "interrupted";
          const resumeText = "从第 " + (Number(r.iter_count || 0) + 1) + " 轮续跑";
          return "<tr data-uuid='" + esc(r.run_uuid) + "'>"
            + "<td>" + esc(r.run_uuid) + "</td><td>" + esc(r.universe) + "</td><td>" + esc(r.period) + "</td>"
            + "<td><span class='" + statusCls(r.status, r.stop_reason) + "' title='" + esc(r.status) + (r.stop_reason ? " / " + esc(r.stop_reason) : "") + "'>" + esc(statusText(r.status, r.stop_reason)) + "</span>"
            + (interrupted ? " <button class='sr-mini-btn' data-resume='" + esc(r.run_uuid) + "' type='button'>" + resumeText + "</button>" : "")
            + "</td>"
            + "<td>" + (r.total_iters == null ? 0 : r.total_iters) + "</td>"
            + "<td>" + num(oos.sharpe) + "</td>"
            + "<td>" + esc(day(r.created_at)) + "</td>"
            + "</tr>";
        }).join("");
        body.querySelectorAll("tr[data-uuid]").forEach((tr) => {
          tr.onclick = (e) => {
            if (e.target && e.target.closest && e.target.closest("button[data-resume]")) return;
            this.loadRun(tr.dataset.uuid, { subscribe: false });
          };
        });
        // 历史项里的续跑：不能触发行点击（会先 loadRun 打乱当前视图）
        body.querySelectorAll("button[data-resume]").forEach((btn) => {
          btn.onclick = (e) => {
            e.stopPropagation();
            this._resumeRun(btn.dataset.resume, btn);
          };
        });
        // 打开面板就载入最近一次研究：否则结论条 / 曲线 / 时间线三块是大片留白
        if (!this.currentRun && runs.length) this.loadRun(runs[0].run_uuid, { subscribe: false });
      } catch (e) {
        body.innerHTML = "<tr><td colspan='7' class='sr-empty'>加载失败：" + esc(e.message || e) + "</td></tr>";
      }
    }

    /* ── 载入任务：先 GET 轮次表补齐，再按需订阅 SSE ── */
    async loadRun(runUuid, opts) {
      this.currentRun = runUuid;
      this._round = null;
      this._closeSse();
      try {
        const res = await this._fetch(API + "/runs/" + encodeURIComponent(runUuid) + "/iters");
        const iters = (res.data && res.data.iters) || [];
        const status = (res.data && res.data.status) || "running";
        this._renderIters(iters, status);
        await this._renderBest(runUuid);
        this._setRunning(status === "running");
        if (opts && opts.subscribe && status === "running") this._subscribeSse(runUuid);
        if (status === "running" && !this.es) this._subscribeSse(runUuid); // 打开面板时也续订
      } catch (e) {
        $("#srIterBody", this.modalEl).innerHTML = "<tr><td colspan='8' class='sr-empty'>加载失败：" + esc(e.message || e) + "</td></tr>";
      }
    }

    /* ── SSE 订阅（时间线与明细表逐轮出现） ── */
    _subscribeSse(runUuid) {
      this._closeSse();
      const es = new EventSource(API + "/runs/" + encodeURIComponent(runUuid) + "/events");
      this.es = es;
      es.addEventListener("strategy_iter_start", (ev) => {
        const d = JSON.parse(ev.data);
        this._setRunning(true);
        this._upsertRow({ round: d.round, hypothesis: d.hypothesis, running: true });
        this._renderTimeline();
      });
      es.addEventListener("strategy_iter_done", (ev) => {
        const d = JSON.parse(ev.data);
        this._upsertRow(d);
        this._renderTimeline();
        if (this._round === d.round) {
          this._selectRound(d.round);
          this._syncLedgerIds(d.round);   // 账本 run_id 只在 /iters 里，补一次再画 K 线
        }
      });
      es.addEventListener("strategy_run_done", () => this._refreshRun(runUuid));
      es.addEventListener("final", () => { this._closeSse(); this._refreshRun(runUuid); });
      es.addEventListener("task_error", (ev) => {
        this._closeSse();
        const d = JSON.parse(ev.data);
        this._renderFailed(d);
        this._setRunning(false);
      });
      es.onerror = () => { /* 断线由 final/DB 兜底，静默重连交给浏览器 */ };
    }

    _closeSse() {
      if (this.es) { try { this.es.close(); } catch (e) { /* noop */ } this.es = null; }
    }

    async _refreshRun(runUuid) {
      this._setRunning(false);
      await this.loadRun(runUuid, { subscribe: false });
      this.loadRuns();
    }

    _setRunning(on) {
      $("#srRunningBar", this.modalEl).style.display = on ? "flex" : "none";
    }

    /* ── 迭代轮次：时间线（主视图）+ 明细表（折叠） ── */
    _renderIters(iters, status) {
      this._iters = iters || [];
      this._iterRows = {};
      const body = $("#srIterBody", this.modalEl);
      const tl = $("#srTimeline", this.modalEl);
      if (!this._iters.length) {
        tl.innerHTML = "<div class='sr-empty'>还没有轮次：发起研究后，每一轮都会在这里出现一个节点</div>";
        body.innerHTML = "<tr><td colspan='8' class='sr-empty'>暂无轮次</td></tr>";
        $("#srIterDetail", this.modalEl).innerHTML = "";
        $("#srCurve", this.modalEl).innerHTML = "<div class='sr-empty'>暂无曲线</div>";
        $("#srCurveFoot", this.modalEl).innerHTML = "";
        $("#srKlineTag", this.modalEl).textContent = "跟随当前轮次（该轮样本内回测账本）";
        this._klineMessage("还没有轮次：跑完第一轮后这里出 K 线与买卖点");
        this._renderChange(null);
        return;
      }
      body.innerHTML = "";
      this._iters.forEach((it) => this._upsertRow(it));
      this._renderTimeline();
      const best = this._iters.filter((it) => it.verdict === "best").pop();
      const pick = best || this._iters[this._iters.length - 1];
      this._selectRound(pick.round);
    }

    _upsertRow(it) {
      this._iterRows = this._iterRows || {};
      this._iterRows[it.round] = it;
      const body = $("#srIterBody", this.modalEl);
      let tr = body.querySelector("tr[data-round='" + it.round + "']");
      const oos = it.oos_metrics || {};
      const isM = it.is_metrics || {};
      const oosSharpe = oos.sharpe != null && !isNaN(oos.sharpe) ? num(oos.sharpe) : "—";
      let verdictHtml;
      if (it.running) verdictHtml = "<span class='sr-spinner'></span>";
      else if (it.verdict === "best") verdictHtml = "<span class='sr-badge sr-badge-best'>最优</span>";
      else if (it.overfit_flag) verdictHtml = "<span class='sr-badge sr-badge-reject' title='IS↑OOS↓ 过拟合拒绝'>过拟合</span>";
      else if (it.verdict === "failed") verdictHtml = "<span class='sr-badge sr-badge-failed'>失败</span>";
      else if (it.verdict === "degenerate") verdictHtml = "<span class='sr-badge sr-badge-failed' title='零成交或数据不足，本轮指标不可用'>无效</span>";
      else if (it.verdict === "keep" || it.verdict === "reject") verdictHtml = "<span class='sr-badge sr-badge-keep'>保留</span>";
      else verdictHtml = "—";
      if (isM.low_sample) {
        verdictHtml += "<span class='sr-badge sr-badge-lowsample' title='IS 样本笔数过少，不参与最优比较'>IS n小</span>";
      }
      if (oos.trades != null && oos.trades < MIN_SAMPLE && it.compile_ok) {
        verdictHtml += "<span class='sr-badge sr-badge-lowsample' title='OOS 样本笔数过少，不参与最优比较'>n小</span>";
      }
      // 意图核对说「不是我说的那个策略」：比收益高低更要紧，且不参与选优
      if (this._offIntent(it)) {
        const devs = (it.intent_check && it.intent_check.deviations) || [];
        verdictHtml += "<span class='sr-badge sr-badge-reject' title='"
          + esc(devs.length ? devs.join("；") : "实现偏离了用户原话") + "'>偏离原话</span>";
      }
      const hypo = it.hypothesis || (it.running ? "生成中…" : (it.error ? "（生成失败）" : "—"));
      tr = tr || document.createElement("tr");
      if (!tr.dataset.round) { tr.dataset.round = it.round; body.appendChild(tr); }
      tr.classList.toggle("sr-failed-row", it.verdict === "failed");
      tr.innerHTML = "<td>" + it.round + "</td>"
        + "<td title='" + esc(it.hypothesis || "") + "'><div class='sr-hypo'>" + esc(hypo) + "</div></td>"
        + "<td class='" + metricCls(isM.sharpe) + "'>" + num(isM.sharpe) + "</td>"
        + "<td class='" + metricCls(oos.sharpe) + "'>" + oosSharpe + "</td>"
        + "<td class='" + metricCls(oos.cagr) + "'>" + pct(oos.cagr) + "</td>"
        + "<td class='" + metricCls(-(oos.mdd || 0)) + "'>" + pct(oos.mdd) + "</td>"
        + "<td>" + nOr(oos.trades, "—") + "</td>"
        + "<td>" + verdictHtml + "</td>";
      tr.onclick = () => this._selectRound(it.round);
    }

    _renderTimeline() {
      const box = $("#srTimeline", this.modalEl);
      if (!box) return;
      if (!this._iters.length) return;
      let maxAbs = 1;
      this._iters.forEach((it) => {
        const a = Math.abs(Number((it.is_metrics || {}).sharpe) || 0);
        const b = Math.abs(Number((it.oos_metrics || {}).sharpe) || 0);
        maxAbs = Math.max(maxAbs, a, b);
      });
      box.innerHTML = this._iters.map((it) => this._nodeHtml(it, maxAbs)).join("");
      box.querySelectorAll(".sr-node").forEach((n) => {
        n.onclick = () => this._selectRound(Number(n.dataset.round));
      });
      if (this._round != null) {
        box.querySelectorAll(".sr-node").forEach((n) => n.classList.toggle("sel", Number(n.dataset.round) === Number(this._round)));
      }
    }

    _nodeHtml(it, maxAbs) {
      const oos = it.oos_metrics || {};
      const isM = it.is_metrics || {};
      const v = nodeVerdict(it);
      const h = (val) => {
        const a = Math.abs(Number(val) || 0);
        return Math.min(30, Math.max(3, Math.round((a / maxAbs) * 30)));
      };
      const neg = (val) => (Number(val) < 0 ? " neg" : "");
      return "<div class='sr-node " + v.cls + "' data-round='" + it.round + "' title='" + esc(it.hypothesis || "") + "'>"
        + "<div class='sr-node-dot'>" + it.round + "</div>"
        + "<div class='sr-node-badge " + v.cls + "'>" + v.label + "</div>"
        + "<div class='sr-bars'>"
        +   "<span class='sr-bar is" + neg(isM.sharpe) + "' style='height:" + h(isM.sharpe) + "px' title='样本内夏普 " + num(isM.sharpe) + "'></span>"
        +   "<span class='sr-bar oos" + neg(oos.sharpe) + "' style='height:" + h(oos.sharpe) + "px' title='样本外夏普 " + num(oos.sharpe) + "'></span>"
        + "</div>"
        + "<div class='sr-node-val " + metricCls(oos.sharpe) + "'>" + num(oos.sharpe) + "</div>"
        + "</div>";
    }

    _selectRound(roundNo) {
      this._round = roundNo;
      const box = $("#srTimeline", this.modalEl);
      if (box) box.querySelectorAll(".sr-node").forEach((n) => n.classList.toggle("sel", Number(n.dataset.round) === Number(roundNo)));
      this._showIterDetail(roundNo);
      this._renderCurve(roundNo);
      this._renderKline(roundNo);
    }

    _showIterDetail(roundNo) {
      const it = (this._iterRows || {})[roundNo];
      if (!it) return;
      this._renderChange(it);
      const body = $("#srIterBody", this.modalEl);
      if (body) body.querySelectorAll("tr").forEach((tr) => tr.classList.toggle("selected", Number(tr.dataset.round) === Number(roundNo)));
      const isM = it.is_metrics || {};
      const oos = it.oos_metrics || {};
      const detail = $("#srIterDetail", this.modalEl);
      if (it.verdict === "failed") {
        detail.innerHTML = "<div class='sr-detail'>"
          + "<h4>第 " + it.round + " 轮 · 生成失败</h4>"
          + "<div class='sr-error'>" + esc(it.compile_error || it.error || "未知错误") + "</div>"
          + "<div class='sr-kv'>建议：换一种更具体、可量化的想法描述重试（例如指明指标与参数方向）。</div>"
          + "</div>";
        return;
      }
      detail.innerHTML = "<div class='sr-detail'>"
        + "<h4>第 " + it.round + " 轮 · " + esc(it.hypothesis || "") + (it.model ? " · 模型 " + esc(it.model) : "") + "</h4>"
        + "<div class='sr-kv'><b>本轮改动：</b><span class='sr-diff'>" + esc(it.strategy_diff || "—") + "</span></div>"
        + "<div class='sr-kv'><b>样本内 IS：</b>年化 " + pct(isM.cagr) + " · 区间总收益 " + pct(isM.total_return) + " · 夏普 " + num(isM.sharpe) + " · 回撤 " + pct(isM.mdd) + " · 胜率 " + pct(isM.win_rate) + " · " + nOr(isM.trades, "—") + " 笔</div>"
        + "<div class='sr-kv'><b>样本外 OOS：</b>年化 " + pct(oos.cagr) + " · 区间总收益 " + pct(oos.total_return) + " · 夏普 " + num(oos.sharpe) + " · 回撤 " + pct(oos.mdd) + " · 胜率 " + pct(oos.win_rate) + " · " + nOr(oos.trades, "—") + " 笔</div>"
        + (it.overfit_flag ? "<div class='sr-error'>IS/OOS 夏普比超 1.3 红线，存在过拟合迹象，该轮不得当选最优。</div>" : "")
        + "</div>";
    }

    /* ── 净值曲线（IS / OOS 两段 + 回撤小图） ── */
    _normCurve(arr) {
      const rows = arr || [];
      const base = rows.length ? Number(rows[0].equity) || 1 : 1;
      return rows.map((p) => [day(p.date), Number(((Number(p.equity) || 0) / base) * 100)]);
    }

    _normDd(arr) {
      // 账本 drawdown_curve_json 里存的就是百分数（实测 min -24.32 即 -24.32%），不要再乘 100
      return (arr || []).map((p) => [day(p.date), -Math.abs(Number(p.drawdown) || 0)]);
    }

    async _renderCurve(roundNo) {
      const box = $("#srCurve", this.modalEl);
      const foot = $("#srCurveFoot", this.modalEl);
      const tag = $("#srCurveTag", this.modalEl);
      const warn = $("#srCurveWarn", this.modalEl);
      const ddBox = $("#srDd", this.modalEl);
      const ddTag = $("#srDdTag", this.modalEl);
      const ddNote = $("#srDdNote", this.modalEl);
      if (!box) return;
      this._disposeChart();
      this._disposeDdChart();
      box.innerHTML = "<div class='sr-empty'>加载曲线…</div>";
      if (foot) foot.innerHTML = "";
      if (warn) { warn.style.display = "none"; warn.innerHTML = ""; }
      if (ddBox) ddBox.innerHTML = "<div class='sr-empty'>加载回撤…</div>";
      if (ddTag) ddTag.textContent = "";
      if (ddNote) ddNote.innerHTML = "";
      let payload = null;
      try {
        const res = await this._fetch(API + "/runs/" + encodeURIComponent(this.currentRun) + "/iters/" + roundNo + "/curve");
        payload = res.data || {};
      } catch (e) {
        box.innerHTML = "<div class='sr-empty'>曲线加载失败：" + esc(e.message || e) + "</div>";
        if (ddBox) ddBox.innerHTML = "<div class='sr-empty'>回撤加载失败</div>";
        return;
      }
      if (this._round !== roundNo) return;   // 用户又点了别的节点，丢弃过期响应
      if (!payload.exists) {
        if (tag) tag.textContent = "第 " + roundNo + " 轮 · 无曲线";
        box.innerHTML = "<div class='sr-empty'>" + esc(payload.reason || "这一轮没有可画的曲线") + "</div>";
        if (ddBox) ddBox.innerHTML = "<div class='sr-empty'>本轮无回撤数据</div>";
        return;
      }
      const isSeg = payload.is || {};
      const oosSeg = payload.oos || {};
      const isPts = this._normCurve(isSeg.equity_curve);
      const oosPts = this._normCurve(oosSeg.equity_curve);
      const ddIs = this._normDd(isSeg.drawdown_curve);
      const ddOos = this._normDd(oosSeg.drawdown_curve);
      // 单位按频率自适应：月线场景写「16 天」会让人以为是 16 个交易日
      const unit = (seg) => ((seg.freq && seg.freq.is_daily === false) ? " 根（非日线）" : " 天");
      if (tag) tag.textContent = "第 " + roundNo + " 轮 · 样本内 " + isPts.length + unit(isSeg)
        + " / 样本外 " + oosPts.length + unit(oosSeg);
      if (warn && payload.degraded) {
        warn.innerHTML = "<b>数据不可信</b>：" + esc(payload.degraded_reason || "本次回测行情不是日线");
        warn.style.display = "";
      }
      if (foot) {
        const segLine = (name, pts, seg, cls) => {
          if (!pts.length) return "";
          return "<div class='sr-foot-row'><span class='sr-foot-dot " + cls + "'></span>" + name + " "
            + esc(pts[0][0]) + " ~ " + esc(pts[pts.length - 1][0])
            + " · " + nOr(tradesOf(seg), "—") + " 笔 · 区间收益 " + pct(seg.total_return)
            + " · 夏普 " + num(seg.sharpe) + "</div>";
        };
        let html = segLine("样本内", isPts, isSeg, "is") + segLine("样本外", oosPts, oosSeg, "oos");
        if (oosSeg.health && oosSeg.health !== "ok") {
          html += "<div class='sr-foot-warn'>样本外体检 " + esc(oosSeg.health) + (oosSeg.health_detail ? "：" + esc(oosSeg.health_detail) : "") + "</div>";
        }
        html += "<div class='sr-foot-hint'>样本内 / 样本外是两段各自独立的回测，曲线都从 100 起算；结论以样本外为准。</div>";
        foot.innerHTML = html;
      }
      const ok = await ensureECharts();
      if (this._round !== roundNo) return;
      if (!ok) {
        box.innerHTML = "<div class='sr-empty'>图表库未加载（" + esc(ECHARTS_SRC) + " 缺失），曲线暂不能画</div>";
        if (ddBox) ddBox.innerHTML = "<div class='sr-empty'>图表库未加载</div>";
        return;
      }
      const dates = isPts.map((p) => p[0]).concat(oosPts.map((p) => p[0]));
      const splitIdx = isPts.length;
      const pad = (n) => new Array(n).fill(null);
      const isData = isPts.map((p) => p[1]).concat(pad(oosPts.length));
      const oosData = pad(isPts.length).concat(oosPts.map((p) => p[1]));

      // ── 净值曲线（单图；回撤已拆成独立卡片）──
      box.innerHTML = "";
      const chart = window.echarts.init(box);
      this._chart = chart;
      chart.setOption({
        animation: false,
        grid: { left: 58, right: 20, top: 34, bottom: 26 },
        legend: { data: ["样本内", "样本外"], top: 2, right: 8, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 11, color: COLOR_MUTED } },
        tooltip: { trigger: "axis", axisPointer: { type: "line" } },
        xAxis: { type: "category", data: dates, boundaryGap: false,
          axisLabel: { fontSize: 10, color: COLOR_MUTED, hideOverlap: true, formatter: (v) => String(v).slice(2) },
          axisLine: { lineStyle: { color: COLOR_AXIS } }, axisTick: { lineStyle: { color: COLOR_AXIS } } },
        yAxis: { type: "value", scale: true, name: "净值", nameGap: 8,
          nameTextStyle: { fontSize: 10, color: COLOR_MUTED },
          axisLabel: { fontSize: 10, color: COLOR_MUTED }, splitLine: { lineStyle: { color: COLOR_SPLIT } } },
        series: [
          { name: "样本内", type: "line", data: isData, showSymbol: false,
            lineStyle: { width: 1.6, color: COLOR_IS }, itemStyle: { color: COLOR_IS },
            markLine: splitIdx > 0 ? { symbol: "none", silent: true, label: { formatter: "IS | OOS", fontSize: 10, color: COLOR_MUTED, position: "insideEndTop" },
              lineStyle: { type: "dashed", color: COLOR_MARK }, data: [{ xAxis: splitIdx - 1 }] } : undefined },
          { name: "样本外", type: "line", data: oosData, showSymbol: false,
            lineStyle: { width: 1.8, color: COLOR_OOS }, itemStyle: { color: COLOR_OOS } },
        ],
      });

      // ── 回撤（独立卡片）：两段拼在同一条时间轴上，都从 0 起算 ──
      if (ddBox) {
        if (!ddIs.length && !ddOos.length) {
          ddBox.innerHTML = "<div class='sr-empty'>这一轮没有回撤数据</div>";
        } else {
          const ddIsData = ddIs.map((p) => p[1]).concat(pad(ddOos.length));
          const ddOosData = pad(ddIs.length).concat(ddOos.map((p) => p[1]));
          ddBox.innerHTML = "";
          const ddChart = window.echarts.init(ddBox);
          this._ddChart = ddChart;
          ddChart.setOption({
            animation: false,
            grid: { left: 58, right: 20, top: 16, bottom: 26 },
            tooltip: { trigger: "axis", valueFormatter: (v) => (v == null ? "—" : Number(v).toFixed(2) + "%") },
            xAxis: { type: "category", data: dates, boundaryGap: false,
              axisLabel: { fontSize: 10, color: COLOR_MUTED, hideOverlap: true, formatter: (v) => String(v).slice(2) },
              axisLine: { lineStyle: { color: COLOR_AXIS } }, axisTick: { lineStyle: { color: COLOR_AXIS } } },
            yAxis: { type: "value", name: "回撤 %", nameGap: 8, max: 0,
              nameTextStyle: { fontSize: 10, color: COLOR_MUTED },
              axisLabel: { fontSize: 10, color: COLOR_MUTED, formatter: "{value}%" },
              splitLine: { lineStyle: { color: COLOR_SPLIT } } },
            series: [
              { name: "样本内回撤", type: "line", data: ddIsData, showSymbol: false,
                lineStyle: { width: 1, color: COLOR_IS }, areaStyle: { color: rgba(COLOR_IS, 0.18) } },
              { name: "样本外回撤", type: "line", data: ddOosData, showSymbol: false,
                lineStyle: { width: 1, color: COLOR_OOS }, areaStyle: { color: rgba(COLOR_OOS, 0.18) } },
            ],
          });
          if (ddTag) {
            ddTag.textContent = "样本内 " + ddIs.length + " 点 / 样本外 " + ddOos.length + " 点 · 越往下越深";
          }
          if (ddNote) ddNote.textContent = this._ddNoteOf(isSeg, oosSeg, ddOos);
        }
      }
    }

    /* 回撤一句话解读：只说不带判断的事实，别把「零回撤」说成「抗跌」 */
    _ddNoteOf(isSeg, oosSeg, ddOos) {
      const oosTrades = tradesOf(oosSeg);
      const oosMdd = oosSeg.mdd;
      if (oosTrades === 0) {
        return "样本外零成交：净值是一条水平线，回撤为 0 —— 这不是抗跌，是没参与。";
      }
      if (oosMdd == null) return "样本外回撤算不出来（数据不足或曲线缺失）。";
      const depth = Math.abs(Number(oosMdd));
      const last = ddOos.length ? Number(ddOos[ddOos.length - 1][1]) : 0;
      const tail = last < -0.01
        ? "期末仍在水下（" + last.toFixed(2) + "%），属于「没回本」而非「回撤后修复」。"
        : "期末已回到新高附近。";
      if (depth < 0.02) return "样本外最大回撤 " + pct(oosMdd) + "，波动很小。" + tail;
      if (depth < 0.1) return "样本外最大回撤 " + pct(oosMdd) + "，属可控区间。" + tail;
      return "样本外最大回撤 " + pct(oosMdd) + "，亏损偏深。" + tail;
    }

    /* ── K 线 + 买卖点（跟随当前轮次；数据取该轮样本内回测账本 SR-IS-sr_<hash>） ──
       用户原话：「为什么不和策略回测那样显示买点卖点」。这里直接复用回测页的
       renderBacktestKline（蜡烛 + 成交量 + 买卖点 + 本策略指标），不复制第二份配置。 */
    _klineMessage(msg) {
      const box = $("#srKline", this.modalEl);
      if (!box) return;
      this._disposeKline();
      box.innerHTML = "<div class='sr-empty'>" + esc(msg) + "</div>";
      const sum = $("#srKlineSummary", this.modalEl);
      if (sum) sum.innerHTML = "";
    }

    async _renderKline(roundNo) {
      const box = $("#srKline", this.modalEl);
      const sum = $("#srKlineSummary", this.modalEl);
      const tag = $("#srKlineTag", this.modalEl);
      if (!box) return;
      const it = (this._iterRows || {})[roundNo];
      if (!it) { this._klineMessage("选中轮次后，这里显示 K 线与买卖点"); if (tag) tag.textContent = ""; return; }
      if (it.running) { this._klineMessage("第 " + roundNo + " 轮还在跑，账本落库后这里出 K 线"); return; }
      if (it.compile_ok === 0 || it.compile_ok === false) {
        this._klineMessage("第 " + roundNo + " 轮没有生成可回测的策略，画不出 K 线");
        if (tag) tag.textContent = "第 " + roundNo + " 轮 · 无可回测策略";
        return;
      }
      if (!("backtest_run_id" in it)) {
        this._klineMessage("第 " + roundNo + " 轮的回测账本还在写入，稍后自动刷新");
        return;
      }
      const runId = it.backtest_run_id;
      if (!runId) {
        this._klineMessage("本轮无回测账本记录（多为更早的研究，账本未落 SR-IS-* 记录）");
        if (tag) tag.textContent = "第 " + roundNo + " 轮 · 无账本记录";
        return;
      }
      if (it.backtest_run_status && it.backtest_run_status !== "completed") {
        this._klineMessage("该轮回测账本状态为 " + it.backtest_run_status + "，没有可信的成交点可看");
        if (tag) tag.textContent = "第 " + roundNo + " 轮 · 回测未完成";
        return;
      }
      this._disposeKline();
      box.innerHTML = "<div class='sr-empty'>加载 K 线与买卖点…</div>";
      if (sum) sum.innerHTML = "";
      let data = null;
      try {
        data = await window.StockRadar.api.getBacktestKline(runId);
      } catch (e) {
        if (this._round !== roundNo) return;
        this._klineMessage("这轮的账本记录读不到 K 线：" + (e.message || e));
        return;
      }
      if (this._round !== roundNo) return;   // 用户又点了别的节点，丢弃过期响应
      const kline = (data && data.kline) || [];
      const trades = (data && data.trades) || [];
      if (!kline.length) {
        this._klineMessage("这一轮的区间没有行情数据（K 线为空）");
        if (tag) tag.textContent = "第 " + roundNo + " 轮 · 无行情";
        return;
      }
      const buys = trades.filter((t) => t.direction === "buy").length;
      const sells = trades.filter((t) => t.direction === "sell").length;
      if (tag) {
        tag.textContent = "第 " + roundNo + " 轮 · 样本内 · " + kline[0].date + " ~ " + kline[kline.length - 1].date
          + (data.name ? " · " + data.name + " " + data.code : "");
      }
      if (sum) sum.innerHTML = this._klineSummaryHtml(trades, buys, sells);
      const ok = await ensureECharts();
      if (this._round !== roundNo) return;
      if (!ok || !window.StockRadar.renderBacktestKline) {
        this._klineMessage("图表库未加载（" + ECHARTS_SRC + " 缺失），K 线暂不能画");
        return;
      }
      box.innerHTML = "";
      window.StockRadar.renderBacktestKline(box, data, { tradeSeries: true });
      try { this._klineChart = window.echarts.getInstanceByDom(box); } catch (e) { this._klineChart = null; }
    }

    /* 成交概况：笔数 / 买点 / 卖点 / 退出原因 top3（从卖出的 signal_reason 统计） */
    _klineSummaryHtml(trades, buys, sells) {
      const reasons = {};
      trades.forEach((t) => {
        if (t.direction !== "sell") return;
        const r = String(t.signal_reason || "").trim() || "未标注退出原因";
        reasons[r] = (reasons[r] || 0) + 1;
      });
      const top = Object.keys(reasons).map((k) => [k, reasons[k]])
        .sort((a, b) => b[1] - a[1]).slice(0, 3);
      let html = "<span class='sr-kline-chip'>成交 " + trades.length + " 笔</span>"
        + "<span class='sr-kline-chip buy'>买点 " + buys + "</span>"
        + "<span class='sr-kline-chip sell'>卖点 " + sells + "</span>";
      if (top.length) {
        html += "<span class='sr-kline-reasons'><b>退出原因 top" + top.length + "：</b>"
          + top.map((r) => esc(r[0]) + " × " + r[1]).join(" · ") + "</span>";
      }
      return html;
    }

    /* 新轮次落库后 /iters 才有 backtest_run_id（SSE 事件载荷不带），选中轮次时补一次 */
    async _syncLedgerIds(roundNo) {
      if (!this.currentRun) return;
      try {
        const res = await this._fetch(API + "/runs/" + encodeURIComponent(this.currentRun) + "/iters");
        const iters = (res.data && res.data.iters) || [];
        const fresh = {};
        iters.forEach((it) => { fresh[it.round] = it; });
        Object.keys(this._iterRows || {}).forEach((k) => {
          const f = fresh[k];
          if (!f || !("backtest_run_id" in f)) return;
          this._iterRows[k].backtest_run_id = f.backtest_run_id;
          this._iterRows[k].backtest_run_status = f.backtest_run_status;
        });
        if (this._round === roundNo) this._renderKline(roundNo);
      } catch (e) { /* 补不到就保留「账本写入中」文案，终态刷新会兜底 */ }
    }

    /* ── 结论条 hero（原「最优策略卡」，现在是一条能一眼看完的结论） ── */
    /* ── KPI 六卡：大数样本外（结论口径）+ 小字样本内对照 ── */
    _renderKpis(card) {
      const sec = $("#srKpiSection", this.modalEl);
      const box = $("#srKpis", this.modalEl);
      if (!sec || !box) return;
      if (!card || !card.exists) { sec.style.display = "none"; return; }
      const oos = card.oos_metrics || {};
      const isM = card.is_metrics || {};
      const rows = [
        ["样本外夏普", num(oos.sharpe), "样本内 " + num(isM.sharpe), metricCls(oos.sharpe)],
        ["样本外年化", pct(oos.cagr), "样本内 " + pct(isM.cagr), metricCls(oos.cagr)],
        ["区间收益", pct(oos.total_return), "样本内 " + pct(isM.total_return), metricCls(oos.total_return)],
        ["最大回撤", pct(oos.mdd), "样本内 " + pct(isM.mdd), "sr-neg"],
        ["胜率", pct(oos.win_rate), "样本内 " + pct(isM.win_rate), ""],
        ["成交笔数", String(nOr(tradesOf(oos), "—")), "样本内 " + String(nOr(tradesOf(isM), "—")), ""],
      ];
      box.innerHTML = rows.map((r) =>
        "<div class='sr-kpi'><div class='sr-kpi-k'>" + esc(r[0]) + "</div>"
        + "<div class='sr-kpi-v " + r[3] + "'>" + esc(r[1]) + "</div>"
        + "<div class='sr-kpi-s'>" + esc(r[2]) + "</div></div>").join("");
      sec.style.display = "";
    }

    /* ── 因子先验 banner：只说「注入了什么」；缺文件时显式说该跑生成脚本 ── */
    _renderPrior(prior) {
      const box = $("#srPrior", this.modalEl);
      if (!box) return;
      if (!prior || !prior.exists) {
        const why = prior && prior.missing_reason;
        if (!why) { box.style.display = "none"; box.innerHTML = ""; return; }
        box.className = "sr-prior warn";
        box.innerHTML = "<b>因子先验未注入</b>：" + esc(why);
        box.style.display = "";
        return;
      }
      const bits = [];
      if (prior.robust && prior.robust.length) bits.push("稳健 " + prior.robust.join("、"));
      if (prior.fragile && prior.fragile.length) bits.push("跨池变号（别当主条件）" + prior.fragile.join("、"));
      if (prior.reverse && prior.reverse.length) bits.push("方向为负 " + prior.reverse.join("、"));
      const scope = prior.cross_pool ? "跨池已验证" : "单池，未做跨池验证（弱先验）";
      const pools = prior.pools && prior.pools.length ? " · " + prior.pools.join("/") : "";
      box.className = "sr-prior";
      box.innerHTML = "<b>因子先验已注入</b>：<span class='sr-prior-scope'>" + esc(scope + pools) + "</span>"
        + "<div class='sr-prior-body'>" + esc(bits.join("；") || "本次先验里没有可用因子") + "</div>"
        + (prior.written_at ? "<div class='sr-prior-src'>生成于 " + esc(prior.written_at) + "</div>" : "");
      box.style.display = "";
    }

    /* ── 本轮改了什么：假设正文 + 参数/结构 diff（原型 ④ 的独立卡） ── */
    /* 与后端 is_off_intent 同口径：只有「核对过且明确不一致」才算偏离（fail-open） */
    _offIntent(it) {
      const ic = (it && it.intent_check) || {};
      return ic.checked === true && ic.matches === false;
    }

    _renderChange(it) {
      const box = $("#srChange", this.modalEl);
      const tag = $("#srChangeTag", this.modalEl);
      if (!box) return;
      if (!it) {
        box.innerHTML = "<div class='sr-empty'>点上面的轮次节点，这里显示那一轮改了什么</div>";
        if (tag) tag.textContent = "";
        return;
      }
      if (it.running) {
        box.innerHTML = "<div class='sr-empty'>这一轮还在生成…</div>";
        if (tag) tag.textContent = "第 " + it.round + " 轮 · 进行中";
        return;
      }
      if (it.compile_ok === 0 || it.compile_ok === false) {
        box.innerHTML = "<div class='sr-error'>这一轮编译失败：" + esc(it.compile_error || it.error || "未知错误") + "</div>";
        if (tag) tag.textContent = "第 " + it.round + " 轮 · 编译失败";
        return;
      }
      const hypo = String(it.hypothesis || "").trim();
      const diff = String(it.strategy_diff || "").trim();
      if (tag) tag.textContent = "第 " + it.round + " 轮" + (it.model ? " · " + it.model : "");
      box.innerHTML = "<div class='sr-change-hypo'>"
        + (hypo ? esc(hypo) : "<span class='sr-empty'>这一轮没有记录假设（较早的研究）</span>")
        + "</div>"
        + (diff ? "<div class='sr-change-diff mono'>" + esc(diff) + "</div>"
                : "<div class='sr-change-diff sr-empty'>这一轮没有参数/结构改动记录</div>")
        + (this._offIntent(it)
            ? "<div class='sr-change-off'>偏离用户原话："
              + esc(((it.intent_check && it.intent_check.deviations) || []).join("；") || "见核对结论")
              + "</div>"
            : "");
    }

    async _renderBest(runUuid) {
      const box = $("#srBestCard", this.modalEl);
      box.className = "sr-hero";
      box.innerHTML = "<div class='sr-empty'>加载中…</div>";
      let data = {};
      try {
        const res = await this._fetch(API + "/runs/" + encodeURIComponent(runUuid));
        data = res.data || {};
      } catch (e) {
        box.innerHTML = "<div class='sr-empty'>加载失败：" + esc(e.message || e) + "</div>";
        return;
      }
      if (this.currentRun !== runUuid) return;
      const run = data.run || {};
      const card = data.best_card || { exists: false };
      const iters = data.iters || [];
      // 续跑入口只在 interrupted 出现：N = 已完成轮数 + 1（从下一轮接着跑）
      const resumeFrom = run.status === "interrupted" ? iters.length + 1 : null;
      const v = verdictOf(card, run, resumeFrom);
      this._renderKpis(card);
      this._renderPrior(data.factor_prior);
      box.className = "sr-hero " + v.cls;
      const meta = [run.universe, run.period].filter(Boolean).join(" · ");
      /* 口径展示：优先用后端解析后的顶层字段（任务行已落库），老 run 回落到 run 行；
         两者都没有时整行不渲染，不硬写一个可能错的口径。 */
      const timing = timingText(data.entry_timing || run.entry_timing,
                                data.exit_timing || run.exit_timing);
      let html = "<div class='sr-hero-top'>"
        + "<div class='sr-hero-name'>" + esc(v.name || run.idea || "这次研究") + "</div>"
        + "<span class='sr-verdict " + v.cls + "'>" + esc(v.label) + "</span>"
        + "</div>"
        + (meta ? "<div class='sr-hero-meta'>" + esc(meta) + "</div>" : "")
        + (timing ? "<div class='sr-hero-meta'>成交口径：" + esc(timing) + "</div>" : "")
        + "<div class='sr-hero-text'>" + esc(v.text) + "</div>";
      if (resumeFrom) {
        html += "<div class='sr-actions'>"
          + "<button class='sr-action-btn' data-resume='" + esc(runUuid) + "' type='button'>"
          + "从第 " + resumeFrom + " 轮续跑</button></div>";
      }
      if (!card.exists) {
        box.innerHTML = html;
        const resumeBtn = box.querySelector("button[data-resume]");
        if (resumeBtn) resumeBtn.onclick = () => this._resumeRun(runUuid, resumeBtn);
        return;
      }
      const oos = card.oos_metrics || {};
      const isM = card.is_metrics || {};
      // 与引擎 compute_eval_verdict 同口径：只有样本外夏普为正时，IS/OOS 比值才有意义
      // （backtest/eval_runner.py:45-47 —— oos_sharpe > 0 才算比值，否则两段同为负数相除会算出假的「过拟合」）
      const ratio = (isM.sharpe != null && oos.sharpe != null && Number(oos.sharpe) > 0)
        ? Number(isM.sharpe) / Number(oos.sharpe) : null;
      const ratioHint = (ratio == null)
        ? (Number(oos.sharpe) > 0 ? "样本内夏普缺失，不适用" : "样本外非正，比值不适用")
        : (ratio > 1.3 ? "> 1.3 判过拟合" : "≤ 1.3 未触红线");
      // 样本内 / 样本外两列并排：同一组指标上下对齐，才看得出「样本内好、样本外垮」
      const isRows = [
        ["年化", pct(isM.cagr), metricCls(isM.cagr)],
        ["区间收益", pct(isM.total_return), metricCls(isM.total_return)],
        ["夏普", num(isM.sharpe), metricCls(isM.sharpe)],
        ["最大回撤", pct(isM.mdd), "sr-neg"],
        ["胜率", pct(isM.win_rate), ""],
        ["成交笔数", String(nOr(tradesOf(isM), "—")), ""],
      ];
      const oosRows = [
        ["年化", pct(oos.cagr), metricCls(oos.cagr)],
        ["区间收益", pct(oos.total_return), metricCls(oos.total_return)],
        ["夏普", num(oos.sharpe), metricCls(oos.sharpe)],
        ["最大回撤", pct(oos.mdd), "sr-neg"],
        ["胜率", pct(oos.win_rate), ""],
        ["成交笔数", String(nOr(tradesOf(oos), "—")), ""],
      ];
      const colHtml = (head, sub, rows, cls) => "<div class='sr-compare-col " + cls + "'>"
        + "<div class='sr-compare-head'>" + head + "<em>" + sub + "</em></div>"
        + "<div class='sr-compare-grid'>" + rows.map((r) =>
          "<div><div class='sr-compare-k'>" + esc(r[0]) + "</div>"
          + "<div class='sr-compare-v " + r[2] + "'>" + esc(r[1]) + "</div></div>").join("")
        + "</div></div>";
      html += "<div class='sr-compare'>"
        + colHtml("样本内 IS", "用于拟合，不能当结论", isRows, "")
        + colHtml("样本外 OOS", "结论以此为准", oosRows, "oos")
        + "</div>";
      // 成交笔数是否够：< MIN_SAMPLE 笔时胜率/夏普都不具统计意义，必须当面说清「不可用」
      const oosTrades = tradesOf(oos);
      if (oosTrades == null) {
        html += "<div class='sr-sample-flag bad'>样本外成交笔数缺失，无法判断样本是否充足，结论不可用。</div>";
      } else if (oosTrades < MIN_SAMPLE) {
        html += "<div class='sr-sample-flag bad'>样本不足：样本外仅 " + oosTrades + " 笔成交（少于 " + MIN_SAMPLE
          + " 笔），胜率与夏普都不具统计意义，结论不可用。</div>";
      } else {
        html += "<div class='sr-sample-flag ok'>样本充足：样本外 " + oosTrades + " 笔成交（≥ " + MIN_SAMPLE
          + " 笔），指标可参考。</div>";
      }
      html += "<div class='sr-note " + (ratio != null && ratio > 1.3 ? "bad" : "ok") + "'>IS/OOS 夏普比 "
        + num(ratio) + " · " + esc(ratioHint) + "</div>";
      html += "<div class='sr-note " + (card.overfit_flag ? "bad" : "ok") + "'>过拟合检验：" + esc(card.overfit_note || "—") + "</div>";
      // 免责提示前的小图标走内联 SVG（P0：功能图标不用 emoji/字符）
      html += "<div class='sr-note mute'>"
        + "<svg class='sr-note-ic' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        + "<path d='M12 9v4'/><path d='M12 17h.01'/><path d='M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'/></svg>"
        + esc(card.disclaimer || "历史表现不代表未来收益，本结果仅为研究结论，不构成投资建议") + "</div>";
      html += "<div class='sr-actions'>"
        + "<button class='sr-action-btn' data-action='save' type='button'>存入策略库</button>"
        + "<button class='sr-action-btn' data-action='monitor' type='button'>加入盯盘监控</button>"
        + "<button class='sr-action-btn' data-action='track' type='button'>标记信号入 signal_eval</button>"
        + "<button class='sr-action-btn sr-action-link' data-action='lifecycle' type='button' title='在策略库打开这个策略的详情（回测记录 / 评判 / 前瞻 / 落地）' title='先在左边「存入策略库」，这里就能看这个策略的回测记录 / 评判 / 前瞻 / 落地'>看它的策略一生 ›</button>"
        + "</div>";
      box.innerHTML = html;
      box.querySelectorAll(".sr-action-btn").forEach((btn) => {
        btn.onclick = () => (btn.dataset.action === "lifecycle"
          ? this._openLifecycle(runUuid, btn)
          : this._adopt(runUuid, btn.dataset.action, btn));
      });
    }

    _renderFailed(d) {
      if (this._hasBest()) return; // 已有结论条则不打断
      const box = $("#srBestCard", this.modalEl);
      box.className = "sr-hero bad";
      box.innerHTML = "<div class='sr-hero-top'><div class='sr-hero-name'>策略生成失败</div>"
        + "<span class='sr-verdict bad'>生成失败</span></div>"
        + "<div class='sr-hero-text'>schema 约束过严或想法模糊。" + esc(d.error || d.message || "")
        + " 建议换一种更具体的想法描述（指明指标与买卖逻辑）重新发起。</div>";
    }

    _hasBest() { return !!$("#srBestCard .sr-hero-name", this.modalEl); }

    /* ── 联动：跳到策略库的「策略详情」（一屏看回测记录 / 评判 / 前瞻 / 落地） ──
       采纳时后端按 sr_<run_uuid 末段小写> 落库，故这里用同一口径拼 id。 */
    async _openLifecycle(runUuid, btn) {
      const sid = "sr_" + String(runUuid || "").split("-").pop().toLowerCase();
      if (btn) btn.disabled = true;
      try {
        await this._fetch("/api/backtest/strategies/" + encodeURIComponent(sid) + "/lifecycle?run_limit=1");
        const panel = window.initBacktestPanel ? window.initBacktestPanel() : null;
        if (!panel) { alert("回测面板未就绪"); return; }
        this.close();
        panel.show();
        panel.openLifecycle(sid);
      } catch (e) {
        alert("看「策略一生」要先采纳这个策略：先点左边的「存入策略库」，再回来点这个按钮。\n（加载失败原因：" + (e.message || e) + "）");
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    /* ── 落地三动作 ── */
    async _adopt(runUuid, action, btn) {
      btn.disabled = true;
      try {
        const res = await this._fetch(API + "/runs/" + encodeURIComponent(runUuid) + "/adopt", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: action }),
        });
        alert(res.msg || (res.code === 0 ? "已落地" : "操作未完成"));
      } catch (e) {
        alert("操作失败：" + (e.message || e));
      } finally {
        btn.disabled = false;
      }
    }
  };

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.StrategyResearchPanel = StrategyResearchPanel;

  // 自注册面板实例（入口按钮在 index.html 内联 onclick，不改 app.js）
  function boot() {
    if (!window.StockRadar.strategyResearchPanel) {
      window.StockRadar.strategyResearchPanel = new StrategyResearchPanel();
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
