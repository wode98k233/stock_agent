/* ── render ── */

function render() {
  renderRunList();
  renderTraceView();
}

function renderRunList() {
  const el = document.getElementById('runList');
  const runs = getFilteredRuns();
  if (!runs.length) {
    el.innerHTML = '<div style="padding:20px;color:var(--muted);text-align:center;font-size:10px">NO DATA</div>';
    return;
  }
  el.innerHTML = runs.map(r => `
    <div class="run-item ${r.id === S.selectedId ? 'active' : ''}" onclick="selectRun('${r.id}')">
      <div class="run-head">
        <span class="run-agent">${esc(r.agent_name || '?')}</span>
        <span class="badge badge-${r.status}">${r.status}</span>
      </div>
      <div class="run-id">${(r.id||'').slice(0,10)}...</div>
      <div class="run-meta">
        <span>${fmtDur(r.duration_ms)}</span>
        <span>${fmtTime(r.created_at)}</span>
      </div>
    </div>
  `).join('');
}

async function selectRun(id) {
  S.selectedId = id;
  renderRunList();
  let run = S.runs.find(r => r.id === id);
  if (S.apiBase && (!run.steps || !run.steps.length)) {
    const detail = await loadRunDetailAPI(id);
    if (detail) {
      const idx = S.runs.findIndex(r => r.id === id);
      if (idx >= 0) S.runs[idx] = detail;
      run = detail;
    }
  }
  renderTraceView();
}

function extractTokenData(steps) {
  const entries = [];
  let cumIn = 0, cumOut = 0, cumCached = 0, cumReasoning = 0, cumCacheRead = 0, cumCacheCreation = 0;
  for (const s of steps) {
    let inT = 0, outT = 0, totalT = 0, cachedT = 0, reasoningT = 0, cacheReadT = 0, cacheCreationT = 0;
    if (s.extra) {
      try {
        const extra = typeof s.extra === 'string' ? JSON.parse(s.extra) : s.extra;
        if (extra.token_usage) {
          inT = extra.token_usage.prompt_tokens || extra.token_usage.input_tokens || 0;
          outT = extra.token_usage.completion_tokens || extra.token_usage.output_tokens || 0;
          totalT = extra.token_usage.total_tokens || (inT + outT);
          cachedT = extra.token_usage.cached_tokens || 0;
          reasoningT = extra.token_usage.reasoning_tokens || 0;
          cacheReadT = extra.token_usage.cache_read_input_tokens || 0;
          cacheCreationT = extra.token_usage.cache_creation_input_tokens || 0;
        }
      } catch(e) {}
    }
    cumIn += inT;
    cumOut += outT;
    cumCached += cachedT;
    cumReasoning += reasoningT;
    cumCacheRead += cacheReadT;
    cumCacheCreation += cacheCreationT;
    entries.push({ step: s, inTokens: inT, outTokens: outT, totalTokens: totalT, cachedTokens: cachedT, reasoningTokens: reasoningT, cacheReadTokens: cacheReadT, cacheCreationTokens: cacheCreationT, cumIn, cumOut, cumTotal: cumIn + cumOut, cumCached, cumReasoning, cumCacheRead, cumCacheCreation });
  }
  return entries;
}

function extractToolHealth(steps) {
  const tools = {};
  for (const s of steps) {
    if (s.step_type !== 'tool') continue;
    const name = s.step_name || 'unknown';
    if (!tools[name]) tools[name] = { name, calls: 0, errors: 0, totalMs: 0 };
    tools[name].calls++;
    if (s.status === 'error') tools[name].errors++;
    if (s.duration_ms) tools[name].totalMs += s.duration_ms;
  }
  return Object.values(tools).sort((a, b) => b.calls - a.calls);
}

function getStepLabel(step) {
  if (step.extra) {
    try {
      const extra = typeof step.extra === 'string' ? JSON.parse(step.extra) : step.extra;
      if (extra.label) return extra.label;
    } catch(e) {}
  }
  return null;
}

