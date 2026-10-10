/* ─────────────────────────────────────────────
   选股雷达 Web — 对话过程流（Agent Transcript）
   think 折叠块 + 工具调用行 + 可展开结果
   数据源：SSE `transcript` 事件 / GET /api/tasks/{id}/transcript 快照 /
   web_messages.extra 回放（entry_id 幂等 upsert）
   设计：docs/superpowers/specs/2026-10-10-agent-transcript-ui-design.md
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.chatTranscript = (() => {
  const turns = new Map(); // turnKey -> {card, nodes:Map, count, done, frozen}
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  function _fmtMs(ms) {
    if (ms == null) return "";
    const s = Number(ms) / 1000;
    return s >= 1 ? `${s.toFixed(1)}s` : `${Math.round(Number(ms))}ms`;
  }

  function _fmtFull(v) {
    // trace 详情接口的 input/output 可能是对象（JSON 列被解析），直接 String() 会变 [object Object]
    if (v == null || v === "") return "（空）";
    if (typeof v === "string") return v;
    try {
      return JSON.stringify(v, null, 2);
    } catch {
      return String(v);
    }
  }

  /* ── 过程卡（每轮一张） ── */
  function _ensureTurn(turnKey, container, anchorEl) {
    let t = turns.get(turnKey);
    if (t && t.card.isConnected) return t;
    const card = document.createElement("div");
    card.className = "transcript-card";
    card.dataset.turn = String(turnKey);
    card.innerHTML = `
      <div class="tcard-hd">
        <span class="tcard-live"></span>
        <span class="tcard-title">执行过程</span>
        <span class="tcard-st">运行中…</span>
      </div>
      <div class="tcard-line"></div>`;
    if (anchorEl && anchorEl.parentNode === container) {
      container.insertBefore(card, anchorEl);
    } else {
      container.appendChild(card);
    }
    t = { card, nodes: new Map(), count: 0, done: false, frozen: false };
    turns.set(turnKey, t);
    return t;
  }

  function _setStatus(t, text, mode) {
    t.card.querySelector(".tcard-st").textContent = text;
    t.card.classList.toggle("done", mode === "done");
    t.card.classList.toggle("frozen", mode === "frozen");
    const live = t.card.querySelector(".tcard-live");
    if (mode) live.style.display = "none";
    else live.style.display = "";
  }

  /* ── think 节点 ── */
  function _renderThink(t, entry, live) {
    let rec = t.nodes.get(entry.entry_id);
    const first = !rec;
    if (first) {
      const el = document.createElement("div");
      el.className = "tr-node tr-think run";
      el.dataset.entry = entry.entry_id;
      el.innerHTML = `
        <div class="tr-head">
          <span class="tr-icon">💭</span>
          <span class="tr-name">思考中…</span>
          <span class="tr-sts"><span class="tr-spin"></span></span>
        </div>
        <div class="tr-body"><div class="tr-think-text"></div></div>
        <div class="tr-note"></div>`;
      t.card.querySelector(".tcard-line").appendChild(el);
      rec = { el, entry, manualOpen: false, typing: false, abort: false };
      t.nodes.set(entry.entry_id, rec);
      t.count += 1;
      const toggle = () => {
        rec.manualOpen = true;
        const body = el.querySelector(".tr-body");
        const open = body.style.display === "block";
        body.style.display = open ? "none" : "block";
        el.querySelector(".tr-note").style.display = open ? "" : "none";
      };
      el.querySelector(".tr-head").addEventListener("click", toggle);
      el.querySelector(".tr-note").addEventListener("click", toggle);
    }
    rec.entry = entry;
    const el = rec.el;
    const text = entry.text || "";
    if (live && !t.done && !t.frozen) {
      // 执行中：展开 + 打字机（前端模拟，LLM 非 token 流）
      el.querySelector(".tr-body").style.display = "block";
      if (!rec.typing && el.querySelector(".tr-think-text").textContent.length < text.length) {
        rec.typing = true;
        _typewrite(rec, text);
      }
    } else {
      el.querySelector(".tr-think-text").textContent = text;
      _settleThink(t, rec, "");
    }
    return rec;
  }

  async function _typewrite(rec, text) {
    const target = rec.el.querySelector(".tr-think-text");
    target.textContent = "";
    for (const ch of text) {
      if (rec.abort) break;
      target.textContent += ch;
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 8));
    }
    rec.typing = false;
  }

  function _settleThink(t, rec, note) {
    const el = rec.el;
    rec.abort = true;
    el.classList.remove("run");
    el.classList.add("ok");
    const text = (rec.entry && rec.entry.text) || "";
    el.querySelector(".tr-name").textContent = "思考过程";
    el.querySelector(".tr-sts").innerHTML = `<span class="tr-badge">${text.length} 字</span>`;
    if (!rec.manualOpen) {
      el.querySelector(".tr-body").style.display = "none";
      const noteEl = el.querySelector(".tr-note");
      noteEl.textContent = note || `▸ 已自动收起（点击展开${text.length ? `，${text.slice(0, 24)}…` : ""}）`;
      noteEl.style.display = "";
    } else {
      el.querySelector(".tr-think-text").textContent = text;
      el.querySelector(".tr-body").style.display = "block";
    }
  }

  /* ── 工具节点 ── */
  function _renderTool(t, entry) {
    let rec = t.nodes.get(entry.entry_id);
    const first = !rec;
    if (first) {
      const el = document.createElement("div");
      el.className = "tr-node tr-tool run";
      el.dataset.entry = entry.entry_id;
      el.innerHTML = `
        <div class="tr-head">
          <span class="tr-icon">⚙</span>
          <span class="tr-name"></span>
          <span class="tr-arg"></span>
          <span class="tr-sts"></span>
        </div>
        <div class="tr-body"></div>`;
      t.card.querySelector(".tcard-line").appendChild(el);
      rec = { el, entry, expanded: false };
      t.nodes.set(entry.entry_id, rec);
      t.count += 1;
      el.querySelector(".tr-head").addEventListener("click", () => {
        rec.expanded = !rec.expanded;
        el.querySelector(".tr-body").style.display = rec.expanded ? "block" : "none";
      });
    }
    rec.entry = entry;
    const el = rec.el;
    el.querySelector(".tr-name").textContent = entry.tool_name || "tool";
    el.querySelector(".tr-arg").textContent = entry.input_summary || "";
    el.querySelector(".tr-arg").title = entry.input_summary || "";

    const sts = el.querySelector(".tr-sts");
    el.classList.remove("run", "ok", "err");
    if (entry.status === "running") {
      el.classList.add("run");
      sts.innerHTML = `<span class="tr-spin"></span>`;
    } else if (entry.status === "error") {
      el.classList.add("err");
      sts.innerHTML = `<span class="tr-badge err">✗ 失败</span><span class="tr-dur">${_fmtMs(entry.duration_ms)}</span>`;
    } else {
      el.classList.add("ok");
      sts.innerHTML = `<span class="tr-badge ok">✓ 成功</span><span class="tr-dur">${_fmtMs(entry.duration_ms)}</span>`;
    }

    if (first || rec.renderedStatus !== entry.status) {
      rec.renderedStatus = entry.status;
      const body = el.querySelector(".tr-body");
      const errLine = entry.status === "error" && entry.error
        ? `<div class="tr-pre"><span class="tr-lbl">错误</span>${esc(entry.error)}</div>` : "";
      body.innerHTML = `
        <div class="tr-pre"><span class="tr-lbl">入参</span>${esc(entry.input_summary || "（空）")}</div>
        <div class="tr-pre"><span class="tr-lbl">出参${entry.status === "error" ? "（含错误）" : ""}</span>${esc(entry.output_summary || "（空）")}</div>
        ${errLine}
        <div class="tr-more">展开全文（懒加载 trace 步骤详情）</div>`;
      const more = body.querySelector(".tr-more");
      if (entry.status !== "running") {
        more.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          more.textContent = "加载中…";
          try {
            const api = window.StockRadar.api;
            const d = await api.getStepDetail(entry.run_id, entry.step_id);
            const full = d && (d.output != null ? d.output : d.detail && d.detail.output);
            const fullIn = d && (d.input != null ? d.input : d.detail && d.detail.input);
            body.innerHTML = `
              <div class="tr-pre"><span class="tr-lbl">入参（全文）</span>${esc(_fmtFull(fullIn != null ? fullIn : entry.input_summary))}</div>
              <div class="tr-pre"><span class="tr-lbl">出参（全文）</span>${esc(_fmtFull(full != null ? full : entry.output_summary))}</div>
              <div class="tr-more">收起全文</div>`;
            body.querySelector(".tr-more").addEventListener("click", (e2) => {
              e2.stopPropagation();
              rec.renderedStatus = null; // 强制重建摘要视图（否则节流分支跳过 body 重建）
              _renderTool(t, entry);
            });
          } catch {
            more.textContent = "加载失败（点标题收起/展开）";
          }
        });
      } else {
        more.remove();
      }
    }
    el.querySelector(".tr-body").style.display = rec.expanded ? "block" : "none";
    return rec;
  }

  /* ── 对外 API ── */
  function upsert(turnKey, entry, container, anchorEl) {
    if (!entry || !entry.entry_id || !container) return;
    const t = _ensureTurn(turnKey, container, anchorEl);
    if (entry.kind === "think") {
      _renderThink(t, entry, true);
    } else {
      _renderTool(t, entry);
    }
    t.card.scrollIntoView && t.card.scrollIntoView({ block: "nearest" });
  }

  function replay(turnKey, entries, container, anchorEl) {
    if (!Array.isArray(entries) || !entries.length) return;
    const t = _ensureTurn(turnKey, container, anchorEl);
    const sorted = [...entries].sort((a, b) => {
      const sa = a.sort || [0, 0]; const sb = b.sort || [0, 0];
      return sa[0] - sb[0] || sa[1] - sb[1];
    });
    sorted.forEach((e) => {
      if (e.kind === "think") _renderThink(t, e, false);
      else _renderTool(t, e);
    });
    if (t.done || t.frozen) return;
    if (sorted.some((e) => e.kind === "think")) {
      t.nodes.forEach((rec, id) => { if (id.startsWith("t:")) _settleThink(t, rec, ""); });
    }
  }

  function finish(turnKey) {
    const t = turns.get(turnKey);
    if (!t) return;
    t.done = true;
    _setStatus(t, `完成 · ${t.count} 节点`, "done");
    t.nodes.forEach((rec, id) => {
      if (id.startsWith("t:")) _settleThink(t, rec, "");
    });
  }

  function freeze(turnKey, label) {
    const t = turns.get(turnKey);
    if (!t) return;
    t.frozen = true;
    _setStatus(t, `${label || "已停止"} · ${t.count} 节点`, "frozen");
    t.nodes.forEach((rec, id) => {
      if (id.startsWith("t:")) {
        _settleThink(t, rec, "▸ 思考被终止（已保留已产生的内容）");
      } else if (rec.el.classList.contains("run")) {
        rec.el.classList.remove("run");
        rec.el.classList.add("frozen");
        rec.el.querySelector(".tr-sts").innerHTML = `<span class="tr-badge frozen">已终止</span>`;
      }
    });
  }

  function reset() {
    turns.clear();
  }

  return { upsert, replay, finish, freeze, reset };
})();
