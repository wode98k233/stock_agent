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
    stopTaskBtn: document.getElementById("stopTaskBtn"),
    templatePreview: document.getElementById("templatePreview"),
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
    execDrawer: document.getElementById("execDrawer"),
    execDrawerMask: document.getElementById("execDrawerMask"),
    execBtn: document.getElementById("execBtn"),
    execScanBar: document.getElementById("execScanBar"),
    execScanFill: document.getElementById("execScanFill"),
  };

  /* ── 执行抽屉 / 顶栏扫描条（方案 C：进度不再占右栏） ── */
  function setExecDrawer(open) {
    if (!dom.execDrawer) return;
    dom.execDrawer.classList.toggle("open", open);
    dom.execDrawer.setAttribute("aria-hidden", open ? "false" : "true");
    if (dom.execDrawerMask) dom.execDrawerMask.classList.toggle("visible", open);
    if (open && window.StockRadar.marketDashboard && window.StockRadar.marketDashboard.closePane) {
      window.StockRadar.marketDashboard.closePane();
    }
  }

  function toggleExecDrawer() {
    if (!dom.execDrawer) return;
    setExecDrawer(!dom.execDrawer.classList.contains("open"));
  }

  function setExecRunning(running) {
    if (dom.execBtn) dom.execBtn.classList.toggle("running", running);
    if (dom.execScanBar) {
      dom.execScanBar.hidden = !running;
      if (!running && dom.execScanFill) dom.execScanFill.style.width = "0%";
    }
    // 运行时：停止按钮顶替发送按钮（同位置同尺寸，对齐主流 AI 对话交互）
    if (dom.sendBtn) dom.sendBtn.style.display = running ? "none" : "";
    if (dom.stopTaskBtn) {
      dom.stopTaskBtn.hidden = !running;
      dom.stopTaskBtn.disabled = false;
      if (!running) dom.stopTaskBtn.title = "停止当前任务（下一检查点生效）";
    }
  }

  function setExecScan(pct) {
    if (!dom.execScanBar || dom.execScanBar.hidden) return;
    const value = Math.max(2, Math.min(100, Number(pct) || 2));
    if (dom.execScanFill) dom.execScanFill.style.width = `${value}%`;
  }

  window.StockRadar.execDrawer = {
    open: () => setExecDrawer(true),
    close: () => setExecDrawer(false),
    toggle: toggleExecDrawer,
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
      btn.dataset.uuid = d.dialog_uuid;
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

      // 从持久化的 progress 事件中还原「记忆上下文」卡片与过程流（transcript）到主对话
      const nodes = Array.from(dom.messageList.children);
      msgs.forEach((m, idx) => {
        if (m.role === "assistant" && m.extra) {
          try {
            const events = JSON.parse(m.extra);
            const list = Array.isArray(events) ? events : [];
            const mc = list.find((ev) => ev.type === "memory_context") || null;
            if (mc && nodes[idx]) {
              chat.appendMemoryContext(dom.messageList, mc, nodes[idx]);
            }
            const trEntries = list.filter((ev) => ev && ev.type === "transcript" && ev.entry_id);
            if (trEntries.length && nodes[idx] && window.StockRadar.chatTranscript) {
              const ct = window.StockRadar.chatTranscript;
              ct.replay(m.message_uuid, trEntries, dom.messageList, nodes[idx]);
              // 回放同样要落终态，否则头部回退「运行中…」
              if (m.status === "completed") {
                ct.finish(m.message_uuid);
              } else if (m.status !== "streaming" && m.status !== "pending") {
                ct.freeze(m.message_uuid, m.status === "failed" ? "出错" : "已停止");
              }
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

        // 运行中恢复：SSE 不回放，先拉过程流快照（think/工具节点），再连增量（entry_id 去重）
        try {
          const snap = await api.getTaskTranscript(runningMsg.task_id);
          if (snap && Array.isArray(snap.entries) && snap.entries.length && window.StockRadar.chatTranscript) {
            window.StockRadar.chatTranscript.replay(
              runningMsg.task_id, snap.entries, dom.messageList, null);
          }
        } catch {}

        connectSSE(runningMsg.task_id);
        // 澄清反问进行中（SSE 事件不回放）：靠 getTask 的 pending 补开弹窗
        if (task.clarify) showClarifyModal(task.clarify);
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
    setExecDrawer(true);

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
    setExecRunning(true);
    setExecScan(3);

    ensureStatusPlayer();

    const es = new EventSource(`/api/tasks/${taskId}/events`);
    state.eventSource = es;

    es.addEventListener("progress", (e) => {
      try {
        const event = JSON.parse(e.data);
        tl.appendProgressEvent(dom.progressList, event);
        state.statusPlayer.enqueue(event);
        if (event.total_steps) {
          setExecScan((Number(event.step) / Number(event.total_steps)) * 100);
        }
        if (event.type === "memory_context") {
          chat.appendMemoryContext(dom.messageList, event);
        }
      } catch {}
    });

    es.addEventListener("title", (e) => {
      // 首轮 classify 生成会话标题：DB 已落库，这里同步列表项与顶栏
      try {
        const data = JSON.parse(e.data);
        if (!data || !data.dialog_uuid || !data.title) return;
        const d = state.dialogs.find((x) => x.dialog_uuid === data.dialog_uuid);
        if (d) d.title = data.title;
        const item = dom.dialogList.querySelector(`.dialog-item[data-uuid="${data.dialog_uuid}"]`);
        const titleEl = item && item.querySelector(".dialog-item-title");
        if (titleEl) titleEl.textContent = data.title;
        if (state.currentId === data.dialog_uuid) {
          dom.dialogTitle.textContent = data.title;
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

    es.addEventListener("transcript", (e) => {
      // 对话过程流：think/工具条目直接落位到过程卡（entry_id 幂等 upsert）
      try {
        const entry = JSON.parse(e.data);
        const ct = window.StockRadar.chatTranscript;
        if (ct && entry && entry.entry_id) {
          ct.upsert(state.taskId, entry, dom.messageList,
            state.statusBubble ? state.statusBubble.wrap : null);
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
        // 过程流定格：异常终止同样不能停在「运行中」
        if (window.StockRadar.chatTranscript) window.StockRadar.chatTranscript.freeze(state.taskId, "出错");
        stopStatusPlayer();
        if (state.statusBubble) {
          chat.finalizeStatusBubble(state.statusBubble, data.message || "执行出错");
          state.statusBubble.bubble.classList.add("failed");
        }
      } catch {}
      dom.taskStatus.textContent = "出错";
      dom.connectionStatus.textContent = "就绪";
      setExecRunning(false);
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
          // 过程卡定格：think 自动收起、工具行保留终态
          if (window.StockRadar.chatTranscript) window.StockRadar.chatTranscript.finish(state.taskId);
          if (state.statusPlayer) {
            await state.statusPlayer.finish(data.content || "");
          } else if (state.statusBubble) {
            await chat.typewriterStatusBubble(state.statusBubble, data.content || "");
          } else {
            chat.appendMessage(dom.messageList, "assistant", data.content || "", true);
          }

          dom.taskStatus.textContent = "完成";
          dom.connectionStatus.textContent = "就绪";
          setExecRunning(false);
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

    es.addEventListener("clarify_required", (e) => {
      try {
        const data = JSON.parse(e.data);
        showClarifyModal(data);
      } catch {}
    });

    es.addEventListener("clarify_timeout", (e) => {
      // 后端澄清等待超时：问答已按推荐选项落库，页面还开着就补渲染气泡再收弹窗
      try {
        const data = JSON.parse(e.data);
        const modal = document.getElementById("clarifyModal");
        if (modal && modal.classList.contains("open")) {
          const qText = (data.questions || []).map((q, i) => `${i + 1}. ${q.question}`).join("\n");
          const aText = (data.questions || [])
            .map((q) => (data.answers && data.answers[q.id] ? `- ${q.question}：${data.answers[q.id]}` : ""))
            .filter(Boolean).join("\n");
          chat.appendMessage(dom.messageList, "assistant", qText, false);
          chat.appendMessage(dom.messageList, "user", "补充信息：\n" + aText + "\n（超时未作答，按推荐选项继续）", false);
          chat.scrollBottom(dom.messageList);
        }
      } catch {}
      closeClarifyModal();
    });

    es.addEventListener("task_cancelled", () => {
      closeClarifyModal(); // 任务停止时澄清弹窗一并收起（否则再提交只会 404 被静默吞掉）
      // 过程流定格：运行中标灰「已终止」、think 中断收起，已产生数据保留
      if (window.StockRadar.chatTranscript) window.StockRadar.chatTranscript.freeze(state.taskId);
      setExecRunning(false);
      stopStatusPlayer();
      es.close();
      state.eventSource = null;
      state.taskId = null;
      dom.sendBtn.disabled = false;
      dom.sendBtn.classList.remove("loading");
      dom.taskStatus.textContent = "已停止";
      dom.connectionStatus.textContent = "就绪";
      refreshCurrentDialog().catch(() => {});
    });

    es.onerror = () => {
      dom.taskStatus.textContent = "连接断开";
      dom.connectionStatus.textContent = "就绪";
      setExecRunning(false);
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

  /* ── 澄清反问弹窗 ── */
  let clarifyTimer = null;   // 倒计时 interval（归零后按推荐选项自动继续）
  let clarifyEscHandler = null; // ESC 关闭（= 都不对重新描述），close 时摘除

  function closeClarifyModal() {
    if (clarifyTimer) {
      clearInterval(clarifyTimer);
      clarifyTimer = null;
    }
    if (clarifyEscHandler) {
      document.removeEventListener("keydown", clarifyEscHandler);
      clarifyEscHandler = null;
    }
    const overlay = document.getElementById("clarifyOverlay");
    const modal = document.getElementById("clarifyModal");
    const bar = document.getElementById("clarifyCountdown");
    if (overlay) {
      overlay.classList.remove("open");
      overlay.onclick = null;
    }
    if (modal) modal.classList.remove("open");
    if (bar) {
      bar.hidden = true;
      bar.classList.remove("timeout");
    }
    const rejectBtn = document.getElementById("clarifyRejectBtn");
    const submitBtn = document.getElementById("clarifySubmitBtn");
    if (rejectBtn) rejectBtn.onclick = null;
    if (submitBtn) submitBtn.onclick = null;
  }

  // 倒计时进度条：满格递减到 0 → 亮红灯 → 自动按推荐选项提交
  function startClarifyCountdown(data, onSubmit) {
    const wrap = document.getElementById("clarifyCountdown");
    const fill = document.getElementById("clarifyCountdownFill");
    const text = document.getElementById("clarifyCountdownText");
    if (!wrap || !fill || !text) return;
    const timeoutMs = Math.max(1, Number(data.timeout_s || 0) * 1000);
    if (!timeoutMs) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    wrap.classList.remove("timeout");
    fill.style.width = "100%";
    // deadline 是服务端绝对时间戳：刷新页面后倒计时不重置
    const deadline = Number(data.deadline_ms) || Date.now() + timeoutMs;
    if (clarifyTimer) clearInterval(clarifyTimer);

    const tick = () => {
      const left = deadline - Date.now();
      const pct = Math.max(0, Math.min(100, (left / timeoutMs) * 100));
      fill.style.width = pct + "%";
      if (left > 0) {
        text.textContent = `剩余 ${Math.ceil(left / 1000)} 秒，超时将按推荐选项继续`;
        return;
      }
      clearInterval(clarifyTimer);
      clarifyTimer = null;
      wrap.classList.add("timeout");
      text.textContent = "已超时，按推荐选项继续分析";
      // 停 1.5s 让用户看清红灯，再自动提交（后端有 10s 余量，不会抢先超时）
      setTimeout(onSubmit, 1500);
    };
    clarifyTimer = setInterval(tick, 200);
    tick();
  }

  function showClarifyModal(data) {
    const overlay = document.getElementById("clarifyOverlay");
    const modal = document.getElementById("clarifyModal");
    const body = document.getElementById("clarifyBody");
    if (!overlay || !modal || !body) return;
    closeClarifyModal(); // 防重复打开（SSE + 刷新恢复同时触发会挂两套监听/定时器）

    const questions = data.questions || [];
    const taskId = data.task_id;
    const originalQuestion = data.original_question || "";
    const submitted = { done: false };
    const skipped = new Set(); // 用户点了「跳过本题」的题（不注入补充分析）

    body.innerHTML = "";
    questions.forEach((q, qi) => {
      const block = document.createElement("div");
      block.className = "clarify-q";
      const title = document.createElement("div");
      title.className = "clarify-q-title";
      title.textContent = `${qi + 1}. ${q.question}`;
      block.appendChild(title);

      const unskip = () => {  // 只要动了选项/输入，跳过态即失效
        if (!skipped.has(q.id)) return;
        skipped.delete(q.id);
        block.classList.remove("skipped");
        block.querySelectorAll("input[type=radio]").forEach((r) => {
          r.checked = r.value === q.recommended;
        });
        syncSkipBtn();
      };
      const radios = [];
      (q.options || []).forEach((opt) => {
        const label = document.createElement("label");
        label.className = "clarify-opt";
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = `clarify_${q.id}`;
        radio.value = opt.value;
        if (opt.value === q.recommended) radio.checked = true;
        radio.addEventListener("change", unskip);
        const text = document.createElement("span");
        text.textContent = opt.label;
        label.appendChild(radio);
        label.appendChild(text);
        if (opt.value === q.recommended) {
          const rec = document.createElement("span");
          rec.className = "rec";
          rec.textContent = "推荐";
          label.appendChild(rec);
        }
        radios.push(radio);
        block.appendChild(label);
      });
      if (q.allow_custom !== false) {
        const input = document.createElement("input");
        input.type = "text";
        input.className = "clarify-custom";
        input.placeholder = "或自行输入…";
        input.dataset.customFor = q.id;
        input.addEventListener("input", () => { if (input.value.trim()) unskip(); });
        block.appendChild(input);
      }

      // 单题跳过：这题不答也别被推荐项顶替（后端按「未提供该信息」处理）
      const skipBtn = document.createElement("button");
      skipBtn.type = "button";
      skipBtn.className = "clarify-skip";
      const syncSkipBtn = () => {
        const on = skipped.has(q.id);
        skipBtn.textContent = on ? "已跳过 · 点此恢复" : "跳过本题";
        skipBtn.classList.toggle("on", on);
      };
      skipBtn.onclick = () => {
        if (skipped.has(q.id)) {
          unskip();
          return;
        }
        skipped.add(q.id);
        block.classList.add("skipped");
        radios.forEach((r) => { r.checked = false; });
        const custom = block.querySelector(".clarify-custom");
        if (custom) custom.value = "";
        syncSkipBtn();
      };
      syncSkipBtn();
      block.appendChild(skipBtn);
      body.appendChild(block);
    });

    overlay.classList.add("open");
    modal.classList.add("open");

    const rejectBtn = document.getElementById("clarifyRejectBtn");
    const submitBtn = document.getElementById("clarifySubmitBtn");

    function submitAnswers() {
      if (submitted.done) return;
      submitted.done = true;
      const answers = {};
      const skippedIds = [];
      questions.forEach((q) => {
        if (skipped.has(q.id)) {
          skippedIds.push(q.id);
          return;
        }
        const custom = body.querySelector(`input[data-custom-for="${q.id}"]`);
        const customVal = custom ? custom.value.trim() : "";
        if (customVal) {
          answers[q.id] = customVal;
          return;
        }
        const checked = body.querySelector(`input[name="clarify_${q.id}"]:checked`);
        answers[q.id] = checked ? checked.value : (q.recommended || "");
      });
      // 本地先渲染「反问 + 作答」，保持 问题→反问→作答→回答 的对话顺序
      // （服务端已按完成时间落库，刷新后以 DB 为准，不会重复）
      try {
        const qText = questions.map((q, i) => `${i + 1}. ${q.question}`).join("\n");
        // 展示用 label（LLM 可能把 value 写成英文 key），自定义输入原样显示
        const labelOf = (q, v) => {
          const hit = (q.options || []).find((o) => o.value === v);
          return hit ? hit.label : v;
        };
        const aText = questions
          .map((q) => {
            if (skipped.has(q.id)) return `- ${q.question}：（跳过）`;
            return answers[q.id] ? `- ${q.question}：${labelOf(q, answers[q.id])}` : "";
          })
          .filter(Boolean)
          .join("\n");
        chat.appendMessage(dom.messageList, "assistant", qText, false);
        chat.appendMessage(dom.messageList, "user", "补充信息：\n" + aText, false);
        chat.scrollBottom(dom.messageList);
      } catch {}
      api.clarifyDecision(taskId, { answers, skipped: skippedIds }).catch(function () {});
      closeClarifyModal();
    }

    function rejectClarify() {
      if (submitted.done) return;
      submitted.done = true;
      api.clarifyDecision(taskId, { rejected: true }).catch(function () {});
      closeClarifyModal();
      try {
        chat.appendMessage(dom.messageList, "user", "（都不对，我重新描述问题）", false);
        chat.scrollBottom(dom.messageList);
      } catch {}
      // 回填原问题供编辑（反驳语义：终止本轮、用户重新描述）
      if (originalQuestion && dom.messageInput) {
        dom.messageInput.value = originalQuestion;
        dom.messageInput.focus();
      }
    }

    rejectBtn.onclick = rejectClarify;
    submitBtn.onclick = submitAnswers;
    // ESC / 点遮罩 = 都不对（弹窗不能只有按钮才能关）
    overlay.onclick = rejectClarify;
    clarifyEscHandler = (e) => { if (e.key === "Escape") { e.preventDefault(); rejectClarify(); } };
    document.addEventListener("keydown", clarifyEscHandler);
    startClarifyCountdown(data, submitAnswers);
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
      await _onSendFailed(text, e);
    }
  }

  // 发送失败统一出口：初始化等待 → 就绪自动重发；其余给出「重试 / 回填输入框」
  async function _onSendFailed(text, e) {
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
          _appendFailedBubble(text, "请求失败: " + e2.message);
        }
      } else {
        const msg = blocker === "memory"
          ? "记忆系统初始化超时，请稍后重试"
          : "Agent 初始化超时，请稍后重试";
        _appendFailedBubble(text, msg);
      }
    } else {
      _appendFailedBubble(text, "请求失败: " + e.message);
    }
    dom.sendBtn.disabled = false;
    dom.sendBtn.classList.remove("loading");
  }

  function _appendFailedBubble(text, msg) {
    const bubble = chat.appendMessage(dom.messageList, "assistant", msg, true);
    bubble.classList.add("failed");
    _attachFailedActions(bubble, text);
    return bubble;
  }

  // 失败气泡补出口：输入框在发送时已清空，不给重试就得手抄重打
  function _attachFailedActions(bubble, text) {
    const bar = document.createElement("div");
    bar.className = "failed-actions";

    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "failed-retry";
    retry.textContent = "重试";
    retry.onclick = async () => {
      bubble.remove();
      dom.sendBtn.disabled = true;
      dom.sendBtn.classList.add("loading");
      try {
        await _deliverMessage(text);
      } catch (e) {
        await _onSendFailed(text, e);
      }
    };

    const refill = document.createElement("button");
    refill.type = "button";
    refill.className = "failed-refill";
    refill.textContent = "回填输入框";
    refill.onclick = () => {
      dom.messageInput.value = text;
      dom.messageInput.focus();
      dom.messageInput.dispatchEvent(new Event("input"));
    };

    bar.appendChild(retry);
    bar.appendChild(refill);
    bubble.appendChild(bar);
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

  /* ── 面板注册护栏：单个面板初始化失败不拖垮全局（dialog 加载/agent mode/其余面板照常） ── */
  function safeSetup(name, setupFn) {
    try {
      setupFn();
    } catch (e) {
      console.error(`[app] ${name} 初始化失败（已隔离，不影响其他模块）:`, e);
    }
  }

  /* ── 自选股 ── */
  safeSetup("WatchlistPanel", () => {
    const watchlistPanel = new window.StockRadar.WatchlistPanel();
    document.getElementById("watchlistBtn").onclick = () => watchlistPanel.open();
  });

  /* ── 盯盘告警 ── */
  safeSetup("MarketMonitorPanel", () => {
    const marketMonitorPanel = new window.StockRadar.MarketMonitorPanel();
    document.getElementById("marketMonitorBtn").onclick = () => marketMonitorPanel.open();
  });

  /* ── 执行详情抽屉（方案 C：进度移出右栏） ── */
  safeSetup("ExecDrawer", () => {
    if (dom.execBtn) dom.execBtn.onclick = toggleExecDrawer;
    if (dom.stopTaskBtn) {
      dom.stopTaskBtn.onclick = async () => {
        const taskId = state.taskId;
        if (!taskId || dom.stopTaskBtn.disabled) return;
        dom.stopTaskBtn.disabled = true;
        dom.stopTaskBtn.title = "正在停止…（下一次检查点生效）";
        try {
          await api.cancelTask(taskId);
        } catch (e) {
          dom.stopTaskBtn.disabled = false;
          dom.stopTaskBtn.title = "停止当前任务（下一检查点生效）";
        }
      };
    }
    if (dom.execDrawerMask) dom.execDrawerMask.onclick = () => setExecDrawer(false);
    const closeBtn = document.getElementById("execDrawerClose");
    if (closeBtn) closeBtn.onclick = () => setExecDrawer(false);
  });

  /* ── 市场情绪面板（方案 C 一期） ── */
  safeSetup("MarketDashboard", () => {
    if (window.StockRadar.marketDashboard) window.StockRadar.marketDashboard.init();
  });

  /* ── 持仓账本 ── */
  safeSetup("PortfolioPanel", () => {
    const portfolioPanel = new window.StockRadar.PortfolioPanel();
    document.getElementById("portfolioBtn").onclick = () => portfolioPanel.open();
  });

  /* ── 质量面板（A7：顶栏「更多」入口，弹窗 DOM 由 QualityPanel 自建） ── */
  safeSetup("QualityPanel", () => {
    const qualityBtn = document.getElementById("qualityPanelBtn");
    if (qualityBtn && window.StockRadar.QualityPanel) {
      const qualityPanel = new window.StockRadar.QualityPanel();
      qualityBtn.onclick = () => qualityPanel.open();
    }
  });

  /* ── K 线图 ── */
  safeSetup("KlinePanel", () => {
    const klinePanel = new window.StockRadar.KlinePanel();
    document.getElementById("klineBtn").onclick = () => klinePanel.open();
    // 暴露 openKlineChart 函数，供自选股等模块调用；opts.markRange 支持 AI 信号评测跳转标记
    window.openKlineChart = (code, opts) => klinePanel.open(code, opts);
  });
  if (!window.openKlineChart) {
    window.openKlineChart = () => console.warn("[app] KlinePanel 未初始化，无法打开 K 线图");
  }

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
    if (dom.sendBtn && dom.sendBtn.disabled) return;   // agent 预热中禁止发送
    sendMessage(dom.messageInput.value);
  };

  dom.messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      // 中文输入法选词回车不算发送（isComposing / 229 = 输入法事件），否则发半截消息
      if (e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      if (dom.sendBtn && dom.sendBtn.disabled) return;  // agent 预热中禁止发送
      sendMessage(dom.messageInput.value);
    }
  });

  dom.messageInput.addEventListener("input", () => {
    dom.messageInput.style.height = "auto";
    dom.messageInput.style.height = Math.min(dom.messageInput.scrollHeight, 160) + "px";
    _scheduleTemplatePreview(dom.messageInput.value);
  });

  /* ── 模板路由预览：输入时展示 TOP5（TOP1 高亮 + 命中词）──
     让用户提前发现「词没匹配到想要的模板」，据此补词（如加"持仓"）。
     纯关键词打分，无 LLM，成本可忽略；仅当前输入，不含追问继承。 */
  let _tpTimer = null;
  let _tpSeq = 0;
  function _tpEsc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function _scheduleTemplatePreview(text) {
    if (_tpTimer) clearTimeout(_tpTimer);
    _tpTimer = setTimeout(() => _loadTemplatePreview(text), 300);
  }
  async function _loadTemplatePreview(text) {
    const box = dom.templatePreview;
    if (!box) return;
    const q = (text || "").trim();
    if (q.length < 2) { box.hidden = true; box.innerHTML = ""; return; }
    const seq = ++_tpSeq;
    try {
      const res = await fetch("/api/report-templates/route-preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: q }),
      });
      if (!res.ok) throw new Error("http " + res.status);
      const data = await res.json();
      if (seq !== _tpSeq) return;   // 丢弃过期响应
      _renderTemplatePreview(data);
    } catch (e) {
      if (seq === _tpSeq) { box.hidden = true; box.innerHTML = ""; }
    }
  }
  function _renderTemplatePreview(data) {
    const box = dom.templatePreview;
    const ranked = (data && data.ranked) || [];
    if (!box || !ranked.length) { if (box) { box.hidden = true; box.innerHTML = ""; } return; }
    const winner = data.winner;
    const chips = ranked.map((t) => {
      const isTop = t.template_id === winner;
      const kws = (t.matched_keywords || []).map((k) => `${k[0]}(${k[1]})`).join(" ");
      const title = kws ? `命中词：${kws}` : "无命中词（不应选中）";
      return `<span class="tp-chip${isTop ? " top" : ""}" title="${_tpEsc(title)}">`
        + `${isTop ? "✓ " : ""}${_tpEsc(t.name)}`
        + `${t.score ? `<em>${t.score}</em>` : ""}</span>`;
    }).join("");
    box.innerHTML = `<span class="tp-label" title="当前输入将匹配到的报告模板（追问轮会沿用上一轮）">模板匹配</span>${chips}`;
    box.hidden = false;
  }

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

  /* ── 启动就绪进度条：页面立即可用；agent/langgraph 预热完成后满条淡出 ──
     前端先渲染页面（K线/自选/回测等与 agent 无关功能即刻可用），
     agent 相关（对话发送）在进度满前禁用——进度值由 /api/health 轮询驱动。 */
  function startBootMonitor() {
    const overlay = document.getElementById("bootOverlay");
    const fill = document.getElementById("bootFill");
    const statusEl = document.getElementById("bootStatus");
    const hint = document.getElementById("composerReadyHint");
    if (!overlay || !fill || !statusEl) return;

    const sendBtn = dom.sendBtn;
    const sendTitleBackup = sendBtn ? sendBtn.title : "";
    const setSendDisabled = (disabled) => {
      if (sendBtn) {
        sendBtn.disabled = disabled;
        sendBtn.title = disabled ? "Agent 引擎准备中，暂不可发送…" : sendTitleBackup;
      }
      if (hint) hint.hidden = !disabled;
    };
    setSendDisabled(true);

    let finished = false;
    const tick = async () => {
      if (finished) return;
      let h = null;
      try { h = await api.getHealth(); } catch (e) { /* 服务尚未接受请求，继续轮询 */ }
      if (!h) {
        fill.style.width = "8%";
        statusEl.textContent = "正在连接服务…";
        setTimeout(tick, 700);
        return;
      }
      let pct, text;
      if (h.agent_ready) {
        pct = 100; text = "全部就绪，欢迎使用";
      } else if (h.readiness === "t2_full" || h.readiness === "t1_basic") {
        pct = 55;
        text = h.memory_warming ? "基础服务就绪，记忆引擎加载中…" : "基础服务就绪，Agent 引擎加载中…";
      } else {
        pct = 25; text = "正在启动基础服务…";
      }
      fill.style.width = pct + "%";
      statusEl.textContent = text;
      if (pct >= 100) {
        finished = true;
        setSendDisabled(false);
        overlay.classList.add("done");
        setTimeout(() => { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 600);
        return;
      }
      setTimeout(tick, 800);
    };
    setTimeout(tick, 200);
  }

  async function init() {
    _applyUiConfig();
    startBootMonitor();
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
