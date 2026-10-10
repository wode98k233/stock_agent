/*
   A7 质量观测面板 —— 三库质量数据统一视图（镜像 portfolio.js 弹窗模式 + 原型视图 4）
   结构：4 指标卡（北极星高亮）→ 周趋势柱状（W## 标签）→ 覆盖率条 → 模式×场景矩阵
        → 解读区（data/quality_notes.json，手工维护）
   数据：GET /api/quality/overview · /matrix · /notes（{code,data,msg} 壳）
   降级：字段 null → 「暂无数据」（AC-2）；单元格 n<5 → 灰底无色阶 + 「n小」角标（AC-3）
*/
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);

  const MODE_LABELS = {
    unified_plan: "Unified Plan",
    react_stock: "ReAct",
    agent_group: "Agent Group",
    pdor: "PDoR",
  };
  const SCENARIO_ORDER = ["个股", "板块", "大盘", "筛选", "热点", "未知"];
  const COLOR_CLASS = { good: "qy-cell-good", mid: "qy-cell-mid", bad: "qy-cell-bad", na: "qy-cell-na" };

  const QualityPanel = class QualityPanel {
    constructor() {
      this._data = { overview: null, matrix: null, notes: null };
      this.days = 90;
      this._buildDom();
    }

    /* ── DOM 构建（弹窗自建，index.html 只挂脚本） ── */
    _buildDom() {
      this.overlayEl = document.createElement("div");
      this.overlayEl.className = "qy-overlay";
      this.modalEl = document.createElement("div");
      this.modalEl.className = "qy-modal";
      this.modalEl.setAttribute("role", "dialog");
      this.modalEl.setAttribute("aria-modal", "true");
      this.modalEl.setAttribute("aria-label", "质量面板");
      this.modalEl.innerHTML = `
        <div class="qy-header">
          <div class="qy-title">质量面板 <span class="qy-sub">Agent 质量观测 · 三库只读聚合</span></div>
          <div class="qy-header-actions">
            <span class="qy-seg" id="qyDaysSeg" title="矩阵统计窗口">
              <button class="qy-seg-btn" data-days="30">30天</button>
              <button class="qy-seg-btn active" data-days="90">90天</button>
              <button class="qy-seg-btn" data-days="180">180天</button>
            </span>
            <button id="qyRefreshBtn" class="qy-refresh-btn" type="button">↻ 刷新</button>
            <button id="qyCloseBtn" class="qy-close-btn" type="button" aria-label="关闭">×</button>
          </div>
        </div>
        <div id="qyBody" class="qy-body"><div class="qy-empty">加载中…</div></div>`;
      document.body.appendChild(this.overlayEl);
      document.body.appendChild(this.modalEl);

      $("#qyCloseBtn", this.modalEl).onclick = () => this.close();
      this.overlayEl.onclick = () => this.close();
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && this.modalEl.classList.contains("open")) this.close();
      });
      $("#qyRefreshBtn", this.modalEl).onclick = () => this.load();
      $("#qyDaysSeg", this.modalEl).addEventListener("click", (e) => {
        const btn = e.target.closest(".qy-seg-btn");
        if (!btn || +btn.dataset.days === this.days) return;
        this.days = +btn.dataset.days;
        $("#qyDaysSeg", this.modalEl).querySelectorAll(".qy-seg-btn").forEach((b) =>
          b.classList.toggle("active", b === btn));
        this.load();
      });
    }

    open() {
      this.overlayEl.classList.add("open");
      this.modalEl.classList.add("open");
      this.load();
    }

    close() {
      this.overlayEl.classList.remove("open");
      this.modalEl.classList.remove("open");
    }

    /* ── 数据加载 ── */
    async load() {
      const body = $("#qyBody", this.modalEl);
      body.innerHTML = '<div class="qy-empty">加载中…</div>';
      const get = (url) => window.StockRadar.api.fetchJSON(url).then((d) => d.data);
      try {
        const [overview, matrix, notes] = await Promise.all([
          get("/api/quality/overview"),
          get(`/api/quality/matrix?dim=mode_scenario&days=${this.days}`),
          get("/api/quality/notes").catch(() => ({ exists: false, notes: [] })),
        ]);
        this._data = { overview, matrix, notes };
        this.render();
      } catch (e) {
        body.innerHTML = `<div class="qy-empty">加载失败：${this._esc(e.message || e)}<br><br>
          <button class="qy-btn" onclick="document.getElementById('qyRefreshBtn').click()">重试</button></div>`;
      }
    }

    /* ── 渲染 ── */
    render() {
      const body = $("#qyBody", this.modalEl);
      body.innerHTML = `
        <div class="qy-stat-grid" id="qyCards"></div>
        <div class="qy-section">
          <div class="qy-section-title">北极星 · 每周可验证结论产出数
            <span class="qy-tag" id="qyBasisTag"></span></div>
          <div class="qy-trend" id="qyTrend"></div>
        </div>
        <div class="qy-section">
          <div class="qy-section-title">模式 × 场景矩阵
            <span class="qy-tag">绿=优 黄=中 红=差 · n&lt;5 灰底（样本不足不出结论）</span></div>
          <div class="qy-coverage" id="qyCoverage"></div>
          <div class="qy-mx-wrap"><table class="qy-mx" id="qyMatrix"></table></div>
        </div>
        <div class="qy-section">
          <div class="qy-section-title">📌 决策建议（复盘解读区 · 手工维护）
            <span class="qy-tag">data/quality_notes.json</span></div>
          <div id="qyNotes"></div>
        </div>`;
      this.renderCards();
      this.renderTrend();
      this.renderCoverage();
      this.renderMatrix();
      this.renderNotes();
    }

    renderCards() {
      const d = this._data.overview || {};
      const na = '<span class="qy-na">暂无数据</span>';
      const vc = d.verifiable_conclusions;
      const basisText = d.verifiable_basis === "with_confidence" ? "带置信度且入跟踪" : "入跟踪结论数（置信度待 A2）";
      const wow = d.wow_pct == null ? "" :
        `上周 ${d.prev_week_count ?? "—"} · <span class="${d.wow_pct >= 0 ? "qy-up" : "qy-down"}">${d.wow_pct >= 0 ? "+" : ""}${d.wow_pct}%</span>`;
      const wr = d.win_rate_after_cost || {};
      const cost = d.avg_cost_per_report || {};
      const costVal = cost.value == null ? na
        : `${Number(cost.value).toLocaleString("zh-CN")} <span class="qy-unit">${this._esc(cost.unit || "tokens")}</span>`;
      const cards = [
        { star: true, label: "本周可验证结论（北极星）", value: vc == null ? na : String(vc), hint: wow || basisText },
        { label: "buy 类信号实现胜率（费后）", value: (wr && wr.value) == null ? na : (wr.value * 100).toFixed(1) + "%",
          hint: wr && wr.n ? `样本 n=${wr.n} · ${this._esc(wr.coverage || "")}` : "暂无 buy 类跟踪样本" },
        { label: "单报告平均成本", value: costVal,
          hint: cost.n ? `token 口径估算 · 近 28 天 n=${cost.n}` : "暂无 trace 成本数据" },
        { label: "语义缓存命中率", value: d.cache_hit_rate == null ? na : (d.cache_hit_rate * 100).toFixed(1) + "%",
          hint: d.cache_hit_rate == null ? "A8 语义缓存未开启（LLM_CACHE_ENABLED）" : "进程内计数 · 重启清零" },
      ];
      $("#qyCards").innerHTML = cards.map((c) => `
        <div class="qy-stat-card${c.star ? " star" : ""}">
          <div class="qy-stat-label">${c.label}</div>
          <div class="qy-stat-value">${c.value}</div>
          <div class="qy-stat-hint">${c.hint || ""}</div>
        </div>`).join("");
      const basisTag = $("#qyBasisTag");
      if (basisTag) basisTag.textContent = `口径：${basisText} · 周边界按北京时间`;
    }

    renderTrend() {
      const trend = (this._data.overview || {}).trend || [];
      const el = $("#qyTrend");
      if (!trend.length) {
        el.innerHTML = '<div class="qy-empty">暂无趋势数据（signal 跟踪库缺失或为空）</div>';
        return;
      }
      const max = Math.max(1, ...trend.map((t) => t.count || 0));
      el.innerHTML = trend.map((t) => `
        <div class="qy-t-col" title="${this._esc(t.week)}（${this._esc(t.start)} ~ ${this._esc(t.end)}，北京时间周）">
          <div class="qy-t-val">${t.count}</div>
          <div class="qy-t-bar" style="height:${Math.max(3, Math.round((t.count / max) * 96))}px"></div>
          <div class="qy-t-lab">${this._esc(t.week)}</div>
        </div>`).join("");
    }

    renderCoverage() {
      const m = this._data.matrix || {};
      const cov = m.coverage || {};
      const src = m.scenario_source || {};
      const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
      $("#qyCoverage").innerHTML =
        `胜率覆盖 ${pct(cov.win_rate)} · 评估覆盖 ${pct(cov.judge_score)} · 场景识别 ${pct(cov.scenario)}` +
        (src.dashboard_id != null
          ? ` <span class="qy-coverage-src">（dashboard_id ${src.dashboard_id} / 模板 ${src.template_id} / 签名 ${src.key_signature} / 未知 ${src.unknown}）</span>`
          : "") +
        (m.note ? `<div class="qy-coverage-note">${this._esc(m.note)}</div>` : "");
    }

    renderMatrix() {
      const m = this._data.matrix || {};
      const cells = m.cells || [];
      const el = $("#qyMatrix");
      if (!cells.length) {
        el.innerHTML = '<tr><td class="qy-empty">暂无矩阵数据（消息库缺失或窗口内无 assistant 报告）</td></tr>';
        return;
      }
      const modes = [...new Set(cells.map((c) => c.mode))]
        .sort((a, b) => (Object.keys(MODE_LABELS).indexOf(a) + 99) % 99 - (Object.keys(MODE_LABELS).indexOf(b) + 99) % 99);
      const scenarios = SCENARIO_ORDER.filter((s) => cells.some((c) => c.scenario === s))
        .concat(cells.map((c) => c.scenario).filter((s) => !SCENARIO_ORDER.includes(s)));
      const cellOf = (mode, sc) => cells.find((c) => c.mode === mode && c.scenario === sc);

      let html = `<thead><tr><th class="qy-rowh">模式 \\ 场景</th>` +
        scenarios.map((s) => `<th>${this._esc(s)}</th>`).join("") + "</tr></thead><tbody>";
      modes.forEach((mode) => {
        html += `<tr><td class="qy-rowh">${this._esc(MODE_LABELS[mode] || mode)}</td>`;
        scenarios.forEach((sc) => {
          const c = cellOf(mode, sc);
          if (!c) {
            html += '<td class="qy-cell-na">—</td>';
            return;
          }
          const small = c.n < 5;
          const cls = COLOR_CLASS[c.color || "na"] || "qy-cell-na";
          const main = c.win_rate != null ? (c.win_rate * 100).toFixed(0) + "%"
            : c.judge_avg != null ? "分 " + c.judge_avg.toFixed(1) : "—";
          const sub = [
            `n=${c.n}${small ? ' <span class="qy-nsmall">n小</span>' : ""}`,
            c.avg_cost != null ? Number(c.avg_cost).toLocaleString("zh-CN") + " tk" : null,
            c.avg_latency_s != null ? c.avg_latency_s + "s" : null,
          ].filter(Boolean).join(" · ");
          html += `<td class="${cls}" title="来源：web_messages(mode×scenario) · signal(dialog_uuid) · eval_cases(trace_run_id) · steps(token_usage)">
            <div class="qy-cell-main">${main}</div><div class="qy-cell-sub">${sub}</div></td>`;
        });
        html += "</tr>";
      });
      html += "</tbody>";
      el.innerHTML = html;
    }

    renderNotes() {
      const notes = (this._data.notes || {}).notes || [];
      const el = $("#qyNotes");
      if (!notes.length) {
        el.innerHTML = `<div class="qy-empty">暂无复盘结论。每周复盘后把决策建议维护到
          <code>data/quality_notes.json</code>（格式：{"notes":[{"date":"YYYY-MM-DD","title":"…","body":"…"}]}），
          刷新本页即可展示。</div>`;
        return;
      }
      el.innerHTML = notes.map((n) => `
        <div class="qy-note">
          <div class="qy-note-head"><b>${this._esc(n.title || "复盘结论")}</b>
            <span class="qy-note-date">${this._esc(n.date || "")}</span></div>
          <div class="qy-note-body">${this._esc(n.body || "")}</div>
        </div>`).join("");
    }

    _esc(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[c]));
    }
  };

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.QualityPanel = QualityPanel;
})();
