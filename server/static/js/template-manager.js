/*
   选股雷达 Web - 报告模板管理
*/

window.StockRadar = window.StockRadar || {};

/* 场景族定义 */
const FAMILY_DEFS = [
  { id: 'market',      label: '大盘总结' },
  { id: 'sector',      label: '板块分析' },
  { id: 'instrument',  label: '个股/产品' },
  { id: 'screening',   label: '选股' },
];

const FAMILY_BY_TEMPLATE = {
  market_daily: 'market',
  macro_daily: 'market',
  event_impact: 'market',
  sector_timing: 'sector',
  sector_landscape: 'sector',
  theme_trading: 'sector',
  industry_chain: 'sector',
  stock_deep_dive: 'instrument',
  etf_analysis: 'instrument',
  portfolio_advice: 'instrument',
  valuation_percentile: 'instrument',
  trend_following: 'instrument',
  earnings_analysis: 'instrument',
  stock_comparison: 'instrument',
  ipo_analysis: 'instrument',
  standard: 'instrument',
  multi_factor_screening: 'screening',
  dividend_screening: 'screening',
  volume_price_alert: 'screening',
  consensus_view: 'screening',
};

/* 元数据字段的 select 选项 */
const META_OPTIONS = {
  report_family: [
    ['market',      'market · 大盘总结'],
    ['sector',      'sector · 板块分析'],
    ['instrument',  'instrument · 个股/产品'],
    ['screening',   'screening · 选股'],
  ],
  asset_type: [
    ['stock',       'stock · 个股'],
    ['etf',         'etf · ETF'],
    ['index',       'index · 指数'],
    ['commodity',   'commodity · 商品'],
    ['fund',        'fund · 基金'],
    ['portfolio',   'portfolio · 组合'],
    ['mixed',       'mixed · 混合'],
  ],
  intent_type: [
    ['daily_summary',   'daily_summary · 每日总结'],
    ['deep_analysis',   'deep_analysis · 深度分析'],
    ['screening',       'screening · 选股筛选'],
    ['timing',          'timing · 择时'],
    ['comparison',      'comparison · 对比'],
    ['event',           'event · 事件驱动'],
  ],
  dashboard_type: [
    ['market',      'market · 市场仪表盘'],
    ['sector',      'sector · 板块仪表盘'],
    ['stock',       'stock · 个股仪表盘'],
    ['screening',   'screening · 筛选仪表盘'],
    ['hot_events',  'hot_events · 热点事件仪表盘'],
  ],
  verbosity: [
    ['brief',       'brief · 简版'],
    ['standard',    'standard · 标准'],
    ['detailed',    'detailed · 深度'],
  ],
};

