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
  let curTab     = 'overview'; // overview | cases | benchmark
  let searchQ    = '';

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
    loadRuns();
  }
  function closePage() {
    $page().classList.remove('open');
    if ($chat()) $chat().style.display = '';
    selectedId = null;
    selectedCaseId = null;
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
  async function loadRuns() {
    try {
      const resp = await fetch('/api/eval/runs?limit=100');
      const data = await resp.json();
      allRuns = data.items || [];
      renderList();
      if (allRuns.length && !selectedId) {
        selectRun(String(allRuns[0].id));
      }
      if (!allRuns.length) renderEmpty();
    } catch (e) { console.error('[eval] 加载批次失败:', e); }
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

  function switchTab(tab) {
    curTab = tab;
    $tabs().querySelectorAll('.eval-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === tab));
    renderMain();
  }

  async function renderMain() {
    const el = $main();
    if (!selectedId) {
      el.innerHTML = '<div class="eval-empty-hint">选择左侧批次查看详情</div>';
      return;
    }
    if (curTab === 'overview')   await renderOverview(el);
    else if (curTab === 'cases')    await renderCases(el);
    else if (curTab === 'benchmark') await renderBenchmark(el);
  }

  /* ── Overview ─────────────────────────────────────────────── */
  async function renderOverview(el) {
    const [runResp, casesResp] = await Promise.all([
      fetch(`/api/eval/runs/${selectedId}`),
      fetch(`/api/eval/runs/${selectedId}/cases`),
    ]);
    const runData  = await runResp.json();
    const casesData = await casesResp.json();
    const run    = runData.run || {};
    const scores = runData.scores || [];
    const cases  = casesData.items || [];

    const scoreHtml = scores.map(s => {
      const v = s.score == null ? 0 : s.score;
      return `<div class="eval-score-badge ${pctClass(v)}">
                <div class="eval-score-val">${(v*100).toFixed(0)}</div>
                <div class="eval-score-label">${dimLabel(s.dimension)}</div>
              </div>`;
    }).join('');

    const failCases = cases.filter(c => c.status === 'fail' || (c.overall_score||0) < 60);
    const failHtml  = failCases.length
      ? failCases.map(c => `<div class="eval-fail-item" data-id="${c.id}">
                            <span class="eval-fail-name">${esc(c.question||'').slice(0,30)}</span>
                            <span class="eval-fail-score">${((c.overall_score||0)).toFixed(0)}</span>
                          </div>`).join('')
      : '<div class="eval-empty-hint" style="padding:12px;">全部通过 ✓</div>';

    el.innerHTML = `
      <div class="eval-overview">
        <div class="eval-overview-header">
          <div class="eval-overview-title">评测批次 #${run.id}</div>
          <div class="eval-overview-meta">
            <span>模式：${esc(run.mode||'-')}</span>
            <span>Tag：${esc(run.tag||'-')}</span>
            <span>创建：${(run.created_at||'').slice(0,19).replace('T',' ')}</span>
            <span>状态：<span class="eval-status-${run.status||''}">${run.status||'-'}</span></span>
          </div>
        </div>
        <div class="eval-score-row">${scoreHtml}</div>
        <div class="eval-chart-row">
          <div class="eval-chart-box">
            <h3>五维雷达图</h3>
            <div class="chart-canvas-wrap"><canvas id="evalRadarChart"></canvas></div>
          </div>
          <div class="eval-chart-box">
            <h3>分数分布</h3>
            <div class="chart-canvas-wrap"><canvas id="evalDistChart"></canvas></div>
          </div>
        </div>
        <div class="eval-section">
          <div class="eval-section-title">失败 / 低分 Case（${failCases.length}）</div>
          <div id="evalFailList">${failHtml}</div>
        </div>
        <div class="eval-section">
          <div class="eval-section-title">效率指标</div>
          <div class="eval-metric-grid">
            <div class="eval-metric-item">
              <div class="eval-metric-val">${run.avg_steps?.toFixed(1)??'-'}</div>
              <div class="eval-metric-label">平均步数</div>
            </div>
            <div class="eval-metric-item">
              <div class="eval-metric-val">${run.avg_tokens?(run.avg_tokens/1000).toFixed(1)+'k':'-'}</div>
              <div class="eval-metric-label">平均 Token</div>
            </div>
            <div class="eval-metric-item">
              <div class="eval-metric-val">${run.avg_duration_ms?(run.avg_duration_ms/1000).toFixed(1)+'s':'-'}</div>
              <div class="eval-metric-label">平均耗时</div>
            </div>
            <div class="eval-metric-item">
              <div class="eval-metric-val">${run.total_cases??'-'}</div>
              <div class="eval-metric-label">总 Case 数</div>
            </div>
          </div>
        </div>
      </div>`;

    // 绘制雷达图
    renderRadarChart(scores);
    // 绘制分数分布图
    renderDistChart(cases);

    // 失败 case 可点击
    el.querySelectorAll('.eval-fail-item').forEach(item => {
      item.addEventListener('click', () => selectCase(Number(item.dataset.id)));
    });
  }

  /* ── Cases 列表 ──────────────────────────────────────────── */
  async function renderCases(el) {
    const resp = await fetch(`/api/eval/runs/${selectedId}/cases`);
    const data  = await resp.json();
    const cases = data.items || [];
    el.innerHTML = `
      <div class="eval-cases">
        <div class="eval-cases-header">
          <div class="eval-cases-title">Case 列表（${cases.length}）</div>
        </div>
        <table class="eval-cases-table">
          <thead><tr><th>ID</th><th>问题</th><th>分类</th><th>分数</th><th>状态</th><th>迭代</th><th>Token</th></tr></thead>
          <tbody>
            ${cases.map(c => `<tr class="eval-case-row ${c.status==='fail'?'fail':''}" data-id="${c.id}">
              <td>${c.id}</td>
              <td class="eval-case-q">${esc(c.question||'').slice(0,40)}</td>
              <td>${esc(c.category||'-')}</td>
              <td class="${pctClass((c.overall_score||0)/100)}">${((c.overall_score||0)).toFixed(0)}</td>
              <td><span class="eval-status-${c.status}">${c.status||'-'}</span></td>
              <td>${c.iteration_count??'-'}</td>
              <td>${c.tokens_in&&c.tokens_out?((c.tokens_in+c.tokens_out)/1000).toFixed(1)+'k':'-'}</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>`;

    el.querySelectorAll('.eval-case-row').forEach(row => {
      row.addEventListener('click', () => selectCase(Number(row.dataset.id)));
    });
  }

  /* ── Benchmark 题库 ──────────────────────────────────────── */
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
              <td>${c.id}</td>
              <td class="eval-case-q">${esc(c.question||'').slice(0,40)}</td>
              <td>${esc(c.category||'-')}</td>
              <td>${esc(c.difficulty||'-')}</td>
              <td>${(c.expected_tools||[]).join(', ')}</td>
              <td>
                <button class="eval-btn sm" onclick="event.stopPropagation();window.StockRadar?.evalManager?.editCase(${c.id})">编辑</button>
                <button class="eval-btn sm danger" onclick="event.stopPropagation();window.StockRadar?.evalManager?.deleteCase(${c.id})">删除</button>
              </td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
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
      const data = parseCase(await resp.json());
      renderCaseDetail(right, data);
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
        <span class="eval-detail-score-val ${pctClass(s.score)}">${(s.score*100).toFixed(0)}</span>
        <span class="eval-detail-score-wt">权重 {s.weight}</span>
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
          <div class="eval-detail-value" style="color:var(--danger,#e74c3c)">${esc(c.error_msg||'无')}</div>
        </div>
        <div class="eval-detail-section">
          <div class="eval-detail-label">Agent 回答</div>
          <div class="eval-detail-value" style="white-space:pre-wrap;max-height:300px;overflow:auto;">${esc(c.answer||'无')}</div>
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
        <label>期望数据源（逗号分隔）</label>
        <input id="evalCaseDS" type="text" value="${(d.expected_datasources||[]).join(', ')}" placeholder="akshare, news" />
      </div>
      <div class="eval-form-group">
        <label>关键词（逗号分隔）</label>
        <input id="evalCaseKw" type="text" value="${(d.keywords||[]).join(', ')}" placeholder="关键词1, 关键词2" />
      </div>
      <div class="eval-form-group">
        <label>参考答案（可选）</label>
        <textarea id="evalCaseRef" rows="3" placeholder="用于事实准确性评分">${esc(d.reference_answer||'')}</textarea>
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
      expected_datasources: document.getElementById('evalCaseDS').value.split(',').map(s=>s.trim()).filter(Boolean),
      keywords:            document.getElementById('evalCaseKw').value.split(',').map(s=>s.trim()).filter(Boolean),
      reference_answer:    document.getElementById('evalCaseRef').value.trim() || null,
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
  function dimLabel(d) {
    return {tool_correctness:'工具调用',output_completeness:'输出完整',factual_accuracy:'事实准确',reasoning_quality:'推理质量',efficiency:'效率'}[d] || d;
  }
  function pctClass(s) {
    if (s == null) return '';
    if (s >= 0.8 || s >= 80) return 'high';
    if (s >= 0.6 || s >= 60) return 'mid';
    return 'low';
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

    const dimLabels = {
      tool_correctness: "工具调用",
      output_completeness: "输出完整",
      factual_accuracy: "事实准确",
      reasoning_quality: "推理质量",
      efficiency: "效率",
    };
    const labels = scores.map(s => dimLabels[s.dimension] || s.dimension);
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


  /* ════════════════════════════════════════════════════════
   *  暴露 API
   * ════════════════════════════════════════════════════════ */
  const api = {
    openPage, closePage, loadRuns, selectRun, switchTab,
    selectCase, createCase, editCase, deleteCase, filterBenchmark,
  };
  window.StockRadar = window.StockRadar || {};
  window.StockRadar.evalManager = api;

  // 把 closeModal 也暴露给 onclick
  window._evalCloseModal = closeModal;
})();
