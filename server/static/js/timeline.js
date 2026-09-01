/* ─────────────────────────────────────────────
   选股雷达 Web — 时间线：进度概要 + Trace 详情
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.timeline = (() => {
  const { esc, TYPE_LABELS: PROGRESS_LABELS, TRACE_ICONS } = window.StockRadar.utils;

  const _stepItems = new Map();

  const _MILESTONE_TYPES = new Set([
    "classifier", "planner", "executor", "replanner", "unified_executor",
  ]);

  const _MILESTONE_MSG = {
    classifier: (s) => s.extra?.label || "意图分类",
    planner: (s) => s.extra?.label || "生成执行计划",
    executor: (s) => s.extra?.label || "执行步骤",
    replanner: (s) => s.extra?.label || "重新规划",
    unified_executor: (s) => s.extra?.label || "统一执行",
  };

  const _STATUS_STEP_MSG = {
    graph: () => "启动工作流",
    classify: () => "识别问题类型",
    classifier: () => "识别问题类型",
    select_template: () => "选择报告模板",
    select_skills: () => "选择可用工具",
    prepare: () => "整理上下文",
    agent: () => "模型分析中",
    llm: (s) => _llmStatusMessage(s),
    tools: () => "执行工具调用",
    tool: (s) => _toolStatusMessage(s),
    template_report: () => "生成结构化报告",
    dashboard: () => "生成决策面板",
    partial_summary: () => "整理阶段性结果",
  };

  function traceStepToProgressEvent(step) {
    if (!_MILESTONE_TYPES.has(step.step_type)) return null;
    const msg = _MILESTONE_MSG[step.step_type];
    return {
      type: step.step_type,
      message: msg(step),
      data: {},
    };
  }

  function traceStepToStatusEvent(step) {
    const msgBuilder = _STATUS_STEP_MSG[step.step_type];
    if (!msgBuilder) return null;

    const message = msgBuilder(step);
    const isDone = step.status === "success";
    const isError = step.status === "error";
    const type = isError ? "error" : isDone ? "step_complete" : "step_start";
    return {
      type,
      message: `${message}${isDone && step.duration_ms != null ? ` · ${Math.round(step.duration_ms)}ms` : ""}`,
      data: {
        step_id: step.id,
        step_type: step.step_type,
        step_name: step.step_name,
        status: step.status,
      },
    };
  }

  function _llmStatusMessage(step) {
    const label = step.extra?.label || step.extra?.node || "LLM";
    if (step.status === "running") return `${label} 调用中`;
    const usage = step.extra?.token_usage;
    if (usage && usage.total_tokens != null) {
      let msg = `${label} 完成 · ${usage.total_tokens} tokens`;
      if (usage.reasoning_tokens) msg += ` · 思考 ${usage.reasoning_tokens}`;
      if (usage.cached_tokens) msg += ` · cached ${usage.cached_tokens} (${(usage.cache_hit_ratio * 100).toFixed(0)}%)`;
      return msg;
    }
    return `${label} 完成`;
  }

  function _toolStatusMessage(step) {
    const name = step.step_name || "工具";
    if (step.status === "running") return `调用 ${name}`;
    if (step.status === "error") return `${name} 调用失败`;
    return `${name} 返回结果`;
  }

  function buildProgressFromSteps(steps) {
    const events = [];
    let toolCount = 0;

    steps.forEach((step) => {
      const pe = traceStepToProgressEvent(step);
      if (pe) {
        if (toolCount > 0) {
          events.push({ type: "tool_call", message: `${toolCount} 次工具调用`, data: {} });
          toolCount = 0;
        }
        events.push(pe);
      } else if (step.step_type === "tool") {
        toolCount++;
      }
    });

    if (toolCount > 0) {
      events.push({ type: "tool_call", message: `${toolCount} 次工具调用`, data: {} });
    }

    return events;
  }

  /* ── 进度事件（流式进度） ── */
  function appendProgressEvent(container, event) {
    if (event.type === "stream_chunk") return;
    if (event.type === "memory_context") {
      _renderMemoryContext(container, event);
      return;
    }

    const item = document.createElement("div");
    const tc = _typeClass(event.type);
    item.className = `tl-item ${tc}`;

    const hasData = event.data && Object.keys(event.data).length > 0;

    let dataPreview = "";
    if (hasData) {
      if (event.data.label) {
        dataPreview = esc(event.data.label);
      } else if (event.data.step_name) {
        dataPreview = esc(event.data.step_name);
      } else {
        const previewKeys = Object.keys(event.data).filter(k => k !== "type" && k !== "message");
        if (previewKeys.length > 0) {
          const firstVal = event.data[previewKeys[0]];
          dataPreview = esc(typeof firstVal === "string" ? firstVal.slice(0, 80) : JSON.stringify(firstVal).slice(0, 80));
        }
      }
    }

    const messageText = esc(event.message || "");
    const maxMsgLen = 80;
    const isLongMsg = messageText.length > maxMsgLen;
    const displayMsg = isLongMsg ? messageText.slice(0, maxMsgLen) + "…" : messageText;
    const hasExpandable = hasData || isLongMsg;

    const header = document.createElement("div");
    header.className = "tl-header";
    header.innerHTML = `
      <span class="tl-type">${PROGRESS_LABELS[event.type] || event.type}</span>
      <span class="tl-name">${displayMsg}</span>
      ${dataPreview ? `<span class="tl-data-preview">${dataPreview}</span>` : ""}
      ${hasExpandable ? `<svg class="tl-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>` : ""}
    `;
    item.appendChild(header);

    if (hasExpandable) {
      const detail = document.createElement("div");
      detail.className = "tl-detail";
      let detailHtml = "";
      if (hasData) {
        detailHtml += `<pre>${esc(JSON.stringify(event.data, null, 2))}</pre>`;
      }
      if (detailHtml) {
        detail.innerHTML = `<div class="tl-detail-inner">${detailHtml}</div>`;
        item.appendChild(detail);
      }

      header.addEventListener("click", () => {
        const isOpen = item.classList.contains("tl-open");
        if (isOpen) {
          item.classList.remove("tl-open");
          header.classList.remove("tl-active");
          if (isLongMsg) {
            const nameEl = header.querySelector(".tl-name");
            nameEl.textContent = displayMsg;
            nameEl.style.whiteSpace = "";
            nameEl.style.overflow = "";
          }
        } else {
          item.classList.add("tl-open");
          header.classList.add("tl-active");
          if (isLongMsg) {
            const nameEl = header.querySelector(".tl-name");
            nameEl.textContent = messageText;
            nameEl.style.whiteSpace = "normal";
            nameEl.style.overflow = "visible";
          }
        }
      });
    }

    container.appendChild(item);
    container.scrollTop = container.scrollHeight;
  }

  /* ── 记忆上下文（用户原始输入 / 用户画像 / 用户相关记忆片段） ── */
  function _renderMemoryContext(container, event) {
    const item = document.createElement("div");
    item.className = "tl-item tl-type-memory mem-ctx";

    const data = event.data || {};
    const rawInput = data.raw_input || "";
    const profile = data.profile || "";
    const fragments = data.memory_fragments || "";

    const header = document.createElement("div");
    header.className = "tl-header";
    header.innerHTML = `
      <span class="tl-type">${PROGRESS_LABELS[event.type] || "记忆上下文"}</span>
      <span class="tl-name">用户原始输入 / 用户画像 / 用户相关记忆片段</span>
      <svg class="tl-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
    `;
    item.appendChild(header);

    const detail = document.createElement("div");
    detail.className = "tl-detail";
    detail.innerHTML = `<div class="tl-detail-inner mem-ctx-inner">
      ${_memCtxBlock("用户原始输入", rawInput, "（空）")}
      ${_memCtxBlock("用户画像", profile, "（暂无用户画像）")}
      ${_memCtxBlock("用户相关记忆片段", fragments, "（无相关记忆）")}
    </div>`;
    item.appendChild(detail);

    header.addEventListener("click", () => {
      const isOpen = item.classList.contains("tl-open");
      if (isOpen) {
        item.classList.remove("tl-open");
        header.classList.remove("tl-active");
      } else {
        item.classList.add("tl-open");
        header.classList.add("tl-active");
      }
    });

    container.appendChild(item);
    container.scrollTop = container.scrollHeight;
  }

  function _memCtxBlock(label, content, emptyText) {
    const isEmpty = !content || !String(content).trim();
    const body = isEmpty ? (emptyText || "（空）") : String(content);
    return `<div class="mem-ctx-block">
      <div class="mem-ctx-label">${esc(label)}</div>
      <div class="mem-ctx-body">${esc(body)}</div>
    </div>`;
  }

  /* ── Trace Step（执行详情） ── */
  function appendTraceStep(container, step) {
    const existing = _stepItems.get(step.id);
    if (existing) {
      _updateTraceStep(existing, step);
      return;
    }

    const icon = step.icon || TRACE_ICONS[step.step_type] || "·";
    const typeClass = step.step_type || "unknown";
    const statusBadge = _statusBadge(step.status);
    const dur = step.duration_ms != null ? ` ${Math.round(step.duration_ms)}ms` : "";
    const label = (step.extra && step.extra.label) ? ` · ${esc(step.extra.label)}` : "";

    const tc = _typeClass(step.step_type);
    const item = document.createElement("div");
    item.className = `tl-item ${tc}`;

    // 头部：可点击展开
    const header = document.createElement("div");
    header.className = "tl-header";
    header.innerHTML = _traceHeaderHtml({ icon, typeClass, step, label, dur, statusBadge });
    item.appendChild(header);

    // 详情区（懒加载）
    const detail = document.createElement("div");
    detail.className = "tl-detail";
    const detailInner = document.createElement("div");
    detailInner.className = "tl-detail-inner";
    detail.appendChild(detailInner);
    item.appendChild(detail);

    let currentStep = step;
    let loaded = false;

    header.addEventListener("click", async () => {
      const isOpen = item.classList.contains("tl-open");
      if (!isOpen) {
        if (!loaded) {
          detailInner.innerHTML = '<div class="tl-loading">加载中…</div>';
          try {
            const d = await window.StockRadar.api.getStepDetail(currentStep.id);
            detailInner.innerHTML = _renderStepBody(d, currentStep);
            loaded = true;
          } catch {
            detailInner.innerHTML = '<div class="tl-error-box">加载失败</div>';
          }
        }
        item.classList.add("tl-open");
        header.classList.add("tl-active");
      } else {
        item.classList.remove("tl-open");
        header.classList.remove("tl-active");
      }
    });

    _stepItems.set(step.id, {
      item,
      header,
      detailInner,
      get step() { return currentStep; },
      set step(value) { currentStep = value; },
      get loaded() { return loaded; },
      set loaded(value) { loaded = value; },
    });

    container.appendChild(item);
    container.scrollTop = container.scrollHeight;
  }

  function _traceHeaderHtml({ icon, typeClass, step, label, dur, statusBadge }) {
    return `
      <span class="tl-icon">${icon}</span>
      <span class="tl-type">${esc(typeClass)}</span>
      <span class="tl-name">${esc(step.step_name || "?")}${label}${dur}</span>
      ${statusBadge}
      <svg class="tl-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
    `;
  }

  function _statusBadge(status) {
    if (status === "running") return '<span class="tl-badge tl-running">运行中</span>';
    if (status === "error") return '<span class="tl-badge tl-error">错误</span>';
    return '<span class="tl-badge tl-success">完成</span>';
  }

  function _updateTraceStep(record, step) {
    const icon = step.icon || TRACE_ICONS[step.step_type] || "·";
    const typeClass = step.step_type || "unknown";
    const dur = step.duration_ms != null ? ` ${Math.round(step.duration_ms)}ms` : "";
    const label = (step.extra && step.extra.label) ? ` · ${esc(step.extra.label)}` : "";
    record.step = step;
    record.header.innerHTML = _traceHeaderHtml({
      icon,
      typeClass,
      step,
      label,
      dur,
      statusBadge: _statusBadge(step.status),
    });
    if (record.loaded && record.item.classList.contains("tl-open")) {
      record.detailInner.innerHTML = '<div class="tl-loading">刷新中…</div>';
      window.StockRadar.api.getStepDetail(step.id).then((d) => {
        record.detailInner.innerHTML = _renderStepBody(d, step);
      }).catch(() => {
        record.detailInner.innerHTML = '<div class="tl-error-box">加载失败</div>';
      });
    } else {
      record.loaded = false;
    }
  }

  function _renderStepBody(detail, step) {
    let html = "";

    if (detail.input) {
      html += `<div class="tl-section-title">Input</div>
        <div class="tl-section-content">${_formatValue(detail.input)}</div>`;
    }

    const msgs = detail.messages || [];
    if (msgs.length) {
      html += '<div class="tl-section-title">Messages</div>';
      html += '<div class="tl-msg-list">';
      msgs.forEach(m => {
        const role = m.role || "unknown";
        html += `<div class="tl-msg-item">
          <div class="tl-msg-role tl-msg-role-${role}">${esc(role)}</div>
          <div class="tl-msg-content">${esc(m.content || "")}</div>
          ${m.reasoning ? `<details class="tl-reasoning-block"><summary>💭 推理过程</summary><div class="tl-reasoning-content">${esc(m.reasoning)}</div></details>` : ""}
          ${m.tool_calls ? `<div class="tl-msg-tool-calls">${esc(typeof m.tool_calls === "object" ? JSON.stringify(m.tool_calls, null, 2) : m.tool_calls)}</div>` : ""}
        </div>`;
      });
      html += '</div>';
    }

    if (step.extra && step.extra.token_usage) {
      const u = step.extra.token_usage;
      html += `<div class="tl-token-info">
        ${u.prompt_tokens != null ? `<span>↑ prompt ${u.prompt_tokens}</span>` : ""}
        ${u.completion_tokens != null ? `<span>↓ completion ${u.completion_tokens}</span>` : ""}
        ${u.reasoning_tokens ? `<span>🧠 思考 ${u.reasoning_tokens}</span>` : ""}
        ${u.total_tokens != null ? `<span>∑ total ${u.total_tokens} tokens</span>` : ""}
        ${u.cached_tokens ? `<span>⚡ cached ${u.cached_tokens} (${(u.cache_hit_ratio * 100).toFixed(1)}%)</span>` : ""}
      </div>`;
    }

    if (detail.output) {
      html += `<div class="tl-section-title">Output</div>
        <div class="tl-section-content">${_formatValue(detail.output)}</div>`;
    }

    if (step.error) {
      html += `<div class="tl-error-box">${esc(step.error)}</div>`;
    }

    return html;
  }

  function _typeLabel(type) {
    if (!type) return "STEP";
    const t = String(type).toLowerCase();
    if (t.includes("llm")) return "LLM";
    if (t.includes("tool")) return "TOOL";
    if (t.includes("chain")) return "CHAIN";
    if (t.includes("agent")) return "AGENT";
    if (t.includes("error")) return "ERROR";
    return t.toUpperCase();
  }

  function _typeClass(type) {
    if (!type) return "";
    const t = String(type).toLowerCase();
    if (t.includes("llm")) return "tl-type-llm";
    if (t.includes("tool")) return "tl-type-tool";
    if (t.includes("chain")) return "tl-type-chain";
    if (t.includes("agent")) return "tl-type-agent";
    if (t.includes("error")) return "tl-type-error";
    return "";
  }

  function _formatValue(val) {
    if (val == null) return "";
    if (typeof val === "string") {
      try { val = JSON.parse(val); } catch { return esc(val); }
    }
    if (typeof val === "object") {
      const str = JSON.stringify(val, null, 2);
      return `<pre>${esc(str)}</pre>`;
    }
    return esc(String(val));
  }

  function clearAll() {
    _stepItems.clear();
  }

  function toggleTraceSection(btn, section) {
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    section.classList.toggle("collapsed", expanded);
    btn.querySelector(".collapse-arrow").textContent = expanded ? "▾" : "▴";
  }

  return {
    appendProgressEvent, appendTraceStep, clearAll, toggleTraceSection,
    traceStepToProgressEvent, traceStepToStatusEvent, buildProgressFromSteps,
  };
})();