function buildTree(steps) {
  const byId = {}, roots = [];
  for (const s of steps) byId[s.event_run_id] = { step: s, children: [] };
  for (const s of steps) {
    const rid = s.event_run_id;
    const prid = s.parent_run_id;
    if (prid && byId[prid]) byId[prid].children.push(byId[rid]);
    else roots.push(byId[rid]);
  }
  for (const node of Object.values(byId))
    node.children.sort((a, b) => (a.step.started_at || '').localeCompare(b.step.started_at || ''));
  roots.sort((a, b) => (a.step.started_at || '').localeCompare(b.step.started_at || ''));
  return roots;
}

function openDrawer(step) {
  S.selectedStep = step;
  S.drawerTab = 'overview';
  document.querySelectorAll('.drawer-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === 'overview');
  });
  document.getElementById('detailDrawer').classList.add('open');
  const icon = TYPE_ICONS[step.step_type] || '·';
  const label = getStepLabel(step);
  document.getElementById('drawerTitle').innerHTML = `${icon} ${esc(step.step_name || '?')}${label ? ` <span class="step-label" style="margin-left:6px">${esc(label)}</span>` : ''}`;
  renderDrawerContent();
}

function renderDrawerMessage(m, dupFingerprints) {
  const role = m.role || 'unknown';
  const c = (m.content || '').trim();
  const isDup = c.length >= 30 && dupFingerprints.has(contentFingerprint(c, 200));
  const withinDups = getWithinMsgDups(c);
  const wsDupCount = withinDups.size;

  const badges = [];
  if (isDup) badges.push('<span class="dup-inline-badge">消息重复</span>');
  if (wsDupCount > 0) badges.push(`<span class="dup-inline-ws-badge" title="此消息内有 ${wsDupCount} 个段落重复出现">段内×${wsDupCount}</span>`);

  return `<div class="msg ${isDup ? 'msg-dup' : ''}">
    <div class="msg-role msg-role-${role}">${role}${badges.join('')}</div>
    <div class="msg-content">${renderFormattedContent(m.content || '')}</div>
    ${m.reasoning ? `<details class="msg-reasoning"><summary>💭 推理过程</summary><div class="msg-reasoning-content">${renderFormattedContent(m.reasoning)}</div></details>` : ''}
    ${m.tool_calls ? `<div class="msg-tool-calls">${renderFormattedContent(m.tool_calls)}</div>` : ''}
  </div>`;
}

function parseMaybeJSON(raw) {
  if (!raw) return null;
  if (typeof raw === 'object') return raw;
  try { return JSON.parse(String(raw)); } catch(e) { return null; }
}

function normalizeMessageContent(content) {
  if (content === null || content === undefined) return '';
  if (typeof content === 'string') return content.trim();
  return JSON.stringify(content);
}

function messageSignature(m) {
  return [
    String(m.role || '').toLowerCase(),
    normalizeMessageContent(m.content),
    m.tool_call_id || '',
    m.tool_calls || ''
  ].join('\n---\n');
}

function outputMessageSignatures(step) {
  const out = parseMaybeJSON(step.output);
  const items = Array.isArray(out) ? out : (out ? [out] : []);
  return new Set(items
    .filter(item => item && typeof item === 'object')
    .map(item => messageSignature(item)));
}

function splitMessagesByStepOutput(step) {
  const msgs = step.messages || [];
  const out = parseMaybeJSON(step.output);
  const outputItems = (Array.isArray(out) ? out : (out ? [out] : []))
    .filter(item => item && typeof item === 'object')
    .map(item => messageSignature(item));
  const outputIndexes = new Set();
  let outIdx = outputItems.length - 1;

  for (let i = msgs.length - 1; i >= 0 && outIdx >= 0; i--) {
    if (messageSignature(msgs[i]) === outputItems[outIdx]) {
      outputIndexes.add(i);
      outIdx -= 1;
    }
  }

  const inputMsgs = [];
  const outputMsgs = [];
  msgs.forEach((m, idx) => {
    if (outputIndexes.has(idx)) outputMsgs.push(m);
    else inputMsgs.push(m);
  });
  return { inputMsgs, outputMsgs };
}

