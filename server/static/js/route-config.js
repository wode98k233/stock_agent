/*
   选股雷达 Web — 模板路由配置面板
   三列布局：左侧模板列表 / 中间关键词编辑器 / 右侧检查面板
*/

window.StockRadar = window.StockRadar || {};

/* eslint-disable no-undef */
/* FAMILY_DEFS / FAMILY_BY_TEMPLATE 由 template-manager.js 在全局作用域定义 */
/* eslint-enable no-undef */

window.StockRadar.RouteConfigPanel = class RouteConfigPanel {
  constructor() {
    /* ── 状态 ── */
    this.rules = {};          // {template_id: [[keyword, weight], ...]}
    this.snapshot = {};       // 已保存副本，用于脏检测
    this.dirty = new Set();   // 有未保存变更的 template_id 集合
    this.currentId = null;    // 当前选中的模板 id
    this.catalog = null;      // getReportTemplates() 返回的完整目录
    this.activeFamily = null; // 当前场景族筛选，null = 全部

    /* ── 绑定顶栏按钮 ── */
    this._('rcCloseBtn').onclick = () => this.close();
    this._('rcOverlay').onclick = () => this.close();
    this._('rcTestBtn').onclick = () => this.openTest();
    this._('rcSaveAllBtn').onclick = () => this.saveAll();
    this._('rcTestCloseBtn').onclick = () => this.closeTest();
    this._('rcTestOverlay').onclick = (e) => {
      if (e.target === this._('rcTestOverlay')) this.closeTest();
    };
    this._('rcTestRunBtn').onclick = () => this.runTest();
    this._('rcTestInput').onkeydown = (e) => {
      if (e.key === 'Enter') this.runTest();
    };
  }

  /* ── DOM 快捷方式 ── */
  _(id) { return document.getElementById(id); }

  /* ── HTML 转义 ── */
  _esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  /* ═══════════════════════════════════════════════════
     面板开关
     ═══════════════════════════════════════════════════ */

  open() {
    /* 关闭模板管理面板，避免两个面板重叠 */
    const tmPanel = document.getElementById('templateManagerPanel');
    const tmOverlay = document.getElementById('templateManagerOverlay');
    if (tmPanel) tmPanel.classList.remove('open');
    if (tmOverlay) tmOverlay.classList.remove('open');

    this._('rcPanel').classList.add('open');
    this._('rcOverlay').classList.add('open');
    if (!this.catalog) {
      this.loadCatalog();
    } else {
      /* 已有数据，直接渲染（确保搜索框事件绑定） */
      this.renderSidebar();
    }
  }

  close() {
    this._('rcPanel').classList.remove('open');
    this._('rcOverlay').classList.remove('open');
  }

  /* ═══════════════════════════════════════════════════
     数据加载
     ═══════════════════════════════════════════════════ */

  async loadCatalog() {
    this._('rcTplList').innerHTML =
      '<div class="rc-empty-hint">正在加载...</div>';
    try {
      const [catalog, routeData] = await Promise.all([
        window.StockRadar.api.getReportTemplates(),
        window.StockRadar.api.getRouteRules(),
      ]);

      this.catalog = catalog;

      /* 构建 rules / snapshot */
      const templates = catalog.templates || [];
      const routeRules = (routeData && routeData.rules) ? routeData.rules : (routeData || {});

      this.rules = {};
      this.snapshot = {};
      this.dirty.clear();

      for (const tpl of templates) {
        const kws = routeRules[tpl.id] || [];
        this.rules[tpl.id] = kws.map(([k, w]) => [k, w]);
        this.snapshot[tpl.id] = kws.map(([k, w]) => [k, w]);
      }

      this.renderSidebar();

      /* 绑定搜索框事件（必须在 renderSidebar 之后） */
      const searchEl = this._('rcSearch');
      if (searchEl) {
        searchEl.oninput = () => this._renderTemplateList();
      }

      /* 自动选中第一个模板 */
      const first = templates[0];
      if (first) this.selectTemplate(first.id);
    } catch (err) {
      this._('rcTplList').innerHTML =
        '<div class="rc-empty-hint">加载失败: ' + this._esc(err.message) + '</div>';
    }
  }

  /* ═══════════════════════════════════════════════════
     模板选择
     ═══════════════════════════════════════════════════ */

  selectTemplate(id) {
    if (!id) return;
    this.currentId = id;
    this._updateSidebarActive();

    const templates = (this.catalog && this.catalog.templates) || [];
    const template = templates.find((t) => t.id === id);
    if (!template) return;

    this._('rcEmptyHint').style.display = 'none';
    this._('rcEditorContent').style.display = 'block';
    this.renderEditor(template);
    this.renderInspector(template);
  }

  /* ═══════════════════════════════════════════════════
     侧边栏渲染
     ═══════════════════════════════════════════════════ */

  renderSidebar() {
    this._renderFamilyTabs();
    this._renderTemplateList();
  }

  _renderFamilyTabs() {
    const templates = (this.catalog && this.catalog.templates) || [];
    const counts = {};
    for (const f of FAMILY_DEFS) counts[f.id] = 0;
    for (const tpl of templates) {
      const family = this._familyForTemplate(tpl);
      if (family && counts[family] !== undefined) counts[family]++;
    }

    let html = '<button class="rc-family-tab' +
      (!this.activeFamily ? ' active' : '') +
      '" type="button" data-family="">' +
      '<div class="rc-family-name">全部 <span class="rc-family-count">' +
      templates.length + '</span></div></button>';

    for (const f of FAMILY_DEFS) {
      html += '<button class="rc-family-tab' +
        (this.activeFamily === f.id ? ' active' : '') +
        '" type="button" data-family="' + this._esc(f.id) + '">' +
        '<div class="rc-family-name">' + this._esc(f.label) +
        ' <span class="rc-family-count">' + counts[f.id] + '</span></div>' +
        '<div class="rc-family-id">' + this._esc(f.id) + '</div></button>';
    }

    this._('rcFamilyTabs').innerHTML = html;

    /* 绑定场景族切换 */
    this._('rcFamilyTabs').querySelectorAll('[data-family]').forEach((tab) => {
      tab.onclick = () => {
        const fam = tab.dataset.family || null;
        this.activeFamily = fam;
        this.renderSidebar();
      };
    });
  }

  _renderTemplateList() {
    const templates = (this.catalog && this.catalog.templates) || [];
    const query = (this._('rcSearch') && this._('rcSearch').value || '').trim().toLowerCase();
    const list = this._('rcTplList');
    let count = 0;
    const rows = [];

    for (const tpl of templates) {
      const family = this._familyForTemplate(tpl);
      if (this.activeFamily && family !== this.activeFamily) continue;

      // 搜索：匹配 name, id, description, 关键词, family, asset_type, dashboard_type
      if (query) {
        const searchFields = [
          tpl.name || '',
          tpl.id || '',
          tpl.description || '',
          (this.rules[tpl.id] || []).map(kw => kw[0]).join(' '),
          tpl.report_family || '',
          tpl.asset_type || '',
          tpl.dashboard_type || '',
          tpl.intent_type || '',
        ].join(' ').toLowerCase();
        if (!searchFields.includes(query)) continue;
      }

      const isSel = tpl.id === this.currentId;
      const isD = this.dirty.has(tpl.id);
      const kws = this.rules[tpl.id] || [];
      count++;

      rows.push(
        '<button class="rc-template-item' + (isSel ? ' active' : '') +
        (isD ? ' dirty' : '') + '" type="button" data-template-id="' +
        this._esc(tpl.id) + '" data-family="' + this._esc(family || '') + '">' +
        '<div class="rc-template-head">' +
        '<div class="rc-template-name">' + this._esc(tpl.name || tpl.id) + '</div>' +
        '<span class="rc-dot ' + (isD ? 'orange' : 'green') + '"></span>' +
        '</div>' +
        '<div class="rc-template-id">' + this._esc(tpl.id) + '</div>' +
        '<div class="rc-template-desc">' + this._esc(tpl.description || '') + '</div>' +
        (kws.length ? '<div class="rc-template-meta">' + kws.map(kw => '<span class="rc-kw-tag">' + this._esc(kw[0]) + '</span>').join('') + '</div>' : '') +
        '<div class="rc-template-meta">' +
        '<span class="rc-kw-count">' + kws.length + ' 个关键词</span>' +
        (isD ? '<span class="rc-dirty-badge">未保存</span>' : '') +
        '</div></button>'
      );
    }

    list.innerHTML = rows.join('') || '<div class="rc-empty-hint">无匹配模板</div>';

    /* 更新侧栏标题 */
    const familyDef = this.activeFamily
      ? FAMILY_DEFS.find((f) => f.id === this.activeFamily)
      : null;
    this._('rcSideHeading').textContent = familyDef ? familyDef.label : '全部模板';
    this._('rcSideCount').textContent = count + ' 个';

    /* 绑定模板点击 */
    list.querySelectorAll('[data-template-id]').forEach((btn) => {
      btn.onclick = () => this.selectTemplate(btn.dataset.templateId);
    });
  }

  _updateSidebarActive() {
    this._('rcTplList').querySelectorAll('[data-template-id]').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.templateId === this.currentId);
    });
  }

  /* ═══════════════════════════════════════════════════
     编辑器渲染
     ═══════════════════════════════════════════════════ */

  renderEditor(template) {
    const id = template.id;
    const kws = this.rules[id] || [];
    const maxW = kws.length ? Math.max(...kws.map((k) => k[1])) : 1;
    const totalW = kws.reduce((s, k) => s + k[1], 0);
    const avgW = kws.length ? Math.round(totalW / kws.length) : 0;
    const isD = this.dirty.has(id);
    const family = this._familyForTemplate(template);
    const familyLabel = FAMILY_DEFS.find((f) => f.id === family)
      ? FAMILY_DEFS.find((f) => f.id === family).label
      : (family || '');

    const el = this._('rcEditorContent');

    /* 表格行 */
    const rows = kws.map((kw, i) => this._kwRow(i, kw[0], kw[1], maxW)).join('');

    el.innerHTML =
      /* 编辑器头部 */
      '<div class="rc-editor-head">' +
        '<div>' +
          '<div class="rc-editor-title">' + this._esc(template.name || id) + '</div>' +
          '<div class="rc-editor-meta">' + this._esc(template.description || '') + '</div>' +
          '<div class="rc-editor-pills">' +
            '<span class="rc-pill sm">' + this._esc(id) + '</span>' +
            (familyLabel ? '<span class="rc-pill sm">' + this._esc(familyLabel) + '</span>' : '') +
          '</div>' +
        '</div>' +
        '<div class="rc-status-strip">' +
          (isD
            ? '<button class="rc-btn sm save-btn-dirty" type="button" data-save-one>未保存 · 点击保存</button>'
            : '<span class="rc-pill green sm">已保存</span>') +
        '</div>' +
      '</div>' +

      /* 指标面板 */
      '<div class="rc-summary-band">' +
        '<div class="rc-metric"><span>关键词数</span><strong>' + kws.length + '</strong></div>' +
        '<div class="rc-metric"><span>总权重</span><strong>' + totalW + '</strong></div>' +
        '<div class="rc-metric"><span>平均权重</span><strong>' + avgW + '</strong></div>' +
        '<div class="rc-metric"><span>最高权重</span><strong>' + maxW + '</strong></div>' +
      '</div>' +

      /* 关键词分区 */
      '<div class="rc-section">' +
        '<div class="rc-section-head" data-toggle-section>' +
          '<div>' +
            '<div class="rc-section-title">路由关键词</div>' +
            '<div class="rc-section-desc">编辑当前模板的匹配关键词和权重，权重越高匹配优先级越高。</div>' +
          '</div>' +
          '<span class="rc-count">' + kws.length + ' 项</span>' +
        '</div>' +
        '<div class="rc-section-body">' +
          '<table class="rc-table">' +
            '<thead><tr>' +
              '<th style="width:32%">关键词</th>' +
              '<th style="width:12%">权重</th>' +
              '<th style="width:46%">权重分布</th>' +
              '<th style="width:10%">操作</th>' +
            '</tr></thead>' +
            '<tbody id="rcKwBody">' + rows + '</tbody>' +
          '</table>' +
          '<div style="padding:10px 0 4px">' +
            '<button class="rc-btn sm" type="button" data-add-kw>+ 添加关键词</button>' +
          '</div>' +
          '<div class="rc-tip">' +
            '<strong>保存方式：</strong>编辑后点击顶部状态按钮可保存当前模板；点击「保存全部」可批量保存所有模板的修改。' +
          '</div>' +
        '</div>' +
      '</div>';

    /* ── 绑定事件 ── */

    /* 保存按钮 */
    const saveBtn = el.querySelector('[data-save-one]');
    if (saveBtn) saveBtn.onclick = () => this.saveOne();

    /* 添加关键词 */
    const addBtn = el.querySelector('[data-add-kw]');
    if (addBtn) addBtn.onclick = () => this.addKw();

    /* 折叠切换 */
    const sectionHead = el.querySelector('[data-toggle-section]');
    if (sectionHead) {
      sectionHead.onclick = () => {
        const body = sectionHead.nextElementSibling;
        if (body) body.classList.toggle('collapsed');
      };
    }

    /* 关键词输入 */
    el.querySelectorAll('#rcKwBody .rc-kw-key').forEach((input) => {
      input.onchange = () => {
        this.editKw(parseInt(input.dataset.index, 10), 'k', input.value);
      };
    });
    el.querySelectorAll('#rcKwBody .rc-kw-weight').forEach((input) => {
      input.onchange = () => {
        this.editKw(parseInt(input.dataset.index, 10), 'w', input.value);
      };
    });
    el.querySelectorAll('#rcKwBody .rc-kw-remove').forEach((btn) => {
      btn.onclick = () => {
        this.removeKw(parseInt(btn.dataset.index, 10));
      };
    });
  }

  /* ── 单行关键词 HTML ── */
  _kwRow(idx, keyword, weight, maxW) {
    const pct = Math.round((weight / maxW) * 100);
    /* 权重颜色阈值 */
    let color;
    if (weight >= 40) color = '#b42318';
    else if (weight >= 25) color = '#b7791f';
    else if (weight >= 10) color = '#176b5b';
    else color = '#15803d';

    return '<tr>' +
      '<td><input class="rc-input rc-kw-key" data-index="' + idx +
        '" value="' + this._esc(keyword) + '"></td>' +
      '<td><input class="rc-input weight rc-kw-weight" data-index="' + idx +
        '" type="number" min="1" max="100" value="' + weight + '"></td>' +
      '<td>' +
        '<div class="rc-weight-bar">' +
          '<div class="rc-weight-track"><div class="rc-weight-fill" style="width:' +
            pct + '%;background:' + color + '"></div></div>' +
          '<span class="rc-weight-label">' + weight + '</span>' +
        '</div>' +
      '</td>' +
      '<td>' +
        '<button class="rc-btn sm danger rc-kw-remove" data-index="' + idx +
          '" title="删除">删除</button>' +
      '</td>' +
    '</tr>';
  }

  /* ═══════════════════════════════════════════════════
     右侧检查面板
     ═══════════════════════════════════════════════════ */

  renderInspector(template) {
    const id = template.id;
    const kws = this.rules[id] || [];
    const maxW = kws.length ? Math.max(...kws.map((k) => k[1])) : 1;
    const top5 = [...kws].sort((a, b) => b[1] - a[1]).slice(0, 5);

    /* 同族模板 */
    const family = this._familyForTemplate(template);
    const templates = (this.catalog && this.catalog.templates) || [];
    const siblings = templates.filter(
      (t) => t.id !== id && this._familyForTemplate(t) === family
    );

    let html = '';

    /* ── 权重 TOP 5 ── */
    html += '<div class="rc-preview-panel">' +
      '<div class="rc-preview-head">权重 TOP 5</div>' +
      '<div class="rc-preview-body">';

    if (top5.length) {
      html += top5.map(([k, w]) => {
        const pct = Math.round((w / maxW) * 100);
        return '<div class="rc-top5-item">' +
          '<span class="rc-top5-name">' + this._esc(k) + '</span>' +
          '<div class="rc-top5-bar"><div style="width:' + pct + '%"></div></div>' +
          '<span class="rc-top5-label">' + w + '</span>' +
        '</div>';
      }).join('');
    } else {
      html += '<div style="padding:8px 0;color:var(--muted);font-size:12px">暂无关键词</div>';
    }

    html += '</div></div>';

    /* ── 同族模板 ── */
    html += '<div class="rc-preview-panel">' +
      '<div class="rc-preview-head">同族模板</div>' +
      '<div class="rc-preview-body">';

    if (siblings.length) {
      html += '<div class="rc-family-list">' +
        siblings.map((t) => {
          return '<div class="rc-family-item">' +
            this._esc(t.name || t.id) +
            ' <span style="color:#94a3b8;font-family:var(--mono);font-size:11px">' +
            this._esc(t.id) + '</span></div>';
        }).join('') +
      '</div>';
    } else {
      html += '<div style="padding:8px 0;color:var(--muted);font-size:12px">无同族模板</div>';
    }

    html += '</div></div>';

    this._('rcInspectorDynamic').innerHTML = html;
  }

  /* ═══════════════════════════════════════════════════
     关键词编辑操作
     ═══════════════════════════════════════════════════ */

  editKw(index, field, value) {
    const id = this.currentId;
    if (!id || !this.rules[id]) return;

    if (field === 'k') {
      this.rules[id][index][0] = value;
    } else {
      this.rules[id][index][1] = Math.max(1, Math.min(100, parseInt(value, 10) || 1));
    }

    this._refreshDirty();
    this._renderTemplateList();

    const template = this._templateById(id);
    if (template) this.renderEditor(template);
  }

  addKw() {
    const id = this.currentId;
    if (!id) return;
    if (!this.rules[id]) this.rules[id] = [];

    this.rules[id].push(['新关键词', 10]);
    this._refreshDirty();
    this._renderTemplateList();

    const template = this._templateById(id);
    if (template) this.renderEditor(template);

    /* 滚动到新增行 */
    const body = this._('rcKwBody');
    if (body && body.lastElementChild) {
      body.lastElementChild.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  removeKw(index) {
    const id = this.currentId;
    if (!id || !this.rules[id]) return;

    this.rules[id].splice(index, 1);
    this._refreshDirty();
    this._renderTemplateList();

    const template = this._templateById(id);
    if (template) this.renderEditor(template);
  }

  /* ═══════════════════════════════════════════════════
     脏检测与保存
     ═══════════════════════════════════════════════════ */

  _refreshDirty() {
    this.dirty.clear();
    for (const id of Object.keys(this.rules)) {
      if (JSON.stringify(this.rules[id]) !== JSON.stringify(this.snapshot[id])) {
        this.dirty.add(id);
      }
    }
  }

  async saveOne() {
    const id = this.currentId;
    if (!id) return;

    try {
      const keywords = this.rules[id] || [];
      await window.StockRadar.api.saveRouteRules(id, keywords);
      this.snapshot[id] = keywords.map(([k, w]) => [k, w]);
      this._refreshDirty();
      this._renderTemplateList();

      const template = this._templateById(id);
      if (template) this.renderEditor(template);

      this._toast('已保存「' + (template ? template.name : id) + '」路由规则');
    } catch (err) {
      alert('保存失败: ' + err.message);
    }
  }

  async saveAll() {
    const ids = [...this.dirty];
    if (!ids.length) {
      this._toast('没有需要保存的修改');
      return;
    }

    let success = 0;
    let failed = 0;

    for (const id of ids) {
      try {
        const keywords = this.rules[id] || [];
        await window.StockRadar.api.saveRouteRules(id, keywords);
        this.snapshot[id] = keywords.map(([k, w]) => [k, w]);
        success++;
      } catch (_) {
        failed++;
      }
    }

    this._refreshDirty();
    this._renderTemplateList();

    if (this.currentId) {
      const template = this._templateById(this.currentId);
      if (template) this.renderEditor(template);
    }

    this._toast(
      failed
        ? '已保存 ' + success + ' 个，' + failed + ' 个失败'
        : '已保存全部 ' + success + ' 个模板路由规则'
    );
  }

  /* ═══════════════════════════════════════════════════
     路由测试弹窗
     ═══════════════════════════════════════════════════ */

  openTest() {
    this._('rcTestOverlay').classList.add('open');
    setTimeout(() => {
      const input = this._('rcTestInput');
      if (input) input.focus();
    }, 100);
  }

  closeTest() {
    this._('rcTestOverlay').classList.remove('open');
  }

  async runTest() {
    const text = this._('rcTestInput') && this._('rcTestInput').value.trim();
    if (!text) return;

    try {
      const data = await window.StockRadar.api.routeTest(text);
      this.renderTestResults(data);
    } catch (err) {
      this._('rcTestResults').innerHTML =
        '<div class="rc-empty-hint">测试失败: ' + this._esc(err.message) + '</div>';
    }
  }

  renderTestResults(data) {
    /* 兼容多种返回格式 */
    const results = Array.isArray(data) ? data
      : (data && data.results) ? data.results
      : (data && data.scores) ? data.scores
      : [];

    const winners = results.filter((r) => (r.score || 0) > 0);
    const zeros = results.filter((r) => (r.score || 0) === 0);

    let html = '<div class="rc-results">';

    if (winners.length) {
      winners.forEach((r, i) => {
        const isWinner = i === 0;
        const matched = r.matched_keywords || r.matched || [];
        const name = r.name || r.template_name || r.id || r.template_id || '-';
        const tid = r.id || r.template_id || '';

        html += '<div class="rc-result-card' + (isWinner ? ' winner' : '') + '">' +
          '<div class="rc-result-rank ' + (isWinner ? 'gold' : 'normal') + '">' +
            (isWinner ? '&#127942;' : (i + 1)) +
          '</div>' +
          '<div class="rc-result-info">' +
            '<div class="rc-result-name">' + this._esc(name) + '</div>' +
            '<div class="rc-result-id">' + this._esc(tid) + '</div>' +
            '<div class="rc-result-kws">' +
              matched.map(([k, w]) =>
                '<span class="rc-kw-tag">' + this._esc(k) + ' +' + w + '</span>'
              ).join('') +
            '</div>' +
          '</div>' +
          '<div class="rc-result-score">' + r.score +
            '<span class="unit"> 分</span>' +
          '</div>' +
        '</div>';
      });
    } else {
      html += '<div class="rc-empty-hint">未匹配任何模板关键词</div>';
    }

    if (zeros.length) {
      html += '<div class="rc-zero-group">' +
        '<div class="rc-zero-header" data-toggle-zero>' +
          '<span>其他 ' + zeros.length + ' 个模板 (0 分)</span>' +
          '<span style="font-size:11px">&#9654;</span>' +
        '</div>' +
        '<div class="rc-zero-list">' +
          zeros.map((r) => {
            const name = r.name || r.template_name || r.id || r.template_id || '-';
            const tid = r.id || r.template_id || '';
            return '<div class="rc-zero-item">' + this._esc(name) + ' (' + this._esc(tid) + ')</div>';
          }).join('') +
        '</div>' +
      '</div>';
    }

    html += '</div>';
    html += '<div class="rc-default-hint">未命中任何关键词时将使用默认模板: <strong>standard</strong></div>';

    this._('rcTestResults').innerHTML = html;

    /* 绑定零分组展开/折叠 */
    this._('rcTestResults').querySelectorAll('[data-toggle-zero]').forEach((header) => {
      header.onclick = () => {
        const list = header.nextElementSibling;
        if (list) list.classList.toggle('open');
      };
    });
  }

  /* ═══════════════════════════════════════════════════
     辅助方法
     ═══════════════════════════════════════════════════ */

  _templateById(id) {
    const templates = (this.catalog && this.catalog.templates) || [];
    return templates.find((t) => t.id === id) || null;
  }

  _familyForTemplate(tpl) {
    if (!tpl) return '';
    /* 优先使用模板自身的 report_family */
    const known = new Set(FAMILY_DEFS.map((f) => f.id));
    const raw = tpl.report_family || tpl.family || '';
    if (known.has(raw)) return raw;

    /* 回退到全局映射表 */
    if (typeof FAMILY_BY_TEMPLATE !== 'undefined' && FAMILY_BY_TEMPLATE[tpl.id]) {
      return FAMILY_BY_TEMPLATE[tpl.id];
    }
    if (typeof FAMILY_BY_TEMPLATE !== 'undefined' && FAMILY_BY_TEMPLATE[raw]) {
      return FAMILY_BY_TEMPLATE[raw];
    }

    /* 根据 dashboard_type / asset_type 推断 */
    const dashboard = tpl.dashboard_type || '';
    if (dashboard === 'market' || dashboard === 'hot_events') return 'market';
    if (dashboard === 'sector') return 'sector';
    if (dashboard === 'screening') return 'screening';
    const asset = tpl.asset_type || '';
    if (['stock', 'etf', 'index', 'commodity', 'fund', 'portfolio', 'instrument'].includes(asset)) {
      return 'instrument';
    }
    return '';
  }

  _toast(msg) {
    const toast = document.createElement('div');
    toast.style.cssText =
      'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);' +
      'padding:10px 20px;background:var(--brand);color:#fff;border-radius:8px;' +
      'font-size:13px;font-weight:800;z-index:999;' +
      'box-shadow:0 8px 24px rgba(0,0,0,0.15);transition:opacity .3s';
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      setTimeout(() => toast.remove(), 300);
    }, 2000);
  }
};
