/* ═══════════════════════════════════════════════════════════════
 * eval-detail.js — 单次评测详情组件（§11.4 统一契约）
 *
 * 三入口共用：题库回归 / 存量回测（contract = case 详情契约），
 * Agent 研判（contract = review 详情契约）。只消费契约，不自取数；
 * open(caseId) 是便捷入口（拉 /api/eval/cases/{id} 的 contract）。
 *
 * 暴露：window.StockRadar.evalDetail = { render, open }
 * ═══════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function scoreLevel(v) {
    if (v == null || v === '' || !isFinite(Number(v))) return 'muted';
    const n = Number(v);
    if (n >= 80) return 'high';
    if (n >= 60) return 'mid';
    return 'low';
  }

  /* ── 证据引用卡（quote 可展开看上下文 / 跳转） ───────────────── */
  function evidenceCard(e) {
    if (!e || typeof e !== 'object') return '';
    const srcLabel = { tool_output: '工具返回', answer: '回答',
                       user_message: '用户原话', think_units: '推理' }[e.source] || e.source || '?';
    const loc = e.turn_index != null ? `turn ${e.turn_index}`
      : (e.step_ref != null ? `step_ref ${e.step_ref}` : '无定位');
    const verified = e.verified !== false && e.verified !== undefined ? e.verified : !e.unverified;
    const cls = verified ? 'ed-ok' : 'ed-bad';
    const chip = verified ? '<span class="ed-chip ok">已核验</span>'
                          : '<span class="ed-chip bad">未核验</span>';
    const jump = (e.step_ref != null && e.step_ref > 0)
      ? `<div class="ed-ev-jump"><a href="/trace?run=&step=${e.step_ref}" target="_blank" rel="noopener">→ trace viewer 定位 step_ref ${e.step_ref}</a></div>`
      : '';
    return `<div class="ed-ev ${cls}" data-open="0">
      <div class="ed-ev-head">${chip}<span>${esc(srcLabel)}</span><span>·</span><span>${esc(loc)}</span></div>
      <div class="ed-ev-quote">${verified ? '<mark>' + esc(e.quote || '') + '</mark>' : esc(e.quote || '')}</div>
      ${jump}</div>`;
  }

  /* ── 单个维度行（规则 = 计算过程；LLM = 判定 + 证据） ─────────── */
  function dimRow(d, caseId) {
    const lv = scoreLevel(d.score);
    const basis = d.basis || {};
    const isRule = d.basis_type === 'rule';
    const judgeTag = d.judge === 'rule' ? '<span class="ed-jtag rule">规则</span>'
      : d.judge === 'llm' ? '<span class="ed-jtag llm">LLM</span>'
      : '<span class="ed-jtag both">规则+LLM</span>';
    const lc = d.low_confidence
      ? '<span class="ed-chip bad" title="低分且无已核验依据">低置信</span>' : '';
    // 人工改分入口（§12）：仅 case 评分（eval.db）且已有分数的维度
    const manualBtn = (caseId != null && d.score != null)
      ? `<button class="ed-manual-btn" data-dim="${esc(d.dimension)}" title="人工改分（AI 原分保留在 detail_json.ai）">✎</button>`
      : '';

    let body = '';
    if (isRule) {
      const kv = Object.entries(basis)
        .filter(([k]) => !['formula'].includes(k))
        .map(([k, v]) => `<div class="ed-kv"><span class="k">${esc(k)}</span><span class="v">${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</span></div>`)
        .join('');
      body = `<div class="ed-calc">${kv}
        ${basis.formula ? `<div class="ed-formula">${esc(basis.formula)}</div>` : ''}</div>`;
    } else {
      const ev = Array.isArray(basis.evidence) ? basis.evidence : [];
      body = `<div class="ed-calc">
          <div class="ed-kv"><span class="k">judge 判定</span><span class="v">${esc(basis.verdict || '—')}</span></div>
          <div class="ed-kv"><span class="k">理由</span><span class="v">${esc(basis.reason || '—')}</span></div>
        </div>
        ${ev.length ? `<div class="ed-ev-list">${ev.map(evidenceCard).join('')}</div>`
          : '<div class="ed-noev">无引用证据（该维度未产出 finding）</div>'}`;
    }

    return `<div class="ed-dim ${isRule ? 'open' : ''}">
      <div class="ed-dim-head">
        <span class="ed-dim-name">${esc(d.label || d.dimension)}</span>${judgeTag}${lc}
        <span class="ed-dim-score ${lv}">${d.score == null ? '-' : Number(d.score).toFixed(0)}</span>
        ${manualBtn}
        <span class="ed-toggle">依据 ▾</span>
      </div>
      <div class="ed-dim-body">${body}</div></div>`;
  }

  /* ── findings 卡（§8 契约） ─────────────────────────────────── */
  function findingCard(f) {
    const unv = !!f.unverified;
    const ev = Array.isArray(f.evidence) ? f.evidence : [];
    return `<div class="ed-finding ${unv ? 'unverified' : ''}">
      <div class="ed-f-head">
        <span class="ed-dim-name">${esc(f.dimension || '')}</span>
        <span class="ed-f-verdict">${esc(f.verdict || '')}</span>
        <span class="ed-f-right">
          ${f.attribution ? `<span class="ed-chip attr">归因 ${esc(f.attribution)}</span>` : ''}
          ${unv ? '<span class="ed-chip bad">unverified</span>' : '<span class="ed-chip ok">已核验</span>'}
        </span>
      </div>
      ${ev.length ? `<div class="ed-blk">证据</div><div class="ed-ev-list">${ev.map(evidenceCard).join('')}</div>` : ''}
      ${(f.issues || []).length ? `<div class="ed-blk">问题</div><ul class="ed-ul">${f.issues.map(i => `<li>${esc(i)}</li>`).join('')}</ul>` : ''}
      ${(f.suggestions || []).length ? `<div class="ed-blk">改进建议</div><ul class="ed-ul sug">${f.suggestions.map(s => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}
      ${unv && f.unverified_reason ? `<div class="ed-unv-reason">降级原因：${esc(f.unverified_reason)}</div>` : ''}
    </div>`;
  }

  /* ── 主渲染：container + contract（可选 raw = 旧行结构） ─────── */
  function render(container, contract, raw) {
    if (!container) return;
    const c = contract || {};
    const t = c.target || {};
    const s = c.scores || {};
    const m = c.meta || {};
    const dims = Array.isArray(c.dimensions) ? c.dimensions : [];
    const findings = Array.isArray(c.findings) ? c.findings : [];
    const ruleDims = dims.filter(d => d.basis_type === 'rule');
    const llmDims = dims.filter(d => d.basis_type !== 'rule');

    const ruleW = ruleDims.reduce((acc, d) => acc + (d.weight || 0), 0);
    const ruleScore = ruleDims.length
      ? (ruleDims.reduce((acc, d) => acc + (d.score || 0) * (d.weight || 0), 0)
         / (ruleW || 1)).toFixed(1) : (s.rule == null ? '-' : Number(s.rule).toFixed(1));
    const diagScore = s.diagnostic == null ? null : Number(s.diagnostic).toFixed(1);
    const unvCount = findings.filter(f => f.unverified).length;

    const metaBits = [];
    if (m.rubric_version) metaBits.push(`rubric <b>${esc(m.rubric_version)}</b>`);
    if (m.judge_model) metaBits.push(`judge <b>${esc(m.judge_model)}</b>`);
    if (m.scored_at) metaBits.push(`评于 <b>${esc(String(m.scored_at).slice(0, 16).replace('T', ' '))}</b>`);
    if (t.trace_run_id) metaBits.push(`trace <b>${esc(t.trace_run_id)}</b>`);
    if (m.evidence && m.evidence.evidence_unverified != null) {
      metaBits.push(`证据核验 <b>${m.evidence.evidence_verified || 0} ✓ / ${m.evidence.evidence_unverified} ✗</b>`);
    }

    container.innerHTML = `
      <div class="ed-root">
        <div class="ed-head">
          <div class="ed-title">
            <div class="ed-sub">${esc(t.scope === 'run' || t.scope === 'dialog' ? 'Agent 研判 · ' + t.scope : 'Case #' + (t.case_id ?? ''))}</div>
            <div class="ed-q">${esc(t.question || raw && raw.question || '')}</div>
          </div>
          <div class="ed-scores">
            <div class="ed-scorecard"><div class="lbl">规则分</div><div class="val">${ruleScore}</div><div class="hint">可复现 · 可比</div></div>
            ${diagScore != null ? `<div class="ed-scorecard"><div class="lbl">研判分</div><div class="val down">${diagScore}</div><div class="hint">主观 · 找痛点</div></div>` : ''}
          </div>
        </div>
        ${metaBits.length ? `<div class="ed-meta">${metaBits.map(x => `<span>${x}</span>`).join('')}</div>` : ''}
        ${unvCount ? `<div class="ed-banner-warn">⚠ ${unvCount} 条 finding 的引用未能在证据中原文命中，已降级标注</div>` : ''}
        ${ruleDims.length ? `<div class="ed-blk big">块一 · 规则维度（计算过程）</div><div class="ed-dims">${ruleDims.map(d => dimRow(d, t.case_id)).join('')}</div>` : ''}
        ${llmDims.length ? `<div class="ed-blk big">块二 · 研判维度（judge 判定 + 引用证据）</div><div class="ed-dims">${llmDims.map(d => dimRow(d, t.case_id)).join('')}</div>` : ''}
        ${findings.length ? `<div class="ed-blk big">findings（${findings.length} 条${unvCount ? `，${unvCount} 条未核验` : ''}）</div>
          <div class="ed-findings">${findings.map(findingCard).join('')}</div>` : ''}
        ${!dims.length && !findings.length ? '<div class="ed-empty">该记录暂无维度数据</div>' : ''}
      </div>`;

    // 展开/收起（事件委托）
    container.querySelectorAll('.ed-dim-head').forEach(head => {
      head.addEventListener('click', () => head.parentElement.classList.toggle('open'));
    });
    container.querySelectorAll('.ed-ev').forEach(card => {
      card.addEventListener('click', () => card.classList.toggle('open'));
    });
    // 人工改分（§12）：不冒泡到维度行的展开/收起
    container.querySelectorAll('.ed-manual-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        manualEdit(container, t.case_id, btn.dataset.dim);
      });
    });
  }

  /* ── 人工改分：写 is_auto=0，AI 原分留底 detail_json.ai，改完原地刷新 ── */
  function manualEdit(container, caseId, dimension) {
    const input = prompt(`人工改分 · ${dimension}\n输入新分数（0-100）：`);
    if (input == null) return;
    const score = Number(input);
    if (!isFinite(score) || score < 0 || score > 100) {
      alert('请输入 0-100 的数字');
      return;
    }
    const note = prompt('改分理由 / 备注（会替换原 reason，AI 原分仍保留）：') || '';
    fetch(`/api/eval/cases/${caseId}/scores`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dimension, score, note }),
    })
      .then(async r => {
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || 'HTTP ' + r.status);
        const resp = await fetch(`/api/eval/cases/${caseId}`);
        const fresh = await resp.json();
        render(container, fresh.contract || {}, fresh);
      })
      .catch(e => alert('改分失败: ' + e.message));
  }

  /* ── 便捷入口：拉取 case 契约并弹层展示 ───────────────────────── */
  function open(caseId) {
    let overlay = document.getElementById('edOverlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'edOverlay';
      overlay.className = 'ed-overlay';
      overlay.innerHTML = `<div class="ed-panel">
        <div class="ed-panel-head">
          <span class="ed-panel-title">评测详情</span>
          <button class="ed-close" type="button">✕</button>
        </div>
        <div class="ed-panel-body"></div></div>`;
      document.body.appendChild(overlay);
      overlay.querySelector('.ed-close').addEventListener('click', () => overlay.classList.remove('open'));
      overlay.addEventListener('click', e => {
        if (e.target === overlay) overlay.classList.remove('open');
      });
    }
    const body = overlay.querySelector('.ed-panel-body');
    body.innerHTML = '<div class="ed-loading">加载中…</div>';
    overlay.classList.add('open');
    fetch(`/api/eval/cases/${caseId}`)
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(data => render(body, data.contract || {}, data))
      .catch(e => { body.innerHTML = `<div class="ed-loading">加载失败: ${esc(e.message)}</div>`; });
  }

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.evalDetail = { render, open };
})();
