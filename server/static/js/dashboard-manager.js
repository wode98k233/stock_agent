/* ─────────────────────────────────────────────
   选股雷达 Web — 仪表盘配置管理
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

(() => {
  const api = window.StockRadar.api;
  const _esc = (v) => { const s = document.createElement("span"); s.textContent = v ?? ""; return s.innerHTML; };

  /** JSON 格式化：顶层压缩，sections/llm_fields 展开 */
  function _compactJSON(obj) {
    return JSON.stringify(obj, null, 2);
  }

  class DashboardManager {
    constructor() {
      this.catalog = null;
      this.currentId = null;
      this.currentDef = null;
      this.dirty = false;
      this.associations = null;
    }

    open() {
      document.getElementById("dashboardManagerOverlay")?.classList.add("open");
      document.getElementById("dashboardManagerPanel")?.classList.add("open");
      if (!this.catalog) this.loadCatalog();
    }

    close() {
      document.getElementById("dashboardManagerOverlay")?.classList.remove("open");
      document.getElementById("dashboardManagerPanel")?.classList.remove("open");
    }

    async loadCatalog() {
      try {
        this.catalog = await api.getDashboards();
        this.associations = this.catalog.associations || { mappings: [], standalone: [] };
        this.render();
        const first = this.catalog.dashboards?.[0];
        if (first) this.selectDashboard(first.id);
      } catch (e) {
        console.error("加载仪表盘目录失败", e);
      }
    }

    render() {
      const body = document.getElementById("dashboardManagerBody");
      if (!body) return;
      body.innerHTML = `
        <div class="template-sidebar">
          <input class="template-search" placeholder="搜索仪表盘…" id="dmSearch">
          <div class="template-list" id="dmList"></div>
        </div>
        <div class="template-editor" id="dmEditor">
          <div style="color:var(--muted);padding:40px;text-align:center">选择一个仪表盘开始编辑</div>
        </div>
        <div class="template-inspector" id="dmInspector"></div>
      `;
      this._renderList();
      body.querySelector("#dmSearch").addEventListener("input", (e) => this._renderList(e.target.value));
    }

    _renderList(filter = "") {
      const el = document.getElementById("dmList");
      if (!el) return;
      const items = (this.catalog?.dashboards || []).filter(d =>
        !filter || d.id.includes(filter) || (d.display_name || "").includes(filter) || (d.description || "").includes(filter)
      );
      el.innerHTML = items.map(d => `
        <button class="template-list-item${d.id === this.currentId ? " active" : ""}" data-did="${_esc(d.id)}">
          <div style="display:flex;align-items:center;justify-content:space-between">
            <span style="font-weight:900">${_esc(d.display_name || d.id)}</span>
            <span class="template-dot"></span>
          </div>
          <div class="template-list-id">${_esc(d.id)}</div>
          <div class="template-list-desc">${_esc(d.description || "")}</div>
        </button>
      `).join("");
      el.querySelectorAll(".template-list-item").forEach(btn => {
        btn.onclick = () => this.selectDashboard(btn.dataset.did);
      });
    }

    async selectDashboard(id) {
      if (this.dirty && !confirm("当前修改未保存，确定切换？")) return;
      try {
        this.currentDef = await api.getDashboard(id);
        this.currentId = id;
        this.dirty = false;
        this._renderList(document.getElementById("dmSearch")?.value || "");
        this.renderEditor();
        this.renderInspector();
      } catch (e) {
        console.error("加载仪表盘失败", e);
      }
    }

    renderEditor() {
      const el = document.getElementById("dmEditor");
      if (!el || !this.currentDef) return;
      const d = this.currentDef;
      const sections = d.sections || [];
      const llmFields = d.llm_fields || [];
      const dims = d.checklist_dimensions || [];

      el.innerHTML = `
        <div class="template-workbar">
          <div>
            <div class="template-workbar-title">${_esc(d.display_name || d.id)}</div>
            <div class="template-workbar-subtitle">${_esc(d.id)}</div>
          </div>
          <div class="template-workbar-actions">
            <button class="secondary-button" id="dmResetBtn">重置</button>
            <button class="send-button" id="dmSaveBtn">保存</button>
          </div>
        </div>

        <div class="template-editor-grid" style="margin-top:14px">
          ${this._field("显示名称", "display_name", d.display_name || "")}
          ${this._field("描述", "description", d.description || "")}
          ${this._field("场景标签", "scenario_tag", d.scenario_tag || "")}
          ${this._textareaField("检查维度 (逗号分隔)", "checklist_dimensions", dims.join(", "), 2)}
        </div>

        <div class="template-section" style="margin-top:14px">
          <div class="template-section-head">
            <div>
              <div class="template-section-title">区块 Sections</div>
              <div class="template-section-desc">仪表盘渲染区块，每个区块有 renderer 和 fields</div>
            </div>
            <span class="template-count">${sections.length}</span>
          </div>
          <div style="padding:12px" id="dmSections">
            ${sections.map((s, i) => this._renderSection(s, i)).join("")}
            <button class="secondary-button" id="dmAddSectionBtn" style="margin-top:8px">+ 添加区块</button>
          </div>
        </div>

        <div class="template-section" style="margin-top:14px">
          <div class="template-section-head">
            <div>
              <div class="template-section-title">LLM 字段</div>
              <div class="template-section-desc">LLM 需要提取的结构化字段</div>
            </div>
            <span class="template-count">${llmFields.length}</span>
          </div>
          <div style="padding:12px" id="dmLlmFields">
            <table class="template-table" style="width:100%">
              <thead><tr><th>名称</th><th>类型</th><th>描述</th><th>示例</th><th></th></tr></thead>
              <tbody>
                ${llmFields.map((f, i) => this._renderLlmFieldRow(f, i)).join("")}
              </tbody>
            </table>
            <button class="secondary-button" id="dmAddFieldBtn" style="margin-top:8px">+ 添加字段</button>
          </div>
        </div>
      `;

      el.querySelector("#dmSaveBtn").onclick = () => this.showSavePreview();
      el.querySelector("#dmResetBtn").onclick = () => this.selectDashboard(this.currentId);
      el.querySelector("#dmAddSectionBtn").onclick = () => this._addSection();
      el.querySelector("#dmAddFieldBtn").onclick = () => this._addLlmField();

      el.querySelectorAll("input, textarea, select").forEach(inp => {
        inp.addEventListener("input", () => { this.dirty = true; });
      });
    }

    _renderSection(s, i) {
      const fields = (s.fields || []).join(", ");
      return `
        <div class="template-preview-card" style="margin-bottom:8px;padding:10px 12px" data-si="${i}">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
            <div style="flex:1;display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:center">
              <input class="template-input" value="${_esc(s.id || "")}" data-key="section-id" placeholder="id">
              <input class="template-input" value="${_esc(s.renderer || "")}" data-key="section-renderer" placeholder="renderer">
              <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--muted)">
                <input type="checkbox" ${s.required ? "checked" : ""} data-key="section-required"> required
              </label>
            </div>
            <button class="icon-button" data-action="remove-section" title="删除">×</button>
          </div>
          <input class="template-input" style="margin-top:6px;width:100%" value="${_esc(fields)}" data-key="section-fields" placeholder="fields (逗号分隔)">
        </div>
      `;
    }

    _renderLlmFieldRow(f, i) {
      return `
        <tr data-fi="${i}">
          <td><input class="template-input" value="${_esc(f.name || "")}" data-key="field-name" style="width:100%"></td>
          <td><input class="template-input" value="${_esc(f.type || "")}" data-key="field-type" style="width:80px"></td>
          <td><input class="template-input" value="${_esc(f.description || "")}" data-key="field-desc" style="width:100%"></td>
          <td><input class="template-input" value="${_esc(f.example || "")}" data-key="field-example" style="width:100%"></td>
          <td><button class="icon-button" data-action="remove-field" title="删除">×</button></td>
        </tr>
      `;
    }

    _addSection() {
      const def = this.currentDef;
      if (!def) return;
      def.sections = def.sections || [];
      def.sections.push({ id: "", renderer: "", required: false, fields: [] });
      this.dirty = true;
      this.renderEditor();
    }

    _addLlmField() {
      const def = this.currentDef;
      if (!def) return;
      def.llm_fields = def.llm_fields || [];
      def.llm_fields.push({ name: "", type: "str", description: "", example: "" });
      this.dirty = true;
      this.renderEditor();
    }

    _collectDefinition() {
      const el = document.getElementById("dmEditor");
      if (!el) return null;
      const def = { ...this.currentDef };

      def.display_name = el.querySelector("[data-key='display_name']")?.value || def.display_name;
      def.description = el.querySelector("[data-key='description']")?.value || def.description;
      def.scenario_tag = el.querySelector("[data-key='scenario_tag']")?.value || def.scenario_tag;

      const dimsStr = el.querySelector("[data-key='checklist_dimensions']")?.value || "";
      def.checklist_dimensions = dimsStr.split(",").map(s => s.trim()).filter(Boolean);

      const sectionCards = el.querySelectorAll("[data-si]");
      def.sections = Array.from(sectionCards).map(card => ({
        id: card.querySelector("[data-key='section-id']")?.value || "",
        renderer: card.querySelector("[data-key='section-renderer']")?.value || "",
        required: card.querySelector("[data-key='section-required']")?.checked || false,
        fields: (card.querySelector("[data-key='section-fields']")?.value || "").split(",").map(s => s.trim()).filter(Boolean),
      }));

      const fieldRows = el.querySelectorAll("[data-fi]");
      def.llm_fields = Array.from(fieldRows).map(row => ({
        name: row.querySelector("[data-key='field-name']")?.value || "",
        type: row.querySelector("[data-key='field-type']")?.value || "str",
        description: row.querySelector("[data-key='field-desc']")?.value || "",
        example: row.querySelector("[data-key='field-example']")?.value || "",
      }));

      return def;
    }

    /* ── 右侧 inspector ── */

    renderInspector() {
      const el = document.getElementById("dmInspector");
      if (!el) return;
      el.innerHTML = `
        <div class="preview-tabs" id="dmTabs">
          <button class="preview-tab active" data-tab="preview">预览</button>
          <button class="preview-tab" data-tab="assoc">关联</button>
          <button class="preview-tab" data-tab="import">导入</button>
        </div>
        <div id="dmTabContent"></div>
      `;
      el.querySelectorAll(".preview-tab").forEach(btn => {
        btn.onclick = () => {
          el.querySelectorAll(".preview-tab").forEach(b => b.classList.remove("active"));
          btn.classList.add("active");
          this._renderTab(btn.dataset.tab);
        };
      });
      this._renderTab("preview");
    }

    _renderTab(tab) {
      const el = document.getElementById("dmTabContent");
      if (!el) return;
      if (tab === "preview") this._renderPreviewTab(el);
      else if (tab === "assoc") this._renderAssocTab(el);
      else if (tab === "import") this._renderImportTab(el);
    }

    _renderPreviewTab(el) {
      const def = this._collectDefinition() || this.currentDef;
      // 按区块折叠展示，而不是一个巨大的 JSON 块
      const sections = def.sections || [];
      const llmFields = def.llm_fields || [];

      el.innerHTML = `
        <div style="display:grid;gap:10px">
          <div class="template-preview-card" style="padding:12px">
            <div style="font-weight:900;margin-bottom:6px;font-size:13px">基本信息</div>
            <pre class="template-diff-value" style="max-height:120px">${_esc(JSON.stringify({
              id: def.id, display_name: def.display_name, description: def.description,
              scenario_tag: def.scenario_tag, checklist_dimensions: def.checklist_dimensions,
            }, null, 2))}</pre>
          </div>
          <div class="template-preview-card" style="padding:12px">
            <div style="font-weight:900;margin-bottom:6px;font-size:13px">区块 Sections (${sections.length})</div>
            ${sections.map(s => `
              <details style="margin-bottom:4px">
                <summary style="cursor:pointer;font-size:12px;color:var(--text);font-weight:800;padding:4px 0">
                  ${_esc(s.id || "(空)")} <span style="color:var(--muted);font-weight:400">${_esc(s.renderer || "")}${s.required ? " · 必选" : ""}</span>
                </summary>
                <pre class="template-diff-value" style="margin:2px 0 6px 16px;max-height:100px">${_esc(JSON.stringify(s, null, 2))}</pre>
              </details>
            `).join("")}
          </div>
          <div class="template-preview-card" style="padding:12px">
            <div style="font-weight:900;margin-bottom:6px;font-size:13px">LLM 字段 (${llmFields.length})</div>
            ${llmFields.map(f => `
              <details style="margin-bottom:4px">
                <summary style="cursor:pointer;font-size:12px;color:var(--text);font-weight:800;padding:4px 0">
                  ${_esc(f.name || "(空)")} <span style="color:var(--muted);font-weight:400">${_esc(f.type || "")}</span>
                </summary>
                <pre class="template-diff-value" style="margin:2px 0 6px 16px;max-height:80px">${_esc(JSON.stringify(f, null, 2))}</pre>
              </details>
            `).join("")}
          </div>
        </div>
      `;
    }

    _renderAssocTab(el) {
      const assoc = this.associations || { mappings: [], standalone: [] };
      const mappings = assoc.mappings || [];
      const standalone = assoc.standalone || [];
      const dashboards = (this.catalog?.dashboards || []).map(d => d.id);

      el.innerHTML = `
        <div>
          <div class="template-section">
            <div class="template-section-head">
              <div class="template-section-title">模板 → 仪表盘映射</div>
              <span class="template-count">${mappings.length}</span>
            </div>
            <div style="padding:12px;max-height:30vh;overflow:auto">
              <table class="template-table" style="width:100%">
                <thead><tr><th>template_id</th><th>dashboard_id</th><th></th></tr></thead>
                <tbody id="dmAssocBody">
                  ${mappings.map((m, i) => `
                    <tr data-ai="${i}">
                      <td><input class="template-input" value="${_esc(m.template_id)}" data-key="assoc-tid" style="width:100%"></td>
                      <td>
                        <select class="template-input" data-key="assoc-did" style="width:100%">
                          ${dashboards.map(d => `<option value="${_esc(d)}" ${d === m.dashboard_id ? "selected" : ""}>${_esc(d)}</option>`).join("")}
                        </select>
                      </td>
                      <td><button class="icon-button" data-action="remove-assoc" title="删除">×</button></td>
                    </tr>
                  `).join("")}
                </tbody>
              </table>
              <button class="secondary-button" id="dmAddAssocBtn" style="margin-top:8px">+ 添加映射</button>
            </div>
          </div>
          <div class="template-section" style="margin-top:12px">
            <div class="template-section-head">
              <div class="template-section-title">独立仪表盘</div>
              <span class="template-count">${standalone.length}</span>
            </div>
            <div style="padding:12px">
              <table class="template-table" style="width:100%">
                <thead><tr><th>dashboard_id</th><th>label</th><th></th></tr></thead>
                <tbody id="dmStandaloneBody">
                  ${standalone.map((s, i) => `
                    <tr data-sai="${i}">
                      <td>
                        <select class="template-input" data-key="sa-did" style="width:100%">
                          ${dashboards.map(d => `<option value="${_esc(d)}" ${d === s.dashboard_id ? "selected" : ""}>${_esc(d)}</option>`).join("")}
                        </select>
                      </td>
                      <td><input class="template-input" value="${_esc(s.label || "")}" data-key="sa-label" style="width:100%"></td>
                      <td><button class="icon-button" data-action="remove-standalone" title="删除">×</button></td>
                    </tr>
                  `).join("")}
                </tbody>
              </table>
              <button class="secondary-button" id="dmAddStandaloneBtn" style="margin-top:8px">+ 添加独立仪表盘</button>
              <button class="send-button" id="dmSaveAssocBtn" style="margin-top:8px">保存关联</button>
            </div>
          </div>
        </div>
      `;

      el.querySelector("#dmAddAssocBtn").onclick = () => {
        this.associations.mappings = this.associations.mappings || [];
        this.associations.mappings.push({ template_id: "", dashboard_id: dashboards[0] || "" });
        this._renderTab("assoc");
      };
      el.querySelector("#dmAddStandaloneBtn").onclick = () => {
        this.associations.standalone = this.associations.standalone || [];
        this.associations.standalone.push({ dashboard_id: dashboards[0] || "", label: "" });
        this._renderTab("assoc");
      };
      el.querySelector("#dmSaveAssocBtn").onclick = () => this._saveAssociations();

      el.querySelectorAll("[data-action='remove-assoc']").forEach(btn => {
        btn.onclick = () => {
          const tr = btn.closest("tr");
          this.associations.mappings.splice(parseInt(tr.dataset.ai), 1);
          this._renderTab("assoc");
        };
      });
      el.querySelectorAll("[data-action='remove-standalone']").forEach(btn => {
        btn.onclick = () => {
          const tr = btn.closest("tr");
          this.associations.standalone.splice(parseInt(tr.dataset.sai), 1);
          this._renderTab("assoc");
        };
      });
    }

    async _saveAssociations() {
      const el = document.getElementById("dmTabContent");
      if (!el) return;
      const mappings = Array.from(el.querySelectorAll("[data-ai]")).map(tr => ({
        template_id: tr.querySelector("[data-key='assoc-tid']")?.value || "",
        dashboard_id: tr.querySelector("[data-key='assoc-did']")?.value || "",
      }));
      const standalone = Array.from(el.querySelectorAll("[data-sai]")).map(tr => ({
        dashboard_id: tr.querySelector("[data-key='sa-did']")?.value || "",
        label: tr.querySelector("[data-key='sa-label']")?.value || "",
      }));
      try {
        await api.saveDashboardAssociations({ version: 1, mappings, standalone });
        this.associations = { version: 1, mappings, standalone };
        alert("关联保存成功");
      } catch (e) {
        alert("保存失败: " + e.message);
      }
    }

    _renderImportTab(el) {
      el.innerHTML = `
        <div>
          <div class="upload-box" id="dmUploadBox">
            <div style="font-size:28px;margin-bottom:10px;color:var(--muted)">◫</div>
            <div style="font-weight:800;font-size:13px">选择或拖入文件</div>
            <div style="margin-top:6px;color:var(--muted);font-size:11px">仪表盘 .json 文件</div>
          </div>
          <input type="file" id="dmFileInput" accept=".json" style="display:none">
          <div id="dmImportResult" style="font-size:12px;margin-bottom:12px"></div>
          <button class="send-button" id="dmImportBtn" style="width:100%">导入</button>
        </div>
      `;
      const uploadBox = el.querySelector("#dmUploadBox");
      const fileInput = el.querySelector("#dmFileInput");
      uploadBox.onclick = () => fileInput.click();
      fileInput.onchange = () => {
        const name = fileInput.files?.[0]?.name || "";
        if (name) {
          uploadBox.innerHTML = `
            <div style="font-size:28px;margin-bottom:10px;color:var(--brand)">✓</div>
            <div style="font-weight:800;font-size:13px">${_esc(name)}</div>
            <div style="margin-top:6px;color:var(--muted);font-size:11px">点击可重新选择</div>
          `;
        }
      };
      // 拖拽支持
      uploadBox.ondragover = (e) => { e.preventDefault(); uploadBox.classList.add("active"); };
      uploadBox.ondragleave = () => uploadBox.classList.remove("active");
      uploadBox.ondrop = (e) => {
        e.preventDefault();
        uploadBox.classList.remove("active");
        if (e.dataTransfer.files?.[0]) {
          fileInput.files = e.dataTransfer.files;
          fileInput.dispatchEvent(new Event("change"));
        }
      };
      el.querySelector("#dmImportBtn").onclick = () => this._executeImport();
    }

    async _executeImport() {
      const fileInput = document.getElementById("dmFileInput");
      const resultEl = document.getElementById("dmImportResult");
      if (!fileInput?.files?.[0]) {
        resultEl.innerHTML = '<span style="color:var(--danger)">请选择文件</span>';
        return;
      }
      try {
        const text = await fileInput.files[0].text();
        const data = JSON.parse(text);
        const result = await api.importDashboard(data);
        resultEl.innerHTML = `<span style="color:var(--success)">导入成功: ${_esc(result.id)} (${_esc(result.action)})</span>`;
        await this.loadCatalog();
        this.selectDashboard(result.id);
      } catch (e) {
        resultEl.innerHTML = `<span style="color:var(--danger)">导入失败: ${_esc(e.message)}</span>`;
      }
    }

    /* ── 保存 diff ── */

    showSavePreview() {
      const collected = this._collectDefinition();
      if (!collected) return;
      const changes = this._getChanges(this.currentDef, collected);
      if (changes.length === 0) {
        alert("没有变更");
        return;
      }
      this._showDiffModal(changes, collected);
    }

    _getChanges(before, after) {
      const changes = [];
      const check = (key, label) => {
        const bv = before[key] === undefined ? "null" : JSON.stringify(before[key], null, 2);
        const av = after[key] === undefined ? "null" : JSON.stringify(after[key], null, 2);
        if (bv !== av) changes.push({ key, label, oldValue: bv, newValue: av });
      };
      check("display_name", "显示名称");
      check("description", "描述");
      check("scenario_tag", "场景标签");
      check("checklist_dimensions", "检查维度");
      check("sections", "区块 Sections");
      check("llm_fields", "LLM 字段");
      return changes;
    }

    _showDiffModal(changes, newDef) {
      const overlay = document.getElementById("dashboardDiffOverlay");
      const modal = document.getElementById("dashboardDiffModal");
      const summary = document.getElementById("dashboardDiffSummary");
      const body = document.getElementById("dashboardDiffBody");
      if (!overlay || !modal) return;

      summary.textContent = `${changes.length} 项变更`;
      body.innerHTML = changes.map(c => `
        <div class="template-diff-row">
          <div class="template-diff-key">${_esc(c.label)}</div>
          <div class="template-diff-columns">
            <div>
              <div class="template-diff-caption">旧值</div>
              <pre class="template-diff-value old">${_esc(c.oldValue)}</pre>
            </div>
            <div>
              <div class="template-diff-caption">新值</div>
              <pre class="template-diff-value new">${_esc(c.newValue)}</pre>
            </div>
          </div>
        </div>
      `).join("");

      overlay.classList.add("open");
      modal.classList.add("open");

      const close = () => { overlay.classList.remove("open"); modal.classList.remove("open"); };
      document.getElementById("dashboardDiffCloseBtn").onclick = close;
      document.getElementById("dashboardDiffCancelBtn").onclick = close;
      overlay.onclick = close;
      document.getElementById("dashboardDiffApplyBtn").onclick = async () => {
        close();
        await this._doSave(newDef);
      };
    }

    async _doSave(definition) {
      try {
        await api.saveDashboard(this.currentId, definition);
        this.currentDef = definition;
        this.dirty = false;
        this.renderEditor();
        this.renderInspector();
        this.catalog = await api.getDashboards();
        this._renderList(document.getElementById("dmSearch")?.value || "");
      } catch (e) {
        alert("保存失败: " + e.message);
      }
    }

    // ── helpers ──
    _field(label, key, value) {
      return `<div class="template-field"><label>${_esc(label)}</label><input class="template-input" data-key="${_esc(key)}" value="${_esc(value)}"></div>`;
    }

    _textareaField(label, key, value, rows = 3) {
      return `<div class="template-field"><label>${_esc(label)}</label><textarea class="template-input" data-key="${_esc(key)}" rows="${rows}">${_esc(value)}</textarea></div>`;
    }
  }

  window.StockRadar.DashboardManager = DashboardManager;
})();