function renderDrawerMessagesContent(step) {
  const msgs = step.messages || [];
  if (!msgs.length) {
    return '<div style="color:var(--muted);text-align:center;padding-top:40px;font-size:10px">非 LLM 节点无消息记录</div>';
  }

  const run = S.runs.find(r => r.id === S.selectedId);
  const steps = run ? run.steps || [] : [];
  const ctxAnalysis = analyzeContext(steps);
  const dupFingerprints = new Set(ctxAnalysis.dupMessages.map(d => d.fingerprint));
  const { inputMsgs, outputMsgs } = splitMessagesByStepOutput(step);

  let html = '<div class="drawer-section drawer-message-section"><div class="drawer-section-title">LLM 消息</div>';

  if (inputMsgs.length) {
    html += '<div class="msg-group msg-group-layer-input">';
    html += '<div class="msg-group-header msg-group-input"><span class="msg-group-title">输入上下文</span><span class="msg-group-hint">prompt messages</span><span class="msg-group-count">' + inputMsgs.length + '</span></div>';
    html += '<div class="msg-group-body msg-group-body-nested">';
    for (const m of inputMsgs) html += renderDrawerMessage(m, dupFingerprints);
    html += '</div></div>';
  }

  if (outputMsgs.length) {
    html += '<div class="msg-group msg-group-layer-output">';
    html += '<div class="msg-group-header msg-group-output"><span class="msg-group-title">本次输出</span><span class="msg-group-hint">completion messages</span><span class="msg-group-count">' + outputMsgs.length + '</span></div>';
    html += '<div class="msg-group-body msg-group-body-nested">';
    for (const m of outputMsgs) html += renderDrawerMessage(m, dupFingerprints);
    html += '</div></div>';
  }

  if (!outputMsgs.length && step.output) {
    html += '<div class="msg-group msg-group-layer-output">';
    html += '<div class="msg-group-header msg-group-output"><span class="msg-group-title">本次输出</span><span class="msg-group-hint">raw step output</span><span class="msg-group-count">1</span></div>';
    html += '<div class="msg-group-body msg-group-body-nested">' + renderFormattedContent(step.output) + '</div></div>';
  }

  html += '</div>';
  return html;
}

function closeDrawer() {
  S.selectedStep = null;
  document.getElementById('detailDrawer').classList.remove('open');
  document.querySelectorAll('.step-card.selected-step').forEach(el => el.classList.remove('selected-step'));
}

