/* ── analysis ── */

function buildParaMap(steps) {
  const paraMap = {};
  const llmSteps = steps.filter(s => s.step_type === 'llm');
  for (let si = 0; si < llmSteps.length; si++) {
    const msgs = llmSteps[si].messages || [];
    for (const m of msgs) {
      const c = (m.content || '').trim();
      if (c.length < 30) continue;
      const paras = c.split(/\n{2,}/).map(p => p.trim()).filter(p => p.length > 20);
      for (const p of paras) {
        if (isJsonParagraph(p)) continue;
        const fp = contentFingerprint(p, 200);
        if (!paraMap[fp]) {
          paraMap[fp] = { firstStepIdx: si, stepIndices: [], stepLabels: [], firstEventRunId: llmSteps[si].event_run_id, preview: p.slice(0, 80) };
        }
        const arr = paraMap[fp].stepIndices;
        if (arr.length === 0 || arr[arr.length - 1] !== si) {
          arr.push(si);
          const lbl = getStepLabel(llmSteps[si]) || llmSteps[si].step_name || `步骤${si + 1}`;
          paraMap[fp].stepLabels.push(lbl);
        }
      }
    }
  }
  return paraMap;
}

function analyzeContext(steps) {
  const result = {
    dupMessages: [],
    dupToolCalls: [],
    tokenSpikes: [],
    dupParagraphs: [],
    paraMap: {},
  };

  // ── 1. 消息指纹匹配（全链路重复检测）──
  const fpMap = {};
  const llmSteps = steps.filter(s => s.step_type === 'llm');
  for (let si = 0; si < llmSteps.length; si++) {
    const msgs = llmSteps[si].messages || [];
    for (const m of msgs) {
      if (m.role === 'system') continue;
      const c = (m.content || '').trim();
      if (c.length < 30) continue;
      const fp = contentFingerprint(c, 200);
      if (!fpMap[fp]) {
        fpMap[fp] = { count: 0, eventRunIds: [], role: m.role || '?', preview: c.slice(0, 80) };
      }
      fpMap[fp].count++;
      const rid = llmSteps[si].event_run_id;
      if (!fpMap[fp].eventRunIds.includes(rid)) fpMap[fp].eventRunIds.push(rid);
    }
  }
  for (const [fp, info] of Object.entries(fpMap)) {
    if (info.count > 1) {
      result.dupMessages.push({ fingerprint: fp, ...info });
    }
  }
  result.dupMessages.sort((a, b) => b.count - a.count);
  const systemDups = Object.entries(fpMap).filter(([fp, info]) => info.role === 'system' && info.count > 1);
  result.systemDupCount = systemDups.length;
  result.wasteTokens = 0;
  for (const d of result.dupMessages) {
    const perMsgTokens = Math.round(d.preview.length * 1.5);
    result.wasteTokens += perMsgTokens * (d.count - 1);
  }

  // ── 2. 单步内重复段落检测 ──
  for (let si = 0; si < llmSteps.length; si++) {
    const msgs = llmSteps[si].messages || [];
    for (let mi = 0; mi < msgs.length; mi++) {
      const c = msgs[mi].content || '';
      if (c.length < 100) continue;
      const paras = c.split(/\n{2,}/).map(p => p.trim()).filter(p => p.length > 30);
      const paraMap = {};
      for (let pi = 0; pi < paras.length; pi++) {
        const fp = contentFingerprint(paras[pi], 150);
        if (!paraMap[fp]) paraMap[fp] = { text: paras[pi], indices: [] };
        paraMap[fp].indices.push(pi);
      }
      for (const [fp, info] of Object.entries(paraMap)) {
        if (info.indices.length > 1) {
          result.dupParagraphs.push({
            llmStepIdx: si, msgIdx: mi, paraIdx: info.indices[0],
            text: info.text.slice(0, 100), count: info.indices.length,
            eventRunId: llmSteps[si].event_run_id,
          });
        }
      }
    }
  }

  // ── 3. 工具调用去重分析 ──
  const tcMap = {};
  for (let si = 0; si < steps.length; si++) {
    const s = steps[si];
    const msgs = s.messages || [];
    for (const m of msgs) {
      let tcs = m.tool_calls;
      if (!tcs) continue;
      if (typeof tcs === 'string') { try { tcs = JSON.parse(tcs); } catch { continue; } }
      if (!Array.isArray(tcs)) continue;
      for (const tc of tcs) {
        const name = tc.name || tc.function?.name || '?';
        const args = typeof tc.args === 'string' ? tc.args : JSON.stringify(tc.args || {});
        const sig = name + ':' + args.slice(0, 200);
        if (!tcMap[sig]) {
          tcMap[sig] = { count: 0, eventRunIds: [], name, argsPreview: args.slice(0, 80) };
        }
        tcMap[sig].count++;
        const rid = s.event_run_id;
        if (!tcMap[sig].eventRunIds.includes(rid)) tcMap[sig].eventRunIds.push(rid);
      }
    }
  }
  for (const [sig, info] of Object.entries(tcMap)) {
    if (info.count > 1) {
      result.dupToolCalls.push({ sig, ...info });
    }
  }
  result.dupToolCalls.sort((a, b) => b.count - a.count);

  // ── 4. Token 增长异常检测 ──
  const tokenData = extractTokenData(steps);
  for (let i = 1; i < tokenData.length; i++) {
    const prev = tokenData[i - 1], cur = tokenData[i];
    if (prev.inTokens > 0 && cur.inTokens > prev.inTokens * 1.5 && cur.inTokens - prev.inTokens > 500) {
      const label = getStepLabel(cur.step) || cur.step.step_name || '?';
      result.tokenSpikes.push({
        stepIdx: i, label, eventRunId: cur.step.event_run_id,
        prevIn: prev.inTokens, curIn: cur.inTokens,
        growth: Math.round((cur.inTokens / prev.inTokens - 1) * 100),
      });
    }
  }

  // ── 5. 全链路段落指纹图 ──
  result.paraMap = buildParaMap(steps);

  result.stepLabelMap = {};
  for (const s of steps) {
    const label = getStepLabel(s) || s.step_name || '?';
    result.stepLabelMap[s.event_run_id] = label;
  }

  return result;
}

