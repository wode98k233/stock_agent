/**
 * 评测管理 JS 模块 —— 三栏全页面
 * 复用 skill-manager 的页面切换模式
 */
(function () {
  'use strict';

  /* ── DOM 快捷 ─────────────────────────────────────────────── */
  const $page   = () => document.getElementById('evalPage');
  const $chat   = () => document.querySelector('.chat-pane');
  const $list   = () => document.getElementById('evalRunList');
  const $main   = () => document.getElementById('evalMain');
  const $right  = () => document.getElementById('evalRight');
  const $search = () => document.getElementById('evalSearch');
  const $tabs   = () => document.getElementById('evalTabs');
  const $modal  = () => document.getElementById('evalModal');

  let allRuns   = [];
  let selectedId = null;      // 当前选中批次 ID（字符串）
  let selectedCaseId = null;  // 当前选中 Case ID（数字）
  let curTab     = 'matrix'; // matrix | regression | existing | judge | registry（对齐原型五视图）
  let searchQ    = '';

  /* 分数统一转 0-100（DB 存 0-100，兼容 0-1 旧数据） */
  function to100(v) {
    v = Number(v) || 0;
    return v > 1 ? v : v * 100;
  }

  /* ── 数据预处理：把后端返回的 JSON 字符串字段解析成 JS 类型 ── */
  function parseCase(c) {
    if (!c) return c;
    ['expected_tools','expected_sections','judge_criteria','tags'].forEach(k => {
      if (c[k] && typeof c[k] === 'string') {
        try { c[k] = JSON.parse(c[k]); } catch(e) {}
      }
      if (!Array.isArray(c[k])) c[k] = [];
    });
    if (c.reference_facts && typeof c.reference_facts === 'string') {
      try { c.reference_facts = JSON.parse(c.reference_facts); } catch(e) {}
    }
    return c;
  }

  /* ════════════════════════════════════════════════════════
   *  页面切换
   * ════════════════════════════════════════════════════════ */
  function openPage() {
    $page().classList.add('open');
    if ($chat()) $chat().style.display = 'none';
    bindEvents();
    switchTab(curTab);
  }
  function closePage() {
    $page().classList.remove('open');
    if ($chat()) $chat().style.display = '';
    selectedId = null;
    selectedCaseId = null;
    if (reviewTimer) { clearInterval(reviewTimer); reviewTimer = null; }
  }

  /* ── 事件绑定（只绑一次） ─────────────────────────────────── */
  let _bound = false;
  function bindEvents() {
    if (_bound) return;
    _bound = true;
    const nBtn = document.getElementById('evalNewBtn');
    const rBtn = document.getElementById('evalRefreshBtn');
    if (nBtn) nBtn.addEventListener('click', openNewRunModal);
    if (rBtn) rBtn.addEventListener('click', () => { loadRuns(); });
    // 模态框取消 / 关闭
    const m = $modal();
    if (m) {
      m.querySelector('.eval-modal-close')?.addEventListener('click', closeModal);
      m.querySelector('.eval-modal-cancel')?.addEventListener('click', closeModal);
      m.querySelector('.eval-modal-save')?.addEventListener('click', onModalSave);
    }
  }

  /* ════════════════════════════════════════════════════════
   *  批次列表
   * ════════════════════════════════════════════════════════ */
  async function fetchRuns() {
    try {
      const resp = await fetch('/api/eval/runs?limit=100');
      const data = await resp.json();
      allRuns = data.items || [];
    } catch (e) { console.error('[eval] 加载批次失败:', e); }
  }

  async function loadRuns() {
    await fetchRuns();
    renderList();
    if (allRuns.length && !selectedId) {
      selectRun(String(allRuns[0].id));
    }
  }

  function renderList() {
    const el = $list();
    if (!allRuns.length) {
      el.innerHTML = '<div class="eval-empty-hint">暂无评测批次，点击"新建评测"开始</div>';
      return;
    }
    el.innerHTML = allRuns.map(r => {
      const score = r.overall_score != null ? r.overall_score.toFixed(1) : '-';
      const date  = (r.created_at || '').slice(0, 10);
      const tag   = r.tag ? `<span class="eval-run-tag">${esc(r.tag)}</span>` : '';
      const cls   = String(r.id) === selectedId ? 'active' : '';
      return `
        <div class="eval-run-item ${cls}" data-id="${r.id}">
          <div class="eval-run-item-top">
            <span class="eval-run-id">#${r.id}</span>
            ${tag}
            <span class="eval-run-date">${date}</span>
          </div>
          <div class="eval-run-item-bottom">
            <span class="eval-run-mode">${esc(r.mode || '-')}</span>
            <span class="eval-run-score">${score}</span>
          </div>
        </div>`;
    }).join('');

    // 用事件委托，避免引号问题
    el.querySelectorAll('.eval-run-item').forEach(item => {
      item.addEventListener('click', () => selectRun(item.dataset.id));
    });
  }

  /* ════════════════════════════════════════════════════════
   *  选中批次 / Tab 切换
   * ════════════════════════════════════════════════════════ */
  async function selectRun(id) {
    selectedId = String(id);
    selectedCaseId = null;
    renderList();
    await renderMain();
    // 清空右侧
    const right = $right();
    if (right) right.innerHTML = '<div class="eval-empty-hint">选择 Case 查看详情</div>';
  }

  /* 视图切换（对齐原型五视图）。兼容旧 tab 名：overview/cases/benchmark/trend→regression、review→judge、backtest→existing */
  const _LEGACY_VIEW = { overview: 'regression', cases: 'regression',
    benchmark: 'regression', trend: 'regression', review: 'judge', backtest: 'existing' };

  function switchTab(tab) {
    curTab = _LEGACY_VIEW[tab] || tab;
    if (curTab !== 'judge' && reviewTimer) { clearInterval(reviewTimer); reviewTimer = null; }
    document.querySelectorAll('#evalSeg button').forEach(b =>
      b.classList.toggle('active', b.dataset.view === curTab));
    renderMain();
  }

  async function renderMain() {
    const el = $main();
    try {
      if (curTab === 'matrix')         await renderMatrix(el);
      else if (curTab === 'regression') await renderRegression(el);
      else if (curTab === 'existing')   await renderExisting(el);
      else if (curTab === 'judge')      await renderJudge(el);
      else if (curTab === 'registry')   await renderRegistry(el);
      else el.innerHTML = '<div class="eval-empty-hint">未知视图</div>';
    } catch (e) {
      console.error('[eval] 视图渲染失败:', e);
      el.innerHTML = `<div class="eval-review-error">视图加载失败: ${esc(e.message)}</div>`;
    }
  }

  /* ── 原型 section 包装 ───────────────────────────────────── */
  function protoSection(title, tag, body, right = '', flush = false) {
    return `<div class="section"><div class="section-title">${title}<span class="tag">${tag}</span>`
      + `<div class="right">${right}</div></div>`
      + `<div class="section-body${flush ? ' section-bodyFlush' : ''}">${body}</div></div>`;
  }

  function fmtScore(v) {
    return (v == null || !isFinite(Number(v))) ? '-' : Number(v).toFixed(1);
  }

  /* token 用量展示：{input_tokens, output_tokens, total_tokens, calls} */
  function fmtTok(n) {
    n = Number(n);
    if (!isFinite(n) || n <= 0) return '0';
    return n >= 10000 ? (n / 10000).toFixed(1) + '万' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
  }
  function tokenMeta(tu) {
    if (!tu || typeof tu !== 'object' || (!tu.total_tokens && !tu.calls)) return '';
    return `Token <b>${fmtTok(tu.total_tokens)}</b>（入 ${fmtTok(tu.input_tokens)} / 出 ${fmtTok(tu.output_tokens)} · ${tu.calls || 0} 次调用）`;
  }
  function sumTokenUsage(list) {
    const acc = { input_tokens: 0, output_tokens: 0, total_tokens: 0, calls: 0 };
    (list || []).forEach(tu => {
      if (!tu || typeof tu !== 'object') return;
      acc.input_tokens += Number(tu.input_tokens) || 0;
      acc.output_tokens += Number(tu.output_tokens) || 0;
      acc.total_tokens += Number(tu.total_tokens) || 0;
      acc.calls += Number(tu.calls) || 0;
    });
    return acc;
  }

  /* ── Benchmark 题库（题库回归视图内切换）──────────────────── */
  let _bmData = [];
  async function renderBenchmark(el) {
    const resp = await fetch('/api/eval/benchmark?limit=200');
    const data = await resp.json();
    _bmData = (data.items || []).map(c => parseCase(c));

    renderBmTable(el, _bmData);
  }
  function renderBmTable(el, cases) {
    const q = (document.getElementById('evalBmSearch')?.value || '').toLowerCase();
    const cat = document.getElementById('evalBmCat')?.value || '';
    const filtered = cases.filter(c => {
      if (q && !(c.question||'').toLowerCase().includes(q)) return false;
      if (cat && c.category !== cat) return false;
      return true;
    });
    const cats = [...new Set(cases.map(c => c.category))];
    const catOpts = cats.map(c => `<option value="${c}">${c}</option>`).join('');
    // 更新分类下拉（只更新一次）
    const sel = document.getElementById('evalBmCat');
    if (sel && sel.options.length <= 1) {
      sel.innerHTML = '<option value="">全部分类</option>' + catOpts;
    }
    el.innerHTML = `
      <div class="eval-benchmark">
        <div class="eval-benchmark-header">
          <div class="eval-benchmark-title">题库管理（${filtered.length} 条）</div>
          <button class="eval-btn primary" onclick="window.StockRadar?.evalManager?.createCase()">＋ 新建 Case</button>
        </div>
        <div class="eval-benchmark-toolbar">
          <input class="eval-search" type="text" placeholder="搜索问题..." id="evalBmSearch"
                 oninput="window.StockRadar?.evalManager?.filterBenchmark()">
          <select id="evalBmCat" onchange="window.StockRadar?.evalManager?.filterBenchmark()">
            <option value="">全部分类</option>
          </select>
        </div>
        <table class="eval-cases-table">
          <thead><tr><th>ID</th><th>问题</th><th>分类</th><th>难度</th><th>期望工具</th><th>操作</th></tr></thead>
          <tbody id="evalBmBody">
            ${filtered.map(c => `<tr>
              <td>${esc(c.id)}</td>
              <td class="eval-case-q">${esc(c.question||'').slice(0,40)}</td>
              <td>${esc(c.category||'-')}</td>
              <td>${esc(c.difficulty||'-')}</td>
              <td>${(c.expected_tools||[]).join(', ')}</td>
              <td>
                <button class="eval-btn sm" data-act="edit" data-id="${esc(c.id)}">编辑</button>
                <button class="eval-btn sm danger" data-act="del" data-id="${esc(c.id)}">删除</button>
              </td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>`;

    el.querySelectorAll('#evalBmBody button[data-act]').forEach(btn => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const id = btn.dataset.id;
        if (btn.dataset.act === 'edit') editCase(id);
        else deleteCase(id);
      });
    });
  }

  function filterBenchmark() {
    const el = $main();
    if (el) renderBmTable(el, _bmData);
  }

  /* ════════════════════════════════════════════════════════
   *  Case 详情（右侧栏）
   * ════════════════════════════════════════════════════════ */
  async function selectCase(caseId) {
    selectedCaseId = caseId;
    const right = $right();
    if (!right) return;
    try {
      const resp = await fetch(`/api/eval/cases/${caseId}`);
      const data = await resp.json();
      // 契约可用时走统一详情组件（§11.4：计算过程 + 证据链）；
      // 否则退回旧渲染（老数据兼容）
      if (data && data.contract && window.StockRadar && window.StockRadar.evalDetail) {
        window.StockRadar.evalDetail.render(right, data.contract, data);
      } else {
        renderCaseDetail(right, parseCase(data));
      }
    } catch (e) {
      right.innerHTML = `<div class="eval-empty-hint">加载失败: ${e.message}</div>`;
    }
  }

  function renderCaseDetail(el, data) {
    const c = data;
    const scores = data.scores || [];
    const scoreHtml = scores.map(s => `
      <div class="eval-detail-score-row">
        <span class="eval-detail-score-dim">${dimLabel(s.dimension)}</span>
        <span class="eval-detail-score-val ${pctClass(s.score)}">${to100(s.score).toFixed(0)}</span>
        <span class="eval-detail-score-wt">权重 ${s.weight}</span>
      </div>`).join('');

    el.innerHTML = `
      <div class="eval-case-detail">
        <div class="eval-detail-header">
          <div class="eval-detail-title">Case #${c.id}</div>
          <span class="eval-status-${c.status}">${c.status}</span>
        </div>
        <div class="eval-detail-section">
          <div class="eval-detail-label">问题</div>
          <div class="eval-detail-value">${esc(c.question||'')}</div>
        </div>
        <div class="eval-detail-section">
          <div class="eval-detail-label">分数</div>
          <div class="eval-detail-overall">${((c.overall_score||0)).toFixed(1)} / 100</div>
        </div>
        <div class="eval-detail-section">
          <div class="eval-detail-label">各维度</div>
          ${scoreHtml}
        </div>
        <div class="eval-detail-section">
          <div class="eval-detail-label">迭代次数</div>
          <div class="eval-detail-value">${c.iteration_count??'-'}</div>
        </div>
        <div class="eval-detail-section">
          <div class="eval-detail-label">Token（in/out）</div>
          <div class="eval-detail-value">${c.tokens_in??0} / ${c.tokens_out??0}</div>
        </div>
        <div class="eval-detail-section">
          <div class="eval-detail-label">错误信息</div>
          <div class="eval-detail-value" style="color:var(--danger,#e74c3c)">${esc(c.error||'无')}</div>
        </div>
        <div class="eval-detail-section">
          <div class="eval-detail-label">Agent 回答</div>
          <div class="eval-detail-value" style="white-space:pre-wrap;max-height:300px;overflow:auto;">${esc(c.agent_response||'无')}</div>
        </div>
      </div>`;
  }

  /* ════════════════════════════════════════════════════════
   *  新建评测 弹窗
   * ════════════════════════════════════════════════════════ */
  function openNewRunModal() {
    openModal({
      title: '新建评测批次',
      body: `
        <div class="eval-form-group">
          <label>评测模式</label>
          <select id="evalModalMode">
            <option value="">加载中...</option>
          </select>
        </div>
        <div class="eval-form-group">
          <label>标签（可选）</label>
          <input id="evalModalTag" type="text" placeholder="如：v1.2-test" />
        </div>
        <div class="eval-form-group">
          <label>分类（可多选，留空=全部）</label>
          <div id="evalModalCats" class="eval-checkbox-group">
            <label><input type="checkbox" value="basic_query" checked> basic_query</label>
            <label><input type="checkbox" value="stock_analysis" checked> stock_analysis</label>
            <label><input type="checkbox" value="market_overview"> market_overview</label>
            <label><input type="checkbox" value="financials"> financials</label>
            <label><input type="checkbox" value="technical"> technical</label>
            <label><input type="checkbox" value="news_sentiment"> news_sentiment</label>
            <label><input type="checkbox" value="portfolio"> portfolio</label>
            <label><input type="checkbox" value="risk_assessment"> risk_assessment</label>
          </div>
        </div>
        <div class="eval-form-group">
          <label>Judge 模型（可选）</label>
          <input id="evalModalJudge" type="text" placeholder="留空使用默认" />
        </div>`,
      onOpen: async () => {
        // 动态加载可用模式列表
        try {
          const resp = await fetch('/api/modes');
          const data = await resp.json();
          const modes = data.modes || [];
          const sel = document.getElementById('evalModalMode');
          if (sel && modes.length) {
            sel.innerHTML = modes.map(m =>
              `<option value="${m.name}">${m.label || m.name}</option>`
            ).join('');
            // 默认选中第一个
            if (data.current) sel.value = data.current;
          }
        } catch(e) {
          console.warn('[eval] 加载模式失败，使用兜底选项', e);
          const sel = document.getElementById('evalModalMode');
          if (sel) {
            sel.innerHTML = [
              '<option value="react_stock">ReAct</option>',
              '<option value="plan_solve">Plan & Solve</option>',
              '<option value="pdor">PDOR</option>',
              '<option value="unified_plan">Unified Plan</option>',
            ].join('');
          }
        }
        // 动态加载分类列表
        await _loadBmCategories();
        const catContainer = document.getElementById('evalModalCats');
        if (catContainer) {
          catContainer.innerHTML = _bmCategories.map(c =>
            `<label><input type="checkbox" value="${c}"> ${c}</label>`
          ).join('');
        }
      },
      onSave: async () => {
        const mode  = document.getElementById('evalModalMode').value;
        const tag   = document.getElementById('evalModalTag').value.trim();
        const judge = document.getElementById('evalModalJudge').value.trim();
        const cats  = [...document.querySelectorAll('#evalModalCats input:checked')].map(cb => cb.value);
        const body = { mode, tag, categories: cats };
        if (judge) body.judge_model = judge;
        const resp = await fetch('/api/eval/run', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.run_id) {
          closeModal();
          await loadRuns();
          selectRun(String(data.run_id));
        } else {
          alert('创建失败: ' + JSON.stringify(data));
        }
      }
    });
  }

  /* ════════════════════════════════════════════════════════
   *  题库 Case 新建 / 编辑 弹窗
   * ════════════════════════════════════════════════════════ */
  let _bmCategories = []; // 缓存从题库数据提取的分类列表

  async function _loadBmCategories() {
    if (_bmCategories.length) return _bmCategories;
    try {
      const resp = await fetch('/api/eval/benchmark?limit=200');
      const data = await resp.json();
      const cats = [...new Set((data.items || []).map(c => c.category).filter(Boolean))];
      _bmCategories = cats.length ? cats : [
        'basic_query','stock_analysis','market_overview','financials',
        'technical','news_sentiment','portfolio','risk_assessment'
      ];
    } catch(e) {
      _bmCategories = [
        'basic_query','stock_analysis','market_overview','financials',
        'technical','news_sentiment','portfolio','risk_assessment'
      ];
    }
    return _bmCategories;
  }

  function _buildCatSelectOptions(selectedCat) {
    return _bmCategories.map(c =>
      `<option value="${c}" ${c === selectedCat ? 'selected' : ''}>${c}</option>`
    ).join('');
  }

  async function createCase() {
    await _loadBmCategories();
    openModal({
      title: '新建题库 Case',
      body: caseFormHtml(),
      onSave: async () => {
        const body = readCaseForm();
        const resp = await fetch('/api/eval/benchmark', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        closeModal();
        if (curTab === 'benchmark') { const el=$main(); if(el) await renderBenchmark(el); }
      }
    });
  }

  async function editCase(id) {
    // 拉取现有数据
    await _loadBmCategories();
    const resp = await fetch(`/api/eval/benchmark/${id}`);
    const data = parseCase(await resp.json());
    openModal({
      title: '编辑题库 Case',
      body: caseFormHtml(data),
      onSave: async () => {
        const body = readCaseForm();
        await fetch(`/api/eval/benchmark/${id}`, {
          method: 'PUT',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(body),
        });
        closeModal();
        if (curTab === 'benchmark') { const el=$main(); if(el) await renderBenchmark(el); }
      }
    });
  }

  function caseFormHtml(d) {
    d = d || {};
    return `
      <div class="eval-form-group">
        <label>问题 *</label>
        <textarea id="evalCaseQ" rows="3" placeholder="用户输入的问题">${esc(d.question||'')}</textarea>
      </div>
      <div class="eval-form-group">
        <label>分类 *</label>
        <select id="evalCaseCat">
          ${_buildCatSelectOptions(d.category||'')}
        </select>
      </div>
      <div class="eval-form-group">
        <label>难度</label>
        <select id="evalCaseDiff">
          <option value="easy"   ${d.difficulty==='easy'?'selected':''}>easy</option>
          <option value="medium" ${d.difficulty==='medium'?'selected':''}>medium</option>
          <option value="hard"   ${d.difficulty==='hard'?'selected':''}>hard</option>
        </select>
      </div>
      <div class="eval-form-group">
        <label>期望工具（逗号分隔）</label>
        <input id="evalCaseTools" type="text" value="${(d.expected_tools||[]).join(', ')}" placeholder="get_stock_data, analyze_stock" />
      </div>
      <div class="eval-form-group">
        <label>期望章节（逗号分隔）</label>
        <input id="evalCaseSections" type="text" value="${(d.expected_sections||[]).join(', ')}" placeholder="行情概览, 估值, 风险提示" />
      </div>
      <div class="eval-form-group">
        <label>标签（逗号分隔）</label>
        <input id="evalCaseTags" type="text" value="${(d.tags||[]).join(', ')}" placeholder="标签1, 标签2" />
      </div>
      <div class="eval-form-group">
        <label>参考答案（可选）</label>
        <textarea id="evalCaseAnswer" rows="3" placeholder="人工标准答案，供研判与人工复核对照">${esc(d.expected_answer||'')}</textarea>
      </div>
      <div class="eval-form-group">
        <label><input id="evalCaseEnabled" type="checkbox" ${d.enabled!==false?'checked':''}> 启用</label>
      </div>`;
  }

  function readCaseForm() {
    return {
      question:            document.getElementById('evalCaseQ').value.trim(),
      category:            document.getElementById('evalCaseCat').value,
      difficulty:          document.getElementById('evalCaseDiff').value,
      expected_tools:      document.getElementById('evalCaseTools').value.split(',').map(s=>s.trim()).filter(Boolean),
      expected_sections:   document.getElementById('evalCaseSections').value.split(',').map(s=>s.trim()).filter(Boolean),
      tags:                document.getElementById('evalCaseTags').value.split(',').map(s=>s.trim()).filter(Boolean),
      expected_answer:     document.getElementById('evalCaseAnswer').value.trim() || null,
      enabled:             document.getElementById('evalCaseEnabled').checked,
    };
  }

  async function deleteCase(id) {
    if (!confirm('确认删除题库 Case #' + id + '？')) return;
    await fetch(`/api/eval/benchmark/${id}`, { method: 'DELETE' });
    if (curTab === 'benchmark') { const el=$main(); if(el) await renderBenchmark(el); }
  }

  /* ════════════════════════════════════════════════════════
   *  通用模态框
   * ════════════════════════════════════════════════════════ */
  function openModal(opts) {
    const m = $modal();
    if (!m) { alert('模态框 HTML 未找到，请联系开发者'); return; }
    m.querySelector('.eval-modal-title').textContent = opts.title || '';
    m.querySelector('.eval-modal-body').innerHTML = opts.body || '';
    m.querySelector('.eval-modal-save').onclick = opts.onSave || closeModal;
    m.classList.add('open');
    // 弹窗打开后的回调（如动态加载数据）
    if (opts.onOpen) { opts.onOpen(); }
  }
  function closeModal() {
    $modal()?.classList.remove('open');
  }
  function onModalSave() {
    // 由 openModal 动态绑定
  }

  /* ════════════════════════════════════════════════════════
   *  工具函数
   * ════════════════════════════════════════════════════════ */
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  /* ── 维度标签：唯一来源是 /api/eval/dimensions（维度注册表） ──────
   * ensureDimLabels() 在概览渲染前拉一次并缓存；拉不到时退回内置映射
   * （仅旧数据兼容），未知键兜底显示 key 本身。 */
  let _dimLabelMap = null;
  let _dimLabelsPromise = null;
  const _DIM_LABEL_FALLBACK = {
    // v1 旧评测维度（历史数据兼容）
    tool_correctness: '工具调用', factual_accuracy: '事实准确',
    reasoning_quality: '推理质量', efficiency: '效率',
    // v1 旧评审维度（历史数据兼容）
    skill_selection: '技能选择', skill_applicability: '技能适配',
    tool_usage: '工具使用', answer_groundedness: '结论有据',
  };
  function ensureDimLabels() {
    if (_dimLabelMap) return Promise.resolve(_dimLabelMap);
    if (_dimLabelsPromise) return _dimLabelsPromise;
    _dimLabelsPromise = fetch('/api/eval/dimensions')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        const map = {};
        (data && data.dimensions || []).forEach(d => { map[d.key] = d.label; });
        _dimLabelMap = Object.assign({}, _DIM_LABEL_FALLBACK, map);
        return _dimLabelMap;
      })
      .catch(() => (_dimLabelMap = Object.assign({}, _DIM_LABEL_FALLBACK)));
    return _dimLabelsPromise;
  }
  function dimLabel(d) {
    if (_dimLabelMap && _dimLabelMap[d]) return _dimLabelMap[d];
    return _DIM_LABEL_FALLBACK[d] || d;
  }
  function pctClass(s) {
    if (s == null) return '';
    if (s >= 0.8 || s >= 80) return 'high';
    if (s >= 0.6 || s >= 60) return 'mid';
    return 'low';
  }

  /* ── 对话评审渲染工具 ─────────────────────────────────────── */
  function scoreLevel(s) {
    if (s == null || s === '') return 'muted';
    const v = Number(s);
    if (!isFinite(v)) return 'muted';
    if (v >= 80) return 'high';
    if (v >= 60) return 'mid';
    return 'low';
  }

  function reviewStatusPill(status, overall) {
    if (status === 'running') return '<span class="eval-review-pill running">进行中</span>';
    if (status === 'failed') return '<span class="eval-review-pill low">失败</span>';
    if (status === 'done') {
      return `<span class="eval-review-pill ${scoreLevel(overall)}">${overall == null ? '-' : overall}</span>`;
    }
    return '<span class="eval-review-pill muted">未评审</span>';
  }

  function reviewTime(r) {
    const raw = (r && (r.finished_at || r.created_at)) || '';
    return esc(raw.slice(0, 16).replace('T', ' '));
  }

  function dimRows(scores) {
    const dims = scores && typeof scores === 'object' ? scores : {};
    const keys = Object.keys(dims);
    if (!keys.length) return '';
    return '<div class="eval-review-dimgrid">' + keys.map(k => {
      const v = Number(dims[k]);
      const lv = scoreLevel(v);
      const w = isFinite(v) ? Math.max(0, Math.min(100, v)) : 0;
      return `<div class="eval-review-dimrow"><span class="name">${esc(dimLabel(k))}</span>`
        + `<span class="eval-review-bar"><i class="${lv}" style="width:${w}%"></i></span>`
        + `<span class="val">${isFinite(v) ? v : '-'}</span></div>`;
    }).join('') + '</div>';
  }

  function findingsHtml(findings, refKey) {
    const items = Array.isArray(findings) ? findings : [];
    if (!items.length) return '';
    return '<div class="eval-review-findings">' + items.map(f => {
      const refVal = f[refKey];
      const ref = refVal
        ? `<span class="ref">${refKey === 'step_ref' ? 'step ' + esc(refVal) : '第 ' + esc(refVal) + ' 轮'}</span>`
        : '';
      const issues = (Array.isArray(f.issues) ? f.issues : [])
        .map(x => `<div class="line issue">· ${esc(x)}</div>`).join('');
      const fixes = (Array.isArray(f.suggestions) ? f.suggestions : [])
        .map(x => `<div class="line fix">→ ${esc(x)}</div>`).join('');
      return `<div class="eval-review-finding"><div class="head">${ref}`
        + `<span class="dim">${esc(dimLabel(f.dimension || ''))}</span></div>`
        + `<div class="verdict">${esc(f.verdict || '')}</div>${issues}${fixes}</div>`;
    }).join('') + '</div>';
  }

  /* ── 可视化图表 ─────────────────────────────────────────── */
  let _radarChart = null;
  let _distChart  = null;

  /**
   * 手动接管 canvas 尺寸 — 关键！
   * Chart.js responsive:true 会覆盖我们设的 width/height，
   * 所以必须 responsive:false + 手动设物理像素。
   *
   * @param {HTMLCanvasElement} canvas
   * @param {number} cssWidth  - 显示宽度 (CSS px)
   * @param {number} cssHeight - 显示高度 (CSS px)
   * @returns {number} dpr
   */
  function _setCanvasSize(canvas, cssWidth, cssHeight) {
    const dpr = window.devicePixelRatio || 1;
    // 物理像素 = 显示像素 × 设备像素比
    canvas.width  = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    // CSS 控制显示大小
    canvas.style.width  = cssWidth + 'px';
    canvas.style.height = cssHeight + 'px';
    return dpr;
  }

  function renderRadarChart(scores) {
    const canvas = document.getElementById("evalRadarChart");
    if (!canvas) return;
    if (_radarChart) { _radarChart.destroy(); _radarChart = null; }

    // 取父容器可用宽度，决定画布显示尺寸（保持正方形）
    const container = canvas.parentElement;
    const availW = container ? container.clientWidth : 380;
    const size = Math.min(Math.max(availW - 16, 240), 420); // 正方形边长

    // ★ 手动设 canvas 物理像素（responsive:false 才不会被覆盖）
    const dpr = _setCanvasSize(canvas, size, size);

    // 标签来自维度注册表（ensureDimLabels 已在概览入口拉取），不再硬编码
    const labels = scores.map(s => dimLabel(s.dimension));
    const data   = scores.map(s => {
      const v = s.score == null ? 0 : s.score;
      return v > 1 ? v : v * 100;
    });
    const ctx = canvas.getContext("2d");
    _radarChart = new Chart(ctx, {
      type: "radar",
      data: { labels, datasets: [{ label: "评分", data,
        fill: true,
        backgroundColor: "rgba(99,102,241,0.12)",
        borderColor: "rgba(99,102,241,0.75)",
        pointBackgroundColor: "#6366f1",
        pointBorderColor: "#fff",
        pointBorderWidth: 1.5,
        pointRadius: 4,
        pointHoverRadius: 6,
        borderWidth: 2,
      }] },
      options: {
        responsive: false,           // ← 关掉！我们自己控尺寸
        devicePixelRatio: dpr,
        layout: { padding: 10 },
        scales: {
          r: {
            min: 0, max: 100,
            ticks: {
              stepSize: 25, color: '#94a3b8',
              backdropColor: 'transparent',
              font: { size: 11 },
              padding: 6,
            },
            grid: { color: 'rgba(148,163,184,0.20)' },
            angleLines: { color: 'rgba(148,163,184,0.20)' },
            pointLabels: {
              color: '#334155',
              font: { size: 12, weight: '500' },
              padding: 12,
            }
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: 'rgba(15,23,42,0.88)',
            titleFont: { size: 12 },
            bodyFont: { size: 12 },
            padding: 10,
            cornerRadius: 6,
            callbacks: {
              label: function(c) { return c.label + ': ' + c.raw.toFixed(0) + ' 分'; }
            }
          }
        },
        animation: { duration: 700, easing: 'easeOutQuart' },
      },
    });
  }

  function renderDistChart(cases) {
    const canvas = document.getElementById("evalDistChart");
    if (!canvas) return;
    if (_distChart) { _distChart.destroy(); _distChart = null; }

    // 取父容器可用宽度，决定画布显示尺寸（宽 > 高）
    const container = canvas.parentElement;
    const availW = container ? container.clientWidth : 380;
    const w = Math.min(Math.max(availW - 16, 280), 500);
    const h = Math.round(w / 2.2); // 宽高比 ~2.2:1

    // ★ 手动设 canvas 物理像素
    const dpr = _setCanvasSize(canvas, w, h);

    const bins = [0,0,0,0,0];
    cases.forEach(c => {
      const s = c.overall_score || 0;
      if (s < 60)      bins[0]++;
      else if (s < 70) bins[1]++;
      else if (s < 80) bins[2]++;
      else if (s < 90) bins[3]++;
      else               bins[4]++;
    });
    const barColors = [
      'rgba(239,68,68,0.78)',
      'rgba(245,158,11,0.78)',
      'rgba(59,130,246,0.78)',
      'rgba(34,197,94,0.78)',
      'rgba(16,185,129,0.88)',
    ];
    const hoverColors = [
      'rgba(239,68,68,0.95)',
      'rgba(245,158,11,0.95)',
      'rgba(59,130,246,0.95)',
      'rgba(34,197,94,0.95)',
      'rgba(16,185,129,1)',
    ];
    const ctx = canvas.getContext("2d");
    _distChart = new Chart(ctx, {
      type: "bar",
      data: { labels: ["<60", "60-69", "70-79", "80-89", "90+"],
        datasets: [{
          label: "Case 数",
          data: bins,
          backgroundColor: barColors,
          hoverBackgroundColor: hoverColors,
          borderRadius: 6,
          borderSkipped: false,
          maxBarThickness: 48,
          barPercentage: 0.6,
        }]
      },
      options: {
        responsive: false,           // ← 关掉！我们自己控尺寸
        devicePixelRatio: dpr,
        layout: { padding: { top: 10, bottom: 4 } },
        scales: {
          y: {
            beginAtZero: true,
            ticks: {
              stepSize: 1,
              color: '#94a3b8',
              font: { size: 11 },
              padding: 8,
            },
            grid: { color: 'rgba(148,163,184,0.15)' },
            border: { display: false },
          },
          x: {
            ticks: {
              color: '#64748b',
              font: { size: 11, weight: '500' },
              padding: 8,
            },
            grid: { display: false },
            border: { display: false },
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: 'rgba(15,23,42,0.88)',
            titleFont: { size: 12, weight: '600' },
            bodyFont: { size: 12 },
            padding: 10,
            cornerRadius: 6,
            displayColors: true,
            boxPadding: 4,
            callbacks: {
              title: function(c) { return '分数区间: ' + c[0].label; },
              label: function(c) { return 'Case 数量: ' + c.raw; }
            }
          }
        },
        animation: { duration: 600, easing: 'easeOutQuart' },
      },
    });
  }

  /* ── 趋势 + 批次对比 ──────────────────────────────────────── */
  let _trendChart = null;
  let _cmpChart  = null;

  async function renderTrend(el) {
    // 批次下拉选项（对比用）
    const opts = allRuns.map(r =>
      `<option value="${esc(r.id)}">#${esc(r.id)}${r.tag ? ' · ' + esc(r.tag) : ''} · ${to100(r.overall_score).toFixed(0)}分</option>`).join('');

    el.innerHTML = `
      <div class="eval-overview">
        <div class="eval-chart-row">
          <div class="eval-chart-box" style="flex:2;">
            <h3>批次总分趋势</h3>
            <div class="chart-canvas-wrap"><canvas id="evalTrendChart"></canvas></div>
          </div>
          <div class="eval-chart-box" style="flex:1;">
            <h3>批次对比（五维）</h3>
            <div class="eval-form-group" style="margin:6px 0;">
              <select id="evalCmpA" class="eval-search" style="width:100%;">${opts}</select>
            </div>
            <div class="eval-form-group" style="margin:6px 0;">
              <select id="evalCmpB" class="eval-search" style="width:100%;">${opts}</select>
            </div>
            <button class="eval-btn primary" id="evalCmpBtn" style="width:100%;">对 比</button>
          </div>
        </div>
        <div class="eval-chart-row">
          <div class="eval-chart-box" style="flex:1;">
            <h3>对比结果</h3>
            <div class="chart-canvas-wrap"><canvas id="evalCmpChart"></canvas></div>
            <div id="evalCmpTable" class="eval-cmp-table"></div>
          </div>
        </div>
      </div>`;

    document.getElementById('evalCmpBtn')?.addEventListener('click', () => loadCompare());
    // 默认对比最近两个批次
    if (allRuns.length >= 2) {
      const a = document.getElementById('evalCmpA'), b = document.getElementById('evalCmpB');
      if (a) a.value = String(allRuns[1].id);
      if (b) b.value = String(allRuns[0].id);
    }
    await drawTrendChart();
    if (allRuns.length >= 2) await loadCompare();
  }

  async function drawTrendChart() {
    const canvas = document.getElementById('evalTrendChart');
    if (!canvas || !window.Chart) return;
    let items = [];
    try {
      const resp = await fetch('/api/eval/trend?limit=20');
      items = (await resp.json()).items || [];
    } catch (e) { console.error('[eval] 加载趋势失败:', e); return; }
    // 时间正序
    items.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));

    const labels = items.map(r => {
      const d = String(r.created_at || '').slice(5, 10);
      return `${d} #${String(r.id).slice(0, 6)}`;
    });
    const data = items.map(r => to100(r.avg_score));

    if (_trendChart) { _trendChart.destroy(); _trendChart = null; }
    const container = canvas.parentElement;
    const w = Math.min(Math.max((container ? container.clientWidth : 500) - 16, 280), 640);
    const h = Math.round(w / 2.4);
    const dpr = _setCanvasSize(canvas, w, h);

    _trendChart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { labels, datasets: [{
        label: '批次总分',
        data,
        borderColor: 'rgba(99,102,241,0.85)',
        backgroundColor: 'rgba(99,102,241,0.10)',
        fill: true, tension: .35,
        pointRadius: 4, pointHoverRadius: 6,
        pointBackgroundColor: '#6366f1', pointBorderColor: '#fff', pointBorderWidth: 1.5,
      }] },
      options: {
        responsive: false, devicePixelRatio: dpr,
        layout: { padding: { top: 10, bottom: 4 } },
        scales: {
          y: { beginAtZero: true, max: 100, ticks: { color: '#94a3b8', font: { size: 11 } }, grid: { color: 'rgba(148,163,184,0.15)' }, border: { display: false } },
          x: { ticks: { color: '#64748b', font: { size: 10 }, maxRotation: 45 }, grid: { display: false }, border: { display: false } },
        },
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: 'rgba(15,23,42,0.88)', padding: 10, cornerRadius: 6,
            callbacks: { label: c => `总分: ${Number(c.raw).toFixed(1)} 分` } },
        },
        animation: { duration: 600, easing: 'easeOutQuart' },
      },
    });
  }

  async function loadCompare() {
    const a = document.getElementById('evalCmpA')?.value;
    const b = document.getElementById('evalCmpB')?.value;
    if (!a || !b) return;
    const resp = await fetch(`/api/eval/compare?run_a=${encodeURIComponent(a)}&run_b=${encodeURIComponent(b)}`);
    const data = await resp.json();
    const sa = data.run_a || [], sb = data.run_b || [];
    const dims = [...new Set([...sa.map(s => s.dimension), ...sb.map(s => s.dimension)])];
    const mapA = Object.fromEntries(sa.map(s => [s.dimension, to100(s.score)]));
    const mapB = Object.fromEntries(sb.map(s => [s.dimension, to100(s.score)]));

    // 雷达对比图
    const canvas = document.getElementById('evalCmpChart');
    if (canvas && window.Chart) {
      if (_cmpChart) { _cmpChart.destroy(); _cmpChart = null; }
      const container = canvas.parentElement;
      const size = Math.min(Math.max((container ? container.clientWidth : 380) - 16, 240), 380);
      const dpr = _setCanvasSize(canvas, size, size);
      _cmpChart = new Chart(canvas.getContext('2d'), {
        type: 'radar',
        data: { labels: dims.map(d => dimLabel(d)), datasets: [
          { label: `#${a}`, data: dims.map(d => mapA[d] ?? 0), fill: true,
            backgroundColor: 'rgba(99,102,241,0.12)', borderColor: 'rgba(99,102,241,0.75)', pointRadius: 3, borderWidth: 2 },
          { label: `#${b}`, data: dims.map(d => mapB[d] ?? 0), fill: true,
            backgroundColor: 'rgba(34,197,94,0.10)', borderColor: 'rgba(34,197,94,0.75)', pointRadius: 3, borderWidth: 2 },
        ] },
        options: {
          responsive: false, devicePixelRatio: dpr, layout: { padding: 10 },
          scales: { r: { min: 0, max: 100, ticks: { stepSize: 25, color: '#94a3b8', backdropColor: 'transparent', font: { size: 10 } },
            grid: { color: 'rgba(148,163,184,0.20)' }, angleLines: { color: 'rgba(148,163,184,0.20)' },
            pointLabels: { color: '#334155', font: { size: 11 }, padding: 8 } } },
          plugins: { legend: { display: true, labels: { font: { size: 11 } } } },
          animation: { duration: 600 },
        },
      });
    }

    // 明细表
    const table = document.getElementById('evalCmpTable');
    if (table) {
      table.innerHTML = `<table class="eval-cases-table">
        <thead><tr><th>维度</th><th>#${esc(a)}</th><th>#${esc(b)}</th><th>Δ</th></tr></thead>
        <tbody>${dims.map(d => {
          const va = mapA[d] ?? 0, vb = mapB[d] ?? 0, diff = vb - va;
          const cls = diff > 0.05 ? 'high' : (diff < -0.05 ? 'low' : 'mid');
          return `<tr><td>${dimLabel(d)}</td>
            <td>${va.toFixed(1)}</td><td>${vb.toFixed(1)}</td>
            <td class="${cls}">${diff >= 0 ? '+' : ''}${diff.toFixed(1)}</td></tr>`;
        }).join('')}</tbody></table>`;
    }
  }


  /* ════════════════════════════════════════════════════════
   *  对话评审 tab
   * ════════════════════════════════════════════════════════ */
  let reviewDialogId = null;
  let reviewData = null;
  let reviewVersion = null;
  let reviewTimer = null;
  let reviewError = '';
  let reviewDialogsCache = [];

  async function loadReviewDialogs() {
    const box = document.getElementById('judgeDlgList') || $list();
    if (!box) return;
    try {
      const r = await fetch('/api/eval/reviews/dialogs');
      reviewDialogsCache = r.ok ? ((await r.json()).items || []) : [];
    } catch (e) { reviewDialogsCache = []; }
    if (reviewDialogId && !reviewDialogsCache.some(d => d.dialog_uuid === reviewDialogId)) {
      reviewDialogId = null;
    }
    if (!reviewDialogsCache.length) {
      box.innerHTML = '<div class="eval-empty-hint">暂无对话</div>';
      return;
    }
    box.innerHTML = reviewDialogsCache.map(d => {
      const review = d.review || {};
      const scored = review.status === 'done' && review.overall != null;
      const cls = scored ? scoreLevel(review.overall) : '';
      const score = scored ? Number(review.overall).toFixed(1)
        : (review.status === 'running' ? '…' : '-');
      return `<div class="eval-run-item ${d.dialog_uuid === reviewDialogId ? 'active' : ''}" data-uuid="${esc(d.dialog_uuid)}">`
        + `<div class="eval-run-item-top"><span class="eval-review-dlg-title">${esc(d.title || '未命名')}</span></div>`
        + `<div class="eval-run-item-bottom"><span class="eval-run-date">${esc((d.updated_at || '').slice(0, 10))}</span>`
        + `<span class="eval-run-score ${cls}">${score}</span></div></div>`;
    }).join('');
    box.querySelectorAll('.eval-run-item').forEach(item => {
      item.addEventListener('click', () => selectReviewDialog(item.dataset.uuid));
    });
  }

  async function selectReviewDialog(uuid) {
    reviewDialogId = uuid;
    reviewVersion = null;
    reviewError = '';
    await loadReviewDialogs();
    await refreshDialogReview();
  }

  async function startDialogReview() {
    if (!reviewDialogId) return;
    const btn = document.getElementById('evalReviewStartBtn');
    if (btn) { btn.disabled = true; btn.textContent = '评审中...'; }
    reviewError = '';
    try {
      const r = await fetch('/api/eval/reviews', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dialog_uuid: reviewDialogId }),
      });
      const data = r.ok ? await r.json() : {};
      if (data.error) reviewError = data.error;
    } catch (e) { reviewError = '请求失败'; }
    await refreshDialogReview();
    await loadReviewDialogs();
  }

  async function refreshDialogReview() {
    if (!reviewDialogId) return;
    const detail = document.getElementById('evalReviewDetail');
    if (!detail) return;
    try {
      const r = await fetch(`/api/eval/reviews?dialog_uuid=${encodeURIComponent(reviewDialogId)}`);
      reviewData = r.ok ? await r.json() : null;
    } catch (e) { reviewData = null; }
    renderDialogReview();
    const reviews = (reviewData && reviewData.dialog_reviews) || [];
    if (reviewTimer) { clearInterval(reviewTimer); reviewTimer = null; }
    if (reviews.some(x => x.status === 'running')) {
      reviewTimer = setInterval(async () => {
        await refreshDialogReview();
        await loadReviewDialogs();
      }, 3000);
    }
  }

  function switchReviewVersion(uuid) { reviewVersion = uuid; renderDialogReview(); }

  function renderDialogReview() {
    const detail = document.getElementById('evalReviewDetail');
    if (!detail) return;
    if (!reviewData) {
      detail.innerHTML = '<div class="eval-review"><div class="eval-review-empty">评审数据加载失败</div></div>';
      return;
    }
    const reviews = reviewData.dialog_reviews || [];
    if (!reviews.length) reviewVersion = null;
    else if (!reviewVersion || !reviews.some(x => x.review_uuid === reviewVersion)) {
      reviewVersion = reviews[0].review_uuid;
    }
    const cur = reviews.find(x => x.review_uuid === reviewVersion) || null;
    const running = reviews.some(x => x.status === 'running');

    let html = '<div class="eval-review">';
    html += '<div class="eval-review-head"><div>'
      + `<div class="eval-review-title">${esc(reviewData.title || '未命名对话')}</div>`
      + `<div class="eval-review-meta">${(reviewData.runs || []).length} 轮对话`
      + `${reviews.length ? ` · ${reviews.length} 个评审版本` : ' · 尚未评审'}</div>`
      + '</div>'
      + `<button class="eval-btn primary" id="evalReviewStartBtn" onclick="window.StockRadar?.evalManager?.startDialogReview()">`
      + `${reviews.length ? '重新评审' : '开始评审'}</button></div>`;
    if (reviewError) html += `<div class="eval-review-error">${esc(reviewError)}</div>`;

    if (reviews.length) {
      html += '<div class="eval-review-versions"><span class="label">版本</span>'
        + reviews.map(x =>
            `<button class="eval-review-version ${x.review_uuid === reviewVersion ? 'active' : ''}" `
            + `onclick="window.StockRadar?.evalManager?.switchReviewVersion('${x.review_uuid}')">`
            + `<span>${esc(x.rubric_version || 'v?')}</span>`
            + reviewStatusPill(x.status, x.overall)
            + `<span class="time">${reviewTime(x)}</span></button>`).join('')
        + '</div>';
    }

    if (!reviews.length) {
      html += '<div class="eval-review-empty">还没有评审记录 —— 点右上「开始评审」生成 v1：<br>'
        + '先逐轮评审每次执行（技能选择 / 工具使用 / 结论依据），再做跨轮综合（承接 / 约束 / 一致性）。</div>';
    } else if (running && (!cur || cur.status === 'running')) {
      html += '<div class="eval-review-skeleton"><div class="sk-line w40"></div>'
        + '<div class="sk-line w80"></div><div class="sk-line w60"></div>'
        + '<div class="sk-line w80"></div></div>';
    } else if (cur) {
      html += renderDialogReviewCard(cur);
    }

    const turnUsage = (reviewData.runs || [])
      .map(r => r.review && r.review.inputs && r.review.inputs.token_usage)
      .concat([(cur && cur.inputs && cur.inputs.token_usage) || null]);
    const totalUsage = sumTokenUsage(turnUsage);
    html += '<div class="eval-review-section-title">逐轮链路'
      + `<span class="hint">点击轮次展开该轮评审细节（维度 / 问题 / 改进建议）</span>`
      + (totalUsage.calls ? `<span class="hint" style="margin-left:auto">本次评审共耗费 ${tokenMeta(totalUsage)}</span>` : '')
      + '</div>';
    html += `<div class="eval-review-turns">${renderReviewTurnChain(reviewData.runs || [], cur)}</div>`;
    html += '</div>';
    detail.innerHTML = html;
    reviewError = '';
  }

  function renderDialogReviewCard(cur) {
    const meta = [cur.judge_model ? esc(cur.judge_model) : '', 'rubric ' + esc(cur.rubric_version || ''),
      reviewTime(cur), cur.duration_ms ? Math.round(cur.duration_ms / 1000) + 's' : '',
      tokenMeta(cur.inputs && cur.inputs.token_usage)]
      .filter(Boolean).join(' · ');
    let html = '<div class="eval-review-summary"><div class="eval-review-summary-top">';
    if (cur.status === 'failed') {
      html += '<span class="eval-review-pill low">评审失败</span>';
    } else {
      const cls = scoreLevel(cur.overall);
      html += `<span class="eval-review-bigscore ${cls}">${cur.overall == null ? '-' : cur.overall}</span>`
        + `<span class="eval-review-pill ${cls}">跨轮综合</span>`;
    }
    html += `<span class="eval-review-summary-meta">${meta}</span></div>`;
    if (cur.status === 'failed') {
      return html + `<div class="eval-review-error">${esc(cur.error || '未知错误')}</div></div>`;
    }
    html += dimRows(cur.scores);
    const summary = (cur.inputs && cur.inputs.summary) || '';
    if (summary) html += `<p class="eval-review-summary-text">${esc(summary)}</p>`;
    html += findingsHtml(cur.findings, 'turn_index');
    html += '</div>';
    return html;
  }

  function renderReviewTurnChain(runs, cur) {
    if (!runs.length) return '<div class="eval-review-empty">没有可展示的轮次</div>';
    const expected = {};
    const inputs = (cur && cur.inputs) || {};
    for (const t of (inputs.turns || [])) expected[t.run_id] = t.review_id;
    return runs.map(run => {
      const review = run.review || {};
      const hasReview = !!review.review_uuid;
      const mismatched = !!(cur && expected[run.run_id] && review.review_uuid
        && expected[run.run_id] !== review.review_uuid);
      let body = '';
      if (!hasReview) {
        body = `<div class="turn-empty">${run.run_id ? '该轮暂未生成评审' : '该轮没有 trace，未参与逐轮评审'}</div>`;
      } else if (review.status === 'running') {
        body = '<div class="turn-empty">该轮评审进行中…</div>';
      } else if (review.status === 'failed') {
        body = `<div class="turn-empty">评审失败：${esc(review.error || '')}</div>`;
      } else {
        body = dimRows(review.scores) + findingsHtml(review.findings, 'step_ref');
        const rInputs = review.inputs || {};
        if (rInputs.summary) body += `<p class="eval-review-summary-text">${esc(rInputs.summary)}</p>`;
        const caps = Array.isArray(rInputs.capabilities_required) ? rInputs.capabilities_required : [];
        if (caps.length) {
          body += '<div class="eval-review-caps">'
            + caps.map(c => `<span class="eval-review-cap">${esc(c)}</span>`).join('') + '</div>';
        }
      }
      if (hasReview) {
        const tu = (review.inputs || {}).token_usage;
        body += `<div class="turn-meta">run ${esc(run.run_id || '')} · ${esc(review.judge_model || '')} · ${reviewTime(review)}`
          + (tu ? ` · Token ${fmtTok(tu.total_tokens)}` : '') + '</div>';
      }
      return `<details class="eval-review-turn"><summary>`
        + `<span class="turn-no">第 ${run.turn_index} 轮</span>`
        + reviewStatusPill(hasReview ? review.status : '', review.overall)
        + `<span class="turn-q">${esc(run.question || '')}</span>`
        + (mismatched ? '<span class="eval-review-pill muted">该版本未采用</span>' : '')
        + '<span class="turn-caret">▾</span></summary>'
        + `<div class="turn-body"><div class="turn-answer">${esc(run.answer_excerpt || '（无回答）')}</div>`
        + body + '</div></details>';
    }).join('');
  }

  /* ════════════════════════════════════════════════════════
   *  视图一：评测矩阵（原型 view-matrix）
   * ════════════════════════════════════════════════════════ */
  async function renderMatrix(el) {
    await fetchRuns();
    await ensureDimLabels();
    let count = { run_count: 0, reviewed_run_count: 0, dialog_reviewed_count: 0 };
    try { count = await (await fetch('/api/eval/backtest/count')).json(); } catch (e) { /* 降级为 0 */ }
    const fresh = allRuns.filter(r => r.mode !== 'existing');
    const latest = fresh[0];
    const prev = fresh[1];
    let aFind = '暂无批次';
    if (latest && latest.overall_score != null) {
      aFind = `最近批次 ${Number(latest.overall_score).toFixed(1)}`;
      if (prev && prev.overall_score != null) {
        const d = latest.overall_score - prev.overall_score;
        aFind += ` · ${d >= 0 ? '▲' : '▼'}${Math.abs(d).toFixed(1)}`;
      }
    }
    el.innerHTML = `
      <div class="section">
        <div class="section-title">评测矩阵<span class="tag">三类入口，同源：一套维度注册表 + 一套取证层 + 一套证据校验 + 一套三级下钻</span></div>
        <div class="section-body">
          <div class="matrix">
            <div class="entry" data-go="regression">
              <div class="jump">→</div>
              <div class="head"><span class="code">A</span><span class="title">题库回归</span></div>
              <div class="desc">有题库，<b>新跑 agent</b>。逐 case 打分 → 批次可比，判断改完 prompt / 技能后是否退化。</div>
              <div class="meta"><span class="chip fresh">来源：新跑</span><span class="chip reg">全规则</span><span class="chip run">run / 多轮脚本</span></div>
              <div class="find">${esc(aFind)}</div>
            </div>
            <div class="entry hot" data-go="existing">
              <div class="jump">→</div>
              <div class="head"><span class="code">B</span><span class="title">存量回测 <span class="new-badge">NEW</span></span></div>
              <div class="desc">不重跑 agent，直接用<b>历史 run</b>评测。换维度体系后可对同一批历史重新打分。</div>
              <div class="meta"><span class="chip existing">来源：历史存量</span><span class="chip reg">规则分</span><span class="chip diag">研判分</span></div>
              <div class="find">${count.run_count} 条历史 run 可直接评</div>
            </div>
            <div class="entry" data-go="judge">
              <div class="jump">→</div>
              <div class="head"><span class="code">C</span><span class="title">Agent 增强研判</span></div>
              <div class="desc">无题库，对单条 run / 单段对话做<b>深度研判</b>，产出 findings + 证据链。即现有 AI 评审，原样保留。</div>
              <div class="meta"><span class="chip diag">深度研判</span><span class="chip run">run</span><span class="chip dialog">dialog</span></div>
              <div class="find">${count.dialog_reviewed_count} 个 dialog 已评</div>
            </div>
          </div>
          <div class="banner" style="margin-top:16px">B 与 C 是两件事：<b>B 批量打分</b>（量化、分布、趋势、可比）＝回测历史质量；<b>C 单条深挖</b>（findings + 证据链）＝定位具体痛点。共用一个维度注册表，不互相替代。</div>
          <div class="banner warn" style="margin-top:8px"><b>三级下钻（三入口共用）</b>：仪表盘回答「整体好不好」→ 列表回答「哪几条差」→ <b>详情回答「这一条为什么得这个分」</b>。点列表行即可进入详情。</div>
        </div>
      </div>`;
    el.querySelectorAll('.entry').forEach(card => {
      card.addEventListener('click', () => switchTab(card.dataset.go));
    });
  }

  /* ════════════════════════════════════════════════════════
   *  视图二：题库回归（原型 view-regression）
   * ════════════════════════════════════════════════════════ */
  let regRunId = null;
  let regShowBenchmark = false;

  async function renderRegression(el) {
    await fetchRuns();
    await ensureDimLabels();
    const freshRuns = allRuns.filter(r => r.mode !== 'existing');
    if (!regRunId || !freshRuns.some(r => String(r.id) === regRunId)) {
      regRunId = freshRuns.length ? String(freshRuns[0].id) : null;
    }
    if (!regRunId) {
      el.innerHTML = protoSection('题库回归', '有题库，新跑 agent；同一题库两次运行逐维度可比',
        '<div class="eval-empty-hint" style="padding:20px">暂无回归批次 —— 点右上「＋ 新建评测」发起一次</div>',
        '<button class="btn primary" onclick="window.StockRadar?.evalManager?.openNewRunModal()">跑一批次</button>');
      return;
    }
    const idx = freshRuns.findIndex(r => String(r.id) === regRunId);
    const prevRun = freshRuns[idx + 1] || null;   // 列表按时间倒序 → 下一项是更早批次
    const [runResp, casesResp, prevData] = await Promise.all([
      fetch(`/api/eval/runs/${regRunId}`).then(r => r.json()),
      fetch(`/api/eval/runs/${regRunId}/cases`).then(r => r.json()),
      prevRun ? fetch(`/api/eval/runs/${prevRun.id}`).then(r => r.json()).catch(() => ({}))
              : Promise.resolve({}),
    ]);
    const run = runResp.run || {};
    const scores = (runResp.scores || []).map(s => ({ ...s, score: to100(s.score) }));
    const prevMap = {};
    ((prevData || {}).scores || []).forEach(s => { prevMap[s.dimension] = to100(s.score); });
    const cases = (casesResp.items || []).slice()
      .sort((a, b) => (a.overall_score ?? 999) - (b.overall_score ?? 999));
    const regressed = scores.filter(s =>
      prevMap[s.dimension] != null && s.score < prevMap[s.dimension] - 0.05);

    const overall = run.overall_score;
    const prevOverall = prevRun ? prevRun.overall_score : null;
    let shiftHtml = '<div class="shift flat">首个批次</div>';
    if (overall != null && prevOverall != null) {
      const d = overall - prevOverall;
      shiftHtml = `<div class="shift ${d >= 0 ? 'up' : 'down'}">${d >= 0 ? '▲' : '▼'} ${Math.abs(d).toFixed(1)} vs 上批次 ${prevOverall.toFixed(1)}</div>`;
    }
    const runOpts = freshRuns.map(r =>
      `<option value="${esc(r.id)}" ${String(r.id) === regRunId ? 'selected' : ''}>#${esc(r.id)}${r.tag ? ' · ' + esc(r.tag) : ''} · ${(r.created_at || '').slice(0, 10)}</option>`).join('');

    const dimRows = scores.map(s => {
      const prev = prevMap[s.dimension];
      const d = prev == null ? null : s.score - prev;
      const lv = s.score >= 80 ? '' : s.score >= 60 ? 'warn' : 'bad';
      return `<div class="dim-row">
        <span class="name">${dimLabel(s.dimension)}</span>
        <div class="bar"><i class="${lv}" style="width:${Math.max(0, Math.min(100, s.score))}%"></i></div>
        <span class="score">${s.score.toFixed(1)}</span>
        <span class="delta ${d == null ? 'flat' : d >= 0 ? 'up' : 'down'}">${d == null ? '-' : (d >= 0 ? '+' : '') + d.toFixed(1)}</span>
      </div>`;
    }).join('');

    const caseRows = cases.map(c => `
      <tr class="clickable" data-id="${c.id}">
        <td class="mono">${c.id}</td>
        <td>${esc((c.question || '').slice(0, 44))}</td>
        <td>${esc(c.category || '-')}</td>
        <td class="mono ${pctClass((c.overall_score || 0) / 100)}">${c.overall_score == null ? '-' : Number(c.overall_score).toFixed(1)}</td>
        <td class="mono">${c.iteration_count ?? '-'}</td>
        <td class="mono">${(c.tokens_in || 0) + (c.tokens_out || 0) ? (((c.tokens_in || 0) + (c.tokens_out || 0)) / 1000).toFixed(1) + 'k' : '-'}</td>
      </tr>`).join('') || '<tr><td colspan="6" class="eval-empty-hint">无 case</td></tr>';

    el.innerHTML = `
      <div class="section">
        <div class="section-title">批次对比<span class="tag">同一题库两次运行，逐维度看退化 · 判定口径全规则</span>
          <div class="right">
            <select id="regRunSel" class="btn">${runOpts}</select>
            <button class="btn" id="regBmToggle">题库管理</button>
            <button class="btn primary" onclick="window.StockRadar?.evalManager?.openNewRunModal()">跑一批次</button>
          </div></div>
        <div class="section-body">
          <div class="stat-grid">
            <div class="stat-card"><div class="label">批次总分</div><div class="value">${fmtScore(overall)}</div>${shiftHtml}</div>
            <div class="stat-card"><div class="label">退化维度</div><div class="value"${regressed.length ? ' style="color:var(--danger,#ef4444)"' : ''}>${regressed.length}</div><div class="hint">${esc(regressed.length ? regressed.map(s => dimLabel(s.dimension)).join(' / ') : '无维度退化')}</div></div>
            <div class="stat-card"><div class="label">Case 数</div><div class="value">${cases.length}</div><div class="hint">${esc(run.mode || '-')}</div></div>
            <div class="stat-card"><div class="label">判定口径</div><div class="value" style="font-size:15px;padding-top:6px">全规则</div><div class="hint">分数可复现、不漂移</div></div>
          </div>
        </div>
      </div>
      <div class="section">
        <div class="section-title">维度雷达<span class="tag">标签与权重由注册表渲染，前端无硬编码</span></div>
        <div class="section-body">
          <div class="radar-wrap">
            <div>
              <div class="chart-canvas-wrap"><canvas id="evalRadarChart"></canvas></div>
              <div class="radar-legend"><span><i class="cur" style="display:inline-block;width:14px;height:3px;border-radius:2px;background:var(--brand,#176b5b);vertical-align:middle;margin-right:5px"></i>本批次</span></div>
            </div>
            <div class="dim-list">${dimRows || '<div class="eval-empty-hint">无维度分数</div>'}</div>
          </div>
        </div>
      </div>
      <div class="section">
        <div class="section-title">case 列表<span class="tag">按总分升序 · 点任意行看「为什么评这个分」</span></div>
        <div class="section-body section-bodyFlush"><table class="grid">
          <thead><tr><th>ID</th><th>问题</th><th>分类</th><th>总分</th><th>迭代</th><th>Token</th></tr></thead>
          <tbody>${caseRows}</tbody></table></div>
      </div>
      <div id="regBmBox" style="display:${regShowBenchmark ? 'block' : 'none'}"></div>
      <div id="regTrendBox"></div>`;

    if (scores.length) renderRadarChart(scores);
    el.querySelectorAll('tr.clickable').forEach(row => {
      row.addEventListener('click', () => window.StockRadar?.evalDetail?.open(Number(row.dataset.id)));
    });
    const sel = document.getElementById('regRunSel');
    if (sel) sel.addEventListener('change', () => { regRunId = sel.value; renderRegression(el); });
    const bmToggle = document.getElementById('regBmToggle');
    if (bmToggle) bmToggle.addEventListener('click', async () => {
      regShowBenchmark = !regShowBenchmark;
      const box = document.getElementById('regBmBox');
      box.style.display = regShowBenchmark ? 'block' : 'none';
      if (regShowBenchmark && !box.dataset.loaded) {
        box.dataset.loaded = '1';
        box.innerHTML = '<div class="eval-empty-hint" style="padding:16px">加载题库…</div>';
        await renderBenchmark(box);
      }
    });
    await renderTrend(document.getElementById('regTrendBox'));
  }

  /* ════════════════════════════════════════════════════════
   *  视图四：Agent 研判（原型 view-judge）
   * ════════════════════════════════════════════════════════ */
  async function renderJudge(el) {
    el.innerHTML = protoSection('Agent 增强研判',
      '对历史对话深度研判 · findings 必须带可核验证据 · 即现有 AI 评审，原样保留增强',
      `<div style="display:grid;grid-template-columns:250px 1fr;gap:20px">
         <div id="judgeDlgList" style="border-right:1px solid #f0f0f0;padding-right:14px;max-height:72vh;overflow:auto"></div>
         <div id="evalReviewDetail"><div class="eval-review-empty">从左侧选择一个对话</div></div>
       </div>`);
    await loadReviewDialogs();
    if (reviewDialogId) await refreshDialogReview();
  }

  /* ════════════════════════════════════════════════════════
   *  视图五：维度注册表（原型 view-registry）
   * ════════════════════════════════════════════════════════ */
  async function renderRegistry(el) {
    let dims = [];
    try { dims = (await (await fetch('/api/eval/dimensions')).json()).dimensions || []; }
    catch (e) { /* 空表 */ }
    const regCnt = dims.filter(d => (d.profiles || []).includes('regression')).length;
    const diagCnt = dims.filter(d => (d.profiles || []).includes('diagnostic')).length;
    const rows = dims.map(d => `
      <tr>
        <td class="mono">${esc(d.key)}</td>
        <td>${esc(d.label)}</td>
        <td class="mono">${esc(d.scope)}</td>
        <td>${(d.profiles || []).map(p => `<span class="chip ${p === 'regression' ? 'reg' : 'diag'}">${p === 'regression' ? '回归' : '诊断'}</span>`).join(' ')}</td>
        <td><span class="chip ${d.judge === 'rule' ? 'reg' : d.judge === 'llm' ? 'diag' : 'existing'}">${esc(d.judge)}</span></td>
        <td class="mono">${Object.entries(d.weights || {}).map(([p, w]) => `${p === 'regression' ? '回' : '诊'}${w}`).join(' / ') || '-'}</td>
        <td>${(d.evidence || []).map(ev => `<span class="chip reuse">${esc(ev)}</span>`).join(' ')}</td>
        <td>${esc((d.failure_modes || []).join('；'))}</td>
      </tr>`).join('');
    el.innerHTML = `
      <div class="section">
        <div class="section-title">维度注册表<span class="tag">单一事实源 · 新增维度 = 加一条注册项，不改引擎</span>
          <div class="right"><span class="chip reg">回归 ${regCnt}</span><span class="chip diag">诊断 ${diagCnt}</span></div></div>
        <div class="section-body section-bodyFlush"><table class="grid">
          <thead><tr><th>key</th><th>维度</th><th>scope</th><th>profile</th><th>判定</th><th>权重</th><th>证据来源</th><th>失败模式</th></tr></thead>
          <tbody>${rows}</tbody></table></div>
      </div>
      <div class="section">
        <div class="section-title">维度契约<span class="tag">缺任何一条的维度不许进系统</span></div>
        <div class="section-body">
          <div class="banner">① <b>证据来源</b>：靠什么判？&nbsp;&nbsp;② <b>失败模式</b>：什么样算不合格，必须具体到形态&nbsp;&nbsp;③ <b>改进动作</b>：扣分了必须给出改哪个 prompt / 技能 / 代码</div>
          <div class="banner warn" style="margin-top:8px">两条硬机制：<b>证据引用强制 + 程序化校验</b>（quote 必须能在证据里原文命中，否则标 <code>unverified</code>）；<b>规则先行，LLM 兜底</b>（能程序化判的绝不交给 LLM）。<br>另：<b>规则维度必须给出 formula 或 threshold</b>，否则详情页回答不了「为什么评这个分」。</div>
        </div>
      </div>`;
  }

  /* ════════════════════════════════════════════════════════
   *  视图三：存量回测（原型 view-existing，入口 B §4）
   * ════════════════════════════════════════════════════════ */
  let _btEs = null;
  let btRunId = null;

  async function renderExisting(el) {
    await fetchRuns();
    await ensureDimLabels();
    const existing = allRuns.filter(r => r.mode === 'existing');
    if (!btRunId || !existing.some(r => String(r.id) === btRunId)) {
      btRunId = existing.length ? String(existing[0].id) : null;
    }
    const runOpts = existing.map(r =>
      `<option value="${esc(r.id)}" ${String(r.id) === btRunId ? 'selected' : ''}>#${esc(r.id)} · ${(r.created_at || '').slice(0, 10)} · ${r.overall_score != null ? Number(r.overall_score).toFixed(1) + '分' : esc(r.status || '')}</option>`).join('');
    const reportHtml = btRunId
      ? '<div class="eval-empty-hint" style="padding:20px">加载回测报告…</div>'
      : '';
    el.innerHTML = `
      <div class="section">
        <div class="section-title">存量回测<span class="tag">历史 run / 对话直接评测，不重跑 agent，只付 judge 成本</span>
          <div class="right"><span class="chip existing">source = existing</span>
            ${existing.length ? `<select id="btRunSel" class="btn">${runOpts}</select>` : ''}
            <button class="btn primary" onclick="window.StockRadar?.evalManager?.triggerBacktest()">开始回测</button></div></div>
        <div class="section-body">
          <div class="banner warn">换维度体系后，可对<b>同一批历史数据重新回测</b>——不必等新数据、不必重跑 agent，直接看质量分布怎么变。</div>
          <div class="filter-row" style="margin-top:12px">
            <input id="btAgent" placeholder="agent_name（可空）" size="20">
            <input id="btSince" placeholder="起始 2026-09-01（可空）" size="18">
            <input id="btUntil" placeholder="截止（可空）" size="18">
            <input id="btLimit" type="number" value="50" min="1" max="500" style="width:90px" title="最多回测条数">
          </div>
          <div class="reuse-row" id="btReuseRow">统计可评目标…</div>
          <div class="progress" id="btProgress"><div class="prog-track"><div class="prog-fill" id="btFill" style="width:0"></div></div><span class="prog-txt" id="btTxt">0%</span></div>
        </div>
      </div>
      ${btRunId ? `<div id="btReportWrap">${reportHtml}</div>` : ''}`;
    loadBacktestCount();
    const sel = document.getElementById('btRunSel');
    if (sel) sel.addEventListener('change', () => {
      btRunId = sel.value;
      renderExisting(el);
    });
    if (btRunId) await renderBacktestReport(document.getElementById('btReportWrap'), btRunId);
  }

  async function loadBacktestCount() {
    const box = document.getElementById('btReuseRow');
    if (!box) return;
    try {
      const agent = document.getElementById('btAgent')?.value?.trim() || '';
      const since = document.getElementById('btSince')?.value?.trim() || '';
      const until = document.getElementById('btUntil')?.value?.trim() || '';
      const limit = Number(document.getElementById('btLimit')?.value || 50);
      const q = `agent_name=${encodeURIComponent(agent)}&since=${encodeURIComponent(since)}`
        + `&until=${encodeURIComponent(until)}&limit=${limit}`;
      const c = await (await fetch(`/api/eval/backtest/count?${q}`)).json();
      const pending = Math.max(0, c.run_count - c.reviewed_run_count);
      box.innerHTML = `<span>命中待评目标 <b>${c.run_count}</b> 条（历史 run）</span>`
        + `<span>其中 <b>${c.reviewed_run_count}</b> 条已有同版本评审（将复用）</span>`
        + `<span>预计去重后判分 <b>${pending}</b> 次 · 零 agent 成本</span>`;
    } catch (e) { box.innerHTML = '<span>目标计数暂不可用</span>'; }
  }

  async function triggerBacktest() {
    const body = {
      agent_name: document.getElementById('btAgent')?.value?.trim() || '',
      since: document.getElementById('btSince')?.value?.trim() || '',
      until: document.getElementById('btUntil')?.value?.trim() || '',
      limit: Number(document.getElementById('btLimit')?.value || 50),
    };
    const wrap = document.getElementById('btProgress');
    const fill = document.getElementById('btFill');
    const txt = document.getElementById('btTxt');
    if (wrap) wrap.classList.add('show');
    if (txt) txt.textContent = '入队中…';
    try {
      const resp = await fetch('/api/eval/backtest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { if (txt) txt.textContent = data.detail || '触发失败'; return; }
      trackBacktestProgress(data.run_id, fill, txt);
    } catch (e) { if (txt) txt.textContent = e.message; }
  }

  function trackBacktestProgress(runId, fill, txt) {
    if (_btEs) { _btEs.close(); _btEs = null; }
    const es = new EventSource(`/api/eval/backtest/${runId}/progress`);
    _btEs = es;
    es.onmessage = (ev) => {
      let msg = {}; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.error === 'not_found') {
        es.close(); _btEs = null;
        if (txt) txt.textContent = '批次不存在';
        return;
      }
      const pct = Math.round((msg.pct || 0) * 100);
      if (fill) fill.style.width = pct + '%';
      if (txt) txt.textContent = `${msg.current ?? 0}/${msg.total ?? '?'}（${pct}%）`;
      if (['completed', 'failed', 'timeout'].includes(msg.status)) {
        es.close(); _btEs = null;
        if (txt) txt.textContent = `${msg.status} ${msg.current ?? 0}/${msg.total ?? '?'}`;
        fetchRuns().then(() => { btRunId = runId; renderMain(); });
      }
    };
    es.onerror = () => { es.close(); _btEs = null; };
  }

  async function renderBacktestReport(el, runId) {
    if (!el) return;
    let rep, casesData;
    try {
      const [repResp, casesResp] = await Promise.all([
        fetch(`/api/eval/backtest/${runId}/report`),
        fetch(`/api/eval/runs/${runId}/cases`),
      ]);
      if (!repResp.ok) {
        const d = await repResp.json().catch(() => ({}));
        el.innerHTML = `<div class="eval-review-error">${esc(d.detail || '报告加载失败')}</div>`;
        return;
      }
      rep = await repResp.json();
      casesData = await casesResp.json();
    } catch (e) { el.innerHTML = `<div class="eval-review-error">${esc(e.message)}</div>`; return; }

    const ov = rep.overall || {};
    const cases = (casesData.items || []).filter(c => c.overall_score != null);

    // 分数分布直方（5 桶，与详情页口径一致）
    const bins = [
      { label: '<60 待改进', n: 0 }, { label: '60-70 及格', n: 0 },
      { label: '70-80 良好-', n: 0 }, { label: '80-90 良好+', n: 0 },
      { label: '90+ 优秀', n: 0 },
    ];
    cases.forEach(c => {
      const s = Number(c.overall_score);
      if (s < 60) bins[0].n++; else if (s < 70) bins[1].n++;
      else if (s < 80) bins[2].n++; else if (s < 90) bins[3].n++; else bins[4].n++;
    });
    const maxBin = Math.max(1, ...bins.map(b => b.n));
    const histHtml = bins.map(b =>
      `<div class="hist-bar" style="height:${Math.round(b.n / maxBin * 100)}%">`
      + `<span class="tip">${b.label}：${b.n}</span></div>`).join('');

    // 时间趋势 spark（按被评 run 执行日期）
    const trend = rep.trend || [];
    const maxTrend = Math.max(1, ...trend.map(t => t.mean_overall || 0));
    const sparkHtml = trend.map(t =>
      `<i style="height:${Math.max(6, Math.round((t.mean_overall || 0) / maxTrend * 100))}%"`
      + ` title="${esc(t.date)}：${fmtScore(t.mean_overall)}（${t.case_count} 条）"></i>`).join('');

    // 逐维度均值表
    const dimRows = Object.entries(rep.dimensions || {}).map(([k, s]) => `
      <tr><td>${dimLabel(k)}</td><td class="mono">${fmtScore(s.mean)}</td>
      <td class="mono">${fmtScore(s.min)}</td><td class="mono">${fmtScore(s.max)}</td>
      <td class="mono">${s.count ?? '-'}</td></tr>`).join('')
      || '<tr><td colspan="5" class="eval-empty-hint">无维度分数</td></tr>';

    // 与上次回测差分
    const delta = rep.delta_vs_previous;
    let deltaHtml;
    if (!delta) deltaHtml = '<div class="eval-empty-hint">没有上一次同筛选条件的回测</div>';
    else {
      const o = delta.overall || {};
      const rowsHtml = Object.entries(delta.dimensions || {}).map(([k, d]) => `
        <tr><td>${dimLabel(k)}</td>
        <td class="mono">${fmtScore(d.previous)} → ${fmtScore(d.current)}</td>
        <td class="mono ${d.diff >= 0 ? 'up' : 'down'}" style="color:${d.diff >= 0 ? 'var(--success,#10b981)' : 'var(--danger,#ef4444)'}">${d.diff >= 0 ? '+' : ''}${fmtScore(d.diff)}</td></tr>`).join('');
      deltaHtml = `<div class="meta-row"><span>规则分均值 <b>${fmtScore(o.previous)} → ${fmtScore(o.current)}</b></span></div>
        <table class="grid" style="margin-top:10px"><thead><tr><th>维度</th><th>上次 → 本次</th><th>差分</th></tr></thead><tbody>${rowsHtml}</tbody></table>`;
    }

    // 被评对象列表（点行进详情）
    const listRows = (casesData.items || []).map(c => `
      <tr class="clickable" data-id="${c.id}">
        <td class="mono">${esc(c.trace_run_id || '-')}</td>
        <td>${esc((c.question || '').slice(0, 40))}</td>
        <td class="mono ${pctClass((c.overall_score || 0) / 100)}">${fmtScore(c.overall_score)}</td>
        <td><span class="chip ${c.status === 'scored' ? 'ok' : c.status === 'failed' ? 'bad' : 'run'}">${esc(c.status || '-')}</span></td>
      </tr>`).join('') || '<tr><td colspan="4" class="eval-empty-hint">无</td></tr>';

    // 改进清单（findings × 归因 聚合）
    let insightRows = '';
    try {
      const items = (await (await fetch('/api/eval/insights?limit=15')).json()).items || [];
      insightRows = items.map((i, n) => `
        <tr>
          <td class="mono">${n + 1}</td>
          <td><b>${dimLabel(i.dimension)}</b>${(i.sample_verdicts || [])[0] ? `<div style="font-size:11.5px;color:var(--text-secondary,#6b7280)">${esc(i.sample_verdicts[0])}</div>` : ''}</td>
          <td><span class="chip bad">${esc(i.attribution)}</span></td>
          <td class="mono">${i.count}</td>
          <td class="mono">${(i.targets || []).length}</td>
        </tr>`).join('')
        || '<tr><td colspan="5" class="eval-empty-hint">暂无 findings —— 先做几次 AI 评审</td></tr>';
    } catch (e) { insightRows = '<tr><td colspan="5" class="eval-empty-hint">改进清单加载失败</td></tr>'; }

    const o = delta && delta.overall ? delta.overall : null;
    el.innerHTML = `
      <div class="section">
        <div class="section-title">回测报告<span class="tag">分布 · 逐维度均值 · 最差排行 · 与上次差分 · 时间趋势</span></div>
        <div class="section-body">
          <div class="stat-grid">
            <div class="stat-card"><div class="label">评测对象</div><div class="value">${rep.completed_cases ?? rep.total_cases ?? '-'}</div><div class="hint">历史 run，不重跑</div></div>
            <div class="stat-card"><div class="label">规则分均值</div><div class="value">${fmtScore(ov.mean)}</div>${o && o.previous != null ? `<div class="shift ${o.diff >= 0 ? 'up' : 'down'}">${o.diff >= 0 ? '▲' : '▼'} ${Math.abs(o.diff).toFixed(1)} vs 上次 ${fmtScore(o.previous)}</div>` : '<div class="hint">首次回测</div>'}</div>
            <div class="stat-card"><div class="label">研判分均值</div><div class="value">${fmtScore(rep.diagnostic_mean)}</div><div class="hint">主观维度整体偏低</div></div>
            <div class="stat-card"><div class="label">评审覆盖率</div><div class="value">${rep.reviewed_rate != null ? Math.round(rep.reviewed_rate * 100) + '%' : '-'}</div><div class="hint">已有同版本 done 评审</div></div>
          </div>
          <div class="blk-label" style="margin-top:18px">分数分布</div>
          <div class="hist-chart">${histHtml}</div>
          <div class="blk-label" style="margin-top:20px">时间趋势（按 run.created_at 分桶）</div>
          <div style="display:flex;align-items:flex-end;gap:16px;margin-top:8px"><div class="spark" style="flex:1">${sparkHtml || '<span class="eval-empty-hint">无 trace 时间信息</span>'}</div></div>
        </div>
      </div>
      <div class="section">
        <div class="section-title">逐维度均值<span class="tag">单一来源 eval_scores 聚合</span></div>
        <div class="section-body section-bodyFlush"><table class="grid">
          <thead><tr><th>维度</th><th>均值</th><th>最低</th><th>最高</th><th>N</th></tr></thead>
          <tbody>${dimRows}</tbody></table></div>
      </div>
      <div class="section">
        <div class="section-title">与上一次回测差分<span class="tag">${delta ? '对比 #' + esc(delta.previous_run_id) : '无对比批次'}</span></div>
        <div class="section-body">${deltaHtml}</div>
      </div>
      <div class="section">
        <div class="section-title">被评对象列表<span class="tag">点任意行进入详情</span></div>
        <div class="section-body section-bodyFlush"><table class="grid">
          <thead><tr><th>trace run</th><th>问题</th><th>规则分</th><th>状态</th></tr></thead>
          <tbody>${listRows}</tbody></table></div>
      </div>
      <div class="section">
        <div class="section-title">改进清单<span class="tag">findings 按 (问题 · 归因) 聚合，指导「先改什么」</span></div>
        <div class="section-body section-bodyFlush"><table class="grid">
          <thead><tr><th>#</th><th>问题</th><th>归因</th><th>出现次数</th><th>涉及目标</th></tr></thead>
          <tbody>${insightRows}</tbody></table></div>
      </div>`;

    el.querySelectorAll('tr.clickable').forEach(row => {
      row.addEventListener('click', () => window.StockRadar?.evalDetail?.open(Number(row.dataset.id)));
    });
  }

  /* ════════════════════════════════════════════════════════
   *  暴露 API
   * ════════════════════════════════════════════════════════ */
  const api = {
    openPage, closePage, loadRuns, selectRun, switchTab,
    selectCase, createCase, editCase, deleteCase, filterBenchmark,
    selectReviewDialog, startDialogReview, switchReviewVersion,
    triggerBacktest, openNewRunModal,
  };
  window.StockRadar = window.StockRadar || {};
  window.StockRadar.evalManager = api;

  // 把 closeModal 也暴露给 onclick
  window._evalCloseModal = closeModal;
})();