window.StockRadar.ReportTemplateManager = class ReportTemplateManager {
  constructor() {
    this.panelEl = document.getElementById('templateManagerPanel');
    this.overlayEl = document.getElementById('templateManagerOverlay');
    this.bodyEl = document.getElementById('templateManagerBody');
    this.catalog = null;
    this.currentId = null;
    this.currentDetail = null;
    this.dirty = false;
    this.pendingTemplate = null;
    this.pendingChanges = [];
    this.activeFamily = null;           // 当前选中的场景族，null 表示全部
    this.activePreviewTab = 'composition'; // 右侧预览区当前 tab
    this.importDrawerOpen = false;       // 导入抽屉是否打开
    this.importMode = 'single';          // 导入模式：single / bundle / upgrade
    this._selectSeq = 0;
    // diff modal 元素（保留兼容）
    this.diffOverlayEl = document.getElementById('templateDiffOverlay');
    this.diffModalEl = document.getElementById('templateDiffModal');
    this.diffBodyEl = document.getElementById('templateDiffBody');
    this.diffSummaryEl = document.getElementById('templateDiffSummary');
    this.diffApplyBtn = document.getElementById('templateDiffApplyBtn');
    this._bindDiffModal();
  }

  open() {
    this.panelEl.classList.add('open');
    this.overlayEl.classList.add('open');
    if (!this.catalog) {
      this.loadCatalog();
    } else {
      // 已有数据，直接渲染（确保 DOM 和事件绑定）
      this.render();
    }
  }

  close() {
    this.panelEl.classList.remove('open');
    this.overlayEl.classList.remove('open');
    this.closeSavePreview();
  }

  async loadCatalog() {
    this._setState('正在加载报告模板...');
    try {
      this.catalog = await window.StockRadar.api.getReportTemplates();
      const templates = this.catalog.templates || [];
      const defaultId = this.catalog.default || templates[0]?.id;
      this.currentId = defaultId;
      this.render();
      if (defaultId) await this.selectTemplate(defaultId);
    } catch (err) {
      this._setState('模板加载失败：' + err.message);
    }
  }

  async selectTemplate(templateId) {
    if (!templateId) return;
    const seq = ++this._selectSeq;
    this.currentId = templateId;
    this.dirty = false;
    this._updateSidebarActive();
    this.bodyEl.querySelectorAll('[data-template-id]').forEach(btn => {
      btn.classList.toggle('loading', btn.dataset.templateId === templateId);
    });
    const editor = this.bodyEl.querySelector('.template-editor');
    if (editor) editor.innerHTML = '<div class="template-manager-state">正在读取模板...</div>';
    try {
      const detail = await window.StockRadar.api.getReportTemplate(templateId);
      if (seq !== this._selectSeq) return;
      this.currentDetail = detail;
      this.renderEditor();
      this.renderInspector();
    } catch (err) {
      if (seq !== this._selectSeq) return;
      if (editor) editor.innerHTML = `<div class="template-manager-state">模板读取失败：${this._esc(err.message)}</div>`;
    } finally {
      if (seq === this._selectSeq) {
        this.bodyEl.querySelectorAll('[data-template-id]').forEach(btn => btn.classList.remove('loading'));
      }
    }
  }

  render() {
    const templates = this.catalog?.templates || [];
    this.bodyEl.innerHTML = `
      <aside class="template-sidebar">
        <div class="template-sidebar-tools">
          <input class="template-search" data-template-search placeholder="搜索模板 / block / skill">
          <button class="template-tool-btn" type="button" data-open-import>导入</button>
        </div>
        ${this._renderFamilyTabs(templates)}
        <div class="template-list">
          ${templates.map(item => this._renderTemplateItem(item)).join('')}
        </div>
      </aside>
      <section class="template-editor"></section>
      <aside class="template-inspector" data-preview-root></aside>
      ${this._renderImportDrawer()}`;

    // 绑定模板项点击
    this.bodyEl.querySelectorAll('[data-template-id]').forEach(btn => {
      btn.onclick = () => this.selectTemplate(btn.dataset.templateId);
    });
    // 绑定搜索
    const search = this.bodyEl.querySelector('[data-template-search]');
    if (search) {
      search.oninput = () => this._filterTemplates(search.value);
    }
    // 绑定场景族 tab
    this.bodyEl.querySelectorAll('.family-tab[data-family]').forEach(tab => {
      tab.onclick = () => this._selectFamily(tab.dataset.family);
    });
    // 绑定预览区 tab
    this.bodyEl.querySelectorAll('[data-preview-tab]').forEach(tab => {
      tab.onclick = () => this._switchPreviewTab(tab.dataset.previewTab);
    });
    // 绑定导入抽屉
    this._bindImportDrawer();
    this.bodyEl.querySelectorAll('[data-open-import]').forEach(btn => {
      btn.onclick = () => this._openImportDrawer();
    });
  }

  /* 渲染场景族 tab */
  _renderFamilyTabs(templates) {
    const counts = {};
    for (const f of FAMILY_DEFS) counts[f.id] = 0;
    for (const t of templates) {
      const family = this._familyForTemplate(t);
      if (family && counts[family] !== undefined) counts[family]++;
    }
    const allActive = !this.activeFamily;
    return `
      <div class="family-tabs">
        <button class="family-tab${allActive ? ' active' : ''}" type="button" data-family="">
          <div class="family-name">全部 <span class="family-count">${templates.length}</span></div>
          <div class="family-id">all</div>
        </button>
        ${FAMILY_DEFS.map(f => `
          <button class="family-tab${this.activeFamily === f.id ? ' active' : ''}" type="button" data-family="${f.id}">
            <div class="family-name">${this._esc(f.label)} <span class="family-count">${counts[f.id]}</span></div>
            <div class="family-id">${this._esc(f.id)}</div>
          </button>`).join('')}
      </div>`;
  }

  /* 选择场景族 */
  _selectFamily(familyId) {
    this.activeFamily = familyId || null;
    // 更新 tab 高亮
    this.bodyEl.querySelectorAll('.family-tab[data-family]').forEach(tab => {
      tab.classList.toggle('active', familyId ? tab.dataset.family === familyId : tab.dataset.family === '');
    });
    // 过滤模板列表
    this._filterTemplates(this.bodyEl.querySelector('[data-template-search]')?.value || '');
  }

  _renderTemplateItem(item) {
    const active = item.id === this.currentId ? ' active' : '';
    const enabled = item.enabled !== false;
    const family = this._familyForTemplate(item);
    return `
      <button class="template-list-item${active}" type="button" data-template-id="${this._esc(item.id)}" data-family="${this._esc(family)}">
        <div class="template-list-title">
          <span>${this._esc(item.name || item.id)}</span>
          <span class="template-dot${enabled ? '' : ' muted'}"></span>
        </div>
        <div class="template-list-id">${this._esc(item.id)}</div>
        <div class="template-list-desc">${this._esc(item.description || '')}</div>
      </button>`;
  }

  renderEditor() {
    const editor = this.bodyEl.querySelector('.template-editor');
    if (!editor || !this.currentDetail) return;
    const template = this.currentDetail.template || {};
    const entry = this.currentDetail.entry || {};
    const outputBlocks = template.output_blocks || [];
    const dataContract = template.data_contract || [];
    const qaRules = template.qa_rules || [];
    const requiredSkills = template.required_skills || [];
    const fallbackSkills = template.fallback_skills || [];
    const horizons = template.supported_horizons || [];

    editor.innerHTML = `
      <div class="template-editor-grid">
      <div class="template-workbar">
        <div>
          <div class="template-workbar-title">模板工作台</div>
          <div class="template-workbar-subtitle">编辑结构、数据契约和质量规则，右侧实时查看 block-slot-source 映射。</div>
        </div>
        <div class="template-workbar-actions">
          <button class="secondary-button" type="button" data-open-import>导入模板</button>
          <button class="secondary-button" type="button" data-preview-save>保存预检</button>
        </div>
      </div>
      <div class="template-manager-summary">
        <div class="template-metric"><span>当前文件</span><strong>${this._esc(this.currentDetail.path || entry.path || '-')}</strong></div>
        <div class="template-metric"><span>输出 blocks</span><strong>${outputBlocks.length} 个</strong></div>
        <div class="template-metric"><span>必需技能</span><strong>${requiredSkills.length} 个</strong></div>
        <div class="template-metric"><span>状态</span><strong>${entry.enabled === false ? '未启用' : '启用中'}</strong></div>
      </div>

      <div class="template-section">
        <div class="template-section-head">
          <div><div class="template-section-title">场景元数据</div><div class="template-section-desc">用于路由、仪表盘类型、构成图分组和后处理压缩。</div></div>
          <span class="template-count">新增</span>
        </div>
        <div class="template-meta-grid">
          ${this._selectField('主场景', 'report_family', template.report_family || '', META_OPTIONS.report_family)}
          ${this._selectField('资产类型', 'asset_type', template.asset_type || '', META_OPTIONS.asset_type)}
          ${this._selectField('意图类型', 'intent_type', template.intent_type || '', META_OPTIONS.intent_type)}
          ${this._selectField('仪表盘', 'dashboard_type', template.dashboard_type || '', META_OPTIONS.dashboard_type)}
          ${this._selectField('输出密度', 'verbosity', template.verbosity || '', META_OPTIONS.verbosity)}
          ${this._field('字数预算', 'max_report_words', template.max_report_words ?? '')}
        </div>
      </div>

      <div class="template-section">
        <div class="template-section-head">
          <div><div class="template-section-title">基础信息</div><div class="template-section-desc">模板身份、角色提示和时间框架。</div></div>
          <span class="template-count">结构化</span>
        </div>
        ${this._field('模板名称', 'name', template.name || '')}
        ${this._field('版本', 'version', template.version || '')}
        ${this._horizonField(template.default_horizon || template.time_horizon?.default || 'short')}
        ${this._textareaField('分析师角色提示', 'role', template.role || '', 4)}
      </div>

      <div class="template-section">
        <div class="template-section-head">
          <div><div class="template-section-title">技能配置</div><div class="template-section-desc">模板需要的工具技能，保存时按逗号分隔写回数组。</div></div>
          <span class="template-count">${requiredSkills.length + fallbackSkills.length} 项</span>
        </div>
        ${this._field('必需技能', 'required_skills', requiredSkills.join(', '))}
        ${this._field('备用技能', 'fallback_skills', fallbackSkills.join(', '))}
        ${this._field('支持周期', 'supported_horizons', horizons.join(', '))}
      </div>

      <div class="template-section">
        <div class="template-section-head">
          <div><div class="template-section-title">输出结构</div><div class="template-section-desc">报告段落顺序，对应 blocks/*.json。</div></div>
          <span class="template-count">${outputBlocks.length} blocks</span>
        </div>
        ${this._renderBlockPicker(outputBlocks)}
      </div>

      <div class="template-section">
        <div class="template-section-head">
          <div><div class="template-section-title">数据契约</div><div class="template-section-desc">数据 slot、字段和工具能力。</div></div>
          <span class="template-count">${dataContract.length} slots</span>
        </div>
        ${this._renderDataContractTable(dataContract)}
        ${this._textareaField('data_contract JSON', 'data_contract', JSON.stringify(dataContract, null, 2), 8)}
      </div>

      <div class="template-section">
        <div class="template-section-head">
          <div><div class="template-section-title">质量规则</div><div class="template-section-desc">约束最终报告的 QA rules。</div></div>
          <span class="template-count">${qaRules.length} 条</span>
        </div>
        <ol class="template-rule-list">
          ${qaRules.slice(0, 6).map(rule => `<li>${this._esc(rule)}</li>`).join('')}
        </ol>
        ${this._textareaField('qa_rules', 'qa_rules', qaRules.join('\n'), 7)}
      </div>

      <div class="template-actions">
        <div class="template-actions-inner">
          <button class="secondary-button" type="button" data-template-reload>重新加载</button>
          <button class="send-button" type="button" data-template-save>保存模板</button>
        </div>
      </div>
      </div>`;

    editor.querySelectorAll('[data-field], [data-block-id], .template-horizon-option input').forEach(input => {
      input.oninput = () => {
        this.dirty = true;
        this._scheduleInspectorRender();
      };
      input.onchange = () => {
        this.dirty = true;
        if (input.closest('.template-horizon-options')) {
          editor.querySelectorAll('.template-horizon-option').forEach(label => {
            label.classList.toggle('active', !!label.querySelector('input')?.checked);
          });
        }
        if (input.dataset.blockId) {
          input.closest('.template-block-option')?.classList.toggle('active', input.checked);
        }
        this._scheduleInspectorRender();
      };
    });
    editor.querySelector('[data-template-reload]').onclick = () => this.selectTemplate(this.currentId);
    editor.querySelector('[data-template-save]').onclick = () => this.showSavePreview();
    editor.querySelectorAll('[data-open-import]').forEach(btn => {
      btn.onclick = () => this._openImportDrawer();
    });
    editor.querySelectorAll('[data-preview-save]').forEach(btn => {
      btn.onclick = () => this._switchPreviewTab('save');
    });
  }

  renderInspector() {
    const inspector = this.bodyEl.querySelector('.template-inspector');
    if (!inspector || !this.currentDetail) return;

    inspector.innerHTML = `
      <div class="template-preview-header">
        <div class="template-preview-title">模板构成图</div>
        <div class="template-preview-subtitle">展示当前模板如何把用户问题路由到 block，再由 slot 和数据源填入报告位置。</div>
      </div>
      <div class="preview-tabs">
        <button class="preview-tab${this.activePreviewTab === 'composition' ? ' active' : ''}" type="button" data-preview-tab="composition">构成图</button>
        <button class="preview-tab${this.activePreviewTab === 'save' ? ' active' : ''}" type="button" data-preview-tab="save">保存预检</button>
        <button class="preview-tab${this.activePreviewTab === 'import' ? ' active' : ''}" type="button" data-preview-tab="import">导入模板</button>
      </div>
      <div class="preview-panes">
        <div class="preview-pane${this.activePreviewTab === 'composition' ? ' active' : ''}" data-preview-pane="composition">
          ${this._renderCompositionMap()}
        </div>
        <div class="preview-pane${this.activePreviewTab === 'save' ? ' active' : ''}" data-preview-pane="save">
          ${this._renderSavePane()}
        </div>
        <div class="preview-pane${this.activePreviewTab === 'import' ? ' active' : ''}" data-preview-pane="import">
          ${this._renderImportPane()}
        </div>
      </div>`;

    // 绑定预览区 tab 切换
    inspector.querySelectorAll('[data-preview-tab]').forEach(tab => {
      tab.onclick = () => this._switchPreviewTab(tab.dataset.previewTab);
    });
    // 绑定导入抽屉按钮
    inspector.querySelectorAll('[data-open-import]').forEach(btn => {
      btn.onclick = () => this._openImportDrawer();
    });
    // 绑定确认保存按钮
    const confirmBtn = inspector.querySelector('[data-confirm-save-pane]');
    if (confirmBtn) confirmBtn.onclick = () => this.showSavePreview();
  }

  /* 切换预览区 tab */
  _switchPreviewTab(name) {
    this.activePreviewTab = name;
    this.bodyEl.querySelectorAll('[data-preview-tab]').forEach(tab => {
      tab.classList.toggle('active', tab.dataset.previewTab === name);
    });
    this.bodyEl.querySelectorAll('[data-preview-pane]').forEach(pane => {
      pane.classList.toggle('active', pane.dataset.previewPane === name);
    });
  }

  _scheduleInspectorRender() {
    window.clearTimeout(this._inspectorTimer);
    this._inspectorTimer = window.setTimeout(() => this.renderInspector(), 120);
  }

  /* 渲染构成图：路由链路 + block 泳道 */
  _renderCompositionMap() {
    const template = this._getDraftTemplate();
    const outputBlocks = template.output_blocks || [];
    const dataContract = template.data_contract || [];
    const qaRules = template.qa_rules || [];
    const blockIds = new Set((this.catalog?.blocks || []).map(block => block.id));
    const requiredSkills = template.required_skills || [];
    const skills = this.catalog?.skills || {};
    const missingBlocks = outputBlocks.filter(id => !blockIds.has(id));
    const missingSkills = requiredSkills.filter(id => !skills[id]?.enabled);

    // 路由链路
    const family = template.report_family || '-';
    const assetType = template.asset_type || '-';
    const templateId = this.currentId || '-';

    let html = `
      <!-- 校验结果摘要 -->
      <div class="composition-board">
        <div class="template-preview-card">
          <div class="template-preview-head">校验结果</div>
          <div class="template-preview-body">
            <div class="template-check">模板 id 与文件路径匹配。</div>
            <div class="template-check${missingBlocks.length ? ' warn' : ''}">${missingBlocks.length ? `缺失 blocks：${this._esc(missingBlocks.join(', '))}` : 'output_blocks 均能找到。'}</div>
            <div class="template-check${missingSkills.length ? ' warn' : ''}">${missingSkills.length ? `未启用技能：${this._esc(missingSkills.join(', '))}` : 'required_skills 均已启用。'}</div>
          </div>
        </div>

        <!-- 路由链路卡片 -->
        <div class="route-card">
          <div class="route-card-head">
            <span>路由链路</span>
            <span class="mini-badge${family !== '-' ? ' green' : ''}">${family !== '-' ? '命中' : '未设置'}</span>
          </div>
          <div class="route-body">
            <div class="route-path">
              <span class="mini-badge">用户问题</span>
              <span class="route-arrow">&rarr;</span>
              <span class="mini-badge green">${this._esc(family)}</span>
              <span class="route-arrow">&rarr;</span>
              <span class="mini-badge blue">${this._esc(assetType)}</span>
              <span class="route-arrow">&rarr;</span>
              <span class="mini-badge orange">${this._esc(templateId)}</span>
            </div>
          </div>
        </div>`;

    // Block 泳道
    outputBlocks.forEach((blockId, idx) => {
      const blockDef = (this.catalog?.blocks || []).find(b => b.id === blockId) || {};
      const blockTitle = blockDef.title || blockId;
      const matchingSlots = this._slotsForBlock(blockId, dataContract);
      // 推导数据源
      const capabilities = new Set();
      for (const slot of matchingSlots) {
        for (const cap of (slot.tool_capabilities || [])) {
          capabilities.add(cap);
        }
      }

      html += `
        <div class="flow-lane">
          <div class="flow-lane-head">
            <div class="lane-index">${idx + 1}</div>
            <div>
              <div class="lane-title">${this._esc(blockTitle)}</div>
              <div class="lane-block">block: ${this._esc(blockId)}</div>
            </div>
            <span class="mini-badge${missingBlocks.includes(blockId) ? ' orange' : ' green'}">${missingBlocks.includes(blockId) ? '缺失' : '可用'}</span>
          </div>
          <div class="lane-body">
            <div class="lane-box">
              <div class="lane-box-title">填入的 slot</div>
              ${matchingSlots.length > 0
                ? matchingSlots.map(s => `
                  <div class="slot-line">
                    <span class="slot-name">${this._esc(s.slot || '-')}</span>
                    <span class="mini-badge${s.hard_required ? ' green' : ' orange'}">${s.hard_required ? '硬性' : '可选'}</span>
                  </div>`).join('')
                  : '<div class="slot-line"><span class="slot-name muted">无需结构化 slot</span><span class="mini-badge">规则驱动</span></div>'}
            </div>
            <div class="lane-box">
              <div class="lane-box-title">数据源</div>
              <div class="source-pills">
                ${capabilities.size > 0
                  ? Array.from(capabilities).map(cap => `<span class="source-pill">${this._esc(cap)}</span>`).join('')
                  : '<span class="source-pill gray">无数据源</span>'}
              </div>
            </div>
          </div>
        </div>`;
    });

    html += '</div>';
    return html;
  }

  _slotsForBlock(blockId, dataContract) {
    const explicit = {
      decision_brief: ['product_quote', 'tracking_asset', 'technical_snapshot', 'stock_snapshot', 'index_snapshot', 'sector_performance'],
      core_summary: ['verdict', 'signal_strength', 'product_quote', 'stock_snapshot', 'index_snapshot'],
      user_context: ['user_position', 'user_position_context', 'risk_preference'],
      instrument_evidence: ['product_quote', 'tracking_asset', 'technical_snapshot', 'capital_flow', 'macro_context'],
      evidence_conflict: ['product_quote', 'capital_flow', 'news_context', 'cross_market_context'],
      action_plan_compact: ['price_levels', 'technical_snapshot', 'scenario_triggers', 'user_position'],
      scenario_matrix_compact: ['scenario_triggers', 'technical_snapshot', 'macro_context'],
      scenario_matrix: ['scenario_triggers', 'technical_snapshot', 'macro_context'],
      risk_warning: ['risk_events', 'news_context', 'audit_opinion', 'capital_flow'],
      next_watch: ['price_levels', 'event_calendar', 'technical_snapshot'],
      market_breadth: ['market_breadth', 'index_snapshot'],
      theme_rotation: ['hot_sectors', 'sector_performance'],
      candidate_ranking: ['candidate_pool', 'growth_factor', 'quality_factor', 'value_factor', 'momentum_factor', 'capital_factor'],
      action_tree: ['price_levels', 'technical_snapshot', 'user_position'],
      position_sizing: ['user_position', 'user_position_context', 'risk_preference'],
    };
    const wanted = explicit[blockId] || [];
    const normalizedBlock = String(blockId || '').toLowerCase();

    const matched = dataContract.filter(slot => {
      const slotName = String(slot.slot || '').toLowerCase();
      const desc = String(slot.description || '').toLowerCase();
      const fields = (slot.fields || []).join(' ').toLowerCase();
      return wanted.includes(slot.slot) || normalizedBlock.includes(slotName) || desc.includes(normalizedBlock) || fields.includes(normalizedBlock);
    });
    return matched.slice(0, 4);
  }

  /* 渲染保存预检 tab pane */
  _renderSavePane() {
    const template = this._getDraftTemplate();
    const outputBlocks = template.output_blocks || [];
    const blockIds = new Set((this.catalog?.blocks || []).map(block => block.id));
    const missingBlocks = outputBlocks.filter(id => !blockIds.has(id));
    const requiredSkills = template.required_skills || [];
    const skills = this.catalog?.skills || {};
    const missingSkills = requiredSkills.filter(id => !skills[id]?.enabled);
    const hasMeta = !!(template.report_family || template.asset_type);

    return `
      <div class="template-preview-card">
        <div class="template-preview-head">结构校验</div>
        <div class="template-preview-body">
          <div class="template-check">模板 id 与文件路径匹配。</div>
          <div class="template-check${missingBlocks.length ? ' warn' : ''}">${missingBlocks.length ? `缺失 blocks：${this._esc(missingBlocks.join(', '))}` : 'output_blocks 均能找到。'}</div>
          <div class="template-check${missingSkills.length ? ' warn' : ''}">${missingSkills.length ? `未启用技能：${this._esc(missingSkills.join(', '))}` : 'required_skills 均已启用。'}</div>
        </div>
      </div>
      <div class="template-preview-card">
        <div class="template-preview-head">兼容提醒</div>
        <div class="template-preview-body">
          <div class="template-check${hasMeta ? '' : ' warn'}">${hasMeta ? '已设置 V2 场景元数据。' : '未设置 report_family / asset_type 等新字段，旧模板读取时会保留未知字段。'}</div>
        </div>
      </div>
      <div class="template-preview-card">
        <div class="template-preview-head">JSON 差异</div>
        <div class="template-preview-body">
          <div class="template-check">点击下方按钮后，将以弹窗展示完整的 JSON diff 列表。</div>
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
        <button class="send-button" type="button" data-confirm-save-pane>确认保存（差异预检）</button>
      </div>`;
  }

  /* 渲染导入模板 tab pane */
  _renderImportPane() {
    return `
      <div class="template-preview-card">
        <div class="template-preview-head">导入模板</div>
        <div class="template-preview-body">
          <div class="import-drop" data-open-import style="cursor:pointer;border:1px dashed var(--line-strong);border-radius:10px;padding:14px;text-align:center;background:var(--surface-soft,#f7fafb);color:#475569;font-size:12px;line-height:1.6;margin-bottom:10px">
            拖入 template.json 或模板 zip<br>支持导入单模板、带 blocks 的模板包、从旧 schema 升级到 2.1
          </div>
          <div style="font-size:12px;color:#64748b;line-height:1.6">
            <div style="margin-bottom:6px"><strong>导入流程：</strong></div>
            <div>1. 读取文件并校验 id、schema、output_blocks、data_contract、qa_rules。</div>
            <div>2. 检查缺失 block、未知 skill、重复 template_id 和路径安全。</div>
            <div>3. 显示导入差异：新增、覆盖、跳过；用户确认后写入模板目录。</div>
          </div>
          <div style="display:flex;justify-content:flex-end;margin-top:12px">
            <button class="send-button" type="button" data-open-import>打开导入抽屉</button>
          </div>
        </div>
      </div>`;
  }

  showSavePreview() {
    if (!this.currentDetail || !this.currentId) return;
    let template;
    try {
      template = this._collectTemplate();
    } catch (err) {
      alert('模板内容无效：' + err.message);
      return;
    }
    const changes = this._getTemplateChanges(this.currentDetail.template || {}, template);
    if (changes.length === 0) {
      alert('没有变更需要保存');
      return;
    }
    this.pendingTemplate = template;
    this.pendingChanges = changes;
    this._renderSavePreview(changes);
    this.diffOverlayEl?.classList.add('open');
    this.diffModalEl?.classList.add('open');
  }

  _getDraftTemplate() {
    if (!this.currentDetail) return {};
    const editor = this.bodyEl.querySelector('.template-editor');
    if (!editor) return this.currentDetail.template || {};
    try {
      return this._collectTemplate();
    } catch {
      return this.currentDetail.template || {};
    }
  }

  async confirmSave() {
    if (!this.currentDetail || !this.currentId || !this.pendingTemplate) return;
    const template = this.pendingTemplate;
    if (this.diffApplyBtn) this.diffApplyBtn.disabled = true;
    try {
      const data = await window.StockRadar.api.saveReportTemplate(this.currentId, template);
      this.currentDetail.template = data.template;
      this.dirty = false;
      this.closeSavePreview();
      this.renderEditor();
      this.renderInspector();
      alert('模板已保存');
    } catch (err) {
      alert('保存失败：' + err.message);
    } finally {
      if (this.diffApplyBtn) this.diffApplyBtn.disabled = false;
    }
  }

  closeSavePreview() {
    this.diffOverlayEl?.classList.remove('open');
    this.diffModalEl?.classList.remove('open');
    this.pendingTemplate = null;
    this.pendingChanges = [];
  }

  _bindDiffModal() {
    document.getElementById('templateDiffCloseBtn')?.addEventListener('click', () => this.closeSavePreview());
    document.getElementById('templateDiffCancelBtn')?.addEventListener('click', () => this.closeSavePreview());
    this.diffOverlayEl?.addEventListener('click', () => this.closeSavePreview());
    this.diffApplyBtn?.addEventListener('click', () => this.confirmSave());
  }

  /* 渲染导入抽屉 HTML */
  _renderImportDrawer() {
    return `
      <div class="drawer-mask" data-import-drawer-mask style="z-index:155"></div>
      <aside class="template-import-drawer" data-import-drawer>
        <div class="drawer-head">
          <div>
            <div class="drawer-title">导入报告模板</div>
            <div class="drawer-subtitle">支持单个 template.json、完整模板 zip，也可以预检旧 schema 并补齐 2.1 元数据。</div>
          </div>
          <button class="secondary-button" type="button" data-close-import>关闭</button>
        </div>
        <div class="drawer-body">
          <div class="import-mode-grid">
            <button class="import-mode-card${this.importMode === 'single' ? ' active' : ''}" type="button" data-import-mode="single">
              <strong>单模板</strong>
              <span>导入一个 template.json，保留现有 blocks。</span>
            </button>
            <button class="import-mode-card${this.importMode === 'bundle' ? ' active' : ''}" type="button" data-import-mode="bundle">
              <strong>模板包</strong>
              <span>导入 template、blocks、rules 的 zip 包。</span>
            </button>
            <button class="import-mode-card${this.importMode === 'upgrade' ? ' active' : ''}" type="button" data-import-mode="upgrade">
              <strong>升级旧版</strong>
              <span>补齐 report_family、dashboard_type 等字段。</span>
            </button>
          </div>

          <div class="upload-box" data-upload-box>
            <strong>选择或拖入文件</strong><br>
            template.json / report-template.zip
          </div>
          <input type="file" data-import-file accept=".json,.zip" style="display:none">

          <div class="import-preview" data-import-preview></div>
        </div>
        <div class="drawer-foot">
          <button class="secondary-button" type="button" data-close-import>取消</button>
          <button class="send-button" type="button" data-confirm-import>预检并导入</button>
        </div>
      </aside>`;
  }

  /* 绑定导入抽屉事件 */
  _bindImportDrawer() {
    const mask = this.bodyEl.querySelector('[data-import-drawer-mask]');
    const closeBtns = this.bodyEl.querySelectorAll('[data-close-import]');
    const modeCards = this.bodyEl.querySelectorAll('[data-import-mode]');
    const uploadBox = this.bodyEl.querySelector('[data-upload-box]');
    const fileInput = this.bodyEl.querySelector('[data-import-file]');
    const confirmBtn = this.bodyEl.querySelector('[data-confirm-import]');

    if (mask) mask.onclick = () => this._closeImportDrawer();
    closeBtns.forEach(btn => btn.onclick = () => this._closeImportDrawer());

    modeCards.forEach(card => {
      card.onclick = () => {
        this.importMode = card.dataset.importMode;
        modeCards.forEach(c => c.classList.toggle('active', c.dataset.importMode === this.importMode));
      };
    });

    if (uploadBox && fileInput) {
      uploadBox.onclick = () => fileInput.click();
      uploadBox.ondragover = (e) => { e.preventDefault(); uploadBox.classList.add('active'); };
      uploadBox.ondragleave = () => uploadBox.classList.remove('active');
      uploadBox.ondrop = (e) => {
        e.preventDefault();
        uploadBox.classList.remove('active');
        if (e.dataTransfer.files.length) this._handleImportFile(e.dataTransfer.files[0]);
      };
      fileInput.onchange = () => {
        if (fileInput.files.length) this._handleImportFile(fileInput.files[0]);
        fileInput.value = '';
      };
    }

    if (confirmBtn) confirmBtn.onclick = () => this._executeImport();
  }

  /* 打开导入抽屉 */
  _openImportDrawer() {
    const mask = this.bodyEl.querySelector('[data-import-drawer-mask]');
    const drawer = this.bodyEl.querySelector('[data-import-drawer]');
    mask?.classList.add('open');
    drawer?.classList.add('open');
    this.importDrawerOpen = true;
    this._switchPreviewTab('import');
  }

  /* 关闭导入抽屉 */
  _closeImportDrawer() {
    const mask = this.bodyEl.querySelector('[data-import-drawer-mask]');
    const drawer = this.bodyEl.querySelector('[data-import-drawer]');
    mask?.classList.remove('open');
    drawer?.classList.remove('open');
    this.importDrawerOpen = false;
  }

  /* 处理导入文件 */
  _handleImportFile(file) {
    const preview = this.bodyEl.querySelector('[data-import-preview]');
    if (!preview) return;
    const uploadBox = this.bodyEl.querySelector('[data-upload-box]');
    if (uploadBox) uploadBox.innerHTML = `<strong>已选择文件</strong><br>${this._esc(file.name)} (${(file.size / 1024).toFixed(1)} KB)`;

    const reader = new FileReader();
    reader.onload = () => {
      try {
        const content = reader.result;
        if (file.name.endsWith('.json')) {
          const data = JSON.parse(content);
          const id = data.template_id || data.id || file.name.replace('.json', '');
          preview.innerHTML = `
            <div class="template-check">文件结构：检测到 <strong>${this._esc(id)}/template.json</strong>。</div>
            <div class="template-check">output_blocks: ${(data.output_blocks || []).length} 个。</div>
            <div class="template-check">data_contract: ${(data.data_contract || []).length} slots。</div>
            <div class="template-check">qa_rules: ${(data.qa_rules || []).length} 条。</div>`;
          this._pendingImportData = data;
          this._pendingImportId = id;
        } else {
          preview.innerHTML = '<div class="template-check warn">zip 包解析暂未实现，请使用单模板 JSON 导入。</div>';
        }
      } catch (err) {
        preview.innerHTML = `<div class="template-check warn">文件解析失败：${this._esc(err.message)}</div>`;
      }
    };
    reader.readAsText(file);
  }

  /* 执行导入 */
  async _executeImport() {
    if (!this._pendingImportData || !this._pendingImportId) {
      alert('请先选择要导入的文件。');
      return;
    }
    try {
      // 使用专用导入接口（会更新 index.json）
      const resp = await fetch('/api/report-templates/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template: this._pendingImportData }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || '导入失败');
      }
      const result = await resp.json();
      this._closeImportDrawer();
      this._pendingImportData = null;
      this._pendingImportId = null;
      await this.loadCatalog();
      const action = result.action === 'create' ? '新增' : '覆盖';
      alert(`模板导入成功（${action}）：${result.id}${result.conflicts?.length ? '\n注意：' + result.conflicts.join('；') : ''}`);
    } catch (err) {
      alert('导入失败：' + err.message);
    }
  }

  _renderSavePreview(changes) {
    if (!this.diffBodyEl) return;
    if (this.diffSummaryEl) {
      this.diffSummaryEl.textContent = `${this.currentId} · ${changes.length} 项变更`;
    }
    this.diffBodyEl.innerHTML = changes.map(change => `
      <div class="template-diff-row">
        <div class="template-diff-key">${this._esc(change.label)}</div>
        <div class="template-diff-columns">
          <div>
            <div class="template-diff-caption">原始值</div>
            <pre class="template-diff-value old">${this._esc(this._formatDiffValue(change.oldValue))}</pre>
          </div>
          <div>
            <div class="template-diff-caption">待保存</div>
            <pre class="template-diff-value new">${this._esc(this._formatDiffValue(change.newValue))}</pre>
          </div>
        </div>
      </div>`).join('');
  }

  _getTemplateChanges(before, after) {
    const labels = {
      name: '模板名称',
      version: '版本',
      role: '分析师角色提示',
      default_horizon: '默认周期',
      required_skills: '必需技能',
      fallback_skills: '备用技能',
      supported_horizons: '支持周期',
      output_blocks: '输出结构',
      data_contract: '数据契约',
      qa_rules: '质量规则',
      report_family: '主场景',
      asset_type: '资产类型',
      intent_type: '意图类型',
      dashboard_type: '仪表盘类型',
      verbosity: '输出密度',
      max_report_words: '字数预算',
    };
    const keys = Array.from(new Set([...Object.keys(before || {}), ...Object.keys(after || {})]));
    return keys
      .filter(key => this._stableStringify(before?.[key]) !== this._stableStringify(after?.[key]))
      .map(key => ({
        key,
        label: labels[key] || key,
        oldValue: before?.[key],
        newValue: after?.[key],
      }));
  }

  _stableStringify(value) {
    if (value === undefined) return '__undefined__';
    return JSON.stringify(value);
  }

  _formatDiffValue(value) {
    if (value === undefined) return '(未设置)';
    if (value === null || value === '') return '(空)';
    if (Array.isArray(value) || typeof value === 'object') {
      return JSON.stringify(value, null, 2);
    }
    return String(value);
  }

  _collectTemplate() {
    const template = JSON.parse(JSON.stringify(this.currentDetail.template || {}));
    const editor = this.bodyEl.querySelector('.template-editor');
    const value = (name) => editor.querySelector(`[data-field="${name}"]`)?.value ?? '';
    const selectedHorizon = editor.querySelector('input[name="template-default-horizon"]:checked')?.value || 'short';
    template.name = value('name').trim();
    template.version = value('version').trim();
    template.role = value('role').trim();
    template.default_horizon = selectedHorizon;
    template.required_skills = this._splitList(value('required_skills'));
    template.fallback_skills = this._splitList(value('fallback_skills'));
    template.supported_horizons = this._splitList(value('supported_horizons'));
    template.output_blocks = Array.from(editor.querySelectorAll('[data-block-id]:checked'))
      .map(input => input.dataset.blockId);
    template.qa_rules = value('qa_rules').split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    const dataContractRaw = value('data_contract').trim();
    template.data_contract = dataContractRaw ? JSON.parse(dataContractRaw) : [];
    // V2 场景元数据
    template.report_family = value('report_family') || undefined;
    template.asset_type = value('asset_type') || undefined;
    template.intent_type = value('intent_type') || undefined;
    template.dashboard_type = value('dashboard_type') || undefined;
    template.verbosity = value('verbosity') || undefined;
    const maxWords = value('max_report_words').trim();
    template.max_report_words = maxWords ? Number(maxWords) : undefined;
    return template;
  }

  _splitList(text) {
    return String(text || '').split(',').map(item => item.trim()).filter(Boolean);
  }

  _field(label, key, value) {
    return `
      <div class="template-field">
        <div class="template-label">${this._esc(label)}<small>${this._esc(key)}</small></div>
        <input class="template-input" data-field="${this._esc(key)}" value="${this._esc(value)}">
      </div>`;
  }

  _horizonField(value) {
    const options = [
      ['short', '短线 · 1-5 个交易日'],
      ['mid', '中线 · 1-3 个月'],
      ['long', '长线 · 3 个月以上'],
    ];
    return `
      <div class="template-field">
        <div class="template-label">默认周期<small>default_horizon</small></div>
        <div class="template-horizon-options" data-field="default_horizon">
          ${options.map(([key, label]) => `
            <label class="template-horizon-option${key === value ? ' active' : ''}">
              <input type="radio" name="template-default-horizon" value="${this._esc(key)}"${key === value ? ' checked' : ''}>
              <span>${this._esc(label)}</span>
            </label>`).join('')}
        </div>
      </div>`;
  }

  _renderBlockPicker(outputBlocks) {
    const active = new Set(outputBlocks || []);
    const knownBlocks = this.catalog?.blocks || [];
    const merged = [];
    for (const id of outputBlocks || []) {
      const block = knownBlocks.find(item => item.id === id) || { id, title: id };
      merged.push(block);
    }
    for (const block of knownBlocks) {
      if (!active.has(block.id)) merged.push(block);
    }
    return `
      <div class="template-block-picker" data-field="output_blocks">
        ${merged.map(block => {
          const checked = active.has(block.id);
          return `
            <label class="template-block-option${checked ? ' active' : ''}">
              <input type="checkbox" data-block-id="${this._esc(block.id)}"${checked ? ' checked' : ''}>
              <span class="template-block-title">${this._esc(block.title || block.id)}</span>
              <span class="template-block-id">${this._esc(block.id)}</span>
            </label>`;
        }).join('')}
      </div>`;
  }

  /* 渲染 select 字段 */
  _selectField(label, key, value, options) {
    const hasCurrentValue = value !== '' && value != null && options.some(([val]) => val === value);
    return `
      <div class="template-field">
        <div class="template-label">${this._esc(label)}<small>${this._esc(key)}</small></div>
        <select class="template-select" data-field="${this._esc(key)}">
          <option value="">-- 未设置 --</option>
          ${hasCurrentValue ? '' : `<option value="${this._esc(value)}" selected>${this._esc(value)} · 当前值</option>`}
          ${options.map(([val, text]) => `<option value="${this._esc(val)}"${val === value ? ' selected' : ''}>${this._esc(text)}</option>`).join('')}
        </select>
      </div>`;
  }

  _textareaField(label, key, value, rows = 5) {
    return `
      <div class="template-field full">
        <div class="template-label">${this._esc(label)}<small>${this._esc(key)}</small></div>
        <textarea class="template-textarea" rows="${rows}" data-field="${this._esc(key)}">${this._esc(value)}</textarea>
      </div>`;
  }

  _renderDataContractTable(items) {
    if (!items.length) return '<div class="template-manager-state">暂无数据契约。</div>';
    return `
      <table class="template-table">
        <thead><tr><th>slot</th><th>字段</th><th>能力</th><th>要求</th></tr></thead>
        <tbody>
          ${items.slice(0, 6).map(item => `
            <tr>
              <td>${this._esc(item.slot || '-')}</td>
              <td>${this._esc((item.fields || []).join('、'))}</td>
              <td>${this._esc((item.tool_capabilities || []).join(', '))}</td>
              <td>${item.hard_required ? '必需' : '可选'}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

  _blockTitle(id) {
    const block = (this.catalog?.blocks || []).find(item => item.id === id);
    return block?.title || id;
  }

  _filterTemplates(query) {
    const q = String(query || '').trim().toLowerCase();
    const family = this.activeFamily;
    this.bodyEl.querySelectorAll('.template-list-item').forEach(btn => {
      const text = btn.textContent.toLowerCase();
      const matchSearch = !q || text.includes(q);
      const btnFamily = btn.dataset.family || '';
      const matchFamily = !family || btnFamily === family;
      btn.style.display = matchSearch && matchFamily ? '' : 'none';
    });
  }

  _familyForTemplate(item) {
    if (!item) return '';
    const known = new Set(FAMILY_DEFS.map(f => f.id));
    const raw = item.report_family || item.family || '';
    if (known.has(raw)) return raw;
    if (FAMILY_BY_TEMPLATE[item.id]) return FAMILY_BY_TEMPLATE[item.id];
    if (FAMILY_BY_TEMPLATE[raw]) return FAMILY_BY_TEMPLATE[raw];
    const dashboard = item.dashboard_type || '';
    if (dashboard === 'market') return 'market';
    if (dashboard === 'sector') return 'sector';
    if (dashboard === 'screening') return 'screening';
    const asset = item.asset_type || '';
    if (['stock', 'etf', 'index', 'commodity', 'fund', 'portfolio', 'instrument'].includes(asset)) {
      return 'instrument';
    }
    return '';
  }

  _updateSidebarActive() {
    this.bodyEl.querySelectorAll('.template-list-item').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.templateId === this.currentId);
    });
  }

  _setState(message) {
    this.bodyEl.innerHTML = `<div class="template-manager-state">${this._esc(message)}</div>`;
  }

  _esc(value) {
    const d = document.createElement('div');
    d.textContent = value == null ? '' : String(value);
    return d.innerHTML;
  }
};
