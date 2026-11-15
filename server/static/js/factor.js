/**
 * 因子研究面板 — 36 个技术因子的批量评估（IC / 分层 / 衰减 / 换手）
 *
 * 依赖：window.StockRadar.api.fetchJSON、/static/vendor/echarts.min.js（懒加载）
 * 设计基线：浅色主题、红涨绿跌、分层顺序色阶、零 emoji、分层标签中性。
 */
(function () {
  'use strict';

  const ECHARTS_SRC = '/static/vendor/echarts.min.js';

  const ICON = {
    layers: '<path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    chart: '<path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="7"/><rect x="12" y="6" width="3" height="11"/><rect x="17" y="13" width="3" height="4"/>',
    calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    trending: '<path d="m22 17-8.5-8.5-5 5L2 7"/><path d="M16 17h6v-6"/>',
    shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    close: '<path d="M18 6 6 18M6 6l12 12"/>',
    play: '<path d="m5 3 14 9-14 9V3Z"/>'
  };

  function icon(name, size) {
    const s = size || 14;
    return '<svg width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true">' + (ICON[name] || ICON.info) + '</svg>';
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function fmt(v, digits, opts) {
    const o = opts || {};
    if (v === null || v === undefined || (typeof v === 'number' && !isFinite(v))) return '—';
    let x = Number(v);
    if (!isFinite(x)) return '—';
    if (o.pct) x *= 100;
    const sign = o.signed && x > 0 ? '+' : '';
    return sign + x.toFixed(digits === undefined ? 3 : digits) + (o.pct ? '%' : '');
  }

  /* 模块级 HTML 转义。S1 审计（s1-auditor）发现的 blocking：本文件裸调 esc()
     但 IIFE 内从未定义（只有 this._esc 方法），历史列表/结果卡在浏览器必然
     ReferenceError 且被空 catch 静默吞掉 —— 本文件所有转义统一走它。 */
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  class FactorPanel {
    constructor() {
      this.overlay = document.getElementById('factorOverlay');
      this.meta = null;
      this.health = null;
      this.payload = null;
      this.selected = null;
      this._charts = {};
      this._running = false;
      this._timer = null;
      this._inited = false;
      this._bound = false;
    }

    show() {
      if (!this.overlay) return;
      this.overlay.classList.add('visible');
      if (!this._bound) this._bind();
      this._initOnce();
    }

    hide() {
      if (this.overlay) this.overlay.classList.remove('visible');
    }

    // ── 初始化 ────────────────────────────────────────────
    /* 注意：面板打开**只等 /list**（36 个因子元数据，实测 2ms），其余全部异步补齐。
       2026-10-18 事故：此前 _initOnce 用 Promise.all 等 /health，而 /health 在服务刚启动、
       市场情绪面板后台预热猛跑时**排了 56 秒的队**（日志实测 duration=56438.9ms）——
       用户侧就是「打开因子页要等几十秒」，而慢的根本不是这个接口的活（函数本身 17ms）。
       附属请求一律不阻塞面板，各自回来各自渲染，回不来就显示「不可用」。 */
    async _initOnce() {
      if (this._inited) {
        this._resizeCharts();
        return;
      }
      this._inited = true;
      this._setState('loading', '正在读取因子清单…');
      this._renderPoolStatus();          // 先出「统计中…」占位
      this._renderReports();             // 先出「加载中…」占位
      try {
        this.meta = await this._api('/api/v1/factor/list');
        this._renderFactorList();
        this._setState('idle', '选择参数后点击「运行评估」<br>' +
          '<span class="fr-note">首次计算约 90 秒（36 因子 × 300 标的），之后命中服务端缓存</span>');
      } catch (e) {
        this._setState('error', '加载失败：' + (e && e.message ? e.message : e));
        return;
      }
      // 附属请求：并行发出，**不 await**（谁先回来谁先渲染）
      this._loadHealth();
      this._loadReports();
    }

    async _loadHealth() {
      try {
        this.health = await this._api('/api/v1/factor/health');
      } catch (e) {
        this.health = null;
      }
      this._healthSettled = true;
      this._renderPoolStatus();
    }

    async _api(url) {
      const api = window.StockRadar && window.StockRadar.api;
      if (api && api.fetchJSON) {
        const r = await api.fetchJSON(url);
        return r && r.data !== undefined ? r.data : r;
      }
      const res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const j = await res.json();
      return j && j.data !== undefined ? j.data : j;
    }

    _bind() {
      this._bound = true;
      const close = document.getElementById('frClose');
      if (close) close.onclick = () => this.hide();
      if (this.overlay) {
        this.overlay.addEventListener('click', (e) => {
          if (e.target === this.overlay) this.hide();
        });
      }
      const run = document.getElementById('frRun');
      if (run) run.onclick = () => this.run();
      const end = document.getElementById('frEnd');
      if (end && !end.value) end.value = new Date().toISOString().slice(0, 10);
      window.addEventListener('resize', () => this._resizeCharts());
    }

    _resizeCharts() {
      Object.keys(this._charts).forEach((k) => {
        try { this._charts[k].resize(); } catch (e) { /* ignore */ }
      });
    }

    // ── 侧边因子列表 ──────────────────────────────────────
    _renderFactorList() {
      const box = document.getElementById('frFactorList');
      const count = document.getElementById('frFactorCount');
      const items = (this.meta && this.meta.factors) || [];
      if (count) count.textContent = items.length + ' 个';
      if (!box) return;

      const groups = {};
      const order = [];
      items.forEach((f) => {
        if (!groups[f.category]) { groups[f.category] = []; order.push(f.category); }
        groups[f.category].push(f);
      });

      box.innerHTML = order.map((cat) => {
        const rows = groups[cat].map((f) =>
          '<label class="fr-factor-item" data-fid="' + f.factor_id + '" title="' + this._esc(f.description) + '">' +
          '<input type="checkbox" class="fr-pick" value="' + f.factor_id + '" checked>' +
          '<span class="fr-fname">' + this._esc(f.name) + '</span>' +
          '<span class="fr-fid">' + this._esc(f.factor_id) + '</span>' +
          '</label>').join('');
        return '<div class="fr-cat-group"><div class="fr-cat-title">' + this._esc(cat) +
          '</div>' + rows + '</div>';
      }).join('');

      // 点击名称 = 选中查看详情（不改变勾选）
      box.querySelectorAll('.fr-factor-item').forEach((el) => {
        el.addEventListener('click', (ev) => {
          if (ev.target && ev.target.tagName === 'INPUT') return;
          this._select(el.dataset.fid);
        });
      });
    }

    _picked() {
      const box = document.getElementById('frFactorList');
      if (!box) return [];
      return Array.from(box.querySelectorAll('.fr-pick:checked')).map((el) => el.value);
    }

    _renderPoolStatus() {
      const el = document.getElementById('frPoolStatus');
      if (!el) return;
      const h = this.health;
      if (h) {
        el.classList.remove('pending', 'failed');
        el.textContent = '标的池 ' + h.with_240_rows + '/' + h.universe +
          ' 就绪 · 快照 ' + (h.as_of_date || '—');
        return;
      }
      // 还没回来 / 失败：给明确状态，别让用户以为面板坏了
      el.classList.add(this._healthSettled ? 'failed' : 'pending');
      el.textContent = this._healthSettled ? '标的池统计不可用（不影响使用）' : '标的池统计中…';
    }

    // ── 运行 ──────────────────────────────────────────────
    async run() {
      if (this._running) return;
      const picked = this._picked();
      if (!picked.length) {
        this._setState('error', '请至少勾选一个因子');
        return;
      }
      const all = (this.meta && this.meta.factors || []).length;

      this._running = true;
      const btn = document.getElementById('frRun');
      if (btn) { btn.disabled = true; btn.innerHTML = icon('play', 12) + ' 计算中…'; }

      /* F7：不再用 90 秒阻塞 GET（零进度、不可取消）。改为
         POST /run → 后台线程 → SSE 推 phase 事件驱动真进度条。 */
      this._setState('loading',
        '正在评估 <b>' + picked.length + '</b> 个因子…' +
        '<div class="fr-prog-wrap">' +
        '<div class="fr-prog"><div class="fr-prog-fill" id="frProgFill" style="width:2%"></div></div>' +
        '<div class="fr-prog-meta"><span id="frPhase">提交任务…</span><span id="frPct">2%</span></div></div>' +
        '<button class="btn" id="frCancel" style="margin-top:10px">取消</button>');

      const self = this;
      let es = null;
      const finish = () => {
        self._running = false;
        if (es) { try { es.close(); } catch (e) { /* noop */ } es = null; }
        if (btn) { btn.disabled = false; btn.innerHTML = icon('play', 12) + ' 运行评估'; }
      };
      const cancelBtn = document.getElementById('frCancel');
      if (cancelBtn) cancelBtn.onclick = async () => {
        if (self._runUuid) {
          try { await fetch('/api/v1/factor/runs/' + encodeURIComponent(self._runUuid) + '/cancel', { method: 'POST' }); } catch (e) { /* noop */ }
        }
      };

      try {
        const d = await this._post('/api/v1/factor/run', {
          index_code: (document.getElementById('frIndex') || {}).value || '000300',
          start: (document.getElementById('frStart') || {}).value || '2024-10-01',
          end: (document.getElementById('frEnd') || {}).value || '',
          ret_period: Number((document.getElementById('frRetPeriod') || {}).value || 5),
          quantiles: Number((document.getElementById('frQuantiles') || {}).value || 5),
          neutralize: (document.getElementById('frNeutralize') || {}).value || 'none',
          lag: 1, min_names: 30,
          factor_ids: picked.length < all ? picked.join(',') : null,
          top: 10
        });
        this._runUuid = d && d.run_uuid;
        if (!this._runUuid) throw new Error('服务端未返回 run_uuid');

        es = new EventSource('/api/v1/factor/runs/' + encodeURIComponent(this._runUuid) + '/events');
        es.addEventListener('phase', (ev) => {
          let p = {}; try { p = JSON.parse(ev.data); } catch (e) { return; }
          const fill = document.getElementById('frProgFill');
          const ph = document.getElementById('frPhase');
          const pc = document.getElementById('frPct');
          if (ph) ph.textContent = (p.label || p.phase || '') + (p.total ? '（' + p.index + '/' + p.total + '）' : '');
          const pct = p.phase === 'evaluate'
            ? (p.index / Math.max(p.total, 1)) * 100
            : Math.min((p.index || 0) / 3 * 100, 95);
          if (fill) fill.style.width = Math.min(100, Math.max(2, pct)).toFixed(1) + '%';
          if (pc) pc.textContent = Math.round(Math.min(100, Math.max(2, pct))) + '%';
        });
        es.addEventListener('final', (ev) => {
          let d2 = {}; try { d2 = JSON.parse(ev.data); } catch (e) { /* noop */ }
          finish();
          if (d2.status === 'done' && d2.report_uuid) {
            this._openReport(d2.report_uuid);
          } else if (d2.status === 'cancelled') {
            this._setState('idle', '已取消。可重新运行，历史报告不受影响。');
          }
        });
        es.addEventListener('error', (ev) => {
          // 无 data 的 'error' 是 EventSource 原生连接错误（不是服务端事件），
          // 不能当「评估失败」——结果已落库，可回列表查看。
          if (!ev || !ev.data) { finish(); return; }
          let d2 = {}; try { d2 = JSON.parse(ev.data); } catch (e) { /* noop */ }
          finish();
          this._setState('error', '评估失败：' + ((d2 && d2.message) || '原因未记录'));
        });
        es.onerror = () => { /* final 有 DB 兜底，不自行重连（连接数上限） */ };
      } catch (e) {
        finish();
        this._setState('error', '提交失败：' + (e && e.message ? e.message : e));
      }
    }

    async _post(url, body) {
      const api = window.StockRadar && window.StockRadar.api;
      if (api && api.fetchJSON) {
        const r = await api.fetchJSON(url, { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body || {}) });
        return r && r.data !== undefined ? r.data : r;
      }
      const res = await fetch(url, { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const j = await res.json();
      return j && j.data !== undefined ? j.data : j;
    }

    /* ── 历史报告（F1 持久化）────────────────────────────── */
    async _loadReports() {
      try {
        const d = await this._api('/api/v1/factor/reports?limit=30');
        this._reports = (d && d.reports) || [];
      } catch (e) {
        this._reports = [];      // 失败也要落定，否则侧栏永远停在「加载中」
      }
      this._renderReports();
    }

    /* 注意：列表未回来时先出**占位且不可点**，不要让侧栏空着也无法判断状态。
       payload 文件已被清理的报告（available=false）置灰 + 不可点 + 说明原因，
       点进去只会报错（2026-10-18）。 */
    _renderReports() {
      const box = document.getElementById('frReports');
      const count = document.getElementById('frReportCount');
      const list = this._reports;
      if (count) count.textContent = list ? (list.length ? list.length + ' 份' : '—') : '…';
      if (!box) return;
      if (!list) {
        box.innerHTML = '<div class="fr-report-item pending"><div class="t">历史报告加载中…</div>' +
          '<div class="m">稍候即可点击</div></div>';
        return;
      }
      if (!list.length) {
        box.innerHTML = '<div class="fr-report-item"><div class="t muted">暂无历史报告（跑一次就有了）</div></div>';
        return;
      }
      box.innerHTML = list.map((r) => {
        const k = r.kpi || {};
        const off = r.available === false;
        return '<div class="fr-report-item' + (r.report_uuid === this._activeReport ? ' on' : '') +
          (off ? ' off' : '') + '"' +
          (off ? ' title="报告数据文件已清理，无法打开（可重跑一次）"' :
                 ' data-report="' + esc(r.report_uuid) + '"') + '>' +
          '<div class="t">' + esc(r.title || r.report_uuid) + (off ? '（已清理）' : '') + '</div>' +
          '<div class="m">' + esc((r.params && r.params.neutralize) || '') +
          ' · DSR 通过 ' + (k.dsr_pass != null ? k.dsr_pass : '—') + '</div>' +
          '<div class="m">' + esc((r.start_date || '') + ' ~ ' + (r.end_date || '')) +
          ' · ' + esc(r.created_at || '') + '</div></div>';
      }).join('');
      box.querySelectorAll('[data-report]').forEach((el) => {
        el.onclick = () => this._openReport(el.dataset.report);
      });
    }

    async _openReport(uuid) {
      this._activeReport = uuid;
      this._setState('loading', '读取报告 ' + esc(uuid) + '…');
      try {
        const d = await this._api('/api/v1/factor/reports/' + encodeURIComponent(uuid));
        if (!d || !d.payload) {
          this._setState('error', '该报告的数据文件已被清理（左侧摘要仍在）。重新跑一次即可。');
          return;
        }
        this.payload = d.payload;
        this.selected = null;
        this._renderAll();
        document.querySelectorAll('#frReports [data-report]').forEach((el) => {
          el.classList.toggle('on', el.dataset.report === uuid);
        });
        this._loadReports();      // 刷新列表（新增的排序会变）
      } catch (e) {
        this._setState('error', '读取失败：' + (e && e.message ? e.message : e));
      }
    }

    // ── 渲染 ──────────────────────────────────────────────
    _renderAll() {
      const p = this.payload;
      if (!p || !p.ranking || !p.ranking.length) {
        this._setState('idle', '没有可用结果');
        return;
      }
      this._setState('ready', '');
      this._renderKpis(p);
      this._renderSig(p);
      this._renderTable(p);
      const first = p.ranking[0].factor_id;
      this._select(this.selected && p.factors[this.selected] ? this.selected : first);
    }

    _renderKpis(p) {
      const el = document.getElementById('frKpis');
      if (!el) return;
      const cov = p.coverage || {};
      const rank = p.ranking || [];
      const top = rank[0] || {};
      const sharpe = rank.slice().sort((a, b) =>
        Math.abs((b.long_short_sharpe || 0)) - Math.abs((a.long_short_sharpe || 0)))[0] || {};
      const passed = rank.filter((r) => (r.dsr || 0) >= 0.95).length;
      const sig = p.significance || {};

      const card = (label, value, sub, cls) =>
        '<div class="fr-kpi"><div class="label">' + label + '</div>' +
        '<div class="value ' + (cls || '') + '">' + value + '</div>' +
        (sub ? '<div class="sub">' + this._esc(sub) + '</div>' : '') + '</div>';

      el.innerHTML = [
        card('评估因子', cov.evaluated || 0, '失败 ' + (cov.failed || 0) + ' · 标的 ' + (cov.n_codes || 0)),
        card('截面期数', cov.n_dates || 0, '样本 ' + (cov.n_obs || 0).toLocaleString()),
        card('最高 |RankIC|', fmt(top.rank_ic_mean, 4, { signed: true }), top.name || '',
          (top.rank_ic_mean || 0) > 0 ? 'pos' : 'neg'),
        card('最高多空夏普', fmt(sharpe.long_short_sharpe, 3), sharpe.name || ''),
        card('DSR ≥ 0.95', passed, 'N = ' + (sig.n_trials || '—') + ' 次试验'),
        card('单调分层', rank.filter((r) => Math.abs(r.monotonicity || 0) > 0.9).length, '|ρ| > 0.9')
      ].join('');
    }

    _renderSig(p) {
      const el = document.getElementById('frSig');
      if (!el) return;
      const s = p.significance || {};
      const ap = (p.appendix && p.appendix.summary) || {};
      el.innerHTML = '<span>' + icon('shield', 13) + ' 多重检验校正</span>' +
        '<span>试验次数 <b>N = ' + (s.n_trials || '—') + '</b></span>' +
        '<span>试验夏普 σ <b>' + fmt(s.sr_std, 4) + '</b></span>' +
        '<span>中性化 <b>' + this._esc(ap.neutralize || '未做') + '</b></span>' +
        '<span>DSR ≥ 0.95 才视为通过校正；N 越大门槛越高</span>';
    }

    _renderTable(p) {
      const el = document.getElementById('frRankTable');
      if (!el) return;
      const rows = (p.ranking || []).map((r) => {
        const cls = (r.rank_ic_mean || 0) > 0 ? 'pos' : 'neg';
        const pass = (r.dsr || 0) >= 0.95
          ? '<span class="fr-pass yes">通过</span>'
          : '<span class="fr-pass no">未通过</span>';
        return '<tr data-fid="' + r.factor_id + '">' +
          '<td class="rank">' + r.rank + '</td>' +
          '<td>' + this._esc(r.name) + '</td>' +
          '<td><span class="fr-cat-tag">' + this._esc(r.category) + '</span></td>' +
          '<td class="' + cls + '">' + fmt(r.rank_ic_mean, 4, { signed: true }) + '</td>' +
          '<td>' + fmt(r.ic_mean, 4, { signed: true }) + '</td>' +
          '<td>' + fmt(r.icir, 3) + '</td>' +
          '<td>' + fmt(r.t_value, 2) + '</td>' +
          '<td>' + fmt(r.ic_win_rate, 0, { pct: true }) + '</td>' +
          '<td>' + fmt(r.long_short_sharpe, 2) + '</td>' +
          '<td>' + fmt(r.dsr, 3) + '</td>' +
          '<td>' + pass + '</td></tr>';
      }).join('');

      el.innerHTML = '<thead><tr><th>#</th><th>因子</th><th>类别</th><th>RankIC</th>' +
        '<th>IC</th><th>ICIR</th><th>t</th><th>胜率</th><th>多空夏普</th><th>DSR</th>' +
        '<th>校正</th></tr></thead><tbody>' + rows + '</tbody>';

      el.querySelectorAll('tbody tr').forEach((tr) => {
        tr.addEventListener('click', () => this._select(tr.dataset.fid));
      });
    }

    _select(fid) {
      if (!fid || !this.payload || !this.payload.factors) return;
      const f = this.payload.factors[fid];
      if (!f) return;
      this.selected = fid;

      document.querySelectorAll('#frRankTable tbody tr').forEach((tr) => {
        tr.classList.toggle('active', tr.dataset.fid === fid);
      });
      document.querySelectorAll('.fr-factor-item').forEach((el) => {
        el.classList.toggle('active', el.dataset.fid === fid);
      });

      const head = document.getElementById('frDetailHead');
      if (head) {
        head.innerHTML = '<span class="name">' + this._esc(f.name) + '</span>' +
          '<span class="fid">' + this._esc(f.factor_id) + '</span>' +
          '<span class="desc">' + this._esc(f.description) + '</span>';
      }
      this._renderCharts(f);
    }

    async _ensureEcharts() {
      if (window.echarts) return window.echarts;
      const loader = window.StockRadar && window.StockRadar.utils &&
        window.StockRadar.utils.loadScriptOnce;
      if (loader) {
        try {
          await loader(ECHARTS_SRC, 'echarts');
        } catch (e) { /* fallthrough */ }
      } else {
        await new Promise((resolve) => {
          const s = document.createElement('script');
          s.src = ECHARTS_SRC;
          s.onload = resolve;
          s.onerror = resolve;
          document.head.appendChild(s);
        });
      }
      return window.echarts || null;
    }

    _setChart(id, option) {
      const el = document.getElementById(id);
      if (!el || !window.echarts) return;
      let c = this._charts[id];
      if (c) { try { c.dispose(); } catch (e) { /* ignore */ } }
      c = window.echarts.init(el);
      c.setOption(option);
      this._charts[id] = c;
    }

    async _renderCharts(f) {
      const echarts = await this._ensureEcharts();
      if (!echarts) {
        this._setState('error', '图表库加载失败：' + ECHARTS_SRC);
        return;
      }
      const up = cssVar('--up', '#e74c3c');
      const down = cssVar('--down', '#176b5b');
      const qColors = [
        cssVar('--fr-q1', '#dcebe8'), cssVar('--fr-q2', '#8fc0b5'), cssVar('--fr-q3', '#3f8f7c'),
        cssVar('--fr-q4', '#8a6a1f'), cssVar('--fr-q5', '#b45309')
      ];
      const grid = { left: 42, right: 14, top: 26, bottom: 28 };
      const axis = (label) => ({
        axisLine: { lineStyle: { color: '#e5e7eb' } },
        axisTick: { show: false },
        axisLabel: { color: '#6b7280', fontSize: 10, fontFamily: 'monospace' },
        splitLine: { lineStyle: { color: '#f1f3f5' } }
      });
      const tooltip = { trigger: 'axis', backgroundColor: '#fff', borderColor: '#e5e7eb',
        textStyle: { color: '#1f2937', fontSize: 11 } };
      const legend = { top: 0, right: 0, itemWidth: 10, itemHeight: 8,
        textStyle: { fontSize: 10, color: '#6b7280' } };

      // 1) IC / RankIC 时序
      const ic = f.ic_series || { dates: [], series: [] };
      this._setChart('frChartIC', {
        tooltip: tooltip, grid: grid, legend: legend,
        xAxis: { type: 'category', data: ic.dates, ...axis(), splitLine: { show: false } },
        yAxis: { type: 'value', ...axis() },
        series: (ic.series || []).map((s, i) => ({
          name: s.name, type: 'line', data: s.data, showSymbol: false, connectNulls: false,
          lineStyle: { width: i === 2 ? 1.3 : 1, type: i === 2 ? 'dashed' : 'solid' },
          itemStyle: { color: [cssVar('--accent', '#2f6f9f'), cssVar('--strategy-accent', '#6d4bc4'), cssVar('--brand', '#176b5b')][i] },
          markLine: i === 0 ? { silent: true, symbol: 'none',
            lineStyle: { color: '#cbd5e1', type: 'dashed', width: 1 },
            label: { show: false }, data: [{ yAxis: 0 }] } : undefined
        }))
      });

      // 2) 分层净值
      const eq = f.quantile_equity || { dates: [], series: [] };
      const s2 = (eq.series || []).map((s, i) => ({
        name: s.name, type: 'line', data: s.data, showSymbol: false,
        lineStyle: { width: 1.3 }, itemStyle: { color: qColors[i] }
      }));
      if (eq.long_short) s2.push({ name: eq.long_short.name, type: 'line',
        data: eq.long_short.data, showSymbol: false, lineStyle: { width: 2 },
        itemStyle: { color: up }, z: 5 });
      if (eq.benchmark) s2.push({ name: eq.benchmark.name, type: 'line',
        data: eq.benchmark.data, showSymbol: false,
        lineStyle: { width: 1, type: 'dashed' }, itemStyle: { color: '#9ca3af' } });
      this._setChart('frChartEquity', {
        tooltip: tooltip, grid: grid, legend: legend,
        xAxis: { type: 'category', data: eq.dates, ...axis(), splitLine: { show: false } },
        yAxis: { type: 'value', scale: true, ...axis() },
        series: s2
      });

      // 3) IC 衰减
      const dec = f.ic_decay || { periods: [], rows: [] };
      this._setChart('frChartDecay', {
        tooltip: { trigger: 'axis', backgroundColor: '#fff', borderColor: '#e5e7eb',
          textStyle: { color: '#1f2937', fontSize: 11 },
          formatter: (ps) => {
            const r = (dec.rows || [])[ps[0].dataIndex] || {};
            return r.period + ' 日<br/>IC 均值 ' + fmt(r.ic_mean, 4) +
              '<br/>ICIR ' + fmt(r.icir, 3) + '<br/>t ' + fmt(r.t_value, 2);
          } },
        grid: grid,
        xAxis: { type: 'category', data: (dec.periods || []).map((p) => p + '日'),
          ...axis(), splitLine: { show: false } },
        yAxis: { type: 'value', ...axis() },
        series: [{ type: 'bar', barWidth: '46%',
          data: (dec.rows || []).map((r) => ({
            value: r.ic_mean,
            itemStyle: { color: (r.ic_mean || 0) >= 0 ? up : down }
          })),
          markLine: { silent: true, symbol: 'none',
            lineStyle: { color: '#cbd5e1', type: 'dashed' },
            label: { show: false }, data: [{ yAxis: 0 }] } }]
      });

      // 4) 换手率
      const to = f.turnover || { dates: [], series: [] };
      this._setChart('frChartTurnover', {
        tooltip: { trigger: 'axis', backgroundColor: '#fff', borderColor: '#e5e7eb',
          textStyle: { color: '#1f2937', fontSize: 11 },
          valueFormatter: (v) => (v === null ? '—' : (v * 100).toFixed(1) + '%') },
        grid: grid, legend: legend,
        xAxis: { type: 'category', data: to.dates, ...axis(), splitLine: { show: false } },
        yAxis: { type: 'value', max: 1, ...axis(),
          axisLabel: { color: '#6b7280', fontSize: 10, formatter: (v) => (v * 100).toFixed(0) + '%' } },
        series: (to.series || []).map((s, i) => ({
          name: s.name, type: 'line', data: s.data, showSymbol: false,
          lineStyle: { width: 1.1 }, itemStyle: { color: qColors[i] }
        }))
      });
    }

    // ── 状态 ──────────────────────────────────────────────
    _setState(kind, html) {
      const state = document.getElementById('frState');
      const content = document.getElementById('frContent');
      if (!state || !content) return;
      if (kind === 'ready') {
        state.style.display = 'none';
        content.hidden = false;
        return;
      }
      state.style.display = 'flex';
      content.hidden = true;
      const spinner = kind === 'loading' ? '<div class="fr-spinner"></div>' : '';
      const ic = kind === 'error' ? icon('alert', 22) : icon('layers', 22);
      state.className = 'fr-state' + (kind === 'error' ? ' error' : '');
      state.innerHTML = spinner + '<div class="fr-state-icon">' + (kind === 'loading' ? '' : ic) + '</div>' +
        '<div>' + html + '</div>';
    }

    _esc(s) {
      return String(s === null || s === undefined ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
  }

  window._factorPanel = null;
  window.initFactorPanel = function () {
    if (!window._factorPanel) window._factorPanel = new FactorPanel();
    return window._factorPanel;
  };
})();