function renderDrawerContent() {
  const el = document.getElementById('drawerBody');
  const s = S.selectedStep;
  if (!s) { el.innerHTML = ''; return; }
  const tab = S.drawerTab;

  if (tab === 'overview') {
    let html = '<div class="drawer-section"><div class="drawer-section-title">基本信息</div>';
    html += '<div class="drawer-kv"><span class="drawer-kv-key">类型</span><span class="drawer-kv-val">' + esc(s.step_type) + '</span></div>';
    html += '<div class="drawer-kv"><span class="drawer-kv-key">名称</span><span class="drawer-kv-val">' + esc(s.step_name || '?') + '</span></div>';
    html += '<div class="drawer-kv"><span class="drawer-kv-key">状态</span><span class="drawer-kv-val">' + esc(s.status) + '</span></div>';
    html += '<div class="drawer-kv"><span class="drawer-kv-key">耗时</span><span class="drawer-kv-val">' + fmtDur(s.duration_ms) + '</span></div>';
    html += '<div class="drawer-kv"><span class="drawer-kv-key">开始</span><span class="drawer-kv-val">' + fmtTime(s.started_at) + '</span></div>';
    html += '<div class="drawer-kv"><span class="drawer-kv-key">结束</span><span class="drawer-kv-val">' + fmtTime(s.finished_at) + '</span></div>';
    html += '</div>';
    if (s.extra) {
      try {
        const extra = typeof s.extra === 'string' ? JSON.parse(s.extra) : s.extra;
        const usage = extra.token_usage;
        if (usage && (usage.total_tokens || usage.prompt_tokens)) {
          const inT = usage.prompt_tokens || usage.input_tokens || 0;
          const outT = usage.completion_tokens || usage.output_tokens || 0;
          const tot = usage.total_tokens || (inT + outT);
          const cachedT = usage.cached_tokens || 0;
          const reasoningT = usage.reasoning_tokens || 0;
          const hitR = usage.cache_hit_ratio || 0;
          html += '<div class="drawer-section"><div class="drawer-section-title">Token 用量</div>';
          html += '<div class="drawer-kv"><span class="drawer-kv-key">Input</span><span class="drawer-kv-val" style="color:var(--accent)">' + inT.toLocaleString() + '</span></div>';
          html += '<div class="drawer-kv"><span class="drawer-kv-key">Output</span><span class="drawer-kv-val" style="color:var(--purple)">' + outT.toLocaleString() + '</span></div>';
          if (reasoningT) {
            html += '<div class="drawer-kv"><span class="drawer-kv-key">思考</span><span class="drawer-kv-val" style="color:#8b5cf6">' + reasoningT.toLocaleString() + '</span></div>';
          }
          html += '<div class="drawer-kv"><span class="drawer-kv-key">Total</span><span class="drawer-kv-val" style="font-weight:600">' + tot.toLocaleString() + '</span></div>';
          if (cachedT) {
            html += '<div class="drawer-kv"><span class="drawer-kv-key">Cached</span><span class="drawer-kv-val" style="color:#10b981">' + cachedT.toLocaleString() + ' (' + (hitR * 100).toFixed(1) + '%)</span></div>';
          }
          html += '</div>';
        }
        if (extra.label || extra.node) {
          html += '<div class="drawer-section"><div class="drawer-section-title">节点标识</div>';
          if (extra.label) html += '<div class="drawer-kv"><span class="drawer-kv-key">Label</span><span class="drawer-kv-val">' + esc(extra.label) + '</span></div>';
          if (extra.node) html += '<div class="drawer-kv"><span class="drawer-kv-key">Node</span><span class="drawer-kv-val">' + esc(extra.node) + '</span></div>';
          html += '</div>';
        }
      } catch(e) {}
    }
    if (s.error) {
      html += '<div class="drawer-section"><div class="drawer-section-title">错误</div><div class="error-box">' + esc(s.error) + '</div></div>';
    }
    el.innerHTML = html;
  } else if (tab === 'input') {
    el.innerHTML = s.input
      ? '<div class="drawer-section"><div class="drawer-section-title">输入数据</div>' + renderFormattedContent(s.input) + '</div>'
      : '<div style="color:var(--muted);text-align:center;padding-top:40px;font-size:10px">无输入数据</div>';
  } else if (tab === 'output') {
    el.innerHTML = s.output
      ? '<div class="drawer-section"><div class="drawer-section-title">输出数据</div>' + renderFormattedContent(s.output) + '</div>'
      : '<div style="color:var(--muted);text-align:center;padding-top:40px;font-size:10px">无输出数据</div>';
  } else if (tab === 'messages') {
    el.innerHTML = renderDrawerMessagesContent(s);
  }
}

