/* AI 信号评测 —— 回测面板子页签渲染
 * v3（2026-11-11）：多周期洞察 + 无卡片样式
 *   · 新增：多周期记分卡 / 标的排行榜 / 周期×维度矩阵 / 维度切片
 *   · 旧区块全部保留：tabs / 4 KPI / 组合净值 / 旧胜率矩阵 / 校准 / TOP5 /
 *     145+ 行样本表（新增分页与排序）/ 基准 alpha / 条件单 / 未识别补标
 *   · 样式去卡片：靠 1px 细线、字号与留白分层；行内操作改文字按钮
 */
(function () {
  "use strict";

  const CYCLES = [5, 10, 20, 30, 60];
  const COSTS = [0, 0.05, 0.1];
  const PERIODS = [
    { k: "5d", v: 5, label: "5 日" },
    { k: "10d", v: 10, label: "10 日" },
    { k: "20d", v: 20, label: "20 日" },
    { k: "30d", v: 30, label: "30 日" },
    { k: "60d", v: 60, label: "60 日" },
    { k: "now", v: "now", label: "至今" },
  ];
  const PERIOD_LABEL = PERIODS.reduce((m, p) => ((m[p.k] = p.label), m), {});
  const LB_SORTS = [
    { k: "signals", label: "信号数" },
    { k: "winrate", label: "胜率" },
    { k: "avg", label: "均值" },
  ];
  const SAMPLE_SIZES = [25, 50, 100, "all"];
  const FILTER_LABEL = { all: "全部", buy: "buy 买入", sell: "sell 卖出", holding: "持仓中", paired: "配对平仓" };
  const DIM_LABEL = { decision: "决策", confidence: "置信度", type: "类型", entry: "入场", month: "月份" };
  const DIM_ORDER = ["decision", "confidence", "type", "entry", "month"];
  const STATUS_LABEL = { ok: "可评估", pending: "待成熟", stale: "数据滞后", missing: "无行情" };
  const SAMPLE_HEAD = [
    { k: "sym", label: "标的", sort: false },
    { k: "dec", label: "信号", sort: false },
    { k: "date", label: "信号日", sort: true },
    { k: "price", label: "成本价", sort: false },
    { k: "conf", label: "置信度", sort: true },
    ...CYCLES.map((c) => ({ k: String(c), label: c + " 日", sort: true })),
    { k: "now", label: "至今", sort: true },
    { k: "peak", label: "最高利润", sort: true },
    { k: "maxdd", label: "最大亏损", sort: true },
    { k: "status", label: "状态", sort: false },
    { k: "ops", label: "操作", sort: false },
  ];

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = (v, d = 1) => (v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(d) + "%");
  const ratio = (v, d = 1) => (v == null ? "—" : (v * 100).toFixed(d) + "%");
  // A 股口径：红 = 正 / 高胜率（up），绿 = 负 / 低胜率（down），灰 = 中性 / 无数据。
  // retTone 用于收益（正红负绿），winTone 用于胜率（≥50% 红 / <50% 绿）。
  const retTone = (v) => (v == null || v === 0 ? "neutral" : v > 0 ? "up" : "down");
  const winTone = (rate) => (rate == null ? "muted" : rate >= 0.5 ? "up" : "down");
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const typeLabel = (t) => (t === "stock" ? "个股" : t === "ETF" ? "ETF" : String(t || "—"));
  const confLevel = (c) => (c >= 0.75 ? "高" : c >= 0.6 ? "中" : "低");
  const DECISION_TXT = { buy: "BUY 买入", sell: "SELL 卖出", hold: "HOLD 观望" };
  const DECISION_CLS = { buy: "buy", sell: "sell", hold: "hold" };
  const decisionChip = (d) =>
    `<span class="se-chip ${DECISION_CLS[d] || "flat"}">${DECISION_TXT[d] || esc(d || "—")}</span>`;
  const periodOf = (row, key) => {
    const p = (row && row.periods) || {};
    const cell = p[key] || {};
    return { n: cell.n || 0, winrate: cell.winrate == null ? null : cell.winrate, avg: cell.avg == null ? null : cell.avg };
  };
  const heat = (wr, n) => {
    // 热力只做 3%–8% 透明底；低样本再降一档，避免把 n=1 读成结论。
    // 高胜率淡红 / 低胜率淡绿（A 股口径），与胜率文字的 up / down 保持一致。
    if (wr == null || !n) return "rgba(28,36,48,.024)";
    const d = wr - 0.5;
    const a = clamp(clamp(0.03 + Math.abs(d) * 0.14, 0.03, 0.08) * (n < 5 ? 0.6 : 1), 0.03, 0.08);
    return d >= 0 ? `rgba(231,76,60,${a.toFixed(3)})` : `rgba(23,107,91,${a.toFixed(3)})`;
  };

  const SignalEval = {
    filter: "all",
    cost: 0, // 单边交易成本%（0 = 毛口径）
    lbPeriod: "5d",
    lbSort: "signals",
    lbExpanded: false,
    samplePage: 1,
    sampleSize: 25,
    sampleSortKey: "date",
    sampleSortDir: "desc",
    sampleOpenId: null,
    unrecPage: 1,
    unrecSize: 10,
    _data: {
      overview: null, matrix: null, signals: null, top: null, unrecognized: null,
      portfolio: null, calibration: null, conditional: null,
    },
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
          window.StockRadar.api.fetchJSON("/api/signal-eval/unrecognized?size=200").then((d) => (this._data.unrecognized = d.data)).catch(() => (this._data.unrecognized = null)),
          window.StockRadar.api.fetchJSON(`/api/signal-eval/calibration${q}`).then((d) => (this._data.calibration = d.data)).catch(() => (this._data.calibration = null)),
          window.StockRadar.api.fetchJSON(`/api/signal-eval/conditional${q}`).then((d) => (this._data.conditional = d.data)).catch(() => (this._data.conditional = null)),
        ]);
        this.render();
      } catch (e) {
        view.innerHTML = `<div class="se-empty">加载失败：${esc(e.message)}<br>
          <button class="se-btn primary" id="seRetryBtn" type="button">重试</button></div>`;
        $("#seRetryBtn")?.addEventListener("click", () => this.open());
      }
    },

    /* ── 成本开关：切换后重拉 ── */
    setCost(v) {
      if (this.cost === v) return;
      this.cost = v;
      this.samplePage = 1;
      this.sampleOpenId = null;
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
        `<button type="button" class="se-seg-btn${this.cost === v ? " active" : ""}" data-cost="${v}" aria-pressed="${this.cost === v}">${v === 0 ? "不计费" : v + "%"}</button>`
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
              <div class="se-rescan-bar" id="seRescanBar" style="display:none">
                <div class="se-rescan-track"><div class="se-rescan-fill" id="seRescanFill"></div></div>
                <span id="seRescanTxt">扫描中...</span>
              </div>
              <button class="se-btn" id="seRescanBtn" type="button">↻ 重扫消息库</button>
            </div>
          </header>

          <div class="se-tabs-wrap">
            <div class="se-tabs" id="seStats" role="tablist" aria-label="信号样本筛选"></div>
            <span class="se-tabs-tail" id="seFilterInfo"></span>
          </div>

          <section class="se-section se-first" aria-labelledby="seSecOverview">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecOverview">概览 · 主指标与多周期记分卡</h2>
                <p>4 项主指标沿用生产口径（buy 5 日 / 期望 / 盈亏比 / alpha）· 记分卡按持有周期拆分</p>
              </div>
              <div class="se-sec-tools">
                <span class="se-tone-legend"><b class="up">红</b>=高胜率/正收益 · <b class="down">绿</b>=低胜率/负收益（A股口径）</span>
                <span class="se-note-chip">5 日主口径 · 60 日样本少仅供参考</span>
              </div>
            </div>
            <div class="se-kpi" id="seOverview"></div>
            <div class="se-cards" id="seScorecards"></div>
          </section>

          <section class="se-section" aria-labelledby="seSecLb">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecLb">标的排行榜</h2>
                <p>按 symbol 聚合买入信号 · 切换周期重算胜率与均值 · BEST / WORST 取当前周期可评估标的</p>
              </div>
              <div class="se-sec-tools">
                <span class="se-sort-lbl">周期</span>
                <span class="se-seg" id="seLbPeriodSeg" role="group" aria-label="排行榜周期"></span>
                <span class="se-sort-lbl">排序</span>
                <span class="se-seg" id="seLbSortSeg" role="group" aria-label="排行榜排序"></span>
              </div>
            </div>
            <div class="se-lb-head" aria-hidden="true">
              <span>#</span>
              <span>标的</span>
              <span class="se-r">信号</span>
              <span class="se-r">所选周期胜率</span>
              <span class="se-r">所选周期均值</span>
              <span class="se-c" title="每行四个迷你条：5 / 10 / 20 / 60 日胜率">5·10·20·60</span>
              <span class="se-c">最新决策</span>
              <span>日期</span>
              <span class="se-c">置信</span>
              <span class="se-c">状态</span>
            </div>
            <div class="se-lb-list" id="seLeaderboard"></div>
            <div class="se-lb-foot">
              <span class="se-lb-count" id="seLbCount"></span>
              <button type="button" class="se-link" id="seLbExpand">展开全部 ↓</button>
              <span class="se-lb-legend" id="seLbLegend"></span>
            </div>
          </section>

          <section class="se-section" aria-labelledby="seSecDims">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecDims">维度切片</h2>
                <p>决策 / 周期 / 置信度 / 类型 / 入场 / 月份 · 统一 5 日口径 · n&lt;5 灰显并标 *</p>
              </div>
              <div class="se-sec-tools"><span class="se-sort-lbl">每行：label / n / 胜率 / 均值</span></div>
            </div>
            <div class="se-dims" id="seDims"></div>
          </section>

          <section class="se-section" aria-labelledby="seSecMx">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecMx">周期 × 维度矩阵</h2>
                <p>列 = 持有周期 + 样本 · 行 = 决策 / 置信度 / 类型 / 入场 / 月份 · 每格 = 胜率 + 分母</p>
              </div>
              <div class="se-sec-tools"><span class="se-sort-lbl">n&lt;5 灰显 · 热力 3%–8%</span></div>
            </div>
            <div class="se-scroll-hint">← 左右滑动查看 30 日 / 60 日 / 样本列 →</div>
            <div class="se-tbl-scroll">
              <table class="se-mx" id="seMxNew"></table>
            </div>
            <div class="se-mx-foot">
              <span class="se-lg"><i class="up"></i>高胜率</span>
              <span class="se-lg"><i class="down"></i>低胜率</span>
              <span class="se-lg"><i class="n"></i>样本不足 · <b>*</b> n&lt;5</span>
              <span class="se-mx-sp">样本 = 该分层在任一周期上的最大分母 · sell 取反向 · 条件单默认排除</span>
            </div>
          </section>

          <section class="se-section" aria-labelledby="seSecAnalysis">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecAnalysis">组合净值与旧胜率矩阵</h2>
                <p>组合曲线按等权独立子仓位计算 · 旧矩阵保留方向 × 周期口径（buy / sell 反向 / 配对 / 置信度）</p>
              </div>
            </div>
            <div class="se-two-col">
              <div class="se-sub">
                <div class="se-sub-head">
                  <h3>组合净值曲线</h3>
                  <span class="tag">buy 收盘建仓 / sell 收盘平仓 · 等权独立子仓位 · 净值=1 起</span>
                </div>
                <div class="se-bd" id="sePortfolio"></div>
              </div>
              <div class="se-sub">
                <div class="se-sub-head">
                  <h3>胜率矩阵</h3>
                  <span class="tag">旧口径 · 方向 × 周期 · 样本≥5 显示 95%CI</span>
                </div>
                <div class="se-scroll-hint">← 左右滑动查看周期列与样本 →</div>
                <div class="se-bd se-matrix-wrap"><table class="se-matrix" id="seMatrix"></table></div>
              </div>
            </div>
          </section>

          <section class="se-section" aria-labelledby="seSecCal">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecCal">校准 · 预测置信度 vs 实现胜率</h2>
                <p>A2 分桶统计 · 柱状 = 实现胜率，竖线 = 平均预测 · 样本 n&lt;10 不出偏差结论</p>
              </div>
            </div>
            <div class="se-scroll-hint">← 左右滑动查看对比 / 偏差 / 状态列 →</div>
            <div class="se-bd" id="seCalibration"></div>
          </section>

          <section class="se-section" aria-labelledby="seSecTop">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecTop">极端案例 TOP5</h2>
                <p>buy 正向 · sell 反向（卖飞 / 卖对）· 收益口径 = 至今（费后）</p>
              </div>
            </div>
            <div class="se-top-grid" id="seTop"></div>
          </section>

          <section class="se-section" aria-labelledby="seSecSamples">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecSamples">信号样本</h2>
                <p id="seSamplesSub">点行展开事件流 · 点标的 / 周期跳 K 线 · 支持排序与分页</p>
              </div>
              <div class="se-sec-tools"><span class="se-sort-lbl">标题可点击排序 · 每页 25 / 50 / 100 / 全部</span></div>
            </div>
            <div class="se-scroll-hint">← 左右滑动查看 5/10/20/30/60 日收益与操作列 →</div>
            <div class="se-tbl-wrap">
              <table class="se-samples" id="seSamples"></table>
            </div>
            <div class="se-pag" id="seSamplesPag"></div>
          </section>

          <section class="se-section" aria-labelledby="seSecBench">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecBench">基准对比 · alpha</h2>
                <p>信号收益 − 同期行业基准 / 沪深300 · 未识别与大盘类不进本表</p>
              </div>
              <div class="se-sec-tools"><span class="se-sort-lbl">仅 buy 有基准 · 指数不扣费</span></div>
            </div>
            <div id="seBenchSummary"></div>
            <div class="se-scroll-hint">← 左右滑动查看基准 / alpha / 跑赢 →</div>
            <div class="se-tbl-wrap">
              <table class="se-samples se-bench" id="seBench"></table>
            </div>
            <p class="se-foot-note">* 列名沿用旧页「沪深300」，实际存的是行业基准（无映射时回退 000300）；指数基准不扣费，个股收益按单边成本 ×2 扣费。</p>
          </section>

          <section class="se-section" aria-labelledby="seSecCond">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecCond">条件单（不计入胜率）</h2>
                <p>回踩 / 突破触发判定 · 非价格条件不参与 · 触发后收益尚未上线</p>
              </div>
              <div class="se-sec-tools"><span class="se-note-chip">条件单不计入胜率</span></div>
            </div>
            <div class="se-bd" id="seConditional"></div>
          </section>

          <section class="se-section" id="seUnrecSection" aria-labelledby="seSecUnrec" style="display:none">
            <div class="se-sec-head">
              <div class="se-sec-title">
                <h2 id="seSecUnrec">未识别标的标注</h2>
                <p>个股/ETF 提问但名称未识别 · 补标后立即参与统计 · 大盘日评已自动归档不在此列</p>
              </div>
            </div>
            <div class="se-scroll-hint">← 左右滑动查看核心结论与补标操作 →</div>
            <div class="se-bd" id="seUnrecognized"></div>
          </section>

          <footer class="se-foot">
            <div><i>主口径</i> buy 5 日胜率（固定 5 日，不混「至今」）；各周期分母 = 该周期有收益的信号数</div>
            <div><i>成本</i> 单边成本 ×2（买 + 卖各一次）；峰值/回撤为诊断极值不扣费</div>
            <div><i>样本</i> n &lt; 5 灰显仅供参考 · 配对平仓为单笔已实现收益、不分周期 · 未识别 / 大盘类不进主统计</div>
            <div><i>条件单</i> 不计入胜率（生产同口径）· 触发后收益后端暂无字段，旧入口保留</div>
            <div><i>组合净值</i> 等权独立子仓位，本期已改为按子仓位数量归一（不再出现 &lt; -60% 的旧口径）</div>
            <div><i>数据</i> db/signal_eval.db 只读 · 页面按需拉取 /api/signal-eval/*</div>
          </footer>
        </div>`;

      $$("#btSignalEvalView .se-seg-btn[data-cost]").forEach((b) =>
        b.addEventListener("click", () => this.setCost(parseFloat(b.dataset.cost))));
      $("#seRescanBtn")?.addEventListener("click", () => this.rescan());
      $("#seLbExpand")?.addEventListener("click", () => {
        this.lbExpanded = !this.lbExpanded;
        this.renderLeaderboard();
      });

      this.renderKpi();
      this.renderScorecards();
      this.renderStats();
      this.renderLeaderboardTools();
      this.renderLeaderboard();
      this.renderDims();
      this.renderNewMatrix();
      this.renderPortfolio();
      this.renderMatrix();
      this.renderCalibration();
      this.renderTop();
      this.renderSamples();
      this.renderBench();
      this.renderConditional();
      this.renderUnrecognized();
    },

    /* ══════════ 4 项旧 KPI ══════════ */
    renderKpi() {
      const el = $("#seOverview");
      if (!el) return;
      const d = this._data.overview || {};
      const w = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
      // 顶部固定 5 日口径：不把不同持有时长混进一个「至今」胜率。
      // n_5d=0（样本不足）显示 —，不回退旧「至今」胜率/CI，避免两个口径混看。
      const n5 = d.buy_n_5d;
      const has5 = Number.isFinite(n5) && n5 > 0;
      const wr5 = has5 ? d.buy_winrate_5d : null;
      let hint5 = "固定5日 · 样本不足";
      if (has5) {
        const gaps = d.buy_data_gaps || 0;
        hint5 = `固定5日 · n=${n5}`;
        // 独立标的修正口径：同一标的多条信号高度相关（5 日窗口重叠），
        // 这里只提示「主口径 n 里有多少是重复信号」，不改主胜率/CI。
        if (Number.isFinite(d.buy_unique_symbols)) {
          hint5 += ` · 独立标的 ${d.buy_unique_symbols}`;
          if (d.buy_dup_signals > 0) hint5 += `（重复 ${d.buy_dup_signals}）`;
        }
        hint5 += gaps > 0 ? ` · 不可评估 ${gaps}` : "";
        // CI 门槛与矩阵一致：n>=5 才显示 95%CI（n<5 的区间没有解释力）
        const ci5 = d.buy_ci_5d;
        if (n5 >= 5 && Array.isArray(ci5) && ci5.length >= 2) {
          hint5 += ` · 95%CI ${Math.round(ci5[0] * 100)}~${Math.round(ci5[1] * 100)}%`;
        }
      }
      if (d.conditional_count) hint5 += ` · 另有 ${d.conditional_count} 条条件单未计入`;
      const cards = [
        {
          l: "buy 5日胜率", v: has5 ? w(wr5) : "—", tag: "主口径",
          cls: winTone(wr5), hint: hint5,
        },
        {
          l: "每笔期望", v: d.expectancy == null ? "—" : (d.expectancy > 0 ? "+" : "") + d.expectancy.toFixed(2) + "%",
          tag: "至今", cls: retTone(d.expectancy), hint: "均盈 / 均亏差值",
        },
        {
          l: "盈亏比", v: d.payoff == null ? "—" : d.payoff.toFixed(2),
          tag: "至今", cls: d.payoff == null ? "neutral" : d.payoff >= 1 ? "up" : "down", hint: "> 1 才划算",
        },
        {
          l: "平均 alpha", v: d.avg_alpha == null ? "—" : (d.avg_alpha > 0 ? "+" : "") + d.avg_alpha.toFixed(1) + "%",
          tag: "至今", cls: retTone(d.avg_alpha),
          hint: d.beat_total ? `跑赢沪深300 ${d.beat_count}/${d.beat_total}` : "暂无基准数据",
        },
      ];
      el.innerHTML = cards.map((c) => `
        <div class="se-kpi-card">
          <div class="lbl">${c.l}<span class="se-kpi-tag">${c.tag}</span></div>
          <div class="val ${c.cls}">${c.v}</div>
          <div class="hint" title="${esc(c.hint)}">${c.hint}</div>
        </div>`).join("");
    },

    /* ══════════ 多周期记分卡 ══════════ */
    renderScorecards() {
      const el = $("#seScorecards");
      if (!el) return;
      const stats = ((this._data.overview || {}).period_stats) || [];
      if (!stats.length) {
        el.innerHTML = '<div class="se-empty">多周期统计未返回，请确认后端版本</div>';
        return;
      }
      el.innerHTML = stats.map((p) => {
        const isMain = p.period === "5d";
        const isFar = p.period === "60d";
        const tag = isMain ? '<span class="se-pc-tag">主口径</span>'
          : isFar ? '<span class="se-pc-tag warn">长线参考</span>'
            : p.period === "now" ? '<span class="se-pc-tag ghost">最新收盘</span>' : "";
        const hasCI = p.n >= 5 && Array.isArray(p.ci) && p.ci.length >= 2;
        const lo = hasCI ? p.ci[0] * 100 : null;
        const hi = hasCI ? p.ci[1] * 100 : null;
        const crosses = hasCI && lo <= 50 && hi >= 50;
        // 胜率数字 + CI 条统一走 winTone（≥50% 红 / <50% 绿 / 无数据灰）
        const wrTone = winTone(p.winrate);
        const ciHtml = hasCI
          ? `<div class="se-pc-ci-top"><span>95% CI</span><b>${lo.toFixed(1)}–${hi.toFixed(1)}%</b>${crosses ? '<span class="over">跨 50%</span>' : ""}</div>
             <div class="se-pc-ci-track"><i style="left:${lo.toFixed(1)}%;width:${(hi - lo).toFixed(1)}%"></i><u></u><s style="left:${(p.winrate == null ? 0 : p.winrate * 100).toFixed(1)}%"></s></div>`
          : `<div class="se-pc-ci-top"><span>95% CI</span><b>n&lt;5 不展示</b></div><div class="se-pc-ci-track"></div>`;
        return `
          <article class="se-pcard ${isMain ? "is-main" : ""} ${isFar ? "is-far" : ""}" data-period="${p.period}"
            title="${PERIOD_LABEL[p.period]} · n=${p.n} · 胜率 ${ratio(p.winrate)} · 均值 ${pct(p.avg)}">
            <div class="se-pc-head"><span class="se-pc-name">${p.period === "now" ? "<b>至今</b>" : `<b>${p.period.replace("d", "")}</b> 日`}</span>${tag}</div>
            <div class="se-pc-num ${wrTone}">${p.winrate == null ? "—" : (p.winrate * 100).toFixed(1)}<i>%</i></div>
            <div class="se-pc-meta"><span>n <b>${p.n}</b></span><span>均值 <b class="${retTone(p.avg)}">${pct(p.avg)}</b></span></div>
            <div class="se-pc-ci ${wrTone} ${hasCI ? "" : "low"}">${ciHtml}</div>
          </article>`;
      }).join("");
    },

    /* ══════════ 顶部 tabs（真过滤样本表） ══════════ */
    renderStats() {
      const el = $("#seStats");
      if (!el) return;
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
        ? `<span class="se-stat se-stat-extra" title="sell 反向：卖出后跌 = 卖对">
             <span class="k">sell 反向胜率</span><span class="v">${Math.round(ov.sell_winrate_reverse * 100)}%</span></span>`
        : "";
      el.innerHTML = btns.map((b) => `
        <button type="button" role="tab" aria-selected="${this.filter === b.k}"
          class="se-stat se-tab ${this.filter === b.k ? "active" : ""}" data-f="${b.k}">
          <span class="k">${b.l}</span><b>${n[b.k]}</b>
        </button>`).join("") + extra;
      $$("#seStats .se-tab[data-f]").forEach((b) =>
        b.addEventListener("click", () => this.setFilter(b.dataset.f)));
      this.updateFilterInfo();
    },

    updateFilterInfo() {
      const info = $("#seFilterInfo");
      if (!info) return;
      const label = FILTER_LABEL[this.filter] || this.filter;
      const total = this._filterSignals().length;
      info.innerHTML = `当前：<b>${label}</b> · ${total} 条` +
        (this.filter === "all" ? "" : ' <button type="button" class="se-link" id="seFilterReset">清除筛选</button>');
      $("#seFilterReset")?.addEventListener("click", () => this.setFilter("all"));
    },

    setFilter(f) {
      this.filter = f;
      this.samplePage = 1;
      this.sampleOpenId = null;
      if (!this._data.signals) return;
      this.renderStats();
      this.renderSamples();
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

    /* ══════════ 标的排行榜 ══════════ */
    renderLeaderboardTools() {
      const pEl = $("#seLbPeriodSeg");
      const sEl = $("#seLbSortSeg");
      if (!pEl || !sEl) return;
      pEl.innerHTML = PERIODS.map((p) =>
        `<button type="button" class="se-seg-btn${this.lbPeriod === p.k ? " active" : ""}"
          data-period="${p.k}" aria-pressed="${this.lbPeriod === p.k}">${p.label}</button>`).join("");
      sEl.innerHTML = LB_SORTS.map((s) =>
        `<button type="button" class="se-seg-btn${this.lbSort === s.k ? " active" : ""}"
          data-sort="${s.k}" aria-pressed="${this.lbSort === s.k}">${s.label}</button>`).join("");
      $$("#seLbPeriodSeg .se-seg-btn").forEach((b) => b.addEventListener("click", () => {
        if (this.lbPeriod === b.dataset.period) return;
        this.lbPeriod = b.dataset.period;
        this.renderLeaderboardTools();
        this.renderLeaderboard();
      }));
      $$("#seLbSortSeg .se-seg-btn").forEach((b) => b.addEventListener("click", () => {
        if (this.lbSort === b.dataset.sort) return;
        this.lbSort = b.dataset.sort;
        this.renderLeaderboardTools();
        this.renderLeaderboard();
      }));
    },

    _sortedSymbols(list) {
      const p = this.lbPeriod;
      const wr = (o) => {
        const d = periodOf(o, p);
        return d.n >= 2 && d.winrate != null ? d.winrate : -1;
      };
      const avg = (o) => {
        const d = periodOf(o, p);
        return d.avg == null ? -999 : d.avg;
      };
      const cmp = {
        signals: (a, b) => b.signals - a.signals || wr(b) - wr(a) || avg(b) - avg(a),
        winrate: (a, b) => wr(b) - wr(a) || b.signals - a.signals || avg(b) - avg(a),
        avg: (a, b) => avg(b) - avg(a) || b.signals - a.signals,
      }[this.lbSort] || ((a, b) => b.signals - a.signals);
      return list.slice().sort(cmp);
    },

    _bestWorst(list) {
      const p = this.lbPeriod;
      const eligible = list.filter((o) => periodOf(o, p).n >= 2);
      const pool = eligible.length ? eligible : list.filter((o) => periodOf(o, p).n >= 1);
      if (!pool.length) return { best: null, worst: null };
      const ranked = pool.slice().sort((a, b) =>
        (periodOf(b, p).winrate ?? -1) - (periodOf(a, p).winrate ?? -1) || b.signals - a.signals);
      return {
        best: ranked[0].symbol_key,
        worst: ranked.length > 1 ? ranked[ranked.length - 1].symbol_key : null,
      };
    },

    renderLeaderboard() {
      const el = $("#seLeaderboard");
      if (!el) return;
      const list = ((this._data.overview || {}).symbol_stats) || [];
      const countEl = $("#seLbCount");
      const legendEl = $("#seLbLegend");
      const expandEl = $("#seLbExpand");
      if (!list.length) {
        el.innerHTML = '<div class="se-empty">暂无按标的聚合的买入信号</div>';
        if (countEl) countEl.textContent = "0 个标的";
        if (legendEl) legendEl.innerHTML = "";
        if (expandEl) expandEl.style.display = "none";
        return;
      }
      const sorted = this._sortedSymbols(list);
      const hi = this._bestWorst(sorted);
      const show = this.lbExpanded ? sorted : sorted.slice(0, 8);
      const pLabel = PERIOD_LABEL[this.lbPeriod];
      el.innerHTML = show.map((o, i) => {
        const d = periodOf(o, this.lbPeriod);
        const single = d.n < 2;
        const isBest = o.symbol_key === hi.best && !single;
        const isWorst = o.symbol_key === hi.worst && !single;
        const flag = isBest ? '<span class="se-lb-flag best">BEST</span>'
          : isWorst ? '<span class="se-lb-flag worst">WORST</span>' : "";
        const code = (o.symbol_key || "").slice(2);
        const latest = o.latest || {};
        const date = (latest.signal_time || "").slice(0, 10);
        const conf = latest.confidence;
        const status = STATUS_LABEL[latest.data_status] || "—";
        const wrCls = single ? "muted" : winTone(d.winrate);
        const barCls = single ? "na" : winTone(d.winrate);
        const dec = latest.decision;
        const decCls = DECISION_CLS[dec] || "hold";
        const multi = [5, 10, 20, 60].map((k) => {
          const pd = periodOf(o, k + "d");
          const cls = pd.n ? winTone(pd.winrate) : "u";
          return `<span class="se-mm-cell"><span class="se-mm ${cls}" title="${k} 日胜率 ${pd.n ? ratio(pd.winrate) : "—"} · n=${pd.n}"><i style="height:${pd.n ? Math.round((pd.winrate || 0) * 100) : 0}%"></i></span><em>${k}</em></span>`;
        }).join("");
        return `
          <div class="se-lb-row ${isBest ? "is-best" : ""} ${isWorst ? "is-worst" : ""}" data-key="${esc(o.symbol_key)}">
            <span class="se-lb-rank">${String(i + 1).padStart(2, "0")}</span>
            <span class="se-lb-name" title="${esc(o.symbol_name || o.symbol_key)} ${esc(code)}">
              <b>${esc(o.symbol_name || o.symbol_key)}</b><span class="se-lb-code">${esc(code)}</span>
              <span class="se-lb-type">${typeLabel(o.type)}</span>${flag}
            </span>
            <span class="se-lb-cell se-lb-sig" data-l="信号数">${o.signals}</span>
            <span class="se-lb-cell se-lb-wr" data-l="${pLabel}胜率">
              <b class="${wrCls}">${single ? "—" : ratio(d.winrate)}</b>
              <span class="se-mini-bar ${barCls}"><i style="width:${single ? 0 : Math.round((d.winrate || 0) * 100)}%"></i><u></u></span>
              ${single ? `<small>n=${d.n}</small>` : ""}
            </span>
            <span class="se-lb-cell se-lb-avg" data-l="${pLabel}均值"><b class="${retTone(d.avg)}">${pct(d.avg)}</b></span>
            <span class="se-lb-cell se-lb-multi" data-l="5 / 10 / 20 / 60 胜率">${multi}</span>
            <span class="se-lb-cell se-lb-dec ${decCls}" data-l="最新决策">${esc(dec || "—")}</span>
            <span class="se-lb-cell se-lb-date" data-l="日期">${esc(date || "—")}</span>
            <span class="se-lb-cell se-lb-conf" data-l="置信度">${conf == null ? '<span class="se-muted">未评级</span>' : `<b>${conf.toFixed(2)}</b><small>${confLevel(conf)}</small>`}</span>
            <span class="se-lb-cell se-lb-status" data-l="状态"><span class="se-status">${status}</span></span>
          </div>`;
      }).join("");

      const totalSignals = sorted.reduce((a, o) => a + o.signals, 0);
      const totalN = sorted.reduce((a, o) => a + periodOf(o, this.lbPeriod).n, 0);
      if (countEl) countEl.textContent = `${sorted.length} 个标的 · ${totalSignals} 条买入信号 · ${pLabel}可评估 ${totalN}`;
      if (expandEl) {
        expandEl.textContent = this.lbExpanded ? "收起排行榜 ↑" : `展开全部 ${sorted.length} 个标的 ↓`;
        expandEl.style.display = sorted.length > 8 ? "" : "none";
      }
      const best = sorted.find((o) => o.symbol_key === hi.best);
      const worst = sorted.find((o) => o.symbol_key === hi.worst);
      if (legendEl) legendEl.innerHTML = best && worst
        ? `最佳 <b>${esc(best.symbol_name || best.symbol_key)}</b> <i>${ratio(periodOf(best, this.lbPeriod).winrate)}</i>
           · 最差 <b>${esc(worst.symbol_name || worst.symbol_key)}</b> <i>${ratio(periodOf(worst, this.lbPeriod).winrate)}</i>`
        : "";
    },

    /* ══════════ 维度切片（6 组） ══════════ */
    renderDims() {
      const el = $("#seDims");
      if (!el) return;
      const ov = this._data.overview || {};
      const dims = ov.dimension_stats || [];
      const periodStats = ov.period_stats || [];
      if (!dims.length && !periodStats.length) {
        el.innerHTML = '<div class="se-empty">维度统计未返回，请确认后端版本</div>';
        return;
      }
      const rowsOf = (dim) => dims.filter((x) => x.dim === dim).map((x) => {
        const d = periodOf(x, "5d");
        const label = dim === "type" ? typeLabel(x.label) : x.label;
        return { label, n: d.n, winrate: d.winrate, avg: d.avg, note: x.note || "", badge: "" };
      });
      const groups = [];
      groups.push({ key: "decision", title: "按决策", note: "5 日口径 · sell 取反向", rows: rowsOf("decision") });
      groups.push({
        key: "period", title: "按周期", note: "buy · 分母随窗口变化",
        rows: periodStats.map((p) => ({
          label: PERIOD_LABEL[p.period] + (p.period === "5d" ? "（主口径）" : p.period === "60d" ? "（长线参考）" : ""),
          n: p.n, winrate: p.winrate, avg: p.avg, note: "", badge: "",
        })),
      });
      ["confidence", "type", "entry", "month"].forEach((dim) => {
        groups.push({
          key: dim,
          title: "按" + DIM_LABEL[dim],
          note: dim === "month" ? "5 日口径 · buy" : "5 日口径 · buy",
          rows: rowsOf(dim),
        });
      });
      el.innerHTML = groups.map((g) => `
        <div class="se-dim" data-dim="${g.key}">
          <div class="se-dim-h"><h3>${g.title}</h3><em>${g.note}</em></div>
          <div class="se-dim-rows">
            ${g.rows.length ? g.rows.map((r) => {
              const low = r.n > 0 && r.n < 5;
              return `<div class="se-dim-row ${low ? "low" : ""}" ${r.note ? `title="${esc(r.note)}"` : ""}>
                <span class="se-dim-l">${esc(r.label)}${r.badge ? `<span class="se-dim-badge">${r.badge}</span>` : ""}</span>
                <span class="se-dim-n ${low ? "low" : ""}">n=${r.n}${low ? "*" : ""}</span>
                <span class="se-dim-wr ${winTone(r.winrate)}">${ratio(r.winrate)}</span>
                <span class="se-dim-avg ${retTone(r.avg)}">${pct(r.avg)}</span>
              </div>`;
            }).join("") : '<div class="se-dim-row"><span class="se-dim-l se-muted">暂无样本</span></div>'}
          </div>
        </div>`).join("");
    },

    /* ══════════ 周期 × 维度矩阵（新） ══════════ */
    renderNewMatrix() {
      const table = $("#seMxNew");
      if (!table) return;
      const dims = ((this._data.overview || {}).dimension_stats) || [];
      if (!dims.length) {
        table.innerHTML = `<tbody><tr><td class="se-empty">维度矩阵未返回</td></tr></tbody>`;
        return;
      }
      let html = `<thead><tr><th>维度 / 分层</th>${PERIODS.map((p) => `<th>${p.label}</th>`).join("")}<th>样本</th></tr></thead><tbody>`;
      DIM_ORDER.forEach((dim) => {
        const rows = dims.filter((x) => x.dim === dim);
        rows.forEach((row, idx) => {
          let sampleN = 0;
          PERIODS.forEach((p) => { sampleN = Math.max(sampleN, periodOf(row, p.k).n); });
          const label = dim === "type" ? typeLabel(row.label) : row.label;
          const badge = row.note ? '<span class="t" title="' + esc(row.note) + '">主样本排除</span>' : "";
          html += `<tr class="${idx === 0 ? "se-mx-grp" : ""}">
            <th class="se-mx-l"><span class="g">${DIM_LABEL[dim]}</span><span class="l">${esc(label)}</span>${badge}</th>`;
          PERIODS.forEach((p) => {
            const c = periodOf(row, p.k);
            if (!c.n) {
              html += `<td class="se-mx-c is-empty" style="background:${heat(null, 0)}" title="该周期无样本"><b>—</b><small>n=0</small></td>`;
              return;
            }
            const low = c.n < 5;
            const wrTone = winTone(c.winrate);
            html += `<td class="se-mx-c ${low ? "low" : ""}" style="background:${heat(c.winrate, c.n)}"
              title="n=${c.n} · 均值 ${pct(c.avg)}">
              <b class="${wrTone}">${ratio(c.winrate)}${low ? "<sup>*</sup>" : ""}</b>
              <span class="se-mx-bar ${wrTone}"><i style="width:${Math.round((c.winrate || 0) * 100)}%"></i></span>
              <small>n=${c.n}</small></td>`;
          });
          html += `<td class="se-mx-n">${sampleN}</td></tr>`;
        });
      });
      html += "</tbody>";
      table.innerHTML = html;
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
      const eqLines = eqSegs.map((s) => `<polyline fill="none" stroke="var(--brand)" stroke-width="1.8" stroke-linejoin="round" points="${toPts(s)}"/>`).join("");
      const benchSegs = hasBench ? segs(d.bench).map((s) =>
        `<polyline fill="none" stroke="var(--muted)" stroke-width="1.3" stroke-dasharray="5 4" points="${toPts(s)}"/>`).join("") : "";

      const lastEq = d.equity[d.equity.length - 1];
      const benchRet = hasBench && d.bench[d.bench.length - 1] != null ? (d.bench[d.bench.length - 1] - 1) * 100 : null;
      const alpha = benchRet != null && d.return != null ? d.return - benchRet : null;
      const endTone = retTone(d.return);
      const stat = (k, v, cls, sub) => `<span class="se-eq-stat"><span class="k">${k}</span><span class="v ${cls}">${v}</span>${sub ? `<span class="sub">${sub}</span>` : ""}</span>`;
      const lastX = x(n - 1).toFixed(1), lastY = y(lastEq).toFixed(1);
      const anchor = lastX > W * 0.75 ? "end" : "start";
      const dx = anchor === "end" ? -9 : 9;
      const posCount = d.position_count == null ? "—" : d.position_count + " 个";
      const openCount = d.open_positions == null ? "" : `未平仓 ${d.open_positions}`;

      el.innerHTML = `
        <div class="se-eq-legend">
          <span><i class="eq"></i>AI 信号组合</span>
          ${hasBench ? '<span><i class="bench"></i>沪深300</span>' : ""}
          <span><i class="one"></i>净值 1.0</span>
          <span class="se-eq-range">${d.dates[0]} → ${d.dates[n - 1]} · ${n} 个交易日</span>
        </div>
        <div class="se-scroll-hint">← 左右滑动查看净值曲线 →</div>
        <div class="se-eq-scroll">
          <svg class="se-eq-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
            ${grid}${benchSegs}${eqLines}${xLbl}
            <circle cx="${lastX}" cy="${lastY}" r="3.2" fill="var(--brand)"/>
            <text class="se-eq-end ${endTone}" x="${(+lastX + dx).toFixed(1)}" y="${(+lastY - 9).toFixed(1)}" font-size="11" font-weight="700"
              font-family="var(--font-mono)" text-anchor="${anchor}">${pct((lastEq - 1) * 100)}</text>
          </svg>
        </div>
        <div class="se-eq-stats">
          ${stat("区间收益", pct(d.return), retTone(d.return))}
          ${stat("最大回撤", "-" + (d.max_dd ?? 0).toFixed(2) + "%", "down")}
          ${stat("同期沪深300", benchRet == null ? "—" : pct(benchRet), retTone(benchRet))}
          ${stat("超额 alpha", alpha == null ? "—" : pct(alpha), retTone(alpha))}
          ${stat("等权子仓位", posCount, "", openCount)}
        </div>`;
    },

    /* ══════════ 旧胜率矩阵（保留） ══════════ */
    renderMatrix() {
      const d = this._data.matrix || { cols: CYCLES.map(String).concat("now"), rows: [] };
      const cols = d.cols || [];
      const fmtC = (c) => (c === "now" ? "至今" : c + " 日");
      let html = `<thead><tr><th>方向</th>${cols.map((c) => `<th>${fmtC(c)}</th>`).join("")}<th>样本</th></tr></thead><tbody>`;
      d.rows.forEach((r) => {
        html += `<tr><td class="rl">${esc(r.label)}</td>`;
        r.cells.forEach((cell) => {
          const wr = cell.winrate;
          // 旧矩阵本来就是「红 = 高胜率」，这里只把 class 归一到 winTone（不再用 good/bad）
          const cls = winTone(wr);
          const heat = wr == null ? "" : cls;
          const sub = cell.ci
            ? `${cell.n} · ${Math.round(cell.ci[0] * 100)}~${Math.round(cell.ci[1] * 100)}%`
            : (cell.n > 0 ? String(cell.n) : "—");
          html += `<td class="se-hm ${heat}"><span class="se-wr ${cls}">${wr == null ? "—" : (wr * 100).toFixed(1) + "%"}</span>
            <div class="se-wr-sub">${sub}</div></td>`;
        });
        html += `<td class="mono">${r.n}</td></tr>`;
      });
      html += "</tbody>";
      const el = $("#seMatrix");
      if (el) el.innerHTML = html;
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
          ? '<span class="se-muted">样本不足</span>'
          : (b.deviation_pp > 0 ? "+" : "") + b.deviation_pp + "pp";
        const devCls = b.deviation_pp != null && Math.abs(b.deviation_pp) > 15 ? "dev-bad" : "dev-ok";
        const bar = b.realized == null
          ? '<span class="se-muted">—</span>'
          : `<div class="se-cal-bar" title="柱状=实现胜率 ${P(b.realized)} · 竖线=平均预测 ${P(b.avg_pred)}">` +
            `<div class="se-cal-fill" style="width:${Math.round(b.realized * 100)}%"></div>` +
            `<div class="se-cal-pred" style="left:${Math.round((b.avg_pred || 0) * 100)}%"></div></div>`;
        rows += `<tr class="${b.status === "over" || b.status === "under" ? "se-cal-bad" : ""}">
          <td class="mono">${esc(b.range)}</td>
          <td class="mono">${b.n || "—"}</td>
          <td class="mono">${b.n ? P(b.avg_pred) : "—"}</td>
          <td class="mono se-cal-realized ${b.n && b.realized != null ? winTone(b.realized) : "muted"}">${b.n ? P(b.realized) : "—"}</td>
          <td>${bar}</td>
          <td class="mono ${devCls}">${dev}</td>
          <td>${statusChip(b)}</td></tr>`;
      });
      const excl = d.excluded || {};
      el.innerHTML = `
        <div class="se-cal-wrap">
          <div class="se-cal-main se-tbl-scroll">
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
            <span class="code">${esc(code)}</span>
            <span class="pct ${cls}">${pct(s.now_ret)}</span>
          </div>
          <div class="se-top-note">${(s.signal_time || "").slice(0, 10)} · ${note}</div>`;
      };
      const col = (title, list, cls, toneCls, noteFn) => `
        <div class="se-top-col ${toneCls}">
          <h4><span class="dot"></span>${title}<span class="cnt">${list.length}</span></h4>
          <div class="se-top-list">
            ${list.length ? list.map((s, i) => item(i + 1, s, cls, noteFn(s))).join("")
              : '<div class="se-top-none">暂无</div>'}
          </div>
        </div>`;
      const el = $("#seTop");
      if (!el) return;
      el.innerHTML =
        col("buy TOP5 利润", d.buy_profit_top || [], "up", "up", () => "买入至今") +
        col("buy TOP5 亏损", d.buy_loss_top || [], "down", "down", () => "买入至今") +
        col("sell TOP5 卖飞", d.sell_fly_top || [], "up", "up", () => "卖出后涨 → 卖飞") +
        col("sell TOP5 卖对", d.sell_right_top || [], "down", "down", () => "卖出后跌 → 卖对");
    },

    /* ══════════ 信号样本表（分页 / 排序 / 详情） ══════════ */
    _sortSamples(rows) {
      const key = this.sampleSortKey;
      const dir = this.sampleSortDir === "asc" ? 1 : -1;
      const val = (s) => {
        if (key === "date") return (s.signal_time || "") + String(s.id).padStart(8, "0");
        if (key === "conf") return s.confidence;
        if (key === "peak") return s.peak;
        if (key === "maxdd") return s.maxDD;
        if (key === "now") return (s.returns || {}).now;
        return (s.returns || {})[+key];
      };
      return rows.slice().sort((a, b) => {
        const va = val(a), vb = val(b);
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (va === vb) return 0;
        return (va < vb ? -1 : 1) * dir;
      });
    },

    _sampleHeaderHtml() {
      return `<colgroup>
        <col style="width:184px"><col style="width:76px"><col style="width:84px"><col style="width:62px"><col style="width:76px">
        ${CYCLES.map(() => '<col style="width:52px">').join("")}
        <col style="width:58px"><col style="width:62px"><col style="width:62px"><col style="width:78px"><col style="width:170px">
      </colgroup>
      <thead><tr>${SAMPLE_HEAD.map((h) => {
        if (!h.sort) return `<th>${h.label}</th>`;
        const on = this.sampleSortKey === h.k;
        const arr = on ? (this.sampleSortDir === "asc" ? "↑" : "↓") : "↕";
        return `<th class="se-th-sort ${on ? "on" : ""}" data-sk="${h.k}"
          aria-sort="${on ? (this.sampleSortDir === "asc" ? "ascending" : "descending") : "none"}">${h.label}<span class="se-arr">${arr}</span></th>`;
      }).join("")}</tr></thead>`;
    },

    renderSamples() {
      const el = $("#seSamples");
      if (!el) return;
      const items = this._filterSignals();
      const sorted = this._sortSamples(items);
      const total = sorted.length;
      const pageSize = this.sampleSize === "all" ? Math.max(1, total) : this.sampleSize;
      const pages = Math.max(1, Math.ceil(total / pageSize));
      if (this.samplePage > pages) this.samplePage = pages;
      const start = (this.samplePage - 1) * pageSize;
      const pageRows = sorted.slice(start, start + pageSize);

      const confChip = (c) => {
        if (c == null) return '<span class="se-chip flat">未评级</span>';
        const lv = c >= 0.75 ? "conf-h" : c >= 0.6 ? "conf-m" : "conf-l";
        const txt = c >= 0.75 ? "高" : c >= 0.6 ? "中" : "低";
        return `<span class="se-chip ${lv}">${txt} ${c.toFixed(2)}</span>`;
      };
      const cycCell = (s, c) => {
        const v = (s.returns || {})[c];
        if (v == null) return `<td><span class="se-ret neutral">—</span></td>`;
        return `<td><span class="se-ret ${retTone(v)} jump" title="打开 K 线，标记信号日 → +${c}日区间"
          data-jump="${esc((s.symbol_key || "").slice(2))}" data-jname="${esc(s.symbol_name || "")}"
          data-jfrom="${(s.signal_time || "").slice(0, 10)}" data-jto="+${c}D">${pct(v)}</span></td>`;
      };
      // 决策三态：buy / sell / hold（hold = 无方向，不再误渲染成 sell 卖出）
      const statusChip = (s) => {
        if (s.decision === "hold") return '<span class="se-chip hold">无方向</span>';
        if (s.decision === "buy") {
          return s.paired ? '<span class="se-chip flat">已了结</span>' : '<span class="se-chip holding">持仓中</span>';
        }
        return '<span class="se-chip flat">已了结</span>';
      };

      let html = this._sampleHeaderHtml() + "<tbody>";
      pageRows.forEach((s) => {
        const code = esc((s.symbol_key || "").slice(2));
        const open = this.sampleOpenId === s.id;
        html += `<tr class="sample-row ${open ? "open" : ""}" data-id="${s.id}">
          <td><span class="se-sym jump" title="打开 ${esc(s.symbol_name || s.symbol_key)} K 线"
            data-jump="${code}" data-jname="${esc(s.symbol_name || "")}">
            ${esc(s.symbol_name || s.symbol_key)}<span class="code">${code}</span></span></td>
          <td>${decisionChip(s.decision)}</td>
          <td class="num">${(s.signal_time || "").slice(0, 10)}</td>
          <td class="num">${s.entry_price != null ? s.entry_price.toFixed(2) : "—"}</td>
          <td>${confChip(s.confidence)}</td>
          ${CYCLES.map((c) => cycCell(s, c)).join("")}
          <td><span class="se-ret ${retTone(s.returns?.now)}">${pct(s.returns?.now)}</span></td>
          <td><span class="se-ret ${retTone(s.peak)}">${pct(s.peak)}</span></td>
          <td><span class="se-ret ${retTone(s.maxDD)}">${pct(s.maxDD)}</span></td>
          <td>${statusChip(s)}</td>
          <td><span class="se-ops">
            <button type="button" class="se-fb-btn" data-rec="${s.id}" data-name="${esc(s.symbol_name || "")}"
              data-price="${s.entry_price != null ? s.entry_price : ""}" title="按信号成本价记入持仓账本">记持仓</button>
            <button type="button" class="se-fb-btn" data-jump="${code}" data-jname="${esc(s.symbol_name || "")}"
              data-jfrom="${(s.signal_time || "").slice(0, 10)}" title="打开 K 线并标记信号日">K 线</button>
            <button type="button" class="se-fb-btn" data-detail="${s.id}">${open ? "收起" : "详情"}</button>
          </span></td></tr>`;
        if (open) html += `<tr class="se-detail-tr"><td colspan="15">${this._eventDetailHtml(s)}</td></tr>`;
      });
      html += "</tbody>";
      el.innerHTML = total
        ? html
        : `<tbody><tr><td colspan="15" class="se-empty">当前筛选下暂无信号样本</td></tr></tbody>`;

      $$("#seSamples th.se-th-sort").forEach((th) => th.addEventListener("click", () => {
        const k = th.dataset.sk;
        if (this.sampleSortKey === k) this.sampleSortDir = this.sampleSortDir === "asc" ? "desc" : "asc";
        else { this.sampleSortKey = k; this.sampleSortDir = "desc"; }
        this.samplePage = 1;
        this.sampleOpenId = null;
        this.renderSamples();
      }));

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
        row.addEventListener("click", (ev) => {
          if (ev.target.closest("button, [data-jump]")) return;
          const id = +row.dataset.id;
          this.sampleOpenId = this.sampleOpenId === id ? null : id;
          this.renderSamples();
        });
      });
      el.querySelectorAll("button[data-detail]").forEach((btn) => {
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const id = +btn.dataset.detail;
          this.sampleOpenId = this.sampleOpenId === id ? null : id;
          this.renderSamples();
        });
      });
      el.querySelectorAll(".se-detail .close-x").forEach((x) => {
        x.addEventListener("click", (ev) => {
          ev.stopPropagation();
          this.sampleOpenId = null;
          this.renderSamples();
        });
      });

      this.updateFilterInfo();
      const sub = $("#seSamplesSub");
      if (sub) {
        const label = FILTER_LABEL[this.filter] || this.filter;
        sub.textContent = `${label} ${total} 条 · 第 ${this.samplePage}/${pages} 页 · 点行展开详情（含通道①②③与同标的 TOP）`;
      }
      this.renderSamplesPagination(total, pages, pageSize);
    },

    renderSamplesPagination(total, pages, pageSize) {
      const el = $("#seSamplesPag");
      if (!el) return;
      const nums = [];
      const maxBtns = 7;
      let a = Math.max(1, this.samplePage - 3), b = Math.min(pages, a + maxBtns - 1);
      a = Math.max(1, b - maxBtns + 1);
      for (let i = a; i <= b; i++) nums.push(i);
      const sizeOn = (sz) => (sz === "all" ? this.sampleSize === "all" : this.sampleSize === sz);
      el.innerHTML = `
        <span class="se-pag-info">共 ${total} 条 · 第 ${this.samplePage} / ${pages} 页 · 每页 ${this.sampleSize === "all" ? "全部" : pageSize} 条</span>
        <span class="se-pag-btns">
          <button type="button" class="se-pgbtn" data-p="prev" ${this.samplePage <= 1 ? "disabled" : ""}>‹ 上一页</button>
          ${nums.map((i) => `<button type="button" class="se-pgbtn ${i === this.samplePage ? "on" : ""}" data-p="${i}">${i}</button>`).join("")}
          <button type="button" class="se-pgbtn" data-p="next" ${this.samplePage >= pages ? "disabled" : ""}>下一页 ›</button>
        </span>
        <span class="se-pag-size"><span class="lbl">每页</span>
          ${SAMPLE_SIZES.map((sz) => `<button type="button" class="se-pgbtn ${sizeOn(sz) ? "on" : ""}" data-sz="${sz}">${sz === "all" ? "全部" : sz}</button>`).join("")}
        </span>`;
      el.querySelectorAll("button[data-p]").forEach((btn) => btn.addEventListener("click", () => {
        const v = btn.dataset.p;
        if (v === "prev") this.samplePage = Math.max(1, this.samplePage - 1);
        else if (v === "next") this.samplePage = Math.min(pages, this.samplePage + 1);
        else this.samplePage = +v;
        this.sampleOpenId = null;
        this.renderSamples();
      }));
      el.querySelectorAll("button[data-sz]").forEach((btn) => btn.addEventListener("click", () => {
        this.sampleSize = btn.dataset.sz === "all" ? "all" : +btn.dataset.sz;
        this.samplePage = 1;
        this.sampleOpenId = null;
        this.renderSamples();
      }));
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
        const decCls = DECISION_CLS[s.decision] || "hold";
        tl += `<div class="se-tl-node ${decCls}">
            <div class="dot"></div>
            <div class="date">${(s.signal_time || "").slice(0, 10)}</div>
            <div class="act">${DECISION_TXT[s.decision] || esc(s.decision || "—")}</div>
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
            `已实现 ${pct(lastPaired.paired_ret)}${lastPaired.paired_sell_date ? " · 卖出 " + lastPaired.paired_sell_date : ""}`,
            retTone(lastPaired.paired_ret))
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
            retTone(lastBuy.returns.now))
          : chan("通道③ 持仓 buy", null, isHolding ? "持仓中 · 收益数据不足" : "已平仓", ""))
        : chan("通道③ 持仓 buy", null, "暂无 buy", "");

      const ranked = symSigs.filter((s) => s.returns?.now != null).sort((a, b) => b.returns.now - a.returns.now);
      const profitTop = ranked.filter((s) => s.returns.now > 0).slice(0, 5);
      const lossTop = ranked.filter((s) => s.returns.now < 0).slice(0, 5);
      const sigRow = (x) => `
        <div class="se-detail-row">
          ${decisionChip(x.decision)}
          <span class="se-detail-date">${(x.signal_time || "").slice(0, 10)}</span>
          <span class="se-ret ${retTone(x.returns?.now)}">${pct(x.returns?.now)}</span>
        </div>`;

      return `<div class="se-detail">
        <div class="se-detail-title">${name}
          <span class="se-detail-code">${code}</span>
          <span class="se-chip flat">同标的 ${symSigs.length} 条信号</span>
          <button type="button" class="se-btn primary" data-jump="${code}" data-jname="${name}">打开 K 线图</button>
          <span class="close-x" role="button" tabindex="0" aria-label="收起详情">✕</span>
        </div>
        <div class="se-timeline">${tl}</div>
        <div class="se-chan-grid">${chanPaired}${chanSell}${chanBuy}</div>
        ${(profitTop.length || lossTop.length) ? `
        <div class="se-detail-cols">
          <div><div class="se-detail-col-h">利润信号 TOP${profitTop.length || "—"}</div>
            ${profitTop.length ? profitTop.map(sigRow).join("") : '<div class="se-muted">无盈利信号</div>'}</div>
          <div><div class="se-detail-col-h">亏损信号 TOP${lossTop.length || "—"}</div>
            ${lossTop.length ? lossTop.map(sigRow).join("") : '<div class="se-muted">无亏损信号</div>'}</div>
        </div>` : ""}
      </div>`;
    },

    /* ══════════ 基准对比 alpha ══════════ */
    renderBench() {
      const el = $("#seBench");
      if (!el) return;
      const rows = ((this._data.signals || {}).items || []).filter((s) => s.alpha != null);
      let html = `<thead><tr><th>标的</th><th>信号</th><th>信号日</th><th>至今</th><th>沪深300</th><th>alpha</th><th>跑赢</th></tr></thead><tbody>`;
      rows.slice(0, 30).forEach((s) => {
        const code = esc((s.symbol_key || "").slice(2));
        html += `<tr title="基准代码 ${esc(s.bench_code || "—")}">
          <td><span class="se-sym jump" data-jump="${code}" data-jname="${esc(s.symbol_name || "")}">${esc(s.symbol_name || s.symbol_key)}<span class="code">${code}</span></span></td>
          <td>${decisionChip(s.decision)}</td>
          <td class="num">${(s.signal_time || "").slice(0, 10)}</td>
          <td><span class="se-ret ${retTone(s.returns?.now)}">${pct(s.returns?.now)}</span></td>
          <td><span class="se-ret ${retTone(s.bench)}">${pct(s.bench)}</span></td>
          <td><span class="se-ret ${retTone(s.alpha)}">${pct(s.alpha)}</span></td>
          <td>${s.alpha > 0 ? '<span class="se-chip buy">跑赢</span>' : '<span class="se-chip sell">跑输</span>'}</td></tr>`;
      });
      html += "</tbody>";
      el.innerHTML = rows.length
        ? html
        : `<tbody><tr><td colspan="7" class="se-empty">暂无 alpha 数据</td></tr></tbody>`;

      const sum = $("#seBenchSummary");
      if (!sum) return;
      const alphas = rows.map((s) => s.alpha);
      if (!alphas.length) { sum.innerHTML = ""; return; }
      const avg = alphas.reduce((a, b) => a + b, 0) / alphas.length;
      const beat = alphas.filter((a) => a > 0).length;
      sum.innerHTML = `<div class="se-bench-sum">
        <span>平均 alpha <b class="${retTone(avg)}">${pct(avg)}</b></span>
        <span>跑赢基准 <b>${beat}/${alphas.length}</b></span>
        <span>跑赢占比 <b>${Math.round((beat / alphas.length) * 100)}%</b></span>
        <span>样本 <b>${alphas.length}</b> 条</span>
        <span class="se-bench-note">alpha = 至今收益（费后）− 同期基准（指数不扣费）</span>
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
      const total = data.total;
      const items = data.items || [];
      const pages = Math.max(1, Math.ceil(items.length / this.unrecSize));
      if (this.unrecPage > pages) this.unrecPage = pages;
      const start = (this.unrecPage - 1) * this.unrecSize;
      const rows = items.slice(start, start + this.unrecSize);
      let html = `<div class="se-tbl-scroll"><table><thead><tr><th>时间</th><th>决策</th><th>对话上下文</th><th>核心结论</th><th>标注</th></tr></thead><tbody>`;
      rows.forEach((s) => {
        const ctx = esc(s.context || "");
        const verdict = esc((s.core_verdict || "").slice(0, 40));
        html += `<tr data-id="${s.id}">
          <td class="num se-unrec-time">${(s.signal_time || "").slice(0, 16).replace("T", " ")}</td>
          <td>${decisionChip(s.decision)}</td>
          <td class="se-unrec-ctx" title="${ctx}">${ctx || "—"}</td>
          <td class="se-unrec-vd" title="${verdict}">${verdict || "—"}</td>
          <td class="se-mark-cell">
            <input class="se-mark-input" placeholder="代码，如 600519" maxlength="6" inputmode="numeric" aria-label="补标代码">
            <button type="button" class="se-fb-btn se-mark-btn" data-act="mark">补标</button>
            <button type="button" class="se-fb-btn se-mark-btn" data-act="mw" title="大盘/板块类决策，不计入胜率统计">大盘类</button>
          </td></tr>`;
      });
      html += "</tbody></table></div>";
      if (pages > 1) {
        const nums = [];
        for (let i = 1; i <= pages; i++) nums.push(i);
        html += `<div class="se-pag se-unrec-pag">
          <span class="se-pag-info">共 ${total} 条 · 第 ${this.unrecPage} / ${pages} 页 · 每页 ${this.unrecSize} 条</span>
          <span class="se-pag-btns">
            <button type="button" class="se-pgbtn" data-up="prev" ${this.unrecPage <= 1 ? "disabled" : ""}>‹</button>
            ${nums.map((i) => `<button type="button" class="se-pgbtn ${i === this.unrecPage ? "on" : ""}" data-up="${i}">${i}</button>`).join("")}
            <button type="button" class="se-pgbtn" data-up="next" ${this.unrecPage >= pages ? "disabled" : ""}>›</button>
          </span>
        </div>`;
      }
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
      el.querySelectorAll(".se-pgbtn[data-up]").forEach((btn) => btn.addEventListener("click", () => {
        const v = btn.dataset.up;
        if (v === "prev") this.unrecPage = Math.max(1, this.unrecPage - 1);
        else if (v === "next") this.unrecPage = Math.min(pages, this.unrecPage + 1);
        else this.unrecPage = +v;
        this.renderUnrecognized();
      }));
    },

    /* ══════════ 条件单（不计入主胜率） ══════════ */
    renderConditional() {
      const d = this._data.conditional || {};
      const el = $("#seConditional");
      if (!el) return;
      const items = d.items || [];
      const rate = d.trigger_rate == null ? "—" : Math.round(d.trigger_rate * 100) + "%";
      const st = {
        triggered: "已触发", not_triggered: "未触发", pending: "待定", unverifiable: "不可判定",
      };
      const entryText = (s) => {
        const lo = s.entry_low, hi = s.entry_high;
        if (s.entry_type === "unverifiable") return "条件不可量化";
        if (s.entry_type === "breakout") return `涨破 ${lo != null ? lo : hi}`;
        if (lo != null && hi != null) return `回踩 ${lo}–${hi}`;
        return `回踩 ≤${hi != null ? hi : lo}`;
      };
      const pending = d.pending || 0;
      const unverifiable = Math.max(0, (d.count || 0) - (d.triggered || 0)
        - (d.not_triggered || 0) - pending);
      const summary = `
        <div class="se-stats">
          <div class="se-stat" style="cursor:default"><span class="k">条件单</span><span class="v">${d.count || 0}</span></div>
          <div class="se-stat" style="cursor:default"><span class="k">触发率</span><span class="v">${rate}</span></div>
          <div class="se-stat" style="cursor:default"><span class="k">已触发</span><span class="v">${d.triggered || 0}</span></div>
          <div class="se-stat" style="cursor:default"><span class="k">未触发</span><span class="v">${d.not_triggered || 0}</span></div>
          <div class="se-stat" style="cursor:default"><span class="k">待定</span><span class="v">${pending}</span></div>
          <div class="se-stat" style="cursor:default"><span class="k">不可判定</span><span class="v">${unverifiable}</span></div>
        </div>`;
      if (!items.length) {
        el.innerHTML = summary + '<div class="se-empty">暂无条件单</div>';
        return;
      }
      const rows = items.map((s) => `
        <tr class="se-cond-row">
          <td>${esc(s.group_key || "—")}</td>
          <td>${esc(s.symbol_key || "")}</td>
          <td>${esc(s.signal_time ? s.signal_time.slice(0, 10) : "")}</td>
          <td>${esc(entryText(s))}</td>
          <td>${esc(st[s.trigger_status] || s.trigger_status || "—")}</td>
          <td>${s.days_to_trigger == null ? "—" : s.days_to_trigger}</td>
        </tr>`).join("");
      el.innerHTML = summary + `
        <div class="se-tbl-scroll">
          <table class="se-samples se-cond">
            <thead><tr><th>方向</th><th>对象</th><th>信号日</th><th>条件</th><th>状态</th><th>天数</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <p class="se-cond-note"><b>不计入胜率（生产同口径）。</b>条件单没有可执行入场价，触发判定只对价格条件生效；
          触发后收益后端暂无字段，旧功能与入口保留，补齐后可按触发日回填并与无条件入场比较。</p>`;
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
     用捕获阶段，保证 stopPropagation 早于行展开监听器生效 */
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
