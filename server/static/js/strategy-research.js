/*
   A3 策略研发面板 —— 想法 → OOS 验证策略（镜像 QualityPanel 弹窗模式 + 原型视图 2）
   结构：发起表单 → 轮次表（SSE 逐行出现，点击看假设/diff/IS/OOS/判定）→ 最优策略卡
        （OOS 大字 + IS 小字 + 过拟合结论 + 免责声明）→ 三动作（save/monitor/track）
   数据：POST /api/strategy-research/run · GET runs · GET runs/{uuid} · GET runs/{uuid}/iters
        GET runs/{uuid}/events (SSE) · POST cancel · POST adopt（{code,data,msg} 壳）
   入口：侧边栏「策略研究」按钮（index.html 内联 onclick，不依赖 app.js）
*/
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const API = "/api/strategy-research";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function pct(v) {
    if (v == null || isNaN(v)) return "—";
    return (v * 100).toFixed(2) + "%";
  }
  function num(v) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toFixed(2);
  }
  function metricCls(v) { return v != null && !isNaN(v) ? (v >= 0 ? "sr-pos" : "sr-neg") : ""; }

  const StrategyResearchPanel = class StrategyResearchPanel {
    constructor() {
      this.currentRun = null;      // 当前查看的 run_uuid
      this.es = null;              // EventSource
      this._buildDom();
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
      this.modalEl.innerHTML = `
        <div class="sr-header">
          <div class="sr-title">策略研究 <span class="sr-sub">想法 → OOS 验证策略 · 历史表现不代表未来收益</span></div>
          <button class="sr-close-btn" type="button" aria-label="关闭">×</button>
        </div>
        <div class="sr-body">
          <div class="sr-form">
            <div style="grid-column: span 1;">
              <label>研究想法（自然语言）</label>
              <textarea id="srIdea" placeholder="例：DPO 底部上穿 MADPO + KDJ 低位共振时买入"></textarea>
            </div>
            <div>
              <label>标的代码</label>
              <input id="srUniverse" value="600519" placeholder="600519" />
            </div>
            <div>
              <label>开始日期</label>
              <input id="srPeriodStart" type="date" />
            </div>
            <div>
              <label>结束日期</label>
              <input id="srPeriodEnd" type="date" />
            </div>
            <button id="srStartBtn" class="sr-start-btn" type="button">发起研究</button>
          </div>
          <div id="srRunningBar" class="sr-running-bar" style="display:none;">
            <span class="sr-spinner"></span><span id="srRunningText">研究中…</span>
            <button id="srCancelBtn" class="sr-action-btn" type="button">取消</button>
          </div>
          <div class="sr-section">
            <div class="sr-section-title">迭代轮次 <span class="sr-tag">SSE 实时逐行推送 · 点击行看详情</span></div>
            <div class="sr-iter-wrap">
              <table class="sr-iter-table">
                <thead><tr>
                  <th>轮次</th><th>假设</th><th>IS 夏普</th><th>OOS 夏普</th>
                  <th>OOS 年化</th><th>OOS 回撤</th><th>OOS 笔数</th><th>判定</th>
                </tr></thead>
                <tbody id="srIterBody"><tr><td colspan="8" class="sr-empty">发起研究或从下方历史任务选择</td></tr></tbody>
              </table>
            </div>
            <div id="srIterDetail"></div>
          </div>
          <div class="sr-section">
            <div class="sr-section-title">最优策略卡 <span class="sr-tag">指标为 OOS 口径（大字），括号内为 IS 口径</span></div>
            <div id="srBestCard"><div class="sr-empty">暂无最优策略</div></div>
          </div>
          <div class="sr-section">
            <div class="sr-section-title">历史任务 <span class="sr-tag">点击载入轮次与最优卡</span></div>
            <div class="sr-iter-wrap">
              <table class="sr-runs-table">
                <thead><tr><th>任务</th><th>标的</th><th>区间</th><th>状态</th><th>轮数</th><th>最优 OOS 夏普</th><th>创建时间</th></tr></thead>
                <tbody id="srRunsBody"><tr><td colspan="7" class="sr-empty">加载中…</td></tr></tbody>
              </table>
            </div>
          </div>
        </div>`;
      document.body.appendChild(this.overlayEl);
      document.body.appendChild(this.modalEl);

      // 默认区间：近 3 年
      const end = new Date();
      const start = new Date(end.getTime() - 365 * 3 * 86400 * 1000);
      const fmt = (d) => d.toISOString().slice(0, 10);
      $("#srPeriodStart", this.modalEl).value = fmt(start);
      $("#srPeriodEnd", this.modalEl).value = fmt(end);

      $(".sr-close-btn", this.modalEl).onclick = () => this.close();
      this.overlayEl.onclick = () => this.close();
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && this.modalEl.classList.contains("open")) this.close();
      });
      $("#srStartBtn", this.modalEl).onclick = () => this.startRun();
      $("#srCancelBtn", this.modalEl).onclick = () => this.cancelRun();
    }

    open() {
      this.overlayEl.classList.add("open");
      this.modalEl.classList.add("open");
      this.loadRuns();
    }

    close() {
      this.overlayEl.classList.remove("open");
      this.modalEl.classList.remove("open");
      this._closeSse();
    }

    _fetch(url, options) {
      return window.StockRadar.api.fetchJSON(url, options);
    }

    /* ── 发起研究 ── */
    async startRun() {
      const idea = $("#srIdea", this.modalEl).value.trim();
      const universe = $("#srUniverse", this.modalEl).value.trim();
      const periodStart = $("#srPeriodStart", this.modalEl).value;
      const periodEnd = $("#srPeriodEnd", this.modalEl).value;
      const btn = $("#srStartBtn", this.modalEl);
      if (!idea || idea.length < 4) { alert("请先填写研究想法（至少 4 个字）"); return; }
      btn.disabled = true;
      try {
        const res = await this._fetch(`${API}/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ idea, universe, period_start: periodStart, period_end: periodEnd, source: "panel" }),
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
        const res = await this._fetch(`${API}/runs/${this.currentRun}/cancel`, { method: "POST" });
        if (res.code === 0) $("#srRunningText", this.modalEl).textContent = "已请求取消，等待当前轮次结束…";
        else alert(res.msg || "取消失败");
      } catch (e) { alert("取消失败：" + (e.message || e)); }
    }

    /* ── 历史任务 ── */
    async loadRuns() {
      const body = $("#srRunsBody", this.modalEl);
      try {
        const res = await this._fetch(`${API}/runs`);
        const runs = (res.data && res.data.runs) || [];
        if (!runs.length) {
          body.innerHTML = '<tr><td colspan="7" class="sr-empty">暂无历史任务</td></tr>';
          return;
        }
        body.innerHTML = runs.map((r) => {
          const oos = r.best_oos_metrics || {};
          return `<tr data-uuid="${esc(r.run_uuid)}">
            <td>${esc(r.run_uuid)}</td><td>${esc(r.universe)}</td><td>${esc(r.period)}</td>
            <td>${esc(r.status)}${r.stop_reason ? `(${esc(r.stop_reason)})` : ""}</td>
            <td>${r.total_iters ?? 0}</td>
            <td>${num(oos.sharpe)}</td>
            <td>${esc((r.created_at || "").slice(0, 19))}</td>
          </tr>`;
        }).join("");
        body.querySelectorAll("tr[data-uuid]").forEach((tr) => {
          tr.onclick = () => this.loadRun(tr.dataset.uuid, { subscribe: false });
        });
      } catch (e) {
        body.innerHTML = `<tr><td colspan="7" class="sr-empty">加载失败：${esc(e.message || e)}</td></tr>`;
      }
    }

    /* ── 载入任务：先 GET 轮次表补齐，再按需订阅 SSE ── */
    async loadRun(runUuid, opts) {
      this.currentRun = runUuid;
      this._closeSse();
      try {
        const res = await this._fetch(`${API}/runs/${encodeURIComponent(runUuid)}/iters`);
        const iters = (res.data && res.data.iters) || [];
        const status = (res.data && res.data.status) || "running";
        this._renderIters(iters, status);
        await this._renderBest(runUuid);
        this._setRunning(status === "running");
        if (opts && opts.subscribe && status === "running") this._subscribeSse(runUuid);
        if (status === "running" && !this.es) this._subscribeSse(runUuid); // 打开面板时也续订
      } catch (e) {
        $("#srIterBody", this.modalEl).innerHTML =
          `<tr><td colspan="8" class="sr-empty">加载失败：${esc(e.message || e)}</td></tr>`;
      }
    }

    /* ── SSE 订阅（轮次表逐行出现，AC-7） ── */
    _subscribeSse(runUuid) {
      this._closeSse();
      const es = new EventSource(`${API}/runs/${encodeURIComponent(runUuid)}/events`);
      this.es = es;
      this._iterRows = this._iterRows || {};
      es.addEventListener("strategy_iter_start", (ev) => {
        const d = JSON.parse(ev.data);
        this._setRunning(true);
        this._upsertRow({ round: d.round, hypothesis: d.hypothesis, running: true });
      });
      es.addEventListener("strategy_iter_done", (ev) => {
        const d = JSON.parse(ev.data);
        this._upsertRow(d);
        if (d.error) this._renderFailed(d);
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

    /* ── 轮次表渲染 ── */
    _renderIters(iters, status) {
      this._iterRows = {};
      const body = $("#srIterBody", this.modalEl);
      if (!iters.length) {
        body.innerHTML = '<tr><td colspan="8" class="sr-empty">暂无轮次</td></tr>';
        $("#srIterDetail", this.modalEl).innerHTML = "";
        return;
      }
      body.innerHTML = "";
      iters.forEach((it) => this._upsertRow(it));
      $("#srIterDetail", this.modalEl).innerHTML = "";
    }

    _upsertRow(it) {
      this._iterRows = this._iterRows || {};
      this._iterRows[it.round] = it;
      const body = $("#srIterBody", this.modalEl);
      let tr = body.querySelector(`tr[data-round="${it.round}"]`);
      const oos = it.oos_metrics || {};
      const isM = it.is_metrics || {};
      const oosSharpe = oos.sharpe != null && !isNaN(oos.sharpe) ? num(oos.sharpe) : "—";
      let verdictHtml;
      if (it.running) {
        verdictHtml = '<span class="sr-spinner"></span>';
      } else if (it.verdict === "best") {
        verdictHtml = '<span class="sr-badge sr-badge-best">最优</span>';
      } else if (it.overfit_flag) {
        verdictHtml = '<span class="sr-badge sr-badge-reject" title="IS↑OOS↓ 过拟合拒绝">过拟合</span>';
      } else if (it.verdict === "failed") {
        verdictHtml = '<span class="sr-badge sr-badge-failed">失败</span>';
      } else if (it.verdict === "keep" || it.verdict === "reject") {
        verdictHtml = '<span class="sr-badge sr-badge-keep">保留</span>';
      } else {
        verdictHtml = "—";
      }
      if (oos.trades != null && oos.trades < 5 && it.compile_ok) {
        verdictHtml += '<span class="sr-badge sr-badge-lowsample" title="OOS 样本笔数过少，不参与最优比较">n小</span>';
      }
      const hypo = it.hypothesis || (it.running ? "生成中…" : (it.error ? "（生成失败）" : "—"));
      const rowHtml = `
        <td>${it.round}</td>
        <td style="max-width:280px;white-space:normal;" title="${esc(it.hypothesis || "")}">${esc(hypo)}</td>
        <td class="${metricCls(isM.sharpe)}">${num(isM.sharpe)}</td>
        <td class="${metricCls(oos.sharpe)}">${oosSharpe}</td>
        <td class="${metricCls(oos.cagr)}">${pct(oos.cagr)}</td>
        <td class="${metricCls(-(oos.mdd || 0))}">${pct(oos.mdd)}</td>
        <td>${oos.trades ?? "—"}</td>
        <td>${verdictHtml}</td>`;
      if (!tr) {
        tr = document.createElement("tr");
        tr.dataset.round = it.round;
        body.appendChild(tr);
      }
      tr.classList.toggle("sr-failed-row", it.verdict === "failed");
      tr.innerHTML = rowHtml;
      tr.onclick = () => this._showIterDetail(it.round);
    }

    _showIterDetail(roundNo) {
      const it = (this._iterRows || {})[roundNo];
      if (!it) return;
      const body = $("#srIterBody", this.modalEl);
      body.querySelectorAll("tr").forEach((tr) => tr.classList.toggle("selected", +tr.dataset.round === roundNo));
      const isM = it.is_metrics || {};
      const oos = it.oos_metrics || {};
      const detail = $("#srIterDetail", this.modalEl);
      if (it.verdict === "failed") {
        detail.innerHTML = `
          <div class="sr-detail">
            <h4>第 ${it.round} 轮 · 生成失败</h4>
            <div class="sr-error">${esc(it.compile_error || it.error || "未知错误")}</div>
            <div class="sr-kv">建议：换一种更具体、可量化的想法描述重试（例如指明指标与参数方向）。</div>
          </div>`;
        return;
      }
      detail.innerHTML = `
        <div class="sr-detail">
          <h4>第 ${it.round} 轮 · ${esc(it.hypothesis || "")} ${it.model ? `· 模型 ${esc(it.model)}` : ""}</h4>
          <div class="sr-kv"><b>本轮改动：</b><span class="sr-diff">${esc(it.strategy_diff || "—")}</span></div>
          <div class="sr-kv"><b>IS（样本内）：</b>年化 ${pct(isM.cagr)} · 夏普 ${num(isM.sharpe)} · 回撤 ${pct(isM.mdd)} · 胜率 ${pct(isM.win_rate)} · ${isM.trades ?? "—"} 笔</div>
          <div class="sr-kv"><b>OOS（样本外）：</b>年化 ${pct(oos.cagr)} · 夏普 ${num(oos.sharpe)} · 回撤 ${pct(oos.mdd)} · 胜率 ${pct(oos.win_rate)} · ${oos.trades ?? "—"} 笔</div>
          ${it.overfit_flag ? '<div class="sr-error">IS/OOS 夏普比超 1.3 红线，存在过拟合迹象，该轮不得当选最优。</div>' : ""}
        </div>`;
    }

    _renderFailed(d) {
      const box = $("#srBestCard", this.modalEl);
      if (this._hasBest()) return; // 已有最优卡则不打断
      box.innerHTML = `
        <div class="sr-failed-box">
          策略生成失败：schema 约束过严或想法模糊。${esc(d.error || d.message || "")}
          <div style="margin-top:6px;">建议换一种更具体的想法描述（指明指标与买卖逻辑）重新发起。</div>
        </div>`;
    }

    _hasBest() {
      return !$("#srBestCard .sr-empty", this.modalEl);
    }

    /* ── 最优策略卡 ── */
    async _renderBest(runUuid) {
      const box = $("#srBestCard", this.modalEl);
      box.innerHTML = '<div class="sr-empty">加载中…</div>';
      try {
        const res = await this._fetch(`${API}/runs/${encodeURIComponent(runUuid)}`);
        const data = res.data || {};
        const card = data.best_card || { exists: false };
        if (!card.exists) {
          const run = data.run || {};
          if (run.status === "failed") {
            box.innerHTML = `
              <div class="sr-failed-box">策略生成失败：schema 约束过严或想法模糊。
                <div style="margin-top:6px;">建议换一种更具体的想法描述（指明指标与买卖逻辑）重新发起。</div>
              </div>`;
          } else {
            box.innerHTML = '<div class="sr-empty">暂无最优策略</div>';
          }
          return;
        }
        const oos = card.oos_metrics || {};
        const isM = card.is_metrics || {};
        box.innerHTML = `
          <div class="sr-card">
            <div class="sr-card-header">
              <div class="sr-card-name">第 ${card.round} 轮 · ${esc(card.strategy_name || "未命名策略")}</div>
              <div>${card.overfit_flag ? '<span class="sr-badge sr-badge-reject">过拟合迹象</span>' : '<span class="sr-badge sr-badge-best">OOS 验证通过</span>'}</div>
            </div>
            <div class="sr-card-hypo">${esc(card.hypothesis || "")}</div>
            <div class="sr-metric-row">
              <div class="sr-metric"><div class="sr-metric-label">OOS 年化收益</div>
                <div class="sr-metric-big ${metricCls(oos.cagr)}">${pct(oos.cagr)}</div>
                <div class="sr-metric-small">IS ${pct(isM.cagr)}</div></div>
              <div class="sr-metric"><div class="sr-metric-label">OOS 夏普</div>
                <div class="sr-metric-big ${metricCls(oos.sharpe)}">${num(oos.sharpe)}</div>
                <div class="sr-metric-small">IS ${num(isM.sharpe)}</div></div>
              <div class="sr-metric"><div class="sr-metric-label">OOS 最大回撤</div>
                <div class="sr-metric-big sr-neg">${pct(oos.mdd)}</div>
                <div class="sr-metric-small">IS ${pct(isM.mdd)}</div></div>
              <div class="sr-metric"><div class="sr-metric-label">OOS 胜率</div>
                <div class="sr-metric-big">${pct(oos.win_rate)}</div>
                <div class="sr-metric-small">IS ${pct(isM.win_rate)}</div></div>
              <div class="sr-metric"><div class="sr-metric-label">OOS 样本笔数</div>
                <div class="sr-metric-big">${oos.trades ?? "—"}${card.low_sample ? '<span class="sr-badge sr-badge-lowsample">n小</span>' : ""}</div>
                <div class="sr-metric-small">IS ${isM.trades ?? "—"} 笔</div></div>
            </div>
            <div class="sr-overfit-note ${card.overfit_flag ? "bad" : "ok"}">过拟合检验：${esc(card.overfit_note || "")}</div>
            <div class="sr-disclaimer">⚠️ ${esc(card.disclaimer || "历史表现不代表未来收益，本结果仅为研究结论，不构成投资建议")}</div>
            <div class="sr-actions">
              <button class="sr-action-btn" data-action="save" type="button">存入策略库</button>
              <button class="sr-action-btn" data-action="monitor" type="button">加入盯盘监控</button>
              <button class="sr-action-btn" data-action="track" type="button">标记信号入 signal_eval</button>
            </div>
          </div>`;
        box.querySelectorAll(".sr-action-btn").forEach((btn) => {
          btn.onclick = () => this._adopt(runUuid, btn.dataset.action, btn);
        });
      } catch (e) {
        box.innerHTML = `<div class="sr-empty">加载失败：${esc(e.message || e)}</div>`;
      }
    }

    /* ── 落地三动作 ── */
    async _adopt(runUuid, action, btn) {
      btn.disabled = true;
      try {
        const res = await this._fetch(`${API}/runs/${encodeURIComponent(runUuid)}/adopt`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action }),
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