function renderTraceView() {
  const placeholder = document.getElementById('placeholder');
  const leftPanel = document.getElementById('left-panel');
  const rightPanel = document.getElementById('right-panel');
  const runHeader = document.getElementById('run-header');

  if (!S.selectedId) {
    placeholder.style.display = 'flex';
    runHeader.style.display = 'none';
    leftPanel.style.display = 'none';
    rightPanel.style.display = 'none';
    closeDrawer();
    return;
  }

  const run = S.runs.find(r => r.id === S.selectedId);
  if (!run) {
    placeholder.style.display = 'flex';
    runHeader.style.display = 'none';
    leftPanel.style.display = 'none';
    rightPanel.style.display = 'none';
    closeDrawer();
    return;
  }

  placeholder.style.display = 'none';
  runHeader.style.display = 'block';
  leftPanel.style.display = 'block';
  rightPanel.style.display = 'block';

  const steps = run.steps || [];
  const tokenData = extractTokenData(steps);
  const toolHealth = extractToolHealth(steps);
  const totalIn = tokenData.reduce((s, t) => s + t.inTokens, 0);
  const totalOut = tokenData.reduce((s, t) => s + t.outTokens, 0);
  const totalCached = tokenData.reduce((s, t) => s + t.cachedTokens, 0);
  const totalTokens = totalIn + totalOut;
  const cacheHitRatio = totalIn > 0 ? totalCached / totalIn : 0;
  const llmSteps = steps.filter(s => s.step_type === 'llm');
  const toolSteps = steps.filter(s => s.step_type === 'tool');
  const errorSteps = steps.filter(s => s.status === 'error');
  const maxStepTokens = Math.max(...tokenData.map(t => t.totalTokens), 1);

  // Run Header
  runHeader.innerHTML = `
    <h2>${esc(run.agent_name || 'Unknown')}</h2>
    <div class="meta-row">
      <span class="badge badge-${run.status}">${run.status}</span>
      <span>ID: ${(run.id||'').slice(0,16)}</span>
      <span>耗时: ${fmtDur(run.duration_ms)}</span>
      <span>时间: ${fmtTime(run.created_at)}</span>
    </div>
    ${run.error ? `<div class="error-box" style="margin-top:6px">${esc(run.error)}</div>` : ''}
  `;

  // Left Panel: Analysis
  let leftHtml = '';

  leftHtml += `<div class="kpi-strip">`;
  leftHtml += kpiCard('总 Token', totalTokens, 'c-accent', `↑${totalIn.toLocaleString()} / ↓${totalOut.toLocaleString()}`);
  leftHtml += kpiCard('LLM 调用', llmSteps.length, 'c-purple', `平均 ${llmSteps.length ? Math.round(totalTokens / llmSteps.length).toLocaleString() : 0} tok/次`);
  if (totalCached) {
    leftHtml += kpiCard('缓存命中', totalCached, 'c-green', `${(cacheHitRatio * 100).toFixed(1)}% hit rate`);
  }
  leftHtml += kpiCard('工具调用', toolSteps.length, 'c-warning', `${toolHealth.length} 种工具`);
  leftHtml += kpiCard('步骤数', steps.length, 'c-success', errorSteps.length ? `<span style="color:var(--error)">${errorSteps.length} 错误</span>` : '无错误');
  const ctxAnalysis = analyzeContext(steps);
  if (ctxAnalysis.wasteTokens > 0) {
    const wastePct = totalTokens > 0 ? Math.round(ctxAnalysis.wasteTokens / totalTokens * 100) : 0;
    leftHtml += `<div class="kpi-card kpi-waste">
      <div class="kpi-label">重复浪费</div>
      <div class="kpi-value c-error">${ctxAnalysis.wasteTokens.toLocaleString()}</div>
      <div class="kpi-sub">可节省 ${wastePct}% 上下文</div>
    </div>`;
  }
  leftHtml += `</div>`;

  const llmTokenData = tokenData.filter(t => t.totalTokens > 0);
  if (llmTokenData.length > 0) {
    leftHtml += `<div class="token-chart-section">`;
    leftHtml += `<div class="section-title"><span class="icon">📊</span> Token 分布</div>`;
    leftHtml += `<div class="waterfall-chart">`;
    for (let i = 0; i < llmTokenData.length; i++) {
      const t = llmTokenData[i];
      const label = getStepLabel(t.step) || t.step.step_name || '?';
      const uncachedIn = Math.max(0, t.inTokens - t.cacheReadTokens);
      const inPct = maxStepTokens > 0 ? (uncachedIn / maxStepTokens * 100) : 0;
      const cachedPct = maxStepTokens > 0 ? (t.cacheReadTokens / maxStepTokens * 100) : 0;
      const outPct = maxStepTokens > 0 ? (t.outTokens / maxStepTokens * 100) : 0;
      leftHtml += `<div class="wf-row" onclick="highlightStep('${t.step.event_run_id}')">
        <span class="wf-idx">${i + 1}</span>
        <span class="wf-name" title="${esc(label)}">${esc(label)}</span>
        <div class="wf-bar-wrap">
          <div class="wf-bar-in" style="width:${inPct}%"></div>
          <div class="wf-bar-cached" style="width:${cachedPct}%"></div>
          <div class="wf-bar-out" style="width:${outPct}%"></div>
        </div>
        <span class="wf-tokens">↑${t.inTokens}${t.cacheReadTokens ? `<span class="wf-cached-hint" title="缓存命中 ${t.cacheReadTokens}"> ·${t.cacheReadTokens}</span>` : ''} ↓${t.outTokens}</span>
      </div>`;
    }
    leftHtml += `</div>`;
    leftHtml += `<div class="legend-row">
      <span><span class="legend-dot" style="background:var(--accent);opacity:0.7"></span>Prompt</span>
      <span><span class="legend-dot" style="background:var(--green);opacity:0.85"></span>缓存命中</span>
      <span><span class="legend-dot" style="background:var(--purple);opacity:0.7"></span>Completion</span>
    </div>`;
    leftHtml += `</div>`;
  }

  if (llmTokenData.length > 1) {
    leftHtml += `<div class="sparkline-section">`;
    leftHtml += `<div class="section-title"><span class="icon">📈</span> 上下文增长</div>`;
    leftHtml += `<canvas id="sparklineCanvas" class="sparkline-canvas"></canvas>`;
    leftHtml += `</div>`;
  }

  if (toolHealth.length > 0) {
    leftHtml += `<div class="tool-health-section">`;
    leftHtml += `<div class="section-title"><span class="icon">🔧</span> 工具健康度</div>`;
    leftHtml += `<div class="th-grid">`;
    for (const t of toolHealth) {
      const avgMs = t.calls > 0 ? Math.round(t.totalMs / t.calls) : 0;
      const sc = t.errors > 0 ? 'err' : avgMs > 5000 ? 'warn' : 'ok';
      leftHtml += `<div class="th-item">
        <span class="th-icon">🔧</span>
        <span class="th-name" title="${esc(t.name)}">${esc(t.name)}</span>
        <span class="th-stats">
          <span class="${sc}">${t.calls}次</span>
          <span>${fmtDur(avgMs)}/次</span>
          ${t.errors > 0 ? `<span class="err">${t.errors}错</span>` : ''}
        </span>
      </div>`;
    }
    leftHtml += `</div></div>`;
  }

  leftHtml += renderAnalysisPanel(steps, ctxAnalysis);
  leftPanel.innerHTML = leftHtml;

  const dupFingerprints = new Set(ctxAnalysis.dupMessages.map(d => d.fingerprint));
  _paraMap = ctxAnalysis.paraMap;
  _paraIdx = 0;

  // Right Panel: Steps tree (no inline body expansion)
  let rightHtml = '';
  if (run.input) {
    rightHtml += `<div class="io-box"><div class="io-box-title">Input</div>${renderFormattedContent(run.input)}</div>`;
  }
  if (steps.length) {
    _stepIdx = 0;
    _curLlmStepIdx = -1;
    _llmIdxMap = {};
    const flatLlmSteps = steps.filter(s => s.step_type === 'llm');
    flatLlmSteps.forEach((s, i) => { _llmIdxMap[s.event_run_id] = i; });
    _stepsByIdx = [];
    const tree = buildTree(steps);
    rightHtml += `<div class="chain-tree">${renderTree(tree, dupFingerprints)}</div>`;
  }
  if (run.output) {
    rightHtml += `<div class="io-box" style="margin-top:8px"><div class="io-box-title">Output</div>${renderFormattedContent(run.output)}</div>`;
  }
  rightPanel.innerHTML = rightHtml;

  if (llmTokenData.length > 1) {
    requestAnimationFrame(() => drawSparkline(llmTokenData));
  }
}

