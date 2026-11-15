/**
 * 回测面板 — 对齐原型 docs/prototypes/backtest.html
 * 依赖: window.StockRadar.api
 */
(function () {
  'use strict';

  class BacktestPanel {
    constructor() {
      this.overlay = document.getElementById('backtestOverlay');
      this.modal = document.querySelector('.backtest-modal');
      this._maximized = false;
      this._dragState = null;
      this.strategies = [];
      this.selectedStrategy = null;
      this._multiMode = false;
      this._selectedStrategies = new Set();
      this._multiSortKey = 'return';
      this._multiSortAsc = false;
      // 回测历史分页状态（三个表独立分页）
      this._historyPage = 1;
      this._batchPage = 1;
      this._multiPage = 1;
      this._historyPageSize = 20;
      this._init();
    }

    async _init() {
      await this._loadStrategies();
      this._bindEvents();
      this._bindEditorEvents();
      this._bindGapEvents();
      this._renderChips();
      this.refreshTestSets();
      this._bindDragAndResize();
      // 全屏切换时自动 resize echarts 实例
      document.addEventListener('fullscreenchange', () => {
        const box = document.querySelector('.bt-chart-box.bt-fullscreen');
        if (!box) {
          // 退出全屏：resize 所有可见 chart
          document.querySelectorAll('.bt-chart-box').forEach(b => b.classList.remove('bt-fullscreen'));
          ['btEquityChart', 'btKlineChart', 'btDrawdownChart', 'btBatchEquityChart'].forEach(id => {
            const el = document.getElementById(id);
            if (el) {
              const instance = echarts.getInstanceByDom(el);
              if (instance) instance.resize();
            }
          });
        }
      });
    }

    async _loadStrategies() {
      try {
        const res = await window.StockRadar.api.getBacktestStrategies();
        this.strategies = res.strategies || [];
        this._renderStrategyList();
      } catch (e) { console.error('加载策略失败:', e); }
    }

    _renderStrategyList() {
      const container = document.getElementById('btStrategyList');
      if (!container) return;
      container.innerHTML = this.strategies.map(s => {
        const tags = (s.tags || []).map(t => {
          let cls = 'tag-trend';
          if (['反转', 'RSI', 'KDJ'].some(k => t.includes(k))) cls = 'tag-reversal';
          if (['动量', '布林带', '突破'].some(k => t.includes(k))) cls = 'tag-momentum';
          return `<span class="bt-tag ${cls}">${this._esc(t)}</span>`;
        }).join('');
        const builtinBadge = s.is_builtin ? '<span class="builtin-badge">内置</span>' : '';
        const safeId = this._esc(s.strategy_id);
        const actions = `
          <div class="card-actions" onclick="event.stopPropagation()">
            <button class="bt-card-action-btn" onclick="window._btPanel.openLifecycle('${safeId}')">详情</button>
            ${s.is_builtin ? '' : `
            <button class="bt-card-action-btn" onclick="window._btPanel.editStrategy('${safeId}')">编辑</button>
            <button class="bt-card-action-btn danger" onclick="window._btPanel.deleteStrategy('${safeId}')">删除</button>`}
          </div>`;
        return `<div class="bt-strategy-card" data-id="${safeId}" onclick="window._btPanel.selectStrategy('${safeId}')">
          <div class="name">${this._esc(s.name)}${builtinBadge}</div>
          <div class="desc">${this._esc(s.description || '')}</div>
          ${tags ? `<div class="tags">${tags}</div>` : ''}
          ${actions}
        </div>`;
      }).join('');
      if (this.strategies.length > 0) this.selectStrategy(this.strategies[0].strategy_id);
    }

    async openLifecycle(strategyId) {
      const panel = document.getElementById('btLifecyclePanel');
      if (!panel) return;
      panel.style.display = 'block';
      panel.innerHTML = '<div class="bt-lc-head"><div class="bt-lc-title">策略详情</div>'
        + '<button class="bt-card-action-btn" onclick="window._btPanel.closeLifecycle()">关闭</button></div>'
        + '<div class="bt-lc-body bt-lc-empty">加载中…</div>';
      try {
        const res = await window.StockRadar.api.getStrategyLifecycle(strategyId);
        this._renderLifecycle(res);
      } catch (e) {
        const body = panel.querySelector('.bt-lc-body');
        if (body) body.innerHTML = '<div class="bt-lc-empty">加载失败：' + this._esc(e.message || e) + '</div>';
      }
    }

    closeLifecycle() {
      const panel = document.getElementById('btLifecyclePanel');
      if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
    }

    /* ── 策略详情的三个出口：评测 / 盯盘 / 研发面板（原来这块是死胡同） ── */
    gotoSignalEval() {
      this.closeLifecycle();
      const btn = document.getElementById('btRightTabSignalEval');
      if (btn) btn.click();
    }

    gotoMonitor() {
      this.closeLifecycle();
      const m = window.StockRadar && window.StockRadar.monitorPanel;
      if (m && m.toggle) m.toggle();
    }

    /* 回测历史里的「研发过程」：打开策略研究面板并载入那一次 run */
    openResearchRun(runUuid) {
      const p = window.StockRadar && window.StockRadar.strategyResearchPanel;
      if (!p || !p.open) return;
      p.open();
      if (p.loadRun && runUuid) p.loadRun(runUuid, { subscribe: false });
    }

    gotoStrategyResearch() {
      this.closeLifecycle();
      const p = window.StockRadar && window.StockRadar.strategyResearchPanel;
      if (p && p.open) p.open();
    }

    _lcPct(v, digits = 1) {
      if (v === null || v === undefined || v === '') return '—';
      const n = Number(v);
      return Number.isFinite(n) ? (n * 100).toFixed(digits) + '%' : '—';
    }

    _lcNum(v, digits = 2) {
      if (v === null || v === undefined || v === '') return '—';
      const n = Number(v);
      return Number.isFinite(n) ? n.toFixed(digits) : '—';
    }

    _renderLifecycle(d) {
      const panel = document.getElementById('btLifecyclePanel');
      if (!panel) return;
      const s = d.strategy || {};
      const runs = d.runs || [];
      const ev = d.evals || {};
      const fw = d.forward || {};
      const mon = d.monitor || {};
      const iters = d.iterations || [];
      const ORIGIN = { manual: '手工', batch: '批量', multi: '多策略', research: '研发', eval: '评判' };

      // ① 回测记录
      const runRows = runs.length ? runs.map(r => {
        const meta = (this._HEALTH_META || {})[r.health] || null;
        const healthCell = meta ? '<span class="bt-health-badge">' + meta.text + '</span>'
                                : '<span class="bt-lc-muted">—</span>';
        return '<tr>'
          + '<td>' + this._esc(r.created_at || '') + '</td>'
          + '<td>' + this._esc(r.code) + ' ' + this._esc(r.stock_name || '') + '</td>'
          + '<td><span class="bt-lc-origin">' + this._esc(ORIGIN[r.origin] || r.origin || '—') + '</span></td>'
          + '<td>' + healthCell + '</td>'
          + '<td>' + this._lcPct(r.total_return) + '</td>'
          + '<td>' + this._lcNum(r.sharpe_ratio) + '</td>'
          + '<td>' + this._lcPct(r.win_rate) + '</td>'
          + '<td>' + (r.trade_count === null || r.trade_count === undefined ? '—' : r.trade_count) + '</td>'
          + '<td>' + (r.status === 'completed'
              ? '<button class="bt-card-action-btn" onclick="window._btPanel.closeLifecycle();window._btPanel.viewRun(\'' + this._esc(r.run_id) + '\')">查看</button>'
              : '<span class="bt-lc-muted">' + this._esc(r.status || '—') + '</span>') + '</td>'
          + '</tr>';
      }).join('') : '<tr><td colspan="9" class="bt-lc-empty">还没有回测记录</td></tr>';

      // ② 评判（样本内外对照）
      const evCounts = Object.keys(ev.counts || {}).map(k => k + ' ' + ev.counts[k]).join(' / ') || '—';
      const evRows = (ev.rows || []).slice(0, 5).map(r => '<tr>'
        + '<td>' + this._esc(r.run_id) + '</td>'
        + '<td>' + this._esc(r.verdict || '—') + (r.overfit_flag ? ' ⚠ 过拟合嫌疑' : '') + '</td>'
        + '<td>' + this._lcNum(r.is_sharpe) + ' / ' + this._lcNum(r.oos_sharpe) + '</td>'
        + '<td>' + this._lcNum(r.sharpe_ratio_io) + '</td>'
        + '<td>' + this._esc(r.split_date || '—') + '</td>'
        + '</tr>').join('');

      // ③ 前瞻跟踪（诚实口径：样本不够就直说）
      const mature = fw.mature_count || 0;
      const fwLine = (fw.win_rate === null || fw.win_rate === undefined)
        ? '前瞻样本不足（成熟 ' + mature + ' 条），无法判断策略前瞻有效性'
        : '前瞻胜率 ' + this._lcPct(fw.win_rate) + '（成熟 ' + mature + '/' + (fw.signal_count || 0) + ' 条）';
      const wilson = (fw.wilson_ci && fw.wilson_ci.length === 2)
        ? '95% Wilson 区间 ' + this._lcPct(fw.wilson_ci[0]) + ' ~ ' + this._lcPct(fw.wilson_ci[1]) : '';

      // ④ 落地
      const monRows = (mon.rules || []).length ? mon.rules.map(r => '<tr>'
        + '<td>' + this._esc(r.stock_code) + ' ' + this._esc(r.stock_name || '') + '</td>'
        + '<td>' + this._esc(r.rule_type) + '</td>'
        + '<td>' + (r.threshold === null || r.threshold === undefined ? '—' : r.threshold) + '</td>'
        + '<td>' + (r.enabled ? '启用' : '停用') + '</td>'
        + '<td>' + this._esc(r.remark || '') + '</td>'
        + '</tr>').join('') : '<tr><td colspan="5" class="bt-lc-empty">没有盯盘规则</td></tr>';

      // ⑤ 迭代史（strategy_hash 反查，只有研发出来的策略才有）
      const iterRows = iters.length ? iters.map(it => '<tr>'
        + '<td>' + (it.round === null || it.round === undefined ? '—' : it.round) + '</td>'
        + '<td>' + this._esc(it.verdict || '—') + (it.overfit_flag ? ' ⚠' : '') + '</td>'
        + '<td>' + this._lcNum((it.oos_metrics || {}).sharpe) + '</td>'
        + '<td>' + this._esc(it.hypothesis || '') + '</td>'
        + '</tr>').join('') : '';

      panel.innerHTML = '<div class="bt-lc-head">'
        + '<div class="bt-lc-title">' + this._esc(s.name || s.strategy_id || '') + ' '
        + (d.verified
          ? '<span class="bt-lc-ok" title="历史上至少有一次回测通过体检（数据完整、成交样本 ≥ 5 笔）">✓ 曾通过体检</span>'
          : '<span class="bt-lc-warn" title="历史上没有任何一次回测通过体检（数据完整、成交样本 ≥ 5 笔）">⚠ 尚未通过体检</span>')
        + '</div>'
        + '<button class="bt-card-action-btn" onclick="window._btPanel.closeLifecycle()">关闭</button></div>'
        + '<div class="bt-lc-body">'
        + '<div class="bt-lc-block"><div class="bt-lc-h">① 回测记录（' + runs.length + ' / ' + (d.runs_total || 0) + '）</div>'
        + '<table class="bt-lc-table"><thead><tr><th>时间</th><th>标的</th><th>来源</th><th>体检</th>'
        + '<th>总收益</th><th>夏普</th><th>胜率</th><th>笔数</th><th>操作</th></tr></thead><tbody>' + runRows + '</tbody></table></div>'
        + '<div class="bt-lc-block"><div class="bt-lc-h">② 评判（' + (ev.evaluated || 0) + ' 次：' + evCounts + '）</div>'
        + (evRows ? '<table class="bt-lc-table"><thead><tr><th>run</th><th>结论</th><th>IS / OOS 夏普</th>'
            + '<th>比值</th><th>切分日</th></tr></thead><tbody>' + evRows + '</tbody></table>'
          : '<div class="bt-lc-empty">还没有样本内外对照（回测完成后会自动补跑）</div>') + '</div>'
        + '<div class="bt-lc-block"><div class="bt-lc-h">③ 前瞻跟踪'
        + '<button class="bt-lc-link" onclick="window._btPanel.gotoSignalEval()" title="去 AI 信号评测页看全部信号的兑现情况">去信号评测 ›</button></div>'
        + '<div class="bt-lc-line">' + fwLine + '</div>'
        + (wilson ? '<div class="bt-lc-line bt-lc-muted">' + wilson + '</div>' : '')
        + '<div class="bt-lc-line bt-lc-muted">已跟踪 ' + (fw.days_tracked || 0) + ' 天</div></div>'
        + '<div class="bt-lc-block"><div class="bt-lc-h">④ 落地（盯盘规则 ' + (mon.matched || 0) + '）'
        + '<button class="bt-lc-link" onclick="window._btPanel.gotoMonitor()" title="打开盯盘面板看这些规则的实时触发">去盯盘面板 ›</button></div>'
        + '<table class="bt-lc-table"><thead><tr><th>标的</th><th>类型</th><th>阈值</th><th>状态</th>'
        + '<th>备注</th></tr></thead><tbody>' + monRows + '</tbody></table></div>'
        + (iterRows ? '<div class="bt-lc-block"><div class="bt-lc-h">⑤ 研发迭代史（strategy_hash 反查）'
            + '<button class="bt-lc-link" onclick="window._btPanel.gotoStrategyResearch()" title="打开策略研究面板，看这个策略每一轮的想法与改动">打开策略研究 ›</button></div>'
            + '<table class="bt-lc-table"><thead><tr><th>轮次</th><th>结论</th><th>OOS 夏普</th>'
            + '<th>假设</th></tr></thead><tbody>' + iterRows + '</tbody></table></div>' : '')
        + '</div>';
    }
    selectStrategy(id) {
      // 多策略模式：toggle 选中
      if (this._multiMode) {
        if (this._selectedStrategies.has(id)) {
          this._selectedStrategies.delete(id);
        } else {
          this._selectedStrategies.add(id);
        }
        document.querySelectorAll('.bt-strategy-card').forEach(c => {
          c.classList.toggle('selected', this._selectedStrategies.has(c.dataset.id));
        });
        this._updateMultiBtn();
        // 显示第一个选中策略的参数
        const first = [...this._selectedStrategies][0];
        const s = first ? this.strategies.find(s => s.strategy_id === first) : null;
        if (s) this._renderParams(s.params_schema);
        return;
      }
      // 单选模式
      this.selectedStrategy = id;
      this._selectedStrategies = new Set([id]);
      document.querySelectorAll('.bt-strategy-card').forEach(c => c.classList.toggle('selected', c.dataset.id === id));
      const s = this.strategies.find(s => s.strategy_id === id);
      if (s) this._renderParams(s.params_schema);
    }

    toggleMultiMode() {
      this._multiMode = !this._multiMode;
      const btn = document.getElementById('btMultiModeBtn');
      if (btn) {
        btn.classList.toggle('active', this._multiMode);
        btn.textContent = this._multiMode ? '多策略: ON' : '多策略对比';
      }
      if (!this._multiMode) {
        // 退出多选：恢复单选
        this._selectedStrategies = new Set();
        document.querySelectorAll('.bt-strategy-card').forEach(c => c.classList.remove('selected'));
      }
      this._updateMultiBtn();
    }

    _updateMultiBtn() {
      const runBtn = document.getElementById('btRunBtn');
      if (runBtn && this._multiMode) {
        const n = this._selectedStrategies.size;
        runBtn.textContent = n >= 2 ? `▶ 多策略对比 (${n}策略)` : '▶ 运行回测';
      } else if (runBtn && !this._multiMode) {
        runBtn.textContent = '▶ 运行回测';
      }
    }

    _renderParams(schema) {
      const container = document.getElementById('btParams');
      if (!container || !schema) return;
      const params = Array.isArray(schema) ? schema : [];
      container.innerHTML = params.map(p => `
        <div class="bt-param-row">
          <label>${p.label}</label>
          <input type="number" data-key="${p.key}" value="${p.default}" min="${p.min || ''}" max="${p.max || ''}">
          ${p.unit ? `<span class="unit">${p.unit}</span>` : ''}
        </div>
      `).join('');
    }

    // ========== 策略编辑器（可视化构造器） ==========

    _bindEditorEvents() {
      document.getElementById('btAddStrategyBtn')?.addEventListener('click', () => this.showEditor());
      document.getElementById('btEditorSaveBtn')?.addEventListener('click', () => this._saveStrategy());
      document.getElementById('btEditorTemplateBtn')?.addEventListener('click', () => this._showTemplatePicker());
    }

    async _ensureMeta() {
      if (!this._btMeta) {
        try {
          this._btMeta = await window.StockRadar.api.fetchJSON('/api/backtest/strategy-meta');
        } catch (e) {
          this._btMeta = { indicators: {}, operators: [], data_refs: [], templates: [] };
        }
      }
      return this._btMeta;
    }

    async showEditor(strategyId) {
      const editor = document.getElementById('btEditor');
      if (!editor) return;
      editor.style.display = 'block';
      // v3 F4 编辑器模式：隐藏策略列表，编辑区占满整宽（380px 侧栏放不下条件树）
      if (this.modal) this.modal.classList.add('bt-editor-mode');
      await this._ensureMeta();

      this._editingId = strategyId || null;
      const idInput = document.getElementById('btEditorId');
      const nameInput = document.getElementById('btEditorName');
      const descInput = document.getElementById('btEditorDesc');
      const title = document.getElementById('btEditorTitle');

      if (strategyId) {
        const s = this.strategies.find(s => s.strategy_id === strategyId);
        if (!s) return;
        title.textContent = '编辑策略';
        idInput.value = s.strategy_id || '';
        idInput.disabled = true;
        nameInput.value = s.name || '';
        descInput.value = s.description || '';
        // 从 JSON 还原表单
        const code = s.code || '';
        if (code.startsWith('{')) {
          try { this._builderFromJson(JSON.parse(code)); } catch(e) { this._builderReset(); }
        } else {
          this._builderReset();
        }
      } else {
        title.textContent = '新建策略';
        idInput.value = '';
        idInput.disabled = false;
        nameInput.value = '';
        descInput.value = '';
        this._builderReset();
      }
      this._hideError();
      this._builderRefreshJson();
      editor.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    closeEditor() {
      const editor = document.getElementById('btEditor');
      if (editor) editor.style.display = 'none';
      if (this.modal) this.modal.classList.remove('bt-editor-mode');
      // 关闭 AI 面板
      const aiPrompt = document.getElementById('btAiPrompt');
      if (aiPrompt) aiPrompt.style.display = 'none';
    }

    // ---- AI 自然语言生成策略 ----

    toggleAiPrompt() {
      const panel = document.getElementById('btAiPrompt');
      if (!panel) return;
      const show = panel.style.display === 'none' || !panel.style.display;
      panel.style.display = show ? 'block' : 'none';
      if (show) {
        document.getElementById('btAiPromptInput')?.focus();
        document.getElementById('btAiStatus')?.style.setProperty('display', 'none');
      }
    }

    async doAiGenerate() {
      const input = document.getElementById('btAiPromptInput');
      const status = document.getElementById('btAiStatus');
      const btn = document.getElementById('btAiGenBtn');
      const prompt = (input?.value || '').trim();

      if (!prompt || prompt.length < 4) {
        status.textContent = '请输入至少4个字的策略描述';
        status.style.display = 'block';
        status.style.color = '#c62828';
        return;
      }

      // 获取已选股票代码
      const codeInput = document.getElementById('btCode');
      const code = codeInput?.value || '';

      status.textContent = '⏳ AI 正在生成策略...';
      status.style.color = 'var(--text-secondary)';
      status.style.display = 'block';
      if (btn) btn.disabled = true;

      try {
        const resp = await fetch('/api/backtest/strategies/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt, code }),
        });

        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `服务器错误 (${resp.status})`);
        }

        const data = await resp.json();
        const strategyJson = data.strategy;
        if (!strategyJson) throw new Error('AI 未返回有效策略');

        // 填入编辑器
        const meta = strategyJson.meta || {};
        const nameInput = document.getElementById('btEditorName');
        const descInput = document.getElementById('btEditorDesc');
        const idInput = document.getElementById('btEditorId');
        if (nameInput) nameInput.value = meta.name || '';
        if (descInput) descInput.value = meta.description || '';
        // 自动生成 ID
        if (idInput && !idInput.disabled) {
          const name = (meta.name || 'ai_strategy').replace(/[^a-zA-Z0-9\u4e00-\u9fff]/g, '_');
          idInput.value = 'ai_' + name.replace(/_+/g, '_').toLowerCase().replace(/[\u4e00-\u9fff]+/g, '').substring(0, 30) + '_' + Date.now().toString(36);
        }

        // 用 JSON 填入构造器
        this._builderFromJson(strategyJson);
        this._builderRefreshJson();
        this._hideError();

        status.textContent = '✅ 策略已生成，请预览后保存';
        status.style.color = '#2e7d32';
        // 3 秒后隐藏状态
        setTimeout(() => { status.style.display = 'none'; }, 3000);
      } catch (e) {
        status.textContent = '❌ ' + (e.message || '生成失败，请重试');
        status.style.color = '#c62828';
        console.error('[AI策略生成]', e);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    editStrategy(id) { this.showEditor(id); }

    async deleteStrategy(id) {
      const s = this.strategies.find(s => s.strategy_id === id);
      if (!s) return;
      if (!confirm(`确认删除策略 "${s.name}"？此操作不可撤销。`)) return;
      try {
        await window.StockRadar.api.deleteBacktestStrategy(id);
        await this._loadStrategies();
      } catch (e) { alert('删除失败: ' + (e.message || e)); }
    }

    // ---- 构造器状态 ----

    _builderReset() {
      this._builderParams = [];      // [{key, value, min, max, step, label, type}]
      this._builderBuyRules = [];     // [{left, op, right}]
      this._builderSellRules = [];
      this._builderChart = [];       // [{type, panel, ...}]
      this._builderRender();
    }

    _builderFromJson(data) {
      const params = data.params || {};
      this._builderParams = Object.keys(params).map(k => {
        const p = params[k] || {};
        // _def 记模板默认值：参数卡「恢复默认」按钮用（v3 F3）
        return { key: k, value: p.value ?? 0, _def: p.value ?? 0, min: p.min ?? 0, max: p.max ?? 999, step: p.step ?? 1, label: p.label ?? k, type: p.type ?? 'float',
                 help: p.help ?? '', recommended: p.recommended ?? null };
      });
      this._builderBuyRules = this._parseRules(data.conditions?.buy);
      this._builderSellRules = this._parseRules(data.conditions?.sell);
      this._builderChart = (data.chart_indicators || []).map(c => {
        const item = { ...c };
        if (!item.panel) item.panel = 'main';
        return item;
      });
      document.getElementById('btBuyLogic').value = data.conditions?.buy?.logic || 'AND';
      document.getElementById('btSellLogic').value = data.conditions?.sell?.logic || 'AND';
      this._builderRender();
    }

    _parseRules(cond) {
      if (!cond || !cond.rules) return [];
      // v3 S3：嵌套组递归解析成**可编辑**的树节点（旧行为：整组序列化成字符串
      // 原样带回，界面上组内完全改不了）
      return cond.rules.map(r => {
        if (r.type === 'group') {
          return { type: 'group', logic: r.logic || 'AND', rules: this._parseRules({ rules: r.rules }) };
        }
        return {
          left: this._defToForm(r.left),
          op: r.op || '>',
          right: this._defToForm(r.right),
        };
      });
    }

    _defToForm(def) {
      if (!def) return { type: 'value', value: 0 };
      if (def.factor) return { type: 'factor', factor: def.factor };   // v3 S4-B
      if (def.func) {
        const args = (def.args || []).map(a => {
          if (typeof a === 'string' && a.startsWith('{') && a.endsWith('}')) return a;
          return a;
        });
        return { type: 'indicator', func: def.func, args, field: def.field || '' };
      }
      if (def.data) return { type: 'data', data: def.data };
      if (def.value != null) return { type: 'value', value: def.value };
      if (def.pattern) return { type: 'value', value: 0 }; // pattern 暂不支持
      return { type: 'value', value: 0 };
    }

    // ---- 渲染 ----

    _builderRender() {
      this._builderRenderParams();
      this._builderRenderRules('buy');
      this._builderRenderRules('sell');
      this._builderRenderChart();
      this._builderRefreshJson();
    }

    _builderRenderParams() {
      const el = document.getElementById('btBuilderParams');
      if (!el) return;
      if (!this._builderParams.length) { el.innerHTML = '<div class="bt-builder-empty">暂无参数，条件中用 {参数名} 自动创建</div>'; return; }
      /* v3 F3 参数可读化：默认值 + 人话解释 + 推荐区间 + 一键恢复。
         help/recommended 来自模板 params（json_templates/*.json）。 */
      el.innerHTML = this._builderParams.map((p, i) => {
        const out = p.recommended && (p.value < p.recommended[0] || p.value > p.recommended[1]);
        return `
        <div class="bt-builder-param${out ? ' out' : ''}" data-pi="${i}">
          <div class="bt-builder-param-row">
            <input value="${this._esc(p.key)}" placeholder="参数名" class="bt-builder-param-key" onchange="window._btPanel._builderUpdateParam(${i},'key',this.value)">
            <input value="${this._esc(p.label)}" placeholder="标签" class="bt-builder-param-label" onchange="window._btPanel._builderUpdateParam(${i},'label',this.value)">
            <input type="number" value="${p.value}" placeholder="默认值" class="bt-builder-param-val" onchange="window._btPanel._builderUpdateParam(${i},'value',parseFloat(this.value)||0)">
            <span class="bt-builder-param-rec">${p.recommended ? `推荐 ${p.recommended[0]}–${p.recommended[1]}` : ''}</span>
            <button class="bt-builder-param-del" onclick="window._btPanel._builderRemoveParam(${i})" title="删除">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
            </button>
          </div>
          ${p.help ? `<div class="bt-builder-param-help">${this._esc(p.help)}</div>` : ''}
          <div class="bt-builder-param-foot">
            <span class="bt-builder-param-out">超出推荐区间，可能过拟合或不稳定（不阻断保存）</span>
            ${p._def !== undefined ? `<button class="bt-btn-ghost-sm" onclick="window._btPanel._builderResetParam(${i})">恢复默认 ${p._def}</button>` : ''}
          </div>
        </div>`;
      }).join('');
    }

    _builderResetParam(i) {
      const p = this._builderParams[i];
      if (!p || p._def === undefined) return;
      p.value = p._def;
      this._builderRenderParams();
      this._builderRefreshJson();
    }

    _builderRenderRules(side) {
      const rules = side === 'buy' ? this._builderBuyRules : this._builderSellRules;
      const el = document.getElementById(side === 'buy' ? 'btBuyRules' : 'btSellRules');
      if (!el) return;
      if (!rules.length) {
        el.innerHTML = `<div class="bt-builder-empty">${side === 'buy' ? '无条件买入（每次 bar 都买入）' : '无卖出条件（仅靠止损止盈离场）'}</div>`;
      } else {
        el.innerHTML = rules.map((r, i) => this._renderRuleNode(side, r, String(i), 0)).join('');
      }
      this._builderRenderPreview(side);
    }

    /* ── v3 S3 嵌套条件树 ─────────────────────────────────────
       path 形如 "0/1"（空串 = 根层），寻址 _builderBuyRules 里的节点。
       深度上限 3（根 + 2 层子组）：再深已无法一眼读懂布尔含义。
       序列化 _rulesToJson 会跳过空组，预览区同步提示。 */
    _rulesArrAt(side, path) {
      let rules = side === 'buy' ? this._builderBuyRules : this._builderSellRules;
      for (const i of path) {
        const g = rules[i];
        if (!g || g.type !== 'group') return null;
        rules = g.rules || [];
      }
      return rules;
    }

    _nodeAt(side, path) {
      const idx = String(path || '').split('/').filter(x => x !== '').map(Number);
      if (!idx.length) return null;
      const arr = this._rulesArrAt(side, idx.slice(0, -1));
      return arr ? arr[idx[idx.length - 1]] : null;
    }

    _renderRuleNode(side, r, path, depth) {
      if (r.type === 'group') {
        const kid = (r.rules || []).map((c, i) =>
          this._renderRuleNode(side, c, path + '/' + i, depth + 1)).join('');
        const addGroup = depth < 2
          ? `<button class="bt-btn-ghost-sm" onclick="window._btPanel._builderAddGroupAt('${side}','${path}')">+ 子组</button>` : '';
        return `<div class="bt-builder-group" data-depth="${depth}">
          <div class="bt-builder-group-head">
            <span class="bt-builder-group-tag">组</span>
            <select class="bt-logic-select" onchange="window._btPanel._builderGroupLogic('${side}','${path}',this.value)">
              <option value="AND"${r.logic === 'AND' ? ' selected' : ''}>同时满足 (AND)</option>
              <option value="OR"${r.logic === 'OR' ? ' selected' : ''}>任一满足 (OR)</option>
            </select>
            <span class="bt-builder-group-n">本组 ${r.rules.length} 条</span>
            <button class="bt-builder-group-del" onclick="window._btPanel._builderRemoveAt('${side}','${path}')">删除组</button>
          </div>
          <div class="bt-builder-group-body">${kid || '<div class="bt-builder-empty">空组（保存时将被忽略）</div>'}</div>
          <div class="bt-builder-group-foot">
            <button class="bt-builder-add-rule" onclick="window._btPanel._builderAddLeafAt('${side}','${path}')">+ 条件</button>
            ${addGroup}
          </div>
        </div>`;
      }
      return `<div class="bt-builder-rule">
        <div class="bt-builder-rule-row">
          <span class="bt-builder-rule-side">左</span>${this._valuePickerHtml(r.left, `${side}:${path}:L`)}
        </div>
        <div class="bt-builder-rule-row">
          <select class="bt-builder-op" onchange="window._btPanel._builderRuleProp('${side}','${path}','op',this.value)">${this._opOptions(r.op)}</select>
        </div>
        <div class="bt-builder-rule-row">
          <span class="bt-builder-rule-side">右</span>${this._valuePickerHtml(r.right, `${side}:${path}:R`)}
        </div>
        <button class="bt-builder-rule-del" onclick="window._btPanel._builderRemoveAt('${side}','${path}')" title="移除">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
        </button>
      </div>`;
    }

    _builderRenderPreview(side) {
      const el = document.getElementById(side === 'buy' ? 'btBuyPreview' : 'btSellPreview');
      if (!el) return;
      const logic = document.getElementById(side === 'buy' ? 'btBuyLogic' : 'btSellLogic')?.value || 'AND';
      const rules = side === 'buy' ? this._builderBuyRules : this._builderSellRules;
      const empty = this._countEmptyGroups(rules);
      const text = this._condText(rules, logic);
      el.innerHTML = '当 <b>' + this._esc(text || '（无条件）') + '</b> 时' + (side === 'buy' ? '买入' : '卖出')
        + (empty ? ` <span style="color:var(--warn,#c88a1e)">· ${empty} 个空组将被忽略</span>` : '');
    }

    _countEmptyGroups(rules) {
      let n = 0;
      (rules || []).forEach(r => {
        if (r.type === 'group') {
          if (!(r.rules || []).length) n++;
          n += this._countEmptyGroups(r.rules);
        }
      });
      return n;
    }

    _condText(rules, logic) {
      const parts = (rules || []).map(r =>
        r.type === 'group' ? this._condText(r.rules, r.logic || 'AND') : this._leafText(r)
      ).filter(Boolean);
      if (!parts.length) return '';
      const j = parts.join(logic === 'AND' ? ' 且 ' : ' 或 ');
      return parts.length > 1 ? `（${j}）` : j;
    }

    _leafText(r) {
      const meta = this._btMeta || {};
      const label = (def) => {
        if (def.type === 'indicator') {
          const im = meta.indicators?.[def.func] || {};
          let t = im.label || def.func;
          if (def.field) t += '·' + (((im.fields || []).find(f => f.value === def.field) || {}).label || def.field);
          (def.args || []).forEach(a => {
            if (typeof a !== 'string' || a.startsWith('{')) t += ' ' + a;   // 数字/参数引用才展示，'close' 之类数据引用略
          });
          return t;
        }
        if (def.type === 'data') return ((meta.data_refs || []).find(d => d.value === def.data) || {}).label || def.data;
        return String(def.value ?? '');
      };
      const op = ((meta.operators || []).find(o => o.value === r.op) || {}).label || r.op;
      return `${label(r.left)} ${op} ${label(r.right)}`;
    }

    _valuePickerHtml(def, prefix) {
      const type = def.type || 'indicator';
      const meta = this._btMeta || {};
      const indicatorOptions = Object.keys(meta.indicators || {}).map(k =>
        `<option value="${k}" ${def.func === k ? 'selected' : ''}>${meta.indicators[k].label || k}</option>`
      ).join('');
      const dataOptions = (meta.data_refs || []).map(d =>
        `<option value="${d.value}" ${def.data === d.value ? 'selected' : ''}>${d.label}</option>`
      ).join('');

      let html = `<select class="bt-builder-val-type" onchange="window._btPanel._builderChangeValType('${prefix}',this.value)">
        <option value="indicator" ${type === 'indicator' ? 'selected' : ''}>指标</option>
        <option value="factor" ${type === 'factor' ? 'selected' : ''}>因子</option>
        <option value="data" ${type === 'data' ? 'selected' : ''}>价格数据</option>
        <option value="value" ${type === 'value' ? 'selected' : ''}>固定值</option>
      </select>`;

      if (type === 'factor') {
        // v3 S4-B：因子操作数（时序语义：该票自己的因子值，不是横截面排名）
        const factorOptions = (meta.factors || []).map(f =>
          `<option value="${f.field}" ${def.factor === f.field ? 'selected' : ''} title="${f.description || ''}">因子 · ${f.label}</option>`
        ).join('');
        html += `<select class="bt-builder-val-factor" onchange="window._btPanel._builderChangeValFactor('${prefix}',this.value)">${factorOptions}</select>`;
        html += `<input class="bt-builder-val-num" type="number" step="any" value="${def.value ?? 0}" onchange="window._btPanel._builderChangeValValue('${prefix}',parseFloat(this.value)||0)">`;
      } else if (type === 'indicator') {
        html += `<select class="bt-builder-val-func" onchange="window._btPanel._builderChangeValFunc('${prefix}',this.value)">${indicatorOptions}</select>`;
        // args
        const funcMeta = meta.indicators?.[def.func || 'ma'] || {};
        const argNames = funcMeta.args || ['data', 'period'];
        const fields = funcMeta.fields || [];
        argNames.forEach((an, ai) => {
          const av = (def.args || [])[ai] || '';
          if (an === 'data') {
            html += `<select class="bt-builder-val-arg" onchange="window._btPanel._builderChangeValArg('${prefix}',${ai},this.value)">${dataOptions}</select>`;
          } else {
            html += `<input class="bt-builder-val-arg-num" type="number" value="${String(av).replace('{','').replace('}','')}" placeholder="${an}" onchange="window._btPanel._builderChangeValArg('${prefix}',${ai},this.value)">`;
          }
        });
        // field picker for multi-line indicators
        if (fields.length) {
          const fieldOptions = fields.map(f =>
            `<option value="${f.value}" ${def.field === f.value ? 'selected' : ''}>${f.label}</option>`
          ).join('');
          html += `<select class="bt-builder-val-field" onchange="window._btPanel._builderChangeValField('${prefix}',this.value)"><option value="">—</option>${fieldOptions}</select>`;
        }
      } else if (type === 'data') {
        html += `<select class="bt-builder-val-data" onchange="window._btPanel._builderChangeValData('${prefix}',this.value)">${dataOptions}</select>`;
      } else {
        html += `<input class="bt-builder-val-num" type="number" value="${def.value ?? 0}" step="any" onchange="window._btPanel._builderChangeValValue('${prefix}',parseFloat(this.value)||0)">`;
      }
      return html;
    }

    _opOptions(cur) {
      const ops = this._btMeta?.operators || [];
      return ops.map(o => `<option value="${o.value}" ${cur === o.value ? 'selected' : ''}>${o.label}</option>`).join('');
    }

    _builderRenderChart() {
      const el = document.getElementById('btChartIndicators');
      if (!el) return;
      const existing = this._builderChart;
      const indicatorItems = ['MA', 'EMA', 'RSI', 'MACD', 'BOLL', 'KDJ'];
      let html = '<div style="display:flex;flex-wrap:wrap;gap:6px;font-size:11px">';
      indicatorItems.forEach(t => {
        const checked = existing.some(c => c.type === t) ? 'checked' : '';
        html += `<label style="display:flex;align-items:center;gap:3px;cursor:pointer;padding:2px 6px;border:1px solid var(--border);border-radius:var(--radius-sm);background:${checked?'var(--brand-bg-light)':'var(--panel)'}"><input type="checkbox" ${checked} onchange="window._btPanel._builderToggleChart('${t}',this.checked)" style="margin:0">${t}</label>`;
      });
      html += '</div>';
      el.innerHTML = html;
    }

    _builderRefreshJson() {
      const data = this._builderBuildJson();
      const area = document.getElementById('btEditorJson');
      if (area) area.value = JSON.stringify(data, null, 2);
    }

    _builderBuildJson() {
      const params = {};
      this._builderParams.forEach(p => {
        params[p.key] = { value: p.value, min: p.min, max: p.max, step: p.step, label: p.label, type: p.type };
      });
      // 自动从规则中提取未定义的 {param} 引用
      this._collectAllRules().forEach(r => {
        [r.left, r.right].forEach(side => {
          if (side.type === 'indicator') (side.args || []).forEach(a => {
            if (typeof a === 'string' && a.startsWith('{') && a.endsWith('}')) {
              const pn = a.slice(1, -1);
              if (!params[pn]) params[pn] = { value: 0 };
            }
          });
          if (side.type === 'value' && typeof side.value === 'string' && String(side.value).startsWith('{')) {
            const pn = String(side.value).slice(1, -1);
            if (!params[pn]) params[pn] = { value: 0 };
          }
        });
      });

      return {
        meta: {
          strategy_id: document.getElementById('btEditorId')?.value || '',
          name: document.getElementById('btEditorName')?.value || '',
          description: document.getElementById('btEditorDesc')?.value || '',
          category: 'custom',
          tags: [],
        },
        params,
        conditions: {
          buy: {
            logic: document.getElementById('btBuyLogic')?.value || 'AND',
            rules: this._rulesToJson(this._builderBuyRules),
          },
          sell: {
            logic: document.getElementById('btSellLogic')?.value || 'AND',
            rules: this._rulesToJson(this._builderSellRules),
          },
        },
        chart_indicators: this._builderChart,
      };
    }

    _collectAllRules() {
      const walk = (rules) => (rules || []).flatMap(r =>
        r.type === 'group' ? walk(r.rules) : [r]);
      return [...walk(this._builderBuyRules), ...walk(this._builderSellRules)];
    }

    _rulesToJson(rules) {
      // v3 S3：递归序列化嵌套组。空组**跳过**（空组在布尔语义里没有意义，
      // 保留会让编译器困惑）；预览区会提示「有 N 个空组将被忽略」。
      return rules.map(r => {
        if (r.type === 'group') {
          const kids = this._rulesToJson(r.rules || []);
          if (!kids.length) return null;
          return { type: 'group', logic: r.logic || 'AND', rules: kids };
        }
        return {
          type: 'indicator',
          left: this._formToDef(r.left),
          op: r.op,
          right: this._formToDef(r.right),
        };
      }).filter(Boolean);
    }

    _formToDef(fd) {
      if (fd.type === 'indicator') {
        const d = { func: fd.func, args: fd.args || [] };
        if (fd.field) d.field = fd.field;
        return d;
      }
      if (fd.type === 'factor') return { factor: fd.factor || 'close_position_20' };  // v3 S4-B
      if (fd.type === 'data') return { data: fd.data || 'close' };
      return { value: fd.value ?? 0 };
    }

    // ---- 构造器交互 ----

    _builderAddParam() {
      this._builderParams.push({ key: '', value: 0, min: 0, max: 999, step: 1, label: '', type: 'int' });
      this._builderRenderParams();
    }

    _builderUpdateParam(i, prop, val) {
      if (this._builderParams[i]) {
        this._builderParams[i][prop] = val;
        this._builderRefreshJson();
        if (prop === 'value') this._builderRenderParams();   // 重新判定「超出推荐区间」
      }
    }

    _builderRemoveParam(i) {
      this._builderParams.splice(i, 1);
      this._builderRenderParams();
      this._builderRefreshJson();
    }

    _builderAddRule(side) { this._builderAddLeafAt(side, ''); }

    /* ── v3 S3：path 寻址的增删改（根 = ''，子组 = "0/1"）────────── */
    _builderAddLeafAt(side, path) {
      const arr = this._rulesArrAt(side, path.split('/').filter(x => x !== '').map(Number));
      if (!arr) return;
      arr.push({ left: { type: 'indicator', func: 'ma', args: ['close', 5] }, op: 'cross_above', right: { type: 'indicator', func: 'ma', args: ['close', 20] } });
      this._builderRenderRules(side);
      this._builderRefreshJson();
    }

    _builderAddGroupAt(side, path) {
      const arr = this._rulesArrAt(side, path.split('/').filter(x => x !== '').map(Number));
      if (!arr) return;
      arr.push({ type: 'group', logic: 'AND', rules: [
        { left: { type: 'indicator', func: 'ma', args: ['close', 5] }, op: 'cross_above', right: { type: 'indicator', func: 'ma', args: ['close', 20] } },
      ] });
      this._builderRenderRules(side);
      this._builderRefreshJson();
    }

    _builderRemoveAt(side, path) {
      const idx = path.split('/').filter(x => x !== '').map(Number);
      const last = idx.pop();
      const arr = this._rulesArrAt(side, idx);
      if (!arr) return;
      arr.splice(last, 1);
      this._builderRenderRules(side);
      this._builderRefreshJson();
    }

    _builderRuleProp(side, path, prop, val) {
      const n = this._nodeAt(side, path);
      if (n) {
        n[prop] = val;
        this._builderRefreshJson();
        this._builderRenderPreview(side);
      }
    }

    _builderGroupLogic(side, path, val) {
      if (!path) {
        const el = document.getElementById(side === 'buy' ? 'btBuyLogic' : 'btSellLogic');
        if (el) el.value = val;
      } else {
        const g = this._nodeAt(side, path);
        if (g) g.logic = val;
      }
      this._builderRefreshJson();
      this._builderRenderPreview(side);
    }

    _resolvePrefix(prefix) {
      // v3 S3：prefix 格式 `${side}:${path}:${L|R}`，path 空串 = 根层叶子
      const m = prefix.match(/^(buy|sell):([0-9/]*):(L|R)$/);
      if (!m) return null;
      const node = this._nodeAt(m[1], m[2]);
      return node ? (m[3] === 'L' ? node.left : node.right) : null;
    }

    _builderOnRuleChanged(prefix) {
      const side = (prefix || '').split(':')[0] || 'buy';
      this._builderRenderRules(side);
      this._builderRefreshJson();
    }

    _builderChangeValType(prefix, newType) {
      const def = this._resolvePrefix(prefix);
      if (!def) return;
      if (newType === 'indicator') { def.type = 'indicator'; def.func = 'ma'; def.args = ['close', 5]; def.field = ''; }
      else if (newType === 'factor') { def.type = 'factor'; def.factor = 'close_position_20'; }   // v3 S4-B
      else if (newType === 'data') { def.type = 'data'; def.data = 'close'; }
      else { def.type = 'value'; def.value = 0; }
      this._builderOnRuleChanged(prefix);
    }

    _builderChangeValFactor(prefix, field) {   // v3 S4-B
      const def = this._resolvePrefix(prefix);
      if (!def) return;
      def.type = 'factor';
      def.factor = field;
      this._builderOnRuleChanged(prefix);
    }

    _builderChangeValFunc(prefix, func) {
      const def = this._resolvePrefix(prefix);
      if (!def) return;
      def.func = func;
      def.args = ['close', 5]; // reset args
      def.field = '';
      this._builderOnRuleChanged(prefix);
    }

    _builderChangeValArg(prefix, idx, val) {
      const def = this._resolvePrefix(prefix);
      if (!def) return;
      if (!def.args) def.args = [];
      def.args[idx] = val;
      this._builderOnRuleChanged(prefix);
    }

    _builderChangeValField(prefix, val) {
      const def = this._resolvePrefix(prefix);
      if (!def) return;
      def.field = val;
      this._builderOnRuleChanged(prefix);
    }

    _builderChangeValData(prefix, val) {
      const def = this._resolvePrefix(prefix);
      if (!def) return;
      def.data = val;
      this._builderOnRuleChanged(prefix);
    }

    _builderChangeValValue(prefix, val) {
      const def = this._resolvePrefix(prefix);
      if (!def) return;
      def.value = val;
      this._builderOnRuleChanged(prefix);
    }

    _resolvePrefix(prefix) {
      // prefix format: buyL0, buyR1, sellL0, sellR1
      const m = prefix.match(/^(buy|sell)(L|R)(\d+)$/);
      if (!m) return null;
      const [, side, lr, idx] = m;
      const rules = side === 'buy' ? this._builderBuyRules : this._builderSellRules;
      const rule = rules[parseInt(idx)];
      if (!rule) return null;
      return lr === 'L' ? rule.left : rule.right;
    }

    _builderToggleChart(type, on) {
      if (on) {
        if (!this._builderChart.some(c => c.type === type)) {
          this._builderChart.push({ type, panel: type === 'MACD' || type === 'RSI' || type === 'KDJ' ? 'sub' : 'main' });
        }
      } else {
        this._builderChart = this._builderChart.filter(c => c.type !== type);
      }
      this._builderRefreshJson();
    }

    async _showTemplatePicker() {
      const meta = this._btMeta || {};
      const templates = meta.templates || [];
      if (!templates.length) { alert('暂无内置模板'); return; }
      const names = templates.map((t, i) => `${i + 1}. ${t.name}`).join('\n');
      const choice = prompt(`选择模板加载（输入编号取消）:\n${names}`, '1');
      if (!choice) return;
      const idx = parseInt(choice) - 1;
      if (isNaN(idx) || idx < 0 || idx >= templates.length) return;
      const t = templates[idx];
      try {
        const res = await window.StockRadar.api.fetchJSON(`/api/backtest/strategies/${encodeURIComponent(t.strategy_id)}`);
        const code = res.code || '';
        if (code.startsWith('{')) {
          this._builderFromJson(JSON.parse(code));
          document.getElementById('btEditorId').value = '';
          document.getElementById('btEditorName').value = t.name + ' (副本)';
          document.getElementById('btEditorDesc').value = res.description || '';
        }
      } catch(e) { alert('加载模板失败: ' + (e.message || e)); }
    }

    // ---- 保存 ----

    async _saveStrategy() {
      const idInput = document.getElementById('btEditorId');
      const nameInput = document.getElementById('btEditorName');
      const descInput = document.getElementById('btEditorDesc');
      const saveBtn = document.getElementById('btEditorSaveBtn');

      const strategyId = (idInput.value || '').trim();
      const name = (nameInput.value || '').trim();
      if (!strategyId) { this._showError('请输入策略 ID'); return; }
      if (!/^[a-zA-Z0-9_]+$/.test(strategyId)) { this._showError('策略 ID 只能包含英文、数字、下划线'); return; }
      if (!name) { this._showError('请输入策略名称'); return; }

      const jsonData = this._builderBuildJson();
      jsonData.meta.strategy_id = strategyId;
      jsonData.meta.name = name;

      saveBtn.disabled = true; saveBtn.textContent = '保存中…';
      try {
        await window.StockRadar.api.saveBacktestStrategy({
          strategy_id: strategyId,
          name,
          description: descInput.value.trim(),
          strategy_json: jsonData,
          category: jsonData.meta.category || 'custom',
        });
        this.closeEditor();
        await this._loadStrategies();
      } catch (e) {
        this._showError('保存失败: ' + (e.message || e));
      } finally {
        saveBtn.disabled = false; saveBtn.textContent = '保存策略';
      }
    }

    _showError(msg) {
      const el = document.getElementById('btEditorError');
      if (el) { el.textContent = msg; el.style.display = 'block'; }
    }

    _hideError() {
      const el = document.getElementById('btEditorError');
      if (el) el.style.display = 'none';
    }

    _esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }

    // ============================================================
    // ========== 弹窗最大化 / 恢复 / 拖动 ==========
    _bindDragAndResize() {
      const tabbar = document.getElementById('btTabbar');
      const btn = document.getElementById('btMaximizeBtn');
      if (btn) btn.addEventListener('click', () => this.toggleMaximize());
      if (!tabbar || !this.modal) return;

      tabbar.addEventListener('mousedown', (e) => {
        // 不响应按钮上的拖动
        if (e.target.closest('button')) return;
        if (this._maximized) return;
        const rect = this.modal.getBoundingClientRect();
        this._dragState = {
          dx: e.clientX - rect.left,
          dy: e.clientY - rect.top,
        };
        tabbar.classList.add('dragging');
        e.preventDefault();
      });
      window.addEventListener('mousemove', (e) => {
        if (!this._dragState || this._maximized) return;
        const x = e.clientX - this._dragState.dx;
        const y = e.clientY - this._dragState.dy;
        this.modal.style.transform = `translate(${x}px, ${y}px)`;
      });
      window.addEventListener('mouseup', () => {
        if (!this._dragState) return;
        this._dragState = null;
        tabbar.classList.remove('dragging');
      });
    }

    toggleMaximize() { this._maximized ? this.restore() : this.maximize(); }

    maximize() {
      if (!this.modal) return;
      this.modal.classList.add('maximized');
      this.modal.style.transform = '';
      this._maximized = true;
      const btn = document.getElementById('btMaximizeBtn');
      if (btn) { btn.title = '恢复'; }
      this._resizeCharts();
    }

    restore() {
      if (!this.modal) return;
      this.modal.classList.remove('maximized');
      this.modal.classList.remove('eval-mode');
      this.modal.style.transform = '';
      this._maximized = false;
      const btn = document.getElementById('btMaximizeBtn');
      if (btn) { btn.title = '全屏'; }
      this._resizeCharts();
    }

    _resizeCharts() {
      setTimeout(() => {
        ['btEquityChart', 'btKlineChart', 'btDrawdownChart', 'btBatchEquityChart'].forEach(id => {
          const el = document.getElementById(id);
          if (el) {
            const inst = echarts.getInstanceByDom(el);
            if (inst) inst.resize();
          }
        });
      }, 220);
    }

    // ============================================================

    _bindEvents() {
      const btn = document.getElementById('btRunBtn');
      if (btn) btn.addEventListener('click', () => this._runBacktest());

      // 多 code 输入：单值时自动识别资产类型 + 渲染 chips
      const codesInput = document.getElementById('btInputCodes');
      if (codesInput) {
        codesInput.addEventListener('input', () => {
          const codes = this._parseCodes(codesInput.value);
          if (codes.length === 1) this._autoDetectAssetType(codes[0]);
          this._renderChips();
        });
      }

      // 保存为测试集按钮
      const saveSetBtn = document.getElementById('btSaveTestSetBtn');
      if (saveSetBtn) saveSetBtn.addEventListener('click', () => this._showSaveTestSetDialog());
    }

    /** 解析多 code 输入：支持换行/逗号/空格分隔，去重，大写，保留 board_xxx */
    _parseCodes(raw) {
      if (!raw) return [];
      const seen = new Set();
      const out = [];
      for (const part of String(raw).split(/[\s,;，；\n\r\t]+/)) {
        const c = part.trim().toUpperCase();
        if (!c) continue;
        if (seen.has(c)) continue;
        seen.add(c);
        out.push(c);
      }
      return out;
    }

    _autoDetectAssetType(code) {
      const select = document.getElementById('btAssetType');
      if (!select) return;
      select.value = this._detectAssetType(code);
    }

    /** 检测资产类型，返回 stock/etf/index/board */
    _detectAssetType(code) {
      if (!code) return 'stock';
      const c = code.toUpperCase();
      if (c.startsWith('BOARD_')) return 'board';
      if (/^(51|52|56|58|15|16|18)\d{4}$/.test(c)) return 'etf';
      if (/^399\d{3}$/.test(c)) return 'index';
      return 'stock';
    }

    /** 渲染代码 chips（对齐原型） */
    _renderChips() {
      const container = document.getElementById('btChips');
      if (!container) return;
      const codes = this._parseCodes(document.getElementById('btInputCodes')?.value || '');
      if (codes.length === 0) {
        container.innerHTML = '<span style="color:var(--muted);font-size:11px;padding:4px">无有效代码</span>';
        return;
      }
      container.innerHTML = codes.map(c => {
        const type = this._detectAssetType(c);
        return `<span class="bt-chip">${this._esc(c)} <span class="bt-chip-type ${type}">${type.toUpperCase()}</span> <span class="bt-chip-close" onclick="window._btPanel?._removeChip('${this._esc(c)}')">✕</span></span>`;
      }).join('');
    }

    /** 删除某个 chip 并同步到 textarea */
    _removeChip(code) {
      const ta = document.getElementById('btInputCodes');
      if (!ta) return;
      const codes = this._parseCodes(ta.value).filter(c => c !== code);
      ta.value = codes.join(', ');
      this._renderChips();
    }

    _togglePositionSize() {
      const mode = document.getElementById('btPositionMode')?.value;
      const row = document.getElementById('btPositionSizeRow');
      const label = document.getElementById('btPositionSizeLabel');
      const hint = document.getElementById('btPositionSizeHint');
      if (!row) return;
      if (mode === 'full') { row.style.display = 'none'; return; }
      row.style.display = 'block';
      if (mode === 'percent') {
        label.textContent = '仓位比例 (%)';
        hint.textContent = '每次使用可用资金的百分比';
        document.getElementById('btPositionSize').value = 50;
      } else {
        label.textContent = '固定金额 (元)';
        hint.textContent = '每次买入固定金额';
        document.getElementById('btPositionSize').value = 50000;
      }
    }

    async _runBacktest() {
      const codesInput = document.getElementById('btInputCodes')?.value || '';
      const codes = this._parseCodes(codesInput);
      if (codes.length === 0) { alert('请输入至少一个标的代码'); return; }

      // 多策略模式：必须 ≥2 策略
      if (this._multiMode) {
        const sids = [...this._selectedStrategies];
        if (sids.length < 2) { alert('多策略对比至少选择 2 个策略'); return; }
        return this._runMulti(sids, codes);
      }

      if (!this.selectedStrategy) { alert('请选择策略'); return; }

      // 单 code 走原路径，多 code 走批量
      if (codes.length === 1) {
        return this._runSingle(codes[0]);
      }
      return this._runBatch(codes);
    }

    /** 单标回测（保持原逻辑） */
    async _runSingle(code) {
      const params = {};
      document.querySelectorAll('#btParams input[data-key]').forEach(input => {
        params[input.dataset.key] = parseFloat(input.value) || 0;
      });

      const positionMode = document.getElementById('btPositionMode')?.value || 'full';
      const positionSize = positionMode === 'full' ? 1.0 : (parseFloat(document.getElementById('btPositionSize')?.value) || 50);

      const assetType = document.getElementById('btAssetType')?.value || 'stock';

      const config = {
        code,
        strategy_id: this.selectedStrategy,
        params,
        start_date: document.getElementById('btStartDate')?.value || '2025-01-01',
        end_date: document.getElementById('btEndDate')?.value || '2026-06-01',
        initial_cash: parseFloat(document.getElementById('btCash')?.value) || 100000,
        commission: parseFloat(document.getElementById('btCommission')?.value) || 0.0003,
        min_commission: parseFloat(document.getElementById('btMinCommission')?.value) || 5,
        stamp_tax: assetType === 'etf' ? 0 : parseFloat(document.getElementById('btStampTax')?.value) || 0.0005,
        transfer_fee: parseFloat(document.getElementById('btTransferFee')?.value) || 0.00001,
        slippage: parseFloat(document.getElementById('btSlippage')?.value) || 0.001,
        adjust_type: document.getElementById('btAdjust')?.value || 'qfq',
        t_plus_1: document.getElementById('btTPlus1')?.checked ?? true,
        price_limit_mode: document.getElementById('btLimitMove')?.checked === false ? 'off' : 'enforce',
        lot_size: assetType === 'board' ? 1 : (document.getElementById('btLotSize100')?.checked ? 100 : 1),
        stop_loss: parseFloat(document.getElementById('btStopLoss')?.value) || 0,
        take_profit: parseFloat(document.getElementById('btTakeProfit')?.value) || 0,
        trailing_stop: parseFloat(document.getElementById('btTrailingStop')?.value) || 0,
        position_mode: positionMode,
        position_size: positionSize,
        asset_type: assetType,
      };

      document.getElementById('btEmpty').style.display = 'none';
      document.getElementById('btRunning').classList.add('visible');
      document.getElementById('btResult').style.display = 'none';
      document.getElementById('btRunBtn').disabled = true;

      this.switchTab(document.querySelector('.bt-tab[data-btab="btResultPanel"]'));

      try {
        const res = await window.StockRadar.api.runBacktest(config);
        const runId = res.run_id;
        this.currentRunId = runId;
        const result = await this._pollResult(runId);
        this._showResult(result, config);
      } catch (e) {
        alert('回测失败: ' + (e.message || e));
      } finally {
        document.getElementById('btRunning').classList.remove('visible');
        document.getElementById('btRunBtn').disabled = false;
      }
    }

    /** 批量回测：提交 batch，SSE 监听进度，渲染批量结果 */
    async _runBatch(codes) {
      const params = {};
      document.querySelectorAll('#btParams input[data-key]').forEach(input => {
        params[input.dataset.key] = parseFloat(input.value) || 0;
      });

      const positionMode = document.getElementById('btPositionMode')?.value || 'full';
      const positionSize = positionMode === 'full' ? 1.0 : (parseFloat(document.getElementById('btPositionSize')?.value) || 50);
      const assetType = document.getElementById('btAssetType')?.value || 'stock';

      const config = {
        codes,
        strategy_id: this.selectedStrategy,
        params,
        start_date: document.getElementById('btStartDate')?.value || '2025-01-01',
        end_date: document.getElementById('btEndDate')?.value || '2026-06-01',
        initial_cash: parseFloat(document.getElementById('btCash')?.value) || 100000,
        commission: parseFloat(document.getElementById('btCommission')?.value) || 0.0003,
        min_commission: parseFloat(document.getElementById('btMinCommission')?.value) || 5,
        stamp_tax: parseFloat(document.getElementById('btStampTax')?.value) || 0.0005,
        transfer_fee: parseFloat(document.getElementById('btTransferFee')?.value) || 0.00001,
        slippage: parseFloat(document.getElementById('btSlippage')?.value) || 0.001,
        adjust_type: document.getElementById('btAdjust')?.value || 'qfq',
        t_plus_1: document.getElementById('btTPlus1')?.checked ?? true,
        price_limit_mode: document.getElementById('btLimitMove')?.checked === false ? 'off' : 'enforce',
        lot_size: assetType === 'board' ? 1 : (document.getElementById('btLotSize100')?.checked ? 100 : 1),
        stop_loss: parseFloat(document.getElementById('btStopLoss')?.value) || 0,
        take_profit: parseFloat(document.getElementById('btTakeProfit')?.value) || 0,
        trailing_stop: parseFloat(document.getElementById('btTrailingStop')?.value) || 0,
        position_mode: positionMode,
        position_size: positionSize,
        concurrency: 3,
      };

      // 切到批量对比 tab + 显示运行中
      this.switchTab(document.querySelector('.bt-tab[data-btab="btBatchPanel"]'));
      document.getElementById('btBatchEmpty').style.display = 'none';
      document.getElementById('btBatchResult').style.display = 'none';
      document.getElementById('btBatchRunning').classList.add('visible');
      document.getElementById('btBatchProgressBar').style.width = '0%';
      document.getElementById('btBatchProgressText').textContent = '提交中...';
      document.getElementById('btRunBtn').disabled = true;

      try {
        const res = await window.StockRadar.api.runBatchBacktest(config);
        this.currentBatchId = res.batch_id;
        await this._streamBatchUntilDone(res.batch_id);
        const detail = await window.StockRadar.api.getBatch(res.batch_id);
        await this._renderBatchResult(detail);
      } catch (e) {
        alert('批量回测失败: ' + (e.message || e));
      } finally {
        document.getElementById('btBatchRunning').classList.remove('visible');
        document.getElementById('btRunBtn').disabled = false;
      }
    }

    /** SSE 订阅批次进度直到完成 */
    _streamBatchUntilDone(batchId) {
      return new Promise((resolve, reject) => {
        const es = window.StockRadar.api.streamBatchProgress(
          batchId,
          (msg) => {
            if (msg.error) { es.close(); reject(new Error(msg.error)); return; }
            const pct = Math.round((msg.pct || 0) * 100);
            const bar = document.getElementById('btBatchProgressBar');
            const txt = document.getElementById('btBatchProgressText');
            if (bar) bar.style.width = pct + '%';
            if (txt) {
              const last = msg.last_code ? ` · 最近: ${msg.last_code} (${msg.last_status || ''})` : '';
              txt.textContent = `${msg.current || 0} / ${msg.total || 0} (${pct}%)${last}`;
            }
            if (msg.status === 'completed' || msg.status === 'failed' || msg.status === 'partial') {
              es.close();
              resolve(msg);
            }
          },
          (err) => { es.close(); reject(err); }
        );
      });
    }

    _HEALTH_META = {
      ok: { text: '体检通过', cls: 'ok' },
      no_signal: { text: '零交易', cls: 'warn' },
      insufficient_bars: { text: '窗口过短', cls: 'warn' },
      data_gap: { text: '数据缺口', cls: 'warn' },
      bad_strategy: { text: '条件未生效', cls: 'bad' },
      invalid_params: { text: '参数不可行', cls: 'muted' },
      error: { text: '执行出错', cls: 'bad' }
    };

    _ORIGIN_LABEL = { manual: '手工', batch: '批量', multi: '多策略', research: '研发' };

    _originBadge(origin) {
      const label = this._ORIGIN_LABEL[origin];
      if (!label) return '';
      return '<span class="bt-origin bt-origin-' + origin + '">' + label + '</span>';
    }

    _renderHealthBadge(r) {
      const el = document.getElementById('btHealth');
      if (!el) return;
      const health = r && r.health;
      if (!health) { el.style.display = 'none'; return; }
      const meta = this._HEALTH_META[health] || { text: health, cls: 'muted' };
      const parts = ['<span class="bt-health-badge">' + meta.text + '</span>'];
      if (r.health_detail) {
        parts.push('<span class="bt-health-detail">' + r.health_detail + '</span>');
      }
      if (r.expected_bars) {
        const pct = ((r.coverage_pct || 0) * 100).toFixed(1);
        parts.push('<span class="bt-health-detail">数据 ' + (r.data_bars || 0) + '/' + r.expected_bars + ' 根（覆盖 ' + pct + '%）</span>');
      }
      if (r.trade_count > 0 && !r.trade_sample_ok) {
        parts.push('<span class="bt-health-detail">样本不足 5 笔，胜率不具统计意义</span>');
      }
      el.className = 'bt-health ' + meta.cls;
      el.innerHTML = parts.join('');
      el.style.display = 'block';
    }

    _bindGapEvents() {
      const box = document.getElementById('btGap');
      if (!box) return;
      box.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-gap-run]');
        if (!btn || btn.disabled) return;
        const runId = btn.dataset.gapRun;
        const index = Number(btn.dataset.gapIndex || 0);
        if (btn.dataset.gapStage === 'rerun') this._rerunAfterHeal(runId, btn);
        else this._healGap(runId, index, btn);
      });
    }

    _renderGapBlock(runId, result) {
      const el = document.getElementById('btGap');
      if (!el) return;
      if (!result || result.health !== 'data_gap') { el.style.display = 'none'; el.innerHTML = ''; return; }
      let gaps = [];
      try { gaps = JSON.parse(result.gaps_json || '[]'); } catch (e) { gaps = []; }
      if (!gaps.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
      const pct = (result.coverage_pct === null || result.coverage_pct === undefined)
        ? '—' : (result.coverage_pct * 100).toFixed(1) + '%';
      const rows = gaps.map((g, i) =>
        '<tr><td>' + (g.start || '—') + ' ~ ' + (g.end || '—') + '</td><td>' + (g.missing || 0) + ' 个交易日</td>' +
        '<td><button class="bt-gap-btn" data-gap-run="' + runId + '" data-gap-index="' + i + '">补这段数据</button></td></tr>').join('');
      el.innerHTML = '<div class="bt-gap"><div class="bt-gap-hd">数据缺口 ' + gaps.length + ' 段 · 覆盖率 ' + pct +
        '　<span class="bt-gap-tip">补完这段数据可以点「补完，重跑一次」</span></div><table>' + rows + '</table></div>';
      el.style.display = 'block';
    }

    _renderEvalBlock(evaluation, runId) {
      const el = document.getElementById('btEval');
      if (!el) return;
      const meta = {
        ok:                  { text: '样本内外一致', cls: 'ok' },
        overfit_suspect:     { text: '过拟合嫌疑', cls: 'bad' },
        insufficient_sample: { text: '样本不足', cls: 'warn' },
        oos_negative:        { text: '样本外为负', cls: 'bad' },
        failed:              { text: '评判失败', cls: 'warn' },
      };
      if (!evaluation) {
        // 评判是后台补跑（设计文档 §7）：首屏看不到属正常，等几秒自动回填
        el.innerHTML = '<div class="bt-eval empty">样本内外对照 · 尚未评判<span class="bt-eval-note">（正在后台补跑，稍后自动刷新）</span></div>';
        el.style.display = 'block';
        if (runId) this._scheduleEvalRefresh(runId, 0);
        return;
      }
      const m = meta[evaluation.verdict] || { text: evaluation.verdict || '未知', cls: 'warn' };
      const row = (k, v) => '<tr><td>' + k + '</td><td>' + (v === null || v === undefined || v === '' ? '—' : v) + '</td></tr>';
      let html = '<div class="bt-eval ' + m.cls + '"><div class="bt-eval-hd">样本内外对照 · ' + m.text + '</div><table>';
      html += row('样本内夏普', evaluation.is_sharpe);
      html += row('样本外夏普', evaluation.oos_sharpe);
      html += row('IS/OOS 夏普比', evaluation.sharpe_ratio_io);
      html += row('切分日', evaluation.split_date);
      html += '</table>';
      if (evaluation.verdict === 'overfit_suspect') {
        html += '<div class="bt-eval-note">IS/OOS 夏普比超过 1.3，按业务规则不得当选最优。</div>';
      }
      if (evaluation.verdict === 'insufficient_sample') {
        html += '<div class="bt-eval-note">样本不足 10 笔，无法区分策略有效与运气。</div>';
      }
      if (evaluation.error_message) {
        html += '<div class="bt-eval-note">' + evaluation.error_message + '</div>';
      }
      html += '</div>';
      el.innerHTML = html;
      el.style.display = 'block';
    }

    _scheduleEvalRefresh(runId, attempt) {
      if (attempt >= 5) {
        // 5 次仍拿不到评判：这次 run 不在自动评判路径上（批量/多策略/研发路径不自动补跑，见设计文档 §7 偏差 1）
        const box = document.getElementById('btEval');
        const note = box ? box.querySelector('.bt-eval-note') : null;
        if (note) note.textContent = '（本次未自动评判：只有手工回测会自动补跑，可点「重跑一次」再评）';
        return;
      }
      setTimeout(async () => {
        try {
          const data = await window.StockRadar.api.getBacktestRunDetail(runId);
          if (data && data.eval) {
            this._lastEval = data.eval;
            this._renderEvalBlock(data.eval, runId);
          } else {
            this._scheduleEvalRefresh(runId, attempt + 1);
          }
        } catch (e) { /* 静默：评判是后台补跑，失败不影响主结果 */ }
      }, 6000);
    }

    async _healGap(runId, gapIndex, btn) {
      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = '正在建补数任务…';
      try {
        const res = await fetch('/api/data/from-gap', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ run_id: runId, gap_index: gapIndex })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        btn.textContent = '补数中…（' + (data.days || 0) + ' 天窗口）';
        let done = false;
        for (let i = 0; i < 60; i++) {
          await new Promise(r => setTimeout(r, 2000));
          const r2 = await fetch('/api/data/tasks/' + encodeURIComponent(data.task_id));
          if (!r2.ok) continue;
          const task = await r2.json();
          const st = String((task && task.status) || '');
          if (st === 'completed' || st === 'done') { done = true; break; }
          if (st === 'failed' || st === 'cancelled') throw new Error(task.error_message || ('任务状态 ' + st));
        }
        btn.disabled = false;
        if (done) {
          btn.textContent = '补完，重跑一次';
          btn.dataset.gapStage = 'rerun';
        } else {
          btn.textContent = '补数还在跑，稍后再点一次';
        }
      } catch (e) {
        btn.disabled = false;
        btn.textContent = label;
        alert('补数失败：' + ((e && e.message) || e));
      }
    }

    async _rerunAfterHeal(runId, btn) {
      btn.disabled = true;
      btn.textContent = '正在重跑…';
      try {
        const res = await fetch('/api/backtest/runs/' + encodeURIComponent(runId) + '/rerun', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'data_gap_healed' })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        btn.textContent = '已重跑：' + (data.run_id || '');
      } catch (e) {
        btn.disabled = false;
        btn.textContent = '重跑失败，再点一次';
        alert('重跑失败：' + ((e && e.message) || e));
      }
    }

    _showResult(r, config) {
      // 空态占位（.bt-empty 是 height:100% 的整屏留白）与进行态都在这里收口：
      // 历史查看 / 批量钻取路径过去只切 tab 不隐藏它们，结果上方会留一大片空白。
      const emptyEl = document.getElementById('btEmpty');
      if (emptyEl) emptyEl.style.display = 'none';
      document.getElementById('btRunning').classList.remove('visible');
      document.getElementById('btResult').style.display = 'block';
      const s = this.strategies.find(s => s.strategy_id === this.selectedStrategy);
      const adjLabel = config.adjust_type === 'qfq' ? '前复权' : config.adjust_type === 'hfq' ? '后复权' : '不复权';
      const assetType = config.asset_type || 'stock';
      const assetLabel = { stock: '个股', etf: 'ETF', index: '指数', board: '板块' }[assetType] || '个股';

      // 结果头部 — 对齐原型
      const header = document.getElementById('btResultHeader');
      if (header) {
        const backBtn = this.currentBatchId
          ? `<button onclick="window._btPanel?.switchTab(document.querySelector('.bt-tab[data-btab=&quot;btBatchPanel&quot;]'))">← 返回批量对比</button>`
          : '';
        header.innerHTML = `
          <div>
            <h2>${config.code}${r.stock_name ? ' ' + r.stock_name : ''} · ${s?.name || this.selectedStrategy} <span style="font-size:12px;padding:2px 8px;border-radius:4px;background:var(--border-light);margin-left:8px;">${assetLabel}</span></h2>
            <div class="subtitle">${config.start_date} ~ ${config.end_date} · 初始资金 ¥${config.initial_cash.toLocaleString()} · ${adjLabel}</div>
          </div>
          <div class="result-actions">
            ${backBtn}
            <button onclick="window._btPanel._runBacktest()">↻ 重新运行</button>
          </div>`;
      }

      this._renderHealthBadge(r);
      this._renderGapBlock(this.currentRunId, r);
      this._renderEvalBlock(this._lastEval, this.currentRunId);

      // 警告信息
      const warnEl = document.getElementById('btWarnings');
      if (warnEl && r.warnings && r.warnings.length > 0) {
        warnEl.innerHTML = r.warnings.map(w =>
          `<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--warning-bg);border:1px solid var(--warning);border-radius:var(--radius);margin-bottom:8px;font-size:12px;color:#92400e;">
            <span>⚠️</span><span>${w}</span>
          </div>`
        ).join('');
        warnEl.style.display = 'block';
      } else if (warnEl) {
        warnEl.style.display = 'none';
      }

      // 指标卡片 — 对齐原型 12 个 + 基准/中性带宽
      const metrics = document.getElementById('btMetrics');
      if (metrics) {
        const benchmarkHtml = r.benchmark_return != null ? `
          <div class="bt-metric"><div class="label">基准收益(沪深300)</div><div class="value ${r.benchmark_return >= 0 ? 'positive' : 'negative'}">${(r.benchmark_return * 100).toFixed(2)}%</div></div>
          <div class="bt-metric"><div class="label">超额收益</div><div class="value ${r.excess_return >= 0 ? 'positive' : 'negative'}">${(r.excess_return * 100).toFixed(2)}%</div></div>` : '';
        const neutralHtml = r.neutral_count > 0 ? `
          <div class="bt-metric"><div class="label">中性交易</div><div class="value neutral">${r.neutral_count} 笔</div></div>` : '';
        // P1：多重检验校正夏普（DSR>95% 视为统计显著；N = 试过的次数）
        const dsrHtml = r.deflated_sharpe != null ? `
          <div class="bt-metric" title="试了 N 次后的多重检验校正：夏普非「运气所致」的概率（>95% 视为显著）"><div class="label">校正夏普(DSR)</div><div class="value ${r.deflated_sharpe >= 0.95 ? 'positive' : 'neutral'}">${(r.deflated_sharpe * 100).toFixed(1)}%<span style="font-size:10px;color:var(--muted);margin-left:4px;">N=${r.trial_count || 1}</span></div></div>` : '';
        metrics.innerHTML = `
          <div class="bt-metric"><div class="label">总收益率</div><div class="value ${r.total_return >= 0 ? 'positive' : 'negative'}">${(r.total_return * 100).toFixed(2)}%</div></div>
          <div class="bt-metric"><div class="label">年化收益率</div><div class="value ${r.annual_return >= 0 ? 'positive' : 'negative'}">${(r.annual_return * 100).toFixed(2)}%</div></div>
          <div class="bt-metric"><div class="label">夏普比率</div><div class="value neutral">${r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(2) : '—'}</div></div>
          <div class="bt-metric"><div class="label">最大回撤</div><div class="value negative">${((r.max_drawdown || 0) * 100).toFixed(2)}%</div></div>
          <div class="bt-metric"><div class="label">Calmar 比率</div><div class="value neutral">${(r.calmar_ratio || 0).toFixed(2)}</div></div>
          <div class="bt-metric"><div class="label">胜率</div><div class="value neutral">${r.win_rate == null ? '—' : (r.win_rate * 100).toFixed(1) + '%'}</div></div>
          <div class="bt-metric"><div class="label">盈亏比</div><div class="value neutral">${(r.profit_factor || 0).toFixed(2)}</div></div>
          <div class="bt-metric"><div class="label">交易次数</div><div class="value neutral">${r.trade_count || 0}</div></div>
          <div class="bt-metric"><div class="label">最大连续盈利</div><div class="value positive">${r.max_consecutive_wins || 0} 次</div></div>
          <div class="bt-metric"><div class="label">最大连续亏损</div><div class="value negative">${r.max_consecutive_losses || 0} 次</div></div>
          <div class="bt-metric"><div class="label">平均持仓天数</div><div class="value neutral">${r.avg_holding_days || 0} 天</div></div>
          <div class="bt-metric"><div class="label">最终资产</div><div class="value positive">¥${(r.final_equity || 0).toLocaleString()}</div></div>
          ${benchmarkHtml}${neutralHtml}${dsrHtml}`;
      }

      // 收益归因瀑布：解释"总收益率"这个数字是怎么来的，放在净值曲线之前
      this._renderWaterfall(r, config);
      // 净值曲线（含基准对比）
      if (r.equity_curve?.length > 0) this._renderEquityChart(r.equity_curve, r);
      // 回撤曲线
      if (r.drawdown_curve?.length > 0) this._renderDrawdownChart(r.drawdown_curve);
      // 月度热力图（A 股惯例：涨红跌绿）
      if (r.monthly_returns) this._renderMonthlyHeatmap(r.monthly_returns);
      // 分年拆解（P1）
      if (r.yearly_stats) this._renderYearlyTable(r.yearly_stats);
      // 交易记录
      if (r.trades?.length > 0) this._renderTrades(r.trades);
      // 行情 K 线 + 买卖点
      if (this.currentRunId) this._renderKlineChart(this.currentRunId).catch(e => console.error('K线渲染异常:', e));
    }

    // ── 全屏切换 ──
    _toggleFullscreen(chartBoxId) {
      const box = document.getElementById(chartBoxId);
      if (!box) return;
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        box.requestFullscreen().catch(() => {});
        // 全屏后延迟 resize echarts（等待 CSS transition 完成）
        setTimeout(() => {
          box.querySelectorAll('.bt-chart').forEach(el => {
            const instance = echarts.getInstanceByDom(el);
            if (instance) instance.resize();
          });
        }, 200);
      }
    }

    _onFullscreenChange(chartBoxId) {
      const box = document.getElementById(chartBoxId);
      if (!box) return;
      if (document.fullscreenElement === box) {
        box.classList.add('bt-fullscreen');
      } else {
        box.classList.remove('bt-fullscreen');
      }
    }

    _renderEquityChart(curve, result) {
      const el = document.getElementById('btEquityChart');
      if (!el || typeof echarts === 'undefined') return;
      const chart = echarts.init(el);

      // 策略净值系列
      const series = [{
        name: '策略净值', type: 'line', data: curve.map(d => d.equity), smooth: true, symbol: 'none',
        lineStyle: { color: '#176b5b', width: 2 },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(23,107,91,0.15)' }, { offset: 1, color: 'rgba(23,107,91,0.01)' }] } },
      }];

      // 基准净值系列（如果有基准数据）
      const legendData = ['策略净值'];
      if (result?.benchmark_return != null && curve.length > 0) {
        // 计算基准净值曲线（假设初始值与策略相同）
        const initialEquity = curve[0]?.equity || 100000;
        const benchmarkReturn = result.benchmark_return;
        // 简化：用线性插值生成基准曲线
        const benchmarkCurve = curve.map((d, i) => {
          const pct = i / (curve.length - 1 || 1);
          return Math.round(initialEquity * (1 + benchmarkReturn * pct) * 100) / 100;
        });
        series.push({
          name: '基准(沪深300)', type: 'line', data: benchmarkCurve, smooth: true, symbol: 'none',
          lineStyle: { color: '#9ca3af', width: 1.5, type: 'dashed' },
        });
        legendData.push('基准(沪深300)');
      }

      chart.setOption({
        tooltip: { trigger: 'axis', backgroundColor: '#fff', borderColor: '#e5e7eb', textStyle: { color: '#1a1a1a', fontSize: 12 } },
        legend: { data: legendData, top: 0, right: 0, textStyle: { fontSize: 11 } },
        grid: { left: 60, right: 20, top: 35, bottom: 25 },
        xAxis: { type: 'category', data: curve.map(d => d.date), axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#9ca3af', fontSize: 10 }, splitLine: { show: false } },
        yAxis: { type: 'value', axisLine: { show: false }, axisLabel: { color: '#9ca3af', fontSize: 10, formatter: v => '¥' + (v / 1000).toFixed(0) + 'k' }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
        series,
      });
      window.addEventListener('resize', () => chart.resize());
    }

    // ── 收益归因瀑布（借 lieflat F9 的语法）──
    // 从「按理想价成交」一路走到「净收益」，每段是一笔可归因的损耗。
    // 跳数锚定在净值收益上反推，四段必然闭合：
    //   净值 + 费用 = 成交收益；成交收益 + 滑点 = 理想价收益
    _renderWaterfall(r, config) {
      const old = document.getElementById('btWaterfallBox');
      if (old) old.remove();
      const trades = r.trades || [];
      if (!trades.length) return;
      const cash = (config && config.initial_cash) || r.initial_cash || 0;
      if (!cash) return;

      let feeAbs = 0, slipAbs = 0;
      trades.forEach((t) => {
        feeAbs += Number(t.commission) || 0;
        slipAbs += Number(t.slippage_cost) || 0;
      });
      const net = Number(r.total_return) || 0;
      const fee = feeAbs / cash;
      const slip = slipAbs / cash;
      // 两段损耗都接近 0 时没有可归因的东西，不画
      if (fee < 0.0005 && slip < 0.0005) return;

      const trade = net + fee;
      const ideal = trade + slip;
      const steps = [
        { label: '理想价收益', v: ideal, kind: 'total' },
        { label: '滑点', v: -slip, kind: 'delta' },
        { label: '成交收益', v: trade, kind: 'subtotal' },
        { label: '交易费用', v: -fee, kind: 'delta' },
        { label: '净收益', v: net, kind: 'total' },
      ];

      const W = 900, H = 300, L = 72, R = 24, TOP = 30, BASE = 238;
      const vals = steps.map(s => s.v);
      const hi = Math.max(...vals) * 1.18, lo = Math.min(0, ...vals) * 1.15;
      const Y = v => BASE - (v - lo) / (hi - lo) * (BASE - TOP);
      const slot = (W - L - R) / steps.length;
      const BW = Math.min(72, slot * 0.5);
      const INK = '#1C1C1A', MUTED = '#8F8E88', UP = '#E74C3C', DOWN = '#176B5B';

      let s = '';
      const gstep = (hi - lo) > 0.2 ? 0.05 : 0.02;
      for (let g = Math.ceil(lo / gstep) * gstep; g <= hi + 1e-9; g += gstep) {
        const isZero = Math.abs(g) < 1e-9;
        s += `<line x1="${L - 16}" y1="${Y(g)}" x2="${W - R}" y2="${Y(g)}"`
          + ` stroke="${isZero ? '#B0AFA9' : '#F2F3F0'}" stroke-width="${isZero ? 1.3 : 1}"/>`;
        s += `<text x="${L - 22}" y="${Y(g) + 3.5}" font-size="8.5"`
          + ` fill="${isZero ? '#6B7280' : MUTED}" text-anchor="end">${(g * 100).toFixed(0)}%</text>`;
      }
      let prev = 0;
      steps.forEach((st, i) => {
        const v = st.v;
        const cx = L + slot * i + slot / 2;
        const x = cx - BW / 2;
        const y0 = st.kind === 'delta' ? Y(prev) : Y(0);
        const y1 = st.kind === 'delta' ? Y(prev + v) : Y(v);
        const top = Math.min(y0, y1), h = Math.max(2, Math.abs(y1 - y0));
        const fill = st.kind === 'delta' ? (v >= 0 ? UP : DOWN) : (st.kind === 'total' ? INK : MUTED);
        s += `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${BW}" height="${h.toFixed(1)}"`
          + ` rx="3" fill="${fill}"/>`;
        const labY = (st.kind === 'delta' ? (v >= 0 ? top - 6 : top + h + 12)
          : (v >= 0 ? top - 6 : top + h + 12));
        s += `<text x="${cx}" y="${labY.toFixed(1)}" font-size="10" font-weight="700"`
          + ` fill="${st.kind === 'delta' ? fill : INK}" text-anchor="middle">`
          + `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%</text>`;
        s += `<text x="${cx}" y="${BASE + 20}" font-size="9.5" fill="#4B5563"`
          + ` text-anchor="middle">${st.label}</text>`;
        if (i < steps.length - 1) {
          const nx = L + slot * (i + 1) + slot / 2 - BW / 2;
          const yLink = st.kind === 'delta' ? Y(prev + v) : Y(v);
          s += `<line x1="${(x + BW).toFixed(1)}" y1="${yLink.toFixed(1)}" x2="${nx}"`
            + ` y2="${yLink.toFixed(1)}" stroke="#D8D7D1" stroke-width="1" stroke-dasharray="2 3"/>`;
        }
        prev = st.kind === 'delta' ? prev + v : v;
      });

      // 佣金口径体检：按配置费率重算一遍，比对记录值。
      // 修复前（backtrader 把万三又除以 100）记录值会明显偏低。
      const cfg = config || {};
      const cRate = Number(cfg.commission) || 0.0003;
      const minComm = Number(cfg.min_commission) || 5;
      const sRate = Number(cfg.stamp_tax) || 0.0005;
      const tRate = Number(cfg.transfer_fee) || 0.00001;
      let feeFixed = 0;
      trades.forEach((t) => {
        const amt = Number(t.amount) || 0;
        let c = Math.max(amt * cRate, minComm) + amt * tRate;
        if (t.direction === 'sell') c += amt * sRate;
        feeFixed += c;
      });
      const stale = feeFixed > 0 && feeAbs < feeFixed * 0.7;
      const note = stale
        ? `<span style="margin-left:10px;font-size:10.5px;color:#B45309;`
          + `background:#FBF7EC;border:1px solid #EFE3C4;border-radius:99px;padding:1px 8px">`
          + `佣金口径待重跑：记录 ${feeAbs.toFixed(0)} 元，按费率应为 ${feeFixed.toFixed(0)} 元</span>`
        : '';
      // 笔数用 trade_count（回合数）：trades.length 是买卖单数，和指标卡的"交易次数"对不上
      const sub = [r.stock_name, (config && config.strategy_name) || '',
        `${r.trade_count || trades.length} 笔`].filter(Boolean).join(' · ');

      const box = document.createElement('div');
      box.className = 'bt-chart-box';
      box.id = 'btWaterfallBox';
      box.innerHTML =
        `<div style="display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;margin-bottom:2px">`
        + `<h3 style="margin:0;font-size:14px">收益归因 · 钱被什么吃掉了</h3>`
        + `<span style="font-size:11px;color:#8a8b86">${sub}</span>${note}</div>`
        + `<div style="font-size:11px;color:#8a8b86;margin:2px 0 8px">`
        + `净值 ${(net * 100).toFixed(2)}% = 理想价收益 − 滑点 − 交易费用。`
        + `滑点已体现在成交价里，费用含佣金与印花税。</div>`
        + `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${s}</svg>`
        + `<div style="font-size:10px;color:#9a9b96;letter-spacing:.05em;margin-top:2px">`
        + `BLACK = 求和项 · GREY = 中间态 · GREEN = 被吃掉的（负向损耗）</div>`;

      const eqBox = document.getElementById('btEquityChartBox');
      if (eqBox && eqBox.parentElement) {
        eqBox.parentElement.insertBefore(box, eqBox);
      }
    }

    _renderDrawdownChart(curve) {
      const el = document.getElementById('btDrawdownChart');
      if (!el || typeof echarts === 'undefined') return;
      const chart = echarts.init(el);
      chart.setOption({
        tooltip: { trigger: 'axis', backgroundColor: '#fff', borderColor: '#e5e7eb', textStyle: { fontSize: 12 }, formatter: p => `${p[0].axisValue}<br/>回撤: ${p[0].value}%` },
        grid: { left: 50, right: 20, top: 10, bottom: 25 },
        xAxis: { type: 'category', data: curve.map(d => d.date), axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#9ca3af', fontSize: 10 }, splitLine: { show: false } },
        yAxis: { type: 'value', max: 0, axisLine: { show: false }, axisLabel: { color: '#9ca3af', fontSize: 10, formatter: v => v + '%' }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
        series: [{
          type: 'line', data: curve.map(d => d.drawdown), smooth: true, symbol: 'none',
          lineStyle: { color: '#e74c3c', width: 1.5 },
          areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(231,76,60,0.3)' }, { offset: 1, color: 'rgba(231,76,60,0.02)' }] } },
        }],
      });
      window.addEventListener('resize', () => chart.resize());
    }

    _renderMonthlyHeatmap(monthly) {
      const el = document.getElementById('btMonthlyHeatmap');
      if (!el) return;
      const months = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
      const years = Object.keys(monthly).sort();
      let html = '<div style="display:flex;gap:4px;align-items:flex-start;">';
      html += '<div style="display:flex;flex-direction:column;gap:2px;"><div style="height:20px;"></div>';
      years.forEach(y => { html += `<div style="height:34px;display:flex;align-items:center;font-size:11px;font-weight:600;color:var(--text-secondary);">${y}</div>`; });
      html += '</div>';
      months.forEach((m, mi) => {
        html += `<div style="display:flex;flex-direction:column;gap:2px;"><div style="height:20px;font-size:10px;color:var(--muted);text-align:center;">${m}</div>`;
        years.forEach(y => {
          const v = (monthly[y] || [0,0,0,0,0,0,0,0,0,0,0,0])[mi] || 0;
          let bg, color;
          // A 股惯例：涨红跌绿（对齐 --up 红 / --down 绿 项目配色变量）
          if (v === 0) { bg = '#f3f4f6'; color = '#9ca3af'; }
          else if (v > 0) { bg = `rgba(231,76,60,${Math.min(v / 8, 0.9)})`; color = v > 4 ? '#fff' : '#e74c3c'; }
          else { bg = `rgba(23,107,91,${Math.min(Math.abs(v) / 4, 0.9)})`; color = v < -2 ? '#fff' : '#176b5b'; }
          html += `<div class="heatmap-cell" style="width:48px;height:32px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;font-family:var(--font-mono);background:${bg};color:${color};">${v > 0 ? '+' : ''}${v === 0 ? '—' : v.toFixed(1) + '%'}</div>`;
        });
        html += '</div>';
      });
      html += '</div>';
      el.innerHTML = html;
    }

    _renderYearlyTable(yearly) {
      // P1 分年拆解：年份 | 收益 | 夏普 | 最大回撤 | 交易
      const el = document.getElementById('btYearlyTable');
      if (!el) return;
      const years = Object.keys(yearly || {}).sort().reverse();
      if (!years.length) {
        el.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px;">无分年数据（区间不足或数据缺失）</div>';
        return;
      }
      const rows = years.map(y => {
        const d = yearly[y] || {};
        const ret = d.ret;
        const retCls = ret == null ? '' : (ret >= 0 ? 'profit' : 'loss');
        const retTxt = ret == null ? '—' : (ret >= 0 ? '+' : '') + Number(ret).toFixed(2) + '%';
        const sh = d.sharpe == null ? '—' : Number(d.sharpe).toFixed(2);
        const mdd = d.mdd == null ? '—' : '-' + Number(d.mdd).toFixed(2) + '%';
        const tr = (d.trades == null) ? '—' : d.trades;
        return `<tr><td class="mono">${y}</td><td class="mono ${retCls}">${retTxt}</td>` +
               `<td class="mono">${sh}</td><td class="mono loss">${mdd}</td><td class="mono">${tr}</td></tr>`;
      }).join('');
      el.innerHTML = `<table class="trades-table"><thead><tr>
        <th>年份</th><th>收益</th><th>夏普</th><th>最大回撤</th><th>交易</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
    }

    _renderTrades(trades) {
      const tbody = document.getElementById('btTradesBody');
      if (!tbody) return;
      tbody.innerHTML = trades.map(t => `
        <tr>
          <td>${t.trade_no}</td>
          <td style="color:${t.direction === 'buy' ? 'var(--up)' : 'var(--down)'};font-weight:600">${t.direction === 'buy' ? '买入' : '卖出'}</td>
          <td class="mono">${t.trade_date}</td>
          <td class="mono">${t.price?.toFixed(2)}</td>
          <td class="mono">${t.quantity}</td>
          <td class="mono">¥${t.amount?.toLocaleString()}</td>
          <td class="mono">${t.commission?.toFixed(2)}</td>
          <td class="${t.pnl >= 0 ? 'profit' : 'loss'}">${t.pnl != null ? (t.pnl >= 0 ? '+' : '') + t.pnl.toFixed(2) : '—'}</td>
          <td class="${t.pnl_pct >= 0 ? 'profit' : 'loss'}">${t.pnl_pct != null ? (t.pnl_pct >= 0 ? '+' : '') + t.pnl_pct.toFixed(2) + '%' : '—'}</td>
        </tr>`).join('');
    }

    async _renderKlineChart(runId) {
      const el = document.getElementById('btKlineChart');
      if (!el) return;
      try {
        const data = await window.StockRadar.api.getBacktestKline(runId);
        window.StockRadar.renderBacktestKline(el, data);
      } catch (e) {
        console.error('K 线加载失败:', e);
        el.innerHTML = '<div style="padding:40px;text-align:center;color:#9ca3af;font-size:13px;">K 线加载失败</div>';
      }
    }

    // ========== SSE 进度 + 结果获取 ==========
    async _pollResult(runId, maxWait = 120000) {
      // 先尝试 SSE 进度推送
      try {
        await this._streamProgress(runId);
      } catch (e) {
        // SSE 失败则降级为轮询
        console.warn('SSE 进度推送失败，降级为轮询:', e);
      }

      // 获取最终结果
      const data = await window.StockRadar.api.getBacktestRunDetail(runId);
      this._lastEval = data.eval || null;
      const status = data.run?.status;
      if (status === 'completed') return this._normalizeResult(data);
      if (status === 'failed') throw new Error(data.run?.error_message || '回测失败');
      throw new Error('回测超时');
    }

    _streamProgress(runId) {
      return new Promise((resolve, reject) => {
        const progressEl = document.getElementById('btProgress');
        const progressText = document.getElementById('btProgressText');
        const progressBar = document.getElementById('btProgressBar');

        const evtSource = new EventSource(`/api/backtest/runs/${runId}/progress`);
        const timeout = setTimeout(() => {
          evtSource.close();
          resolve(); // 超时不 reject，让后续获取结果
        }, 120000);

        evtSource.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.error) {
              evtSource.close();
              clearTimeout(timeout);
              resolve();
              return;
            }

            // 更新进度条
            const pct = Math.round((data.pct || 0) * 100);
            if (progressBar) progressBar.style.width = pct + '%';
            if (progressText) {
              if (data.total > 0) {
                progressText.textContent = `处理 ${data.current}/${data.total} 条K线 (${pct}%)`;
              } else {
                progressText.textContent = `运行中... ${pct}%`;
              }
            }

            if (data.status === 'completed' || data.status === 'failed') {
              evtSource.close();
              clearTimeout(timeout);
              resolve();
            }
          } catch (e) { /* ignore parse errors */ }
        };

        evtSource.onerror = () => {
          evtSource.close();
          clearTimeout(timeout);
          resolve(); // 不 reject，降级为轮询
        };
      });
    }

    _normalizeResult(data) {
      // API 返回 equity_curve_json / drawdown_curve_json / monthly_returns_json
      // 前端期望 equity_curve / drawdown_curve / monthly_returns
      const r = data.result || {};
      return {
        ...r,
        run_id: data.run?.run_id,
        stock_name: data.run?.stock_name || '',
        trades: data.trades || [],
        equity_curve: r.equity_curve || r.equity_curve_json || [],
        drawdown_curve: r.drawdown_curve || r.drawdown_curve_json || [],
        monthly_returns: r.monthly_returns || r.monthly_returns_json || {},
        yearly_stats: r.yearly_stats || r.yearly_stats_json || null,
      };
    }

    // ========== Tab 切换 ==========
    switchTab(btn) {
      if (!btn) return;
      document.querySelectorAll('.bt-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.bt-tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = document.getElementById(btn.dataset.btab);
      if (panel) panel.classList.add('active');
      if (btn.dataset.btab === 'btHistoryPanel') this.loadHistory();
    }

    // ========== 回测历史 ==========
    async loadHistory(which = 'all') {
      try {
        const ps = this._historyPageSize;
        // 按需并行加载（翻页时只刷新对应表，避免全刷）
        const taskMap = {};
        if (which === 'all' || which === 'runs') {
          taskMap.runs = window.StockRadar.api.getBacktestRuns(null, null, this._historyPage, ps);
        }
        if (which === 'all' || which === 'batches') {
          taskMap.batches = window.StockRadar.api.listBatches(this._batchPage, ps).catch(() => ({ batches: [], total: 0 }));
        }
        if (which === 'all' || which === 'multis') {
          taskMap.multis = window.StockRadar.api.listMulti(this._multiPage, ps).catch(() => ({ multis: [], total: 0 }));
        }
        const keys = Object.keys(taskMap);
        const results = await Promise.all(keys.map(k => taskMap[k]));
        const data = {};
        keys.forEach((k, i) => { data[k] = results[i]; });

        const tbody = document.getElementById('btHistoryBody');
        const batchTbody = document.getElementById('btBatchHistoryBody');
        const multiTbody = document.getElementById('btMultiHistoryBody');

        // ── 单股回测 ──
        if (data.runs) {
          const runs = data.runs.runs || [];
          const runsTotal = data.runs.total || 0;
          if (tbody) {
            if (runs.length === 0) {
              tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:20px;">暂无单股回测记录</td></tr>';
            } else {
              tbody.innerHTML = runs.map(r => {
                const statusCls = r.status === 'completed' ? 'completed' : r.status === 'failed' ? 'failed' : r.status === 'running' ? 'running' : 'pending';
                const statusLabel = { completed: '已完成', failed: '失败', running: '运行中', pending: '等待中' }[r.status] || r.status;
                const ret = r.total_return != null ? (r.total_return * 100).toFixed(2) + '%' : '—';
                const retCls = r.total_return > 0 ? 'profit' : r.total_return < 0 ? 'loss' : '';
                const sharpe = r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(2) : '—';
                const dd = r.max_drawdown != null ? (r.max_drawdown * 100).toFixed(2) + '%' : '—';
                const trades = r.trade_count != null ? r.trade_count : '—';
                let assetType = 'stock';
                try { if (r.config_json) { const cfg = typeof r.config_json === 'string' ? JSON.parse(r.config_json) : r.config_json; assetType = cfg.asset_type || 'stock'; } } catch(e) {}
                const assetLabel = { stock: '个股', etf: 'ETF', index: '指数', board: '板块' }[assetType] || '个股';
                const assetBadge = { stock: '', etf: 'style="color:#7c3aed"', index: 'style="color:#0369a1"', board: 'style="color:#b45309"' }[assetType] || '';
                return `<tr>
                  <td><code style="font-family:var(--font-mono);font-size:11px">${r.run_id}</code>${this._originBadge(r.origin)}</td>
                  <td>${r.stock_name || r.code} <span style="font-size:10px;padding:1px 5px;border-radius:4px;background:var(--border-light);margin-left:4px;" ${assetBadge}>${assetLabel}</span></td>
                  <td>${r.strategy_name || r.strategy_id}</td>
                  <td><span class="dc-badge ${statusCls}"><span class="dot"></span>${statusLabel}</span></td>
                  <td class="${retCls}" style="font-family:var(--font-mono)">${ret}</td>
                  <td style="font-family:var(--font-mono)">${sharpe}</td>
                  <td style="font-family:var(--font-mono)">${dd}</td>
                  <td style="font-family:var(--font-mono)">${trades}</td>
                  <td style="font-size:11px;color:var(--muted)">${r.created_at || ''}</td>
                  <td>
                ${r.status === 'completed' ? `<button class="dc-btn dc-btn-sm" onclick="_btPanel.viewRun('${r.run_id}')">查看</button>` : ''}
                ${this.strategies.some(s => s.strategy_id === r.strategy_id) ? `<button class="dc-btn dc-btn-sm" title="打开这个策略的详情：回测记录 / 评判 / 前瞻 / 落地" onclick="_btPanel.openLifecycle('${this._esc(r.strategy_id)}')">策略详情</button>` : ''}
                ${r.origin === 'research' && r.ref_id ? `<button class="dc-btn dc-btn-sm" title="看这一次研发的每一轮想法与改动" onclick="_btPanel.openResearchRun('${this._esc(r.ref_id)}')">研发过程</button>` : ''}
                ${r.status === 'failed' ? `<button class="dc-btn dc-btn-sm" style="color:#dc2626;border-color:#dc2626;" onclick="_btPanel._deleteFailedItem('run','${r.run_id}')">删除</button>` : ''}
              </td>
                </tr>`;
              }).join('');
            }
          }
          this._renderPagination('btHistoryPagination', {
            page: this._historyPage, pageSize: ps, total: runsTotal,
            onChange: (p) => { this._historyPage = p; this.loadHistory('runs'); }
          });
        }

        // ── 批量回测 ──
        if (data.batches && batchTbody) {
          const batches = data.batches.batches || [];
          const batchesTotal = data.batches.total || 0;
          if (batches.length === 0) {
            batchTbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:20px;">暂无批量回测记录</td></tr>';
          } else {
            batchTbody.innerHTML = batches.map(b => {
              const statusCls = b.status === 'completed' ? 'completed' : b.status === 'failed' ? 'failed' : b.status === 'running' ? 'running' : 'pending';
              const statusLabel = { completed: '已完成', failed: '失败', running: '运行中', pending: '等待中', partial: '部分完成' }[b.status] || b.status;
              const avgRet = b.avg_total_return != null ? (b.avg_total_return * 100).toFixed(2) + '%' : '—';
              const retCls = b.avg_total_return > 0 ? 'profit' : b.avg_total_return < 0 ? 'loss' : '';
              const avgSharpe = b.avg_sharpe != null ? b.avg_sharpe.toFixed(2) : '—';
              const avgDD = b.avg_max_drawdown != null ? (b.avg_max_drawdown * 100).toFixed(2) + '%' : '—';
              return `<tr>
                <td><code style="font-family:var(--font-mono);font-size:11px">${b.batch_id}</code></td>
                <td>${b.strategy_name || b.strategy_id}</td>
                <td><span class="dc-badge ${statusCls}"><span class="dot"></span>${statusLabel}</span></td>
                <td class="${retCls}" style="font-family:var(--font-mono)">${avgRet}</td>
                <td style="font-family:var(--font-mono)">${avgSharpe}</td>
                <td style="font-family:var(--font-mono)">${avgDD}</td>
                <td style="font-family:var(--font-mono)">${b.code_count || '—'} 标的</td>
                <td style="font-size:11px;color:var(--muted)">${b.created_at || ''}</td>
                <td>
                  ${b.status === 'completed' || b.status === 'partial' ? `<button class="dc-btn dc-btn-sm" onclick="_btPanel.viewBatch('${b.batch_id}')">查看</button>` : ''}
                  ${b.status === 'failed' ? `<button class="dc-btn dc-btn-sm" style="color:#dc2626;border-color:#dc2626;" onclick="_btPanel._deleteFailedItem('batch','${b.batch_id}')">删除</button>` : ''}
                </td>
              </tr>`;
            }).join('');
          }
          this._renderPagination('btBatchHistoryPagination', {
            page: this._batchPage, pageSize: ps, total: batchesTotal,
            onChange: (p) => { this._batchPage = p; this.loadHistory('batches'); }
          });
        }

        // ── 多策略对比 ──
        if (data.multis && multiTbody) {
          const multis = data.multis.multis || [];
          const multisTotal = data.multis.total || 0;
          if (multis.length === 0) {
            multiTbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px;">暂无策略对比记录</td></tr>';
          } else {
            multiTbody.innerHTML = multis.map(m => {
              const statusCls = m.status === 'completed' ? 'completed' : m.status === 'failed' ? 'failed' : m.status === 'running' ? 'running' : 'pending';
              const statusLabel = { completed: '已完成', failed: '失败', running: '运行中', pending: '等待中', partial: '部分完成' }[m.status] || m.status;
              const sids = (typeof m.strategy_ids_json === 'string' ? JSON.parse(m.strategy_ids_json) : m.strategy_ids_json) || [];
              const cids = (typeof m.codes_json === 'string' ? JSON.parse(m.codes_json) : m.codes_json) || [];
              return `<tr>
                <td><code style="font-family:var(--font-mono);font-size:11px">${m.multi_id}</code></td>
                <td>${sids.length} 策略</td>
                <td>${cids.length} 标的</td>
                <td><span class="dc-badge ${statusCls}"><span class="dot"></span>${statusLabel}</span></td>
                <td style="font-size:11px;color:var(--muted)">${m.created_at || ''}</td>
                <td style="font-size:11px;color:var(--muted)">${m.duration_seconds ? m.duration_seconds + 's' : '—'}</td>
                <td>
                  ${m.status === 'completed' || m.status === 'partial' ? `<button class="dc-btn dc-btn-sm" onclick="_btPanel.viewMulti('${m.multi_id}')">查看</button>` : ''}
                  ${m.status === 'failed' ? `<button class="dc-btn dc-btn-sm" style="color:#dc2626;border-color:#dc2626;" onclick="_btPanel._deleteFailedItem('multi','${m.multi_id}')">删除</button>` : ''}
                </td>
              </tr>`;
            }).join('');
          }
          this._renderPagination('btMultiHistoryPagination', {
            page: this._multiPage, pageSize: ps, total: multisTotal,
            onChange: (p) => { this._multiPage = p; this.loadHistory('multis'); }
          });
        }
      } catch (e) { console.error('加载历史失败:', e); }
    }

    /** 通用分页渲染（三个历史表共用） */
    _renderPagination(containerId, { page, pageSize, total, onChange }) {
      const container = document.getElementById(containerId);
      if (!container) return;
      if (!total || total <= 0) { container.innerHTML = ''; return; }
      const totalPages = Math.max(1, Math.ceil(total / pageSize));
      const prevDisabled = page <= 1 ? 'disabled' : '';
      const nextDisabled = page >= totalPages ? 'disabled' : '';
      container.innerHTML = `
        <div class="bt-pagination">
          <button class="dc-btn dc-btn-sm" ${prevDisabled} data-page="${page - 1}">上一页</button>
          <span style="margin:0 12px;font-size:12px;color:var(--text-secondary);">
            第 ${page} / ${totalPages} 页 · 共 ${total} 条
          </span>
          <button class="dc-btn dc-btn-sm" ${nextDisabled} data-page="${page + 1}">下一页</button>
        </div>`;
      container.querySelectorAll('button[data-page]').forEach(btn => {
        btn.onclick = () => {
          if (btn.disabled) return;
          onChange(parseInt(btn.dataset.page, 10));
        };
      });
    }

    /** 从历史页查看批量回测详情 */
    async viewBatch(batchId) {
      try {
        const detail = await window.StockRadar.api.getBatch(batchId);
        if (!detail || !detail.batch) { alert('批次不存在'); return; }
        const batch = detail.batch;
        // 切到批量对比 tab
        this.switchTab(document.querySelector('.bt-tab[data-btab="btBatchPanel"]'));
        document.getElementById('btBatchEmpty').style.display = 'none';
        // running/pending：显示进度条 + 监听 SSE，完成后渲染最终结果
        if (batch.status === 'running' || batch.status === 'pending') {
          document.getElementById('btBatchRunning').classList.add('visible');
          try {
            await this._streamBatchUntilDone(batchId);
          } catch (e) { /* SSE 失败不阻塞，用当前快照渲染 */ }
          const finalDetail = await window.StockRadar.api.getBatch(batchId);
          document.getElementById('btBatchRunning').classList.remove('visible');
          await this._renderBatchResult(finalDetail);
        } else {
          document.getElementById('btBatchRunning').classList.remove('visible');
          await this._renderBatchResult(detail);
        }
      } catch (e) { alert('加载批次失败: ' + (e.message || e)); }
    }

    async viewRun(runId) {
      try {
        const data = await window.StockRadar.api.getBacktestRunDetail(runId);
        if (data.result) {
          this.switchTab(document.querySelector('.bt-tab[data-btab="btResultPanel"]'));
          const config = JSON.parse(data.run?.config_json || '{}');
          const result = this._normalizeResult(data);
          this.currentRunId = runId;
          this._lastEval = data.eval || null;
          this._showResult(result, config);
        }
      } catch (e) { alert('加载失败: ' + (e.message || e)); }
    }

    // ============================================================
    // 批量结果渲染
    // ============================================================

    async _renderBatchResult(detail) {
      const batch = detail.batch || {};
      const items = detail.items || [];
      this.currentBatchItems = items;

      document.getElementById('btBatchEmpty').style.display = 'none';
      document.getElementById('btBatchResult').style.display = 'block';

      // 1) 头部
      const header = document.getElementById('btBatchHeader');
      const s = this.strategies.find(s => s.strategy_id === batch.strategy_id);
      if (header) {
        header.innerHTML = `
          <div>
            <h2>${s?.name || batch.strategy_name || batch.strategy_id} · 批量对比</h2>
            <div class="subtitle">${batch.start_date} ~ ${batch.end_date} · ${batch.code_count} 个标的 · 初始资金 ¥${(batch.initial_cash || 0).toLocaleString()}</div>
          </div>
          <div class="result-actions">
            <button onclick="window._btPanel?._runBatch(window._btPanel?.currentBatchItems?.map(it=>it.code))">↻ 重跑</button>
          </div>`;
      }

      // 2) 逐个拉取 run 详情，汇总指标
      const enriched = [];
      for (const it of items) {
        if (it.status !== 'completed' || !it.run_id) { enriched.push({ ...it }); continue; }
        try {
          const data = await window.StockRadar.api.getBacktestRunDetail(it.run_id);
          enriched.push({
            ...it,
            name: data.run?.stock_name || it.name || '',
            result: this._normalizeResult(data),
            equity_curve: data.result?.equity_curve || data.result?.equity_curve_json || [],
          });
        } catch (e) {
          enriched.push({ ...it, error: e.message });
        }
      }
      this.currentBatchEnriched = enriched;

      // 3) 综合汇总卡片
      this._renderBatchSummary(enriched, batch);

      // 4) 对比图（由 chart-batch-compare.js 渲染）
      if (window.StockRadar?.BatchCompareChart) {
        window.StockRadar.BatchCompareChart.bindToggles();
        window.StockRadar.BatchCompareChart.render(
          document.getElementById('btBatchEquityChart'),
          enriched,
          {
            initialCash: batch.initial_cash || 100000,
            onLegendToggle: () => this._renderBatchSummary(this.currentBatchEnriched, batch, true),
          }
        );
      }

      // 5) 明细表
      this._renderBatchItemsTable(enriched, batch);
    }

    _renderBatchSummary(enriched, batch, fromLegendToggle = false) {
      const summary = document.getElementById('btBatchSummary');
      if (!summary) return;

      const completed = enriched.filter(e => e.status === 'completed' && e.result);
      const successCount = completed.length;
      const failedCount = enriched.filter(e => e.status === 'failed').length;

      // 统一用 enriched 数据实时计算，不依赖 DB 聚合快照
      const allCodes = enriched.map(e => e.code);
      const rawVisible = (typeof window.StockRadar?.BatchCompareChart?.getVisibleCodes === 'function')
        ? window.StockRadar.BatchCompareChart.getVisibleCodes()
        : allCodes;
      // 容错：过滤掉不在当前 enriched 中的 code（避免旧批次残留）
      const validVisible = rawVisible.filter(c => allCodes.includes(c));
      // 图例切换时用可见标的，初始渲染时用全部（因为 chart 还没 render）
      const visible = (fromLegendToggle && validVisible.length > 0) ? validVisible : allCodes;

      const visibleCompleted = completed.filter(e => visible.includes(e.code));

      const avg = (key) => {
        if (visibleCompleted.length === 0) return 0;
        return visibleCompleted.reduce((s, e) => s + (e.result[key] || 0), 0) / visibleCompleted.length;
      };
      const sum = (key) => visibleCompleted.reduce((s, e) => s + (e.result[key] || 0), 0);

      const avgTotalReturn = avg('total_return');
      const avgAnnualReturn = avg('annual_return');
      const avgSharpe = avg('sharpe_ratio');
      const avgMaxDrawdown = avg('max_drawdown');
      const avgWinRate = avg('win_rate');
      const totalTrades = sum('trade_count');
      const profitCount = visibleCompleted.filter(e => (e.result.total_return || 0) > 0).length;
      const lossCount = visibleCompleted.filter(e => (e.result.total_return || 0) < 0).length;

      summary.innerHTML = `
        <div class="bt-metric aggregate"><div class="label">综合平均收益</div><div class="value ${avgTotalReturn>=0?'positive':'negative'}">${(avgTotalReturn*100).toFixed(2)}%</div><div class="sub" style="font-size:10px;color:var(--muted);margin-top:2px">${visibleCompleted.length} 个可见标的</div></div>
        <div class="bt-metric"><div class="label">综合平均年化</div><div class="value ${avgAnnualReturn>=0?'positive':'negative'}">${(avgAnnualReturn*100).toFixed(2)}%</div></div>
        <div class="bt-metric"><div class="label">综合平均夏普</div><div class="value neutral">${avgSharpe.toFixed(2)}</div></div>
        <div class="bt-metric"><div class="label">综合平均最大回撤</div><div class="value negative">${(avgMaxDrawdown*100).toFixed(2)}%</div></div>
        <div class="bt-metric"><div class="label">综合平均胜率</div><div class="value neutral">${(avgWinRate*100).toFixed(1)}%</div></div>
        <div class="bt-metric"><div class="label">综合总交易次数</div><div class="value neutral">${totalTrades}</div></div>
        <div class="bt-metric"><div class="label">盈利标的</div><div class="value positive">${profitCount} 个</div></div>
        <div class="bt-metric"><div class="label">亏损标的</div><div class="value negative">${lossCount} 个</div></div>
        <div class="bt-metric"><div class="label">参与标的</div><div class="value neutral">${successCount} / ${enriched.length}</div></div>
        ${failedCount > 0 ? `<div class="bt-metric"><div class="label">失败标的</div><div class="value negative">${failedCount} 个</div></div>` : ''}
      `;
    }

    _renderBatchItemsTable(enriched, batch) {
      const tbody = document.getElementById('btBatchItemsBody');
      if (!tbody) return;

      // 明细表标题区：失败时可一键重试所有失败 item（复用原 batch，成功数据保留）
      const actionsEl = document.getElementById('btBatchItemsActions');
      if (actionsEl) {
        const failedCount = enriched.filter(e => e.status === 'failed').length;
        const batchRunning = batch && ['running', 'pending'].includes(batch.status);
        actionsEl.innerHTML = (failedCount > 0 && !batchRunning)
          ? `<button class="dc-btn dc-btn-sm" onclick="window._btPanel?.rerunFailedItems('${batch.batch_id}')">↻ 重试所有失败 (${failedCount})</button>`
          : '';
      }

      if (enriched.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:30px;">无明细数据</td></tr>';
        return;
      }
      tbody.innerHTML = enriched.map(e => {
        const r = e.result || {};
        const assetLabel = { stock: '个股', etf: 'ETF', index: '指数', board: '板块' }[e.asset_type] || e.asset_type || '—';
        const statusCls = e.status === 'completed' ? 'completed' : e.status === 'failed' ? 'failed' : 'running';
        const statusLabel = { completed: '已完成', failed: '失败', pending: '等待', running: '运行中' }[e.status] || e.status;
        const ret = r.total_return != null ? (r.total_return * 100).toFixed(2) + '%' : '—';
        const retCls = (r.total_return || 0) > 0 ? 'profit' : (r.total_return || 0) < 0 ? 'loss' : '';
        const ann = r.annual_return != null ? (r.annual_return * 100).toFixed(2) + '%' : '—';
        const shp = r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(2) : '—';
        const dd = r.max_drawdown != null ? (r.max_drawdown * 100).toFixed(2) + '%' : '—';
        const wr = r.win_rate != null ? (r.win_rate * 100).toFixed(1) + '%' : '—';
        const tc = r.trade_count != null ? r.trade_count : '—';
        const drillBtn = e.status === 'completed' && e.run_id
          ? `<button class="dc-btn dc-btn-sm" onclick="window._btPanel?.drillDown('${e.run_id}', '${this._esc(e.code)}')">钻取</button>`
          : (e.status === 'failed'
            ? `<button class="dc-btn dc-btn-sm" onclick="window._btPanel?.rerunItem('${batch.batch_id}', '${this._esc(e.code)}')">重跑</button>`
            : '');
        const errTip = e.error_message ? ` title="${this._esc(e.error_message)}"` : '';
        return `<tr${errTip}>
          <td><code style="font-family:var(--font-mono);font-size:11px">${this._esc(e.code)}</code></td>
          <td><span style="font-size:10px;padding:1px 5px;border-radius:4px;background:var(--border-light);">${assetLabel}</span></td>
          <td><span class="dc-badge ${statusCls}"><span class="dot"></span>${statusLabel}</span></td>
          <td class="${retCls}" style="font-family:var(--font-mono)">${ret}</td>
          <td style="font-family:var(--font-mono)">${ann}</td>
          <td style="font-family:var(--font-mono)">${shp}</td>
          <td style="font-family:var(--font-mono)">${dd}</td>
          <td style="font-family:var(--font-mono)">${wr}</td>
          <td style="font-family:var(--font-mono)">${tc}</td>
          <td>${drillBtn}</td>
        </tr>`;
      }).join('');
    }

    // ============================================================
    // 多策略批量回测（M 策略 × N 标的）
    // ============================================================

    async _runMulti(strategyIds, codes) {
      const params = {};
      document.querySelectorAll('#btParams input[data-key]').forEach(input => {
        params[input.dataset.key] = parseFloat(input.value) || 0;
      });
      const positionMode = document.getElementById('btPositionMode')?.value || 'full';
      const positionSize = positionMode === 'full' ? 1.0 : (parseFloat(document.getElementById('btPositionSize')?.value) || 50);
      const assetType = document.getElementById('btAssetType')?.value || 'stock';

      const config = {
        strategy_ids: strategyIds,
        codes,
        params,
        start_date: document.getElementById('btStartDate')?.value || '2025-01-01',
        end_date: document.getElementById('btEndDate')?.value || '2026-06-01',
        initial_cash: parseFloat(document.getElementById('btCash')?.value) || 100000,
        commission: parseFloat(document.getElementById('btCommission')?.value) || 0.0003,
        min_commission: parseFloat(document.getElementById('btMinCommission')?.value) || 5,
        stamp_tax: assetType === 'etf' ? 0 : parseFloat(document.getElementById('btStampTax')?.value) || 0.0005,
        transfer_fee: parseFloat(document.getElementById('btTransferFee')?.value) || 0.00001,
        slippage: parseFloat(document.getElementById('btSlippage')?.value) || 0.001,
        adjust_type: document.getElementById('btAdjust')?.value || 'qfq',
        t_plus_1: document.getElementById('btTPlus1')?.checked ?? true,
        price_limit_mode: document.getElementById('btLimitMove')?.checked === false ? 'off' : 'enforce',
        lot_size: assetType === 'board' ? 1 : (document.getElementById('btLotSize100')?.checked ? 100 : 1),
        stop_loss: parseFloat(document.getElementById('btStopLoss')?.value) || 0,
        take_profit: parseFloat(document.getElementById('btTakeProfit')?.value) || 0,
        trailing_stop: parseFloat(document.getElementById('btTrailingStop')?.value) || 0,
        position_mode: positionMode, position_size: positionSize,
        concurrency: 3,
      };

      this.switchTab(document.querySelector('.bt-tab[data-btab="btMultiPanel"]'));
      document.getElementById('btMultiEmpty').style.display = 'none';
      document.getElementById('btMultiResult').style.display = 'none';
      document.getElementById('btMultiRunning').classList.add('visible');
      document.getElementById('btMultiProgressBar').style.width = '0%';
      document.getElementById('btMultiProgressText').textContent = '提交中...';
      document.getElementById('btRunBtn').disabled = true;

      try {
        const res = await window.StockRadar.api.runMultiBacktest(config);
        this.currentMultiId = res.multi_id;
        await this._streamMultiUntilDone(res.multi_id);
        const detail = await window.StockRadar.api.getMulti(res.multi_id);
        await this._renderMultiResult(detail);
      } catch (e) {
        alert('多策略回测失败: ' + (e.message || e));
      } finally {
        document.getElementById('btMultiRunning').classList.remove('visible');
        document.getElementById('btRunBtn').disabled = false;
      }
    }

    _streamMultiUntilDone(multiId) {
      return new Promise((resolve, reject) => {
        const es = window.StockRadar.api.streamMultiProgress(multiId,
          (msg) => {
            if (msg.error) { es.close(); reject(new Error(msg.error)); return; }
            const pct = Math.round((msg.pct || 0) * 100);
            const bar = document.getElementById('btMultiProgressBar');
            const txt = document.getElementById('btMultiProgressText');
            if (bar) bar.style.width = pct + '%';
            if (txt) txt.textContent = `${msg.completed || 0} / ${msg.total || 0} 策略完成 (${pct}%)`;
            if (msg.status === 'completed' || msg.status === 'failed') {
              es.close(); resolve(msg);
            }
          },
          (err) => { es.close(); reject(err); }
        );
      });
    }

    async _renderMultiResult(detail) {
      const multi = detail.multi || {};
      const items = detail.items || [];
      const codeNames = detail.code_names || {};

      document.getElementById('btMultiEmpty').style.display = 'none';
      document.getElementById('btMultiResult').style.display = 'block';

      // 解析 strategy_ids / codes
      const strategyIds = Array.isArray(multi.strategy_ids_json) ? multi.strategy_ids_json :
        (typeof multi.strategy_ids_json === 'string' ? JSON.parse(multi.strategy_ids_json) : []);
      const strategyNames = Array.isArray(multi.strategy_names_json) ? multi.strategy_names_json :
        (typeof multi.strategy_names_json === 'string' ? JSON.parse(multi.strategy_names_json) : []);
      const codes = Array.isArray(multi.codes_json) ? multi.codes_json :
        (typeof multi.codes_json === 'string' ? JSON.parse(multi.codes_json) : []);
      const strategies = strategyIds.map((id, i) => ({ id, name: strategyNames[i] || id }));

      // 补全 items 的 code_name
      items.forEach(it => { if (!it.code_name) it.code_name = codeNames[it.code] || ''; });

      this._currentMultiData = { multi, items, strategies, codes, codeNames };

      // 1) Hero card
      this._renderMultiHero(items, strategies, codeNames);

      // 2) Heatmap
      if (window.StockRadar?.MultiCompareChart) {
        window.StockRadar.MultiCompareChart.renderHeatmap(
          document.getElementById('btMultiHeatmap'),
          items, strategies, codes, codeNames,
          { onCellClick: (s, c) => this._onMultiCellClick(s, c) }
        );
      }

      // 3) Ranking table
      this._renderMultiRanking(items, strategies, codeNames);

      // 4) Strategy tabs
      this._renderMultiStrategyTabs(items, strategies, codes, codeNames, multi);

      // 颜色切换事件
      this._bindMultiColorToggle(items, strategies, codes, codeNames);
    }

    _renderMultiHero(items, strategies, codeNames) {
      const hero = document.getElementById('btMultiHero');
      if (!hero) return;
      const completed = items.filter(it => it.status === 'completed' && it.sharpe_ratio != null);
      if (completed.length === 0) {
        hero.style.display = 'none'; return;
      }
      hero.style.display = '';
      const best = completed.reduce((a, b) => (b.sharpe_ratio || 0) > (a.sharpe_ratio || 0) ? b : a);
      const stratName = strategies.find(s => s.id === best.strategy_id)?.name || best.strategy_id;
      hero.innerHTML = `
        <div>
          <div><span class="badge">🏆 最佳风险收益比</span></div>
          <div class="hero-metrics">
            <div class="hero-metric"><div class="val">${stratName} × ${best.code_name || best.code}</div><div class="lbl">策略 × 标的</div></div>
            <div class="hero-metric"><div class="val" style="color:#ffd700">${best.total_return >= 0 ? '+' : ''}${((best.total_return||0)*100).toFixed(2)}%</div><div class="lbl">总收益</div></div>
            <div class="hero-metric"><div class="val">${best.sharpe_ratio != null ? best.sharpe_ratio.toFixed(2) : '—'}</div><div class="lbl">夏普比率</div></div>
            <div class="hero-metric"><div class="val">${((best.max_drawdown||0)*100).toFixed(2)}%</div><div class="lbl">最大回撤</div></div>
          </div>
        </div>`;
    }

    _renderMultiRanking(items, strategies, codeNames) {
      const tbody = document.getElementById('btMultiRankingBody');
      if (!tbody) return;
      const completed = items.filter(it => it.status === 'completed');
      if (completed.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:20px;">暂无可对比数据</td></tr>';
        return;
      }
      const map = { return: 'total_return', excess: 'excess_return', sharpe: 'sharpe_ratio', drawdown: 'max_drawdown', consecutive: 'max_consecutive_losses' };
      const k = map[this._multiSortKey] || 'total_return';
      const sorted = [...completed].sort((a, b) => {
        const va = a[k] ?? 0, vb = b[k] ?? 0;
        return this._multiSortAsc ? (va - vb) : (vb - va);
      });
      tbody.innerHTML = sorted.map(d => {
        const stratName = strategies.find(s => s.id === d.strategy_id)?.name || d.strategy_id;
        const nm = d.code_name || codeNames[d.code] || d.code;
        const excess = d.excess_return || ((d.total_return || 0) - (d.bench_return || 0.04));
        return `<tr>
          <td>${stratName}</td>
          <td>${nm} <span style="font-size:10px;color:var(--muted);">${d.code}</span></td>
          <td class="${(d.total_return||0)>=0?'profit':'loss'}">${(d.total_return||0)>=0?'+':''}${((d.total_return||0)*100).toFixed(2)}%</td>
          <td class="${excess>=0?'profit':'loss'}">${excess>=0?'+':''}${(excess*100).toFixed(2)}pp</td>
          <td>${(d.sharpe_ratio||0).toFixed(2)}</td>
          <td class="loss">${((d.max_drawdown||0)*100).toFixed(2)}%</td>
          <td>${d.max_consecutive_losses||0} 笔</td>
          <td><button class="dc-btn dc-btn-sm" onclick="window._btPanel?.drillDown('${d.run_id}', '${d.code}')">钻取</button></td>
        </tr>`;
      }).join('');

      // 绑定排序
      tbody.closest('table')?.querySelectorAll('.bt-sortable').forEach(th => {
        th.onclick = () => {
          const sk = th.dataset.sort;
          if (this._multiSortKey === sk) this._multiSortAsc = !this._multiSortAsc;
          else { this._multiSortKey = sk; this._multiSortAsc = false; }
          this._renderMultiRanking(items, strategies, codeNames);
        };
      });
    }

    _renderMultiStrategyTabs(items, strategies, codes, codeNames, multi) {
      const tabsEl = document.getElementById('btMultiStrategyTabs');
      const contentEl = document.getElementById('btMultiStrategyContent');
      if (!tabsEl || !contentEl) return;

      tabsEl.innerHTML = strategies.map((s, i) =>
        `<button class="bt-strategy-tab${i === 0 ? ' active' : ''}" data-idx="${i}" onclick="window._btPanel?._switchMultiStrategyTab(${i})">${s.name}</button>`
      ).join('');

      this._switchMultiStrategyTab(0);
    }

    async _switchMultiStrategyTab(idx) {
      const d = this._currentMultiData;
      if (!d) return;
      const { items, strategies, codes, codeNames, multi } = d;
      const s = strategies[idx];
      if (!s) return;

      document.querySelectorAll('#btMultiStrategyTabs .bt-strategy-tab').forEach(t => t.classList.remove('active'));
      document.querySelector(`#btMultiStrategyTabs .bt-strategy-tab[data-idx="${idx}"]`)?.classList.add('active');

      const stratItems = items.filter(it => it.strategy_id === s.id && it.status === 'completed');
      const avgRet = stratItems.length > 0
        ? stratItems.reduce((sum, it) => sum + (it.total_return || 0), 0) / stratItems.length : 0;

      const contentEl = document.getElementById('btMultiStrategyContent');
      if (!contentEl) return;
      if (stratItems.length === 0) {
        contentEl.innerHTML = '<div style="padding:40px;text-align:center;color:var(--muted);">该策略下暂无完成数据</div>';
        return;
      }

      // 加载该策略的 batch 详情，内嵌渲染净值对比图
      const batchItem = items.find(it => it.strategy_id === s.id && it.batch_id);
      let chartHtml = '';
      if (batchItem) {
        try {
          const detail = await window.StockRadar.api.getBatch(batchItem.batch_id);
          chartHtml = `<div class="bt-chart" id="btMultiEquityChart_${idx}" style="height:320px;margin-bottom:12px;"></div>`;
          // 延迟渲染图表（等 DOM 插入后再初始化 ECharts）
          setTimeout(async () => {
            const container = document.getElementById(`btMultiEquityChart_${idx}`);
            if (!container || !window.StockRadar?.BatchCompareChart || !detail.items) return;
            const enriched = [];
            for (const it of (detail.items || [])) {
              if (it.status === 'completed' && it.run_id) {
                try {
                  const runData = await window.StockRadar.api.getBacktestRunDetail(it.run_id);
                  enriched.push({
                    ...it,
                    result: this._normalizeResult(runData),
                    equity_curve: runData.result?.equity_curve || runData.result?.equity_curve_json || [],
                  });
                } catch (e) { enriched.push({ ...it }); }
              } else { enriched.push({ ...it }); }
            }
            window.StockRadar.BatchCompareChart.render(container, enriched, {
              initialCash: detail.batch?.initial_cash || 100000,
            });
          }, 150);
        } catch (e) { /* batch 加载失败，跳过图表 */ }
      }

      contentEl.innerHTML = `
        <div style="margin-bottom:10px;font-size:12px;color:var(--text-secondary);">
          ${stratItems.length} 个标的 · 平均收益 <b style="color:${avgRet >= 0 ? 'var(--positive)' : 'var(--negative)'}">${avgRet >= 0 ? '+' : ''}${(avgRet * 100).toFixed(2)}%</b>
        </div>
        ${chartHtml}
        <table class="trades-table" style="width:100%;">
          <thead><tr><th>标的</th><th>收益</th><th>夏普</th><th>最大回撤</th><th>最大连亏</th><th>操作</th></tr></thead>
          <tbody>${stratItems.map(it => {
            const nm = it.code_name || codeNames[it.code] || it.code;
            return `<tr>
              <td>${nm} <span style="font-size:10px;color:var(--muted);">${it.code}</span></td>
              <td class="${(it.total_return||0)>=0?'profit':'loss'}">${(it.total_return||0)>=0?'+':''}${((it.total_return||0)*100).toFixed(2)}%</td>
              <td>${(it.sharpe_ratio||0).toFixed(2)}</td>
              <td class="loss">${((it.max_drawdown||0)*100).toFixed(2)}%</td>
              <td>${it.max_consecutive_losses||0} 笔</td>
              <td><button class="dc-btn dc-btn-sm" onclick="window._btPanel?.drillDown('${it.run_id}', '${it.code}')">钻取</button></td>
            </tr>`;
          }).join('')}</tbody>
        </table>
      `;
    }

    _onMultiCellClick(strategy, code) {
      // 点击热力图格子：切换到对应策略 tab
      const d = this._currentMultiData;
      if (!d) return;
      const si = d.strategies.findIndex(s => s.id === strategy.id);
      if (si >= 0) this._switchMultiStrategyTab(si);
    }

    _bindMultiColorToggle(items, strategies, codes, codeNames) {
      const toggle = document.getElementById('btMultiColorToggle');
      if (!toggle) return;
      // 移除旧事件，重新绑定
      toggle.querySelectorAll('button').forEach(btn => {
        btn.onclick = () => {
          toggle.querySelectorAll('button').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          const mode = btn.dataset.mode;
          if (window.StockRadar?.MultiCompareChart) {
            window.StockRadar.MultiCompareChart.setColorMode(mode);
            window.StockRadar.MultiCompareChart.renderHeatmap(
              document.getElementById('btMultiHeatmap'),
              items, strategies, codes, codeNames,
              { onCellClick: (s, c) => this._onMultiCellClick(s, c) }
            );
          }
        };
      });
    }

    /** 从历史页查看多策略批次 */
    async viewMulti(multiId) {
      try {
        const detail = await window.StockRadar.api.getMulti(multiId);
        if (!detail || !detail.multi) { alert('批次不存在'); return; }
        const multi = detail.multi;
        this.switchTab(document.querySelector('.bt-tab[data-btab="btMultiPanel"]'));
        document.getElementById('btMultiEmpty').style.display = 'none';
        // running/pending：显示进度条 + 监听 SSE，完成后渲染最终结果
        if (multi.status === 'running' || multi.status === 'pending') {
          document.getElementById('btMultiRunning').classList.add('visible');
          document.getElementById('btMultiProgressBar').style.width = '0%';
          document.getElementById('btMultiProgressText').textContent = '监听进度中...';
          try {
            await this._streamMultiUntilDone(multiId);
          } catch (e) { /* SSE 失败不阻塞，用当前快照渲染 */ }
          const finalDetail = await window.StockRadar.api.getMulti(multiId);
          document.getElementById('btMultiRunning').classList.remove('visible');
          await this._renderMultiResult(finalDetail);
        } else {
          document.getElementById('btMultiRunning').classList.remove('visible');
          await this._renderMultiResult(detail);
        }
      } catch (e) { alert('加载批次失败: ' + (e.message || e)); }
    }

    /** 后台 SSE 清理全部失败的运行记录 */
    cleanFailed() {
      const confirmed = confirm('确认删除所有失败的运行记录？\n\n包括：失败的单股回测、失败的批量回测、失败的多策略回测。\n成功/运行中的记录不受影响。');
      if (!confirmed) return;

      // 显示进度条
      const cleanBar = document.getElementById('btCleanProgress');
      const cleanText = document.getElementById('btCleanProgressText');
      if (cleanBar) { cleanBar.style.display = 'block'; cleanBar.querySelector('.progress-fill').style.width = '0%'; }
      if (cleanText) cleanText.textContent = '扫描中...';

      window.StockRadar.api.streamCleanFailed(
        (msg) => {
          const s = msg.stats || {};
          const text = [`单股:${s.runs || 0}/${s.runs_total || 0}`, `批量:${s.batches || 0}/${s.batches_total || 0}`, `多策略:${s.multis || 0}/${s.multis_total || 0}`].join('  ');
          if (cleanText) cleanText.textContent = text;

          const total = (s.runs_total || 0) + (s.batches_total || 0) + (s.multis_total || 0);
          const done = (s.runs || 0) + (s.batches || 0) + (s.multis || 0);
          if (cleanBar && total > 0) {
            cleanBar.querySelector('.progress-fill').style.width = (done / total * 100).toFixed(0) + '%';
          }

          if (msg.phase === 'done') {
            if (cleanBar) cleanBar.style.display = 'none';
            alert(`已清理 ${msg.total_cleaned} 条失败记录`);
            this.loadHistory();
          }
        },
        (err) => {
          if (cleanBar) cleanBar.style.display = 'none';
          alert('清理失败: ' + (err.message || err));
        }
      );
    }

    /** 删除单条失败记录 */
    async _deleteFailedItem(type, id) {
      const confirmed = confirm(`确认删除此记录？\n${type}: ${id}`);
      if (!confirmed) return;
      try {
        if (type === 'run') await window.StockRadar.api.deleteBacktestRun(id);
        else if (type === 'batch') await window.StockRadar.api.deleteBatch(id);
        else if (type === 'multi') await window.StockRadar.api.deleteMulti(id);
        this.loadHistory();
      } catch (e) { alert('删除失败: ' + (e.message || e)); }
    }

    /** 钻取到单 code 详情：用 run_id 加载到回测结果 tab */
    async drillDown(runId, code) {
      try {
        const data = await window.StockRadar.api.getBacktestRunDetail(runId);
        if (!data.result) { alert('该标的无回测结果'); return; }
        this.switchTab(document.querySelector('.bt-tab[data-btab="btResultPanel"]'));
        const config = JSON.parse(data.run?.config_json || '{}');
        const result = this._normalizeResult(data);
        this.currentRunId = runId;
        this._lastEval = data.eval || null;
        this._showResult(result, config);
      } catch (e) { alert('加载失败: ' + (e.message || e)); }
    }

    async rerunItem(batchId, code) {
      // 单个重试：提交后监听 batch SSE，完成再刷新（不再弹 alert + 立即刷新看 pending）
      try {
        await window.StockRadar.api.rerunBatchItem(batchId, code);
        document.getElementById('btBatchRunning')?.classList.add('visible');
        try {
          await this._streamBatchUntilDone(batchId);
        } catch (e) { /* SSE 失败不阻塞，用当前快照渲染 */ }
        document.getElementById('btBatchRunning')?.classList.remove('visible');
        const detail = await window.StockRadar.api.getBatch(batchId);
        await this._renderBatchResult(detail);
      } catch (e) { alert('重跑失败: ' + (e.message || e)); }
      finally { document.getElementById('btBatchRunning')?.classList.remove('visible'); }
    }

    /** 批量重试所有失败 item（复用原 batch，成功数据保留，只重摆失败的积木） */
    async rerunFailedItems(batchId) {
      try {
        const res = await window.StockRadar.api.rerunBatchFailed(batchId);
        document.getElementById('btBatchRunning')?.classList.add('visible');
        try {
          await this._streamBatchUntilDone(batchId);
        } catch (e) { /* SSE 失败不阻塞，用当前快照渲染 */ }
        document.getElementById('btBatchRunning')?.classList.remove('visible');
        // 完成后拉取最新 batch 详情（汇总 + 状态 + 明细均已更新）
        const detail = await window.StockRadar.api.getBatch(batchId);
        await this._renderBatchResult(detail);
        // 同步刷新历史列表里的状态
        this.loadHistory('batches').catch(() => {});
        const cnt = res?.count || 0;
        const ok = detail?.batch?.status;
        if (ok === 'completed') {
          console.log(`批量重试完成：${cnt} 个标的全部成功`);
        } else if (ok === 'partial') {
          console.log(`批量重试完成：${cnt} 个标的部分成功，仍有失败项`);
        }
      } catch (e) { alert('批量重试失败: ' + (e.message || e)); }
      finally { document.getElementById('btBatchRunning')?.classList.remove('visible'); }
    }

    // ============================================================
    // 测试集 CRUD
    // ============================================================

    async refreshTestSets() {
      try {
        const res = await window.StockRadar.api.listTestSets();
        const select = document.getElementById('btTestSetSelect');
        if (!select) return;
        const sets = res.test_sets || [];
        this._testSets = sets;
        const cur = select.value;
        select.innerHTML = '<option value="">— 选择测试集载入 —</option>' +
          sets.map(s => `<option value="${s.id}">${this._esc(s.name)} (${s.code_count}标的)</option>`).join('');
        if (cur) select.value = cur;
      } catch (e) { console.error('加载测试集列表失败:', e); }
    }

    loadTestSet(id) {
      if (!id || !this._testSets) return;
      const ts = this._testSets.find(s => String(s.id) === String(id));
      if (!ts) return;
      const codes = ts.codes_json || ts.codes || [];
      document.getElementById('btInputCodes').value = codes.join('\n');
      this._renderChips();
    }

    async _showSaveTestSetDialog() {
      const codes = this._parseCodes(document.getElementById('btInputCodes')?.value || '');
      if (codes.length === 0) { alert('请先输入至少一个标的代码'); return; }

      // 填充模态弹窗
      const modal = document.getElementById('btSaveTestsetModal');
      if (!modal) {
        // 回退到 prompt
        const name = prompt('请输入测试集名称：', `测试集_${new Date().toISOString().slice(0, 10)}`);
        if (!name || !name.trim()) return;
        try {
          await window.StockRadar.api.createTestSet({ name: name.trim(), codes, description: '', tags: [] });
          alert('已保存为测试集');
          await this.refreshTestSets();
        } catch (e) { alert('保存失败: ' + (e.message || e)); }
        return;
      }

      // 重置表单
      document.getElementById('btTestsetName').value = `测试集_${new Date().toISOString().slice(0, 10)}`;
      document.getElementById('btTestsetDesc').value = '';
      document.getElementById('btTestsetTags').value = '';
      const chipsBox = document.getElementById('btModalChips');
      if (chipsBox) {
        chipsBox.innerHTML = codes.map(c => {
          const type = this._detectAssetType(c);
          return `<span class="bt-chip">${this._esc(c)} <span class="bt-chip-type ${type}">${type.toUpperCase()}</span></span>`;
        }).join('');
      }
      const cntEl = document.getElementById('btModalChipsCount');
      if (cntEl) cntEl.textContent = codes.length;

      modal.classList.add('visible');

      // 绑定保存按钮（一次性，避免重复绑定）
      const saveBtn = document.getElementById('btTestsetSaveBtn');
      if (saveBtn && !saveBtn._btBound) {
        saveBtn._btBound = true;
        saveBtn.addEventListener('click', async () => {
          const name = document.getElementById('btTestsetName').value.trim();
          if (!name) { alert('请输入测试集名称'); return; }
          const desc = document.getElementById('btTestsetDesc').value.trim();
          const tags = document.getElementById('btTestsetTags').value.split(/[,，\s]+/).map(t => t.trim()).filter(Boolean);
          saveBtn.disabled = true;
          saveBtn.textContent = '保存中...';
          try {
            await window.StockRadar.api.createTestSet({ name, codes, description: desc, tags });
            modal.classList.remove('visible');
            await this.refreshTestSets();
          } catch (e) {
            alert('保存失败: ' + (e.message || e));
          } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = '保存';
          }
        });
      }
    }

    manageTestSets() {
      const modal = document.getElementById('btManageTestsetModal');
      if (!modal) {
        // 回退到 prompt 模式
        this._manageTestSetsSimple();
        return;
      }
      this._renderManageTestsetTable();
      modal.classList.add('visible');
    }

    _renderManageTestsetTable() {
      const tbody = document.getElementById('btManageTestsetBody');
      if (!tbody) return;
      const sets = this._testSets || [];
      if (sets.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:20px">暂无测试集</td></tr>';
        return;
      }
      tbody.innerHTML = sets.map(s => {
        const created = (s.created_at || s.created || '').slice(0, 10);
        return `<tr>
          <td>${this._esc(s.name)}</td>
          <td class="mono">${s.code_count || (s.codes_json || s.codes || []).length}</td>
          <td class="mono">${created}</td>
          <td>
            <button class="bt-modal-btn" onclick="window._btPanel?._renameTestSet(${s.id})">重命名</button>
            <button class="bt-modal-btn danger" onclick="window._btPanel?._deleteTestSet(${s.id})">删除</button>
          </td>
        </tr>`;
      }).join('');
    }

    async _renameTestSet(id) {
      const ts = (this._testSets || []).find(s => s.id === id);
      if (!ts) return;
      const name = prompt('请输入新名称：', ts.name);
      if (!name || !name.trim() || name.trim() === ts.name) return;
      try {
        await window.StockRadar.api.updateTestSet(id, { name: name.trim(), codes: ts.codes_json || ts.codes || [] });
        await this.refreshTestSets();
        this._renderManageTestsetTable();
      } catch (e) { alert('重命名失败: ' + (e.message || e)); }
    }

    async _deleteTestSet(id) {
      const ts = (this._testSets || []).find(s => s.id === id);
      if (!ts) return;
      if (!confirm(`确认删除测试集「${ts.name}」？此操作不可撤销。`)) return;
      try {
        await window.StockRadar.api.deleteTestSet(id);
        await this.refreshTestSets();
        this._renderManageTestsetTable();
      } catch (e) { alert('删除失败: ' + (e.message || e)); }
    }

    _manageTestSetsSimple() {
      if (!this._testSets || this._testSets.length === 0) { alert('暂无测试集'); return; }
      const lines = this._testSets.map((s, i) => `${i + 1}. ${s.name} (${s.code_count}标的)`).join('\n');
      const input = prompt(`当前测试集：\n${lines}\n\n输入序号删除对应测试集（取消则不操作）：`, '');
      if (!input) return;
      const idx = parseInt(input, 10) - 1;
      if (isNaN(idx) || idx < 0 || idx >= this._testSets.length) { alert('序号无效'); return; }
      const ts = this._testSets[idx];
      if (!confirm(`确认删除测试集「${ts.name}」？`)) return;
      window.StockRadar.api.deleteTestSet(ts.id)
        .then(() => { alert('已删除'); this.refreshTestSets(); })
        .catch(e => alert('删除失败: ' + (e.message || e)));
    }

    /** 持仓/自选「策略扫描」跳转：打开回测面板并预填批量标的
     * （多代码自动走批量回测 batch-run；用户选策略后点「开始回测」） */
    openScanWithCodes(codes) {
      const list = (codes || []).map(c => String(c).trim()).filter(Boolean);
      this.show();
      const input = document.getElementById('btInputCodes');
      if (input) {
        input.value = list.join(',');
        const codesParsed = this._parseCodes(input.value);
        if (codesParsed.length === 1) this._autoDetectAssetType(codesParsed[0]);
        this._renderChips();
      }
      // 呼入后给用户一句引导
      if (list.length) {
        const hint = document.getElementById('btCodesHint');
        if (hint) hint.textContent = `已载入 ${list.length} 只标的（批量回测）。选择策略后点「开始回测」。`;
      }
    }

    show() {
      if (this.overlay) this.overlay.classList.add('visible');
      this.refreshTestSets();
      // 每次打开都重置为默认居中窗口，避免上次拖动/最大化的位置残留
      this.restore();
    }
    hide() {
      if (this.overlay) this.overlay.classList.remove('visible');
      if (this.modal) this.modal.classList.remove('eval-mode');
      this.restore();
    }
  }

  window._btPanel = null;
  window.initBacktestPanel = function () {
    if (!window._btPanel) window._btPanel = new BacktestPanel();
    return window._btPanel;
  };
})();
