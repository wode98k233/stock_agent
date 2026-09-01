/* ─────────────────────────────────────────────
   选股雷达 Web — 入口：状态、SSE、初始化
   ───────────────────────────────────────────── */

(() => {
  "use strict";

  const api = window.StockRadar.api;
  const chat = window.StockRadar.chat;
  const tl = window.StockRadar.timeline;
  const ConfigPanel = window.StockRadar.ConfigPanel;
  const ReportTemplateManager = window.StockRadar.ReportTemplateManager;
  const StatusPlayer = window.StockRadar.StatusPlayer.StatusPlayer;

  const state = {
    dialogs: [],
    currentId: null,
    taskId: null,
    eventSource: null,
    statusBubble: null,
    statusPlayer: null,
    selectedMode: "react_stock",
  };

  const dom = {
    dialogList: document.getElementById("dialogList"),
    messageList: document.getElementById("messageList"),
    progressList: document.getElementById("progressList"),
    traceList: document.getElementById("traceList"),
    messageInput: document.getElementById("messageInput"),
    sendBtn: document.getElementById("sendBtn"),
    newDialogBtn: document.getElementById("newDialogBtn"),
    templateManagerBtn: document.getElementById("templateManagerBtn"),
    modeSelect: document.getElementById("modeSelect"),
    modeTrigger: document.getElementById("modeTrigger"),
    modeLabel: document.getElementById("modeLabel"),
    modeDropdown: document.getElementById("modeDropdown"),
    dialogTitle: document.getElementById("dialogTitle"),
    dialogMeta: document.getElementById("dialogMeta"),
    connectionStatus: document.getElementById("connectionStatus"),
    taskStatus: document.getElementById("taskStatus"),
    clearEventsBtn: document.getElementById("clearEventsBtn"),
    traceToggle: document.getElementById("traceToggle"),
    traceSection: document.getElementById("traceSection"),
  };

  function timeAgo(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    return d.toLocaleDateString("zh-CN");
  }

  function ensureStatusPlayer() {
    if (!state.statusBubble) {
      state.statusBubble = chat.createStatusBubble(dom.messageList);
    }
    if (!state.statusPlayer) {
      state.statusPlayer = new StatusPlayer(state.statusBubble, chat);
      state.statusPlayer.start();
    }
    return state.statusPlayer;
  }

  function enqueueStatus(event) {
    ensureStatusPlayer().enqueue(event);
  }

  function stopStatusPlayer() {
    if (!state.statusPlayer) return;
    state.statusPlayer.stop();
    state.statusPlayer = null;
  }

  /* ── 对话列表 ── */
  async function loadDialogs() {
    try {
      state.dialogs = await api.getDialogs();
      renderDialogList();
    } catch (e) {
      console.error("加载对话列表失败", e);
    }
  }

  function renderDialogList() {
    dom.dialogList.innerHTML = "";
    state.dialogs.forEach((d) => {
      const btn = document.createElement("button");
      btn.className = "dialog-item" + (d.dialog_uuid === state.currentId ? " active" : "");
      btn.innerHTML = `
        <div class="dialog-item-title">${chat.esc(d.title || "新对话")}</div>
        <div class="dialog-item-meta">${timeAgo(d.updated_at || d.created_at)}</div>
        <button class="dialog-delete-btn" data-uuid="${d.dialog_uuid}" title="删除对话">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M4 4l6 6M10 4l-6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        </button>`;
      btn.onclick = () => selectDialog(d.dialog_uuid);
      dom.dialogList.appendChild(btn);
    });

    dom.dialogList.querySelectorAll(".dialog-delete-btn").forEach((delBtn) => {
      delBtn.onclick = (e) => {
        e.stopPropagation();
        const uuid = delBtn.dataset.uuid;
        const existing = document.querySelector(".dialog-delete-confirm");
        if (existing) {
          existing.remove();
        }
        const confirm = document.createElement("div");
        confirm.className = "dialog-delete-confirm";
        confirm.innerHTML = `<span>删除此对话？</span><div class="confirm-actions"><button class="confirm-no" type="button">取消</button><button class="confirm-yes" type="button">删除</button></div>`;
        document.body.appendChild(confirm);

        const rect = delBtn.getBoundingClientRect();
        confirm.style.position = "fixed";
        confirm.style.right = (window.innerWidth - rect.right + rect.width / 2) + "px";
        confirm.style.top = (rect.bottom + 6) + "px";

        const closeConfirm = (e2) => {
          if (e2) e2.stopPropagation();
          confirm.remove();
          document.removeEventListener("click", outsideClick);
        };

        const outsideClick = (e3) => {
          if (!confirm.contains(e3.target) && e3.target !== delBtn && !delBtn.contains(e3.target)) {
            closeConfirm();
          }
        };

        requestAnimationFrame(() => {
          document.addEventListener("click", outsideClick);
        });

        confirm.querySelector(".confirm-yes").onclick = (e2) => {
          e2.stopPropagation();
          closeConfirm();
          api.deleteDialog(uuid).then(() => {
            if (state.currentId === uuid) {
              state.currentId = null;
              dom.messageList.innerHTML = "";
              dom.dialogTitle.textContent = "新对话";
              dom.dialogMeta.textContent = "请选择或创建对话";
              chat.showEmptyState(dom.messageList);
            }
            loadDialogs();
          });
        };
        confirm.querySelector(".confirm-no").onclick = closeConfirm;
      };
    });
  }

  async function selectDialog(id) {
    state.currentId = id;
    renderDialogList();
    // crossfade 过渡
    dom.messageList.style.opacity = "0";
    dom.messageList.style.transition = "opacity 120ms ease";
    await new Promise(r => setTimeout(r, 120));
    try {
      await loadMessages(id);
      const d = state.dialogs.find((x) => x.dialog_uuid === id);
      if (d) {
        dom.dialogTitle.textContent = d.title || "新对话";
        dom.dialogMeta.textContent = new Date(d.created_at).toLocaleString("zh-CN");
      }
    } catch (e) {
      console.error("选择对话失败", e);
    }
    requestAnimationFrame(() => {
      dom.messageList.style.opacity = "1";
    });
  }

  // 暴露 selectDialog 给日历组件使用
  window.StockRadar._selectDialog = selectDialog;

  async function loadMessages(dialogId) {
    try {
      const msgs = await api.getMessages(dialogId);
      dom.messageList.innerHTML = "";
      dom.progressList.innerHTML = "";
      dom.traceList.innerHTML = "";
      tl.clearAll();

      if (msgs.length === 0) {
        chat.showEmptyState(dom.messageList);
        return;
      }

      const runningMsg = msgs.find(
        (m) => m.role === "assistant" && (m.status === "streaming" || m.status === "pending") && m.task_id
      );

      if (runningMsg) {
        await _resumeRunningTask(msgs, runningMsg);
        return;
      }

      msgs.forEach((m) => {
        const meta = m.role === "assistant" ? { trace_run_id: m.trace_run_id, task_id: m.task_id, dialog_uuid: m.dialog_uuid, log_file: m.log_file, message_uuid: m.message_uuid } : null;
        chat.appendMessage(dom.messageList, m.role, m.content, false, meta);
      });
      chat.scrollBottom(dom.messageList);

      // 从持久化的 progress 事件中还原「记忆上下文」卡片到主对话
      const nodes = Array.from(dom.messageList.children);
      msgs.forEach((m, idx) => {
        if (m.role === "assistant" && m.extra) {
          try {
            const events = JSON.parse(m.extra);
            const mc = Array.isArray(events) ? events.find((ev) => ev.type === "memory_context") : null;
            if (mc && nodes[idx]) {
              chat.appendMemoryContext(dom.messageList, mc, nodes[idx]);
            }
          } catch {}
        }
      });

      dom.messageList.querySelectorAll(".msg-detail-btn").forEach((btn) => {
        btn.onclick = () => _showTraceForMessage(btn);
      });

      // 异步加载：不阻塞 UI 渲染
      _tryAddGroupExpandButton(dialogId).catch(() => {});
      _loadHistoryTrace(msgs).catch(() => {});
    } catch (e) {
      console.error("加载消息失败", e);
    }
  }

  async function _tryAddGroupExpandButton(dialogId) {
    try {
      const resp = await fetch(`/api/dialogs/${dialogId}/group-messages`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.messages || data.messages.length === 0) return;
      const groupChat = window.StockRadar?.groupChat;
      if (!groupChat) return;
      // 找最后一条 assistant 消息的 action-bar
      const msgs = dom.messageList.querySelectorAll('.message.assistant');
      const lastMsg = msgs[msgs.length - 1];
      if (!lastMsg) return;
      const actionBar = lastMsg.querySelector('.msg-action-bar');
      if (!actionBar) return;
      // 避免重复添加
      if (actionBar.querySelector('.msg-group-btn')) return;
      const btn = groupChat.createGroupChatButton(dialogId);
      actionBar.appendChild(btn);
    } catch {}
  }

  async function _resumeRunningTask(msgs, runningMsg) {
    try {
      const task = await api.getTask(runningMsg.task_id);
      if (task.status === "running" || task.status === "queued") {
        msgs.forEach((m) => {
          if (m.message_uuid === runningMsg.message_uuid) return;
          const meta = m.role === "assistant" ? { trace_run_id: m.trace_run_id, task_id: m.task_id, dialog_uuid: m.dialog_uuid, log_file: m.log_file, message_uuid: m.message_uuid } : null;
          chat.appendMessage(dom.messageList, m.role, m.content, false, meta);
        });

        dom.messageList.querySelectorAll(".msg-detail-btn").forEach((btn) => {
          btn.onclick = () => _showTraceForMessage(btn);
        });

        connectSSE(runningMsg.task_id);
        enqueueStatus({
          type: "step_start",
          message: "恢复执行中…",
        });
        dom.sendBtn.disabled = true;
        dom.sendBtn.classList.add("loading");
        return;
      }
    } catch {}

    msgs.forEach((m) => chat.appendMessage(dom.messageList, m.role, m.content, false));
    chat.scrollBottom(dom.messageList);

    // 从持久化的 progress 事件中还原「记忆上下文」卡片到主对话
    const nodes2 = Array.from(dom.messageList.children);
    msgs.forEach((m, idx) => {
      if (m.role === "assistant" && m.extra) {
        try {
          const events = JSON.parse(m.extra);
          const mc = Array.isArray(events) ? events.find((ev) => ev.type === "memory_context") : null;
          if (mc && nodes2[idx]) {
            chat.appendMemoryContext(dom.messageList, mc, nodes2[idx]);
          }
        } catch {}
      }
    });
  }

  function _renderTraceSteps(steps) {
    steps.forEach((step) => tl.appendTraceStep(dom.traceList, step));
    const progressEvents = tl.buildProgressFromSteps(steps);
    progressEvents.forEach((pe) => tl.appendProgressEvent(dom.progressList, pe));
  }

  async function _showTraceForMessage(btn) {
    const traceRunId = btn.dataset.traceRunId;
    const dialogUuid = btn.dataset.dialogUuid;

    dom.progressList.innerHTML = "";
    dom.traceList.innerHTML = "";
    tl.clearAll();
    dom.taskStatus.textContent = "加载详情…";

    if (traceRunId) {
      try {
        const steps = await api.getTraceSteps(traceRunId);
        if (steps.length > 0) {
          _renderTraceSteps(steps);
          dom.taskStatus.textContent = `${steps.length} 步`;
        } else {
          dom.taskStatus.textContent = "无详情";
        }
      } catch {
        dom.taskStatus.textContent = "加载失败";
      }
      return;
    }

    if (dialogUuid) {
      try {
        const data = await api.getDialogTrace(dialogUuid);
        _renderHistoryTrace(data);
      } catch {
        dom.taskStatus.textContent = "加载失败";
      }
      return;
    }

    dom.taskStatus.textContent = "无详情";
  }

  function _renderHistoryTrace(data) {
    if (dom.traceSection) dom.traceSection.classList.remove("collapsed");
    dom.progressList.innerHTML = "";
    dom.traceList.innerHTML = "";
    tl.clearAll();

    if (data.progress && data.progress.length > 0) {
      data.progress.forEach((pe) => tl.appendProgressEvent(dom.progressList, pe));
    } else if (data.steps && data.steps.length > 0) {
      const progressEvents = tl.buildProgressFromSteps(data.steps);
      progressEvents.forEach((pe) => tl.appendProgressEvent(dom.progressList, pe));
    }

    if (data.steps && data.steps.length > 0) {
      data.steps.forEach((step) => tl.appendTraceStep(dom.traceList, step));
    }

    const progressCount = data.progress ? data.progress.length : 0;
    const stepCount = data.steps ? data.steps.length : 0;
    if (stepCount > 0) {
      dom.taskStatus.textContent = `${stepCount} 步`;
    } else if (progressCount > 0) {
      dom.taskStatus.textContent = `${progressCount} 个进度事件`;
    } else {
      dom.taskStatus.textContent = "无详情";
    }
  }

  async function _loadHistoryTrace(msgs) {
    const lastWithTrace = [...msgs].reverse().find((m) => m.trace_run_id);
    if (lastWithTrace) {
      dom.taskStatus.textContent = "加载详情…";
      try {
        const steps = await api.getTraceSteps(lastWithTrace.trace_run_id);
        let progress = [];
        if (lastWithTrace.extra) {
          try {
            const parsed = JSON.parse(lastWithTrace.extra);
            if (Array.isArray(parsed)) progress = parsed;
          } catch {}
        }
        const data = { trace_run_id: lastWithTrace.trace_run_id, progress, steps };
        _renderHistoryTrace(data);
      } catch {
        dom.taskStatus.textContent = "暂无任务";
      }
      return;
    }

    if (!state.currentId) {
      dom.taskStatus.textContent = "暂无任务";
      return;
    }

    dom.taskStatus.textContent = "查找详情…";
    try {
      const data = await api.getDialogTrace(state.currentId);
      _renderHistoryTrace(data);
    } catch {
      dom.taskStatus.textContent = "暂无任务";
    }
  }

  /* ── SSE ── */
  function connectSSE(taskId) {
    disconnectSSE();
    state.taskId = taskId;
    dom.progressList.innerHTML = "";
    dom.traceList.innerHTML = "";
    tl.clearAll();
    if (dom.traceSection) dom.traceSection.classList.remove("collapsed");
    dom.taskStatus.textContent = "执行中…";
    dom.connectionStatus.textContent = "执行中";

    ensureStatusPlayer();

    const es = new EventSource(`/api/tasks/${taskId}/events`);
    state.eventSource = es;

    es.addEventListener("progress", (e) => {
      try {
        const event = JSON.parse(e.data);
        tl.appendProgressEvent(dom.progressList, event);
        state.statusPlayer.enqueue(event);
        if (event.type === "memory_context") {
          chat.appendMemoryContext(dom.messageList, event);
        }
      } catch {}
    });

    es.addEventListener("trace_step", (e) => {
      try {
        const step = JSON.parse(e.data);
        tl.appendTraceStep(dom.traceList, step);
        if (window.StockRadar.attrPanel) window.StockRadar.attrPanel.onLiveStep(step);
        const pe = tl.traceStepToStatusEvent(step) || tl.traceStepToProgressEvent(step);
        if (pe) {
          tl.appendProgressEvent(dom.progressList, pe);
          state.statusPlayer.enqueue(pe);
        }
      } catch {}
    });

    // Agent 群模式进度事件
    ["group_plan", "group_step_start", "group_step_result",
     "group_observe", "group_adjust", "group_replan", "group_resolve"
    ].forEach(function(evtType) {
      es.addEventListener(evtType, function(e) {
        try {
          var data = JSON.parse(e.data);
          var msg = data.message || evtType;
          var pe = { type: evtType, message: msg, data: data };
          tl.appendProgressEvent(dom.progressList, pe);
          state.statusPlayer.enqueue(pe);
        } catch {}
      });
    });

    es.addEventListener("task_error", (e) => {
      try {
        const data = JSON.parse(e.data);
        stopStatusPlayer();
        if (state.statusBubble) {
          chat.finalizeStatusBubble(state.statusBubble, data.message || "执行出错");
          state.statusBubble.bubble.classList.add("failed");
        }
      } catch {}
      dom.taskStatus.textContent = "出错";
      dom.connectionStatus.textContent = "就绪";
      es.close();
      state.eventSource = null;
      state.statusBubble = null;
      dom.sendBtn.disabled = false;
      dom.sendBtn.classList.remove("loading");
    });

    es.addEventListener("final", (e) => {
      (async () => {
        try {
          const data = JSON.parse(e.data);
          if (state.statusPlayer) {
            await state.statusPlayer.finish(data.content || "");
          } else if (state.statusBubble) {
            await chat.typewriterStatusBubble(state.statusBubble, data.content || "");
          } else {
            chat.appendMessage(dom.messageList, "assistant", data.content || "", true);
          }

          dom.taskStatus.textContent = "完成";
          dom.connectionStatus.textContent = "就绪";
        } catch {}
        es.close();
        state.eventSource = null;
        state.statusPlayer = null;
        state.statusBubble = null;
        dom.sendBtn.disabled = false;
        dom.sendBtn.classList.remove("loading");
        await refreshCurrentDialog();
        if (window.StockRadar.attrPanel) window.StockRadar.attrPanel.refresh();

        // 新提问完成：检查 group_messages 并追加展开按钮
        try {
          const finalData = JSON.parse(e.data);
          if (finalData.dialog_uuid) {
            await _tryAddGroupExpandButton(finalData.dialog_uuid);
          }
        } catch {}
      })();
    });

    es.addEventListener("budget_decision_required", (e) => {
      try {
        const data = JSON.parse(e.data);
        showBudgetDecisionModal(data);
      } catch {}
    });

    es.onerror = () => {
      dom.taskStatus.textContent = "连接断开";
      dom.connectionStatus.textContent = "就绪";
      stopStatusPlayer();
      es.close();
      state.eventSource = null;
      dom.sendBtn.disabled = false;
      dom.sendBtn.classList.remove("loading");
    };
  }

  function disconnectSSE() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    stopStatusPlayer();
  }

  /* ── 预算决策弹窗 ── */
  function showBudgetDecisionModal(data) {
    const overlay = document.getElementById("budgetOverlay");
    const modal = document.getElementById("budgetModal");
    const body = document.getElementById("budgetBody");
    if (!overlay || !modal || !body) return;

    const steps = data.completed_steps || 0;
    const reason = data.reason || "预算超限";
    body.innerHTML =
      '<div class="budget-reason">' + esc(reason) + '</div>' +
      '<div class="budget-stats">已完成 ' + steps + ' 步，已有数据可生成总结</div>';

    overlay.classList.add("open");
    modal.classList.add("open");

    const continueBtn = document.getElementById("budgetContinueBtn");
    const cancelBtn = document.getElementById("budgetCancelBtn");

    function close() {
      overlay.classList.remove("open");
      modal.classList.remove("open");
      continueBtn.onclick = null;
      cancelBtn.onclick = null;
    }

    continueBtn.onclick = function () {
      api.sendBudgetDecision(data.task_id, "continue").catch(function () {});
      close();
    };
    cancelBtn.onclick = function () {
      api.sendBudgetDecision(data.task_id, "cancel").catch(function () {});
      close();
    };
  }

  /* ── 发送消息 ── */

  // 未就绪判定：后端 require_agent_ready / require_memory_ready 返回 503。
  // 区分阻塞来源：'memory'（记忆系统冷启动）或 'agent'（Agent 预热中）。
  function _blockerOf(e) {
    if (!e || !e.detail || typeof e.detail !== "object") return null;
    if (e.detail.blocker === "memory") return "memory";
    if (e.detail.readiness && e.detail.readiness !== "t2_full") return "agent";
    return null;
  }

  // 给等待气泡注入一条进度条（记忆/agent 初始化中展示）
  function _attachProgressBar(statusBubble) {
    if (!statusBubble || !statusBubble.bubble) return;
    if (statusBubble.bubble.querySelector(".status-progress")) return;
    const bar = document.createElement("div");
    bar.className = "status-progress";
    bar.innerHTML = '<div class="status-progress-bar"></div>';
    statusBubble.bubble.insertBefore(bar, statusBubble.bubble.firstChild);
  }

  // 真正发送并处理响应（task_id 走 SSE，content 直接渲染）
  async function _deliverMessage(text) {
    const data = await api.sendMessage(state.currentId, text, state.selectedMode);
    if (data.task_id) {
      connectSSE(data.task_id);
    } else if (data.content) {
      chat.appendMessage(dom.messageList, "assistant", data.content, true);
      dom.sendBtn.disabled = false;
      dom.sendBtn.classList.remove("loading");
    }
  }

  // 轮询 /api/health 直到 agent_ready 且（记忆启用时）memory_ready。
  // 期间更新等待气泡文案 + 进度条：若记忆系统正在冷启动，明确提示用户需等待。
  async function _waitReady(statusBubble, { maxWaitMs = 180000, intervalMs = 1500 } = {}) {
    const start = Date.now();
    const textEl = statusBubble && statusBubble.statusLine
      ? statusBubble.statusLine.querySelector(".status-text") : null;
    while (Date.now() - start < maxWaitMs) {
      await new Promise((r) => setTimeout(r, intervalMs));
      try {
        const h = await api.getHealth();
        const agentOk = !!(h && h.agent_ready);
        const memOk = !(h && h.memory_enabled) || !!(h && h.memory_ready);
        const secs = Math.round((Date.now() - start) / 1000);
        if (agentOk && memOk) return true;
        if (textEl) {
          if (!memOk) {
            textEl.textContent = `记忆系统正在初始化，已等待 ${secs}s，就绪后自动发送…`;
          } else if (!agentOk) {
            textEl.textContent = `Agent 正在初始化，已等待 ${secs}s，就绪后自动发送…`;
          }
        }
      } catch { /* health 偶发失败继续等 */ }
    }
    return false;
  }

  async function sendMessage(text) {
    if (!text.trim()) return;

    if (!state.currentId) {
      await createDialog();
      if (!state.currentId) return;
    }

    const emptyEl = dom.messageList.querySelector(".empty-state");
    if (emptyEl) emptyEl.remove();

    chat.appendMessage(dom.messageList, "user", text, true);
    dom.messageInput.value = "";
    dom.sendBtn.disabled = true;
    dom.sendBtn.classList.add("loading");

    try {
      await _deliverMessage(text);
    } catch (e) {
      const blocker = _blockerOf(e);
      if (blocker) {
        // Agent / 记忆系统仍在初始化：显示带进度条的等待气泡，就绪后自动重发，避免直接失败
        const status = chat.createStatusBubble(dom.messageList);
        _attachProgressBar(status);
        const textEl = status.statusLine.querySelector(".status-text");
        if (textEl) {
          textEl.textContent = blocker === "memory"
            ? "记忆系统正在初始化，就绪后自动发送…"
            : "Agent 正在初始化，就绪后自动发送…";
        }
        const ready = await _waitReady(status);
        status.wrap.remove();
        if (ready) {
          try {
            await _deliverMessage(text);
            return;
          } catch (e2) {
            const bubble = chat.appendMessage(dom.messageList, "assistant", "请求失败: " + e2.message, true);
            bubble.classList.add("failed");
          }
        } else {
          const msg = blocker === "memory"
            ? "记忆系统初始化超时，请稍后重试"
            : "Agent 初始化超时，请稍后重试";
          const bubble = chat.appendMessage(dom.messageList, "assistant", msg, true);
          bubble.classList.add("failed");
        }
      } else {
        const bubble = chat.appendMessage(dom.messageList, "assistant", "请求失败: " + e.message, true);
        bubble.classList.add("failed");
      }
      dom.sendBtn.disabled = false;
      dom.sendBtn.classList.remove("loading");
    }
  }

  async function createDialog() {
    try {
      const data = await api.createDialog(null, state.selectedMode);
      state.currentId = data.dialog_uuid;
      await loadDialogs();
      dom.dialogTitle.textContent = data.title || "新对话";
      dom.dialogMeta.textContent = new Date(data.created_at).toLocaleString("zh-CN");
      dom.messageList.innerHTML = "";
      dom.traceList.innerHTML = "";
      dom.progressList.innerHTML = "";
      dom.taskStatus.textContent = "就绪";
      chat.showEmptyState(dom.messageList);
    } catch (e) {
      console.error("创建对话失败", e);
    }
  }

  async function refreshCurrentDialog() {
    if (state.currentId) {
      await loadDialogs();
      await loadMessages(state.currentId);
    }
  }

  let _modeModes = [];

  async function loadModes() {
    try {
      const data = await api.getModes();
      _modeModes = data.modes || [];
      // 优先使用 localStorage 中保存的模式，否则使用服务器返回的当前模式
      const savedMode = localStorage.getItem("selected_mode");
      const current = savedMode && _modeModes.some(m => m.name === savedMode)
        ? savedMode
        : (data.current || (_modeModes.length > 0 ? _modeModes[0].name : "react_stock"));
      state.selectedMode = current;
      dom.modeSelect.value = current;

      dom.modeDropdown.innerHTML = "";
      _modeModes.forEach((m) => {
        const opt = document.createElement("button");
        opt.type = "button";
        opt.className = "mode-option" + (m.name === current ? " active" : "");
        opt.dataset.value = m.name;
        opt.innerHTML = `
          <div>
            <div class="mode-option-name">${chat.esc(m.label || m.name)}</div>
            ${m.description ? `<div class="mode-option-desc">${chat.esc(m.description)}</div>` : ""}
          </div>
          <svg class="mode-option-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        `;
        opt.addEventListener("click", (e) => {
          e.stopPropagation();
          _selectMode(m.name);
        });
        dom.modeDropdown.appendChild(opt);
      });

      const currentMode = _modeModes.find(m => m.name === current);
      dom.modeLabel.textContent = currentMode ? (currentMode.label || currentMode.name) : current;
      var idleMode = document.getElementById('detailIdleMode');
      if (idleMode) idleMode.textContent = '当前模式：' + (currentMode ? (currentMode.label || currentMode.name) : current);
    } catch {}
  }

  function _selectMode(modeName) {
    state.selectedMode = modeName;
    dom.modeSelect.value = modeName;
    localStorage.setItem("selected_mode", modeName);
    const m = _modeModes.find(x => x.name === modeName);
    dom.modeLabel.textContent = m ? (m.label || m.name) : modeName;
    var idleMode = document.getElementById('detailIdleMode');
    if (idleMode) idleMode.textContent = '当前模式：' + (m ? (m.label || m.name) : modeName);
    dom.modeDropdown.querySelectorAll(".mode-option").forEach(o => {
      o.classList.toggle("active", o.dataset.value === modeName);
    });
    _closeModeDropdown();
  }

  function _toggleModeDropdown() {
    const isOpen = dom.modeDropdown.classList.contains("open");
    if (isOpen) {
      _closeModeDropdown();
    } else {
      dom.modeDropdown.classList.add("open");
      dom.modeTrigger.setAttribute("aria-expanded", "true");
    }
  }

  function _closeModeDropdown() {
    dom.modeDropdown.classList.remove("open");
    dom.modeTrigger.setAttribute("aria-expanded", "false");
  }

  if (dom.modeTrigger) {
    dom.modeTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      _toggleModeDropdown();
    });
    dom.modeTrigger.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        _toggleModeDropdown();
      }
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".mode-selector")) {
        _closeModeDropdown();
      }
    });
  }

  /* ── 配置面板 ── */
  const configPanel = new ConfigPanel();

  /* ── 报告模板管理 ── */
  const templateManager = new ReportTemplateManager();
  if (dom.templateManagerBtn) {
    dom.templateManagerBtn.onclick = () => templateManager.open();
  }
  document.getElementById("templateManagerCloseBtn").onclick = () => templateManager.close();
  document.getElementById("templateManagerOverlay").onclick = () => templateManager.close();

  /* ── 仪表盘管理 ── */
  const DashboardManager = window.StockRadar.DashboardManager;
  const dashboardManager = new DashboardManager();
  const dmBtn = document.getElementById("dashboardManagerBtn");
  if (dmBtn) dmBtn.onclick = () => dashboardManager.open();
  document.getElementById("dashboardManagerCloseBtn").onclick = () => dashboardManager.close();
  document.getElementById("dashboardManagerOverlay").onclick = () => dashboardManager.close();

  /* ── 模板路由配置 ── */
  const routeConfig = new window.StockRadar.RouteConfigPanel();
  document.getElementById("templateRouteConfigBtn").onclick = () => routeConfig.open();

  /* ── 日志关联（回答溯源 · 执行归因）── */
  const attrPanel = new window.StockRadar.AttributionPanel();
  document.getElementById("logTraceBtn").onclick = () => attrPanel.open(state.currentId);
  window.StockRadar.attrPanel = attrPanel;

  /* ── 交易日历 ── */
  const calendarPanel = new window.StockRadar.TradingCalendar();
  document.getElementById("calendarBtn").onclick = () => calendarPanel.open();

  /* ── 自选股 ── */
  const watchlistPanel = new window.StockRadar.WatchlistPanel();
  document.getElementById("watchlistBtn").onclick = () => watchlistPanel.open();

  /* ── K 线图 ── */
  const klinePanel = new window.StockRadar.KlinePanel();
  document.getElementById("klineBtn").onclick = () => klinePanel.open();

  // 暴露 openKlineChart 函数，供自选股等模块调用；opts.markRange 支持 AI 信号评测跳转标记
  window.openKlineChart = (code, opts) => klinePanel.open(code, opts);

  /* ── 策略回测 ── */
  const btPanel = window.initBacktestPanel();
  document.getElementById("backtestBtn").onclick = () => btPanel.show();
  document.getElementById("backtestOverlay").onclick = (e) => { if (e.target.id === 'backtestOverlay') btPanel.hide(); };

  /* ── 数据中心 ── */
  const dcPanel = window.initDataCenterPanel();
  document.getElementById("dataCenterBtn").onclick = () => dcPanel.show();
  document.getElementById("dcOverlay").onclick = (e) => { if (e.target.id === 'dcOverlay') dcPanel.hide(); };

  /* ── 热点新闻 ── */
  if (window.StockRadar?.newsPanel) {
    document.getElementById("newsBtn").onclick = () => window.StockRadar.newsPanel.show();
    document.getElementById("newsOverlay").onclick = () => window.StockRadar.newsPanel.hide();
  }

  /* ── 资金流向 ── */
  if (window.StockRadar?.flowPanel) {
    document.getElementById("flowBtn").onclick = () => window.StockRadar.flowPanel.show();
    document.getElementById("flowOverlay").onclick = () => window.StockRadar.flowPanel.hide();
  }

  document.getElementById("configBtn").onclick = () => configPanel.open();
  document.getElementById("configCloseBtn").onclick = () => configPanel.close();
  document.getElementById("configOverlay").onclick = () => configPanel.close();
  document.getElementById("configDiffBtn").onclick = () => configPanel.showDiff();

  /* ── 关闭服务 ── */
  window.StockRadar.shutdownServer = async function () {
    if (!confirm('确认关闭 Web 服务？\n\n关闭后需要通过命令行重新启动。')) return;
    try {
      await api.fetchJSON('/api/system/shutdown', { method: 'POST' });
      alert('服务已关闭。');
    } catch (e) {
      // shutdown 会导致连接断开，可能抛出网络错误，忽略
      alert('关闭请求已发送。如果页面未响应，服务可能已经终止。');
    }
  };

  /* ── 重启服务 ── */
  window.StockRadar.restartServer = async function () {
    if (!confirm('确认重启整个 Web 服务？\n\n服务将在 1 秒后重启，重启后页面将在几秒后自动刷新。')) return;
    try {
      await api.fetchJSON('/api/system/restart', { method: 'POST' });
      // 轮询等待服务恢复
      let attempts = 0;
      const check = setInterval(async () => {
        attempts++;
        try {
          await api.fetchJSON('/api/modes');
          clearInterval(check);
          location.reload();
        } catch (e) {
          if (attempts > 30) {
            clearInterval(check);
            alert('服务重启超时，请手动刷新页面。');
          }
        }
      }, 1000);
    } catch (e) {
      alert('重启请求失败: ' + (e.message || e));
    }
  };

  document.getElementById("diffCloseBtn").onclick = () => configPanel.closeDiff();
  document.getElementById("diffOverlay").onclick = () => configPanel.closeDiff();
  document.getElementById("diffCancelBtn").onclick = () => configPanel.closeDiff();
  document.getElementById("diffApplyBtn").onclick = () => configPanel.applyConfig();

  /* ── 事件绑定 ── */
  dom.newDialogBtn.onclick = () => createDialog();
  dom.clearEventsBtn.onclick = () => {
    dom.progressList.innerHTML = "";
    dom.traceList.innerHTML = "";
  };

  if (dom.traceToggle && dom.traceSection) {
    dom.traceToggle.onclick = () => tl.toggleTraceSection(dom.traceToggle, dom.traceSection);
  }

  document.getElementById("composer").onsubmit = (e) => {
    e.preventDefault();
    sendMessage(dom.messageInput.value);
  };

  dom.messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(dom.messageInput.value);
    }
  });

  dom.messageInput.addEventListener("input", () => {
    dom.messageInput.style.height = "auto";
    dom.messageInput.style.height = Math.min(dom.messageInput.scrollHeight, 160) + "px";
  });

  /* ── 初始化 ── */
  // 侧边栏「日志关联」溯源按钮：默认隐藏（开发者功能），WEB_SHOW_TRACE_TOOLS=true 时显示
  function _applyUiConfig() {
    api.getConfig().then((data) => {
      const items = (data && data.items) || [];
      const item = items.find((x) => x.key === "WEB_SHOW_TRACE_TOOLS");
      const show = item ? !!item.value : false;
      const btn = document.getElementById("logTraceBtn");
      if (btn) btn.hidden = !show;
    }).catch(() => {});
  }

  async function init() {
    _applyUiConfig();
    await loadModes();
    await loadDialogs();
    if (state.dialogs.length > 0) {
      selectDialog(state.dialogs[0].dialog_uuid);
    } else {
      chat.showEmptyState(dom.messageList);
    }
  }

  init();
})();
