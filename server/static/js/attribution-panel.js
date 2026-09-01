/*
  选股雷达 Web — 回答溯源 · 执行归因
  替换旧 log-trace-panel.js（"关联检索 · 日志 × 链路"）。
  自动绑定当前对话，Tab 化展示概览/数据来源/工具调用/执行轨迹/原始日志，
  每条来源/工具/轨迹均可在对话中定位（双向高亮，保留 SSE 实时增量）。

  后端：GET /api/dialogs/{uuid}/runs  +  GET /api/dialogs/{uuid}/attribution?run_id=
  复用：window.StockRadar.api.{getRuns,getAttribution}
        window.StockRadar.chat.highlightMessage(uuid)
*/

window.StockRadar = window.StockRadar || {};

window.StockRadar.AttributionPanel = class AttributionPanel {
  constructor() {
    this.dialogUuid = null;
    this.runs = [];
    this.data = null;
    this.currentRunId = null;
    this.isOpen = false;
    this._liveTimer = null;

    this._("attrCloseBtn").onclick = () => this.close();
    this._("attrOverlay").onclick = () => this.close();
    this._("attrRunSelect").onchange = (e) => {
      const rid = e.target.value;
      if (rid) this.loadAttribution(rid);
    };
    this._("attrTabs").querySelectorAll(".ap-tab").forEach((btn) => {
      btn.onclick = () => this._switchTab(btn.dataset.tab);
    });
  }

  /* ── DOM 快捷 ── */
  _(id) { return document.getElementById(id); }
  _esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  _svg(inner) {
    return '<svg class="ap-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">' + inner + "</svg>";
  }
  _icon(name) {
    const M = {
      chart: '<polyline points="3 17 9 11 13 15 21 7"/><polyline points="3 21 21 21"/>',
      doc: '<path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>' +
           '<line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/>',
      fund: '<path d="M3 17l6-6 4 4 8-8"/><path d="M21 7v6h-6"/>',
      news: '<rect x="4" y="5" width="16" height="14" rx="2"/><line x1="8" y1="9" x2="16" y2="9"/><line x1="8" y1="13" x2="16" y2="13"/>',
      brain: '<path d="M12 5a3 3 0 0 0-3 3 3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3 3 3 0 0 0-3 3z"/>' +
             '<circle cx="12" cy="12" r="1.4"/>',
      locate: '<circle cx="12" cy="12" r="3"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/>' +
               '<line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/>',
      clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/>',
      tool: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4l-6 6 2 2 6-6a4 4 0 0 0 5.4-5.4l-2.3 2.3z"/>',
      check: '<polyline points="20 6 9 17 4 12"/>',
      warn: '<path d="M12 3l9 16H3z"/><line x1="12" y1="9" x2="12" y2="14"/><circle cx="12" cy="17" r=".6"/>',
      error: '<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="13"/><circle cx="12" cy="16.5" r=".6"/>',
    };
    return this._svg(M[name] || M.tool);
  }
  _fmtDur(ms) {
    if (ms == null) return "—";
    if (ms < 1000) return ms + "ms";
    return (ms / 1000).toFixed(1) + "s";
  }
  _statusBadge(status) {
    const map = { success: ["ok", "完成"], error: ["err", "错误"], running: ["run", "执行中"], unknown: ["", "未知"] };
    const [cls, label] = map[status] || map.unknown;
    return `<span class="ap-badge ap-badge-${cls || "unk"}">${label}</span>`;
  }

  /* ── 面板开关 ── */
  open(dialogUuid) {
    this.dialogUuid = dialogUuid || (window.StockRadar.app && window.StockRadar.app.state
      ? window.StockRadar.app.state.currentId : null);
    if (!this.dialogUuid) {
      this._setStatus("请先选择一个对话", true);
      return;
    }
    this.isOpen = true;
    this._("attrPanel").classList.add("open");
    this._("attrOverlay").classList.add("open");
    this.loadRuns();
  }

  close() {
    this.isOpen = false;
    this._("attrPanel").classList.remove("open");
    this._("attrOverlay").classList.remove("open");
    if (this._liveTimer) { clearTimeout(this._liveTimer); this._liveTimer = null; }
    this._clearLocate();
  }

  _setStatus(msg, isError) {
    const el = this._("attrStatus");
    el.innerHTML = msg || "";
    el.classList.toggle("ap-status-error", !!isError);
  }

  _switchTab(tab) {
    this._("attrTabs").querySelectorAll(".ap-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tab);
    });
    ["overview", "sources", "chain", "raw"].forEach((t) => {
      this._("attr" + t.charAt(0).toUpperCase() + t.slice(1)).classList.toggle("active", t === tab);
    });
  }

  /* ── 加载 run 列表 ── */
  async loadRuns() {
    this._setStatus("加载运行列表…", false);
    try {
      const data = await window.StockRadar.api.getRuns(this.dialogUuid);
      this.runs = (data && data.runs) || [];
      this._renderRunSelect();
      if (!this.runs.length) {
        this._("attrRunPill").hidden = true;
        this._setStatus("当前对话暂无已记录的溯源运行", false);
        this.data = null;
        this._renderEmpty();
        return;
      }
      const def = this.runs[this.runs.length - 1];
      this.loadAttribution(def.run_id);
    } catch (err) {
      this._setStatus("运行列表加载失败：" + (err && err.message ? err.message : err), true);
    }
  }

  _renderRunSelect() {
    const sel = this._("attrRunSelect");
    if (!this.runs.length) { sel.innerHTML = ""; return; }
    sel.innerHTML = this.runs.map((r) => {
      const st = r.status === "success" ? "✓" : r.status === "error" ? "✕" : "•";
      const q = (r.query || "(无输入)").slice(0, 40);
      return `<option value="${this._esc(r.run_id)}">${st} ${this._esc(q)} · ${this._fmtDur(r.duration_ms)}</option>`;
    }).join("");
    const def = this.runs[this.runs.length - 1];
    if (def) sel.value = def.run_id;
  }

  /* ── 加载某 run 的溯源聚合 ── */
  async loadAttribution(runId) {
    if (!runId) return;
    this.currentRunId = runId;
    this._setStatus("聚合溯源数据…", false);
    try {
      const data = await window.StockRadar.api.getAttribution(this.dialogUuid, runId);
      this.data = data;
      this._renderRunPill(data.run);
      this._renderOverview(data);
      this._renderSources(data);
      this._renderRaw(data);
      const total = (data.sources ? data.sources.length : 0) +
        (data.tools ? data.tools.length : 0) + (data.stages ? data.stages.length : 0);
      this._setStatus(`已溯源 ${total} 个条目 · 点击「定位」可在对话中高亮其支撑来源`, false);
      // 思考链路：独立接口，失败不影响其他 Tab
      try {
        const chain = await window.StockRadar.api.getTraceChain(runId);
        this._renderChain(chain);
      } catch (e) {
        this._renderChain(null);
      }
    } catch (err) {
      this._setStatus("溯源聚合失败：" + (err && err.message ? err.message : err), true);
    }
  }

  _renderRunPill(run) {
    const pill = this._("attrRunPill");
    if (!run) { pill.hidden = true; return; }
    pill.hidden = false;
    const txt = this._("attrRunPillText");
    const st = run.status === "success" ? "已完成" : run.status === "error" ? "出错" : "执行中";
    txt.textContent = `${st} · ${this._fmtDur(run.duration_ms)}`;
    pill.className = "ap-run-pill" + (run.status === "success" ? " ok" : run.status === "error" ? " err" : " run");
  }

  _renderEmpty() {
    const empty = '<div class="ap-empty">当前对话暂无溯源数据</div>';
    ["Overview", "Sources", "Chain", "Raw"].forEach((t) => {
      this._("attr" + t).innerHTML = empty;
    });
  }

  _locateBtn(uuid) {
    if (!uuid) return "";
    return `<button class="ap-locate" type="button" data-locate="${this._esc(uuid)}" title="在对话中定位">` +
      this._icon("locate") + "<span>定位</span></button>";
  }

  _bindLocate(container) {
    container.querySelectorAll("[data-locate]").forEach((b) => {
      b.onclick = () => this.locate(b.dataset.locate);
    });
  }

  /* ── 概览 ── */
  _renderOverview(d) {
    const o = d.overview || {};
    const cards = [
      ["耗时", this._fmtDur(o.duration_ms), "clock", ""],
      ["数据源", (o.data_source_count || 0) + " 类", "doc", ""],
      ["工具调用", (o.tool_count || 0) + " 次", "tool", ""],
      ["LLM 调用", (o.llm_call_count || 0) + " 次", "brain", ""],
    ];
    const cardsHtml = cards.map(([label, val, ic, sub]) => `
      <div class="ap-kpi">
        <div class="ap-kpi-bar"></div>
        <div class="ap-kpi-ic">${this._icon(ic)}</div>
        <div class="ap-kpi-label">${label}</div>
        <div class="ap-kpi-val">${this._esc(val)}</div>
      </div>`).join("");
    const statusLine = `
      <div class="ap-ov-status">
        ${this._statusBadge(o.status)}
        ${d.run && d.run.query ? `<span class="ap-ov-q">${this._esc(d.run.query.slice(0, 60))}</span>` : ""}
        ${(o.support_message_count || 0) > 0 ? `<span class="ap-ov-badge">关联 ${o.support_message_count} 条助手消息</span>` : ""}
      </div>`;
    this._("attrOverview").innerHTML = statusLine + `<div class="ap-kpi-row">${cardsHtml}</div>`;
  }

  /* ── 数据来源 ── */
  _renderSources(d) {
    const box = this._("attrSources");
    const sources = d.sources || [];
    if (!sources.length) { box.innerHTML = '<div class="ap-empty">本次运行未归类到已知数据源</div>'; return; }
    box.innerHTML = sources.map((s) => `
      <div class="ap-src">
        <div class="ap-src-ic">${this._icon(this._domainIcon(s.domain))}</div>
        <div class="ap-src-main">
          <div class="ap-src-label">${this._esc(s.label)}</div>
          <div class="ap-src-sub">${s.step_ids.length} 个步骤${s.support_message_uuid ? " · 支撑一段回答" : ""}</div>
        </div>
        ${this._locateBtn(s.support_message_uuid)}
      </div>`).join("");
    this._bindLocate(box);
  }

  _domainIcon(domain) {
    return { "行情": "chart", "基本面": "doc", "资金": "fund", "资讯": "news", "数据": "doc", "推理": "brain" }[domain] || "tool";
  }

  /* ── 工具调用 ── */
  _renderTools(d) {
    const box = this._("attrTools");
    const tools = d.tools || [];
    if (!tools.length) { box.innerHTML = '<div class="ap-empty">无工具调用</div>'; return; }
    box.innerHTML = tools.map((t) => `
      <div class="ap-tool">
        <div class="ap-tool-ic ap-tool-ic-${this._esc((t.domain || "其他").toLowerCase())}">${this._icon(this._domainIcon(t.domain))}</div>
        <div class="ap-tool-main">
          <div class="ap-tool-name">${this._esc(t.display_name || t.step_name)}</div>
          <div class="ap-tool-meta">
            ${t.domain && t.domain !== "其他" ? `<span class="ap-tag">${this._esc(t.domain)}</span>` : ""}
            ${t.symbol ? `<span class="ap-tag ap-tag-mono">${this._esc(t.symbol)}</span>` : ""}
            ${this._statusBadge(t.status)}
            <span class="ap-tool-dur">${this._fmtDur(t.duration_ms)}</span>
          </div>
        </div>
        ${this._locateBtn(t.support_message_uuid)}
      </div>`).join("");
    this._bindLocate(box);
  }

  /* ── 执行轨迹（阶段级浓缩）── */
  _renderStages(d) {
    const box = this._("attrStages");
    const stages = d.stages || [];
    if (!stages.length) { box.innerHTML = '<div class="ap-empty">无执行轨迹</div>'; return; }
    const items = stages.map((s, i) => `
      <div class="ap-stage">
        <div class="ap-stage-node ${s.status === "success" ? "ok" : s.status === "error" ? "err" : "run"}">${i + 1}</div>
        <div class="ap-stage-body">
          <div class="ap-stage-top">
            <span class="ap-stage-label">${this._esc(s.label)}</span>
            ${this._statusBadge(s.status)}
            <span class="ap-stage-dur">${this._fmtDur(s.duration_ms)}</span>
            ${this._locateBtn(s.support_message_uuid)}
          </div>
        </div>
      </div>`).join("");
    box.innerHTML = `<div class="ap-stage-track">${items}</div>`;
    this._bindLocate(box);
  }

  /* ── 思考链路（面向用户的执行回放）──
     节点类型：think（LLM 思考 + 工具调用）/ tool（工具执行）/ milestone（阶段） */
  _renderChain(items) {
    const box = this._("attrChain");
    if (!items || !items.length) {
      box.innerHTML = '<div class="ap-empty">暂无思考链路数据</div>';
      return;
    }
    const nodes = items.map((it) => {
      if (it.type === "think") {
        const calls = (it.tool_calls || []).map((c) => {
          const q = c.args && c.args.query ? String(c.args.query).slice(0, 70) : "";
          return `<span class="ap-chain-call">🔧 ${this._esc(c.name)}${q ? `：${this._esc(q)}` : ""}</span>`;
        }).join("");
        const rz = (it.reasoning || "").trim();
        const rzHtml = rz
          ? `<details class="ap-chain-rz"><summary>🧠 AI 思考</summary><div class="ap-chain-rz-body">${this._esc(rz)}</div></details>`
          : "";
        return `<div class="ap-chain-node ap-chain-think">
          ${rzHtml || '<div class="ap-chain-empty-hint">发起工具调用</div>'}
          ${calls ? `<div class="ap-chain-calls">${calls}</div>` : ""}
        </div>`;
      }
      if (it.type === "tool") {
        const failed = it.status === "error" || (it.summary || "").indexOf("失败") >= 0;
        return `<div class="ap-chain-node ap-chain-tool${failed ? " err" : ""}">
          <div class="ap-chain-tool-q">🔧 ${this._esc(it.step_name)}：${this._esc(it.query || "(无查询语句)")}</div>
          ${it.summary ? `<div class="ap-chain-tool-s">${this._esc(it.summary)}</div>` : ""}
        </div>`;
      }
      return `<div class="ap-chain-node ap-chain-milestone">
        <span class="ap-chain-milestone-dot"></span>
        <span class="ap-chain-milestone-label">${this._esc(it.label || "阶段")}</span>
        ${this._statusBadge(it.status)}
        ${it.duration_ms != null ? `<span class="ap-chain-milestone-dur">${this._fmtDur(it.duration_ms)}</span>` : ""}
      </div>`;
    }).join("");
    box.innerHTML = `<div class="ap-chain-track">${nodes}</div>`;
  }

  /* ── 原始日志（由 steps 现推）── */
  _renderRaw(d) {
    const box = this._("attrRaw");
    const lines = d.raw_log || [];
    if (!lines.length) { box.innerHTML = '<div class="ap-empty">无日志</div>'; return; }
    const cls = (lv) => lv === "ERROR" ? "e" : lv === "WARN" ? "w" : "";
    box.innerHTML = '<div class="ap-term">' + lines.map((l) => {
      const ts = l.ts ? l.ts.replace("T", " ").slice(0, 19) : "--:--:--";
      const dur = l.duration_ms != null ? ` <span class="ap-term-dur">${this._fmtDur(l.duration_ms)}</span>` : "";
      const badge = l.status
        ? `<span class="ap-badge ap-badge-${l.status === "error" ? "err" : l.status === "success" ? "ok" : "run"}">${l.status === "error" ? "错误" : l.status === "success" ? "完成" : l.status}</span>`
        : "";
      let detail = "";
      if (l.input || l.output || l.error) {
        detail = `
          <details class="ap-term-detail"><summary>详情</summary>
            ${l.error ? `<div class="ap-term-err">❌ ${this._esc(l.error)}</div>` : ""}
            ${l.input ? `<div class="ap-term-k">查询</div><div class="ap-term-v">${this._esc(l.input)}</div>` : ""}
            ${l.output ? `<div class="ap-term-k">结果</div><div class="ap-term-v">${this._esc(l.output)}</div>` : ""}
          </details>`;
      }
      return `<div class="ap-log-line">
        <span class="ap-term-ts">${this._esc(ts)}</span>
        <span class="ap-term-lv ${cls(l.level)}">${this._esc(l.level)}</span>
        <span class="ap-term-tx">${this._esc(l.text)}</span>${dur}${badge}
        ${detail}
      </div>`;
    }).join("") + '</div>';
  }

  /* ── 双向定位 ── */
  locate(uuid) {
    if (!uuid) return;
    if (window.StockRadar.chat && window.StockRadar.chat.highlightMessage) {
      window.StockRadar.chat.highlightMessage(uuid);
    }
  }

  _clearLocate() {
    if (window.StockRadar.chat && window.StockRadar.chat.clearLocate) {
      window.StockRadar.chat.clearLocate();
    }
  }

  /* ── SSE 实时增量（保留）── */
  onLiveStep(step) {
    if (!this.isOpen || !this.dialogUuid) return;
    if (step && step.run_id) this.currentRunId = step.run_id;
    if (this._liveTimer) clearTimeout(this._liveTimer);
    this._liveTimer = setTimeout(() => {
      this._liveTimer = null;
      if (this.currentRunId) this.loadAttribution(this.currentRunId).catch(() => {});
    }, 600);
  }

  refresh() {
    if (!this.isOpen) return;
    this.loadRuns();
  }
};
