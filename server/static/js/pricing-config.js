/* ─────────────────────────────────────────────
   选股雷达 Web — 定价目录编辑器（成本 / 计价）
   设计文档 docs/superpowers/specs/2026-10-13-report-cost-estimation-design.md §9.2 / §9.7

   为什么要一个专属面板而不是塞进 .env：
   「供应商 → 模型 → 峰谷价」是嵌套表，塞 .env 既没法维护也没法版本化/备份。
   范式同 route-config.js（overlay + aside 抽屉 + 保存前校验 + 备份回滚）。

   ⚠️ 价目里**没有任何 API Key**（凭证仍在 .env），所以这个面板可以随便编辑保存。
   ───────────────────────────────────────────── */
window.StockRadar = window.StockRadar || {};

window.StockRadar.PricingConfigPanel = class PricingConfigPanel {
  constructor() {
    this.panel = document.getElementById('pricingPanel');
    this.overlay = document.getElementById('pricingOverlay');
    this.catalog = null;
    this.dirty = false;
    this.meta = {};
    this._bind();
  }

  _bind() {
    const on = (id, ev, fn) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener(ev, fn);
    };
    on('pcCloseBtn', 'click', () => this.close());
    on('pcOverlay', 'click', () => this.close());
    on('pcSaveBtn', 'click', () => this.save());
    on('pcValidateBtn', 'click', () => this.validate());
    on('pcResetBtn', 'click', () => this.reset());
    on('pcTrialBtn', 'click', () => this.toggleTrial());
    on('pcTrialRunBtn', 'click', () => this.runTrial());
    on('pcTrialCloseBtn', 'click', () => this.toggleTrial(false));
    on('pcAddProviderBtn', 'click', () => this.addProvider());
    on('pcAddServiceBtn', 'click', () => this.addService());
  }

  async open() {
    if (this.overlay) this.overlay.classList.add('open');
    if (this.panel) this.panel.classList.add('open');
    await this.load();
  }

  close() {
    if (this.dirty && !window.confirm('有未保存的改动，确定关闭？')) return;
    if (this.overlay) this.overlay.classList.remove('open');
    if (this.panel) this.panel.classList.remove('open');
  }

  async load() {
    try {
      const data = await window.StockRadar.api.getPricingCatalog();
      this.catalog = data.catalog || {};
      this.meta = data;
      this.dirty = false;
      this.render();
      this._msg('info', '价目版本 ' + (this.catalog.version || '-')
        + (data.is_default ? '（当前用的是仓库默认价目，保存后生成你自己的运行时价目）'
                           : '（运行时价目：' + (data.path || '') + '）'));
    } catch (e) {
      this._msg('error', '读取价目失败：' + (e.message || e));
    }
  }

  // ── 渲染 ──
  render() {
    this._renderProviders();
    this._renderModels();
    this._renderServices();
    this._renderSchedule();
    const ver = document.getElementById('pcVersion');
    if (ver) ver.textContent = this.catalog && this.catalog.version ? ('v' + this.catalog.version) : '';
  }

  _providers() {
    if (!this.catalog.providers || typeof this.catalog.providers !== 'object') this.catalog.providers = {};
    return this.catalog.providers;
  }

  _renderProviders() {
    const box = document.getElementById('pcProviders');
    if (!box) return;
    const rows = Object.entries(this._providers()).map(([name, p]) => {
      const billing = p.billing || 'unknown';
      const expired = billing === 'free' && p.valid_until && p.valid_until < this._today();
      return `<tr class="${expired ? 'pc-expired' : ''}">
        <td><input class="pc-in pc-name" data-name="${this._esc(name)}" value="${this._esc(name)}"></td>
        <td><input class="pc-in pc-hosts" data-name="${this._esc(name)}" value="${this._esc((p.hosts || []).join(', '))}" placeholder="api.deepseek.com, 10.0.0.1"></td>
        <td>
          <select class="pc-in pc-billing" data-name="${this._esc(name)}">
            ${['paid', 'free', 'unknown'].map((b) => `<option value="${b}"${b === billing ? ' selected' : ''}>${{ paid: '付费', free: '免费', unknown: '未知' }[b]}</option>`).join('')}
          </select>
        </td>
        <td><input class="pc-in pc-until" type="date" data-name="${this._esc(name)}" value="${this._esc(p.valid_until || '')}" ${billing === 'free' ? '' : 'disabled'}></td>
        <td><input class="pc-in pc-note" data-name="${this._esc(name)}" value="${this._esc(p.note || '')}"></td>
        <td><button class="pc-btn pc-del-provider" data-name="${this._esc(name)}" title="删除供应商">删除</button></td>
      </tr>`;
    }).join('');
    box.innerHTML = `<table class="pc-table">
      <thead><tr><th>供应商</th><th>匹配 host（逗号分隔）</th><th>计费方式</th><th>免费有效期</th><th>备注</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="pc-empty">还没有供应商，点右上角「+ 供应商」</td></tr>'}</tbody>
    </table>`;

    box.querySelectorAll('input,select').forEach((el) => {
      el.addEventListener('change', () => this._onProviderEdit(el));
    });
    box.querySelectorAll('.pc-del-provider').forEach((btn) => {
      btn.addEventListener('click', () => {
        const name = btn.dataset.name;
        if (!window.confirm('删除供应商「' + name + '」？它的模型价目会一起删掉。')) return;
        delete this._providers()[name];
        this.dirty = true;
        this.render();
      });
    });
  }

  _onProviderEdit(el) {
    const name = el.dataset.name;
    const prov = this._providers()[name];
    if (!prov) return;
    if (el.classList.contains('pc-name')) {
      const next = (el.value || '').trim();
      if (!next || next === name) { el.value = name; return; }
      if (this._providers()[next]) { window.alert('供应商「' + next + '」已存在'); el.value = name; return; }
      const rebuilt = {};
      for (const [k, v] of Object.entries(this._providers())) rebuilt[k === name ? next : k] = v;
      this.catalog.providers = rebuilt;
      this.dirty = true;
      this.render();
      return;
    }
    if (el.classList.contains('pc-hosts')) {
      prov.hosts = (el.value || '').split(',').map((s) => s.trim()).filter(Boolean);
    } else if (el.classList.contains('pc-billing')) {
      prov.billing = el.value;
    } else if (el.classList.contains('pc-until')) {
      prov.valid_until = el.value || '';
    } else if (el.classList.contains('pc-note')) {
      prov.note = el.value || '';
    }
    this.dirty = true;
    if (el.classList.contains('pc-billing')) this.render();
  }

  _renderModels() {
    const box = document.getElementById('pcModels');
    if (!box) return;
    const provs = this._providers();
    const blocks = Object.entries(provs).map(([name, prov]) => {
      const models = (prov.models && typeof prov.models === 'object') ? prov.models : {};
      const rows = Object.entries(models).map(([key, m]) => {
        if (m.free === true) {
          return `<tr><td>${this._esc(key)}</td><td colspan="7" class="pc-empty">免费（该供应商按免费处理，无需价目）</td>
            <td><button class="pc-btn pc-del-model" data-provider="${this._esc(name)}" data-model="${this._esc(key)}">删除</button></td></tr>`;
        }
        const off = m.off_peak || {};
        const peak = m.peak || {};
        const cell = (tier, k, val) => `<input class="pc-in pc-price" type="number" step="0.001" min="0"
            data-provider="${this._esc(name)}" data-model="${this._esc(key)}" data-tier="${tier}" data-key="${k}" value="${val === undefined || val === null ? '' : val}">`;
        return `<tr>
          <td><input class="pc-in pc-modelkey" data-provider="${this._esc(name)}" data-model="${this._esc(key)}" value="${this._esc(key)}"></td>
          <td><input class="pc-in pc-aliases" data-provider="${this._esc(name)}" data-model="${this._esc(key)}" value="${this._esc((m.aliases || []).join(', '))}" placeholder="deepseek-flash, deepseek-v4-pro"></td>
          <td>${cell('off_peak', 'in_hit', off.in_hit)}</td>
          <td>${cell('off_peak', 'in_miss', off.in_miss)}</td>
          <td>${cell('off_peak', 'out', off.out)}</td>
          <td>${cell('peak', 'in_hit', peak.in_hit)}</td>
          <td>${cell('peak', 'in_miss', peak.in_miss)}</td>
          <td>${cell('peak', 'out', peak.out)}</td>
          <td class="pc-actions">
            <button class="pc-btn pc-double" data-provider="${this._esc(name)}" data-model="${this._esc(key)}" title="峰价 = 谷价 × 倍数">峰=谷×2</button>
            <button class="pc-btn pc-del-model" data-provider="${this._esc(name)}" data-model="${this._esc(key)}">删除</button>
          </td>
        </tr>`;
      }).join('');
      return `<div class="pc-model-block">
        <div class="pc-model-head">
          <span class="pc-model-name">${this._esc(name)}</span>
          <span class="pc-model-billing">${this._esc(prov.billing || 'unknown')}</span>
          <button class="pc-btn pc-add-model" data-provider="${this._esc(name)}">+ 模型</button>
        </div>
        <table class="pc-table">
          <thead><tr><th>模型 key</th><th>别名（同价）</th><th>闲·命中</th><th>闲·未命中</th><th>闲·输出</th><th>峰·命中</th><th>峰·未命中</th><th>峰·输出</th><th></th></tr></thead>
          <tbody>${rows || '<tr><td colspan="9" class="pc-empty">还没有模型价目</td></tr>'}</tbody>
        </table>
        <div class="pc-hint">单位：元 / 百万 token。「别名」用来把 deepseek-flash、deepseek-v4-pro 这类写法收敛到同一价目。</div>
      </div>`;
    }).join('');
    box.innerHTML = blocks || '<div class="pc-empty">先添加供应商，再给它配模型价目</div>';

    box.querySelectorAll('input').forEach((el) => el.addEventListener('change', () => this._onModelEdit(el)));
    box.querySelectorAll('.pc-del-model').forEach((btn) => btn.addEventListener('click', () => {
      const prov = this._providers()[btn.dataset.provider];
      if (!prov || !prov.models) return;
      delete prov.models[btn.dataset.model];
      this.dirty = true;
      this.render();
    }));
    box.querySelectorAll('.pc-add-model').forEach((btn) => btn.addEventListener('click', () => {
      const prov = this._providers()[btn.dataset.provider];
      if (!prov) return;
      if (!prov.models) prov.models = {};
      let key = 'new-model';
      let i = 2;
      while (prov.models[key]) key = 'new-model-' + (i++);
      prov.models[key] = { aliases: [], off_peak: { in_miss: 0, in_hit: 0, out: 0 }, peak: { in_miss: 0, in_hit: 0, out: 0 } };
      this.dirty = true;
      this.render();
    }));
    box.querySelectorAll('.pc-double').forEach((btn) => btn.addEventListener('click', () => {
      const prov = this._providers()[btn.dataset.provider];
      const m = prov && prov.models ? prov.models[btn.dataset.model] : null;
      if (!m) return;
      const mult = Number((this.catalog.peak_schedule || {}).peak_multiplier || 2) || 2;
      const off = m.off_peak || {};
      m.peak = {
        in_miss: Number(off.in_miss || 0) * mult,
        in_hit: Number(off.in_hit || 0) * mult,
        out: Number(off.out || 0) * mult,
      };
      this.dirty = true;
      this.render();
    }));
  }

  _onModelEdit(el) {
    const prov = this._providers()[el.dataset.provider];
    const m = prov && prov.models ? prov.models[el.dataset.model] : null;
    if (!m) return;
    if (el.classList.contains('pc-modelkey')) {
      const next = (el.value || '').trim();
      if (!next || next === el.dataset.model) { el.value = el.dataset.model; return; }
      if (prov.models[next]) { window.alert('模型「' + next + '」已存在'); el.value = el.dataset.model; return; }
      const rebuilt = {};
      for (const [k, v] of Object.entries(prov.models)) rebuilt[k === el.dataset.model ? next : k] = v;
      prov.models = rebuilt;
      this.dirty = true;
      this.render();
      return;
    }
    if (el.classList.contains('pc-aliases')) {
      m.aliases = (el.value || '').split(',').map((s) => s.trim()).filter(Boolean);
    } else if (el.classList.contains('pc-price')) {
      const tier = el.dataset.tier;
      const key = el.dataset.key;
      if (!m[tier]) m[tier] = {};
      m[tier][key] = el.value === '' ? 0 : Number(el.value);
    }
    this.dirty = true;
  }

  _renderSchedule() {
    const box = document.getElementById('pcSchedule');
    if (!box) return;
    const sch = this.catalog.peak_schedule || (this.catalog.peak_schedule = {});
    const weekdays = sch.weekdays || [1, 2, 3, 4, 5];
    const windows = sch.windows || [];
    box.innerHTML = `
      <div class="pc-row">
        <label>时区</label>
        <input class="pc-in pc-tz" value="${this._esc(sch.tz || 'Asia/Shanghai')}">
        <span class="pc-hint">DeepSeek 以北京时间为准；改成 UTC 会把高峰窗口整体挪 8 小时</span>
      </div>
      <div class="pc-row">
        <label>高峰星期</label>
        <span class="pc-weekdays">${[1, 2, 3, 4, 5, 6, 7].map((d) => `<label class="pc-wd"><input type="checkbox" class="pc-wd-cb" value="${d}"${weekdays.indexOf(d) >= 0 ? ' checked' : ''}>${{ 1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '日' }[d]}</label>`).join('')}</span>
      </div>
      <div class="pc-row">
        <label>高峰时段</label>
        <span class="pc-windows">${windows.map((w, i) => `<span class="pc-win"><input class="pc-in pc-win-start" data-i="${i}" value="${this._esc(w[0])}"><span>–</span><input class="pc-in pc-win-end" data-i="${i}" value="${this._esc(w[1])}"><button class="pc-btn pc-del-win" data-i="${i}">×</button></span>`).join('')}
          <button class="pc-btn" id="pcAddWinBtn">+ 时段</button></span>
      </div>
      <div class="pc-row">
        <label>峰价兜底倍数</label>
        <input class="pc-in pc-mult" type="number" step="0.1" min="0" value="${sch.peak_multiplier === undefined ? 2 : sch.peak_multiplier}">
        <span class="pc-hint">模型没显式给峰价时按「谷价 × 倍数」；DeepSeek 恰好是 2 倍</span>
      </div>`;

    const tz = box.querySelector('.pc-tz');
    if (tz) tz.addEventListener('change', () => { sch.tz = tz.value.trim() || 'Asia/Shanghai'; this.dirty = true; });
    const mult = box.querySelector('.pc-mult');
    if (mult) mult.addEventListener('change', () => { sch.peak_multiplier = Number(mult.value) || 0; this.dirty = true; });
    box.querySelectorAll('.pc-wd-cb').forEach((cb) => cb.addEventListener('change', () => {
      sch.weekdays = Array.from(box.querySelectorAll('.pc-wd-cb')).filter((x) => x.checked).map((x) => Number(x.value));
      this.dirty = true;
    }));
    box.querySelectorAll('.pc-win-start,.pc-win-end').forEach((el) => el.addEventListener('change', () => {
      const i = Number(el.dataset.i);
      if (!sch.windows[i]) return;
      sch.windows[i][el.classList.contains('pc-win-start') ? 0 : 1] = el.value.trim();
      this.dirty = true;
    }));
    box.querySelectorAll('.pc-del-win').forEach((btn) => btn.addEventListener('click', () => {
      sch.windows.splice(Number(btn.dataset.i), 1);
      this.dirty = true;
      this.render();
    }));
    const add = document.getElementById('pcAddWinBtn');
    if (add) add.addEventListener('click', () => {
      if (!sch.windows) sch.windows = [];
      sch.windows.push(['09:00', '12:00']);
      this.dirty = true;
      this.render();
    });
  }

  // ── 按次计费服务（Search / Fetch）──────────────────────────
  // 与上面的「供应商 → 模型 → 峰谷价」是**两套口径**：这里是「元 / 每次成功请求」，
  // 不分峰谷（官方 Search/Fetch 一口价），计价走 utils/pricing.calc_service_cost，
  // 落库在 tool step 的 extra.cost(kind='service')，不与 token 成本混算。

  _services() {
    if (!this.catalog.services || typeof this.catalog.services !== 'object') this.catalog.services = {};
    return this.catalog.services;
  }

  _renderServices() {
    const box = document.getElementById('pcServices');
    if (!box) return;
    const rows = Object.entries(this._services()).map(([name, s]) => {
      const billing = s.billing || 'paid';
      const price = Number(s.price || 0);
      // 计费示例按当前口径现算：免费恒 0；未知 = 未计价（不显示成 ¥0）
      const demo = billing === 'free' ? '免费'
        : billing === 'unknown' ? '未计价'
          : `1 次 ¥${price.toFixed(4)} · 100 次 ¥${(price * 100).toFixed(4)}`;
      return `<tr>
        <td><input class="pc-in pc-svcname" data-service="${this._esc(name)}" value="${this._esc(name)}" placeholder="deepseek-search"></td>
        <td>
          <select class="pc-in pc-svcbilling" data-service="${this._esc(name)}">
            ${['paid', 'free', 'unknown'].map((b) => `<option value="${b}"${b === billing ? ' selected' : ''}>${{ paid: '付费', free: '免费', unknown: '未知' }[b]}</option>`).join('')}
          </select>
        </td>
        <td><input class="pc-in pc-svcprice" type="number" step="0.0001" min="0" data-service="${this._esc(name)}" value="${this._esc(s.price === undefined ? '' : s.price)}"></td>
        <td><input class="pc-in pc-svcnote" data-service="${this._esc(name)}" value="${this._esc(s.note || '')}"></td>
        <td class="pc-hint pc-svcdemo">${this._esc(demo)}</td>
        <td><button class="pc-btn pc-del-service" data-service="${this._esc(name)}" title="删除服务">删除</button></td>
      </tr>`;
    }).join('');
    box.innerHTML = `<table class="pc-table">
      <thead><tr><th>服务名</th><th>计费方式</th><th>单价（元 / 次）</th><th>备注</th><th>计费示例</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="pc-empty">还没有按次计费的服务，点右上角「+ 服务」</td></tr>'}</tbody>
    </table>
    <div class="pc-hint">服务名是内部键（代码里写死的调用点）：<code>deepseek-search</code> = web_search 命中 DeepSeek 源、<code>deepseek-fetch</code> = web_fetch 抓取。改名会让成本记成「未计价」。</div>`;

    box.querySelectorAll('input,select').forEach((el) => {
      el.addEventListener('change', () => this._onServiceEdit(el));
    });
    box.querySelectorAll('.pc-del-service').forEach((btn) => {
      btn.addEventListener('click', () => {
        const name = btn.dataset.service;
        if (!window.confirm('删除按次服务「' + name + '」？它之后的调用会被记成「未计价」。')) return;
        delete this._services()[name];
        this.dirty = true;
        this.render();
      });
    });
  }

  _onServiceEdit(el) {
    const name = el.dataset.service;
    const svc = this._services()[name];
    if (!svc) return;
    if (el.classList.contains('pc-svcname')) {
      const next = (el.value || '').trim();
      if (!next || next === name) { el.value = name; return; }
      if (this._services()[next]) { window.alert('按次服务「' + next + '」已存在'); el.value = name; return; }
      const rebuilt = {};
      for (const [k, v] of Object.entries(this._services())) rebuilt[k === name ? next : k] = v;
      this.catalog.services = rebuilt;
      this.dirty = true;
      this.render();
      return;
    }
    if (el.classList.contains('pc-svcbilling')) {
      svc.billing = el.value;
    } else if (el.classList.contains('pc-svcprice')) {
      svc.price = el.value === '' ? 0 : Number(el.value);
    } else if (el.classList.contains('pc-svcnote')) {
      svc.note = el.value || '';
    }
    this.dirty = true;
    // 计费示例列要跟着刷新（单价/计费方式都会改变它）
    if (el.classList.contains('pc-svcbilling') || el.classList.contains('pc-svcprice')) this.render();
  }

  addService() {
    const name = (window.prompt('新服务名（代码里写死的调用键，如 deepseek-search）') || '').trim();
    if (!name) return;
    if (this._services()[name]) { window.alert('已存在同名服务'); return; }
    this._services()[name] = { billing: 'paid', provider: 'deepseek', unit: 'per_request', price: 0.005, note: '' };
    this.dirty = true;
    this.render();
  }

  addProvider() {
    const name = (window.prompt('新供应商名（如 deepseek / opencode-zen）') || '').trim();
    if (!name) return;
    if (this._providers()[name]) { window.alert('已存在同名供应商'); return; }
    this._providers()[name] = { billing: 'paid', hosts: [], note: '', models: {} };
    this.dirty = true;
    this.render();
  }

  // ── 校验 / 保存 / 重置 ──
  async validate() {
    try {
      const res = await window.StockRadar.api.validatePricingCatalog(this.catalog);
      this._showIssues(res, '校验通过：没有发现问题');
    } catch (e) {
      this._msg('error', '校验请求失败：' + (e.message || e));
    }
  }

  async save() {
    try {
      const res = await window.StockRadar.api.savePricingCatalog(this.catalog);
      this.dirty = false;
      this._msg('ok', '已保存（版本 ' + res.version + '）' + (res.backup ? '，上一版备份：' + res.backup : ''));
      if (res.warnings && res.warnings.length) this._showIssues({ warnings: res.warnings }, '已保存，但有提醒');
      await this.load();
    } catch (e) {
      const detail = (e.detail && (e.detail.detail || e.detail)) || e.message || e;
      this._msg('error', '保存被拒绝：' + detail);
    }
  }

  async reset() {
    if (!window.confirm('恢复成仓库内置的默认价目？当前运行时价目会先备份。')) return;
    try {
      const res = await window.StockRadar.api.resetPricingCatalog();
      this._msg('ok', '已恢复默认（版本 ' + res.version + '）');
      await this.load();
    } catch (e) {
      this._msg('error', '恢复失败：' + (e.message || e));
    }
  }

  // ── 试算 ──
  toggleTrial(force) {
    const box = document.getElementById('pcTrial');
    if (!box) return;
    const show = force === undefined ? box.hidden : !!force;
    box.hidden = !show;
  }

  async runTrial() {
    const val = (id) => {
      const el = document.getElementById(id);
      return el ? el.value : '';
    };
    const payload = {
      model: val('pcTrialModel'),
      base_url: val('pcTrialHost'),
      input_tokens: Number(val('pcTrialIn') || 0),
      cached_tokens: Number(val('pcTrialCached') || 0),
      output_tokens: Number(val('pcTrialOut') || 0),
    };
    try {
      const res = await window.StockRadar.api.trialPricingCost(payload);
      const card = (title, c) => `<div class="pc-trial-card">
        <div class="pc-trial-title">${title}</div>
        <div class="pc-trial-money">${c.total === null ? '未计价' : (c.billing === 'free' ? '免费' : '¥' + Number(c.total).toFixed(4))}</div>
        <div class="pc-trial-sub">命中 ¥${Number(c.input_hit || 0).toFixed(4)} · 未命中 ¥${Number(c.input_miss || 0).toFixed(4)} · 输出 ¥${Number(c.output || 0).toFixed(4)}</div>
      </div>`;
      const out = document.getElementById('pcTrialResult');
      if (out) {
        out.innerHTML = `<div class="pc-trial-grid">
            ${card('现在这一刻（' + (res.now.tier === 'peak' ? '高峰' : '闲时') + '）', res.now)}
            ${card('闲时价', res.off_peak)}
            ${card('高峰价', res.peak)}
          </div>
          <div class="pc-hint">供应商 ${this._esc(res.provider || '-')} · 模型 ${this._esc(res.model || '-')} · 计费 ${this._esc(res.billing)} · 高峰时段 ${this._esc((res.peak_windows || []).map((w) => w.join('-')).join(' / ') || '未配置')}（${this._esc(res.peak_tz || '')}）</div>`;
      }
    } catch (e) {
      this._msg('error', '试算失败：' + (e.message || e));
    }
  }

  // ── 工具 ──
  _msg(kind, text) {
    const box = document.getElementById('pcMsgs');
    if (!box) return;
    box.innerHTML = '<div class="pc-msg pc-msg-' + kind + '">' + this._esc(text) + '</div>';
  }

  _showIssues(res, okText) {
    const errs = (res && res.errors) || [];
    const warns = (res && res.warnings) || [];
    const parts = [];
    if (errs.length) parts.push('<div class="pc-msg pc-msg-error">不能保存：<ul>' + errs.map((e) => '<li>' + this._esc(e) + '</li>').join('') + '</ul></div>');
    if (warns.length) parts.push('<div class="pc-msg pc-msg-warn">提醒（不影响保存）：<ul>' + warns.map((e) => '<li>' + this._esc(e) + '</li>').join('') + '</ul></div>');
    if (!parts.length) parts.push('<div class="pc-msg pc-msg-ok">' + this._esc(okText || '没有问题') + '</div>');
    const box = document.getElementById('pcMsgs');
    if (box) box.innerHTML = parts.join('');
  }

  _today() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  _esc(s) {
    const div = document.createElement('div');
    div.textContent = s === undefined || s === null ? '' : String(s);
    return div.innerHTML;
  }
};