function kpiCard(label, value, cls, sub) {
  return `<div class="kpi-card">
    <div class="kpi-label">${label}</div>
    <div class="kpi-value ${cls}">${typeof value === 'number' ? value.toLocaleString() : value}</div>
    <div class="kpi-sub">${sub}</div>
  </div>`;
}

function drawSparkline(data) {
  const canvas = document.getElementById('sparklineCanvas');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const w = rect.width, h = rect.height;
  const pad = { top: 8, right: 44, bottom: 16, left: 8 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;
  const maxVal = Math.max(...data.map(d => d.cumTotal), 1);
  const n = data.length;

  ctx.strokeStyle = 'rgba(56,189,248,0.06)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = pad.top + (plotH / 3) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
  }

  ctx.beginPath(); ctx.strokeStyle = 'rgba(56,189,248,0.6)'; ctx.lineWidth = 1.5;
  for (let i = 0; i < n; i++) {
    const x = pad.left + (i / (n - 1)) * plotW;
    const y = pad.top + plotH - (data[i].cumTotal / maxVal) * plotH;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();

  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = pad.left + (i / (n - 1)) * plotW;
    const y = pad.top + plotH - (data[i].cumTotal / maxVal) * plotH;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
  grad.addColorStop(0, 'rgba(56,189,248,0.15)');
  grad.addColorStop(1, 'rgba(56,189,248,0.01)');
  ctx.fillStyle = grad; ctx.fill();

  ctx.beginPath(); ctx.strokeStyle = 'rgba(192,132,252,0.5)'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
  for (let i = 0; i < n; i++) {
    const x = pad.left + (i / (n - 1)) * plotW;
    const y = pad.top + plotH - (data[i].cumIn / maxVal) * plotH;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke(); ctx.setLineDash([]);

  for (let i = 0; i < n; i++) {
    const x = pad.left + (i / (n - 1)) * plotW;
    const y = pad.top + plotH - (data[i].cumTotal / maxVal) * plotH;
    ctx.beginPath(); ctx.arc(x, y, 2, 0, Math.PI * 2); ctx.fillStyle = 'rgba(56,189,248,0.8)'; ctx.fill();
  }

  ctx.font = '8px monospace'; ctx.fillStyle = 'rgba(100,116,139,0.8)'; ctx.textAlign = 'left';
  for (let i = 0; i <= 3; i++) {
    const val = Math.round(maxVal * (1 - i / 3));
    ctx.fillText(fmtCompact(val), w - pad.right + 3, pad.top + (plotH / 3) * i + 3);
  }
  ctx.textAlign = 'center';
  for (let i = 0; i < n; i++) {
    if (n <= 8 || i % Math.ceil(n / 8) === 0 || i === n - 1) {
      ctx.fillText(String(i + 1), pad.left + (i / (n - 1)) * plotW, h - 2);
    }
  }
}

function renderTree(nodes, dupFingerprints) {
  let html = '';
  for (const node of nodes) {
    const idx = _stepIdx++;
    const s = node.step;
    _stepsByIdx[idx] = s;
    if (s.step_type === 'llm') _curLlmStepIdx = _llmIdxMap[s.event_run_id] !== undefined ? _llmIdxMap[s.event_run_id] : _curLlmStepIdx;
    const label = getStepLabel(s);
    const typeClass = `type-${s.step_type}`;
    const errClass = s.status === 'error' ? 'status-error' : '';
    const icon = TYPE_ICONS[s.step_type] || '·';
    const hasBody = s.input || s.output || s.error || (s.messages && s.messages.length) || s.extra;
    const isRoot = !s.parent_run_id;
    const hasChildren = node.children.length > 0;

    html += `<div class="chain-node ${isRoot ? 'root-node' : ''}">`;
    html += `<div class="chain-node-inner">`;
    html += `<div class="step-card ${typeClass} ${errClass}" id="step-${idx}" data-rid="${esc(s.event_run_id)}" data-idx="${idx}">
      <div class="step-head" onclick="openDrawerByStep(${idx})">
        <span class="step-type-icon">${icon}</span>
        <span class="step-name">${esc(s.step_name || '?')}</span>
        ${label ? `<span class="step-label">${esc(label)}</span>` : ''}
        <span class="badge badge-${s.status}" style="margin-left:auto;margin-right:4px">${s.status}</span>
        <span class="step-dur">${fmtDur(s.duration_ms)}</span>
      </div>
    </div>`;
    html += `</div>`;
    if (hasChildren) {
      html += `<div class="chain-children">${renderTree(node.children, dupFingerprints)}</div>`;
    }
    html += `</div>`;
  }
  return html;
}

function renderStepBody(s, dupFingerprints) {
  const dupFP = dupFingerprints || new Set();
  let html = '';
  if (s.input) {
    html += `<div class="io-box" style="margin-bottom:6px"><div class="io-box-title">Input</div>${renderFormattedContent(s.input)}</div>`;
  }
  const msgs = s.messages || [];
  if (msgs.length) {
    html += '<div class="msg-list">';
    for (const m of msgs) {
      const role = m.role || 'unknown';
      const c = (m.content || '').trim();
      const isDup = c.length >= 30 && dupFP.has(contentFingerprint(c, 200));
      const withinDups = getWithinMsgDups(c);
      const wsDupCount = withinDups.size;
      const msgContent = c.length > 100 && (c.includes('\n\n') || hasMarkdown(c))
        ? renderNumberedParagraphs(c, _curLlmStepIdx, withinDups)
        : renderFormattedContent(m.content || '');
      const badges = [];
      if (isDup) badges.push('<span class="dup-inline-badge">消息重复</span>');
      if (wsDupCount > 0) badges.push(`<span class="dup-inline-ws-badge" title="此消息内有 ${wsDupCount} 个段落重复出现">段内×${wsDupCount}</span>`);
      html += `<div class="msg ${isDup ? 'msg-dup' : ''}">
        <div class="msg-role msg-role-${role}">${role}${badges.join('')}</div>
        <div class="msg-content">${msgContent}</div>
        ${m.tool_calls ? `<div class="msg-tool-calls">${renderFormattedContent(m.tool_calls)}</div>` : ''}
      </div>`;
    }
    html += '</div>';
  }
  if (s.extra) {
    try {
      const extra = typeof s.extra === 'string' ? JSON.parse(s.extra) : s.extra;
      const usage = extra.token_usage;
      if (usage && (usage.total_tokens || usage.prompt_tokens)) {
        const inT = usage.prompt_tokens || usage.input_tokens || 0;
        const outT = usage.completion_tokens || usage.output_tokens || 0;
        const tot = usage.total_tokens || (inT + outT);
        const cachedT = usage.cached_tokens || 0;
        html += `<div class="token-info">
          <span class="t-in">↑ ${inT.toLocaleString()}</span>
          <span class="t-out">↓ ${outT.toLocaleString()}</span>
          <span class="t-total">∑ ${tot.toLocaleString()}</span>
          ${cachedT ? `<span class="t-cached" style="color:#10b981">⚡ ${cachedT.toLocaleString()}</span>` : ""}
        </div>`;
      }
    } catch(e) {}
  }
  if (s.output) {
    html += `<div class="io-box" style="margin-top:6px"><div class="io-box-title">Output</div>${renderFormattedContent(s.output)}</div>`;
  }
  if (s.error) {
    html += `<div class="error-box">${esc(s.error)}</div>`;
  }
  return html;
}

function highlightStep(eventRunId) {
  document.querySelectorAll('.step-card.highlighted').forEach(el => el.classList.remove('highlighted'));
  const target = document.querySelector(`.step-card[data-rid="${eventRunId}"]`);
  if (target) {
    target.classList.add('highlighted');
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setTimeout(() => target.classList.remove('highlighted'), 3000);
    const idx = parseInt(target.dataset.idx);
    if (!isNaN(idx) && _stepsByIdx[idx]) openDrawer(_stepsByIdx[idx]);
  }
}

function openDrawerByStep(idx) {
  const step = _stepsByIdx[idx];
  if (!step) return;
  document.querySelectorAll('.step-card.selected-step').forEach(el => el.classList.remove('selected-step'));
  const card = document.getElementById(`step-${idx}`);
  if (card) card.classList.add('selected-step');
  openDrawer(step);
}
