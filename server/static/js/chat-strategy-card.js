/* ─────────────────────────────────────────────
   选股雷达 Web — 策略会话卡片渲染（conversational strategy loop）
   ───────────────────────────────────────────── */

/* 把后端 strategy-card 代码块渲染成 DOM。
   标记与字段契约见 agents/shared/strategy_card.py（后端为唯一事实源）：
   render_card_block() 输出「三反引号 + strategy-card + JSON + 三反引号」的围栏块，
   经 marked 解析后即 <pre><code class="language-strategy-card">JSON</code></pre>。

   渲染挂点（不侵入 chat-core.js）：chat-core.js::setBubbleContent() 在 renderMd 之后
   调用 chatMarkdown._highlightCodeBlocks(mdDiv)。本文件包装该函数，抢在 hljs 高亮之前
   把卡片块替换成 DOM —— 既命中所有对话渲染路径（appendMessage / 流式收尾 / 历史回填），
   也避免 hljs 对未知语言 language-strategy-card 的告警与二次改写。
   识别方式对齐 chat-markdown.js:74 的 mermaid 先例（按 language-xxx 类名判语言）。 */

(function () {
  'use strict';

  var CARD_MARKER = 'strategy-card';
  var LANG_CLASS = 'language-' + CARD_MARKER;
  var HOOK_FLAG = '__strategyCardHooked';

  var HEALTH_LABEL = {
    ok: '通过',
    no_signal: '无信号',
    low_sample: '样本不足',
    insufficient_bars: 'K线不足',
    data_gap: '数据缺口',
    bad_strategy: '策略不可回测',
    invalid_params: '参数不成立',
    error: '回测出错',
  };

  function pct(v) {
    var n = Number(v);
    if (v === null || v === undefined || v === '' || !isFinite(n)) return '暂无';
    return (Math.round(n * 1000) / 10) + '%';
  }


  /* 胜率专用：存量 2026-07 那批结果存的是百分数刻度（9.0 = 9%），引擎现口径是小数。
     不归一就会把 9% 显示成 900%（后端 /batches/{id}/validation 已同样归一）。 */
  function pctWin(v) {
    var n = Number(v);
    if (v === null || v === undefined || v === '' || !isFinite(n)) return '暂无';
    return pct(n > 1 ? n / 100 : n);
  }

  function num(v, d) {
    var n = Number(v);
    if (v === null || v === undefined || v === '' || !isFinite(n)) return '暂无';
    var p = Math.pow(10, d === undefined ? 2 : d);
    return String(Math.round(n * p) / p);
  }

  function int(v, fallback) {
    var n = Number(v);
    if (v === null || v === undefined || v === '' || !isFinite(n)) return fallback;
    return String(Math.round(n));
  }

  /* 成交口径文案：枚举与 agents/shared/strategy_card.py / strategy_base.py 一致。
     entry 与 exit 都由后端给出解析后的值（exit 空串已在后端换算成跟随 entry），
     这里只翻译；两个都缺时返回空串，宁可整行不渲染，也不硬猜一个口径。 */
  var TIMING_LABEL = { next_open: '次日开盘', same_close: '尾盘', next_close: '次日收盘' };
  function timingLabel(entry, exitT) {
    var e = TIMING_LABEL[String(entry || '').toLowerCase()] || '';
    var x = TIMING_LABEL[String(exitT || '').toLowerCase()] || '';
    if (!e && !x) return '';
    if (e && x) return e + '买入 → ' + x + '卖出';
    return e ? e + '买入' : x + '卖出';
  }

  /* 对话正文里的卡片（agents/shared/strategy_card.py 产出）只带买入侧口径，
     卖出侧要按 run_uuid 从任务详情补（GET /runs/{uuid} 的顶层 entry_timing/exit_timing）。
     消息区每次新增消息都会整段重渲染，所以结果按 run_uuid 缓存、并发请求去重。 */
  var timingDetailCache = {};
  var timingDetailPending = {};

  function fetchRunTiming(runUuid, done) {
    if (!runUuid || typeof fetch !== 'function') return;
    if (timingDetailCache[runUuid]) { done(timingDetailCache[runUuid]); return; }
    if (timingDetailPending[runUuid]) return;
    timingDetailPending[runUuid] = true;
    fetch('/api/strategy-research/runs/' + encodeURIComponent(runUuid),
          { headers: { Accept: 'application/json' } })
      .then(function (r) { return r && r.ok ? r.json() : null; })
      .then(function (res) {
        delete timingDetailPending[runUuid];
        var d = (res && res.data) || {};
        if (!d.entry_timing && !d.exit_timing) return;
        timingDetailCache[runUuid] = d;
        done(d);
      })
      .catch(function () { delete timingDetailPending[runUuid]; });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function renderNotes(notes) {
    var box = el('div', 'sc-notes');
    var list = Array.isArray(notes) ? notes : [];
    list.forEach(function (t) {
      if (t === null || t === undefined || t === '') return;
      box.appendChild(el('div', 'sc-note', t));
    });
    return box;
  }

  function renderMetrics(kind, m) {
    var box = el('div', 'sc-metrics sc-' + kind);
    box.appendChild(el('div', 'sc-metrics-title', kind === 'oos' ? '样本外 OOS' : '样本内 IS'));
    // 两个收益口径分开写清楚：年化＝平均每年赚多少；区间总收益＝这段回测整体赚多少
    var rows = [['夏普', num(m.sharpe)],
                ['年化收益', pct(m.cagr)],
                ['区间总收益', pct(m.total_return)],
                ['最大回撤', pct(m.mdd)],
                ['胜率', pct(m.win_rate)],
                ['成交笔数', num(m.trades, 0)]];
    rows.forEach(function (r) {
      var row = el('div', 'sc-metric');
      row.appendChild(el('span', 'sc-metric-k', r[0]));
      row.appendChild(el('span', 'sc-metric-v', r[1]));
      box.appendChild(row);
    });
    return box;
  }

  /* 按钮点击：只把文本填进输入框，**不自动发送**（用户 2026-10-12 拍板）。
     理由：像「再加一个条件：」这种半句话，得让用户自己把条件补完再按回车，
     直接发出去等于替用户决定了条件是什么。与 index.html 示例问题 chip 的既有做法一致。
     填入后派发 input 事件，让输入框自适应高度与模板路由预览照常生效。 */
  function sendOrFill(text) {
    var input = document.getElementById('messageInput');
    if (!input) return;
    input.value = text;
    if (typeof input.focus === 'function') input.focus();
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /* 直接跳模块（联动）：有对应模块就跳过去，模块不在（页面版本不一致/未加载）就退回填输入框。
     nav 取值：kline=回测模块的 K 线视图（含买卖点）/ lifecycle=策略一生 / research=策略研究面板 */
  function navigate(nav, payload) {
    var p = payload || {};
    if (nav === 'kline') {
      var runId = p.oos_run_id || p.is_run_id || '';
      if (runId && window._btPanel && typeof window._btPanel.viewRun === 'function') {
        window._btPanel.viewRun(runId);
        return true;
      }
    } else if (nav === 'lifecycle') {
      if (p.strategy_id && window._btPanel && typeof window._btPanel.openLifecycle === 'function') {
        window._btPanel.openLifecycle(p.strategy_id);
        return true;
      }
    } else if (nav === 'research') {
      var rp = window.StockRadar && window.StockRadar.strategyResearchPanel;
      if (rp && typeof rp.openPage === 'function') {
        rp.openPage(p.run_uuid || p.run_id || '');
        return true;
      }
      if (rp && typeof rp.open === 'function') { rp.open(); return true; }
    }
    return false;
  }

  /* ── 结果卡：4 张 KPI（对齐原型 2026-10-11 v1「结果卡」）──────────────
     原型是「总收益 / 年化 / 胜率 / 最大回撤」四张大卡，主值取样本外（结论口径），
     副行给对照（样本内 / 基准 / 胜负笔数）。原实现是两段竖排 6 行指标，读起来像
     两张表而不是一张结论，跟原型对不上。 */
  function renderKpiGrid(payload) {
    var box = el('div', 'sc-kpis');
    var oos = payload.oos || {};
    var is = payload.is || {};
    var trades = num(oos.trades);
    var wr = num(oos.win_rate);
    var sub = '';
    if (trades !== null && wr !== null) {
      var wins = Math.round(trades * wr);
      sub = wins + '胜 / ' + Math.max(0, trades - wins) + '负';
    }
    var bm = num(payload.benchmark_return);
    var cards = [
      ['总收益', pct(oos.total_return),
        bm !== null ? '同期基准 ' + pct(bm) : ('样本内 ' + pct(is.total_return)),
        num(oos.total_return)],
      ['年化收益', pct(oos.cagr), '样本内 ' + pct(is.cagr), num(oos.cagr)],
      ['胜率', pct(oos.win_rate), sub || '样本内 ' + pct(is.win_rate), num(oos.win_rate)],
      ['最大回撤', pct(oos.mdd), '样本内 ' + pct(is.mdd), -Math.abs(num(oos.mdd) || 0)]
    ];
    cards.forEach(function (c) {
      var cell = el('div', 'sc-kpi');
      cell.appendChild(el('div', 'sc-kpi-k', c[0]));
      var v = el('div', 'sc-kpi-v', c[1]);
      if (c[3] !== null && isFinite(c[3])) v.className += c[3] >= 0 ? ' pos' : ' neg';
      cell.appendChild(v);
      cell.appendChild(el('div', 'sc-kpi-s', c[2]));
      box.appendChild(cell);
    });
    return box;
  }

  /* IS / OOS 对照表：同一组口径上下两列，才看得出「样本内好、样本外垮」。
     类名用 sc-isos-*：sc-compare-table 是「换标的对照卡」的既有样式，复用会互相覆盖。 */
  function renderCompareTable(payload) {
    var table = el('table', 'sc-isos-table');
    var oos = payload.oos || {};
    var is = payload.is || {};
    var thead = el('thead');
    var htr = el('tr');
    ['口径', '样本内 IS（前 70%）', '样本外 OOS（后 30%）'].forEach(function (t) {
      htr.appendChild(el('th', null, t));
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    var rows = [
      ['夏普比率', num(is.sharpe), num(oos.sharpe)],
      ['年化收益', pct(is.cagr), pct(oos.cagr)],
      ['交易笔数', num(is.trades, 0), num(oos.trades, 0)],
      ['胜率', pct(is.win_rate), pct(oos.win_rate)]
    ];
    var tbody = el('tbody');
    rows.forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', null, r[0]));
      tr.appendChild(el('td', null, r[1]));
      var td = el('td', 'sc-isos-oos', r[2]);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  function renderActions(actions, payload) {
    var box = el('div', 'sc-actions');
    var list = Array.isArray(actions) ? actions : [];
    list.forEach(function (a) {
      if (!a) return;
      var label = a.label === undefined || a.label === null ? '' : String(a.label);
      if (!label) return;
      /* fill = 填进输入框的文本；按钮标题保持短，fill 可以是让用户接着补的半句话 */
      var fill = a.fill === undefined || a.fill === null || a.fill === '' ? label : String(a.fill);
      var nav = a.nav === undefined || a.nav === null ? '' : String(a.nav);
      var btn = el('button', 'sc-btn', label);
      btn.type = 'button';
      btn.title = nav ? '直接打开对应模块（打不开时退回填入输入框）'
                      : '填入输入框（不会自动发送，你可以接着补充）';
      btn.setAttribute('data-act', a.id === undefined || a.id === null ? '' : String(a.id));
      btn.addEventListener('click', function () {
        if (nav && navigate(nav, payload)) return;
        sendOrFill(fill);
      });
      box.appendChild(btn);
    });
    return box;
  }

  /* ── 进度卡：阶段条 + 轮次时间线 ────────────────────────────────
     设计 2026-10-17：原型顶部是 stepper（澄清→口径→研发→结果），对话里也要能
     一眼看出「现在在哪一段」，而不只是一根百分比条。阶段字段由后端
     build_progress_card 提供（phase / phase_steps / phase_index）。

     注意：类名 sc-phase-* / sc-round-* 必须全局唯一：同文件里 .sc-progress 既表示
     卡片变体又表示文本行，靠后代选择器区分——新类不要重复这个坑。 */
  var PHASE_DEFAULT = ['确认口径', '迭代研发', '结果'];
  var ROUND_MAX_ROWS = 6;      /* 卡片里最多展示最近 6 轮，其余折叠到面板 */

  function phaseIndexOf(payload) {
    var idx = Number(payload && payload.phase_index);
    if (isFinite(idx) && idx >= 0) return idx;
    var ph = String((payload && payload.phase) || '');
    if (ph === 'confirming') return 0;
    if (ph === 'done' || ph === 'failed') return 2;
    return 1;
  }

  function renderPhaseStrip(payload) {
    var steps = (payload && Array.isArray(payload.phase_steps) && payload.phase_steps.length)
      ? payload.phase_steps : PHASE_DEFAULT;
    var idx = phaseIndexOf(payload);
    var failed = String((payload && payload.phase) || '') === 'failed';
    var box = el('div', 'sc-phase');
    for (var i = 0; i < steps.length; i++) {
      var cls = 'sc-phase-item';
      if (i < idx) cls += ' done';
      if (i === idx) cls += failed ? ' bad' : ' on';
      box.appendChild(el('span', cls, String(steps[i])));
      if (i < steps.length - 1) box.appendChild(el('span', 'sc-phase-arw', '›'));
    }
    return box;
  }

  function roundVerdict(r) {
    if (r && r.compile_ok === false) return '编译失败';
    if (r && r.overfit_flag) return '过拟合';
    var v = String((r && r.verdict) || '');
    /* 取值来自 strategy_iter.verdict（实测分布：keep 92 / reject 18 / best 11 / failed 3） */
    if (v === 'best') return '最优';
    if (v === 'keep') return '保留';
    if (v === 'reject') return '淘汰';
    if (v === 'degenerate') return '无效（零成交/数据不足）';
    if (v === 'failed') return '失败';
    return v || '完成';
  }

  function roundVerdictCls(r) {
    if (r && (r.compile_ok === false || String(r.verdict) === 'failed')) return ' bad';
    if (r && String(r.verdict) === 'degenerate') return ' warn';
    if (r && r.overfit_flag) return ' warn';
    if (String((r && r.verdict) || '') === 'best') return ' best';
    return '';
  }

  function roundRow(r, pending) {
    var row = el('div', 'sc-round' + (pending ? ' pending' : ''));
    row.appendChild(el('span', 'sc-round-dot', pending ? '○' : '●'));
    row.appendChild(el('span', 'sc-round-no',
      '第 ' + (r && r.round !== null && r.round !== undefined ? r.round : '?') + ' 轮'));
    row.appendChild(el('span', 'sc-round-hyp',
      String((r && r.hypothesis) || (pending ? '迭代中…' : ''))));
    var meta = '';
    if (r && r.oos_sharpe !== null && r.oos_sharpe !== undefined) {
      meta += 'OOS ' + Number(r.oos_sharpe).toFixed(2);
    }
    if (r && r.oos_trades !== null && r.oos_trades !== undefined) {
      meta += (meta ? ' · ' : '') + r.oos_trades + ' 笔';
    }
    row.appendChild(el('span', 'sc-round-meta', meta));
    if (!pending) {
      row.appendChild(el('span', 'sc-round-verdict' + roundVerdictCls(r), roundVerdict(r)));
    }
    return row;
  }

  function renderRounds(rounds, total, done) {
    var box = el('div', 'sc-rounds');
    var list = Array.isArray(rounds) ? rounds.slice() : [];
    var shown = list.slice(-ROUND_MAX_ROWS);
    if (list.length > shown.length) {
      box.appendChild(el('div', 'sc-rounds-more',
        '前 ' + (list.length - shown.length) + ' 轮已折叠，完整过程看「看研发过程」'));
    }
    shown.forEach(function (r) { box.appendChild(roundRow(r, false)); });
    if (done < total) box.appendChild(roundRow(null, true));
    return box;
  }

  /* 原地重绘时间线（SSE 每轮触发）。整块替换而不是逐节点更新：
     轮次数量少、结构简单，替换比 diff 更不容易出状态残留。 */
  function _paintRounds(card, rounds, total, done) {
    if (!card) return;
    var old = card.querySelector('.sc-rounds');
    if (!old || !old.parentNode) return;
    old.parentNode.replaceChild(renderRounds(rounds, total, done), old);
  }

  function renderProgress(payload) {
    var rows = el('div', 'sc-progress-rows');
    var total = int(payload.total_rounds, '8');
    var done = int(payload.round_done, '0');

    rows.appendChild(renderPhaseStrip(payload));

    var bar = el('div', 'sc-progress-bar');
    var fill = el('div', 'sc-progress-fill');
    bar.appendChild(fill);
    rows.appendChild(bar);
    rows.appendChild(el('div', 'sc-progress', '已完成 ' + done + ' / ' + total + ' 轮'));

    rows.appendChild(renderRounds(payload.rounds || [], total, done));

    var last = payload.latest || {};
    var line = el('div', 'sc-sub sc-progress-now', '正在起跑…');
    if (last.hypothesis) {
      line.textContent = '第 ' + (last.round === null || last.round === undefined ? '?' : String(last.round))
        + ' 轮：' + String(last.hypothesis);
    }
    rows.appendChild(line);
    rows.appendChild(el('div', 'sc-progress-hint',
      '页面会自己更新，跑完自动出结果卡。要中止说「结束会话」。'));
    /* 联动：进度卡直接开策略研究面板（面板打开即载入最近一次研究，与卡片是同一个 run） */
    var actions = el('div', 'sc-actions');
    var open = el('button', 'sc-btn', '看研发过程');
    open.type = 'button';
    open.title = '打开策略研究面板，看每一轮的想法、改动与 IS/OOS 指标';
    open.addEventListener('click', function () {
      var rp = window.StockRadar && window.StockRadar.strategyResearchPanel;
      var uuid = payload && payload.run_uuid;
      if (rp && typeof rp.openPage === 'function') { rp.openPage(uuid); return; }
      if (rp && typeof rp.open === 'function') { rp.open(); return; }
      /* 面板没加载（版本不一致/脚本没到位）：必须说话，不能点了没反应 */
      alert('策略研究面板未加载，请刷新页面后重试。');
    });
    actions.appendChild(open);
    rows.appendChild(actions);
    return rows;
  }

  /* ── 实时进度订阅（对话内的策略研发进度条）──────────────────────────────
     为什么必须在这里做：后端 /api/strategy-research/runs/{uuid}/events 早就存在
     （strategy-research.js 面板一直在用），但对话轮从不订阅它 —— 起跑就返回，
     用户只能再问一句「跑到哪了」。这里让进度卡自己订阅 SSE 并原地刷新。

     为什么状态放模块级表：卡片 DOM 会被 app.js 的消息重建销毁（与 verifyState 同理），
     每次重渲染都要按 run_uuid 把订阅与进度贴回来。

     为什么按 run_uuid 只订阅一次：EventSource 开太多会把浏览器 6 条连接占满
     （历史里已因为门闸挂起耗尽连接导致静态资源 503，见 WarmupGateMiddleware 那次修复）。 */
  var liveState = {};   /* run_uuid → {es, rounds:[], status, doneRound, total} */
  var LIVE_MAX_ROWS = 40;   /* 同时订阅的研发任务上限，超出的不再订阅（面板仍可看） */

  function _liveCount() {
    var n = 0;
    for (var k in liveState) { if (Object.prototype.hasOwnProperty.call(liveState, k)) n++; }
    return n;
  }

  /* 消息重渲染后卡片 DOM 会被换掉：SSE 回调里捕获的 card 指向**已脱离文档**的旧节点，
     继续往上画等于画在空气里（实测 2026-10-18：进度只有刷新页面才更新 —— 刷新后
     卡片只渲染一次，捕获的节点一直在文档里，所以又"正常"了）。
     回调内一律按 data-run-uuid 现查活着的节点，查不到才退回捕获值。 */
  function liveNodeFor(runUuid, fallback) {
    if (!runUuid) return fallback;
    var nodes = document.querySelectorAll('.sc-card[data-run-uuid]');
    var alt = null;
    for (var i = 0; i < nodes.length; i++) {
      if (String(nodes[i].getAttribute('data-run-uuid') || '') !== runUuid) continue;
      /* 同 run 可能同时存在进度卡与结果卡：优先带进度条的那张 */
      if (nodes[i].querySelector && nodes[i].querySelector('.sc-progress-bar')) return nodes[i];
      if (!alt) alt = nodes[i];
    }
    return alt || fallback;
  }

  function _liveBump(payload, node) {
    if (!node) return;
    var total = int(payload && payload.total_rounds, '8') || 8;
    var done = Number((payload && payload.round_done) || 0) || 0;
    var fill = node.querySelector('.sc-progress-fill');
    if (fill) fill.style.width = Math.min(100, Math.round((done / total) * 100)) + '%';
    var txt = node.querySelector('.sc-progress');
    if (txt) txt.textContent = '已完成 ' + done + ' / ' + total + ' 轮';
  }

  function _applyLive(card, payload) {
    var st = liveState[runUuidOf(card)];
    if (!st) return;
    var total = int(payload && payload.total_rounds, '8') || 8;
    var done = Math.max(int(payload && payload.round_done, '0') || 0, st.doneRound || 0);
    /* 快照（含 rounds）比本地累积更权威：刷新后先拿到的是全量，别再叠加 */
    if (payload && Array.isArray(payload.rounds) && payload.rounds.length >= (st.rounds || []).length) {
      st.rounds = payload.rounds;
    }
    _liveBump({ total_rounds: total, round_done: done }, card);
    _paintRounds(card, st.rounds || [], total, done);
    var last = (st.rounds || [])[st.rounds.length - 1];
    var line = card.querySelector('.sc-progress-now');
    if (line && last) {
      line.textContent = '第 ' + last.round + ' 轮：' + last.hypothesis;
    }
  }

  function runUuidOf(card) {
    return (card && card.getAttribute && card.getAttribute('data-run-uuid')) || '';
  }

  function _closeLive(runUuid) {
    var st = liveState[runUuid];
    if (st && st.es) { try { st.es.close(); } catch (e) { /* noop */ } }
    delete liveState[runUuid];
  }

  /* 跑完自动把进度卡换成结果卡（设计 2026-10-17 §2.2）。
     为什么不发消息让用户再问一句：原型与设计文档 §5.6 都要求「跑完自动出结果」。
     为什么走接口而不是前端拼：结果卡的数字口径（best_iter / 体检 / 能力缺口 /
     Wilson 区间）全在后端，前端拼会分叉。
     幂等：替换后的结果卡带 data-upgraded=1；消息重渲染后重新订阅、重新 final，
     会再取一次（数据一致），不会叠加。 */
  function upgradeToResult(card, runUuid) {
    if (!card || !runUuid) return;
    if (card.getAttribute && card.getAttribute('data-upgraded') === '1') return;
    card.setAttribute('data-upgraded', '1');
    var url = '/api/strategy-research/runs/' + encodeURIComponent(runUuid) + '/card';
    fetch(url, { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        var data = res && res.data;
        if (!data || data.kind !== 'result' || !data.payload) {
          throw new Error('no result payload');
        }
        var node = renderCard(data.payload, messageKeyOf(card));
        node.setAttribute('data-upgraded', '1');
        if (card.parentNode) card.parentNode.replaceChild(node, card);
      })
      .catch(function () {
        /* 升级失败：退回「点一下拿结果」，别让卡片卡在「正在生成结果卡…」 */
        if (card.getAttribute) card.removeAttribute('data-upgraded');
        var hint = card.querySelector('.sc-progress-hint');
        if (hint) hint.textContent = '跑完了。点下面按钮看完整结果（也可以直接问我「跑到哪了」）。';
        if (!card.querySelector('.sc-progress-go')) {
          var go = el('button', 'sc-btn sc-progress-go', '看完整结果');
          go.type = 'button';
          go.addEventListener('click', function () { sendOrFill('跑到哪了'); });
          card.appendChild(go);
        }
      });
  }

  /* 续跑入口（2026-10-18）：只对 interrupted 开放。
     为什么放在卡片上：中断是「服务重启/崩溃」这类外部原因，不该逼用户去面板里找。
     幂等：按钮带 data-resume，重复渲染不叠加；点击后立刻 disabled 防连点。 */
  function ensureResume(card, runUuid, doneRound) {
    if (!card || !runUuid) return;
    if (card.querySelector('[data-resume]')) return;
    var actions = card.querySelector('.sc-actions');
    if (!actions) return;
    var n = Math.max(0, Number(doneRound) || 0);
    var btn = el('button', 'sc-btn sc-resume-btn',
      n > 0 ? ('从第 ' + (n + 1) + ' 轮续跑') : '续跑这次研究');
    btn.type = 'button';
    btn.setAttribute('data-resume', '1');
    btn.title = '服务重启中断了这次研发；续跑会接着同一个任务继续，不从头开始';
    btn.addEventListener('click', function () {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = '正在续跑…';
      fetch('/api/strategy-research/runs/' + encodeURIComponent(runUuid) + '/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          return { ok: r.ok, body: j || {} };
        });
      }).then(function (res) {
        if (!res.ok) {
          var b = res.body || {};
          throw new Error(b.detail || b.msg || '续跑失败');
        }
        btn.remove();
        var hint = card.querySelector('.sc-progress-hint');
        if (hint) hint.textContent = '已续跑，从第 ' + (n + 1) + ' 轮继续…';
        subscribeLive(card, { run_uuid: runUuid, total_rounds: 8, round_done: n });
      }).catch(function (e) {
        btn.disabled = false;
        btn.textContent = '续跑失败，点我重试';
        var hint = card.querySelector('.sc-progress-hint');
        if (hint) hint.textContent = '续跑没成功：' + (e && e.message ? e.message : e);
      });
    });
    actions.appendChild(btn);
  }

  function subscribeLive(card, payload) {
    var runUuid = runUuidOf(card);
    if (!runUuid) return;
    /* 中断的任务不会再有事件：不订阅，直接给续跑入口。
       （否则 EventSource 会挂着轮询到服务端 1h 超时，白占浏览器 6 条连接之一） */
    if (String((payload && payload.status) || '') === 'interrupted') {
      ensureResume(card, runUuid, int(payload && payload.round_done, '0'));
      return;
    }
    var total = int(payload && payload.total_rounds, '8') || 8;
    var st = liveState[runUuid];
    if (st && st.es) { _applyLive(card, payload); return; }        /* 已在订阅，只贴状态 */
    if (_liveCount() >= LIVE_MAX_ROWS) { _applyLive(card, payload); return; }
    if (window.EventSource === undefined) { _applyLive(card, payload); return; }

    st = { es: null, rounds: [], doneRound: int(payload && payload.round_done, '0') || 0, total: total, final: false };
    liveState[runUuid] = st;
    var es;
    try {
      es = new EventSource('/api/strategy-research/runs/' + encodeURIComponent(runUuid) + '/events');
    } catch (e) {
      delete liveState[runUuid];
      return;
    }
    st.es = es;

    function roundDone(d) {
      if (!d || d.round === undefined || d.round === null) return;
      var arr = st.rounds;
      var hit = null;
      for (var i = 0; i < arr.length; i++) { if (arr[i].round === d.round) { hit = arr[i]; break; } }
      if (!hit) {
        hit = { round: d.round, hypothesis: d.hypothesis || '', verdict: d.verdict || '', overfit_flag: !!d.overfit_flag };
        arr.push(hit);
        if (d.round > st.doneRound) st.doneRound = d.round;
      } else {
        hit.verdict = d.verdict || hit.verdict;
        hit.hypothesis = d.hypothesis || hit.hypothesis;
      }
      var node = liveNodeFor(runUuid, card);
      _liveBump({ total_rounds: st.total, round_done: st.doneRound }, node);
      _paintRounds(node, arr, st.total, st.doneRound);
      var line = node.querySelector('.sc-progress-now');
      if (line) {
        line.textContent = d.verdict === 'failed'
          ? ('第 ' + d.round + ' 轮失败：' + (d.error || '原因未记录'))
          : ('第 ' + d.round + ' 轮：' + (hit.hypothesis || ''));
      }
      var hint = node.querySelector('.sc-progress-hint');
      if (d.verdict === 'failed' && hint) {
        hint.textContent = '这一轮跑挂了，正在收尾。详细原因研发结束后会写进结果卡。';
      }
    }

    es.addEventListener('strategy_iter_start', function (ev) {
      var d = null; try { d = JSON.parse(ev.data); } catch (e) { return; }
      roundDone(d);
    });
    es.addEventListener('strategy_iter_done', function (ev) {
      var d = null; try { d = JSON.parse(ev.data); } catch (e) { return; }
      roundDone(d);
    });
    /* 终态：**自动**把进度卡换成结果卡（设计 2026-10-17 §2.2）。
       旧行为是改一句文案 + 让用户点「看完整结果」（等于把球踢回给用户），
       与原型「跑完就能看到结果」不符。失败态仍然只提示 + 引导重跑。 */
    function onFinal(d) {
      st.final = true;
      _closeLive(runUuid);
      var status = String((d && d.status) || '');
      var ok = !status || status === 'done';
      var node = liveNodeFor(runUuid, card);
      var hint = node.querySelector('.sc-progress-hint');
      if (ok) {
        _liveBump({ total_rounds: st.total, round_done: st.total }, node);
        _paintRounds(node, st.rounds || [], st.total, st.total);
        if (hint) hint.textContent = '跑完了，正在生成结果卡…';
        upgradeToResult(node, runUuid);
        return;
      }
      if (status === 'interrupted') {
        if (hint) {
          hint.textContent = '已中断（服务重启），已完成 ' + (st.doneRound || 0) + ' 轮。'
            + '可以续跑，不必从头再来。';
        }
        ensureResume(node, runUuid, st.doneRound || 0);
        _liveBump({ total_rounds: st.total, round_done: st.doneRound }, node);
        return;
      }
      if (hint) {
        hint.textContent = '研发结束但没跑成（状态：' + (status || 'unknown') + '）。'
          + '可以问我「为什么」，或说「重跑」再试一次。';
      }
      _liveBump({ total_rounds: st.total, round_done: st.doneRound }, node);
    }
    es.addEventListener('strategy_run_done', function (ev) {
      var d = null; try { d = JSON.parse(ev.data); } catch (e) { d = null; }
      if (d && d.status) { d = d; }
      onFinal({ status: (d && d.status) || 'done' });
    });
    es.addEventListener('final', function (ev) {
      var d = null; try { d = JSON.parse(ev.data); } catch (e) { d = null; }
      onFinal(d || {});
    });
    es.addEventListener('task_error', function (ev) {
      var d = null; try { d = JSON.parse(ev.data); } catch (e) { d = null; }
      _closeLive(runUuid);
      var node = liveNodeFor(runUuid, card);
      var hint = node.querySelector('.sc-progress-hint');
      if (hint) hint.textContent = '研发任务中断：' + ((d && d.message) || '原因未记录');
    });
    /* 断线不自行重连：final 有 DB 兜底，重连反而可能占满浏览器连接（6 条上限）。*/
    es.onerror = function () { /* 等 final / 用户手动追问兜底 */ };

    _applyLive(card, payload);
  }

  function renderCard(payload, msgKey) {
    payload = payload || {};
    var kind = payload.kind || 'result';
    /* 追加分支（不改既有 kind 的行为）：批量验证卡 / 换标的对照卡 */
    if (kind === 'batch_validation' || kind === 'batch-validation') {
      return renderBatchValidationCard(payload);
    }
    if (kind === 'compare' || kind === 'peer_compare') {
      return renderCompareCard(payload);
    }
    var card = el('div', 'sc-card sc-' + kind);
    /* run_uuid 落到 DOM 上：Plan 3 的台账跳转/按钮接线按它定位这一轮策略 */
    var runUuid = payload.run_uuid === undefined || payload.run_uuid === null
      ? '' : String(payload.run_uuid);
    card.setAttribute('data-run-uuid', runUuid);
    /* 验证流程的状态键：消息 id + run_uuid（见 messageKeyOf 注释） */
    if (runUuid) card.setAttribute('data-verify-key', String(msgKey || '') + '|' + runUuid);

    var head = el('div', 'sc-head');
    /* 标题优先显示策略想法本身（用户在这个会话里说的那句话），标的与区间降为副标题 */
    var idea = String(payload.idea || '').trim();
    if (idea) head.appendChild(el('div', 'sc-title', idea));
    var title = String(payload.universe || '') + ' · ' + (payload.period || '某段区间');
    head.appendChild(el('div', idea ? 'sc-sub' : 'sc-title', title));
    /* 成交口径：优先用 payload 里的 entry/exit（/card 端点已按任务行补全）；
       对话正文卡片只有买入侧，再按 run_uuid 拉任务详情补卖出侧。
       两个都缺时整行隐藏——宁可少一行，也不硬猜一个可能错的口径。 */
    var timingEl = el('div', 'sc-sub');
    var paintTiming = function (text, raw) {
      if (!text) { timingEl.style.display = 'none'; return; }
      timingEl.textContent = raw ? text : '成交口径：' + text;
      timingEl.style.display = '';
    };
    head.appendChild(timingEl);
    var timingText = timingLabel(payload.entry_timing, payload.exit_timing);
    if (timingText) paintTiming(timingText);
    else if (payload.entry_timing_text) paintTiming(payload.entry_timing_text, true);
    else paintTiming('');
    if (kind === 'result' && runUuid
        && (!payload.entry_timing || !payload.exit_timing)) {
      fetchRunTiming(runUuid, function (d) {
        paintTiming(timingLabel(d.entry_timing, d.exit_timing));
      });
    }
    if (payload.health) {
      /* 徽章文字保留后端原值（对外契约，便于机器核对），中文释义放 title 提示 */
      var h = String(payload.health).toLowerCase();
      var badge = el('span', 'sc-badge sc-badge-' + h, '体检 ' + h);
      if (HEALTH_LABEL[h]) badge.setAttribute('title', HEALTH_LABEL[h]);
      head.appendChild(badge);
    }
    card.appendChild(head);

    if (kind === 'progress') {
      card.appendChild(renderProgress(payload));
      /* 起跑那一刻就订阅 SSE：进度条自己走到底，不需要用户再问一句。
         订阅失败（无 EventSource / 连接数超限）时静默降级为静态进度条。*/
      try { subscribeLive(card, payload); } catch (e) { /* 降级：静态卡仍可用 */ }
      return card;
    }

    if (payload.oos) card.appendChild(renderKpiGrid(payload));
    if (payload.oos || payload.is) card.appendChild(renderCompareTable(payload));
    if (payload.wilson) {
      card.appendChild(el('div', 'sc-ci',
        '胜率 95% 置信区间（Wilson）：' + pct(payload.wilson.low) + ' ~ ' + pct(payload.wilson.high)
        + '（n=' + num(payload.wilson.n, 0) + '）'));
    }
    card.appendChild(renderNotes(payload.notes));
    card.appendChild(renderActions(payload.actions, payload));
    if (kind === 'result') card.appendChild(renderPoolStudy(runUuid));
    appendVerifyEntry(card, payload);
    /* 消息重建后把验证流程的状态贴回来（没有状态时是空操作） */
    applyVerifyState(card);
    /* 同一 run 的进度卡可能刚被替换成结果卡：把实时订阅关掉，避免同一 run 两条连接。
       （连接数上限 6 条，重复订阅会挤掉静态资源请求 —— 历史上出现过因此 503。）*/
    if (runUuid && liveState[runUuid]) _closeLive(runUuid);
    return card;
  }

  /* ── 「换个标的验证」与「批量验证卡」（设计 §10 / Plan 3 任务 8）────────────────
     后端契约：
     - GET  /api/backtest/strategies/{strategy_id}/peer-candidates?code=&limit=
            → {source, board_name, candidates:[{code,name}], hint}
     - POST /api/backtest/runs/{run_id}/rerun-with {code,start_date?,end_date?}
            → {run_id: 新 run，异步在后端线程里跑}
     - GET  /api/backtest/runs/{run_id} → {run, result, ...}（result 就是对照表要的字段）
     - GET  /api/backtest/batches/{batch_id}/validation → 批量验证三档计数 + hint

     换票流程为什么能自己找到「原 run」：卡片只带 run_uuid（策略研发任务 id，SR-…），
     不带回测 run_id。策略研发的 IS/OOS 回测写库时把 ref_id 记成 run_uuid
     （backtest/strategy_research.py:578-583），所以这里用
     GET /api/backtest/runs?code=<原标的> 反查 ref_id 命中的那条回测记录当基准。
     卡片 payload 若直接给了 run_id（payload.run_id / backtest_run_id），则优先用它。 */

  var VERIFY_MIN_TRADES = 5;    /* 与后端 VALIDATION_MIN_TRADES 同口径，仅用于提示文案 */
  var VERIFY_POLL_MAX = 60;     /* 最多轮询 60 次 */
  var VERIFY_POLL_MS = 1500;    /* 每次间隔 1.5s → 最长约 90s */
  /* 卡片是「由 payload 重新生成」的：app.js 刷新/新增消息时会把整段消息列表重建，
     追加进卡片的 DOM（进度行、候选面板、对照卡）几百毫秒内就会被销毁。
     所以验证流程的状态按 run_uuid 存在模块级表里，每次卡片重渲染后再贴回去。 */
  var verifyState = {};   /* key → {status, panel:{base,data}, compare:{baseRunId,code,payload}} */

  /* 卡片身份 = 所在消息的 DOM id（chat-core.js:784 给 assistant 消息写 id="msg-<message_uuid>"，
     整段消息重建时 id 不变）+ 这一轮策略的 run_uuid。
     只按 run_uuid 存状态是不够的：同一个 run_uuid 会有多条消息各带一张结果卡（每次问「跑到哪了」都会贴一张），
     状态会同时贴到所有同 id 的卡上。 */
  function messageKeyOf(node) {
    while (node && node !== document) {
      if (node.id && String(node.id).indexOf('msg-') === 0) return String(node.id);
      node = node.parentNode;
    }
    return '';
  }

  function cardKey(payload) {
    if (!payload) return '';
    var raw = payload.run_uuid || payload.run_id || payload.backtest_run_id || '';
    return raw === undefined || raw === null ? '' : String(raw);
  }

  function runUuidOf(card) {
    return card && card.getAttribute ? String(card.getAttribute('data-run-uuid') || '') : '';
  }

  /* 实时身份：卡片挂进文档之后才算得出来。appendMessage 是先渲染正文（chat-core.js:801）、
     最后才把 .message 接进列表（:1030），渲染那一刻整棵子树还是游离的，祖先上既没有 .message
     也没有 msg-<uuid> 的 id；所以渲染时写下的 data-verify-key 恒为空，必须在挂载后再算一次。 */
  function liveKeyOf(card) {
    if (!card || !card.getAttribute) return '';
    var runUuid = runUuidOf(card);
    if (!runUuid) return '';
    var msgId = messageKeyOf(card);
    if (!msgId) return '';
    /* 同一条消息里可能有多张同 run 的卡：按文档顺序补一个序号，避免共用同一份状态 */
    var all = document.querySelectorAll('.sc-card[data-run-uuid]');
    var n = 0;
    for (var i = 0; i < all.length; i++) {
      if (all[i] === card) break;
      if (messageKeyOf(all[i]) === msgId && runUuidOf(all[i]) === runUuid) n++;
    }
    return msgId + '|' + runUuid + (n ? '|' + n : '');
  }

  function keyOfCard(card) {
    return liveKeyOf(card) || (card && card.getAttribute ? String(card.getAttribute('data-verify-key') || '') : '');
  }

  function stateFor(key, create) {
    if (!key) return null;
    if (!verifyState[key] && create) verifyState[key] = { status: '', panel: null, compare: null };
    return verifyState[key] || null;
  }

  /* 消息重建后拿到新的卡片实例：按实时身份找回文档里活着的那张（属性可能是渲染期的空值，故现算） */
  function liveCard(key) {
    if (!key) return null;
    var nodes = document.querySelectorAll('.sc-card[data-run-uuid]');
    for (var i = 0; i < nodes.length; i++) {
      if (liveKeyOf(nodes[i]) === key) return nodes[i];
    }
    for (var j = 0; j < nodes.length; j++) {
      if (String(nodes[j].getAttribute('data-verify-key') || '') === key) return nodes[j];
    }
    return null;
  }

  function setStatusFor(card, key, text) {
    var st = stateFor(key, true);
    if (st) st.status = String(text === undefined || text === null ? '' : text);
    var target = liveCard(key) || card;
    if (target) applyVerifyState(target);
    return target;
  }

  function insertCompareCard(card, spec) {
    if (!card || !spec || !spec.payload) return;
    /* 同一张卡旁边只留最新的一张对照卡（换第二个候选时旧的要撤掉） */
    var host = card.parentNode;
    if (host) {
      Array.prototype.forEach.call(host.querySelectorAll('[data-verify-compare]'), function (n) {
        if (n.parentNode) n.parentNode.removeChild(n);
      });
    }
    var node = renderCompareCard(spec.payload);
    node.setAttribute('data-verify-compare', String(spec.baseRunId || '') + ':' + String(spec.code || ''));
    if (host) host.insertBefore(node, card.nextSibling);
    else card.appendChild(node);
  }

  /* 卡片刚渲染时还在游离状态，消息 id 取不到：等一个宏任务（那时 wrap 已接进列表）再贴一次 */
  function deferApply(card) {
    setTimeout(function () { applyVerifyState(card); }, 0);
  }

  /* 把状态贴回卡片（renderCard 末尾、卡片插入文档后、异步回调完成时都会调；幂等） */
  function applyVerifyState(card) {
    if (!card) return false;
    var key = liveKeyOf(card);
    if (key) card.setAttribute('data-verify-key', key);
    else key = keyOfCard(card);
    var st = stateFor(key, false);
    if (!st) return false;
    if (st.status) setVerifyStatus(card, st.status);
    if (st.panel) renderCandidatePanel(card, st.panel.base, st.panel.data);
    if (st.compare) insertCompareCard(card, st.compare);
    return true;
  }

  function apiFetch(path, options) {
    var api = window.StockRadar && window.StockRadar.api;
    if (!api || typeof api.fetchJSON !== 'function') {
      return Promise.reject(new Error('API 模块未就绪'));
    }
    return api.fetchJSON(path, options);
  }

  function delay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  /* 卡片里能拿到的原标的：优先 payload.code，其次从 universe 里抓代码 */
  function baseCodeOf(payload) {
    var raw = payload && (payload.code || payload.stock_code);
    if (!raw && payload) raw = payload.universe;
    var m = String(raw === undefined || raw === null ? '' : raw).match(/(?:sh|sz|bj)?[0-9]{6}/i);
    return m ? m[0] : '';
  }

  function baseRunIdOf(payload) {
    var raw = payload && (payload.run_id || payload.backtest_run_id);
    return raw === undefined || raw === null ? '' : String(raw);
  }

  function closestCard(node) {
    while (node && node !== document) {
      if (node.getAttribute && String(node.className || '').split(/\s+/).indexOf('sc-card') >= 0) {
        return node;
      }
      node = node.parentNode;
    }
    return null;
  }

  /* 卡片上挂一行进度/错误提示（同卡只保留一行，重复调用就地更新） */
  function setVerifyStatus(card, text) {
    if (!card) return null;
    var node = card.querySelector('.sc-verify-status');
    if (!node) {
      node = el('div', 'sc-sub sc-verify-status');
      card.appendChild(node);
    }
    node.textContent = String(text || '');
    return node;
  }

  /* 「换票对照卡」：两列并排（原标的 vs 新标的）。
     payload = {base, candidate, base_code, candidate_code, note}
     base / candidate 直接用 GET /api/backtest/runs/{id} 的 result 对象
     （字段：total_return / annual_return / sharpe_ratio / max_drawdown / trade_count / win_rate）。 */
  function renderCompareCard(payload) {
    payload = payload || {};
    var base = payload.base || null;
    var cand = payload.candidate || null;
    var baseCode = String(payload.base_code || (base && base.code) || '原标的');
    var candCode = String(payload.candidate_code || (cand && cand.code) || '新标的');

    var card = el('div', 'sc-card sc-compare');
    var head = el('div', 'sc-head');
    head.appendChild(el('div', 'sc-title', '换标的对照：' + baseCode + ' vs ' + candCode));
    head.appendChild(el('div', 'sc-sub', '同一套策略与参数，只换了标的'));
    card.appendChild(head);

    var fmtPct = function (v) { return pct(v); };
    var fmtNum0 = function (v) { return num(v, 0); };
    var rows = [['总收益', 'total_return', fmtPct], ['年化', 'annual_return', fmtPct],
                ['夏普', 'sharpe_ratio', num], ['最大回撤', 'max_drawdown', fmtPct],
                ['笔数', 'trade_count', fmtNum0], ['胜率', 'win_rate', pctWin]];
    var table = el('table', 'sc-compare-table');
    var thead = el('thead');
    var htr = el('tr');
    ['指标', baseCode, candCode].forEach(function (t) { htr.appendChild(el('th', null, t)); });
    thead.appendChild(htr);
    table.appendChild(thead);
    var tbody = el('tbody');
    rows.forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', null, r[0]));
      tr.appendChild(el('td', null, r[2](base ? base[r[1]] : null)));
      tr.appendChild(el('td', null, r[2](cand ? cand[r[1]] : null)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    card.appendChild(table);

    var notes = Array.isArray(payload.notes) ? payload.notes.slice() : [];
    if (!notes.length) notes.push(String(payload.note || '样本量不足时不要下结论'));
    var bt = Number(base && base.trade_count), ct = Number(cand && cand.trade_count);
    if ((isFinite(bt) && bt < VERIFY_MIN_TRADES) || (isFinite(ct) && ct < VERIFY_MIN_TRADES)) {
      notes.push('有一侧成交笔数少于 ' + VERIFY_MIN_TRADES + ' 笔，样本不够时不要下结论。');
    }
    card.appendChild(renderNotes(notes));
    return card;
  }

  function batchCountText(v) {
    var n = Number(v);
    return (isFinite(n) ? String(Math.round(n)) : '0') + ' 只';
  }

  /* ── 池子级研究（2026-10-18）──
     用户要的是研究员式交付：「多少只 / 哪些行业 / 区间 → 整体成立与否 → 哪些行业更好」。
     后端后台跑（全池 300 只约 3-4 分钟），这里轮询进度，跑完就地渲染报告卡。 */
  function renderPoolStudy(runUuid) {
    var wrap = el('div', 'sc-peer-wrap');
    if (!runUuid) return wrap;
    var tail = el('div', 'sc-peer-result');
    var btn = el('button', 'sc-btn', '池子级研究（沪深300 · 近 1 年）');
    btn.type = 'button';
    btn.title = '把这一轮的最优策略跑遍沪深300成分股，给出「整体成不成立 + 哪些行业更好」的研究结论';
    var row = el('div', 'sc-actions');
    row.appendChild(btn);
    wrap.appendChild(row);
    wrap.appendChild(tail);

    btn.addEventListener('click', function () {
      if (btn.disabled) return;
      btn.disabled = true;
      var old = btn.textContent;
      var status = el('div', 'sc-note', '正在启动池子研究…');
      tail.appendChild(status);

      fetch('/api/strategy-research/runs/' + encodeURIComponent(runUuid) + '/pool-study', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ index_code: '000300', limit: 0, days: 365 })
      }).then(function (r) { return r.json(); }).then(function (res) {
        var sid = res && res.data && res.data.study_id;
        if (!sid) throw new Error((res && res.msg) || '没有返回 study_id');
        row.removeChild(btn);
        pollPoolStudy(sid, status, tail);
      }).catch(function (e) {
        btn.disabled = false;
        btn.textContent = old;
        status.textContent = '池子研究启动失败：' + (e && e.message ? e.message : e);
      });
    });

    function pollPoolStudy(sid, statusNode, host) {
      var ticks = 0;
      var timer = setInterval(function () {
        ticks += 1;
        fetch('/api/strategy-research/pool-studies/' + encodeURIComponent(sid))
          .then(function (r) { return r.json(); })
          .then(function (res) {
            var d = (res && res.data) || {};
            if (d.status === 'done') {
              clearInterval(timer);
              statusNode.parentNode && statusNode.parentNode.removeChild(statusNode);
              host.appendChild(renderPoolReport(d.report || {}));
              // 卡片只有这一次研究的结论；历史研究在独立页面里看
              var more = el('div', 'sc-actions');
              var go = el('button', 'sc-btn', '查看全部池子研究');
              go.type = 'button';
              go.addEventListener('click', function () {
                if (window.StockRadar && window.StockRadar.poolStudy) {
                  window.StockRadar.poolStudy.openPage(sid);
                }
              });
              more.appendChild(go);
              host.appendChild(more);
              return;
            }
            if (d.status === 'failed') {
              clearInterval(timer);
              statusNode.textContent = '池子研究失败：' + (d.error || '原因未记录');
              return;
            }
            var p = d.progress || {};
            statusNode.textContent = '池子研究中… 已跑 ' + (p.done || 0) + ' / ' + (p.total || '?')
              + ' 只（失败 ' + (p.fail || 0) + '）。全池 300 只实测 4-5 分钟（机器忙时 7 分钟），可以先切走做别的。';
          })
          .catch(function () { /* 网络抖动：下一拍再试 */ });
        if (ticks > 600) clearInterval(timer);   // 20 分钟兜底，避免无限轮询
      }, 2000);
    }
    return wrap;
  }

  /* 池子级研究报告卡：先用一句话给结论，再给覆盖/三档/行业/局限。
     顺序是刻意的——用户要能直接复述第一句话。 */
  function renderPoolReport(rep) {
    rep = rep || {};
    var card = el('div', 'sc-card sc-pool');
    var v = rep.verdict || {};
    var head = el('div', 'sc-head');
    var uni = rep.universe || {};
    var period = rep.period || {};
    head.appendChild(el('div', 'sc-title',
      '池子级研究：' + String(rep.index_code || '') + ' · ' + String(uni.total || 0)
      + ' 只 · ' + String(uni.industries || 0) + ' 个行业'));
    head.appendChild(el('div', 'sc-sub',
      String(period.start || '') + ' ~ ' + String(period.end || '')
      + '（' + String(period.days || 0) + ' 天）'));
    card.appendChild(head);

    var hero = el('div', 'sc-pool-verdict sc-pool-' + String(v.key || 'unknown'));
    hero.appendChild(el('span', 'sc-pool-label', String(v.label || '—')));
    hero.appendChild(el('div', 'sc-pool-headline', String(v.headline || '')));
    card.appendChild(hero);

    var o = rep.overall || {};
    var kpis = el('div', 'sc-kpis');
    [['有效 / 可评判', String(o.valid || 0) + ' / ' + String(o.scored || 0)],
     ['胜率中位', pct(o.win_rate_median)],
     ['收益中位', pct(o.total_return_median)],
     ['夏普中位', num(o.sharpe_median)]
    ].forEach(function (r) {
      var cell = el('div', 'sc-kpi');
      cell.appendChild(el('div', 'sc-kpi-k', r[0]));
      cell.appendChild(el('div', 'sc-kpi-v', r[1]));
      if (r[0] === '胜率中位' && o.win_rate_p25 != null && o.win_rate_p75 != null) {
        cell.appendChild(el('div', 'sc-kpi-s',
          'p25 ' + pct(o.win_rate_p25) + ' / p75 ' + pct(o.win_rate_p75)));
      }
      kpis.appendChild(cell);
    });
    card.appendChild(kpis);

    var rows = Array.isArray(rep.by_industry) ? rep.by_industry : [];
    if (rows.length) {
      var table = el('table', 'sc-pool-table');
      var thead = el('thead');
      var htr = el('tr');
      ['行业', '只数', '可评判', '有效占比', '胜率中位', '收益中位'].forEach(function (t) {
        htr.appendChild(el('th', null, t));
      });
      thead.appendChild(htr);
      table.appendChild(thead);
      var tbody = el('tbody');
      // 只展示样本够的行业（n>=3），按有效占比降序；末尾补一行说明被折叠的数量
      var shown = rows.filter(function (r) { return (r.scored || 0) >= 3; }).slice(0, 8);
      shown.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', null, String(r.industry || '')));
        tr.appendChild(el('td', null, String(r.n || 0)));
        tr.appendChild(el('td', null, String(r.scored || 0)));
        tr.appendChild(el('td', null, pct(r.valid_ratio)));
        tr.appendChild(el('td', null, pct(r.win_rate_median)));
        tr.appendChild(el('td', null, pct(r.total_return_median)));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      card.appendChild(table);
      var hidden = rows.length - shown.length;
      if (hidden > 0) {
        card.appendChild(el('div', 'sc-sub',
          '另有 ' + hidden + ' 个行业样本不足或未展示（n<3 的行业结论不稳定）。'));
      }
    }

    var cav = Array.isArray(rep.caveats) ? rep.caveats : [];
    if (cav.length) {
      var box = el('div', 'sc-pool-caveats');
      box.appendChild(el('div', 'sc-metrics-title', '这份结论的边界'));
      cav.forEach(function (t) { box.appendChild(el('div', 'sc-note', String(t))); });
      card.appendChild(box);
    }

    var items = Array.isArray(rep.items) ? rep.items : [];
    if (items.length) {
      var tb = el('table', 'sc-pool-table');
      var th2 = el('thead');
      var ht2 = el('tr');
      ['标的', '行业', '判定', '笔数', '胜率', '区间收益'].forEach(function (t) {
        ht2.appendChild(el('th', null, t));
      });
      th2.appendChild(ht2); tb.appendChild(th2);
      var tb2 = el('tbody');
      items.forEach(function (it) {
        var tr = el('tr');
        tr.appendChild(el('td', null, String(it.code || '') + ' ' + String(it.name || '')));
        tr.appendChild(el('td', null, String(it.industry || '—')));
        tr.appendChild(el('td', null, String(it.bucket || '')));
        tr.appendChild(el('td', null, String(it.trades == null ? '—' : it.trades)));
        tr.appendChild(el('td', null, pct(it.win_rate)));
        tr.appendChild(el('td', null, pct(it.total_return)));
        tb2.appendChild(tr);
      });
      tb.appendChild(tb2);
      card.appendChild(el('div', 'sc-sub', '收益最高的 ' + items.length + ' 只（按区间收益降序）'));
      card.appendChild(tb);
    }
    return card;
  }

  /* 「批量验证卡」：必须同时显示 有效 / 弱 / 无法评判 三个数 + hint。
     只显示「有效 N 只」会让用户以为剩下的全是无效——「没达标」与「没测出来」是两件事。 */
  function renderBatchValidationCard(payload) {
    payload = payload || {};
    var card = el('div', 'sc-card sc-batch-validation');
    var head = el('div', 'sc-head');
    var name = String(payload.strategy_name || payload.strategy_id || '批量验证');
    head.appendChild(el('div', 'sc-title', '批量验证：' + name));
    head.appendChild(el('div', 'sc-sub',
      '共 ' + batchCountText(payload.code_count) + '标的，有结果 '
      + batchCountText(payload.completed_count) + '（三档口径分开看）'));
    card.appendChild(head);

    var box = el('div', 'sc-metrics sc-validation');
    box.appendChild(el('div', 'sc-metrics-title', '验证结论三档'));
    [['有效（胜率 > 50% 且样本 ≥ 5 笔）', batchCountText(payload.valid_count)],
     ['弱（有结果但没达标）', batchCountText(payload.weak_count)],
     ['无法评判（数据缺口 / K 线不足等）', batchCountText(payload.unscored_count)],
     ['夏普中位数', num(payload.median_sharpe)],
     ['胜率中位数', pct(payload.median_win_rate)],
     ['总收益中位数', pct(payload.median_total_return)]
    ].forEach(function (r) {
      var row = el('div', 'sc-metric');
      row.appendChild(el('span', 'sc-metric-k', r[0]));
      row.appendChild(el('span', 'sc-metric-v', r[1]));
      box.appendChild(row);
    });
    card.appendChild(box);

    var notes = [];
    if (payload.hint) notes.push(String(payload.hint));
    if (Number(payload.unscored_count) > 0) {
      notes.push('「无法评判」的 ' + batchCountText(payload.unscored_count)
        + '不计入「弱」：数据或样本问题不等于策略不行。');
    }
    card.appendChild(renderNotes(notes));

    var items = Array.isArray(payload.items) ? payload.items : [];
    if (items.length) {
      var table = el('table', 'sc-batch-table');
      var thead = el('thead');
      var htr = el('tr');
      ['代码', '名称', '笔数', '胜率', '夏普', '体检'].forEach(function (t) {
        htr.appendChild(el('th', null, t));
      });
      thead.appendChild(htr);
      table.appendChild(thead);
      var tbody = el('tbody');
      items.forEach(function (it) {
        it = it || {};
        var tr = el('tr');
        var h = String(it.health || '').toLowerCase();
        tr.appendChild(el('td', null, String(it.code || '')));
        tr.appendChild(el('td', null, String(it.name || '')));
        tr.appendChild(el('td', null, num(it.trade_count, 0)));
        tr.appendChild(el('td', null, pct(it.win_rate)));
        tr.appendChild(el('td', null, num(it.sharpe_ratio)));
        tr.appendChild(el('td', null, (h || '未知') + (HEALTH_LABEL[h] ? '（' + HEALTH_LABEL[h] + '）' : '')));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      card.appendChild(table);
    }
    return card;
  }

  /* 原 run 定位：卡片直接带 run_id 就用它；否则按 code 反查 ref_id == run_uuid 的那条 */
  function resolveBaseRun(code, runUuid, runId) {
    if (runId) return Promise.resolve({ run_id: runId, code: code, strategy_id: '' });
    if (!code) return Promise.reject(new Error('卡片里没有标的代码，无法换票'));
    return apiFetch('/api/backtest/runs?code=' + encodeURIComponent(code) + '&page_size=50')
      .then(function (d) {
        var rows = (d && d.runs) || [];
        var hit = runUuid ? rows.filter(function (r) {
          return String(r.ref_id || '') === String(runUuid);
        }) : [];
        var pool = hit.length ? hit : rows;
        var done = pool.filter(function (r) { return String(r.status || '') === 'completed'; });
        var best = (done.length ? done : pool)[0];
        if (!best) throw new Error('没找到可用于对照的回测记录（' + code + '）');
        return best;
      });
  }

  function renderCandidatePanel(card, base, data) {
    var old = card.querySelector('.sc-verify-panel');
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var panel = el('div', 'sc-verify-panel');
    panel.setAttribute('data-base-run', String(base.run_id || ''));
    panel.setAttribute('data-base-code', String(base.code || ''));
    if (data && data.hint) panel.appendChild(el('div', 'sc-note', String(data.hint)));
    var list = (data && data.candidates) || [];
    if (!list.length) {
      panel.appendChild(el('div', 'sc-note', '没有可换的候选标的：先加自选股或建仓，再回来验证。'));
      card.appendChild(panel);
      return;
    }
    var row = el('div', 'sc-actions');
    list.forEach(function (c) {
      var label = c && c.name ? (c.name + '(' + c.code + ')') : String((c && c.code) || '');
      if (!label) return;
      var btn = el('button', 'sc-btn', '换到 ' + label);
      btn.type = 'button';
      btn.title = '用当前策略的同一套参数在这只票上重跑一次，并与原标的并排对照';
      btn.setAttribute('data-act', 'verify-peer-run');
      btn.setAttribute('data-code', String(c.code || ''));
      row.appendChild(btn);
    });
    panel.appendChild(row);
    card.appendChild(panel);
  }

  function onVerifyPeer(btn) {
    var card = closestCard(btn);
    if (!card) return;
    var key = keyOfCard(card);
    var code = String(btn.getAttribute('data-code') || '');
    var uuid = String(card.getAttribute('data-run-uuid') || '');
    setStatusFor(card, key, '正在找同行业/持仓里的候选标的…');
    resolveBaseRun(code, uuid, String(btn.getAttribute('data-run-id') || ''))
      .then(function (base) {
        return apiFetch('/api/backtest/strategies/'
          + encodeURIComponent(String(base.strategy_id || 'peer'))
          + '/peer-candidates?code=' + encodeURIComponent(String(base.code || code))
          + '&limit=3').then(function (data) {
            var st = stateFor(key, true);
            if (st) st.panel = { base: base, data: data };
            setStatusFor(card, key, '选一只候选标的，用同一套参数重跑做外部验证');
          });
      })
      .catch(function (e) {
        setStatusFor(card, key, '取候选失败：' + ((e && e.message) || e));
      });
  }

  function pollRun(runId) {
    var n = 0;
    function once() {
      return apiFetch('/api/backtest/runs/' + encodeURIComponent(runId)).then(function (d) {
        var run = (d && d.run) || {};
        var st = String(run.status || '');
        if (st === 'completed') return d;
        if (st === 'failed' || st === 'cancelled') {
          throw new Error('新回测 ' + st + '：' + (run.error_message || '未给出原因'));
        }
        n += 1;
        if (n >= VERIFY_POLL_MAX) {
          throw new Error('新回测还在跑，稍后刷新页面看对照结果（run_id=' + runId + '）');
        }
        return delay(VERIFY_POLL_MS).then(once);
      });
    }
    return once();
  }

  function onVerifyPeerRun(btn) {
    var card = closestCard(btn);
    if (!card) return;
    var key = keyOfCard(card);
    var panel = btn.parentNode;
    while (panel && panel !== card && !(panel.getAttribute && panel.getAttribute('data-base-run'))) {
      panel = panel.parentNode;
    }
    var baseRunId = panel ? String(panel.getAttribute('data-base-run') || '') : '';
    var baseCode = panel ? String(panel.getAttribute('data-base-code') || '') : '';
    var code = String(btn.getAttribute('data-code') || '');
    if (!baseRunId || !code) {
      setStatusFor(card, key, '缺少原回测 run_id，无法换票');
      return;
    }
    Array.prototype.forEach.call(card.querySelectorAll('button[data-act="verify-peer-run"]'),
      function (b) { b.disabled = true; });
    setStatusFor(card, key, '正在用同一套参数回测 ' + code + ' …（通常几秒）');
    apiFetch('/api/backtest/runs/' + encodeURIComponent(baseRunId) + '/rerun-with', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code }),
    }).then(function (r) {
      return pollRun(r.run_id).then(function (cand) {
        return apiFetch('/api/backtest/runs/' + encodeURIComponent(baseRunId))
          .then(function (base) {
            var st = stateFor(key, true);
            if (st) {
              st.compare = {
                baseRunId: baseRunId,
                code: code,
                payload: {
                  base: (base && base.result) || {},
                  candidate: (cand && cand.result) || {},
                  base_code: baseCode,
                  candidate_code: code,
                },
              };
            }
            setStatusFor(card, key, '对照已生成：' + baseCode + ' vs ' + code);
          });
      });
    }).catch(function (e) {
      var live = liveCard(key) || card;
      Array.prototype.forEach.call(live.querySelectorAll('button[data-act="verify-peer-run"]'),
        function (b) { b.disabled = false; });
      setStatusFor(card, key, '换票验证失败：' + ((e && e.message) || e));
    });
  }

  /* 「换个标的验证」入口：卡片带了 run_uuid（或 run_id）与标的代码才出现。
     只对 result 卡追加，不改既有渲染逻辑。 */
  function appendVerifyEntry(card, payload) {
    var code = baseCodeOf(payload);
    var uuid = String(payload.run_uuid || '');
    var runId = baseRunIdOf(payload);
    if (!code || (!uuid && !runId)) return;
    var box = el('div', 'sc-actions sc-actions-extra');
    var btn = el('button', 'sc-btn', '换个标的验证');
    btn.type = 'button';
    btn.title = '用同一套参数换一只票重跑，看结论是不是只在原来那只票上成立';
    btn.setAttribute('data-act', 'verify-peer');
    btn.setAttribute('data-code', code);
    if (runId) btn.setAttribute('data-run-id', runId);
    box.appendChild(btn);
    card.appendChild(box);
  }

  function onCardActionClick(ev) {
    var node = ev.target;
    while (node && node !== document) {
      if (node.getAttribute) {
        var act = String(node.getAttribute('data-act') || '');
        if (act === 'verify-peer') { onVerifyPeer(node); return; }
        if (act === 'verify-peer-run') { onVerifyPeerRun(node); return; }
      }
      node = node.parentNode;
    }
  }

  /* 事件委托挂一次即可：卡片是 DOM 替换生成的，逐个绑会漏掉后续渲染的卡 */
  function hookVerifyDelegation() {
    if (document.__strategyVerifyHooked) return true;
    document.__strategyVerifyHooked = true;
    document.addEventListener('click', onCardActionClick);
    return true;
  }

  /* 卡片块判定：正常路径靠 marked 写在 code 上的 language-strategy-card 类名；
     兜底接受正文自带 marker 前缀（围栏 info string 丢失/降级渲染时）。 */
  function isCardBlock(code) {
    if (!code) return false;
    var cls = String(code.className || '');
    if (cls.split(/\s+/).indexOf(LANG_CLASS) >= 0) return true;
    return String(code.textContent || '').indexOf(CARD_MARKER) === 0;
  }

  /* code 文本 → payload；不是合法 JSON 返回 null（流式半截块原样保留，等完整文本重渲染） */
  function parseBlock(code) {
    var text = String((code && code.textContent) || '');
    if (text.indexOf(CARD_MARKER) === 0) text = text.slice(CARD_MARKER.length);
    text = text.trim();
    if (!text) return null;
    try {
      var p = JSON.parse(text);
      return (p && typeof p === 'object' && !Array.isArray(p)) ? p : null;
    } catch (e) {
      return null;
    }
  }

  /* 把 container 里的 strategy-card 代码块换成卡片 DOM；返回替换数量（幂等） */
  function upgrade(container) {
    if (!container || typeof container.querySelectorAll !== 'function') return 0;
    var codes = container.querySelectorAll('pre > code');
    var msgKey = messageKeyOf(container);
    var n = 0;
    Array.prototype.forEach.call(codes, function (code) {
      if (!isCardBlock(code)) return;
      var payload = parseBlock(code);
      if (!payload) return;
      var pre = code.parentNode;
      if (!pre || !pre.parentNode) return;
      var node = renderCard(payload, msgKey);
      pre.parentNode.replaceChild(node, pre);
      /* 插入文档后再贴一次：对照卡要插在卡片旁边，需要 card.parentNode */
      applyVerifyState(node);
      deferApply(node);
      n++;
    });
    return n;
  }

  function hookHighlight() {
    var md = window.StockRadar && window.StockRadar.chatMarkdown;
    if (!md || typeof md._highlightCodeBlocks !== 'function') return false;
    if (md._highlightCodeBlocks[HOOK_FLAG]) return true;
    var orig = md._highlightCodeBlocks;
    var wrapped = function (container) {
      try { upgrade(container); } catch (e) { /* 卡片渲染失败不影响正文渲染 */ }
      return orig.apply(this, arguments);
    };
    wrapped[HOOK_FLAG] = true;
    md._highlightCodeBlocks = wrapped;
    return true;
  }

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.strategyCard = {
    CARD_MARKER: CARD_MARKER,
    LANG_CLASS: LANG_CLASS,
    upgrade: upgrade,
    renderCard: renderCard,
    renderCompareCard: renderCompareCard,
    renderBatchValidationCard: renderBatchValidationCard,
    parseBlock: parseBlock,
    isCardBlock: isCardBlock,
    hookHighlight: hookHighlight,
    hookVerifyDelegation: hookVerifyDelegation,
  };

  /* 委托不依赖其他模块，立刻挂上（卡片可能在任何时刻被替换渲染） */
  hookVerifyDelegation();

  if (!hookHighlight()) {
    // 脚本顺序异常（chat-markdown.js 尚未求值）时补挂
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', hookHighlight);
    }
    window.addEventListener('load', hookHighlight);
  }
})();
