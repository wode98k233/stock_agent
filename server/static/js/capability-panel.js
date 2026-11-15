/* 引擎能力清单 + 扩展提议面板（AI 出方案 → 人审）
   设计取向：这一页是「引擎脚手架」的可见面 ——
   上半是引擎现在能表达什么（export_capabilities 的单一事实源），
   下半是模型想要但引擎没有的东西（能力缺口）转成的扩展方案，等人拍板。
   ⛔ 批准不会让引擎立刻支持它（effective=false），真正落地仍走开发流程。 */
window.StockRadar = window.StockRadar || {};

window.StockRadar.CapabilityPanel = class CapabilityPanel {
  constructor() {
    this.panel = document.getElementById('capPanel');
    this.overlay = document.getElementById('capOverlay');
    this.lastRunUuid = '';
    this._bind();
  }

  _bind() {
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener('click', fn); };
    on('capCloseBtn', () => this.close());
    on('capOverlay', () => this.close());
    on('capReloadBtn', () => this.load());
    on('capFromRunBtn', () => this.proposeFromRun());
  }

  open() {
    if (this.overlay) this.overlay.classList.add('open');
    if (this.panel) this.panel.classList.add('open');
    this.load();
  }

  close() {
    if (this.overlay) this.overlay.classList.remove('open');
    if (this.panel) this.panel.classList.remove('open');
  }

  async load() {
    const api = window.StockRadar.api;
    try {
      const [cat, props] = await Promise.all([
        api.getCapabilityCatalog(),
        api.getCapabilityProposals(''),
      ]);
      this._renderCatalog(cat || {});
      this._renderProposals((props && props.items) || []);
      this._msg('info', '能力表与提议都是实时读盘：改引擎后刷新即可看到；提议目录 data/capability_proposals/');
    } catch (e) {
      this._msg('error', '读取失败：' + (e.message || e));
    }
  }

  _renderCatalog(cap) {
    const box = document.getElementById('capCatalog');
    if (!box) return;
    const indicators = Object.keys(cap.indicators || {}).join('、');
    const specials = Object.entries(cap.special_forms || {})
      .map(([k, v]) => '<li><b>' + this._esc(k) + '</b>：' + this._esc(v) + '</li>').join('');
    box.innerHTML = `
      <div class="cap-kv"><b>数据字段</b>：${this._esc((cap.data_fields || []).join('、'))}</div>
      <div class="cap-kv"><b>指标</b>：${this._esc(indicators)}</div>
      <div class="cap-kv"><b>算子</b>：${this._esc((cap.operators || []).join('、'))}</div>
      <div class="cap-kv"><b>时序</b>：${this._esc(JSON.stringify(cap.temporal || {}, null, 0))}</div>
      <div class="cap-kv"><b>成交口径</b>：${this._esc((cap.timings || []).join('、'))}</div>
      <div class="cap-kv"><b>特殊写法</b>：<ul class="cap-ul">${specials}</ul></div>`;
  }

  _renderProposals(items) {
    const box = document.getElementById('capProposals');
    if (!box) return;
    if (!items.length) {
      box.innerHTML = '<div class="cap-empty">还没有提议。跑一次策略研发后点右上角「按缺口生成方案」，'
        + '或在研究面板里看到「引擎表达不了 X」时来这里生成。</div>';
      return;
    }
    box.innerHTML = items.map((p, i) => {
      const def = p.definition || {};
      const params = (def.params || []).map((x) => x.name + '=' + x.default).join(', ');
      const risks = (p.risks || []).map((r) => '<li>' + this._esc(r) + '</li>').join('');
      const tests = (p.test_plan || []).map((r) => '<li>' + this._esc(r) + '</li>').join('');
      const status = String(p.status || '');
      const badge = status === 'approved' ? '✅ 已批准（引擎还没支持）'
        : (status === 'rejected' ? '❌ 已拒绝' : '⏳ 待审');
      return `<div class="cap-card" data-i="${i}">
        <div class="cap-card-head">
          <b>${this._esc(p.kind)} / ${this._esc(p.name)}</b>
          <span class="cap-badge ${status}">${badge}</span>
        </div>
        <div class="cap-kv">${this._esc(p.summary || '')}</div>
        <div class="cap-kv"><b>语义</b>：${this._esc(def.semantics || '—')}</div>
        <div class="cap-kv"><b>公式</b>：<code>${this._esc(def.formula || '—')}</code></div>
        <div class="cap-kv"><b>数据来源</b>：${this._esc(def.data_source || '—')}${params ? ' · <b>参数</b>：' + this._esc(params) : ''}</div>
        <div class="cap-kv"><b>要动引擎</b>：${this._esc(p.engine_impact || '—')}</div>
        ${risks ? '<div class="cap-kv"><b>风险</b><ul class="cap-ul">' + risks + '</ul></div>' : ''}
        ${tests ? '<div class="cap-kv"><b>验收计划</b><ul class="cap-ul">' + tests + '</ul></div>' : ''}
        <div class="cap-kv cap-mono">${this._esc(JSON.stringify(p.sample_rule_json || {}))}</div>
        ${status === 'pending_review' ? `<div class="cap-actions">
          <button class="cap-btn primary" data-act="approve">批准方案</button>
          <button class="cap-btn" data-act="reject">拒绝</button>
        </div>` : ''}
      </div>`;
    }).join('');
    box.querySelectorAll('.cap-card').forEach((card) => {
      const item = items[Number(card.dataset.i)] || {};
      card.querySelectorAll('button[data-act]').forEach((btn) => {
        btn.addEventListener('click', () => this.review(item, btn.dataset.act));
      });
    });
  }

  async review(item, act) {
    const decision = act === 'approve' ? 'approved' : 'rejected';
    const note = window.prompt(decision === 'approved' ? '批准备注（可留空）' : '拒绝原因（可留空）', '') || '';
    try {
      await window.StockRadar.api.reviewCapabilityProposal({
        path: item.path || '', decision, note,
      });
      this._msg('ok', (decision === 'approved' ? '已批准' : '已拒绝') + '：' + (item.name || '')
        + (decision === 'approved' ? '（引擎还没支持它，需走开发流程落地）' : ''));
      this.load();
    } catch (e) {
      this._msg('error', '审批失败：' + (e.message || e));
    }
  }

  async proposeFromRun() {
    let runUuid = this.lastRunUuid;
    if (!runUuid) runUuid = window.prompt('按哪次研发的缺口生成？（填 run_uuid，如 SR-20261014-57282A）', '') || '';
    if (!runUuid) return;
    this._msg('info', '正在让 AI 出方案…（每个缺口一次 LLM 调用，最多 3 条）');
    try {
      const res = await window.StockRadar.api.proposeCapabilityFromRun(runUuid);
      const gaps = res.gaps || [];
      if (!gaps.length) {
        this._msg('ok', '这次研发没有记录到能力缺口（引擎都表达得了）');
        return;
      }
      this._msg('ok', '缺口 ' + gaps.length + ' 项，已生成方案 ' + (res.generated || 0) + ' 条'
        + '：' + gaps.map((g) => g.name).join('、'));
      this.load();
    } catch (e) {
      this._msg('error', '生成失败：' + (e.message || e));
    }
  }

  _msg(kind, text) {
    const box = document.getElementById('capMsgs');
    if (!box) return;
    box.innerHTML = '<div class="cap-msg cap-msg-' + kind + '">' + this._esc(text) + '</div>';
  }

  _esc(s) {
    const d = document.createElement('div');
    d.textContent = s === undefined || s === null ? '' : String(s);
    return d.innerHTML;
  }
};

window.StockRadar.capabilityPanel = new window.StockRadar.CapabilityPanel();
