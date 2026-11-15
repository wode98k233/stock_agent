/**
 * 研究台账页 —— 数据来自 /api/thesis/*（list / {id} / refresh / history / alerts）
 *
 * 台账 = 「研究结论的可执行化」：程序按定义口径重复取数、打分、留快照，
 * 并在结论变化时给出告警。本页只做展示与触发刷新，不重算任何东西。
 *
 * 布局约定见 css/thesis.css 头注释（纯 flex/grid，不用 JS 测高）。
 */
(function () {
  'use strict';

  var ECHARTS_CDN = '/static/vendor/echarts.min.js';  // 本地 vendor 优先：jsdelivr 在部分网络（含本机）不可达，会让全站图表静默不渲染
  var SEV_LABEL = { critical: '严重', warn: '注意', info: '提示' };
  var SCORE_CLASS = { '2': 's2', '1': 's1', '0': 's0', '-1': 'sm', '-2': 'sm' };
  var STATUS_COLOR = {
    '很有利': 'var(--up, #e74c3c)', '有利': 'var(--brand, #176b5b)',
    '中性': 'var(--warn, #b45309)', '偏不利': 'var(--down, #176b5b)',
    '不利': 'var(--down, #176b5b)', '数据缺失': 'var(--border-main, #8f8e88)'
  };

  function $(id) { return document.getElementById(id); }
  function page() { return $('thPage'); }
  function esc(s) {
    var u = window.StockRadar && window.StockRadar.utils;
    return (u && u.esc) ? u.esc(s == null ? '' : String(s))
      : String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
  }

  // 先转义再做最小化的 **加粗**（口径说明等长文案由定义提供，允许极简 md）
  function _mdBold(s) {
    return esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  }

  var _bound = false;
  var _items = [];
  var _active = null;
  var _chart = null;          // 历史趋势（总分柱 + 因子分数折线）
  var _attrChart = null;      // 变动归因横条
  var _evCache = {};          // "tid|factor" → evidence 载荷（懒加载后保留，切换标签不再回放）
  var _evCharts = {};         // factorKey → echarts 实例数组（切换/关闭时释放，避免隐藏容器里堆图）
  // 因子证据停靠区：点开多个因子时不再把卡片撑开/挤走其它卡，而是统一在
  // 因子网格下方展示，标签页切换。_evOpen 是「已打开」的 key 列表（按点击顺序），
  // _evActive 是当前展示的那一个；全部打开也只渲染当前标签的 4 张图。
  var _evOpen = [];
  var _evActive = null;
  var _evTid = null;
  var _reportBusy = false;
  var _reportModal = null;
  // 报告 PNG 缓存：{thesis_id: {blob, rep, at}}。后端 /report 也有 30 分钟缓存，
  // 这里再缓存 blob，避免同一张台账「生成→看→调格式→再点」重画图表+重编码 PNG。
  var REPORT_CACHE_TTL_MS = 30 * 60 * 1000;
  var _reportCache = {};
  var _busy = false;

  // 因子折线配色：A 股语义色优先（红=有利/绿=不利），后续用可区分的辅助色。
  var FACTOR_COLORS = ['#e74c3c', '#176b5b', '#b45309', '#7b5ea7', '#8f8e88',
                       '#b05a1e', '#a9a8a1', '#2e9e6b', '#a03050', '#4b7ba8'];
  /* 组合层是「元层」：其他台账都是它的输入，故在左侧列表单独占一个入口，
     不混进 _items（_items 是 data/thesis/*.json 的台账定义）。 */
  var PORTFOLIO_ID = '__portfolio__';

  // ── 打开 / 关闭 ──────────────────────────────────────────────────

  function openPage() {
    var p = page();
    if (!p) return;
    p.classList.add('open');
    var chat = document.querySelector('.chat-pane');
    if (chat) chat.style.display = 'none';
    bindEvents();
    load();
  }

  function closePage() {
    var p = page();
    if (p) p.classList.remove('open');
    // 关页面前把图全释放：否则 ECharts 实例会挂在已隐藏的容器上继续吃内存/监听
    _disposeCharts();
    _evHideDock();
    var chat = document.querySelector('.chat-pane');
    if (chat) chat.style.display = '';
  }

  function bindEvents() {
    if (_bound) return;
    _bound = true;
    $('thRefreshBtn')?.addEventListener('click', refreshActive);
    // 双轴总览：每次点开都重新拉一遍（后端按 def_hash 缓存，热缓存很快）
    $('thChartBtn')?.addEventListener('click', function () { openScorePanel(false); });
    // 「↻ 重新拉取」= 强制刷新：跳过后端 1h 缓存直接重算，拿到最新数据日
    $('thChartReload')?.addEventListener('click', function () { openScorePanel(true); });
    $('thChartClose')?.addEventListener('click', function () { closeScorePanel(); });
    // 包一层：addEventListener 会把 Event 当第一个参数传进来，若直接绑
    // generateReport(force) 会把「事件对象」当成 force=true，缓存永远失效。
    $('thReportBtn')?.addEventListener('click', function () { generateReport(false); });
    // 因子有效性汇总 / 因子「关联性证据」/ 散点口径切换都由 renderDetail 动态生成
    // （每次切台账都会重建），故用事件委托而不是逐个绑定——否则切换台账后按钮就失效了。
    document.addEventListener('click', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      // 双轴总览的卡片：点一下关掉总览并进该台账详情（面板里的图随之释放）
      var sc = t.closest('.th-chart-card');
      if (sc && sc.dataset && sc.dataset.tid) {
        closeScorePanel();
        loadDetail(sc.dataset.tid);
        return;
      }
      var b = t.closest('#thEvAllBtn');
      if (b) { _loadEvidenceBatch(b.dataset.tid, b); return; }
      var ev = t.closest('.th-ev-btn');
      if (ev) { _toggleEvidence(ev.dataset.tid, ev.dataset.key, ev); return; }
      var tab = t.closest('.th-ev-tab');
      if (tab) {
        var x = t.closest('.th-ev-tab-x');
        if (x) { _evClose(x.dataset.evClose); }
        else { _evActivate(tab.dataset.evTab); }
        return;
      }
      if (t.closest('#thEvCloseAll')) { _evCloseAll(); return; }
      if (t.closest('#thEvClosePanel')) { _evCloseAll(); return; }
      var mode = t.closest('.th-ev-mode');
      if (mode) { _setEvScatterMode(mode.dataset.key, mode.dataset.mode, mode); }
    });
    // 抽屉里的「添加因子」：卡片被抽屉挡住时仍能继续打开其它因子
    document.addEventListener('change', function (e) {
      var t = e.target;
      if (!t || t.id !== 'thEvAdd' || !t.value) return;
      var key = t.value;
      t.value = '';
      _toggleEvidence(_evTid, key, null);
    });
    // 报告预览弹窗按 Esc 关闭（预览图很大，挡住整页时最直觉的退出方式）；
    // 双轴总览开着时优先关总览，避免「按 Esc 把底下的报告预览关了、总览还在」。
    document.addEventListener('keydown', function (e) {
      if (!(e && e.key === 'Escape')) return;
      if (_scorePanelOpen) { closeScorePanel(); return; }
      _closeReportPreview();
    });
  }

  // 切台账/关页面前把图表实例全释放：容器已随 innerHTML 重建，旧实例会变成残留监听。
  function _disposeCharts() {
    if (_chart) { try { _chart.dispose(); } catch (e) { /* 忽略 */ } _chart = null; }
    if (_attrChart) { try { _attrChart.dispose(); } catch (e) { /* 忽略 */ } _attrChart = null; }
    _disposeScoreDetailChart();
    _disposeScoreMiniCharts();
    _scoreDetailToken++;                 // 作废在途的双轴图请求（旧响应不许再画进新容器）
    closeScorePanel();                   // 总览若开着，一并关掉并作废其请求
    _disposeAllEvCharts();
    _evOpen = [];
    _evActive = null;
    _evTid = null;
  }

  function setStatus(text) {
    var el = $('thMeta');
    if (el) el.textContent = text || '';
  }

  // ── 列表 ────────────────────────────────────────────────────────

  async function load(preferId) {
    setStatus('加载中…');
    try {
      var resp = await fetch('/api/thesis/list');
      var json = await resp.json();
      if (json.code !== 0) throw new Error(json.msg || '列表读取失败');
      _items = (json.data && json.data.items) || [];
      renderList();
      var want = preferId || _active
        || (_items[0] && _items[0].thesis_id);
      if (want) await loadDetail(want);
      else {
        $('thMain').innerHTML = '<div class="th-loading">还没有台账定义。</div>';
        setStatus('0 个台账');
      }
    } catch (e) {
      setStatus('加载失败');
      $('thMain').innerHTML = '<div class="th-loading">台账列表加载失败：' + esc(e.message) + '</div>';
    }
  }

  function _dotClass(it) {
    if (it.definition_ok === false) return 'bad';
    if (!it.has_snapshot) return 'none';
    var a = it.action || '';
    if (/不建议|减仓/.test(a)) return 'bad';
    if (/观望|控制仓位/.test(a)) return 'warn';
    return 'ok';
  }

  function renderList() {
    var head = $('thSideHead');
    if (head) {
      head.innerHTML = '跟踪台账 <span class="th-count">' + _items.length + '</span>';
    }
    var box = $('thSideList');
    if (!box) return;
    /* 组合层入口置顶：它是「元层」（综合各台账输出），不属于单专项台账 */
    var pfEntry = '<button type="button" class="th-item th-item-pf'
      + (_active === PORTFOLIO_ID ? ' active' : '')
      + '" data-id="' + PORTFOLIO_ID + '">'
      + '<span class="th-item-name"><span class="th-dot ok"></span>组合层</span>'
      + '<div class="th-item-desc">跨资产配比 · 共同风险</div>'
      + '<div class="th-item-foot"><span class="th-item-total">—</span>'
      + '<span>权重映射</span><span></span></div>'
      + '</button>';
    if (!_items.length) {
      box.innerHTML = pfEntry + '<div class="th-empty">还没有台账定义。<br>新增 data/thesis/*.json 即可。</div>';
      bindListEvents(box);
      return;
    }
    box.innerHTML = pfEntry + _items.map(function (it) {
      var total = (it.total === null || it.total === undefined) ? '—'
        : (it.total > 0 ? '+' + it.total : String(it.total));
      return '<button type="button" class="th-item' + (it.thesis_id === _active ? ' active' : '')
        + '" data-id="' + esc(it.thesis_id) + '">'
        + '<span class="th-item-name"><span class="th-dot ' + _dotClass(it) + '"></span>'
        + esc(it.name) + '</span>'
        + '<div class="th-item-desc">' + esc(it.asset_label || it.thesis_id) + '</div>'
        + '<div class="th-item-foot"><span class="th-item-total">' + esc(total) + '</span>'
        + '<span>' + esc(it.verdict || '未刷新') + '</span>'
        + '<span>' + esc(it.trade_date || '') + '</span></div>'
        + '</button>';
    }).join('');
    bindListEvents(box);
  }

  function bindListEvents(box) {
    box.querySelectorAll('.th-item').forEach(function (btn) {
      btn.addEventListener('click', function () { loadDetail(btn.dataset.id); });
    });
  }

  // ── 详情 ────────────────────────────────────────────────────────

  async function loadDetail(id) {
    _active = id;
    renderList();
    if (id === PORTFOLIO_ID) { await loadPortfolio(); return; }
    setStatus('读取 ' + id + ' …');
    _disposeCharts();
    try {
      // 详情与历史**一起取**：因子卡的 sparkline / 历史分位 / 较上次变化 chip
      // 都依赖 history。若分两次 await，首屏会先画出「暂无快照」占位再回填，
      // 既闪一下，也让渲染测试只能断言到「还没回填」的状态。
      var res = await Promise.all([
        fetch('/api/thesis/' + encodeURIComponent(id)),
        fetch('/api/thesis/' + encodeURIComponent(id) + '/history?days=180')
          .then(function (r) { return r.json(); })
          .catch(function () { return null; })
      ]);
      var json = await res[0].json();
      if (json.code !== 0) throw new Error(json.msg || '详情读取失败');
      var hist = (res[1] && res[1].code === 0 && res[1].data && res[1].data.items) || [];
      renderDetail(json.data, hist);
      setStatus('共 ' + _items.length + ' 个台账');
      await renderChart(id, hist, json.data);
    } catch (e) {
      $('thMain').innerHTML = '<div class="th-loading">读取失败：' + esc(e.message) + '</div>';
      setStatus('读取失败');
    }
  }

  // ── 组合层视图 ──────────────────────────────────────────────────
  //
  // 组合层 = 把「环境判断」接到「可执行权重」的那一跳。刻意独立成视图：
  // 单专项台账页放组合配比会在多处重复出现（且出现在与组合无关的台账里）——见 renderDetail 头的说明。

  async function loadPortfolio() {
    setStatus('读取组合层 …');
    // 组合视图会整块替换 thMain，先释放上一个台账留下的 echarts 实例（容器已随之移除）
    _disposeCharts();
    try {
      var resp = await fetch('/api/thesis/portfolios');
      var json = await resp.json();
      if (json.code !== 0) throw new Error(json.msg || '组合读取失败');
      var items = (json.data && json.data.items) || [];
      $('thMain').innerHTML = _portfoliosHtml(items);
      setStatus('组合层 · ' + items.length + ' 个组合');
    } catch (e) {
      $('thMain').innerHTML = '<div class="th-loading">组合层读取失败：' + esc(e.message) + '</div>';
      setStatus('读取失败');
    }
  }

  function _fmtW(tid, weights) {
    var w = weights && weights[tid];
    if (!w) return '—';
    return w[0] === w[1] ? (w[0] + '%') : (w[0] + '~' + w[1] + '%');
  }

  function _risksHtml(risks) {
    if (!risks || !risks.length) return '';
    var rows = risks.map(function (r) {
      var state = r.triggered ? '<span class="th-risk-tag hit">已触发</span>'
        : (r.evaluable ? '<span class="th-risk-tag">未触发</span>'
          : '<span class="th-risk-tag na">无法评估</span>');
      var conf = (r.confidence && r.confidence.level) || '';
      var confLabel = { high: '高', mid: '中', low: '低' }[conf] || conf;
      return '<div class="th-risk' + (r.triggered ? ' hit' : '') + '">'
        + '<div class="th-risk-head"><b>' + esc(r.label || r.factor) + '</b>'
        + state
        + '<span class="th-risk-legs">' + (r.legs || []).map(esc).join(' × ') + '</span></div>'
        + (r.rationale ? '<div class="th-risk-why">' + _mdBold(r.rationale) + '</div>' : '')
        + (r.triggered ? '<div class="th-risk-action">触发动作：' + _mdBold(r.action || '—') + '</div>'
          : (r.action ? '<div class="th-risk-action" style="font-weight:400;color:var(--text-secondary,#6b7280)">'
            + '建议动作：' + _mdBold(r.action) + '</div>' : ''))
        + (confLabel ? '<div class="th-risk-conf">置信度 ' + esc(confLabel) + '</div>' : '')
        + '</div>';
    }).join('');
    return '<div class="th-card"><div class="th-card-head">共同风险暴露'
      + '<span class="th-sub">跨腿共有的底层驱动</span></div>'
      + '<div class="th-scroll"><div class="th-sec-note">两腿共享同一底层暴露时，'
      + '内部调权重只是把风险从一条腿搬到另一条腿，正确动作是整体降仓。</div>'
      + rows + '</div></div>';
  }

  function _riskHtml(risk) {
    if (!risk || !risk.available) {
      return '<div class="th-card"><div class="th-card-head">组合结构'
        + '<span class="th-sub">相关性 / 波动</span></div>'
        + '<div class="th-scroll"><div class="th-sec-note">'
        + esc((risk && risk.reason) || '暂不可用') + '</div></div></div>';
    }
    var legs = (risk.leg_vols || []).map(function (x) {
      return '<span class="th-pfstat"><b>' + esc(x[0]) + '</b>'
        + '<em>年化波动 ' + esc(x[1]) + '%</em></span>';
    }).join('');
    return '<div class="th-card"><div class="th-card-head">组合结构'
      + '<span class="th-sub">近 ' + esc(risk.window) + ' 个交易日 · 本地日线自算</span></div>'
      + '<div class="th-scroll">'
      + '<div class="th-pfstats">'
      + '<span class="th-pfstat"><b>' + esc(risk.corr === null ? '—' : risk.corr) + '</b><em>腿间日收益相关</em></span>'
      + '<span class="th-pfstat"><b>' + esc(risk.equal_weight_vol) + '%</b><em>等权组合年化波动</em></span>'
      + '<span class="th-pfstat"><b>' + esc(risk.avg_leg_vol) + '%</b><em>两腿平均波动</em></span>'
      + '<span class="th-pfstat"><b class="v-up">'
      + esc(risk.diversification_pp) + 'pp</b><em>分散化降波（'
      + esc(risk.diversification_pct) + '%）</em></span>'
      + '</div>'
      + '<div class="th-legs-inline">' + legs + '</div>'
      + '<div class="th-sec-note" style="margin:7px 0 0">波动比 '
      + esc(risk.vol_ratio) + '×——组合最大回撤由高波动端主导（本条为本地复算，'
      + '用于复核定义 basis 里的结论性描述是否仍成立）。</div>'
      + '</div></div>';
  }

  function _bandsHtml(o) {
    if (!o || !o.bands || !o.bands.length) return '';
    var gap = o.upgrade_gap;
    var gapTx = gap
      ? '距上一档「' + esc(gap.to_label) + '」还差 <b>' + esc(gap.delta) + '</b> 分（需总分 ≥ ' + esc(gap.need_total) + '）'
      : (o.current_index === 0 ? '已在最高档' : '—');
    var rows = o.bands.map(function (b) {
      var w = Object.keys(b.weights || {}).map(function (k) {
        return esc(k) + ' ' + esc(_fmtW(k, b.weights));
      }).join(' · ');
      return '<div class="th-band' + (b.current ? ' current' : '') + '">'
        + '<span class="th-band-mark">' + (b.current ? '▶' : '') + '</span>'
        + '<span class="th-band-label">' + esc(b.label) + '</span>'
        + '<span class="th-band-min">≥ ' + esc(b.min_total) + '</span>'
        + '<span class="th-band-w">' + w + '</span></div>';
    }).join('');
    return '<div class="th-card"><div class="th-card-head">档位全景'
      + '<span class="th-sub">驱动腿 ' + esc(o.driver) + ' 当前 ' + esc(o.driver_total) + '</span></div>'
      + '<div class="th-scroll">'
      + '<div class="th-sec-note">' + gapTx + '</div>'
      + rows + '</div></div>';
  }

  function _portfoliosHtml(items) {
    if (!items.length) {
      return '<div class="th-hero"><span class="th-verdict" style="color:var(--text-secondary,#6b7280)">无组合</span>'
        + '<span class="th-action">还没有组合定义，新增 data/portfolio_map.json 即可。</span></div>';
    }
    return items.map(function (p) {
      if (p.ok === false) {
        return '<div class="th-hero">'
          + '<span class="th-verdict" style="color:var(--text-secondary,#6b7280)">暂不可用</span>'
          + '<span class="th-action">' + esc(p.reason || '—') + '</span>'
          + '<div class="th-hero-right">' + esc(p.name || p.id || '未命名组合') + '</div></div>';
      }
      var band = p.band || {};
      var total = (p.combo_total === null || p.combo_total === undefined) ? '—'
        : (p.combo_total > 0 ? '+' + p.combo_total : String(p.combo_total));
      var hero = '<div class="th-hero">'
        + '<span class="th-verdict" style="color:var(--brand,#176b5b)">' + esc(total) + '</span>'
        + '<span class="th-action">' + esc(p.name || p.id) + '</span>'
        + '<div class="th-hero-right">当前档位<br><b>' + esc(band.label || '未定档') + '</b></div>'
        + '</div>';

      var legs = (p.legs || []).map(function (l) {
        var t = (l.total === null || l.total === undefined) ? '—'
          : (l.total > 0 ? '+' + l.total : String(l.total));
        return '<div class="th-leg">'
          + '<span class="th-leg-role">' + esc(l.role || '') + '</span>'
          + '<span class="th-leg-name">' + esc(l.label || l.thesis_id) + '</span>'
          + '<span class="th-leg-score">' + esc(t) + '</span>'
          + '<span class="th-leg-verdict">' + esc(l.verdict || '未刷新') + '</span>'
          + '</div>';
      }).join('');
      var wrows = Object.keys(band.weights || {}).map(function (tid) {
        var leg = (p.legs || []).filter(function (x) { return x.thesis_id === tid; })[0] || {};
        return '<div class="th-leg"><span class="th-leg-role">建议权重</span>'
          + '<span class="th-leg-name">' + esc(leg.label || tid) + '</span>'
          + '<span class="th-leg-score">' + esc(_fmtW(tid, band.weights)) + '</span><span></span></div>';
      }).join('');

      var rows = '<div class="th-rows">'
        + '<div class="th-card"><div class="th-card-head">腿 · 状态与建议权重</div>'
        + '<div class="th-scroll">' + legs + wrows
        + (band.note ? '<div class="th-sec-note" style="margin-top:8px">' + _mdBold(band.note) + '</div>' : '')
        + '</div></div>'
        + _riskHtml(p.risk)
        + _bandsHtml(p.bands_outline)
        + _risksHtml(p.risks)
        + (p.basis ? '<div class="th-card"><div class="th-card-head">依据</div>'
          + '<div class="th-scroll"><div class="th-sec-note" style="margin-bottom:0">'
          + _mdBold(p.basis) + '</div></div></div>' : '')
        + '</div>';
      return hero + rows;
    }).join('');
  }

  function renderDetail(d, points) {
    if (!d) return;
    points = points || [];
    var snap = d.snapshot;
    var main = $('thMain');
    if (!main) return;
    // 每次重绘都从「没有展开任何因子」开始：旧 DOM 已被替换，保留 open 列表
    // 会让停靠区/高亮状态指向不存在的卡片。
    _disposeScoreDetailChart();
    _scoreDetailToken++;             // 作废上一轮详情图的在途请求
    _disposeAllEvCharts();
    _evHideDock();
    _evOpen = [];
    _evActive = null;
    _evTid = snap ? (snap.thesis_id || _active) : _active;

    var hero;
    if (!snap) {
      hero = '<div class="th-hero">'
        + '<span class="th-verdict" style="color:var(--text-secondary,#6b7280)">未刷新</span>'
        + '<span class="th-action">该台账还没有快照，点「刷新」跑一次。</span>'
        + '<div class="th-hero-right">定义：' + esc(d.name)
        + (d.definition_ok === false ? '<br><span class="th-flag">定义存在问题</span>' : '')
        + '</div></div>';
      main.innerHTML = hero + '<div class="th-rows"><div class="th-card"><div class="th-scroll">'
        + '<div class="th-empty">刷新后这里会显示五因子读数、信号命中、结论失效条件与变更告警。</div>'
        + '</div></div></div>';
      return;
    }

    // 口径声明优先取快照，缺失时回落到定义（caliber_note 未入快照列，老快照里就没有）
    var caliber = snap.caliber_note || (d.definition || {})['_说明_口径'] || '';
    var total = snap.total > 0 ? '+' + snap.total : String(snap.total);
    // 低置信度因子的分数贡献：不参与加权（那会改台账的档位语义），
    // 但必须在结论旁显式说出来——否则「标着置信度低、却贡献了大部分分数」
    // 会让读者以为总分比实际更可靠（2026-09-22 实测：+4 里有 +3 来自 low 项）。
    var lowF = (snap.factors || []).filter(function (f) {
      return (f.confidence || {}).level === 'low' && f.score;
    });
    var lowW = lowF.reduce(function (a, f) { return a + Math.abs(f.score); }, 0);
    var allW = (snap.factors || []).reduce(function (a, f) {
      return a + (f.score ? Math.abs(f.score) : 0);
    }, 0);

    hero = '<div class="th-hero">'
      + '<span class="th-verdict" style="color:' + (STATUS_COLOR[snap.verdict] || 'var(--text,#1c1c1a)')
      + '">' + esc(snap.verdict) + '</span>'
      + '<span class="th-action">' + esc(snap.action || '') + '</span>'
      + '<div class="th-hero-right">综合总分 <b>' + esc(total) + '</b> / ±'
      + esc((snap.factor_count || 5) * 2) + '<br>有效因子 '
      + esc(snap.valid_count) + ' / ' + esc(snap.factor_count)
      + '　数据截止 ' + esc(snap.data_asof || snap.trade_date)
      + (lowW
          ? '<br>其中 <b>' + esc(lowW) + '</b> / ' + esc(allW)
            + ' 分来自低置信度因子（' + esc(lowF.length) + ' 项，宜按更低权重看待）'
          : '')
      + '</div></div>';

    main.innerHTML = hero
      + (caliber
          ? '<div class="th-caliber"><b>数据口径</b>' + _mdBold(caliber) + '</div>'
          : '')
      + _scoreDetailCardHtml()
      + _stagesHtml(snap.stages)
      + _phasesHtml(snap.phases)
      + '<div class="th-rows">'
      + '<div class="th-row th-row-factors"><div class="th-card">'
      + '<div class="th-card-head">因子读数 <span class="th-sub">评分 -2 不利 ~ +2 很有利</span></div>'
      + '<div class="th-scroll"><div class="th-factors">'
      + _factorsHtml(snap.factors, snap.thesis_id, points) + '</div>'
      + _warnHtml(snap.warnings) + '</div></div></div>'
      + '<div class="th-row th-row-trend"><div class="th-card">'
      + '<div class="th-card-head">历史趋势与变动归因 '
      + '<span class="th-sub">总分柱 / 逐因子分数线（图例可勾选）· 最新两点逐因子 Δ</span></div>'
      + '<div class="th-scroll"><div class="th-trend-grid">'
      + '<div><div id="thChart" class="th-chart"></div>'
      + '<div class="th-empty" id="thChartNote" style="display:none"></div></div>'
      + _attrHtml(points, snap)
      + '</div></div></div></div>'
      + '<div class="th-row th-row-bottom">'
      + '<div class="th-card"><div class="th-card-head">信号命中</div>'
      + '<div class="th-scroll">' + _signalsHtml(snap.signals) + '</div></div>'
      + '<div class="th-card"><div class="th-card-head">失效条件与变更告警 '
      + '<span class="th-sub">' + _alertCount(d) + '</span></div>'
      + '<div class="th-scroll">' + _invalidationHtml(snap.invalidation)
      + '<div class="th-card-head" style="border-top:1px solid var(--border-light,#f0f0f0);'
      + 'border-bottom:none;padding-top:9px">近期变更</div>'
      + _alertsHtml(d.alerts) + '</div></div>'
      + '</div>'
      + '<div class="th-row th-row-analysis">'
      + _scenariosHtml(snap.scenarios)
      + _transmissionHtml(snap.transmission)
      + '</div>'
      + _evidenceBatchHtml(snap.thesis_id)
      + '<div id="thMatrix"></div>'
      + '</div>';

    _loadMatrix();
    // 双轴图按定义重放，与快照无关；每次进详情都重新取（后端有 def_hash 缓存）
    _renderScoreDetail(snap.thesis_id || _active);
  }

  // 组合层权重建议卡**不在**单专项台账页出现（按需求移除：单专项页看的是这一张，
  // 组合配比在多处重复属噪音）。组合层已独立成左侧列表置顶的「组合层」视图
  // ——消费同一份 data/portfolio_map.json（v2 起支持多组合 / 共同风险暴露）。

  // ── 因子有效性汇总（批量准入检查） ──────────────────────────────
  //
  // 单因子证据要「点开某个因子」才看得到，回答不了「这张台账的因子有几个真有
  // 区分度」。这里一键跑完整张台账（ctx 只准备一次，约 20s），输出分档分布 +
  // 逐因子明细——新建台账/新增因子时的准入检查就靠它。

  var TIER_CN = {
    strong: '强', medium: '中', weak: '弱',
    insufficient: '积累中', unsupported: '结构不可测',
  };

  function _evidenceBatchHtml(thesisId) {
    return '<div class="th-card" id="thEvCard"><div class="th-card-head">因子有效性汇总'
      + '<span class="th-sub">批量校验全部因子（约 20–60 秒）</span>'
      + '<button class="th-btn" type="button" id="thEvAllBtn" data-tid="'
      + esc(thesisId) + '" style="margin-left:auto">跑一遍</button></div>'
      + '<div class="th-scroll" id="thEvAllBox"><div class="th-sec-note">'
      + '检验每个因子的历史读数与标的未来 20/60 日收益的关系（相关系数 + 五分位分层）。'
      + '用途：确认这张台账的因子<b>确实有区分度</b>，而不是凑数。'
      + '「积累中」＝序列还太短（多为指数估值类，源只给近 20 个交易日、靠逐期累积）；'
      + '「结构不可测」＝复合/手工因子，无单一序列可回放。</div></div></div>';
  }

  function _evTableHtml(d) {
    if (!d || d.ok === false) {
      return '<div class="th-sec-note">' + esc((d && d.reason) || '读取失败') + '</div>';
    }
    var s = d.summary || {};
    var sum = '<div class="th-ev-sum">'
      + '<span>可验证 <b>' + esc(d.verifiable) + '/' + esc(d.factor_count) + '</b></span>'
      + '<span>真有支撑 <b>' + esc(d.usable) + '</b></span>'
      + Object.keys(TIER_CN).map(function (k) {
        return '<span>' + esc(TIER_CN[k]) + ' <b>' + esc(s[k] || 0) + '</b></span>';
      }).join('')
      + '<span>标的 ' + esc((d.target || {}).name || '—') + '</span>'
      + '</div>';
    var rows = (d.rows || []).map(function (r) {
      return '<tr>'
        + '<td>' + esc(r.factor_name) + '</td>'
        + '<td><span class="th-tier ' + esc(r.tier) + '">' + esc(TIER_CN[r.tier] || r.tier) + '</span></td>'
        + '<td class="mono">' + (r.corr === null || r.corr === undefined ? '—' : esc(r.corr)) + '</td>'
        + '<td class="mono">' + (r.q5_q1 === null || r.q5_q1 === undefined ? '—' : esc(r.q5_q1)) + '</td>'
        + '<td class="mono">' + (r.n === null || r.n === undefined ? '—' : esc(r.n)) + '</td>'
        + '<td style="color:var(--text-secondary,#6b7280)">' + esc(r.verdict || '') + '</td>'
        + '</tr>';
    }).join('');
    return sum + '<table class="th-ev-table"><thead><tr>'
      + '<th>因子</th><th>支撑</th><th>r</th><th>Q5−Q1(pp)</th><th>n</th><th>结论</th>'
      + '</tr></thead><tbody>' + rows + '</tbody></table>';
  }

  async function _loadEvidenceBatch(tid, btn) {
    var box = $('thEvAllBox');
    if (!box) return;
    if (btn) { btn.disabled = true; btn.textContent = '计算中…'; }
    box.innerHTML = '<div class="th-sec-note">正在回放各因子历史序列并计算（首次较慢，约 20–60 秒）…</div>';
    try {
      var resp = await fetch('/api/thesis/' + encodeURIComponent(tid) + '/factor-evidence');
      var json = await resp.json();
      if (json.code !== 0) throw new Error(json.msg || '读取失败');
      box.innerHTML = _evTableHtml(json.data);
    } catch (e) {
      box.innerHTML = '<div class="th-sec-note">因子有效性汇总失败：' + esc(e.message) + '</div>';
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '重跑'; }
    }
  }

  // 单元格底色。配色遵循 A 股习惯：涨（正）=红、跌（负）=绿；相关性同理
  // （红=正相关 / 绿=负相关，与原始研究的热力图口径一致）。
  function _cellColor(v, sat) {
    if (v === null || v === undefined || isNaN(v)) return 'transparent';
    var a = Math.min(Math.abs(Number(v)) / sat, 1) * 0.5;
    return Number(v) >= 0 ? 'rgba(192,57,43,' + a.toFixed(2) + ')'
                          : 'rgba(30,132,73,' + a.toFixed(2) + ')';
  }

  var _matrixCache = null;

  // 市场矩阵：资产 × 窗口涨跌幅热力图 + 相关性矩阵（后端用本地日线自算）。
  // 这是横向对比能力——台账原先只有单资产读数，既无法验证「恒科波动是红利 2.1 倍」
  // 这类写死在文案里的常量，也看不出「红利与沪深300 已经脱钩」。
  async function _loadMatrix() {
    var box = $('thMatrix');
    if (!box) return;
    try {
      if (!_matrixCache) {
        var res = await fetch('/api/thesis/market-matrix');
        var j = await res.json();
        _matrixCache = (j && j.data) || null;
      }
      var d = _matrixCache;
      if (!d || !d.rows || !d.rows.length) { box.innerHTML = ''; return; }

      var head = '<tr><th>标的</th>'
        + (d.windows || []).map(function (w) {
            return '<th class="c">' + esc(w) + '</th>';
          }).join('')
        + '<th class="c">最新</th></tr>';
      var body = d.rows.map(function (r) {
        return '<tr><td>' + esc(r.name) + '</td>'
          + (r.values || []).map(function (v) {
              var txt = (v === null || v === undefined) ? '—'
                : (v > 0 ? '+' : '') + Number(v).toFixed(2);
              return '<td class="c" style="background:' + _cellColor(v, 10) + '">' + txt + '</td>';
            }).join('')
          + '<td class="c th-mono">' + esc(r.last) + '</td></tr>';
      }).join('');
      var heat = '<div class="th-card"><div class="th-card-head">资产 × 时间窗口'
        + '<span class="th-sub">区间涨跌幅 %　红=涨 / 绿=跌</span></div>'
        + '<div class="th-scroll"><table class="th-matrix">' + head + body + '</table></div></div>';

      var corr = '';
      var c = d.correlation || {};
      if (c.matrix && c.matrix.length) {
        var ch = '<tr><th></th>'
          + c.labels.map(function (l) { return '<th class="c">' + esc(l) + '</th>'; }).join('')
          + '</tr>';
        var cb = c.labels.map(function (l, i) {
          return '<tr><td>' + esc(l) + '</td>'
            + c.matrix[i].map(function (v) {
                return '<td class="c" style="background:' + _cellColor(v, 1) + '">'
                  + Number(v).toFixed(2) + '</td>';
              }).join('') + '</tr>';
        }).join('');
        corr = '<div class="th-card"><div class="th-card-head">相关性矩阵'
          + '<span class="th-sub">' + esc(c.basis) + '　红=正 / 绿=负</span></div>'
          + '<div class="th-scroll"><table class="th-matrix">' + ch + cb + '</table></div></div>';
      }
      box.innerHTML = '<div class="th-row th-row-analysis">' + heat + corr + '</div>';
    } catch (e) {
      box.innerHTML = '';
    }
  }

  // 双维阶段矩阵（黄金类台账）：纵轴＝驱动力（若干因子得分之和）、横轴＝位置（单因子得分），
  // 3×3 九宫格高亮当前所处格。
  // 为什么单开一块：verdicts 是**一维**的（总分→档位），而「驱动有利 + 位置已高」与
  // 「驱动有利 + 位置便宜」总分相近、操作含义完全相反（前者持有不追、后者是最好的买点）。
  // 线性总分给不出这个区分 —— 这正是原始报告的立论。
  var TONE_CLASS = {
    bull: 'th-tone-bull', warn: 'th-tone-warn',
    neutral: 'th-tone-neutral', bear: 'th-tone-bear'
  };

  function _signed(v) {
    if (v === null || v === undefined) return '—';
    return (Number(v) > 0 ? '+' : '') + Number(v);
  }

  function _stagesHtml(st) {
    if (!st || !st.cells || !st.cells.length) return '';
    var driveBands = (st.drive_bands || []).map(function (b) { return b.name; });
    var posBands = (st.pos_bands || []).map(function (b) { return b.name; });
    if (!driveBands.length || !posBands.length) return '';

    function cellOf(d, p) {
      for (var i = 0; i < st.cells.length; i++) {
        var c = st.cells[i];
        if (c.drive === d && c.pos === p) return c;
      }
      return null;
    }

    var head = '<tr><th class="th-stg-h"></th>'
      + posBands.map(function (p) { return '<th class="th-stg-h">' + esc(p) + '</th>'; }).join('')
      + '</tr>';
    var body = driveBands.map(function (d) {
      return '<tr><td class="th-stg-h">' + esc(d) + '</td>'
        + posBands.map(function (p) {
            var c = cellOf(d, p);
            if (!c) return '<td class="th-stg-c"></td>';
            return '<td class="th-stg-c ' + (TONE_CLASS[c.tone] || '')
              + (c.current ? ' is-cur' : '') + '" title="' + esc(c.desc || '') + '">'
              + esc(c.stage || '') + '</td>';
          }).join('') + '</tr>';
    }).join('');

    var cur = st.stage || '无法判定';
    var heroCls = st.stage ? (TONE_CLASS[st.tone] || '') : '';
    // 位置轴可能是「单个因子」（黄金/铜的 pos_key）或「多个因子之和」（猪周期的 pos_keys）。
    // 后者在返回值里 pos_key 会被置空，只报键名会显示成空白，故按个数给一句人话。
    var posDesc = st.pos_keys && st.pos_keys.length > 1
      ? (st.pos_factor_count + ' 个因子得分之和')
      : (st.pos_key || '位置');
    var incomplete = (st.complete === false)
      ? '<span class="th-flag">阶段矩阵仅累加 '
        + esc((st.drive_valid_count || 0) + (st.pos_valid_count || 0)) + '/'
        + esc((st.drive_factor_count || 0) + (st.pos_factor_count || 0))
        + ' 个因子，档位可能偏移</span>'
      : '';
    var note = '纵轴＝驱动力（' + esc(st.drive_factor_count) + ' 个因子得分之和，满分 ±'
      + esc(st.drive_full) + '）；横轴＝位置（' + esc(posDesc) + '，满分 ±'
      + esc(st.pos_full) + '）。深色格为当前所处位置。本块与情景推演、历史阶段均不参与打分。';

    return '<div class="th-card th-stages"><div class="th-card-head">阶段定位矩阵'
      + '<span class="th-sub">驱动力 × 位置　九宫格</span></div>'
      + '<div class="th-stg-hero">'
      + '<span class="th-stg-badge ' + heroCls + '">' + esc(cur) + '</span>'
      + '<span class="th-stg-desc">' + esc(st.desc || '') + '</span>'
      + '<span class="th-stg-num">驱动力 <b>' + esc(_signed(st.drive_total)) + '</b> / ±'
      + esc(st.drive_full) + '（' + esc(st.drive_band || '—') + '）<br>'
      + '位置 <b>' + esc(_signed(st.pos_score)) + '</b>（' + esc(st.pos_band || '—') + '）</span>'
      + '</div>' + incomplete
      + '<table class="th-stg-matrix">' + head + body + '</table>'
      + '<div class="th-note">' + note + '</div></div>';
  }

  // 历史阶段定位条（红利类台账）：把报告的 20 年四阶段划出来，高亮当前所在格。
  // 阶段定义来自原始研究（溢价中枢 + ETF 规模 + 资金结构），是纯评分给不了的「历史坐标」。
  function _phasesHtml(phases) {
    if (!phases || !phases.length) return '';
    var cur = (phases.filter(function (p) { return p.current; })[0] || {}).name || '—';
    var cells = phases.map(function (p) {
      return '<div class="th-phase' + (p.current ? ' is-cur' : '') + '">'
        + '<div class="th-phase-name">' + esc(p.name) + '</div>'
        + '<div class="th-phase-span">' + esc(p.from) + ' ~ ' + esc(p.to || '至今') + '</div>'
        + '<div class="th-phase-feat">' + esc(p.feature || '') + '</div></div>';
    }).join('');
    return '<div class="th-card th-phases"><div class="th-card-head">历史阶段定位'
      + '<span class="th-sub">当前：' + esc(cur) + '　（阶段划分出自原始研究）</span></div>'
      + '<div class="th-phase-track">' + cells + '</div></div>';
  }

  // 情景推演：命中的那条高亮，并逐条列出条件与当前值——让人看见「差哪一条」，
  // 而不是只看到「没命中」。概率为原报告的分析判断，非量化预测。
  function _scenariosHtml(scenarios) {
    if (!scenarios || !scenarios.length) return '';
    var rows = scenarios.map(function (s) {
      var conds = (s.conditions || []).map(function (c) {
        return '<span class="th-cond' + (c.hit ? ' is-hit' : '') + '">'
          + esc(c.metric) + ' ' + esc(c.op) + ' ' + esc(c.value)
          + '（现 ' + esc(c.current) + '）</span>';
      }).join('');
      return '<div class="th-scen' + (s.hit ? ' is-cur' : '') + '">'
        + '<div class="th-scen-head"><b>' + esc(s.name) + '</b>'
        + '<span class="th-scen-prob">' + esc(s.probability) + '%</span>'
        + (s.hit ? '<span class="th-scen-tag">当前</span>' : '') + '</div>'
        + '<div class="th-scen-out">' + esc(s.outcome || '') + '</div>'
        + (conds ? '<div class="th-scen-conds">' + conds + '</div>' : '')
        + '<div class="th-scen-factors">因子组合：' + esc(s.factors || '—') + '</div>'
        + '</div>';
    }).join('');
    return '<div class="th-card"><div class="th-card-head">情景推演'
      + '<span class="th-sub">条件全中即高亮</span></div>'
      + '<div class="th-scroll"><div class="th-scens">' + rows + '</div></div></div>';
  }

  // 传导链路：宏观 → 路径 → 影响。命中（条件满足）的路径高亮。
  function _transmissionHtml(items) {
    if (!items || !items.length) return '';
    var rows = items.map(function (t) {
      return '<div class="th-trans' + (t.hit ? ' is-on' : '') + '">'
        + '<div class="th-trans-head"><b>' + esc(t.path) + '</b>'
        + '<span class="th-trans-status">' + esc(t.status || '') + '</span>'
        + (t.hit ? '<span class="th-scen-tag">当前生效</span>' : '') + '</div>'
        + '<div class="th-trans-mech">' + esc(t.mechanism || '') + '</div>'
        + '<div class="th-trans-effect">' + esc(t.effect || '') + '</div></div>';
    }).join('');
    return '<div class="th-card"><div class="th-card-head">传导链路'
      + '<span class="th-sub">宏观 → 路径 → 影响</span></div>'
      + '<div class="th-scroll"><div class="th-transs">' + rows + '</div></div></div>';
  }

  // 历史分位带：标记当前读数在本地历史序列中的位置（0 = 历史最低，100 = 历史最高）。
  // 样本不足时引擎给 null —— 这里显式写「样本不足」而不画条：一条没有统计意义的分位
  // 比不显示更容易误导。
  function _pctBand(f) {
    // 读数本身就是「分位」的因子（如黄金⑦位置维度）不再画这条带：它的 value 已经是
    // 0~100 的分位，再算一次「读数在序列中的分位」既无意义、又会显示成另一个数。
    if (f.pct_rank_off) return '';
    if (f.pct_rank === null || f.pct_rank === undefined) {
      return '<div class="th-pct th-pct-empty">历史分位：样本不足（需 ≥30 期）</div>';
    }
    var p = Math.max(0, Math.min(100, Number(f.pct_rank)));
    return '<div class="th-pct" title="当前读数在本地历史序列中的分位：0 = 历史最低，100 = 历史最高">'
      + '<span>历史分位</span>'
      + '<span class="th-pct-track"><i style="left:' + p.toFixed(1) + '%"></i></span>'
      + '<b>' + Math.round(p) + '%</b></div>';
  }

  // 置信度：把「这条读数有多可信」显式标出来（样本量 / 主观设定程度）。
  // 报告反复自我设限（加息年份仅 5–8 个观测、五因子评分含主观权重），
  // 页面上不标就等于默认每条读数同等可信。
  var CONF_LABEL = { high: '置信度高', mid: '置信度中', low: '置信度低' };

  function _confHtml(conf) {
    if (!conf) return '';
    var lvl = conf.level;
    if (!CONF_LABEL[lvl]) return '';
    return '<div class="th-conf th-conf-' + lvl + '" title="' + esc(conf.why || '') + '">'
      + esc(CONF_LABEL[lvl]) + (conf.why ? ' · ' + esc(conf.why) : '') + '</div>';
  }

  // ── 历史快照派生：sparkline / 历史区间 / 较上次变化 / 变动归因 ─────
  //
  // 数据全部来自 GET /history 的 items[].values（逐因子快照读数）。
  // 口径：缺失就是缺失——某期没有该因子读数时整期跳过，绝不补 0（补 0 会伪造
  // 「归零」这种根本不存在的变动，和台账「数据缺失 ≠ 中性」的硬约束一致）。

  function _seriesValues(points, key) {
    var out = [];
    (points || []).forEach(function (p) {
      var v = p && p.values && p.values[key];
      if (v === null || v === undefined || v === '') return;
      var n = Number(v);
      if (isFinite(n)) out.push(n);
    });
    return out;
  }

  function _fmtSigned(v, unit) {
    if (v === null || v === undefined || !isFinite(Number(v))) return '—';
    var n = Number(v);
    var u = String(unit || '');
    var dg = u.indexOf('bp') >= 0 ? 1 : (u.indexOf('亿元') >= 0 ? 0 : 2);
    return (n > 0 ? '+' : '') + n.toFixed(dg);
  }

  // 线性插值分位（与 numpy.percentile 的线性口径一致，不引入依赖）
  function _quantile(sorted, q) {
    if (!sorted || !sorted.length) return null;
    if (sorted.length === 1) return sorted[0];
    var pos = (sorted.length - 1) * q;
    var lo = Math.floor(pos), hi = Math.ceil(pos);
    if (lo === hi) return sorted[lo];
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  // SVG sparkline：纯字符串拼，不为每个因子建 ECharts 实例（一张台账 5–8 个因子，
  // 每个都 init 会拖慢首屏并占满 webgl/canvas 上下文）。
  function _sparklineHtml(vals, unit) {
    if (!vals || vals.length < 2) {
      return '<div class="th-spark th-spark-empty" title="历史快照不足 2 期，暂画不出形状">'
        + '<span>' + (vals && vals.length === 1 ? '仅 1 期快照' : '暂无历史快照') + '</span></div>';
    }
    var w = 100, h = 26, pad = 2;
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    var span = (max - min) || 1;
    var pts = vals.map(function (v, i) {
      var x = pad + (i / (vals.length - 1)) * (w - pad * 2);
      var y = h - pad - ((v - min) / span) * (h - pad * 2);
      return x.toFixed(2) + ',' + y.toFixed(2);
    }).join(' ');
    var up = vals[vals.length - 1] >= vals[0];
    var last = pts.split(' ').pop().split(',');
    return '<div class="th-spark" title="最近 ' + vals.length + ' 期读数（'
      + min.toFixed(2) + ' ~ ' + max.toFixed(2) + (unit || '') + '；红=区间上行 / 绿=区间下行）">'
      + '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true">'
      + '<polyline class="th-spark-line ' + (up ? 'up' : 'down') + '" points="' + pts
      + '" fill="none" stroke-width="1.4" vector-effect="non-scaling-stroke"'
      + ' stroke-linejoin="round" stroke-linecap="round"/>'
      + '<circle class="th-spark-dot ' + (up ? 'up' : 'down') + '" cx="' + last[0]
      + '" cy="' + last[1] + '" r="1.8"/>'
      + '</svg><span class="th-spark-lab">' + vals.length + ' 期</span></div>';
  }

  // 历史区间条：p10 / p25 / 中位 / p75 / p90 + 当前值标记 + 「历史分位 x% · 样本 N 期」。
  // 样本不足 20 期时退回快照里的 pct_rank 口径（老页面行为），两者都没有就写「样本不足」。
  function _rangeBarHtml(vals, f) {
    if (f && f.pct_rank_off) return '';
    if (!vals || vals.length < 20) return _pctBand(f);
    var s = vals.slice().sort(function (a, b) { return a - b; });
    var lo = s[0], hi = s[s.length - 1];
    var rng = (hi - lo) || 1;
    function pos(v) { return Math.max(0, Math.min(100, ((v - lo) / rng) * 100)); }
    var cur = (f && f.value !== null && f.value !== undefined) ? Number(f.value) : NaN;
    if (!isFinite(cur)) cur = s[s.length - 1];
    var p10 = _quantile(s, 0.10), p25 = _quantile(s, 0.25), p50 = _quantile(s, 0.50),
        p75 = _quantile(s, 0.75), p90 = _quantile(s, 0.90);
    var below = 0;
    vals.forEach(function (v) { if (v <= cur) below += 1; });
    var pct = Math.round(100 * below / vals.length);
    var unit = (f && f.unit) || '';
    var tip = '历史区间：p10 ' + _fmtSigned(p10, unit) + ' / p25 ' + _fmtSigned(p25, unit)
      + ' / 中位 ' + _fmtSigned(p50, unit) + ' / p75 ' + _fmtSigned(p75, unit)
      + ' / p90 ' + _fmtSigned(p90, unit) + '；当前读数在本地序列中的分位 ' + pct + '%';
    return '<div class="th-range" title="' + esc(tip) + '">'
      + '<span class="th-range-lab">历史区间</span>'
      + '<span class="th-range-track">'
      + '<i class="th-range-band" style="left:' + pos(p10).toFixed(1) + '%;width:'
      + (pos(p90) - pos(p10)).toFixed(1) + '%"></i>'
      + '<i class="th-range-box" style="left:' + pos(p25).toFixed(1) + '%;width:'
      + (pos(p75) - pos(p25)).toFixed(1) + '%"></i>'
      + '<i class="th-range-med" style="left:' + pos(p50).toFixed(1) + '%"></i>'
      + '<b class="th-range-cur" style="left:' + pos(cur).toFixed(1) + '%"></b>'
      + '</span>'
      // 明确写「快照分位」：这里是 /history 的逐日快照口径（26 期），
      // 与证据面板回放全历史得到的「历史分位」（如 51 期）不是同一个样本；
      // 不标口径会让两个数字看起来互相矛盾。
      + '<span class="th-range-txt">快照分位 <b>' + pct + '%</b> · ' + vals.length + ' 期</span>'
      + '</div>';
  }

  // 较上次变化 chip：values 差值 + 单位（+0.18pp / -5.3bp）。只有 1 期时明确写「—」。
  function _changeChipHtml(vals, f) {
    if (!vals || vals.length < 2) {
      return '<span class="th-chg th-chg-na" title="历史快照不足 2 期">较上次 —</span>';
    }
    var d = vals[vals.length - 1] - vals[vals.length - 2];
    var cls = d > 0 ? 'up' : (d < 0 ? 'down' : 'flat');
    var unit = (f && f.unit) || '';
    return '<span class="th-chg ' + cls + '" title="最近两期读数：'
      + _fmtSigned(vals[vals.length - 2], unit) + ' → ' + _fmtSigned(vals[vals.length - 1], unit)
      + '">较上次 ' + _fmtSigned(d, unit) + (unit ? esc(unit) : '') + '</span>';
  }

  function _fmtScore(v) {
    if (v === null || v === undefined) return '—';
    return (Number(v) > 0 ? '+' : '') + Number(v);
  }

  // 逐因子变动归因（最新两个历史点）。delta 只在两端都有分数时才算：
  // 某期缺这个因子 → delta=null（「未采集」，不是 0）。
  function _attribution(points, snap) {
    var res = {ok: false, rows: [], prev: null, cur: null, total_text: ''};
    if (!points || points.length < 2) return res;
    var prev = points[points.length - 2], cur = points[points.length - 1];
    var nameOf = {};
    ((snap && snap.factors) || []).forEach(function (f) {
      if (f && f.key) nameOf[f.key] = f.name || f.key;
    });
    var keys = Object.keys(cur.scores || {});
    Object.keys(prev.scores || {}).forEach(function (k) {
      if (keys.indexOf(k) < 0) keys.push(k);
    });
    var rows = keys.map(function (k) {
      var a = (prev.scores || {})[k], b = (cur.scores || {})[k];
      var pa = (a === null || a === undefined) ? null : Number(a);
      var pb = (b === null || b === undefined) ? null : Number(b);
      return {
        key: k, name: nameOf[k] || k,
        prev: pa, cur: pb,
        delta: (pa === null || pb === null) ? null : (pb - pa),
        prev_value: (prev.values || {})[k], cur_value: (cur.values || {})[k],
        unit: (cur.units || prev.units || {})[k] || ''
      };
    });
    rows.sort(function (x, y) {
      var dx = x.delta === null ? -1 : Math.abs(x.delta);
      var dy = y.delta === null ? -1 : Math.abs(y.delta);
      return dy - dx;
    });
    res.ok = true;
    res.rows = rows;
    res.prev = prev;
    res.cur = cur;
    var moved = rows.filter(function (r) { return r.delta !== null && r.delta !== 0; });
    var head = '总分 ' + _fmtScore(prev.total) + ' → ' + _fmtScore(cur.total)
      + '（' + _fmtScore((Number(cur.total) || 0) - (Number(prev.total) || 0)) + '）';
    if (!moved.length) {
      res.total_text = head + '：各因子分数均无变化';
    } else {
      res.total_text = head + '：'
        + moved.map(function (r) {
            return (r.name || r.key) + ' ' + _fmtScore(r.prev) + ' → ' + _fmtScore(r.cur)
              + '（' + _fmtScore(r.delta) + '）';
          }).join('、')
        + '；其余不变';
    }
    return res;
  }

  // 变动归因卡块（图 + 一行人话）。只有 1 个历史点时给占位与归档口径说明，不画假图。
  function _attrHtml(points, snap) {
    var att = _attribution(points, snap);
    if (!att.ok) {
      var why = (points && points.length === 1)
        ? '目前只有 1 个历史点，做不了变动归因。趋势按「数据口径日」归档，'
          + '同一天重复刷新只覆盖不新增；等下一交易日的数据出来再刷新即可看到逐因子变动。'
        : '还没有历史快照，点「刷新」跑一次后才会有趋势与归因。';
      return '<div class="th-attr" id="thAttr">'
        + '<div class="th-attr-head">变动归因 <span class="th-sub">最新两点逐因子 Δ分</span></div>'
        + '<div class="th-attr-note" id="thAttrNote">' + esc(why) + '</div></div>';
    }
    var moved = att.rows.filter(function (r) { return r.delta !== null && r.delta !== 0; });
    var chart = moved.length
      ? '<div id="thAttrChart" class="th-attr-chart"></div>'
      : '<div class="th-attr-note">最新两点（' + esc(att.prev.trade_date) + ' → '
        + esc(att.cur.trade_date) + '）各因子分数均无变化，无需归因图。</div>';
    return '<div class="th-attr" id="thAttr">'
      + '<div class="th-attr-head">变动归因 <span class="th-sub">'
      + esc(att.prev.trade_date) + ' → ' + esc(att.cur.trade_date) + '　按 |Δ| 排序</span></div>'
      + chart
      + '<div class="th-attr-note" id="thAttrNote">' + esc(att.total_text) + '</div>'
      + '<div class="th-attr-tip">Δ 只按因子分数（-2 ~ +2）计算；某期缺该因子读数时不算 Δ'
      + '（缺失 ≠ 0），读数变化见下方因子卡。</div></div>';
  }

  function _factorsHtml(factors, thesisId, points) {
    if (!factors || !factors.length) return '<div class="th-empty">没有因子数据。</div>';
    return factors.map(function (f) {
      var cls = f.score === null || f.score === undefined
        ? 'sn' : (SCORE_CLASS[String(f.score)] || 'sn');
      var unit = String(f.unit == null ? '' : f.unit);
      // 注意：不能用 Math.abs(unit).indexOf(...) —— unit 是字符串，Math.abs 得 NaN，
      // 再取 .indexOf 直接抛 "is not a function"。这类运行时错误只有真正渲染才暴露。
      var isYi = unit.indexOf('亿元') >= 0;
      var isSigned = unit.indexOf('pp') >= 0 || unit.indexOf('%偏离') >= 0;
      var num = Number(f.value);
      var val;
      if (f.value === null || f.value === undefined) {
        val = '—';
      } else if (!isFinite(num)) {
        val = String(f.value);
      } else if (isYi) {
        val = String(Math.round(num));
      } else {
        val = num.toFixed(2);
        // 与后端 _fmt_metric 口径一致：pp 类指标正数带 +（"17.65pp" 读不出方向）
        if (isSigned && num > 0) val = '+' + val;
      }
      var scoreText = (f.score === null || f.score === undefined)
        ? '—' : (f.score > 0 ? '+' + f.score : String(f.score));
      // 手工维护的因子分数照常计入总分，这里只把「人工填写」标出来，
      // 避免读者把 6 条读数都当成当日自动取到的。
      var isManual = !!f.manual;
      var vals = _seriesValues(points, f.key);
      // 读数本身就是「分位」的因子（黄金⑦位置维度）不画历史区间条：
      // 再算一次「读数在序列中的分位」既无意义、也会显示成另一个数。
      var range = _rangeBarHtml(vals, f);
      var nsr = (f.no_series_rank && !f.pct_rank_off)
        ? '<div class="th-nsr" title="这条读数本身就是分位/等级，不再套一层历史分位">本条即分位</div>'
        : '';
      return '<div class="th-factor' + (isManual ? ' is-manual' : '') + '"'
        + ' data-key="' + esc(f.key) + '" data-tid="' + esc(thesisId || '') + '">'
        + '<div class="th-factor-head"><span class="th-factor-name">' + esc(f.name)
        + (isManual
            ? '<span class="th-badge-manual" title="该因子暂无自动数据源，由人工维护；分数照常计入总分">手工维护</span>'
            : '')
        + '</span>'
        + '<span class="th-factor-status">' + esc(f.status || '') + '</span>'
        + '<span class="th-score ' + cls + '">' + esc(scoreText) + '</span></div>'
        + '<div class="th-factor-value">' + esc(val)
        + '<span class="unit">' + esc(unit) + '</span></div>'
        + '<div class="th-factor-chg">' + _changeChipHtml(vals, f) + '</div>'
        + _sparklineHtml(vals, unit)
        + range
        + nsr
        + '<div class="th-factor-detail">' + esc(f.detail) + '</div>'
        + _confHtml(f.confidence)
        + '<div class="th-factor-src">来源：' + esc(f.source) + '<br>数据日期：' + esc(f.asof)
        + (f.stale ? '　<span class="th-flag">数据可能滞后</span>' : '')
        + (f.note ? '<br>' + esc(f.note) : '') + '</div>'
        + '<div class="th-factor-ev">'
        + '<button type="button" class="th-ev-btn" data-tid="' + esc(thesisId || '')
        + '" data-key="' + esc(f.key) + '">▸ 关联性证据</button>'
        + '</div>'
        + '</div>';
    }).join('');
  }

  // 关联性证据抽屉的骨架在 index.html 的 #thPage 根下（fixed 定位，直接挂在
  // 页面根，不受 .th-card/.th-main 的 overflow 裁剪）。这里只操作 tab/内容。

  function _warnHtml(warnings) {
    if (!warnings || !warnings.length) return '';
    return '<div class="th-warnbox"><b>数据提示</b><ul>'
      + warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('')
      + '</ul></div>';
  }

  function _signalsHtml(signals) {
    var groups = [['buy', '加仓 / 持有信号'], ['warn', '减仓 / 警惕信号']];
    var html = '';
    groups.forEach(function (g) {
      var rows = (signals && signals[g[0]]) || [];
      html += '<table class="th-rows-table"><thead><tr><th></th><th>' + g[1]
        + '</th><th style="text-align:right">当前值</th></tr></thead><tbody>';
      if (!rows.length) {
        html += '<tr><td colspan="3" class="th-empty">无</td></tr>';
      } else {
        rows.forEach(function (r) {
          html += '<tr class="' + (r.hit ? 'hit' : '') + '"><td class="mk">'
            + (r.hit ? '√' : '—') + '</td><td>' + esc(r.label) + '</td>'
            + '<td class="cur">' + esc(r.current) + '</td></tr>';
        });
      }
      html += '</tbody></table>';
    });
    return html;
  }

  function _invalidationHtml(invalidation) {
    if (!invalidation || !invalidation.length) {
      return '<div class="th-empty">该台账未定义失效条件。</div>';
    }
    var hit = invalidation.filter(function (r) { return r.hit; }).length;
    var html = '<div class="th-empty" style="padding-top:0">'
      + (hit ? '<span class="th-flag">已命中 ' + hit + ' 条 —— 多头结论不再成立</span>'
        : '全部未命中（结论仍成立）') + '</div>'
      + '<table class="th-rows-table"><tbody>';
    invalidation.forEach(function (r) {
      html += '<tr class="' + (r.hit ? 'crit' : '') + '"><td class="mk">'
        + (r.hit ? '!' : '—') + '</td><td>' + esc(r.text) + '</td>'
        + '<td class="cur">' + esc(r.current) + '</td></tr>';
    });
    return html + '</tbody></table>';
  }

  function _alertsHtml(alerts) {
    if (!alerts || !alerts.length) {
      return '<div class="th-empty">近 30 天没有变更（只在结论真的变了的时候才会出现）。</div>';
    }
    return alerts.map(function (a) {
      return '<div class="th-alert">'
        + '<span class="th-alert-sev ' + esc(a.severity) + '">'
        + esc(SEV_LABEL[a.severity] || a.severity) + '</span>'
        + '<div class="th-alert-body"><div class="th-alert-msg">' + esc(a.message) + '</div>'
        + '<div class="th-alert-time">' + esc(a.trade_date) + '</div></div></div>';
    }).join('');
  }

  function _alertCount(d) {
    var s = d.alert_summary || {};
    var parts = [];
    if (s.critical) parts.push('严重 ' + s.critical);
    if (s.warn) parts.push('注意 ' + s.warn);
    if (s.info) parts.push('提示 ' + s.info);
    return parts.length ? parts.join(' · ') : '近 30 天无变更';
  }

  // ── 趋势图（总分柱 + 逐因子分数折线）+ 变动归因 ──────────────────────

  function _factorMetas(points, detail) {
    var out = [], seen = {};
    var factors = (detail && detail.snapshot && detail.snapshot.factors) || [];
    factors.forEach(function (f) {
      if (!f || !f.key || seen[f.key]) return;
      seen[f.key] = 1;
      out.push({key: f.key, name: f.name || f.key, unit: f.unit || ''});
    });
    // 快照里没有、但历史里有分数的因子（定义变更前后的老快照）也要能画
    (points || []).forEach(function (p) {
      Object.keys((p && p.scores) || {}).forEach(function (k) {
        if (seen[k]) return;
        seen[k] = 1;
        out.push({key: k, name: k, unit: (p.units && p.units[k]) || ''});
      });
    });
    return out;
  }

  async function renderChart(id, points, detail) {
    var el = $('thChart');
    if (!el) return;
    points = points || [];

    // 容器在重新渲染后会换掉旧的 canvas，先释放再重建，避免 echarts 警告与内存堆积
    if (_chart) { try { _chart.dispose(); } catch (e) { /* 忽略 */ } _chart = null; }
    if (_attrChart) { try { _attrChart.dispose(); } catch (e) { /* 忽略 */ } _attrChart = null; }

    var note = $('thChartNote');
    if (points.length < 2) {
      if (note) {
        note.style.display = '';
        // 说明归档口径：趋势按**数据口径日**归档，不是按点击次数。
        // 同一天重复刷新只覆盖不新增（避免趋势图出现重复点），
        // 所以「多刷几次」不会长出趋势——要等新一天的数据到位。
        note.textContent = '目前只有 1 个历史点。趋势按「数据口径日」归档，'
          + '同一天重复刷新只覆盖不新增；等下一交易日的数据出来再刷新即可看到趋势。';
      }
      el.style.display = 'none';
      return;
    }
    el.style.display = '';
    if (note) note.style.display = 'none';
    var echartsLib = await ensureECharts();
    if (!echartsLib) {
      if (note) { note.style.display = ''; note.textContent = '图表库加载失败（离线环境）。'; }
      el.style.display = 'none';
      return;
    }

    var metas = _factorMetas(points, detail);
    var dates = points.map(function (p) { return String(p.trade_date || '').slice(5); });
    var series = [{
      name: '总分',
      type: 'bar',
      yAxisIndex: 0,
      barMaxWidth: 26,
      data: points.map(function (p) {
        return {
          value: p.total,
          itemStyle: {
            // 只有两个色相：正分红、负分绿（A 股惯例）；强弱用饱和度区分，
            // 不再出现「弱正=品牌绿 / 弱负=琥珀」这种四种互不相干的颜色。
            color: p.total >= 0
              ? (p.total >= 2 ? '#e74c3c' : 'rgba(231,76,60,.45)')
              : (p.total <= -2 ? '#176b5b' : 'rgba(23,107,91,.45)'),
            borderRadius: [3, 3, 0, 0]
          }
        };
      }),
      markLine: {
        silent: true, symbol: 'none',
        lineStyle: { color: 'rgba(128,128,128,.45)', type: 'dashed' },
        data: [{ yAxis: 0 }]
      }
    }];
    metas.forEach(function (m, i) {
      series.push({
        name: m.name,
        type: 'line',
        yAxisIndex: 1,
        smooth: false,
        showSymbol: false,
        connectNulls: true,
        lineStyle: { width: 1.4, opacity: 0.85, color: FACTOR_COLORS[i % FACTOR_COLORS.length] },
        itemStyle: { color: FACTOR_COLORS[i % FACTOR_COLORS.length] },
        data: points.map(function (p) {
          var v = (p.scores || {})[m.key];
          return (v === null || v === undefined) ? null : Number(v);
        })
      });
    });

    _chart = echartsLib.init(el);
    _chart.setOption({
      grid: { left: 46, right: 58, top: 34, bottom: 26 },
      legend: {
        type: 'scroll', top: 2, left: 8, right: 8, itemWidth: 12, itemHeight: 8,
        textStyle: { fontSize: 10, color: '#6b7280' },
        data: ['总分'].concat(metas.map(function (m) { return m.name; }))
      },
      tooltip: {
        trigger: 'axis',
        confine: true,
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var p = points[ps[0].dataIndex] || {};
          var lines = [esc(p.trade_date) + '　总分 ' + _fmtScore(p.total)
            + '（' + esc(p.verdict || '—') + '）'];
          metas.forEach(function (m) {
            var sc = (p.scores || {})[m.key];
            var v = (p.values || {})[m.key];
            var st = (p.statuses || {})[m.key];
            if (sc === null || sc === undefined) {
              if (v === null || v === undefined) return;
            }
            var unit = (p.units || {})[m.key] || m.unit || '';
            lines.push(esc(m.name) + '：' + _fmtScore(sc) + ' 分　'
              + (v === null || v === undefined ? '读数 —' : _fmtSigned(v, unit) + esc(unit))
              + (st ? '　' + esc(st) : ''));
          });
          return lines.join('<br>');
        }
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { fontSize: 10, color: '#8f8e88' }
      },
      yAxis: [{
        type: 'value',
        name: '总分',
        nameTextStyle: { fontSize: 10, color: '#8f8e88' },
        axisLabel: { fontSize: 10, color: '#8f8e88' },
        splitLine: { lineStyle: { color: 'rgba(128,128,128,.15)' } }
      }, {
        type: 'value',
        name: '因子分',
        min: -2, max: 2, interval: 1,
        nameTextStyle: { fontSize: 10, color: '#8f8e88' },
        axisLabel: { fontSize: 10, color: '#8f8e88' },
        splitLine: { show: false }
      }],
      series: series
    });
    _renderAttribution(echartsLib, points, detail);
  }

  // 变动归因横条：Δscore 按 |Δ| 排序，红=加分 / 绿=减分（A 股习惯）。
  function _renderAttribution(echartsLib, points, detail) {
    var host = $('thAttrChart');
    if (!host) return;
    var att = _attribution(points, (detail && detail.snapshot) || null);
    if (!att.ok) return;
    var moved = att.rows.filter(function (r) { return r.delta !== null && r.delta !== 0; });
    if (!moved.length) { host.style.display = 'none'; return; }
    // 横向条形图按 |Δ| 从小到大排（ECharts category 轴自下而上），视觉上大的在上
    var rows = moved.slice().sort(function (a, b) { return Math.abs(a.delta) - Math.abs(b.delta); });
    _attrChart = echartsLib.init(host);
    _attrChart.setOption({
      grid: { left: 130, right: 40, top: 8, bottom: 20 },
      tooltip: {
        trigger: 'item',
        formatter: function (p) {
          var r = rows[(p && p.dataIndex) || 0] || {};
          return esc(r.name) + '<br>分数 ' + _fmtScore(r.prev) + ' → ' + _fmtScore(r.cur)
            + '（Δ ' + _fmtScore(r.delta) + '）'
            + (r.cur_value === null || r.cur_value === undefined ? ''
              : '<br>读数 ' + _fmtSigned(r.prev_value, r.unit) + esc(r.unit) + ' → '
                + _fmtSigned(r.cur_value, r.unit) + esc(r.unit));
        }
      },
      xAxis: {
        type: 'value',
        axisLabel: { fontSize: 10, color: '#8f8e88' },
        splitLine: { lineStyle: { color: 'rgba(128,128,128,.15)' } }
      },
      yAxis: {
        type: 'category',
        data: rows.map(function (r) { return r.name; }),
        axisLabel: { fontSize: 10, color: '#6b7280', width: 120, overflow: 'truncate' },
        axisTick: { show: false }
      },
      series: [{
        type: 'bar',
        barMaxWidth: 14,
        data: rows.map(function (r) {
          return {
            value: r.delta,
            itemStyle: { color: r.delta > 0 ? '#e74c3c' : '#176b5b', borderRadius: 3 }
          };
        }),
        label: {
          show: true, position: 'right', fontSize: 10, color: '#6b7280',
          formatter: function (p) { return _fmtScore(p.value); }
        },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { color: 'rgba(128,128,128,.45)' },
          data: [{ xAxis: 0 }]
        }
      }]
    });
  }

  function ensureECharts() {
    if (window.echarts) return Promise.resolve(window.echarts);
    var loader = window.StockRadar && window.StockRadar.utils
      && window.StockRadar.utils.loadScriptOnce;
    if (loader) {
      return loader(ECHARTS_CDN, 'echarts')
        .then(function () { return window.echarts || null; })
        .catch(function () { return null; });
    }
    return new Promise(function (resolve) {
      var s = document.createElement('script');
      s.src = ECHARTS_CDN;
      s.onload = function () { resolve(window.echarts || null); };
      s.onerror = function () { resolve(null); };
      document.head.appendChild(s);
    });
  }

  // ── 双轴图：评分 vs 标的（总览 8 张小图 + 详情页大图） ─────────────
  //
  // 这张图回答的是「这套台账的总分到底有没有跟着标的走」：左轴＝等权总分
  // （后端按**当前定义阈值**重放历史），右轴＝标的价格。两轴都 `scale:true`
  // **各自**自适应——既不做归一化，也不许把两轴锁成同一范围：分数是 ±N 的
  // 离散量、价格是上千点位，共用一个刻度必然压平一方（样式见 montage.png）。
  // 总览与详情共用同一份 `_scoreChartOption()`，只有尺寸/交互上的差别。

  // 双轴图取色：总分=品牌绿（主角），标的=墨黑（右轴读数），翻转=琥珀（唯一强调），
  // 期货/辅助=浅灰。原来是蓝/绛红/橙/灰四色并列，与站内没有任何关系。
  var SCORE_COLORS = { total: '#176b5b', flip: '#b45309', price: '#1c1c1a', fut: '#a9a8a1' };
  var SCORE_MAX_CONC = 3;      // 总览并发上限：后端每张图都要按定义重放，一次打满会拖慢首图
  var _scoreMiniCharts = {};   // 卡片序号 → echarts 实例（总览小图）
  var _scoreDetail = null;     // 详情页大图实例
  var _scorePanelOpen = false;
  var _scorePanelToken = 0;    // 每次打开/关闭/重载换令牌，用来丢弃过期响应
  var _scoreDetailToken = 0;

  // 定义版本提示：每张台账上次见到的 {def_hash, def_version}。
  // 用 localStorage 持久化才能跨页面刷新保留；隐私模式/Node 桩里 localStorage
  // 可能不存在或直接抛异常（Safari 私有模式），所以读写全部 try/catch，
  // 失败时退化为内存 Map——页面内仍能提示，绝不能让详情页渲染失败。
  var CHART_VER_KEY = 'sr_thesis_chart_ver_v1';
  var _chartVerMem = {};       // localStorage 不可用时的兜底存储

  function _numOf(v) {
    if (v === null || v === undefined || v === '') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  // 后端时间串是本地时间 "YYYY-MM-DD HH:MM[:SS]"（ISO 的 T 分隔在部分浏览器
  // 直接 Date.parse 会把空格形式当非法），统一换成 T 再解析；解析不了返回 null。
  function _parseServerTime(s) {
    if (!s) return null;
    var t = Date.parse(String(s).replace(' ', 'T'));
    return isFinite(t) ? t : null;
  }

  // 相关系数统一两位小数、正数带 +；缺失写 —（不写 0.00，避免读者当成「无关」）
  function _rhoText(v) {
    var n = _numOf(v);
    if (n === null) return '—';
    return (n > 0 ? '+' : '') + n.toFixed(2);
  }

  function _valText(v, signed) {
    var n = _numOf(v);
    if (n === null) return '—';
    return ((signed && n > 0) ? '+' : '') + n.toFixed(2);
  }

  // 卡片与详情副标题共用的三个 ρ：水平 / 未来 h 日 / 翻转后
  function _scoreMetricsHtml(d) {
    var m = (d && d.metrics) || {};
    var h = (d && d.h) || 20;
    return '<span class="th-chart-rho"><em>ρ水平</em><b>' + esc(_rhoText(m.r_level)) + '</b></span>'
      + '<span class="th-chart-rho"><em>ρ未来' + esc(h) + '日</em><b>' + esc(_rhoText(m.r_fwd)) + '</b></span>'
      + '<span class="th-chart-rho"><em>翻转后</em><b>' + esc(_rhoText(m.r_fwd_flip)) + '</b></span>';
  }

  // 副标题：口径要能一眼核对（版本 · 数据区间 · 样本数），不靠猜
  function _scoreRangeText(d) {
    var rg = (d && d.range) || [];
    if (rg.length < 2 || !rg[0] || !rg[1]) return '—';
    return '数据 ' + rg[0] + '~' + rg[1];
  }

  // 缓存新鲜度：后端 1h 缓存命中时 cached=true + cache_age_s（秒），force 重算时
  // cached=false。设计上「1 小时内即使数据前进一天也返回缓存」，所以必须把
  // 「这份图多久前算的」写在脸上，用户才知道要不要点「重新拉取」。
  //   cached=false → 刚重算
  //   cached=true  → 缓存 10 分钟前 / 缓存 2 小时 5 分钟前
  // cache_age_s 缺失（后端旧版本）时用 computed_at 兜底，再缺就只写「缓存命中」。
  function _cacheAgeText(d) {
    if (!d) return '';
    if (d.cached === false) return '刚重算';
    var sec = _numOf(d.cache_age_s);
    if (sec === null) {
      var t = _parseServerTime(d.computed_at);
      if (t === null) return d.cached === true ? '缓存命中' : '';
      sec = Math.max(0, (Date.now() - t) / 1000);
    }
    if (sec < 60) return '缓存 刚刚';
    var mins = Math.max(1, Math.round(sec / 60));
    if (mins < 60) return '缓存 ' + mins + ' 分钟前';
    var hrs = Math.floor(mins / 60);
    var rem = mins % 60;
    return '缓存 ' + hrs + ' 小时' + (rem ? ' ' + rem + ' 分钟' : '') + '前';
  }

  // 总览卡片：数据截止 + 缓存新鲜度（详情副标题另带区间与样本数）
  function _scoreFreshText(d) {
    var rg = (d && d.range) || [];
    var parts = [];
    if (rg.length > 1 && rg[1]) parts.push('数据 ' + rg[1]);
    var age = _cacheAgeText(d);
    if (age) parts.push(age);
    return parts.join(' · ');
  }

  // ── 定义版本提示（localStorage 持久化，失败退化为内存） ──────────
  // localStorage 在隐私模式 / Node 渲染桩里可能不存在、访问即抛、或 setItem 配额满：
  // 读写全部 try/catch，最差退化为内存 Map（同一页面内仍能提示），绝不阻断详情渲染。
  function _chartVerStore() {
    try {
      var ls = window.localStorage;
      return ls || null;
    } catch (e) { return null; }
  }

  function _chartVerLoad() {
    var ls = _chartVerStore();
    if (!ls) return _chartVerMem;
    try {
      var raw = ls.getItem(CHART_VER_KEY);
      var obj = raw ? JSON.parse(raw) : {};
      return (obj && typeof obj === 'object') ? obj : {};
    } catch (e) { return _chartVerMem; }
  }

  function _chartVerSave(map) {
    var ls = _chartVerStore();
    if (ls) {
      try {
        ls.setItem(CHART_VER_KEY, JSON.stringify(map));
      } catch (e) { /* 配额满/被禁用：下面同步进内存兜底 */ }
    }
    _chartVerMem = map;   // 内存兜底始终同步：下次 getItem 抛错时还有这份
  }

  // 返回要展示的提示文案（'' = 不提示），并把「本次见到的定义」记下来。
  // 首次见（没有记录）只记录不提示，避免第一次打开就误报「定义已更新」。
  function _chartVersionNotice(d) {
    var tid = (d && d.thesis_id) || _active;
    var hash = (d && d.def_hash) || '';
    if (!tid || !hash) return '';     // 后端还没给 def_hash：不猜，宁可不提示
    var ver = (d && d.def_version) || '';
    var map = _chartVerLoad();
    var prev = map[tid] || null;
    map[tid] = { def_hash: hash, def_version: ver };
    _chartVerSave(map);
    if (!prev || !prev.def_hash || prev.def_hash === hash) return '';
    var oldV = prev.def_version || '';
    if (ver && oldV && oldV !== ver) return '定义已更新 v' + oldV + '→v' + ver + '，图已重算';
    // 版本号没动但内容哈希变了：典型是直接改阈值/因子却没升 version，不能装作没发生
    return '定义内容已更新（阈值/因子），图已重算';
  }

  // 详情卡头 chip：与副标题同级；清空时同时隐藏（桩 DOM 会复用元素，不能残留旧文案）
  function _paintChartNotice(text) {
    var el = $('thScoreChartNotice');
    if (!el) return;
    if (!text) {
      el.textContent = '';
      el.style.display = 'none';
      return;
    }
    el.textContent = text;
    el.title = text;
    el.style.display = '';
  }

  // 总览卡片版本号旁的小圆点：只在版本变化时出现，tooltip 给完整原因
  function _paintChartDot(i, text) {
    var dot = $('thChartDot_' + i);
    if (!dot) return;
    dot.style.display = text ? '' : 'none';
    dot.title = text || '定义已更新';
  }

  // 图配置（总览小图与详情大图共用）。mini=true 只缩小字号/边距并去掉 dataZoom。
  function _scoreChartOption(d, mini) {
    var dates = (d && d.dates) || [];
    var total = (d && d.total) || [];
    var flip = (d && d.flip) || [];
    var price = (d && d.price) || [];
    var fut = (d && d.fut) || [];
    var h = (d && d.h) || 20;
    var fs = mini ? 9.5 : 10.5;
    var legendNames = ['总分(左)', '翻转后(左)', '标的价格(右)', '价格(' + h + '日后,右)'];
    var opt = {
      animation: false,
      color: [SCORE_COLORS.total, SCORE_COLORS.flip, SCORE_COLORS.price, SCORE_COLORS.fut],
      grid: mini
        ? { left: 36, right: 44, top: 26, bottom: 18 }
        : { left: 60, right: 68, top: 32, bottom: 28 },
      legend: {
        type: 'scroll', top: 0, left: 2, right: 2,
        itemWidth: mini ? 10 : 14, itemHeight: mini ? 7 : 9,
        itemGap: mini ? 6 : 12,
        textStyle: { fontSize: fs, color: '#6b7280' },
        data: legendNames
      },
      tooltip: {
        trigger: 'axis', confine: true,
        axisPointer: { type: 'cross' },
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var i = ps[0].dataIndex;
          if (i === null || i === undefined || i < 0) return '';
          return '<b>' + esc(dates[i] || '') + '</b>'
            + '<br>总分(左)：' + esc(_valText(total[i], true))
            + '<br>翻转后(左)：' + esc(_valText(flip[i], true))
            + '<br>标的价格(右)：' + esc(_valText(price[i], false))
            + '<br>' + esc(h) + ' 日后价格(右)：' + esc(_valText(fut[i], false));
        }
      },
      xAxis: {
        type: 'category', data: dates, boundaryGap: false,
        axisLine: { lineStyle: { color: '#dfe4ec' } },
        axisTick: { show: false },
        axisLabel: { fontSize: fs, color: '#8f8e88', hideOverlap: true }
      },
      yAxis: [
        {   // 左轴：总分，自适应本序列 min~max
          type: 'value', name: mini ? '' : '总分', position: 'left', scale: true,
          // 轴名放轴中间竖排：放顶部会和顶部图例撞在一起（报告 PNG 里尤其明显）
          nameLocation: mini ? 'end' : 'middle', nameRotate: mini ? 0 : 90,
          nameGap: mini ? 12 : 44,
          nameTextStyle: { fontSize: fs, color: SCORE_COLORS.total, padding: [0, 0, 2, 0] },
          axisLine: { show: true, lineStyle: { color: SCORE_COLORS.total } },
          axisLabel: { fontSize: fs, color: SCORE_COLORS.total },
          splitLine: { lineStyle: { color: 'rgba(128,128,128,.14)' } }
        },
        {   // 右轴：价格，独立自适应（不锁到左轴范围）
          type: 'value', name: mini ? '' : '价格', position: 'right', scale: true,
          nameLocation: mini ? 'end' : 'middle', nameRotate: mini ? 0 : 90,
          nameGap: mini ? 12 : 44,
          nameTextStyle: { fontSize: fs, color: SCORE_COLORS.price, padding: [0, 0, 2, 0] },
          axisLine: { show: true, lineStyle: { color: SCORE_COLORS.price } },
          axisLabel: { fontSize: fs, color: SCORE_COLORS.price },
          splitLine: { show: false }
        }
      ],
      series: [
        {   // 阶梯（step:'end'）：分数是按交易日离散跳变的，折线会伪造插值过程
          name: legendNames[0], type: 'line', step: 'end', showSymbol: false, symbol: 'none',
          lineStyle: { width: mini ? 1.6 : 2, color: SCORE_COLORS.total },
          itemStyle: { color: SCORE_COLORS.total }, data: total
        },
        {   // 反号（+ ↔ −）：用来检查「总分与价格反向」时是否只是符号约定问题
          name: legendNames[1], type: 'line', step: 'end', showSymbol: false, symbol: 'none',
          lineStyle: { width: mini ? 1 : 1.3, type: 'dashed', color: SCORE_COLORS.flip },
          itemStyle: { color: SCORE_COLORS.flip }, data: flip
        },
        {   // 标的价格：右轴
          name: legendNames[2], type: 'line', yAxisIndex: 1, showSymbol: false, symbol: 'none',
          lineStyle: { width: mini ? 1 : 1.4, color: SCORE_COLORS.price },
          itemStyle: { color: SCORE_COLORS.price }, data: price
        },
        {   // h 日后的价格（尾部 h 个 null）：总分与它同向 = 评分对未来有指示
          name: legendNames[3], type: 'line', yAxisIndex: 1, showSymbol: false, symbol: 'none',
          connectNulls: false,
          lineStyle: { width: 1, type: 'dotted', color: SCORE_COLORS.fut },
          itemStyle: { color: SCORE_COLORS.fut }, data: fut
        }
      ]
    };
    if (!mini) opt.dataZoom = [{ type: 'inside' }];
    return opt;
  }

  // 失败要能区分「接口还没装配」与「装上了但算不出来」：
  //   404 / 非 JSON / 没有 code 字段 → 「图表服务未就绪」（前端降级文案，绝不能白屏）
  //   {code:1, msg} → 「图表生成失败：msg」（把原因如实带给用户）
  function _chartFetchFail(json) {
    if (!json || typeof json !== 'object') return '图表服务未就绪';
    if (json.code === 0) return '';
    var msg = json.msg || json.detail || json.message || '';
    if (json.code === undefined || /not found|notfound|404|不存在|未找到/i.test(String(msg))) {
      return '图表服务未就绪';
    }
    return '图表生成失败：' + (msg || '未知原因');
  }

  async function _fetchScoreChart(tid, force) {
    var url = '/api/thesis/' + encodeURIComponent(tid) + '/chart?force='
      + (force ? '1' : '0') + '&points=1200';
    var resp = null;
    try {
      resp = await fetch(url);
    } catch (e) {
      return { ok: false, notReady: true, msg: '图表服务未就绪（服务未启动或网络不可达）' };
    }
    var json = null;
    try { json = await resp.json(); } catch (e) { json = null; }
    var fail = _chartFetchFail(json);
    if (fail) return { ok: false, notReady: /未就绪/.test(fail), msg: fail };
    var d = (json && json.data) || null;
    if (!d || !d.dates || !d.dates.length) {
      return { ok: false, notReady: true, msg: '图表服务未就绪（返回数据为空）' };
    }
    if (d.dates.length < 2 || !d.total || !d.total.length) {
      return { ok: false, msg: '样本不足（n=' + d.dates.length + '），暂不画图' };
    }
    return { ok: true, data: d };
  }

  function _scoreItems() {
    return (_items || []).filter(function (it) {
      return it && it.thesis_id && it.thesis_id !== PORTFOLIO_ID;
    });
  }

  async function _fetchThesisList() {
    try {
      var r = await fetch('/api/thesis/list');
      var j = await r.json();
      if (j && j.code === 0 && j.data && j.data.items) return j.data.items || [];
    } catch (e) { /* 下面统一降级：面板里给空态文案 */ }
    return [];
  }

  function _setChartMeta(text) {
    var el = $('thChartMeta');
    if (el) el.textContent = text || '—';
  }

  function _scoreCardHtml(it, i) {
    return '<div class="th-chart-card" data-tid="' + esc(it.thesis_id) + '"'
      + ' title="点击查看「' + esc(it.name || it.thesis_id) + '」详情">'
      + '<div class="th-chart-card-head">'
      + '<span class="th-chart-card-name">' + esc(it.name || it.thesis_id) + '</span>'
      + '<span class="th-chart-card-ver" id="thChartVer_' + i + '"></span>'
      // 定义变更小圆点：默认隐藏，取到新 def_hash 且与上次不同才亮
      + '<span class="th-chart-card-dot" id="thChartDot_' + i
      + '" style="display:none" title="定义已更新"></span>'
      + '<span class="th-chart-card-fresh" id="thChartFresh_' + i + '"></span>'
      + '<span class="th-chart-rho-wrap" id="thChartRho_' + i + '"></span>'
      + '</div>'
      + '<div class="th-chart-mini" id="thChartMini_' + i + '"></div>'
      + '<div class="th-chart-note" id="thChartNote_' + i + '">加载中…</div>'
      + '</div>';
  }

  function _paintScoreNote(i, msg) {
    var host = $('thChartMini_' + i);
    var note = $('thChartNote_' + i);
    var fresh = $('thChartFresh_' + i);
    if (host) host.style.display = 'none';
    if (fresh) fresh.textContent = '';
    _paintChartDot(i, '');            // 没数据就没资格提示「图已重算」
    if (note) {
      note.style.display = '';
      note.textContent = msg || '图表服务未就绪';
      if (note.classList) note.classList.add('err');
    }
  }

  function _paintScoreMini(it, i, d, ec) {
    var ver = $('thChartVer_' + i);
    var rho = $('thChartRho_' + i);
    var note = $('thChartNote_' + i);
    var fresh = $('thChartFresh_' + i);
    if (ver) ver.textContent = d.def_version ? ('v' + d.def_version) : '';
    if (fresh) fresh.textContent = _scoreFreshText(d);
    if (rho) rho.innerHTML = _scoreMetricsHtml(d);
    // 版本提示：与详情页共用同一份 localStorage 记录（谁先拉到新定义谁提示）
    _paintChartDot(i, _chartVersionNotice(d));
    var host = $('thChartMini_' + i);
    if (!host) return;
    if (_scoreMiniCharts[i]) {
      try { _scoreMiniCharts[i].dispose(); } catch (e) { /* 忽略 */ }
      delete _scoreMiniCharts[i];
    }
    var inst = null;
    try {
      inst = ec.init(host);
      _scoreMiniCharts[i] = inst;          // 先登记再 setOption：抛错也能被 dispose 掉
      inst.setOption(_scoreChartOption(d, true));
    } catch (e) {
      if (inst) { try { inst.dispose(); } catch (e2) { /* 忽略 */ } }
      delete _scoreMiniCharts[i];
      _paintScoreNote(i, '图表渲染失败：' + ((e && e.message) || e));
      return;
    }
    if (note) { note.style.display = 'none'; note.textContent = ''; }
  }

  function _disposeScoreMiniCharts() {
    Object.keys(_scoreMiniCharts).forEach(function (k) {
      try { _scoreMiniCharts[k].dispose(); } catch (e) { /* 忽略 */ }
    });
    _scoreMiniCharts = {};
  }

  function _disposeScoreDetailChart() {
    if (_scoreDetail) {
      try { _scoreDetail.dispose(); } catch (e) { /* 忽略 */ }
      _scoreDetail = null;
    }
  }

  // 总览：每次打开都重新 fetch（后端按 def_hash 缓存，热缓存很快）；
  // 并发 ≤ SCORE_MAX_CONC，单张失败只影响自己那张卡，不影响其它卡。
  // force=true 只在「↻ 重新拉取」时传：跳过后端 1h 缓存，强制按最新数据重算。
  async function openScorePanel(force) {
    force = (force === true);
    var panel = $('thChartPanel');
    if (!panel) return;
    _evCloseAll();                 // 证据抽屉与总览不叠在一起
    _closeReportPreview();
    panel.style.display = 'flex';
    _scorePanelOpen = true;
    var token = ++_scorePanelToken;
    _disposeScoreMiniCharts();
    var grid = $('thChartGrid');
    if (!grid) return;
    var items = _scoreItems();
    if (!items.length) {
      items = await _fetchThesisList();
      if (token !== _scorePanelToken) return;
      items = (items || []).filter(function (it) {
        return it && it.thesis_id && it.thesis_id !== PORTFOLIO_ID;
      });
    }
    if (!items.length) {
      grid.innerHTML = '<div class="th-chart-empty">还没有台账定义。先在台账页加载列表，再点「双轴总览」。</div>';
      _setChartMeta('0/0');
      return;
    }
    grid.innerHTML = items.map(_scoreCardHtml).join('');
    _setChartMeta('已加载 0/' + items.length);

    var ec = await ensureECharts();
    if (token !== _scorePanelToken) return;
    if (!ec) {
      items.forEach(function (it, i) { _paintScoreNote(i, '图表库加载失败（离线环境）。'); });
      _setChartMeta('图表库加载失败');
      return;
    }

    var okCount = 0, failed = 0, cursor = 0;
    async function worker() {
      while (cursor < items.length) {
        var i = cursor++;
        if (token !== _scorePanelToken) return;
        var it = items[i];
        var res = await _fetchScoreChart(it.thesis_id, force);
        if (token !== _scorePanelToken) return;   // 面板已关/重开：结果作废，不许画进新 DOM
        if (res.ok) {
          okCount++;
          _paintScoreMini(it, i, res.data, ec);
        } else {
          failed++;
          _paintScoreNote(i, res.msg || '图表服务未就绪');
        }
        _setChartMeta('已加载 ' + okCount + '/' + items.length
          + (failed ? '（' + failed + ' 张未就绪）' : ''));
      }
    }
    var jobs = [];
    var conc = Math.max(1, Math.min(SCORE_MAX_CONC, items.length));
    for (var w = 0; w < conc; w++) jobs.push(worker());
    await Promise.all(jobs);
    if (token !== _scorePanelToken) return;
    _setChartMeta('已加载 ' + okCount + '/' + items.length
      + (failed ? '（' + failed + ' 张未就绪）' : ''));
  }

  function closeScorePanel() {
    _scorePanelToken++;              // 作废在途请求
    _disposeScoreMiniCharts();
    _scorePanelOpen = false;
    var panel = $('thChartPanel');
    if (panel) panel.style.display = 'none';
    var grid = $('thChartGrid');
    if (grid) grid.innerHTML = '';   // 清 DOM：下次打开重建，不留旧图
    _setChartMeta('—');
  }

  // 详情页全宽卡：hero/口径之后的第一块内容（先看「评分 vs 标的」，再看五因子读数）
  function _scoreDetailCardHtml() {
    return '<div class="th-card th-score-card" id="thScoreChartCard">'
      + '<div class="th-card-head">评分 vs 标的（双轴）'
      + '<span class="th-sub" id="thScoreChartSub">加载中…</span>'
      // 定义变更 chip：与副标题同级；默认隐藏，只有 def_hash 变过才亮（见 _chartVersionNotice）
      + '<span class="th-chart-notice" id="thScoreChartNotice" style="display:none"></span>'
      + '</div>'
      + '<div class="th-scroll">'
      + '<div id="thScoreChart" class="th-score-chart"></div>'
      + '<div class="th-empty" id="thScoreChartNote" style="display:none"></div>'
      + '<div class="th-sec-note" style="margin:6px 0 0">左轴＝等权总分（当前定义阈值的'
      + '历史重放），右轴＝标的价格；两轴各自 scale 自适应。灰点线是 h 日后的价格——'
      + '总分与它同向，才说明评分对未来有指示。内样本形状核对，不构成样本外验证。</div>'
      + '</div></div>';
  }

  async function _renderScoreDetail(tid) {
    var token = ++_scoreDetailToken;
    _disposeScoreDetailChart();
    var sub = $('thScoreChartSub');
    // 先清掉上一次的 chip：切台账/重绘时旧提示不许残留（桩 DOM 会复用元素）
    _paintChartNotice('');
    var res = await _fetchScoreChart(tid, false);
    if (token !== _scoreDetailToken) return;    // 已切到别的台账：旧响应丢弃
    var host = $('thScoreChart');
    var note = $('thScoreChartNote');
    if (!host) return;
    if (!res.ok) {
      host.style.display = 'none';
      if (sub) sub.textContent = res.notReady ? '图表服务未就绪' : '暂不可用';
      if (note) { note.style.display = ''; note.textContent = res.msg || '图表服务未就绪'; }
      return;
    }
    var d = res.data;
    // 版本提示：内部会把本次 def_hash/version 记进 localStorage（首次见只记录不提示）
    var notice = _chartVersionNotice(d);
    if (sub) {
      var fresh = _cacheAgeText(d);
      sub.innerHTML = esc('v' + (d.def_version || '—')) + ' · ' + esc(_scoreRangeText(d))
        + ' · n=' + esc(d.n || d.dates.length)
        + (fresh ? ' · ' + esc(fresh) : '')
        + '　' + _scoreMetricsHtml(d);
    }
    _paintChartNotice(notice);
    var ec = await ensureECharts();
    if (token !== _scoreDetailToken) return;
    if (!ec) {
      host.style.display = 'none';
      if (note) { note.style.display = ''; note.textContent = '图表库加载失败（离线环境）。'; }
      return;
    }
    try {
      _scoreDetail = ec.init(host);
      _scoreDetail.setOption(_scoreChartOption(d, false));
    } catch (e) {
      _disposeScoreDetailChart();
      host.style.display = 'none';
      if (note) {
        note.style.display = '';
        note.textContent = '图表渲染失败：' + ((e && e.message) || e);
      }
      return;
    }
    host.style.display = '';
    if (note) note.style.display = 'none';
  }

  // ── 刷新 ────────────────────────────────────────────────────────

  async function refreshActive() {
    if (!_active || _busy) return;
    /* 组合层没有「重算」概念——它读的是各台账既有快照，只需重新拉一次 */
    if (_active === PORTFOLIO_ID) { await loadPortfolio(); return; }
    _busy = true;
    var btn = $('thRefreshBtn');
    if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
    setStatus('重算中（要取数，约 5–15 秒）…');
    try {
      var resp = await fetch('/api/thesis/' + encodeURIComponent(_active) + '/refresh',
        { method: 'POST' });
      var json = await resp.json();
      if (json.code !== 0) throw new Error(json.msg || '刷新失败');
      var d = json.data || {};
      var bits = [];
      if (d.snapshot_created) bits.push('新增历史点'); else bits.push('同一批数据，覆盖');
      if (d.definition_changed) bits.push('定义已变更并留痕');
      if (d.new_alert_count) bits.push('新增告警 ' + d.new_alert_count + ' 条');
      setStatus(bits.join(' · '));
      // 台账数据变了：报告 PNG 与后端报告数据都必须失效（后端 refresh 已清）
      delete _reportCache[_active];
      await load(_active);
    } catch (e) {
      setStatus('刷新失败：' + e.message);
    } finally {
      _busy = false;
      if (btn) { btn.disabled = false; btn.textContent = '↻ 刷新'; }
    }
  }

  // ── 因子「关联性证据」折叠区：4 张图 + 数据表 + 方向校验 ─────────────
  //
  // 回答的是「凭什么这个因子在这张卡上」：走势是否同向（形状）、散点方向与强度、
  // 五分位是否单调、滚动相关何时反转。**不做假图**：复合/手工/样本不足一律显示
  // 原因或占位。数据按 "tid|factor" 缓存，折叠再展开不重跑历史回放。

  var _evMode = {};   // "tid|factor" → 'value' | 'score'（散点口径）

  function _evCorr(h) {
    if (!h) return null;
    var v = (h.corr_value !== undefined) ? h.corr_value : h.corr;
    return (v === null || v === undefined || !isFinite(Number(v))) ? null : Number(v);
  }

  function _evCorrScore(h) {
    if (!h) return null;
    var v = h.corr_score;
    if (v === null || v === undefined) return null;
    return isFinite(Number(v)) ? Number(v) : null;
  }

  function _evHorizon(d) {
    var hs = ((d && d.horizons) || []).filter(Boolean);
    // 20 日是主口径**且必须真的算出相关系数**；否则退到第一个算得出 corr 的期限。
    // 退到的那个可能就是末尾的短窗口（h=5）——标题必须按 h.label 写「未来 5 个
    // 交易日」，不能硬编码 20（契约变化 2026-10-27）。
    var h20 = hs.filter(function (x) { return Number(x.h) === 20; })[0] || null;
    if (h20 && _evCorr(h20) !== null) return h20;
    var withCorr = hs.filter(function (x) { return _evCorr(x) !== null; });
    if (withCorr.length) {
      return withCorr.filter(function (x) { return !x.short_window; })[0] || withCorr[0];
    }
    return h20 || hs.filter(function (x) { return !x.short_window; })[0] || hs[0] || null;
  }

  // layers 兼容两代形状：
  //   新：{bucket,code,label,mean_return,win_rate,n,current}
  //   旧：裸数字数组（只有分组均值）
  function _evLayers(h) {
    var raw = (h && h.layers) || [];
    return raw.map(function (l, i) {
      if (l && typeof l === 'object') {
        var mr = l.mean_return;
        return {
          bucket: l.bucket || (i + 1),
          code: l.code || ('Q' + (i + 1)),
          label: l.label || ('Q' + (i + 1)),
          mean: (mr === null || mr === undefined || !isFinite(Number(mr))) ? null : Number(mr),
          win_rate: (l.win_rate === null || l.win_rate === undefined) ? null : Number(l.win_rate),
          n: (l.n === null || l.n === undefined) ? null : Number(l.n),
          current: !!l.current, lo: l.lo, hi: l.hi
        };
      }
      var mv = (l === null || l === undefined) ? null : Number(l);
      return {bucket: i + 1, code: 'Q' + (i + 1), label: 'Q' + (i + 1),
              mean: (mv === null || !isFinite(mv)) ? null : mv,
              win_rate: null, n: null, current: false};
    });
  }

  function _evCurrentLayer(d, h) {
    var layers = _evLayers(h);
    var cur = (d && d.current) || {};
    var idx = -1;
    layers.forEach(function (l, i) { if (l.current) idx = i; });
    if (idx < 0 && cur.bucket_label) {
      var m = /Q\s*(\d)/i.exec(String(cur.bucket_label));
      if (m) idx = Number(m[1]) - 1;
    }
    if (idx < 0 && cur.bucket_index !== null && cur.bucket_index !== undefined) {
      // 后端 bucket_index 是 1 基（cur_idx = i+1），越界就不认
      var bi = Number(cur.bucket_index);
      if (bi >= 1 && bi <= layers.length) idx = bi - 1;
    }
    if (idx < 0 || idx >= layers.length) idx = -1;
    return {layers: layers, index: idx,
            label: (layers[idx] || {}).label || cur.bucket_label || '—'};
  }

  function _fmtPct(v) {
    if (v === null || v === undefined || !isFinite(Number(v))) return '—';
    var n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  function _disposeEvCharts(factorKey) {
    (_evCharts[factorKey] || []).forEach(function (c) {
      try { c.dispose(); } catch (e) { /* 忽略 */ }
    });
    _evCharts[factorKey] = [];
  }

  function _disposeAllEvCharts() {
    Object.keys(_evCharts).forEach(function (k) { _disposeEvCharts(k); });
  }

  function _evDockEls() {
    return {dock: $('thEvDock'), tabs: $('thEvTabs'), add: $('thEvAdd'),
            body: $('thEvDockBody'), count: $('thEvDockCount')};
  }

  function _evShowDock() {
    var d = $('thEvDock');
    if (d) d.style.display = 'flex';
  }

  function _evHideDock() {
    var d = $('thEvDock');
    if (d) d.style.display = 'none';
  }

  function _evSyncAddOptions() {
    var sel = $('thEvAdd');
    if (!sel) return;
    var opts = ['<option value="">＋ 添加因子…</option>'];
    Array.prototype.forEach.call(document.querySelectorAll('.th-factor'), function (card) {
      var k = card.dataset ? card.dataset.key : '';
      if (!k || _evOpen.indexOf(k) >= 0) return;
      opts.push('<option value="' + esc(k) + '">' + esc(_evFactorName(k)) + '</option>');
    });
    sel.innerHTML = opts.join('');
    sel.disabled = opts.length === 1;
  }

  function _evFactorName(key) {
    var card = document.querySelector('.th-factor[data-key="' + key + '"]');
    var el = card && card.querySelector ? card.querySelector('.th-factor-name') : null;
    if (!el) return key;
    var txt = (el.textContent || '').replace('手工维护', '').trim();
    return txt || key;
  }

  function _evUpdateCards() {
    Array.prototype.forEach.call(document.querySelectorAll('.th-factor'), function (card) {
      var k = card.dataset ? card.dataset.key : '';
      var on = _evOpen.indexOf(k) >= 0;
      if (on) card.classList.add('is-ev-open');
      else card.classList.remove('is-ev-open');
      var btn = card.querySelector ? card.querySelector('.th-ev-btn') : null;
      if (btn) btn.textContent = on ? '▾ 已打开证据' : '▸ 关联性证据';
    });
  }

  function _evRenderTabs() {
    var els = _evDockEls();
    if (els.tabs) {
      els.tabs.innerHTML = _evOpen.map(function (k) {
        var on = k === _evActive;
        return '<button type="button" class="th-ev-tab' + (on ? ' is-on' : '') + '"'
          + ' data-ev-tab="' + esc(k) + '">'
          + '<span class="th-ev-tab-name">' + esc(_evFactorName(k)) + '</span>'
          + '<span class="th-ev-tab-x" data-ev-close="' + esc(k) + '" title="关闭">×</span>'
          + '</button>';
      }).join('');
    }
    if (els.count) {
      els.count.textContent = _evOpen.length
        ? '已打开 ' + _evOpen.length + ' 个 · 点标签切换（只渲染当前标签）' : '';
    }
    _evSyncAddOptions();
  }

  function _evActivate(key) {
    if (_evOpen.indexOf(key) < 0) return;
    _evActive = key;
    _evShowDock();
    _evRenderTabs();
    _evUpdateCards();
    var body = $('thEvDockBody');
    if (!body) return;
    _disposeAllEvCharts();
    _evLoadEvidence(body, _evTid || _active || '', key);
  }

  function _evLoadEvidence(body, thesisId, factorKey) {
    var ck = thesisId + '|' + factorKey;
    if (_evCache[ck]) { _renderEvBox(body, _evCache[ck], thesisId, factorKey); return; }
    _disposeAllEvCharts();
    body.innerHTML = '<div class="th-ev-off">加载中（首次要回放该因子的历史序列，约 10–60 秒）…</div>';
    fetch('/api/thesis/' + encodeURIComponent(thesisId)
          + '/evidence?factor=' + encodeURIComponent(factorKey))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var d = j && j.data;
        if (!d) {
          if (_evActive === factorKey) {
            body.innerHTML = '<div class="th-ev-off">证据接口未返回数据（不做假图）。</div>';
          }
          return;
        }
        _evCache[ck] = d;
        // 用户可能在请求期间切到别的标签：只有仍是当前标签才渲染
        if (_evActive === factorKey && _evOpen.indexOf(factorKey) >= 0) {
          _renderEvBox(body, d, thesisId, factorKey);
        }
      })
      .catch(function (e) {
        if (_evActive === factorKey) {
          body.innerHTML = '<div class="th-ev-off">关联性证据加载失败：'
            + esc((e && e.message) || '网络错误') + '（不做假图）</div>';
        }
      });
  }

  function _toggleEvidence(thesisId, factorKey, btn) {
    if (!thesisId) thesisId = _active;
    _evTid = thesisId || _evTid;
    var i = _evOpen.indexOf(factorKey);
    if (i >= 0) { _evClose(factorKey); return; }
    _evOpen.push(factorKey);
    _evActivate(factorKey);
  }

  function _evClose(key) {
    var i = _evOpen.indexOf(key);
    if (i < 0) return;
    _evOpen.splice(i, 1);
    if (_evActive === key) {
      // 关掉当前标签 → 优先激活左邻，没有就取下一个；全部关掉则收起停靠区
      _evActive = _evOpen[Math.max(0, i - 1)] || _evOpen[0] || null;
    }
    var els = _evDockEls();
    if (!_evOpen.length) {
      _evActive = null;
      _disposeAllEvCharts();
      if (els.body) els.body.innerHTML = '';
      _evHideDock();
    } else if (_evActive) {
      _evActivate(_evActive);
    }
    _evRenderTabs();
    _evUpdateCards();
  }

  function _evCloseAll() {
    _evOpen = [];
    _evActive = null;
    _disposeAllEvCharts();
    var els = _evDockEls();
    if (els.body) els.body.innerHTML = '';
    _evHideDock();
    _evRenderTabs();
    _evUpdateCards();
  }

  function _renderEvBox(box, d, thesisId, factorKey) {
    _disposeEvCharts(factorKey);
    if (!d.supported) {
      var n = (d.sample && d.sample.n) || d.n || 0;
      var isSnap = (d.source === 'snapshot');
      box.innerHTML = '<div class="th-ev">'
        + '<div class="th-ev-verdict" style="font-weight:400">无时序验证数据：'
        + esc(d.reason || '结构上不可回放') + '（不做假图）</div>'
        + (isSnap
            ? '<div class="th-ev-acc">积累中 ' + esc(n) + '/30 期——手工/不可回放因子靠每日快照累积，'
              + '攒够 30 期后才会算分位与关联性。</div>'
            : '')
        + '<div class="th-ev-note">页面仍会显示快照 sparkline（上方因子卡），'
        + '但不会给出相关系数/分层收益，避免用不足的样本编结论。</div></div>';
      return;
    }
    var ck = thesisId + '|' + factorKey;
    box.dataset.mode = _evMode[ck] || 'value';
    box.innerHTML = _evidenceHtml(d, thesisId, factorKey);
    _drawEvidenceCharts(d, factorKey, box.dataset.mode);
  }

  function _evidenceHtml(d, thesisId, factorKey) {
    var h = _evHorizon(d);
    var corr = _evCorr(h), corrScore = _evCorrScore(h);
    var tgt = d.target || {};
    var sample = d.sample || {};
    var cur = d.current || {};
    var sStart = sample.start || d.series_start || '';
    var sEnd = sample.end || '';
    // 「因子观测总数 n」≠「真正进入统计的有效 n」：目标日线只覆盖近两年时，
    // 225 期观测里可能只有 25 期能算出未来收益。**标题必须报有效 n**，
    // 观测总数只作为附注（契约 2026-10-27 新增 effective_n）。
    var sN = sample.n, sEff = sample.effective_n;
    var nAll = (sEff !== null && sEff !== undefined) ? sEff : sN;
    var mode = _evMode[thesisId + '|' + factorKey] || 'value';
    var layers = _evCurrentLayer(d, h);
    var q5q1 = (h && h.q5_q1 !== null && h.q5_q1 !== undefined) ? Number(h.q5_q1) : null;
    if (q5q1 === null && layers.layers.length >= 2) {
      var a = layers.layers[0].mean, b = layers.layers[layers.layers.length - 1].mean;
      if (a !== null && b !== null) q5q1 = b - a;
    }
    var unit = d.unit || '';
    var hv = (h && Number(h.h)) || 20;
    // 期限标签用后端给的（周频/月频台账的 h 不是「日」；短窗口是「未来 5 个交易日」）
    var hlabel = (h && h.label) || ('未来 ' + hv + ' 个交易日');
    var isShort = !!(h && h.short_window);
    var curLayer = layers.layers[layers.index] || {};

    var head = '<div class="th-ev-title">关联性证据 — ' + esc(tgt.name || d.factor_name || factorKey)
      + ' <span>' + esc(tgt.freq_label || tgt.freq || '')
      + (tgt.n ? ' · 标的样本 ' + esc(tgt.n) : '')
      + (sStart ? ' · 因子序列 ' + esc(sStart) + (sEnd ? ' ~ ' + esc(sEnd) : '') : '')
      + (nAll !== null && nAll !== undefined
          ? ' · 有效 n=' + esc(nAll)
            + (sN !== null && sN !== undefined && String(sN) !== String(nAll)
                ? '（观测 ' + esc(sN) + '）' : '')
          : '') + '</span>'
      + (isShort ? ' <span class="th-ev-short" title="短窗口：样本更少、未来收益重叠更严重，仅作方向参考">'
          + '短窗口参考</span>' : '')
      + '</div>'
      + (isShort
          ? '<div class="th-ev-note th-ev-short-note">本图用短窗口期限「' + esc(hlabel)
            + '」（常规 20/60 日期限样本不足）：重叠更严重、统计更不稳，只作方向参考。</div>'
          : '')
      + (sample.note && sN !== null && sN !== undefined
          && String(sN) !== String(nAll)
          ? '<div class="th-ev-note th-ev-sample-note">' + esc(sample.note) + '</div>' : '');

    var grid = '<div class="th-ev-grid">'
      // ① 走势对照（双坐标轴：左=因子读数、右=标的。两轴各按自身刻度，看形状也看拐点）
      + '<div class="th-ev-cell"><div class="th-ev-cell-head">① 走势对照'
      + '<span>左轴 因子 / 右轴 标的</span></div>'
      + (d.dual && (d.dual.dates || []).length
          ? '<div id="evc-' + esc(factorKey) + '-dual" class="th-ev-chart2"></div>'
          : '<div class="th-ev-off">后端未返回走势对照序列（降级：不画假图）</div>')
      + '</div>'
      // ② 散点 + 拟合线
      + '<div class="th-ev-cell"><div class="th-ev-cell-head">② 散点 + 拟合线'
      + '<span id="evr-' + esc(factorKey) + '-scatter">'
      + (corr === null ? 'r —' : 'r ' + _fmtSigned(corr))
      + ' · n ' + esc((h && h.n) || nAll || '—') + '</span>'
      + '<span class="th-ev-modes">'
      + '<button type="button" class="th-ev-mode' + (mode === 'value' ? ' is-on' : '')
      + '" data-key="' + esc(factorKey) + '" data-mode="value">原始读数</button>'
      + '<button type="button" class="th-ev-mode' + (mode === 'score' ? ' is-on' : '')
      + '" data-key="' + esc(factorKey) + '" data-mode="score">评分</button>'
      + '</span></div>'
      + '<div id="evc-' + esc(factorKey) + '-scatter" class="th-ev-chart2"></div>'
      + '<div class="th-ev-note" id="evn-' + esc(factorKey) + '-scatter">X=因子'
      + (mode === 'score' ? '分数' : (unit ? '读数（' + esc(unit) + '）' : '读数'))
      + '，Y=' + esc(hlabel) + '收益（%）</div></div>'
      // ③ 五分位分层
      + '<div class="th-ev-cell"><div class="th-ev-cell-head">③ 五分位未来收益'
      + '<span>Q5−Q1 ' + (q5q1 === null ? '—' : _fmtSigned(q5q1) + 'pp')
      + (layers.index >= 0 ? ' · 当前桶 ' + esc(curLayer.code || curLayer.label) : '') + '</span></div>'
      + (layers.layers.length
          ? '<div id="evc-' + esc(factorKey) + '-layers" class="th-ev-chart2"></div>'
          : '<div class="th-ev-off">后端未返回分层收益（降级：不画假图）</div>')
      + '<div class="th-ev-note">按读数分五组，每组历史平均' + esc(hlabel)
      + '收益；当前读数所在桶高亮。</div></div>'
      // ④ 滚动相关
      + '<div class="th-ev-cell"><div class="th-ev-cell-head">④ 滚动相关'
      + '<span>' + (_rollInfo(d) || '正相关占比 —') + '</span></div>'
      + (d.rolling && (d.rolling.dates || []).length
          ? '<div id="evc-' + esc(factorKey) + '-rolling" class="th-ev-chart2"></div>'
          : '<div class="th-ev-off">后端未返回滚动相关序列（降级：不画假图）</div>')
      + '<div class="th-ev-note">红=正相关 / 绿=负相关；0 轴上下穿越即相关性反转。</div></div>'
      + '</div>';

    var dirText = (d.direction && d.direction.text) || '';
    if (!dirText && corr !== null) {
      dirText = '读数与未来收益' + (corr > 0 ? '正相关' : (corr < 0 ? '负相关' : '基本无关'))
        + '（r=' + _fmtSigned(corr) + '）'
        + (corrScore === null ? '；评分方向未提供。'
            : (corrScore * corr < 0 ? '；但按阈值转换后的评分方向相反，评分表可能反了，需复核。'
                                    : '；评分方向与读数一致。'));
    }
    var table = '<table class="th-ev-table2"><thead><tr>'
      + '<th>当前读数</th><th>历史分位</th><th>当前桶均收益</th>'
      + '<th>胜率 / n</th><th>corr_value</th><th>corr_score</th>'
      + '</tr></thead><tbody><tr>'
      + '<td class="mono">' + ((cur.value === null || cur.value === undefined)
          ? '—' : _fmtSigned(cur.value, unit) + esc(unit)) + '</td>'
      + '<td class="mono">' + (cur.percentile === null || cur.percentile === undefined
          ? '—' : Math.round(Number(cur.percentile) * 100) + '%') + '</td>'
      + '<td class="mono">' + (curLayer.code ? curLayer.code + ' ' : '')
        + _fmtPct(curLayer.mean) + '</td>'
      + '<td class="mono">' + (curLayer.win_rate === null || curLayer.win_rate === undefined
          ? '—' : Number(curLayer.win_rate).toFixed(1) + '%')
        + ' / ' + (curLayer.n === null || curLayer.n === undefined ? '—' : curLayer.n) + '</td>'
      + '<td class="mono">' + (corr === null ? '—' : _fmtSigned(corr)) + '</td>'
      + '<td class="mono">' + (corrScore === null ? '—' : _fmtSigned(corrScore)) + '</td>'
      + '</tr></tbody></table>';

    var caveats = (d.caveats || []).length
      ? '<ul class="th-ev-caveats">' + d.caveats.map(function (c) {
          return '<li>' + esc(c) + '</li>';
        }).join('') + '</ul>'
      : '<div class="th-ev-note">后端未返回 caveats：相关不等于因果，分组区间为样本内所选。</div>';

    return '<div class="th-ev">'
      + '<div class="th-ev-verdict">' + esc(d.verdict || '—') + '</div>'
      + head + grid + table
      + '<div class="th-ev-dir">方向校验：' + esc(dirText || '—') + '</div>'
      + caveats
      + '</div>';
  }

  function _rollInfo(d) {
    var r = d.rolling || {};
    var parts = [];
    if (r.label) parts.push(r.label);
    if (r.positive_share !== null && r.positive_share !== undefined) {
      parts.push('正相关占比 ' + Number(r.positive_share).toFixed(0) + '%');
    }
    var cv = (r.current_corr_value !== undefined) ? r.current_corr_value : d.rolling_current;
    if (cv !== null && cv !== undefined) parts.push('当前 ' + _fmtSigned(cv));
    return parts.join(' · ');
  }

  function _scatterPoints(d, mode) {
    if (mode === 'score') {
      if (d.scatter_score && d.scatter_score.length) return d.scatter_score;
      return null;
    }
    return (d.scatter && d.scatter.length) ? d.scatter : null;
  }

  function _setEvScatterMode(factorKey, mode, btn) {
    var box = document.getElementById('ev-' + factorKey);
    if (!box) return;
    var tid = box.dataset.tid || _active || '';
    var ck = tid + '|' + factorKey;
    var d = _evCache[ck];
    if (!d || !d.supported) return;
    _evMode[ck] = mode;
    box.dataset.mode = mode;
    var buttons = box.querySelectorAll ? box.querySelectorAll('.th-ev-mode') : [];
    for (var i = 0; i < buttons.length; i++) {
      var b = buttons[i];
      if (b.dataset && b.dataset.mode === mode) b.classList.add('is-on');
      else b.classList.remove('is-on');
    }
    var host = document.getElementById('evc-' + factorKey + '-scatter');
    if (!host) return;
    var pts = _scatterPoints(d, mode);
    // 口径切换只重画散点（其余 3 张不变），避免整块重建导致滚动位置跳动
    disposeIn(_evCharts[factorKey], host);
    host.innerHTML = '';
    // 清掉 ECharts 挂在容器上的实例标记，否则重新 init 会走「容器已有实例」的告警分支
    try { host.removeAttribute('_echarts_instance_'); } catch (e) { /* 忽略 */ }
    if (!pts) {
      host.innerHTML = '<div class="th-ev-off">该口径未返回散点数据（不做假图）。</div>';
      return;
    }
    var ch = _drawScatter(window.echarts, d, factorKey, host, mode);
    if (ch) (_evCharts[factorKey] = _evCharts[factorKey] || []).push(ch);
    // 说明行跟着口径改（否则切到「评分」后还写着「X=因子读数」）
    var note = document.getElementById('evn-' + factorKey + '-scatter');
    if (note) {
      var h = _evHorizon(d);
      var hlabel = (h && h.label) || ('未来 ' + ((h && h.h) || 20) + ' 个交易日');
      var unit = d.unit || '';
      note.textContent = 'X=因子'
        + (mode === 'score' ? '分数' : (unit ? '读数（' + unit + '）' : '读数'))
        + '，Y=' + hlabel + '收益（%）';
    }
    void btn;
  }

  // 释放绑在某个容器上的实例（口径切换时只换这一张图）
  function disposeIn(list, host) {
    if (!list || !host) return;
    for (var i = list.length - 1; i >= 0; i--) {
      var c = list[i];
      var dom = null;
      try { dom = c.getDom ? c.getDom() : (c.__host || null); } catch (e) { dom = c.__host || null; }
      if (dom === host || (c && c.__host === host)) {
        try { c.dispose(); } catch (e) { /* 忽略 */ }
        list.splice(i, 1);
      }
    }
  }

  function _drawEvidenceCharts(d, factorKey, mode) {
    var lib = window.echarts;
    if (!lib || !d || !d.supported) return;
    var charts = _evCharts[factorKey] || [];
    _evCharts[factorKey] = charts;

    // 单张图渲染失败（ECharts 版本差异、字段异常）不能毁掉整个证据面板：
    // 失败的那一格就地写原因，其余三张照常显示——这是「不画假图」的落地方式。
    function safe(name, fn) {
      var host = document.getElementById('evc-' + factorKey + '-' + name);
      if (!host) return;
      try {
        var ch = fn(host);
        if (ch) charts.push(ch);
      } catch (e) {
        console.warn('[thesis] 证据图渲染失败(' + name + '):', e && e.message);
        try { host.innerHTML = '<div class="th-ev-off">该图渲染失败：'
          + esc((e && e.message) || '未知错误') + '</div>'; } catch (e2) { /* 忽略 */ }
      }
    }

    if (d.dual && (d.dual.dates || []).length) {
      safe('dual', function (host) { return _drawDual(lib, host, d); });
    }
    safe('scatter', function (host) { return _drawScatter(lib, d, factorKey, host, mode || 'value'); });
    safe('layers', function (host) { return _drawLayers(lib, host, d); });
    if (d.rolling && (d.rolling.dates || []).length) {
      safe('rolling', function (host) { return _drawRolling(lib, host, d); });
    }
  }

  // ① 走势对照：**双坐标轴**——左轴因子读数、右轴标的。
  //
  // 为什么用双轴而不是标准化：标准化（z-score×10+100）能让两条线共用一个刻度，
  // 但代价是读者看不到「因子实际读数是多少」「标的实际点位是多少」，
  // 而且波动大的因子会把标的压成一条平线（资金面 Shibor vs 债券指数就是典型）。
  // 双轴各自按自身量级取刻度，形状与拐点都能直接读；单位差异由轴名承担。
  // 后端同时返回 factor/target（原值）与 factor_norm/target_norm（标准化），这里取原值。
  function _drawDual(lib, host, d) {
    var dual = d.dual || {};
    var dates = dual.dates || [];
    // 点数多时按步长抽样：1200 个点画在 500px 宽里只会糊成一片，还会拖慢渲染
    var step = Math.max(1, Math.ceil(dates.length / 400));
    var idx = [];
    for (var i = 0; i < dates.length; i += step) idx.push(i);
    if (idx[idx.length - 1] !== dates.length - 1) idx.push(dates.length - 1);
    function pick(arr) { return idx.map(function (i) { return arr[i]; }); }

    var unit = d.unit || '';
    var fName = '因子' + (unit ? '（' + unit + '）' : '');
    var tgt = d.target || {};
    var tName = '标的' + (tgt.unit ? '（' + tgt.unit + '）' : '');
    var fac = pick(dual.factor || []);
    var tar = pick(dual.target || []);

    function _num(v, digits) {
      if (v === null || v === undefined || isNaN(Number(v))) return '—';
      return Number(v).toFixed(digits === undefined ? 2 : digits);
    }

    var ch = lib.init(host);
    ch.setOption({
      grid: { left: 54, right: 54, top: 34, bottom: 22 },
      legend: { top: 0, itemWidth: 12, itemHeight: 8,
                textStyle: { fontSize: 10, color: '#6b7280' },
                data: [fName, tName] },
      tooltip: {
        trigger: 'axis',
        confine: true,
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var i2 = ps[0].dataIndex;
          var k = (i2 >= 0 && i2 < idx.length) ? idx[i2] : -1;
          if (k < 0) return '';
          return esc(dates[k] || '')
            + '<br>' + esc(fName) + ' <b>' + esc(_num(fac[i2], 3)) + '</b>'
            + '<br>' + esc(tName) + ' <b>' + esc(_num(tar[i2], 2)) + '</b>';
        }
      },
      xAxis: { type: 'category', data: pick(dates),
               axisLabel: { fontSize: 9, color: '#8f8e88' } },
      yAxis: [
        { type: 'value', scale: true, name: fName, nameTextStyle: { fontSize: 9, color: '#8f8e88' },
          axisLabel: { fontSize: 9, color: '#8f8e88' },
          splitLine: { lineStyle: { color: 'rgba(128,128,128,.15)' } } },
        { type: 'value', scale: true, name: tName, nameTextStyle: { fontSize: 9, color: '#8f8e88' },
          axisLabel: { fontSize: 9, color: '#8f8e88' }, splitLine: { show: false } }
      ],
      series: [
        { name: fName, type: 'line', yAxisIndex: 0, showSymbol: false, smooth: false,
          lineStyle: { width: 1.4, color: '#e74c3c' }, itemStyle: { color: '#e74c3c' },
          data: fac },
        { name: tName, type: 'line', yAxisIndex: 1, showSymbol: false, smooth: false,
          lineStyle: { width: 1.6, color: '#8f8e88' }, itemStyle: { color: '#8f8e88' },
          data: tar }
      ]
    });
    return ch;
  }

  // ② 散点 + 最小二乘拟合线。r 用后端给的值（它基于同一份过滤后的样本）。
  function _drawScatter(lib, d, factorKey, host, mode) {
    var pts = _scatterPoints(d, mode);
    if (!pts) return null;
    var h = _evHorizon(d);
    var corr = _evCorr(h);
    var xs = [], ys = [];
    pts.forEach(function (p) {
      if (!p || p.length < 2) return;
      var x = Number(p[0]), y = Number(p[1]);
      if (isFinite(x) && isFinite(y)) { xs.push(x); ys.push(y); }
    });
    if (!xs.length) return null;
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    var n = xs.length, sx = 0, sy = 0, sxx = 0, sxy = 0;
    for (var i = 0; i < n; i++) { sx += xs[i]; sy += ys[i]; sxx += xs[i] * xs[i]; sxy += xs[i] * ys[i]; }
    var den = n * sxx - sx * sx;
    var slope = den ? (n * sxy - sx * sy) / den : 0;
    var intercept = (sy - slope * sx) / n;
    var fit = (maxX === minX)
      ? [[minX, intercept]]
      : [[minX, slope * minX + intercept], [maxX, slope * maxX + intercept]];
    var ch = lib.init(host);
    ch.__host = host;   // 口径切换时据此定位要释放的实例（ECharts 实例没有稳定可用的 getDom 桩）
    ch.setOption({
      grid: { left: 46, right: 12, top: 10, bottom: 30 },
      tooltip: {
        trigger: 'item',
        confine: true,
        formatter: function (p) {
          if (!p || !p.value) return '';
          return (mode === 'score' ? '因子分 ' : '因子值 ') + p.value[0]
            + '<br>' + ((h && h.label) || ('未来 ' + ((h && h.h) || 20) + ' 个交易日'))
            + ' ' + _fmtPct(p.value[1]);
        }
      },
      xAxis: { type: 'value', scale: true, name: mode === 'score' ? '因子分' : '因子值',
               nameTextStyle: { fontSize: 9 }, axisLabel: { fontSize: 9, color: '#8f8e88' },
               splitLine: { lineStyle: { color: 'rgba(128,128,128,.12)' } } },
      yAxis: { type: 'value', scale: true, name: '未来收益 %',
               nameTextStyle: { fontSize: 9 }, axisLabel: { fontSize: 9, color: '#8f8e88' },
               splitLine: { lineStyle: { color: 'rgba(128,128,128,.12)' } } },
      series: [
        { name: '样本', type: 'scatter', symbolSize: 4, data: pts,
          itemStyle: { opacity: 0.4, color: '#a9a8a1' } },
        { name: '拟合线', type: 'line', showSymbol: false, data: fit,
          lineStyle: { width: 1.6, color: corr !== null && corr < 0 ? '#176b5b' : '#e74c3c' },
          itemStyle: { color: corr !== null && corr < 0 ? '#176b5b' : '#e74c3c' } }
      ]
    });
    return ch;
  }

  // ③ 五分位未来收益柱状图。A 股惯例：正收益红、负收益绿；当前桶加粗描边。
  function _drawLayers(lib, host, d) {
    var h = _evHorizon(d);
    var cur = _evCurrentLayer(d, h);
    var layers = cur.layers;
    if (!layers.length) return null;
    var hlabel = (h && h.label) || ('未来 ' + ((h && h.h) || 20) + ' 个交易日');
    var ch = lib.init(host);
    ch.setOption({
      grid: { left: 42, right: 10, top: 22, bottom: 30 },
      tooltip: {
        trigger: 'axis',
        confine: true,
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var l = layers[ps[0].dataIndex] || {};
          return esc(l.label) + (l.current ? '（当前桶）' : '')
            + '<br>平均 ' + _fmtPct(l.mean)
            + (l.win_rate === null ? '' : '　胜率 ' + Number(l.win_rate).toFixed(1) + '%')
            + (l.n === null ? '' : '　n=' + l.n);
        }
      },
      // 轴标签用短码 Q1..Q5：完整 label 带区间（"Q4（2.1 ~ 3.0pp）"）在 168px 里
      // 会互相压住，完整区间放 tooltip
      xAxis: { type: 'category', data: layers.map(function (l) { return l.code || l.label; }),
               axisLabel: { fontSize: 10, color: '#8f8e88' },
               axisTick: { show: false } },
      // y 轴名保持短（完整期限写在卡片头与说明里），否则「未来 20 个交易日」
      // 会横着压住上半张图
      yAxis: { type: 'value', name: '收益 %',
               nameTextStyle: { fontSize: 9 }, axisLabel: { fontSize: 9, color: '#8f8e88' },
               splitLine: { lineStyle: { color: 'rgba(128,128,128,.12)' } } },
      series: [{
        type: 'bar',
        barMaxWidth: 22,
        data: layers.map(function (l) {
          var pos = l.mean !== null && l.mean >= 0;
          return {
            value: l.mean,
            itemStyle: {
              color: l.mean === null ? '#c2c1ba' : (pos ? '#e74c3c' : '#176b5b'),
              borderRadius: 3,
              borderColor: l.current ? '#1c1c1a' : 'transparent',
              borderWidth: l.current ? 2 : 0,
              opacity: l.current ? 1 : 0.78
            },
            label: {
              show: true, position: 'top', fontSize: 9, color: '#6b7280',
              formatter: l.current ? '当前' : ''
            }
          };
        }),
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { color: 'rgba(128,128,128,.45)' },
          data: [{ yAxis: 0 }]
        }
      }]
    });
    return ch;
  }

  // ④ 滚动相关：0 轴 + 正/负区间底色（红=正 / 绿=负），标题带正相关占比。
  // ⚠️ 底色不用 visualMap：本机 ECharts 版本对「line + visualMap(dimension:1) + 大量
  // null」会抛 `Cannot read properties of undefined (reading 'coord')`（实测），
  // 改用两条只填面积的辅助系列（正值区间红、负值区间绿）+ 一条连续折线。
  function _drawRolling(lib, host, d) {
    var r = d.rolling || {};
    var dates = r.dates || [];
    var vals = r.corr_value || [];
    var scores = r.corr_score || [];
    var step = Math.max(1, Math.ceil(dates.length / 400));
    var idx = [];
    for (var i = 0; i < dates.length; i += step) idx.push(i);
    if (idx[idx.length - 1] !== dates.length - 1) idx.push(dates.length - 1);
    function pick(arr) { return idx.map(function (i) { return arr[i]; }); }
    var pickVals = pick(vals);
    var pickScores = scores.length ? pick(scores) : [];
    var posArea = pickVals.map(function (v) {
      return (v === null || v === undefined || v <= 0) ? null : v;
    });
    var negArea = pickVals.map(function (v) {
      return (v === null || v === undefined || v >= 0) ? null : v;
    });
    var lastVal = null;
    for (var k = pickVals.length - 1; k >= 0; k--) {
      if (pickVals[k] !== null && pickVals[k] !== undefined) { lastVal = pickVals[k]; break; }
    }
    var series = [{
      name: 'corr(读数, 未来收益)',
      type: 'line',
      showSymbol: false,
      data: pickVals,
      lineStyle: { width: 1.6 },
      itemStyle: { color: '#a9a8a1' },
      z: 4,
      markLine: {
        silent: true, symbol: 'none',
        lineStyle: { color: 'rgba(128,128,128,.35)' },
        data: (lastVal === null ? [] : [{
          yAxis: 0,
          label: { show: false }
        }]).concat(lastVal === null ? [] : [{
          yAxis: lastVal,
          lineStyle: { color: 'rgba(28,36,48,.55)', type: 'dashed' },
          label: {
            show: true, position: 'insideEndTop', fontSize: 9, color: '#1c1c1a',
            formatter: '当前 ' + _fmtSigned(lastVal)
          }
        }])
      }
    }, {
      name: '正区间', type: 'line', showSymbol: false, silent: true,
      data: posArea, lineStyle: { opacity: 0 },
      areaStyle: { color: 'rgba(192,57,43,.20)', origin: 'start' }, z: 1
    }, {
      name: '负区间', type: 'line', showSymbol: false, silent: true,
      data: negArea, lineStyle: { opacity: 0 },
      areaStyle: { color: 'rgba(30,132,73,.22)', origin: 'end' }, z: 1
    }];
    if (pickScores.length) {
      series.push({
        name: 'corr(评分, 未来收益)',
        type: 'line',
        showSymbol: false,
        data: pickScores,
        lineStyle: { width: 1.2, type: 'dashed' },
        itemStyle: { color: '#8f8e88' }
      });
    }
    var ch = lib.init(host);
    ch.setOption({
      grid: { left: 42, right: 12, top: 26, bottom: 26 },
      legend: {
        top: 0, itemWidth: 12, itemHeight: 8,
        textStyle: { fontSize: 10, color: '#6b7280' },
        data: ['corr(读数, 未来收益)'].concat(pickScores.length ? ['corr(评分, 未来收益)'] : [])
      },
      tooltip: {
        trigger: 'axis',
        confine: true,
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var j = ps[0].dataIndex;
          var k2 = idx[j];
          return esc(dates[k2] || '') + '<br>corr_value '
            + _fmtSigned(vals[k2]) + (pickScores.length
              ? '<br>corr_score ' + _fmtSigned(scores[k2]) : '');
        }
      },
      xAxis: { type: 'category', data: pick(dates),
               axisLabel: { fontSize: 9, color: '#8f8e88' } },
      yAxis: { type: 'value', min: -1, max: 1, interval: 0.5,
               axisLabel: { fontSize: 9, color: '#8f8e88' },
               splitLine: { lineStyle: { color: 'rgba(128,128,128,.12)' } } },
      series: series
    });
    return ch;
  }

  // ── 报告图片（浏览器端生成，不引入服务端 matplotlib/playwright） ─────
  //
  // 流程：GET /report（后端一次组装，首次含证据计算约 20–60 秒）→ 在 body 末尾插入
  // 宽度 1240px 的离屏容器 → 用 ECharts **SVG renderer** 画 3 张图 → html-to-image
  // 截 PNG → 预览弹窗（下载 / 关闭）。
  //
  // 两条硬约束：
  // 1) 必须 SVG renderer：html-to-image 对 <canvas> 支持很差，克隆时位图丢失，
  //    报告图里的图表会整块空白。
  // 2) 离屏容器**不能用负坐标**（left:-99999px）：html-to-image 用
  //    <svg><foreignObject> 渲染克隆树，负坐标会落到视口外被裁成空白图。
  //    这里按 chat-export 同款做法：fixed 定位在 (0,0) + z-index 压底 + 不响应指针。

  function _pad2(n) { return (n < 10 ? '0' : '') + n; }

  function _sanitizeName(s) {
    return String(s == null ? '' : s)
      .replace(/[\\/:*?"<>|\s]+/g, '_').replace(/_+/g, '_').replace(/^_+|_+$/g, '');
  }

  function _reportFileName(rep) {
    var name = (rep && rep.thesis && rep.thesis.name) || _active || '台账';
    name = _sanitizeName(name);
    if (!name) name = '台账';
    if (name.length > 40) name = name.slice(0, 40);
    var d = new Date();
    var ymd = String(d.getFullYear()) + _pad2(d.getMonth() + 1) + _pad2(d.getDate());
    return '研究台账_' + name + '_' + ymd + '.png';
  }

  function _setReportBtn(btn, busy) {
    if (!btn) return;
    btn.disabled = !!busy;
    btn.textContent = busy ? '生成中…（约 20–60 秒）' : '生成报告';
  }

  function _toast(text, kind) {
    var u = window.StockRadar && window.StockRadar.utils;
    if (u && typeof u.toast === 'function') { u.toast(text, kind || 'info'); return; }
    setStatus(text);
  }

  function _reportRows(rep) {
    var ev = rep && rep.evidence;
    if (!ev) return [];
    if (Array.isArray(ev)) return ev;
    return ev.rows || ev.items || [];
  }

  function _defaultCaveats() {
    return [
      '相关不等于因果：因子与未来收益的统计关系只说明「历史上同向」，不构成因果或预测保证。',
      '未来收益窗口重叠（20/60 日）会让有效独立样本少于名义 n，统计显著性因此偏乐观。',
      '五分位分组区间是样本内所选，换区间/换窗口后单调性可能消失，需样本外再验证。',
    ];
  }

  function _reportHtml(rep) {
    var th = rep.thesis || {};
    var snap = rep.snapshot || null;
    var def = rep.definition || {};
    var hist = (rep.history && rep.history.items) || [];
    var att = rep.attribution || null;
    var rows = _reportRows(rep);
    var alerts = rep.alerts || [];
    var caveats = (rep.caveats && rep.caveats.length) ? rep.caveats : _defaultCaveats();

    var snapFactors = (snap && snap.factors) || [];
    var defFactors = def.factors || [];
    var factors = snapFactors.length ? snapFactors : defFactors;
    var unitOf = {};
    factors.forEach(function (f) { if (f && f.key) unitOf[f.key] = f.unit || ''; });

    var asof = snap ? (snap.data_asof || snap.trade_date) : '—';
    var total = snap ? _fmtScore(snap.total) : '—';
    var lowF = snapFactors.filter(function (f) {
      return (f.confidence || {}).level === 'low' && f.score;
    });
    var lowW = lowF.reduce(function (a, f) { return a + Math.abs(f.score); }, 0);

    // 变动归因：优先后端 attribution，缺失时按 history 末两点现算（同一口径）
    var attrRows = (att && att.rows) ? att.rows : [];
    var attrNote = (att && att.note) || '';
    if (!attrRows.length) {
      var calc = _attribution(hist, snap);
      if (calc.ok) {
        attrRows = calc.rows;
        attrNote = attrNote || calc.total_text;
      }
    }
    var attrMoved = attrRows.filter(function (r) {
      return r.delta !== null && r.delta !== undefined && Number(r.delta) !== 0;
    });

    var hero = '<section class="th-rp-hero">'
      + '<div class="th-rp-hero-main">'
      + '<span class="th-rp-total">总分 ' + esc(total) + '</span>'
      + '<span class="th-rp-verdict">' + esc(snap ? (snap.verdict || '未刷新') : '未刷新') + '</span>'
      + '<span class="th-rp-action">' + esc(snap ? (snap.action || '') : '') + '</span>'
      + '</div>'
      + '<div class="th-rp-hero-facts">'
      + '有效因子 ' + esc(snap ? snap.valid_count : '—') + ' / ' + esc(snap ? snap.factor_count : '—')
      + (lowW ? '　<span class="th-rp-warn">其中 ' + esc(lowW) + ' 分来自低置信度因子（'
          + esc(lowF.length) + ' 项），宜按更低权重看待</span>' : '')
      + '</div></section>';

    // 最近变动：一行人话 + 归因图 + 告警
    var attrBlock = '<section class="th-rp-sec">'
      + '<h2>最近变动<span>' + esc((att && att.prev_trade_date) || '')
      + ((att && att.cur_trade_date) ? ' → ' + esc(att.cur_trade_date) : '') + '</span></h2>'
      + (attrNote ? '<div class="th-rp-note">' + esc(attrNote) + '</div>' : '')
      + (attrMoved.length
          ? '<div id="thReportAttr" class="th-rp-chart-sm"></div>'
          : '<div class="th-rp-note">最新两点各因子分数均无变化（或历史不足 2 点）。</div>')
      + (alerts.length
          ? '<div class="th-rp-alerts">' + alerts.slice(0, 6).map(function (a) {
              return '<div class="th-rp-alert"><b>' + esc(SEV_LABEL[a.severity] || a.severity)
                + '</b>' + esc(a.message) + '<em>' + esc(a.trade_date || '') + '</em></div>';
            }).join('') + '</div>'
          : '')
      + '</section>';

    var trendBlock = '<section class="th-rp-sec">'
      + '<h2>历史表现<span>总分柱 / 逐因子分数折线（y 轴 -2 ~ +2）</span></h2>'
      + (hist.length >= 2
          ? '<div id="thReportTrend" class="th-rp-chart"></div>'
          : '<div class="th-rp-note">历史快照不足 2 期（趋势按数据口径日归档，同一天重复刷新只覆盖不新增）。</div>')
      + '</section>';

    // 评分 vs 标的：同详情页的双轴图（_scoreChartOption 只有一份，报告里不另写口径）。
    // 数据来自 /chart（不在 /report 里），取数失败只隐藏本图并写原因，报告其余部分照常导出。
    var scoreBlock = '<section class="th-rp-sec">'
      + '<h2>评分 vs 标的<span>左轴总分 / 右轴价格 · 双轴各自自适应</span></h2>'
      + '<div id="thReportScore" class="th-rp-chart" style="height:360px"></div>'
      + '<div class="th-rp-note" id="thReportScoreNote" style="display:none"></div>'
      + '<div class="th-rp-note th-rp-caliber">口径：分数为当前定义的历史重放（内样本），'
      + '仅形状核对，不构成样本外验证。</div>'
      + '</section>';

    var tableBlock = '<section class="th-rp-sec">'
      + '<h2>数据证明<span>因子读数与标的未来收益的关联性（分档 / 相关 / 分层）</span></h2>'
      + (rows.length ? _reportTableHtml(rows, unitOf)
          : '<div class="th-rp-note">证据计算未返回（'
            + esc((rep.evidence && (rep.evidence.reason || (rep.evidence.ok === false ? '证据失败' : ''))) || '无数据')
            + '）。本报告不含证据总表，结论仅依据快照读数。</div>')
      + _reportHeatBlock(rows)
      + '</section>';

    var inval = (snap && snap.invalidation) || [];
    var warn = (snap && snap.warnings) || [];
    var riskBlock = '<section class="th-rp-sec">'
      + '<h2>失效条件与数据提示</h2>'
      + (inval.length
          ? '<ul class="th-rp-list">' + inval.map(function (r) {
              return '<li class="' + (r.hit ? 'hit' : '') + '">'
                + (r.hit ? '【已命中】' : '未命中　') + esc(r.text)
                + '（当前 ' + esc(r.current) + '）</li>';
            }).join('') + '</ul>'
          : '<div class="th-rp-note">该台账未定义失效条件。</div>')
      + (warn.length
          ? '<ul class="th-rp-list">' + warn.map(function (w) {
              return '<li>' + esc(w) + '</li>';
            }).join('') + '</ul>'
          : '')
      + '</section>';

    var caveatBlock = '<section class="th-rp-sec th-rp-caveats">'
      + '<h2>结论边界</h2><ul class="th-rp-list">'
      + caveats.map(function (c) { return '<li>' + esc(c) + '</li>'; }).join('')
      + '</ul>'
      + '<div class="th-rp-foot">仅供研究 · 不构成投资建议　·　'
      + esc(th.name || '') + '　·　生成时间 ' + esc(rep.generated_at || '')
      + '　·　数据截止 ' + esc(asof) + '</div></section>';

    return '<div class="th-rp-inner">'
      + '<header class="th-rp-head">'
      + '<div class="th-rp-title">研究台账 · ' + esc(th.name || '') + '</div>'
      + '<div class="th-rp-sub">标的：' + esc(th.asset_label || '—')
      + '　·　口径：' + esc((snap && snap.caliber_note) || th.caliber_note || '见定义')
      + '</div>'
      + '<div class="th-rp-meta">生成时间 ' + esc(rep.generated_at || '—')
      + '　·　数据截止 ' + esc(asof) + '</div>'
      + '</header>'
      + hero + attrBlock + trendBlock + scoreBlock + tableBlock + riskBlock + caveatBlock
      + '</div>';
  }

  function _reportTableHtml(rows, unitOf) {
    var DIR_CN = {
      positive: '正相关', negative: '负相关', mixed: '不稳定',
      discrete: '分组型', weak: '弱', '—': '—'
    };
    var body = rows.map(function (r) {
      // 行里可能自带 unit（后端 5.3 新增字段），缺了才回落到定义
      var unit = r.unit || unitOf[r.factor_key] || '';
      var tier = TIER_CN[r.tier] || r.tier || '—';
      var unsupported = (r.supported === false);
      var cur = (r.current_value === null || r.current_value === undefined)
        ? '—' : _fmtSigned(r.current_value, unit) + esc(unit);
      var pct = (r.percentile === null || r.percentile === undefined)
        ? '—' : Math.round(Number(r.percentile) * 100) + '%';
      var dir = DIR_CN[r.direction] || r.direction || '—';
      // 「方向」只有在真的算出相关系数与样本时才成立。corr/n 缺失时 direction 字段
      // 仍可能是 positive/negative（按阈值表声明），直接显示会把「未验证」伪装成
      // 「已验证方向」——必须显式写「—（样本不足）」并置灰。
      var hasStat = !(r.corr === null || r.corr === undefined
                      || r.n === null || r.n === undefined);
      // |r|<0.15 时后端 direction_check 明确不判方向（aligned=null）；报告表若还把
      // 阈值表声明的方向写成「正相关/负相关」，等于把「没验出来」伪装成「已验证」。
      var weak = hasStat && Math.abs(Number(r.corr)) < 0.15;
      var dirCell = unsupported
        ? '<span class="th-rp-muted">' + esc(r.reason || '不可回放') + '</span>'
        : (!hasStat
            ? '<span class="th-rp-muted">—（样本不足）</span>'
            : (weak
                ? '<span class="th-rp-muted">—（相关性弱，不判方向）</span>'
                : (r.aligned === false
                    ? '<span class="th-rp-bad">方向不一致</span>'
                    : (r.aligned === true ? esc(dir) + '（方向一致）' : esc(dir)))));
      return '<tr>'
        + '<td class="name">' + esc(r.factor_name || r.factor_key)
        + '<span class="th-rp-tier ' + esc(r.tier || '') + '">' + esc(tier) + '</span>'
        // 短窗口行必须标出来：h=5 的 r 与 h=20 的 r 不可混为一谈
        + (r.short_window
            ? '<span class="th-rp-short" title="短窗口：样本更少、未来收益重叠更严重，仅作方向参考">短窗口</span>'
            : '')
        + '</td>'
        + '<td class="num">' + cur + '</td>'
        + '<td class="num">' + pct + '</td>'
        + '<td class="num">' + (r.current_bucket ? esc(r.current_bucket) + ' ' : '')
        + _fmtPct(r.bucket_mean_return) + '</td>'
        + '<td class="num">' + (r.bucket_win_rate === null || r.bucket_win_rate === undefined
            ? '—' : Number(r.bucket_win_rate).toFixed(1) + '%')
        // 胜率是「当前桶」的胜率，分母必须是桶内 n；全样本 n 另标注，
        // 否则会出现「20% / 46」把 10 个桶样本说成 46 个的误导。
        + ' / ' + (r.bucket_n === null || r.bucket_n === undefined
            ? '—' : esc(r.bucket_n))
        + (r.n === null || r.n === undefined ? '' : '（总 ' + esc(r.n) + '）') + '</td>'
        + '<td class="num">' + (r.corr === null || r.corr === undefined ? '—' : _fmtSigned(r.corr)) + '</td>'
        + '<td class="num">' + (r.corr_score === null || r.corr_score === undefined
            ? '—' : _fmtSigned(r.corr_score)) + '</td>'
        + '<td class="num">' + (r.q5_q1 === null || r.q5_q1 === undefined
            ? '—' : _fmtSigned(r.q5_q1) + 'pp') + '</td>'
        + '<td class="dir">' + dirCell + '</td>'
        + '</tr>';
    }).join('');
    var hasShort = rows.some(function (r) { return !!r.short_window; });
    var hasBad = rows.some(function (r) { return r.aligned === false; });
    return '<table class="th-rp-table"><thead><tr>'
      + '<th>因子</th><th>当前值</th><th>历史分位</th><th>当前桶未来收益</th>'
      + '<th>当前桶胜率 / 桶 n</th><th>corr_value</th><th>corr_score</th><th>Q5−Q1</th><th>方向校验</th>'
      + '</tr></thead><tbody>' + body + '</tbody></table>'
      + (hasBad
          ? '<div class="th-rp-note">「方向不一致」= 历史读数与未来收益的相关方向，和阈值表'
            + '（高读数给正分/负分）相反；这是评分表需要复核的信号，不是买卖建议。'
            + '若 corr_score 同时为负，优先复核该因子的阈值表。</div>'
          : '')
      + (hasShort
          ? '<div class="th-rp-note">标「短窗口」的行使用更短期限（常规 20/60 日样本不足时'
            + '补算，如未来 5 个交易日）：重叠更严重、统计更不稳，仅作方向参考，'
            + '不要与常规期限的 r 直接比较。</div>'
          : '');
  }

  // 热力图取「最近 3 年」窗口：各因子的回放起点差很多（m1m2/fund_flow 从 2008 起、
  // ①② 从 2024 起），取全量并集会在左侧留一大片空白，近期「由正转负」反而被压得看不见。
  // 窗口内没有数据的因子直接跳过（下方注明），不画空行。
  function _heatWindow(rows, years) {
    years = years || 3;
    var usable = rows.filter(function (r) {
      return r.rolling && (r.rolling.dates || []).length && (r.rolling.corr_value || []).length;
    });
    var all = [], seen = {}, latest = '';
    usable.forEach(function (r) {
      r.rolling.dates.forEach(function (d) {
        var ds = String(d || '');
        if (!ds) return;
        if (ds > latest) latest = ds;
        if (!seen[ds]) { seen[ds] = 1; all.push(ds); }
      });
    });
    all.sort();
    var cutoff = '';
    if (latest) {   // 同月同日往前推 N 年（日期都是 YYYY-MM-DD 定长串，可直接比大小）
      cutoff = String(parseInt(latest.slice(0, 4), 10) - years) + latest.slice(4);
    }
    var winDates = all.filter(function (d) { return !cutoff || d >= cutoff; });
    var step = Math.max(1, Math.ceil(winDates.length / 72));
    var dates = [];
    for (var i = 0; i < winDates.length; i += step) dates.push(winDates[i]);
    if (winDates.length && dates[dates.length - 1] !== winDates[winDates.length - 1]) {
      dates.push(winDates[winDates.length - 1]);
    }
    var colOf = {};
    dates.forEach(function (d, j) { colOf[d] = j; });

    var names = [], data = [], skipped = [];
    usable.forEach(function (r) {
      var map = {}, hit = false;
      r.rolling.dates.forEach(function (d, j) {
        var ds = String(d || '');
        if (cutoff && ds < cutoff) return;
        var v = r.rolling.corr_value[j];
        if (v === null || v === undefined || !isFinite(Number(v))) return;
        map[ds] = Number(v); hit = true;
      });
      if (!hit) { skipped.push(r.factor_name || r.factor_key); return; }
      var yi = names.length;
      names.push(r.factor_name || r.factor_key);
      dates.forEach(function (d) {
        if (!(d in map)) return;
        data.push([colOf[d], yi, Math.max(-1, Math.min(1, map[d]))]);
      });
    });
    return {usable: usable, dates: dates, names: names, data: data,
            skipped: skipped, cutoff: cutoff, latest: latest};
  }

  function _reportHeatBlock(rows) {
    var w = _heatWindow(rows, 3);
    if (!w.usable.length) {
      return '<div class="th-rp-note">滚动相关序列未返回（需 report 携带 details 明细），'
        + '本报告跳过「因子 × 时间」相关性热力图。</div>';
    }
    if (!w.names.length) {
      return '<div class="th-rp-note">滚动相关序列都落在近 3 年窗口之外（'
        + esc(w.skipped.join('、')) + '），本报告不画热力图。</div>';
    }
    return '<div class="th-rp-heat-head">因子 × 时间滚动相关（近 3 年）'
      + '<span>红=正相关 / 绿=负相关；横轴为滚动窗口末端日期，窗口自 '
      + esc(w.cutoff) + ' 起</span></div>'
      + '<div id="thReportHeat" class="th-rp-chart-sm"></div>'
      + (w.skipped.length
          ? '<div class="th-rp-note">以下因子在近 3 年窗口内没有滚动相关数据，已跳过：'
            + esc(w.skipped.join('、')) + '。</div>'
          : '');
  }

  // 历史趋势（报告版）：字号更大、更宽，SVG renderer。
  function _drawReportTrend(lib, charts, rep, hist) {
    var el = document.getElementById('thReportTrend');
    if (!el || hist.length < 2) return;
    var metas = _factorMetas(hist, {snapshot: rep.snapshot});
    var dates = hist.map(function (p) { return String(p.trade_date || '').slice(5); });
    var series = [{
      name: '总分', type: 'bar', yAxisIndex: 0, barMaxWidth: 26,
      data: hist.map(function (p) {
        return {
          value: p.total,
          itemStyle: {
            // 与主视图同一套语义：正分红、负分绿，强弱用饱和度
            color: p.total >= 0
              ? (p.total >= 2 ? '#e74c3c' : 'rgba(231,76,60,.45)')
              : (p.total <= -2 ? '#176b5b' : 'rgba(23,107,91,.45)'),
            borderRadius: [3, 3, 0, 0]
          }
        };
      })
    }];
    metas.forEach(function (m, i) {
      series.push({
        name: m.name, type: 'line', yAxisIndex: 1, showSymbol: false, connectNulls: true,
        // 线要细、透明度略降：右侧因子轴是 ±2 的小刻度，粗线会盖住左侧总分柱顶
        lineStyle: {width: 1.15, opacity: 0.8, color: FACTOR_COLORS[i % FACTOR_COLORS.length]},
        itemStyle: {color: FACTOR_COLORS[i % FACTOR_COLORS.length]},
        data: hist.map(function (p) {
          var v = (p.scores || {})[m.key];
          return (v === null || v === undefined) ? null : Number(v);
        })
      });
    });
    var ch = lib.init(el, null, {renderer: 'svg'});
    charts.push(ch);
    ch.setOption({
      animation: false,
      // 图例放到**下方并允许换行**：7–8 个因子在顶部一行会被右轴裁掉（实测 ⑦ 被切）。
      grid: {left: 56, right: 60, top: 22, bottom: 96},
      legend: {
        type: 'plain', bottom: 0, left: 'center', width: '94%',
        itemWidth: 14, itemHeight: 8, itemGap: 12,
        textStyle: {fontSize: 11, color: '#6b7280'},
        data: ['总分'].concat(metas.map(function (m) { return m.name; }))
      },
      tooltip: {
        trigger: 'axis',
        confine: true,
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var p = hist[ps[0].dataIndex] || {};
          var lines = [esc(p.trade_date) + '　总分 ' + _fmtScore(p.total)];
          metas.forEach(function (m) {
            var sc = (p.scores || {})[m.key];
            if (sc === null || sc === undefined) return;
            lines.push(esc(m.name) + '：' + _fmtScore(sc) + ' 分');
          });
          return lines.join('<br>');
        }
      },
      xAxis: {type: 'category', data: dates,
              axisLabel: {fontSize: 12, color: '#6b7280'}},
      yAxis: [
        {type: 'value', name: '总分', nameTextStyle: {fontSize: 12, color: '#6b7280'},
         axisLabel: {fontSize: 12, color: '#6b7280'},
         splitLine: {lineStyle: {color: 'rgba(128,128,128,.15)'}}},
        {type: 'value', name: '因子分', min: -2, max: 2, interval: 1,
         nameTextStyle: {fontSize: 12, color: '#6b7280'},
         axisLabel: {fontSize: 12, color: '#6b7280'}, splitLine: {show: false}}
      ],
      series: series
    });
  }

  function _drawReportAttr(lib, charts, attrRows) {
    var el = document.getElementById('thReportAttr');
    if (!el) return;
    var rows = attrRows.filter(function (r) {
      return r.delta !== null && r.delta !== undefined && Number(r.delta) !== 0;
    }).sort(function (a, b) {
      return Math.abs(a.delta) - Math.abs(b.delta);
    });
    if (!rows.length) return;
    var ch = lib.init(el, null, {renderer: 'svg'});
    charts.push(ch);
    ch.setOption({
      animation: false,
      grid: {left: 190, right: 60, top: 12, bottom: 26},
      tooltip: {trigger: 'item', formatter: function (p) {
        if (!p || p.dataIndex === undefined) return '';
        var r = rows[p.dataIndex] || {};
        return esc(r.name) + '<br>Δ分 ' + _fmtScore(r.delta);
      }},
      xAxis: {type: 'value', axisLabel: {fontSize: 12, color: '#6b7280'},
              splitLine: {lineStyle: {color: 'rgba(128,128,128,.15)'}}},
      yAxis: {type: 'category', data: rows.map(function (r) { return r.name || r.key; }),
              axisLabel: {fontSize: 12, color: '#1c1c1a', width: 180, overflow: 'truncate'},
              axisTick: {show: false}},
      series: [{
        type: 'bar', barMaxWidth: 18,
        data: rows.map(function (r) {
          return {value: Number(r.delta),
                  itemStyle: {color: r.delta > 0 ? '#e74c3c' : '#176b5b', borderRadius: 3}};
        }),
        label: {show: true, position: 'right', fontSize: 12, color: '#6b7280',
                formatter: function (p) { return _fmtScore(p.value); }}
      }]
    });
  }

  function _drawReportHeat(lib, charts, rows) {
    var el = document.getElementById('thReportHeat');
    if (!el) return;
    // 与 _reportHeatBlock 用同一套窗口逻辑（近 3 年 + 跳过窗口内无数据的因子）
    var w = _heatWindow(rows, 3);
    if (!w.names.length) return;
    var dates = w.dates, names = w.names, data = w.data;
    var ch = lib.init(el, null, {renderer: 'svg'});
    charts.push(ch);
    ch.setOption({
      animation: false,
      grid: {left: 190, right: 90, top: 12, bottom: 56},
      tooltip: {trigger: 'item', formatter: function (p) {
        if (!p || !p.value) return '';
        return esc(names[p.value[1]] || '') + '<br>' + esc(dates[p.value[0]] || '')
          + '　corr ' + _fmtSigned(p.value[2]);
      }},
      xAxis: {type: 'category', data: dates,
              axisLabel: {fontSize: 10, color: '#6b7280', interval: Math.max(0, Math.floor(dates.length / 8))},
              axisTick: {show: false}},
      yAxis: {type: 'category', data: names,
              axisLabel: {fontSize: 12, color: '#1c1c1a', width: 180, overflow: 'truncate'},
              axisTick: {show: false}},
      visualMap: {
        min: -1, max: 1, calculable: false, orient: 'horizontal',
        left: 'center', bottom: 0, itemHeight: 90, itemWidth: 10,
        textStyle: {fontSize: 11, color: '#6b7280'},
        inRange: {color: ['#176b5b', '#f3f4f6', '#e74c3c']}
      },
      series: [{
        type: 'heatmap', data: data,
        itemStyle: {borderColor: '#ffffff', borderWidth: 1},
        emphasis: {itemStyle: {borderColor: '#1c1c1a', borderWidth: 1}}
      }]
    });
  }

  // 报告里的「评分 vs 标的」双轴图：图配置与详情页共用 `_scoreChartOption`（口径只有一份）。
  // 必须用 SVG renderer：html-to-image 对 canvas 支持很差（与其余三张报告图一致）。
  // /chart 404、样本不足、渲染抛错都只隐藏这一节并写原因——报告其余部分照常导出。
  async function _drawReportScore(lib, charts, rep) {
    var el = document.getElementById('thReportScore');
    var note = document.getElementById('thReportScoreNote');
    function hide(msg) {
      if (el) el.style.display = 'none';
      if (note) { note.style.display = ''; note.textContent = msg; }
    }
    // 报告 payload 的 thesis 字段是 `id`（不是 thesis_id）：三级兜底，缺了就不画
    var tid = (rep && rep.thesis && (rep.thesis.thesis_id || rep.thesis.id))
      || (rep && rep.thesis_id) || _active;
    if (!tid) { hide('报告未带台账标识，双轴图已跳过（其余内容不受影响）。'); return; }
    var res = await _fetchScoreChart(tid, false);
    if (!res.ok) {
      hide('双轴图未生成：' + (res.msg || '图表服务未就绪') + '（报告其余部分不受影响）');
      return;
    }
    if (!el) return;
    try {
      var ch = lib.init(el, null, {renderer: 'svg'});
      charts.push(ch);           // 先登记再 setOption：抛错也能被 finally 释放
      var opt = _scoreChartOption(res.data, false);
      delete opt.dataZoom;       // 静态 PNG 不需要缩放交互，少一层布局计算
      ch.setOption(opt);
    } catch (e) {
      hide('双轴图渲染失败：' + ((e && e.message) || e) + '（报告其余部分不受影响）');
      return;
    }
    el.style.display = '';
    if (note) { note.style.display = 'none'; note.textContent = ''; }
  }

  function _afterLayout() {
    return new Promise(function (resolve) {
      var raf = (typeof window !== 'undefined' && window.requestAnimationFrame)
        ? window.requestAnimationFrame.bind(window)
        : function (fn) { return setTimeout(fn, 16); };
      raf(function () { raf(function () { setTimeout(resolve, 30); }); });
    });
  }

  async function _renderReportPng(lib, rep) {
    var exporter = window.StockRadar && window.StockRadar.chatExport;
    if (!exporter || typeof exporter.elementToPngBlob !== 'function') {
      throw new Error('图片导出模块缺失（chat-export.js 未加载）');
    }
    var host = document.createElement('div');
    host.className = 'th-report';
    host.id = 'thReportRoot';
    // 注意：不用负坐标离屏（见本节头注释）；z-index 压底 + pointer-events:none 即可不可见
    host.style.cssText = 'position:fixed;left:0;top:0;width:1240px;background:#ffffff;'
      + 'z-index:-9999;pointer-events:none;';
    host.innerHTML = _reportHtml(rep);
    document.body.appendChild(host);

    var charts = [];
    try {
      var hist = (rep.history && rep.history.items) || [];
      _drawReportTrend(lib, charts, rep, hist);
      var rows = _reportRows(rep);
      var att = rep.attribution || null;
      var attrRows = (att && att.rows) ? att.rows : [];
      if (!attrRows.length) {
        var calc = _attribution(hist, rep.snapshot || null);
        if (calc.ok) attrRows = calc.rows;
      }
      _drawReportAttr(lib, charts, attrRows);
      _drawReportHeat(lib, charts, rows);
      // 双轴图必须在 _afterLayout/导出之前 init+setOption 完成，否则 PNG 里是空容器
      await _drawReportScore(lib, charts, rep);
      await _afterLayout();
      var w = host.scrollWidth || 1240;
      var h = host.scrollHeight || host.offsetHeight || 0;
      var ratio = 2;
      var MAX_SIDE = 8000;   // 与 chat-export 的 canvas 单边保护同量级
      if (h && h * ratio > MAX_SIDE) ratio = Math.max(1, MAX_SIDE / h);
      return await exporter.elementToPngBlob(host, {
        pixelRatio: ratio, width: w, height: h || undefined, backgroundColor: '#ffffff'
      });
    } finally {
      charts.forEach(function (c) { try { c.dispose(); } catch (e) { /* 忽略 */ } });
      if (host.parentNode) host.parentNode.removeChild(host);
    }
  }

  function _showReportPreview(blob, rep) {
    _closeReportPreview();
    var url = URL.createObjectURL(blob);
    var name = _reportFileName(rep);
    var mask = document.createElement('div');
    mask.className = 'th-report-modal';
    mask.innerHTML = '<div class="th-report-box">'
      + '<div class="th-report-bar"><b>研究报告图</b>'
      + '<span class="th-report-fn">' + esc(name) + '</span>'
      + '<button type="button" class="th-btn" data-act="copy">复制图片</button>'
      + '<button type="button" class="th-btn th-primary" data-act="dl">下载 PNG</button>'
      + '<button type="button" class="th-btn" data-act="regen">重新生成</button>'
      + '<button type="button" class="th-btn" data-act="close">关闭</button></div>'
      + '<div class="th-report-imgwrap"><img alt="研究台账报告图片" src="' + url + '"></div>'
      + '</div>';
    document.body.appendChild(mask);
    _reportModal = {el: mask, url: url};
    mask.addEventListener('click', function (e) {
      var act = (e.target && e.target.closest) ? e.target.closest('[data-act]') : null;
      if (act && act.dataset.act === 'copy') {
        _copyReportImage(blob);
        return;
      }
      if (act && act.dataset.act === 'dl') {
        var dl = window.StockRadar.chatExport.downloadBlob;
        try { dl(blob, name); }
        catch (err) { _toast('下载失败：' + ((err && err.message) || '浏览器拒绝'), 'error'); }
        return;
      }
      if (act && act.dataset.act === 'regen') {
        _closeReportPreview();
        generateReport(true);
        return;
      }
      if ((act && act.dataset.act === 'close') || e.target === mask) _closeReportPreview();
    });
  }

  // 复制 PNG 到剪贴板：用户可直接在微信/文档/聊天框里 Ctrl+V。
  // 浏览器要求安全上下文（localhost/https）+ 用户手势，失败时明确提示改用下载。
  async function _copyReportImage(blob) {
    if (!blob) { _toast('没有可复制的图片', 'error'); return; }
    if (!navigator.clipboard || !navigator.clipboard.write
        || typeof ClipboardItem === 'undefined') {
      _toast('当前浏览器不支持复制图片，请用「下载 PNG」', 'error');
      return;
    }
    try {
      await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
      _toast('已复制报告图片，可直接粘贴', 'info');
    } catch (e) {
      var msg = (e && e.name === 'NotAllowedError')
        ? '浏览器拒绝了剪贴板权限（请允许后重试，或用下载 PNG）'
        : ((e && e.message) || '浏览器拒绝');
      _toast('复制图片失败：' + msg, 'error');
    }
  }

  function _closeReportPreview() {
    if (!_reportModal) return;
    if (_reportModal.el && _reportModal.el.parentNode) {
      _reportModal.el.parentNode.removeChild(_reportModal.el);
    }
    try { URL.revokeObjectURL(_reportModal.url); } catch (e) { /* 忽略 */ }
    _reportModal = null;
  }

  async function generateReport(force) {
    // 兼容旧调用/事件对象：只有严格 true 才强制重算，避免 Event 被当成 true
    force = (force === true);
    if (_reportBusy) return;
    if (!_active || _active === PORTFOLIO_ID) {
      _toast('组合层暂不支持生成报告，请先选择单专项台账', 'info');
      return;
    }
    var id = _active;

    // 前端缓存：同一张台账 30 分钟内直接复用 PNG，不重画图表也不重编码。
    var cached = _reportCache[id];
    if (!force && cached && (Date.now() - cached.at) <= REPORT_CACHE_TTL_MS) {
      var mins = Math.max(1, Math.round((Date.now() - cached.at) / 60000));
      setStatus('报告已生成（' + mins + ' 分钟前的缓存）');
      _toast('使用 30 分钟内的报告缓存；需要最新数据请点「重新生成」', 'info');
      _showReportPreview(cached.blob, cached.rep);
      return;
    }
    if (force) delete _reportCache[id];

    var btn = $('thReportBtn');
    _reportBusy = true;
    _setReportBtn(btn, true);
    setStatus(force ? '重新生成报告中（强制重算，约 20–60 秒）…'
                    : '生成报告中（首次含证据计算，约 20–60 秒）…');
    _toast(force ? '正在强制重新生成报告，约 20–60 秒…'
                 : '正在生成报告：首次含证据计算，约 20–60 秒…', 'info');
    try {
      var url = '/api/thesis/' + encodeURIComponent(id) + '/report' + (force ? '?force=1' : '');
      var resp = await fetch(url);
      var json = await resp.json();
      if (!json || json.code !== 0) throw new Error((json && json.msg) || '报告接口返回失败');
      var rep = json.data || {};
      var echartsLib = await ensureECharts();
      if (!echartsLib) throw new Error('图表库加载失败（离线环境），无法渲染报告图表');
      var blob = await _renderReportPng(echartsLib, rep);
      if (!blob) throw new Error('图片生成失败（html-to-image 返回空）');
      _reportCache[id] = {blob: blob, rep: rep, at: Date.now()};
      setStatus('报告已生成' + (rep.cache && rep.cache.hit ? '（后端缓存）' : ''));
      _showReportPreview(blob, rep);
    } catch (e) {
      var msg = (e && e.message) || '未知错误';
      setStatus('报告生成失败：' + msg);
      // 离线时 html-to-image 加载失败会走到这里：必须明确提示，不能白屏
      _toast('报告生成失败：' + msg, 'error');
    } finally {
      _reportBusy = false;
      _setReportBtn(btn, false);
    }
  }

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.thesis = {
    openPage: openPage,
    closePage: closePage,
    load: load,
    loadDetail: loadDetail,
    refreshActive: refreshActive,
    toggleEvidence: _toggleEvidence,
    setScatterMode: _setEvScatterMode,
    generateReport: generateReport,
    // 双轴图（总览面板 / 详情大图）：供页面交互与渲染测试直接调用
    openScorePanel: openScorePanel,
    closeScorePanel: closeScorePanel,
    // 供静态/渲染测试与其它模块复用（不参与页面交互）
    _attribution: _attribution,
    _rangeBarHtml: _rangeBarHtml,
    _sparklineHtml: _sparklineHtml
  };
})();