function renderAnalysisPanel(steps, analysis) {
  const hasAny = analysis.dupMessages.length || analysis.dupToolCalls.length ||
                 analysis.tokenSpikes.length || analysis.dupParagraphs.length ||
                 Object.values(analysis.paraMap).some(p => p.stepIndices.length > 1);
  if (!hasAny) return '';

  let html = '<div class="ctx-analysis">';
  html += '<div class="section-title"><span class="icon">🔍</span> 上下文分析</div>';

  // 重复消息 - 按 role 分组
  if (analysis.dupMessages.length) {
    const roleOrder = ['assistant', 'tool', 'human', 'user', 'system'];
    const roleLabels = { assistant: 'AI 响应', tool: '工具消息', human: '用户输入', user: '用户输入', system: '系统提示' };
    const roleGroups = {};
    for (const d of analysis.dupMessages) {
      const r = d.role || 'unknown';
      if (!roleGroups[r]) roleGroups[r] = [];
      roleGroups[r].push(d);
    }
    html += '<div class="ctx-group">';
    html += `<div class="ctx-group-title">重复消息 <span class="ctx-count">${analysis.dupMessages.length} 组</span></div>`;
    for (const role of roleOrder) {
      const items = roleGroups[role];
      if (!items || !items.length) continue;
      const isSystem = role === 'system';
      const collapsedClass = isSystem ? ' ctx-group-collapsed' : '';
      html += `<div class="ctx-group-role${collapsedClass}">`;
      html += `<div class="ctx-group-role-header role-${role}" onclick="toggleCtxRoleGroup(this)">
        <span class="ctx-group-role-chevron">▶</span>
        <span>${roleLabels[role] || role}</span>
        <span class="ctx-group-role-count">${items.length} 组${isSystem ? '（正常）' : ''}</span>
      </div>`;
      html += '<div class="ctx-group-role-body">';
      for (const d of items.slice(0, 8)) {
        const stepTags = d.eventRunIds.map(rid => {
          const lbl = (analysis.stepLabelMap && analysis.stepLabelMap[rid]) || rid.slice(0,6);
          const step = steps.find(s => s.event_run_id === rid);
          const typeCls = step ? ` ctx-step-tag-${step.step_type}` : '';
          return `<span class="ctx-step-tag${typeCls}" onclick="highlightStep('${esc(rid)}')" title="${esc(lbl)}">${esc(lbl.length > 6 ? lbl.slice(0,6) + '…' : lbl)}</span>`;
        }).join('');
        html += `<div class="ctx-item">
          <span class="dup-badge">×${d.count}</span>
          <span class="ctx-preview" title="${esc(d.preview)}">${esc(d.preview.slice(0, 60))}</span>
          <span class="ctx-steps">${stepTags}</span>
        </div>`;
      }
      html += '</div></div>';
    }
    html += '</div>';
  }

  // 重复工具调用
  if (analysis.dupToolCalls.length) {
    html += '<div class="ctx-group">';
    html += `<div class="ctx-group-title">重复工具调用 <span class="ctx-count">${analysis.dupToolCalls.length} 组</span></div>`;
    for (const d of analysis.dupToolCalls.slice(0, 6)) {
      const stepTags = d.eventRunIds.map(rid => {
        const lbl = (analysis.stepLabelMap && analysis.stepLabelMap[rid]) || rid.slice(0,6);
        const step = steps.find(s => s.event_run_id === rid);
        const typeCls = step ? ` ctx-step-tag-${step.step_type}` : '';
        return `<span class="ctx-step-tag${typeCls}" onclick="highlightStep('${esc(rid)}')" title="${esc(lbl)}">${esc(lbl.length > 6 ? lbl.slice(0,6) + '…' : lbl)}</span>`;
      }).join('');
      html += `<div class="ctx-item">
        <span class="dup-badge">×${d.count}</span>
        <span class="ctx-tool-name">${esc(d.name)}</span>
        <span class="ctx-preview" title="${esc(d.argsPreview)}">${esc(d.argsPreview.slice(0, 50))}</span>
        <span class="ctx-steps">${stepTags}</span>
      </div>`;
    }
    html += '</div>';
  }

  // 单步内重复段落
  if (analysis.dupParagraphs.length) {
    html += '<div class="ctx-group">';
    html += `<div class="ctx-group-title">单步内重复段落 <span class="ctx-count">${analysis.dupParagraphs.length} 处</span></div>`;
    for (const d of analysis.dupParagraphs.slice(0, 5)) {
      html += `<div class="ctx-item ctx-spike" onclick="highlightStep('${d.eventRunId}')">
        <span class="dup-badge">×${d.count}</span>
        <span class="ctx-preview">步骤${d.llmStepIdx + 1} msg#${d.msgIdx}: ${esc(d.text.slice(0, 50))}</span>
      </div>`;
    }
    html += '</div>';
  }

  // 跨步骤重复段落
  const crossDupParas = Object.entries(analysis.paraMap)
    .filter(([, info]) => info.stepIndices.length > 1)
    .sort((a, b) => b[1].stepIndices.length - a[1].stepIndices.length);
  if (crossDupParas.length) {
    html += '<div class="ctx-group">';
    html += `<div class="ctx-group-title">跨步骤重复段落 <span class="ctx-count">${crossDupParas.length} 组</span></div>`;
    for (const [fp, info] of crossDupParas.slice(0, 6)) {
      const count = info.stepIndices.length;
      html += `<div class="ctx-item ctx-spike" onclick="highlightStep('${esc(info.firstEventRunId)}')">
        <span class="dup-badge">×${count}</span>
        <span class="ctx-preview" title="${esc(info.preview)}">${esc(info.preview.slice(0, 60))}</span>
        <span class="ctx-detail">${count} 个步骤</span>
      </div>`;
    }
    html += '</div>';
  }

  // Token 增长异常
  if (analysis.tokenSpikes.length) {
    html += '<div class="ctx-group">';
    html += `<div class="ctx-group-title">Token 增长异常 <span class="ctx-count">${analysis.tokenSpikes.length} 处</span></div>`;
    for (const d of analysis.tokenSpikes) {
      html += `<div class="ctx-item ctx-spike" onclick="highlightStep('${d.eventRunId}')">
        <span class="token-spike">+${d.growth}%</span>
        <span class="ctx-preview">${esc(d.label)}</span>
        <span class="ctx-detail">${d.prevIn.toLocaleString()} → ${d.curIn.toLocaleString()} tokens</span>
      </div>`;
    }
    html += '</div>';
  }

  html += '</div>';
  return html;
}

function toggleCtxRoleGroup(header) {
  const group = header.parentElement;
  group.classList.toggle('ctx-group-collapsed');
  const chevron = header.querySelector('.ctx-group-role-chevron');
  if (chevron) chevron.textContent = group.classList.contains('ctx-group-collapsed') ? '▶' : '▼';
}
