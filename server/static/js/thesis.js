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

  var ECHARTS_CDN = 'https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js';
  var SEV_LABEL = { critical: '严重', warn: '注意', info: '提示' };
  var SCORE_CLASS = { '2': 's2', '1': 's1', '0': 's0', '-1': 'sm', '-2': 'sm' };
  var STATUS_COLOR = {
    '很有利': 'var(--up, #c0392b)', '有利': 'var(--brand, #176b5b)',
    '中性': 'var(--warn, #c88a1e)', '偏不利': 'var(--down, #1e8449)',
    '不利': 'var(--down, #1e8449)', '数据缺失': 'var(--border-main, #8a97a8)'
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
  var _chart = null;
  var _busy = false;
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
    var chat = document.querySelector('.chat-pane');
    if (chat) chat.style.display = '';
  }

  function bindEvents() {
    if (_bound) return;
    _bound = true;
    $('thRefreshBtn')?.addEventListener('click', refreshActive);
    // 因子有效性汇总按钮由 renderDetail 动态生成（每次切台账都会重建），
    // 故用事件委托而不是逐个绑定——否则切换台账后按钮就失效了。
    document.addEventListener('click', function (e) {
      var b = e.target && e.target.closest && e.target.closest('#thEvAllBtn');
      if (b) _loadEvidenceBatch(b.dataset.tid, b);
    });
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
    try {
      var resp = await fetch('/api/thesis/' + encodeURIComponent(id));
      var json = await resp.json();
      if (json.code !== 0) throw new Error(json.msg || '详情读取失败');
      renderDetail(json.data);
      setStatus('共 ' + _items.length + ' 个台账');
      await renderChart(id);
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
    if (_chart) { try { _chart.dispose(); } catch (e) { /* 忽略 */ } _chart = null; }
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
      + '用于复核定义 basis 里写死的「波动是 2.1 倍 / 降 17%」是否仍成立）。</div>'
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

  function renderDetail(d) {
    if (!d) return;
    var snap = d.snapshot;
    var main = $('thMain');
    if (!main) return;

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
      + '<span class="th-verdict" style="color:' + (STATUS_COLOR[snap.verdict] || 'var(--text,#1c2430)')
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
      + _stagesHtml(snap.stages)
      + _phasesHtml(snap.phases)
      + '<div class="th-rows">'
      + '<div class="th-row th-row-factors"><div class="th-card">'
      + '<div class="th-card-head">因子读数 <span class="th-sub">评分 -2 不利 ~ +2 很有利</span></div>'
      + '<div class="th-scroll"><div class="th-factors">' + _factorsHtml(snap.factors, snap.thesis_id) + '</div>'
      + _warnHtml(snap.warnings) + '</div></div></div>'
      + '<div class="th-row th-row-bottom">'
      + '<div class="th-card"><div class="th-card-head">历史趋势 <span class="th-sub">总分 / 数据口径日期</span></div>'
      + '<div class="th-scroll"><div id="thChart" class="th-chart"></div>'
              + '<div class="th-empty" id="thChartNote" style="display:none">目前只有 1 个历史点。趋势按「数据口径日」归档，同一天重复刷新只覆盖不新增；等下一交易日的数据出来再刷新即可看到趋势。</div></div></div>'
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
      + '<span class="th-sub">批量校验全部因子（约 20 秒）</span>'
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
    box.innerHTML = '<div class="th-sec-note">正在回放各因子历史序列并计算（首次较慢，约 20 秒）…</div>';
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

  function _factorsHtml(factors, thesisId) {
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
      return '<div class="th-factor' + (isManual ? ' is-manual' : '') + '">'
        + '<div class="th-factor-head"><span class="th-factor-name">' + esc(f.name)
        + (isManual
            ? '<span class="th-badge-manual" title="该因子暂无自动数据源，由人工维护；分数照常计入总分">手工维护</span>'
            : '')
        + '</span>'
        + '<span class="th-factor-status">' + esc(f.status || '') + '</span>'
        + '<span class="th-score ' + cls + '">' + esc(scoreText) + '</span></div>'
        + '<div class="th-factor-value">' + esc(val)
        + '<span class="unit">' + esc(unit) + '</span></div>'
        + _pctBand(f)
        + '<div class="th-factor-detail">' + esc(f.detail) + '</div>'
        + _confHtml(f.confidence)
        + '<div class="th-factor-src">来源：' + esc(f.source) + '<br>数据日期：' + esc(f.asof)
        + (f.stale ? '　<span class="th-flag">数据可能滞后</span>' : '')
        + (f.note ? '<br>' + esc(f.note) : '') + '</div>'
        + '<div class="th-factor-ev">'
        + '<button type="button" class="th-ev-btn" onclick="StockRadar.thesis.toggleEvidence(\''
        + (thesisId || '') + '\',\'' + esc(f.key) + '\', this)">▸ 数据验证</button>'
        + '<div id="ev-' + esc(f.key) + '" class="th-ev-box" style="display:none"></div>'
        + '</div>'
        + '</div>';
    }).join('');
  }

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

  // ── 趋势图 ──────────────────────────────────────────────────────

  async function renderChart(id) {
    var el = $('thChart');
    if (!el) return;
    var points = [];
    try {
      var resp = await fetch('/api/thesis/' + encodeURIComponent(id) + '/history?days=180');
      var json = await resp.json();
      if (json.code === 0) points = (json.data && json.data.items) || [];
    } catch (e) { points = []; }

    // 容器在重新渲染后会换掉旧的 canvas，先释放再重建，避免 echarts 警告与内存堆积
    if (_chart) { try { _chart.dispose(); } catch (e) { /* 忽略 */ } _chart = null; }

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
    var echartsLib = await ensureECharts();
    if (!echartsLib) {
      if (note) { note.style.display = ''; note.textContent = '图表库加载失败（离线环境）。'; }
      return;
    }
    _chart = echartsLib.init(el);
    _chart.setOption({
      grid: { left: 42, right: 14, top: 14, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        formatter: function (ps) {
          var p = points[ps[0].dataIndex] || {};
          return p.trade_date + '<br>总分 ' + (p.total > 0 ? '+' + p.total : p.total)
            + '　' + (p.verdict || '') + '<br>有效因子 ' + p.valid_count + '/' + p.factor_count;
        }
      },
      xAxis: {
        type: 'category',
        data: points.map(function (p) { return p.trade_date.slice(5); }),
        axisLabel: { fontSize: 10, color: '#8a97a8' }
      },
      yAxis: {
        type: 'value',
        name: '总分',
        nameTextStyle: { fontSize: 10, color: '#8a97a8' },
        axisLabel: { fontSize: 10, color: '#8a97a8' },
        splitLine: { lineStyle: { color: 'rgba(128,128,128,.15)' } }
      },
      series: [{
        type: 'bar',
        barMaxWidth: 26,
        data: points.map(function (p) {
          return {
            value: p.total,
            itemStyle: {
              // 红=有利 / 绿=不利（A 股惯例），与页面得分色阶一致
              color: p.total >= 2 ? '#c0392b' : (p.total <= -2 ? '#1e8449'
                : (p.total >= 0 ? '#176b5b' : '#c88a1e')),
              borderRadius: [3, 3, 0, 0]
            }
          };
        }),
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { color: 'rgba(128,128,128,.45)', type: 'dashed' },
          data: [{ yAxis: 0 }]
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
      await load(_active);
    } catch (e) {
      setStatus('刷新失败：' + e.message);
    } finally {
      _busy = false;
      if (btn) { btn.disabled = false; btn.textContent = '↻ 刷新'; }
    }
  }

  // ── 因子「数据验证」折叠区：关联性回测的证据链 ──────────────────────
  // 不做假图：复合因子（双轴/相对/手工）与样本不足的，显示原因而不是硬画。

  function _toggleEvidence(thesisId, factorKey, btn) {
    var box = document.getElementById('ev-' + factorKey);
    if (!box) return;
    var open = box.style.display === 'block';
    box.style.display = open ? 'none' : 'block';
    btn.textContent = open ? '▸ 数据验证' : '▾ 数据验证';
    if (open || box.dataset.loaded === '1') return;
    box.innerHTML = '<div class="th-ev-off">加载中…</div>';
    fetch('/api/thesis/' + encodeURIComponent(thesisId)
          + '/evidence?factor=' + encodeURIComponent(factorKey))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var d = j && j.data;
        if (!d) { box.innerHTML = ''; return; }
        if (!d.supported) {
          box.innerHTML = '<div class="th-ev-off">无时序验证数据：' + esc(d.reason || '')
            + '（不做假图）</div>';
          box.dataset.loaded = '1';
          return;
        }
        var fmt = function (v, unit) {
          return (v === undefined) ? '—' : (v > 0 ? '+' : '') + v + (unit || '');
        };
        var rows = (d.horizons || []).map(function (h) {
          return '<tr><td>未来 ' + h.h + ' 个交易日</td><td class="c">' + h.n + '</td>'
            + '<td class="c">' + (h.corr === undefined ? '—' : fmt(h.corr)) + '</td>'
            + '<td class="c">' + (h.q5_q1 === undefined ? '—' : fmt(h.q5_q1, 'pp')) + '</td></tr>';
        }).join('');
        box.innerHTML = '<div class="th-ev">'
          + '<div class="th-ev-verdict">' + esc(d.verdict || '') + '</div>'
          + '<div class="th-ev-title">关联性回测 — ' + esc(d.target.name)
          + ' · 因子序列自 ' + esc(d.series_start) + '</div>'
          + '<table class="th-rows-table"><tr><th>期限</th><th>样本(日)</th>'
          + '<th>相关系数</th><th>Q5−Q1 分层差</th></tr>' + rows + '</table>'
          + '<div id="evc-' + factorKey + '" class="th-ev-chart"></div>'
          + '<div class="th-ev-note">散点：X=因子值，Y=未来 20 个交易日收益（%）——'
          + '点的走向与疏密就是「这个因子有没有用」的原始证据</div></div>';
        box.dataset.loaded = '1';
        var host = document.getElementById('evc-' + factorKey);
        if (host && window.echarts && d.scatter && d.scatter.length) {
          var ch = echarts.init(host);
          ch.setOption({
            grid: {left: 56, right: 14, top: 14, bottom: 34},
            xAxis: {type: 'value', name: '因子值', scale: true,
                    nameTextStyle: {fontSize: 10}},
            yAxis: {type: 'value', name: '未来20日 %', scale: true,
                    nameTextStyle: {fontSize: 10}},
            series: [{type: 'scatter', symbolSize: 4, data: d.scatter,
                      itemStyle: {opacity: 0.45}}],
            tooltip: {trigger: 'item', formatter: function (pp) {
              return '因子 ' + pp.value[0] + '<br>未来20日 ' + pp.value[1] + '%';
            }}
          });
        }
      })
      .catch(function () { box.innerHTML = '<div class="th-ev-off">加载失败</div>'; });
  }

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.thesis = {
    openPage: openPage,
    closePage: closePage,
    load: load,
    loadDetail: loadDetail,
    refreshActive: refreshActive,
    toggleEvidence: _toggleEvidence
  };
})();
