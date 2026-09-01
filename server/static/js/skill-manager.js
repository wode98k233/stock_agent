/**
 * Skill 管理模块（三栏全页面）
 * - 全页面显示/隐藏（替代抽屉）
 * - 左侧：搜索 + 筛选 + 列表
 * - 中间：summary、基础信息、工具列表、预检结果、操作按钮
 * - 右侧：导入向导（上传、步骤、结构说明）
 */
(function () {
  'use strict';

  /* ── DOM ── */
  const $page    = () => document.getElementById('skillPage');
  const $chat    = () => document.querySelector('.chat-pane');
  const $list    = () => document.getElementById('skillList');
  const $main    = () => document.getElementById('skillMain');
  const $search  = () => document.getElementById('skillSearch');
  const $tabs    = () => document.getElementById('skillTabs');
  const $upload  = () => document.getElementById('skillUploadZone');
  const $fileInput = () => document.getElementById('skillFileInput');

  let allSkills = [];
  let selectedName = null;
  let currentFilter = 'all';
  let searchQuery = '';

  /* ── 页面切换 ── */
  function openPage() {
    $page().classList.add('open');
    if ($chat()) $chat().style.display = 'none';
    loadSkills();
  }

  function closePage() {
    $page().classList.remove('open');
    if ($chat()) $chat().style.display = '';
    selectedName = null;
  }

  /* ── 加载列表 ── */
  async function loadSkills() {
    try {
      const resp = await fetch('/api/skills');
      const data = await resp.json();
      allSkills = data.items || [];
      renderList();
      // 自动选中第一个
      const filtered = getFiltered();
      if (filtered.length && !selectedName) {
        loadSkillDetail(filtered[0].name);
      }
    } catch (e) {
      console.error('加载技能列表失败:', e);
    }
  }

  /* ── 筛选 ── */
  function getFiltered() {
    let list = allSkills;
    if (currentFilter === 'internal') list = list.filter(s => s.source === 'internal');
    else if (currentFilter === 'external') list = list.filter(s => s.source === 'external');
    else if (currentFilter === 'user') list = list.filter(s => s.source === 'user');
    else if (currentFilter === 'error') list = list.filter(s => !s.has_build_tools);
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      list = list.filter(s =>
        s.name.toLowerCase().includes(q) ||
        (s.category || '').toLowerCase().includes(q) ||
        (s.description || '').toLowerCase().includes(q)
      );
    }
    return list;
  }

  /* ── 渲染列表 ── */
  function renderList() {
    const el = $list();
    const filtered = getFiltered();
    if (!filtered.length) {
      el.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px;font-size:13px;">没有匹配的技能</div>';
      return;
    }
    let html = '';
    for (const s of filtered) {
      const isActive = selectedName === s.name;
      const dotClass = s.enabled ? (s.has_build_tools ? '' : ' warn') : ' disabled';
      const sourceLabel = { internal: '内置', external: '外部包', user: '用户导入' }[s.source] || s.source;
      html += `<button class="skill-item${isActive ? ' active' : ''}" data-name="${esc(s.name)}">
        <div class="skill-item-head"><span>${esc(s.name)}</span><span class="skill-status-dot${dotClass}"></span></div>
        <div class="skill-item-meta">${esc(s.category || '未分类')} · ${sourceLabel} · ${s.tools_count} 个工具</div>
        <div class="skill-badge-row">
          <span class="skill-badge${s.enabled ? ' brand' : ''}">${s.enabled ? '启用' : '未启用'}</span>
          ${s.source === 'user' && !s.has_build_tools ? '<span class="skill-badge">待预检</span>' : ''}
        </div>
      </button>`;
    }
    el.innerHTML = html;
    el.querySelectorAll('.skill-item').forEach(item => {
      item.addEventListener('click', () => loadSkillDetail(item.dataset.name));
    });
  }

  /* ── 加载详情 ── */
  async function loadSkillDetail(name) {
    selectedName = name;
    renderList(); // 更新选中态
    try {
      const resp = await fetch(`/api/skills/${encodeURIComponent(name)}`);
      if (!resp.ok) throw new Error(await resp.text());
      const d = await resp.json();
      renderDetail(d);
    } catch (e) {
      console.error('加载技能详情失败:', e);
    }
  }

  /* ── 渲染详情 ── */
  function renderDetail(d) {
    const el = $main();
    const sourceLabel = { internal: '内置', external: '外部包', user: '用户导入' }[d.source] || d.source;
    const statusText = d.enabled ? (d.has_build_tools ? '可用' : '构建失败') : '已禁用';

    let html = '';

    // summary cards
    html += `<div class="skill-summary">
      <div class="skill-metric"><span>当前 Skill</span><strong>${esc(d.name)}</strong></div>
      <div class="skill-metric"><span>工具数量</span><strong>${d.tools_count}</strong></div>
      <div class="skill-metric"><span>来源</span><strong>${sourceLabel}</strong></div>
      <div class="skill-metric"><span>状态</span><strong>${statusText}</strong></div>
    </div>`;

    // 基础信息
    html += `<div class="skill-section">
      <div class="skill-section-head">
        <div>
          <div class="skill-section-title">基础信息</div>
          <div class="skill-section-desc">这些字段来自 SKILL.md YAML 和目录层信息，会影响 Agent 选择技能。</div>
        </div>
        <span class="skill-badge brand">进入技能目录</span>
      </div>
      <div class="skill-kv"><div class="skill-kv-label">name</div><div class="skill-kv-value skill-mono">${esc(d.name)}</div></div>
      <div class="skill-kv"><div class="skill-kv-label">version</div><div class="skill-kv-value skill-mono">${esc(d.version || '-')}</div></div>
      <div class="skill-kv"><div class="skill-kv-label">category</div><div class="skill-kv-value">${esc(d.category || '-')}</div></div>
      <div class="skill-kv"><div class="skill-kv-label">description</div><div class="skill-kv-value">${esc(d.description || '-')}</div></div>`;

    if (d.catalog_info) {
      if (d.catalog_info.target) {
        html += `<div class="skill-kv"><div class="skill-kv-label">核心目标</div><div class="skill-kv-value">${esc(d.catalog_info.target)}</div></div>`;
      }
      if (d.catalog_info.key_params && d.catalog_info.key_params.length) {
        html += `<div class="skill-kv"><div class="skill-kv-label">关键参数</div><div class="skill-kv-value">${d.catalog_info.key_params.map(p => esc(p)).join('、')}</div></div>`;
      }
    }
    html += '</div>';

    // 工具列表
    if (d.tools && d.tools.length) {
      html += `<div class="skill-section">
        <div class="skill-section-head">
          <div>
            <div class="skill-section-title">工具列表</div>
            <div class="skill-section-desc">工具名要和 build_tools 返回的 LangChain tool 对齐，参数来自函数签名或 Pydantic schema。</div>
          </div>
          <button class="skill-btn" id="skillPreflightBtn">运行预检</button>
        </div>
        <table class="skill-tool-table">
          <thead><tr><th>工具</th><th>参数</th><th>说明</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>`;
      for (const t of d.tools) {
        const params = t.param_schema && t.param_schema.properties
          ? Object.keys(t.param_schema.properties).join(': ' + (t.param_schema.type || 'any') + ', ')
          : '-';
        const buildable = t.buildable ? 'ok' : '缺失';
        const badgeClass = t.buildable ? 'brand' : '';
        html += `<tr>
          <td class="skill-mono">${esc(t.tool_name)}</td>
          <td class="skill-mono">${esc(params)}</td>
          <td>${esc(t.description || '-')}</td>
          <td><span class="skill-badge ${badgeClass}">${buildable}</span></td>
          <td>${t.buildable ? `<button class="skill-btn skill-tool-test-btn" data-tool='${JSON.stringify(t)}' data-skill='${esc(d.name)}'>测试</button>` : '-'}</td>
        </tr>`;
      }
      html += '</tbody></table></div>';
    }

    // 预检结果（如果已有 checks 数据则展示，否则留空）
    if (d.checks && d.checks.length) {
      const passed = d.checks.filter(c => c.status === 'ok').length;
      html += `<div class="skill-section">
        <div class="skill-section-head">
          <div>
            <div class="skill-section-title">预检结果</div>
            <div class="skill-section-desc">导入或启用前展示同一套校验，避免坏 skill 进入 Agent 执行链。</div>
          </div>
          <span class="skill-badge brand">${passed}/${d.checks.length} 通过</span>
        </div>
        <div class="skill-check-list">`;
      for (const c of d.checks) {
        const cls = c.status === 'warning' ? ' warn' : c.status === 'error' ? ' error' : '';
        const badgeText = c.status === 'ok' ? 'ok' : c.status === 'warning' ? '提示' : '失败';
        const badgeClass = c.status === 'ok' ? 'brand' : '';
        html += `<div class="skill-check${cls}"><span>${esc(c.name)}</span><span class="skill-badge ${badgeClass}">${badgeText}</span></div>`;
      }
      html += '</div></div>';
    }

    // 使用指南
    if (d.usage_guide) {
      html += `<div class="skill-section">
        <div class="skill-section-head">
          <div>
            <div class="skill-section-title">使用指南</div>
            <div class="skill-section-desc">供 LLM 理解如何调用工具。</div>
          </div>
        </div>
        <div style="padding:12px 14px;font-size:13px;line-height:1.65;color:#334155;white-space:pre-wrap;">${esc(d.usage_guide)}</div>
      </div>`;
    }

    // 路径
    if (d.skill_dir) {
      html += `<div class="skill-section">
        <div class="skill-section-head">
          <div><div class="skill-section-title">文件路径</div></div>
        </div>
        <div style="padding:10px 14px;font-size:12px;color:#526779;word-break:break-all;font-family:monospace;">${esc(d.skill_dir)}</div>
      </div>`;
    }

    // 底部操作
    html += `<div class="skill-footer-actions">
      <button class="skill-btn" id="skillToggleBtn">${d.enabled ? '禁用 Skill' : '启用 Skill'}</button>
      ${d.source === 'user' ? '<button class="skill-btn danger" id="skillDeleteBtn">删除</button>' : ''}
      <button class="skill-btn primary" id="skillImportBtn">导入新 Skill</button>
    </div>`;

    el.innerHTML = html;

    // 绑定按钮
    const toggleBtn = document.getElementById('skillToggleBtn');
    if (toggleBtn) toggleBtn.addEventListener('click', () => toggleSkill(d.name, !d.enabled));
    const deleteBtn = document.getElementById('skillDeleteBtn');
    if (deleteBtn) deleteBtn.addEventListener('click', () => deleteSkill(d.name));
    const importBtn = document.getElementById('skillImportBtn');
    if (importBtn) importBtn.addEventListener('click', () => $fileInput().click());
    const preflightBtn = document.getElementById('skillPreflightBtn');
    if (preflightBtn) preflightBtn.addEventListener('click', () => runPreflight(d.name));

    // 绑定工具测试按钮
    el.querySelectorAll('.skill-tool-test-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const toolMeta = JSON.parse(btn.dataset.tool);
        openToolTestModal(btn.dataset.skill, toolMeta);
      });
    });
  }

  /* ── 启用/禁用 ── */
  async function toggleSkill(name, enabled) {
    try {
      const resp = await fetch(`/api/skills/${encodeURIComponent(name)}/enabled`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      await loadSkills();
      loadSkillDetail(name);
    } catch (e) {
      console.error('切换技能状态失败:', e);
      alert('操作失败: ' + e.message);
    }
  }

  /* ── 删除 ── */
  async function deleteSkill(name) {
    if (!confirm(`确认删除技能 "${name}"？此操作不可恢复。`)) return;
    try {
      const resp = await fetch(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' });
      if (!resp.ok) throw new Error(await resp.text());
      selectedName = null;
      $main().innerHTML = '<div class="skill-empty-hint"><p>技能已删除</p></div>';
      await loadSkills();
    } catch (e) {
      console.error('删除技能失败:', e);
      alert('删除失败: ' + e.message);
    }
  }

  /* ── 重新扫描 ── */
  async function rescan() {
    try {
      const resp = await fetch('/api/skills/rescan', { method: 'POST' });
      if (!resp.ok) throw new Error(await resp.text());
      selectedName = null;
      $main().innerHTML = '<div class="skill-empty-hint"><p>已重新扫描，请选择技能查看详情</p></div>';
      await loadSkills();
    } catch (e) {
      console.error('重新扫描失败:', e);
      alert('重新扫描失败: ' + e.message);
    }
  }

  /* ── 预检 ── */
  async function runPreflight(name) {
    // 目前只刷新详情，后续接入 validate-import API
    await loadSkillDetail(name);
  }

  /* ── 文件上传 ── */
  function handleFileUpload(file) {
    if (!file || !file.name.endsWith('.zip')) {
      alert('请选择 .zip 文件');
      return;
    }
    // 后续接入 validate-import / import API
    alert('导入功能开发中：' + file.name);
  }

  /* ── 工具测试弹窗 ── */

  /**
   * 从 param_schema 动态生成表单 HTML
   */
  function buildToolFormHTML(param_schema) {
    if (!param_schema || !param_schema.properties) {
      return '<div style="color:var(--muted);font-size:13px;">该工具无参数</div>';
    }
    const props = param_schema.properties;
    const required = param_schema.required || [];
    let html = '';
    for (const [name, prop] of Object.entries(props)) {
      const isRequired = required.includes(name);
      const type = prop.type || 'string';
      const desc = prop.description || '';
      const label = `${isRequired ? '* ' : ''}${esc(name)}`;
      const placeholder = esc(desc || `请输入 ${name}`);

      html += `<div class="skill-test-field">
        <label class="skill-test-label">${label} <span class="skill-test-type">(${esc(type)})</span></label>`;
      if (type === 'boolean') {
        html += `<select class="skill-test-input" data-name="${esc(name)}" data-type="boolean">
          <option value="">--</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>`;
      } else if (type === 'integer' || type === 'number') {
        html += `<input class="skill-test-input" type="number" data-name="${esc(name)}" data-type="${type}" placeholder="${placeholder}"${isRequired ? ' required' : ''}>`;
      } else if (type === 'array' || type === 'object') {
        html += `<textarea class="skill-test-input skill-test-textarea" data-name="${esc(name)}" data-type="${type}" placeholder='${placeholder} (JSON 格式)' rows="3"${isRequired ? ' required' : ''}></textarea>`;
      } else {
        // string 或其他：短字段用 input，长描述用 textarea
        const useTextarea = desc.length > 50 || name.includes('query') || name.includes('text') || name.includes('content');
        if (useTextarea) {
          html += `<textarea class="skill-test-input skill-test-textarea" data-name="${esc(name)}" data-type="string" placeholder="${placeholder}" rows="2"${isRequired ? ' required' : ''}></textarea>`;
        } else {
          html += `<input class="skill-test-input" type="text" data-name="${esc(name)}" data-type="string" placeholder="${placeholder}"${isRequired ? ' required' : ''}>`;
        }
      }
      if (desc) {
        html += `<div class="skill-test-hint">${esc(desc)}</div>`;
      }
      html += '</div>';
    }
    return html;
  }

  /**
   * 从模态框表单收集参数
   */
  function collectFormParams(container) {
    const params = {};
    container.querySelectorAll('.skill-test-input').forEach(input => {
      const name = input.dataset.name;
      const type = input.dataset.type;
      let val = input.value.trim();
      if (val === '') return; // 跳过空值
      if (type === 'boolean') {
        params[name] = val === 'true';
      } else if (type === 'integer') {
        params[name] = parseInt(val, 10);
      } else if (type === 'number') {
        params[name] = parseFloat(val);
      } else if (type === 'array' || type === 'object') {
        try {
          params[name] = JSON.parse(val);
        } catch {
          params[name] = val; // 非法 JSON 时当字符串传
        }
      } else {
        params[name] = val;
      }
    });
    return params;
  }

  /**
   * 执行工具测试
   */
  async function runToolTest(skillName, toolName, resultEl, btnEl) {
    const formContainer = document.getElementById('skillTestForm');
    const params = collectFormParams(formContainer);

    btnEl.disabled = true;
    btnEl.textContent = '执行中...';
    resultEl.innerHTML = '<div style="color:var(--muted);font-size:13px;">正在执行...</div>';

    try {
      const resp = await fetch(`/api/skills/${encodeURIComponent(skillName)}/tools/${encodeURIComponent(toolName)}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ params }),
      });
      const data = await resp.json();

      if (data.ok) {
        let outputStr = typeof data.result === 'string' ? data.result : JSON.stringify(data.result, null, 2);
        // 截断过长输出
        if (outputStr.length > 5000) {
          outputStr = outputStr.substring(0, 5000) + '\n... (输出过长，已截断)';
        }
        resultEl.innerHTML = `
          <div class="skill-test-result-header success">
            <span>✅ 执行成功</span>
            <span class="skill-test-duration">${data.duration_ms}ms</span>
          </div>
          <pre class="skill-test-output">${esc(outputStr)}</pre>`;
      } else {
        resultEl.innerHTML = `
          <div class="skill-test-result-header error">
            <span>❌ 执行失败</span>
            <span class="skill-test-duration">${data.duration_ms}ms</span>
          </div>
          <pre class="skill-test-output error">${esc(data.error)}</pre>`;
      }
    } catch (e) {
      resultEl.innerHTML = `
        <div class="skill-test-result-header error">
          <span>❌ 请求异常</span>
        </div>
        <pre class="skill-test-output error">${esc(e.message)}</pre>`;
    } finally {
      btnEl.disabled = false;
      btnEl.textContent = '执行测试';
    }
  }

  /**
   * 打开工具测试模态框
   */
  function openToolTestModal(skillName, toolMeta) {
    // 移除已有模态框
    const existing = document.getElementById('skillTestModal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'skillTestModal';
    modal.className = 'skill-test-modal';
    modal.innerHTML = `
      <div class="skill-test-modal-overlay"></div>
      <div class="skill-test-modal-content">
        <div class="skill-test-modal-header">
          <div>
            <div class="skill-test-modal-title">测试工具: <span class="skill-mono">${esc(toolMeta.tool_name)}</span></div>
            <div class="skill-test-modal-subtitle">${esc(toolMeta.description || '')}</div>
          </div>
          <button class="skill-test-modal-close" id="skillTestClose">&times;</button>
        </div>
        <div class="skill-test-modal-body">
          <div id="skillTestForm" class="skill-test-form">
            ${buildToolFormHTML(toolMeta.param_schema)}
          </div>
          <div class="skill-test-actions">
            <button class="skill-btn primary" id="skillTestRun">执行测试</button>
          </div>
          <div id="skillTestResult" class="skill-test-result-area"></div>
        </div>
      </div>`;

    document.body.appendChild(modal);

    // 绑定事件
    const closeBtn = document.getElementById('skillTestClose');
    const overlay = modal.querySelector('.skill-test-modal-overlay');
    const runBtn = document.getElementById('skillTestRun');
    const resultEl = document.getElementById('skillTestResult');

    const closeModal = () => modal.remove();
    closeBtn.addEventListener('click', closeModal);
    overlay.addEventListener('click', closeModal);
    document.addEventListener('keydown', function escHandler(e) {
      if (e.key === 'Escape') { closeModal(); document.removeEventListener('keydown', escHandler); }
    });

    runBtn.addEventListener('click', () => runToolTest(skillName, toolMeta.tool_name, resultEl, runBtn));

    // 自动聚焦第一个输入框
    const firstInput = modal.querySelector('.skill-test-input');
    if (firstInput) firstInput.focus();
  }

  /* ── 工具 ── */
  function esc(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = String(str);
    return d.innerHTML;
  }

  /* ── 初始化 ── */
  function init() {
    // 打开/关闭
    const btn = document.getElementById('skillBtn');
    if (btn) btn.addEventListener('click', openPage);
    const backBtn = document.getElementById('skillBackBtn');
    if (backBtn) backBtn.addEventListener('click', closePage);

    // 重新扫描
    const rescanBtn = document.getElementById('skillRescanBtn');
    if (rescanBtn) rescanBtn.addEventListener('click', rescan);

    // 搜索
    const search = $search();
    if (search) {
      search.addEventListener('input', () => {
        searchQuery = search.value.trim();
        renderList();
      });
    }

    // 筛选 tabs
    const tabs = $tabs();
    if (tabs) {
      tabs.addEventListener('click', (e) => {
        const tab = e.target.closest('.skill-tab');
        if (!tab) return;
        tabs.querySelectorAll('.skill-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentFilter = tab.dataset.filter || 'all';
        renderList();
      });
    }

    // 上传
    const upload = $upload();
    const fileInput = $fileInput();
    if (upload) {
      upload.addEventListener('click', () => fileInput && fileInput.click());
      upload.addEventListener('dragover', (e) => { e.preventDefault(); upload.style.borderColor = 'var(--brand)'; });
      upload.addEventListener('dragleave', () => { upload.style.borderColor = ''; });
      upload.addEventListener('drop', (e) => {
        e.preventDefault();
        upload.style.borderColor = '';
        const file = e.dataTransfer.files[0];
        if (file) handleFileUpload(file);
      });
    }
    if (fileInput) {
      fileInput.addEventListener('change', () => {
        if (fileInput.files[0]) handleFileUpload(fileInput.files[0]);
        fileInput.value = '';
      });
    }

    // ESC 关闭
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && $page().classList.contains('open')) closePage();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.SkillManager = { openPage, closePage, loadSkills };

  // 桥接到 window.StockRadar.skillManager（index.html 入口按钮依赖
  // `window.StockRadar?.skillManager?.openPage()`）。app.js 可能晚于本文件
  // 加载，故轮询等待 StockRadar 就绪后再挂载。
  (function bridgeSkillManager() {
    if (window.StockRadar) {
      window.StockRadar.skillManager = window.SkillManager;
      return;
    }
    setTimeout(bridgeSkillManager, 100);
  })();
})();
