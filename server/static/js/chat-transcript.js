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
  // 被 splitTurn 分段出去的卡（DOM 保留在列表里，但不再接受新条目）；
  // finish/freeze 仍要遍历它们，否则卡头永远停在「运行中…」
  const closed = [];
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  /* ── 打字机（全局单飞 + 截止时间收敛）
     ⚠️ 必须全局单飞：每个 think 条目各起一个循环会导致「同一时刻多条同时展开、
     且越落越远」——落后量只增不减，永远追不上 Agent 实际速度。
     实现：① typing 单例（同时只有一个 rec 在打）；② 代际令牌 typing.gen 作废旧循环；
     ③ 每帧按「剩余字数 / 剩余帧数」重算步长，落后时自动加速、文本变长时同样收敛；
     ④ 每帧读 rec.entry.text（最新值）而非调用时快照，否则打字途中到达的更长文本
        会因 rec.typing 被跳过、旧循环结束后无新事件 → 尾部永久丢失。
     节奏常数：_TYPE_MS_PER_CHAR × 字数，clamp 到 [_TYPE_MIN_MS, _TYPE_MAX_MS]。 */
  const _TYPE_FRAME_MS = 14;
  const _TYPE_MS_PER_CHAR = 5;
  const _TYPE_MIN_MS = 240;
  // ⚠️ 预算是「计划时长」：setTimeout 有 ~1-2ms/帧调度开销，实际墙钟会略超，
  // 故上限留出余量（实测 1200 字计划 1.2s / 实际 ~1.6s），别写成名义值。
  const _TYPE_MAX_MS = 1200;

  const typing = { rec: null, gen: 0, timer: null, onTick: null };

  function _stopTyping(rec) {
    if (typing.timer) { clearTimeout(typing.timer); typing.timer = null; }
    if (rec) rec.typing = false;
    if (typing.rec === rec) typing.rec = null;
  }

  function _typewrite(rec, text) {
    // 新代际接管：先让正在打的旧块立刻补齐全文（不留残余循环）
    if (typing.rec && typing.rec !== rec) _flushTyping(typing.rec);
    const myGen = ++typing.gen;
    if (typing.timer) { clearTimeout(typing.timer); typing.timer = null; }

    const target = rec.el.querySelector(".tr-think-text");
    // 续打而非清空重打：同一 entry_id 的 reasoning 可能继续增长，
    // 清空会让已展开的内容闪一下（用户看到内容回退）
    const shown = target.textContent || "";
    let i = shown && text.indexOf(shown) === 0 ? shown.length : 0;
    if (!i) target.textContent = "";

    const frames = Math.max(1, Math.round(
      Math.min(_TYPE_MAX_MS, Math.max(_TYPE_MIN_MS, text.length * _TYPE_MS_PER_CHAR)) / _TYPE_FRAME_MS
    ));
    let frameNo = 0;
    rec.typing = true;
    typing.rec = rec;

    function tick() {
      if (myGen !== typing.gen) return;              // 被新代际接管
      const cur = rec.el.querySelector(".tr-think-text") || target;
      const full = (rec.entry && rec.entry.text) || text;
      if (i < full.length) {
        frameNo += 1;
        const left = Math.max(1, frames - frameNo);
        i = Math.min(full.length, i + Math.max(1, Math.ceil((full.length - i) / left)));
        cur.textContent = full.slice(0, i);
        if (typing.onTick) typing.onTick();
      }
      if (i < full.length) {
        typing.timer = setTimeout(() => { typing.timer = null; tick(); }, _TYPE_FRAME_MS);
        return;
      }
      _stopTyping(rec);
    }
    tick();
  }

  // 被打断：立刻把剩余内容一次性补齐（下一条思考开始 / 快照回放 / 任务收尾）
  function _flushTyping(rec) {
    if (!rec) return;
    _stopTyping(rec);
    const full = (rec.entry && rec.entry.text) || "";
    const target = rec.el.querySelector(".tr-think-text");
    if (target && target.textContent.length < full.length) target.textContent = full;
  }

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
      <div class="tcard-hd" role="button" tabindex="0" aria-expanded="true" title="点击收起 / 展开执行过程">
        <span class="tcard-caret">▾</span>
        <span class="tcard-live"></span>
        <span class="tcard-title">执行过程</span>
        <span class="tcard-st">运行中…</span>
        <span class="tcard-hint">点击展开</span>
      </div>
      <div class="tcard-line"></div>`;
    if (anchorEl && anchorEl.parentNode === container) {
      container.insertBefore(card, anchorEl);
    } else {
      container.appendChild(card);
    }
    t = {
      key: String(turnKey), card, nodes: new Map(), count: 0,
      done: false, frozen: false, segDone: false,
      collapsed: false, userToggled: false,
    };
    turns.set(turnKey, t);
    // 卡头即开关：整卡收起 / 展开（点击 + 键盘）
    const hd = card.querySelector(".tcard-hd");
    const toggle = () => _toggleCollapse(t);
    hd.addEventListener("click", toggle);
    hd.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
    });
    return t;
  }

  /* ── 整卡收起 / 展开 ──
     痛点（2026-10-26 用户反馈）：一轮对话 80+ 节点，整张卡几百像素，
     多轮下来「一次对话拉得老长」，报告反倒要翻很久才看到。
     口径：
     - 卡头就是开关（点击 / Enter / 空格），收起后只留一行标题 + 节点数；
     - 完成（done）与澄清分段（seg）时**自动收起**，用户点一下即可展开；
     - 停止 / 出错（frozen）**不自动收起** —— 失败现场要能直接看到最后一步；
     - 用户在本卡上手动开合过（userToggled）就不再自动收起，尊重手选。 */
  function _setCollapsed(t, collapsed) {
    t.collapsed = !!collapsed;
    t.card.classList.toggle("collapsed", t.collapsed);
    const hd = t.card.querySelector(".tcard-hd");
    if (hd) hd.setAttribute("aria-expanded", t.collapsed ? "false" : "true");
    if (!t.collapsed) return;
    // 收起时把打字机一次性补齐：隐藏节点里继续逐帧写字毫无意义，
    // 且展开瞬间会看到半截推理（用户以为模型只想了这么多）。
    t.nodes.forEach((rec, id) => { if (id.startsWith("t:")) _flushTyping(rec); });
  }

  function _toggleCollapse(t) {
    t.userToggled = true;
    _setCollapsed(t, !t.collapsed);
    // 卡高变化后修正滚动位置：跟随开启时仍贴底，暂停时不动用户的阅读位置
    const follow = window.StockRadar && window.StockRadar.chatFollow;
    if (follow) follow.pin();
  }

  /* mode: null=运行中 / done=完成 / frozen=停止 / seg=已分段（澄清后不再收节点） */
  function _setStatus(t, text, mode) {
    t.card.querySelector(".tcard-st").textContent = text;
    t.card.classList.toggle("done", mode === "done");
    t.card.classList.toggle("frozen", mode === "frozen");
    t.card.classList.toggle("seg", mode === "seg");
    if (mode) t.segDone = true;
    const live = t.card.querySelector(".tcard-live");
    if (mode) live.style.display = "none";
    else live.style.display = "";
    // 跑完就收成一行（失败/停止不自动收，见 _setCollapsed 上方口径）
    if ((mode === "done" || mode === "seg") && !t.userToggled) _setCollapsed(t, true);
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
        <div class="tr-body">
          <div class="tr-think-text"></div>
          <div class="tr-out" hidden>
            <div class="tr-out-hd"><span class="tr-out-caret">▸</span>输出正文<span class="tr-out-n"></span></div>
            <div class="tr-out-pre"></div>
          </div>
        </div>
        <div class="tr-note"></div>`;
      t.card.querySelector(".tcard-line").appendChild(el);
      rec = { el, entry, manualOpen: false, typing: false, outOpen: false, outDone: false };
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
      // 输出正文默认收起（多数是 JSON 控制载荷，全展开会把过程卡撑爆）
      const outHd = el.querySelector(".tr-out-hd");
      outHd.addEventListener("click", (ev) => {
        ev.stopPropagation();
        rec.outOpen = !rec.outOpen;
        el.querySelector(".tr-out").classList.toggle("open", rec.outOpen);
      });
    }
    rec.entry = entry;
    const el = rec.el;
    const text = entry.text || "";
    const content = entry.content || "";
    // 纯正文轮（无 reasoning）没有「思考」可打，节点语义不同
    if (!text && content && first) {
      el.querySelector(".tr-icon").textContent = "📝";
      el.querySelector(".tr-name").textContent = "输出正文";
    }
    _renderOut(rec, entry);
    if (live && !t.done && !t.frozen) {
      // 执行中：展开 + 打字机（前端模拟，LLM 非 token 流）
      el.querySelector(".tr-body").style.display = "block";
      if (!rec.typing && el.querySelector(".tr-think-text").textContent.length < text.length) {
        // 「同时只展开最新一条」：上一条思考立刻定格收尾（含补齐未打完的内容）
        _settleOthers(t, entry.entry_id);
        _typewrite(rec, text);
      }
    } else {
      _flushTyping(rec);
      el.querySelector(".tr-think-text").textContent = text;
      _settleThink(t, rec, "");
    }
    return rec;
  }

  /* ── AI 正文（messages.content）：markdown 或 JSON 控制载荷 ──
     幂等：内容未变不重建 DOM（upsert 会以 ~1.5s 轮询重复推同一条目）。 */
  function _renderOut(rec, entry) {
    const content = entry.content || "";
    const box = rec.el.querySelector(".tr-out");
    if (!content) {
      if (!rec.outDone) { box.hidden = true; rec.outDone = true; }
      return;
    }
    if (!rec.outDone) {
      const pre = rec.el.querySelector(".tr-out-pre");
      pre.textContent = _fmtOut(content);
      const n = rec.el.querySelector(".tr-out-n");
      n.textContent = ` ${content.length} 字${entry.content_truncated ? "（已截断）" : ""}`;
      box.hidden = false;
      rec.outDone = true;
    }
  }

  // JSON 美化 + 去掉 ```json 围栏；非 JSON 原样（markdown 走 pre-wrap 展示）
  function _fmtOut(s) {
    const raw = String(s || "").trim();
    const fenced = raw.match(/^```(?:json)?\s*\n([\s\S]*?)\n?```$/i);
    const body = fenced ? fenced[1] : raw;
    try {
      return JSON.stringify(JSON.parse(body), null, 2);
    } catch {
      return body;
    }
  }

  // 让本轮内其它仍在打字/仍展开的 think 块收尾（manualOpen 的不动，用户优先）
  function _settleOthers(t, exceptId) {
    t.nodes.forEach((rec, id) => {
      if (id === exceptId || !id.startsWith("t:")) return;
      if (rec.typing || rec.el.classList.contains("run")) {
        _settleThink(t, rec, "▸ 下一轮思考已开始，这里先收起了");
      }
    });
  }

  function _settleThink(t, rec, note) {
    const el = rec.el;
    // 收尾先停打字机：光改状态位只能让循环「下一帧」退出，挂起的 setTimeout 会留着；
    // _stopTyping 直接 clearTimeout，不留残余协程。
    const wasBusy = rec.typing || el.classList.contains("run");
    _stopTyping(rec);
    const entry = rec.entry || {};
    const text = entry.text || "";
    const content = entry.content || "";
    const textEl = el.querySelector(".tr-think-text");
    // ⚠️ 必须无条件写全文：被「下一轮思考」挤下时往往停在打字中途，
    // 只收起不补齐会让展开后看到的是半截推理（用户以为模型只想了这么多）。
    textEl.textContent = text;
    if (!wasBusy) {
      // 已定过终态的节点：只补齐文本，不覆盖既有收起原因
      return;
    }
    const bodyEl = el.querySelector(".tr-body");
    const noteEl = el.querySelector(".tr-note");
    const onlyContent = !text && !!content;
    el.classList.remove("run");
    el.classList.add("ok");
    // 字数徽标：有思考记思考字数，纯正文轮记正文字数（否则显示「0 字」）
    const chars = text.length || content.length;
    const unit = onlyContent ? "正文" : "字";
    el.querySelector(".tr-name").textContent = onlyContent ? "输出正文" : "思考过程";
    el.querySelector(".tr-sts").innerHTML = `<span class="tr-badge">${chars} ${unit}</span>`;
    if (!rec.manualOpen) {
      bodyEl.style.display = "none";
      const preview = (text || content).slice(0, 24);
      noteEl.textContent = note || `▸ 已自动收起（点击展开${preview ? `，${preview}…` : ""}）`;
      noteEl.style.display = "";
    } else {
      // 用户手动展开过 → 保持展开，不再自动收起
      bodyEl.style.display = "block";
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
    // 滚动交给跟随控制器统一管（follow 关闭时不劫持用户的阅读位置）
    const follow = window.StockRadar && window.StockRadar.chatFollow;
    if (follow) follow.pin();
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

  /* ── 同轮分段（澄清反问用）────────────────────────────────────────
     问题：过程卡按 turnKey 唯一，_ensureTurn 复用同一张卡；澄清问答的两个气泡
     追加到列表**末尾**后，卡片（连同锚定的状态气泡）仍在它们上方，
     于是「反问之后的继续执行」被画到反问上方 —— 顺序反了。

     修法：提交/超时提交反问后调 splitTurn —— 当前卡定格收尾并移出 turns，
     下一次 upsert 在列表末尾（anchorEl=null）另起一张新卡。

     ⚠️ 分段出去的卡必须保留，否则 finish/freeze 找不到它们 → 卡头永远停在「运行中…」。
     故 closed 里留存引用，finish/freeze/replay 都要一并遍历。 */
  function splitTurn(turnKey) {
    const t = turns.get(turnKey);
    if (!t) return false;
    t.nodes.forEach((rec, id) => {
      if (id.startsWith("t:")) _settleThink(t, rec, "▸ 已澄清，本段过程结束");
    });
    // 头部定格：不再有本段新增节点，但仍是「运行中」大流程的一部分
    _setStatus(t, `已澄清 · ${t.count} 节点`, "seg");
    turns.delete(turnKey);
    closed.push(t);
    return true;
  }

  function _allTurns(turnKey) {
    const out = [];
    const t = turns.get(turnKey);
    if (t) out.push(t);
    closed.forEach((c) => { if (c.key === turnKey) out.push(c); });
    return out;
  }

  function finish(turnKey) {
    _allTurns(turnKey).forEach((t) => {
      t.done = true;
      if (!t.segDone) _setStatus(t, `完成 · ${t.count} 节点`, "done");
      t.nodes.forEach((rec, id) => {
        if (id.startsWith("t:")) _settleThink(t, rec, "");
      });
    });
  }

  function freeze(turnKey, label) {
    _allTurns(turnKey).forEach((t) => {
      t.frozen = true;
      if (!t.segDone) _setStatus(t, `${label || "已停止"} · ${t.count} 节点`, "frozen");
      t.nodes.forEach((rec, id) => {
        if (id.startsWith("t:")) {
          _settleThink(t, rec, "▸ 思考被终止（已保留已产生的内容）");
        } else if (rec.el.classList.contains("run")) {
          rec.el.classList.remove("run");
          rec.el.classList.add("frozen");
          rec.el.querySelector(".tr-sts").innerHTML = `<span class="tr-badge frozen">已终止</span>`;
        }
      });
    });
  }

  function reset() {
    // 切换对话/会话：必须停掉在飞的打字机，否则旧循环继续往已卸载的节点写文本
    _stopTyping(typing.rec);
    typing.gen += 1;
    turns.clear();
    closed.length = 0;
  }

  // 打字机每帧回调（跟随滚动驱动），由 chat-follow.js 接管
  function setOnTick(fn) {
    typing.onTick = typeof fn === "function" ? fn : null;
  }

  return { upsert, replay, finish, freeze, reset, splitTurn, setOnTick };
})();
