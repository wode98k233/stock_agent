/* 全局总览页：壳层 + SSE 流式填充 + 进度/日志（视觉基线 docs/prototypes/global-overview-v2.html） */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const now = () => new Date().toTimeString().slice(0, 8);
  const nowMs = () => { const d = new Date(); return d.toTimeString().slice(0, 8) + '.' + String(d.getMilliseconds()).padStart(3, '0'); };
  const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* 数值安全与格式化（原型 fmt/sign + 真实 SSE payload 的空值兜底） */
  const fmt = (n, d = 2) => Number(n).toFixed(d);
  const sign = (n, d = 2) => (n > 0 ? '+' : '') + Number(n).toFixed(d);
  const dash = '—';
  const num = (v) => (typeof v === 'number' && isFinite(v)) ? v
    : (typeof v === 'string' && v.trim() !== '' && isFinite(Number(v)) ? Number(v) : null);
  const nf = (v, d = 2) => (num(v) === null ? dash : fmt(v, d));
  const sg = (v, d = 2) => (num(v) === null ? dash : sign(v, d));
  const posCls = (v) => (num(v) === null ? 'v-muted' : (v >= 0 ? 'v-up' : 'v-down'));
  const emptyState = (text) => `<div class="v-muted" style="font-size:11px;padding:6px 0">${esc(text)}</div>`;
  const CURRENCY_UNITS = ['¥', '￥', 'CNY', 'RMB', '元'];
  function fmtCost(v, unit) {
    const n = num(v);
    if (n === null) return dash;
    const u = String(unit || '').trim();
    if (!u) return n.toLocaleString('zh-CN');
    if (CURRENCY_UNITS.indexOf(u) >= 0) return `¥${n.toLocaleString('zh-CN')}`;
    return `${n.toLocaleString('zh-CN')} ${esc(u)}`;
  }
  function fmtVal(v) { return (Number.isInteger(v) && Math.abs(v) >= 1000) ? v.toLocaleString() : String(v); }
  /* token 量级压缩：1.42M / 288.7K / 1,234 */
  function fmtTok(v) {
    const n = num(v);
    if (n === null) return dash;
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (a >= 1e4) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString('zh-CN');
  }
  function fmtIso(s) {
    if (!s) return dash;
    const d = new Date(s);
    if (isNaN(d.getTime())) return String(s).slice(0, 16).replace('T', ' ');
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function fmtScanAt(s) {
    if (!s) return dash;
    const str = String(s);
    const nowD = new Date();
    const p = (n) => String(n).padStart(2, '0');
    const today = `${nowD.getFullYear()}-${p(nowD.getMonth() + 1)}-${p(nowD.getDate())}`;
    const hhmm = str.slice(11, 16);
    return str.slice(0, 10) === today ? `今日 ${hhmm}` : `${str.slice(5, 10)} ${hhmm}`;
  }
  const state = {
    open: false,
    blocks: {},
    running: false, runKind: 'initial', runStart: 0, runTimer: null,
    completed: 0, failed: 0, logCount: 0,
    scanned: false, scan: null, scanFailed: false, planBlocks: [],
    autoEnabled: true, marketRefreshS: 0,
    /* 单块重试的独立流：key = blocks.join(',')，value = AbortController。
       与 state.running（主流的全局互斥）分开 —— 重试不该被正在跑的流挡住，
       也不该打断正在跑的流。后端按块加锁（overview_service._lock_for），
       同块重复点击只会复用同一把锁，多开请求的代价可接受。 */
    retries: new Map(),
    /* 会话档案的本地视图状态（搜索 / 排序 / 按热力图某天过滤）：只重渲染块体，不重跑流 */
    sessionFilterDate: '', sessionQuery: '', sessionOrder: 'recent',
  };

  /* 当前正在被解析的事件来自哪条流：'main' 主流 | 'retry' 独立重试流。
     'final' 事件据此决定要不要动全局进度/按钮/计时。 */
  let runRole = 'main';

  const PAGE_SIZE = 15;
  /* 会话上下文档案单独一档页长：这一块卡片高、条数多（实测 27 条 / 单块 >1000px），
     按 15 条渲染会把整屏撑爆（中列被拉长、左右两列留白）。默认 8 条 + 卡内滚动，
     高度锁在 .sess-scroll 的 max-height 内 —— 其余块（portfolio / watchlist）仍用 15。 */
  const SESSION_PAGE_SIZE = 8;
  function pagerState(blockId) {
    if (!state.pager) state.pager = {};
    if (!state.pager[blockId]) state.pager[blockId] = { page: 1, expanded: false };
    return state.pager[blockId];
  }
  function pageSlice(blockId, items, size) {
    const n = size || PAGE_SIZE;
    const ps = pagerState(blockId);
    const maxPage = Math.max(1, Math.ceil(items.length / n));
    if (ps.page > maxPage) ps.page = maxPage;
    if (ps.expanded || items.length <= n) return items;
    const start = (ps.page - 1) * n;
    return items.slice(start, start + n);
  }
  function pagerHtml(blockId, total, label, size) {
    const n = size || PAGE_SIZE;
    if (total <= n) return '';
    const ps = pagerState(blockId);
    const maxPage = Math.ceil(total / n);
    /* label：调用方给的「条数」文案。会话档案必须带「后端上限 200」—— 那个条数来自
       list_dialogs(limit=200)，截断后写成「共 N 条」会被读成总数（KPI 的 runs 同款处理）。
       不传 label 时文案与改造前逐字一致，portfolio / watchlist 不受影响。 */
    const head = label || `共 ${total} 条`;
    const headAll = label ? `已展开 ${label}` : `已展开全部 ${total} 条`;
    if (ps.expanded) {
      return `<div class="ov-pager"><span>${headAll}</span><span class="spacer"></span>`
           + `<button type="button" data-pager="collapse" data-block="${blockId}">收起分页</button></div>`;
    }
    return `<div class="ov-pager"><span>${head} · 第 ${ps.page}/${maxPage} 页</span><span class="spacer"></span>`
         + `<button type="button" data-pager="prev" data-block="${blockId}" ${ps.page <= 1 ? 'disabled' : ''}>上一页</button>`
         + `<button type="button" data-pager="next" data-block="${blockId}" ${ps.page >= maxPage ? 'disabled' : ''}>下一页</button>`
         + `<button type="button" data-pager="expand" data-block="${blockId}">展开全部</button></div>`;
  }

  let streamAbort = null;
  let timers = [];
  let autoInterval = null;      // 行情类静默刷新计时（每秒倒计时）
  let runtimeInterval = null;   // 运行状态轮询
  let autoLeft = 0;
  const MARKET_BLOCKS = ['market', 'shortline', 'portfolio', 'watchlist'];

  /* ════════════════════ 图形工具（自原型 docs/prototypes/global-overview-v2.html 逐段移植） ════════════════════ */

  const spark = (arr, cls = 'hot', small = false) => {
    const max = Math.max(...arr), min = Math.min(...arr);
    return `<span class="spark${small ? ' sm' : ''}">${arr.map(v => {
      const h = 20 + (max === min ? 0.5 : (v - min) / (max - min)) * 80;
      return `<i class="${v === max ? cls : ''}" style="height:${h}%"></i>`;
    }).join('')}</span>`;
  };

  function gaugeHtml(score) {
    const rot = (Math.max(0, Math.min(100, score)) / 100 * 180 - 90).toFixed(1);
    return `<div class="gauge"><div class="gauge-ring"></div><div class="gauge-hole"></div><div class="gauge-needle" style="transform:rotate(${rot}deg)"></div><div class="gauge-cap"></div></div>`;
  }
  function ringHtml(pct, color = 'var(--brand)', text = null) {
    const p = Math.min(100, pct);
    return `<div class="ring" style="background:conic-gradient(${color} 0 ${p}%, var(--hover) ${p}% 100%)"><b>${text ?? (pct.toFixed(1) + '%')}</b></div>`;
  }
  function poolRing(up, broken, down) {
    const t = up + broken + down, a = up / t * 100, b = a + broken / t * 100;
    return `<div class="ring" style="background:conic-gradient(#ef8a7e 0 ${a.toFixed(1)}%, #f0b429 ${a.toFixed(1)}% ${b.toFixed(1)}%, #7fb8a9 ${b.toFixed(1)}% 100%)"><b>${up}</b></div>`;
  }
  function lineSvg(values, { w = 240, h = 48, color = '#176b5b', fill = true } = {}) {
    const max = Math.max(...values), min = Math.min(...values), rng = (max - min) || 1;
    const X = (i) => (i / (values.length - 1) * (w - 8) + 4).toFixed(1);
    const Y = (v) => (h - 5 - (v - min) / rng * (h - 13)).toFixed(1);
    const pts = values.map((v, i) => `${X(i)},${Y(v)}`).join(' ');
    const area = `${X(0)},${h - 2} ${pts} ${X(values.length - 1)},${h - 2}`;
    const last = values.length - 1;
    return `<svg class="linechart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${fill ? `<polygon points="${area}" fill="rgba(23,107,91,.08)"/>` : ''}
    <line x1="0" y1="${h - 2}" x2="${w}" y2="${h - 2}" stroke="#e5e7eb" stroke-width="1"/>
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${X(last)}" cy="${Y(values[last])}" r="2.6" fill="${color}"/>
  </svg>`;
  }
  function divBar(score, full) {
    const v = parseFloat(score), pct = Math.min(100, Math.abs(v) / full * 100) / 2;
    const side = v >= 0 ? `left:50%;width:${pct}%` : `right:50%;width:${pct}%`;
    return `<div class="divbar"><span class="axis"></span><i class="${v >= 0 ? 'pos' : 'neg'}" style="${side}"></i></div>`;
  }
  const fRow = ([lb, sc, val]) => {
    /* 缺失因子（score=null）不给 0 分（数据缺失 ≠ 中性），只显示占位 */
    if (sc === null || sc === undefined) {
      return `<div class="f-row"><span class="f-label">${esc(lb)}</span><span class="f-bar"></span><span class="f-score zero">${dash}</span><span class="f-val">${esc(val)}</span></div>`;
    }
    const w = Math.min(100, Math.abs(sc) / 2 * 100) / 2;
    const side = sc >= 0 ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
    const cls = sc > 0 ? 'pos' : sc < 0 ? 'neg' : 'zero';
    return `<div class="f-row"><span class="f-label">${esc(lb)}</span><span class="f-bar"><i class="${cls}" style="${side}"></i></span><span class="f-score ${cls}">${sc > 0 ? '+' + sc : sc}</span><span class="f-val">${esc(val)}</span></div>`;
  };
  function ladderMini(items) {
    const max = Math.max(...items.map(x => x[1]));
    return `<div class="lm">${items.map(([k, n]) => `
    <div class="lm-c"><span class="n">${n}</span><span class="lm-bar" style="height:${Math.max(6, n / max * 44)}px"></span><span class="k">${esc(k)}</span></div>`).join('')}</div>`;
  }
  function calibChart(buckets) {
    return `<div class="calib">${buckets.map(([lb, p, a, n]) => `
    <div class="calib-c">
      <div class="calib-bars">
        <i class="pred" style="height:${(p * 62).toFixed(0)}px" title="预测置信度 ${(p * 100).toFixed(0)}%"></i>
        <i class="act" style="height:${(a * 62).toFixed(0)}px" title="实际胜率 ${(a * 100).toFixed(0)}%"></i>
      </div>
      <span class="lb">${esc(lb)}</span><span class="nb">n=${n}</span>
    </div>`).join('')}</div>`;
  }
  const pairBars = (prev, cur) => {
    const max = Math.max(prev, cur) || 1;
    return `<span class="pair" title="上日 ${Math.round(prev)} 亿 / 今日 ${Math.round(cur)} 亿"><i style="height:${Math.max(8, prev / max * 26).toFixed(0)}px"></i><i class="cur" style="height:${Math.max(8, cur / max * 26).toFixed(0)}px"></i></span>`;
  };
  const macroRowHtml = (m) => {
    const color = m.cls === 'v-up' ? '#e74c3c' : m.cls === 'v-down' ? '#176b5b' : '#9ca3af';
    return `
  <div class="macro-row" title="数据截止 ${esc(m.dt)}${m.stale ? ' · ' + esc(m.stale) : ''}">
    <span class="nm">${esc(m.l)}</span>
    ${m.sp.length >= 2 ? lineSvg(m.sp, { w: 64, h: 20, fill: false, color }) : '<span></span>'}
    <span class="v">${esc(m.v)}<span class="u">${esc(m.u)}</span></span>
    <span class="dp ${m.cls}">${esc(m.dp)}</span>
    ${m.stale ? `<span class="stale">${esc(m.stale)} · 数据截止 ${esc(m.dt)}</span>` : ''}
  </div>`;
  };


  /* ════════════════════ 上下文洞察三块（任务 12：usage / activity / sessions）
     视觉基线 docs/prototypes/2026-10-17-context-insight.html 的 section#skinB（浅色）。
     本任务只允许改 overview.js / index.html，原型类名对应的样式在此按需注入
     （作用域 #ovPage .ov-insight，色值一律引用 variables.css 既有令牌，不新增色板）。
     ⚠️ 同屏两块日线的「日」口径不同，禁止拼成同一根时间轴、也禁止互相标注日期：
        activity.days[].date      = calendar_query 直接对 UTC 时间戳 strftime -> UTC 日；
        daily_tokens / daily_cost = store 的**后端本地** today - (days-1-i) -> 本地日。
     ══════════════════════════════════════════════════════════════════════════════ */

  var INSIGHT_CSS = [
    '#ovPage .ov-insight .grid { display: grid; gap: 10px; }',
    '#ovPage .ov-insight .g-kpi { grid-template-columns: repeat(auto-fit, minmax(112px, 1fr)); }',
    '#ovPage .ov-insight .stack { display: grid; gap: 10px; }',
    '#ovPage .ov-insight .small { font-size: 11px; }',
    /* KPI 带（原型 .kpi / .k-l / .k-v / .k-s） */
    '#ovPage .ov-insight .kpi { min-width: 0; }',
    '#ovPage .ov-insight .kpi .k-l { font-size: 10.5px; color: var(--muted); margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }',
    '#ovPage .ov-insight .kpi .k-v { font-size: 19px; font-weight: 700; line-height: 1.15; font-family: var(--font-mono); }',
    '#ovPage .ov-insight .kpi .k-s { font-size: 10.5px; color: var(--muted); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }',
    '#ovPage .ov-insight .kpi .k-v.c-ok { color: var(--success); }',
    '#ovPage .ov-insight .kpi .k-v.c-muted { color: var(--muted); }',
    /* 近 N 天 token × 费用双柱（原型 .bars7：堆叠，Token 在下、费用在上） */
    '#ovPage .ov-insight .bars7 { display: flex; align-items: flex-end; gap: 10px; height: 120px; }',
    '#ovPage .ov-insight .bars7 .d { flex: 1; display: flex; flex-direction: column-reverse; align-items: center; gap: 1px; height: 100%; min-width: 0; }',
    '#ovPage .ov-insight .bars7 .d i { display: block; width: 62%; border-radius: 2px 2px 0 0; }',
    '#ovPage .ov-insight .bars7 .d i.tk { background: var(--brand); }',
    '#ovPage .ov-insight .bars7 .d i.cs { background: var(--warning); }',
    '#ovPage .ov-insight .bars7-x { display: flex; gap: 10px; margin-top: 4px; }',
    '#ovPage .ov-insight .bars7-x span { flex: 1; min-width: 0; overflow: hidden; text-align: center; font-size: 10px; color: var(--muted); }',
    /* 8 周 × 7 天热力图（原型 .heat；每格 = 一个 UTC 日） */
    '#ovPage .ov-insight .heat { display: grid; grid-template-columns: 26px repeat(8, 1fr); gap: 3px; align-items: center; }',
    '#ovPage .ov-insight .heat .hd { font-size: 9.5px; color: var(--muted); text-align: center; }',
    '#ovPage .ov-insight .heat .w { font-size: 9.5px; color: var(--muted); }',
    '#ovPage .ov-insight .heat i { display: block; aspect-ratio: 1; border-radius: 2px; background: var(--border-light); cursor: pointer; }',
    '#ovPage .ov-insight .heat i.empty { cursor: default; }',
    '#ovPage .ov-insight .heat i.l1 { background: color-mix(in srgb, var(--brand) 22%, transparent); }',
    '#ovPage .ov-insight .heat i.l2 { background: color-mix(in srgb, var(--brand) 42%, transparent); }',
    '#ovPage .ov-insight .heat i.l3 { background: color-mix(in srgb, var(--brand) 66%, transparent); }',
    '#ovPage .ov-insight .heat i.l4 { background: var(--brand); }',
    '#ovPage .ov-insight .heat i.sel { outline: 2px solid var(--warning); outline-offset: 1px; }',
    /* Token 构成环形（原型 .ring / .ring-mid；覆写 overview.css 给 .ring 的 ::after 白盘） */
    '#ovPage .ov-insight .ring { position: relative; width: 132px; height: 132px; flex: none; }',
    '#ovPage .ov-insight .ring::after { display: none; }',
    '#ovPage .ov-insight .ring svg { display: block; transform: rotate(-90deg); }',
    '#ovPage .ov-insight .ring .ring-mid { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }',
    '#ovPage .ov-insight .ring .ring-mid .v { font-size: 18px; font-weight: 700; line-height: 1.1; font-family: var(--font-mono); }',
    '#ovPage .ov-insight .ring .ring-mid .l { font-size: 10px; color: var(--muted); }',
    '#ovPage .ov-insight .ring .ring-mid .r { margin-top: 2px; font-size: 10.5px; font-weight: 700; color: var(--success); }',
    '#ovPage .ov-insight .ring.mini-ring { width: 44px; height: 44px; }',
    '#ovPage .ov-insight .ring-wrap { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }',
    '#ovPage .ov-insight .legend { flex: 1 1 180px; min-width: 180px; max-width: 420px; display: grid; gap: 4px; }',
    '#ovPage .ov-insight .legend .li { display: flex; align-items: center; gap: 6px; font-size: 11px; white-space: nowrap; }',
    '#ovPage .ov-insight .legend .li .dot { flex: none; width: 8px; height: 8px; border-radius: 2px; }',
    '#ovPage .ov-insight .legend .li .nm { flex: 1; overflow: hidden; text-overflow: ellipsis; }',
    '#ovPage .ov-insight .legend .li .vl { min-width: 52px; text-align: right; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }',
    '#ovPage .ov-insight .legend .li .pc { min-width: 46px; text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }',
    /* 会话上下文档案：卡片 + 工具条（原型 .session / .toolbar / .searchbox / .chip） */
    '#ovPage .ov-insight .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }',
    '#ovPage .ov-insight .searchbox { flex: 1; min-width: 120px; padding: 5px 9px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--panel); color: var(--text); font-family: inherit; font-size: 12px; }',
    '#ovPage .ov-insight .chip { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border: 1px solid var(--border); border-radius: 999px; background: var(--panel); color: var(--text-secondary); font-size: 11px; white-space: nowrap; cursor: pointer; }',
    '#ovPage .ov-insight .chip:hover { border-color: var(--brand); color: var(--brand); }',
    '#ovPage .ov-insight .chip.on { border-color: var(--brand); background: var(--brand-light); color: var(--brand); font-weight: 700; }',
    '#ovPage .ov-insight .session { display: grid; grid-template-columns: 46px minmax(0, 1fr) auto; align-items: center; gap: 11px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); cursor: pointer; }',
    '#ovPage .ov-insight .session:hover { border-color: var(--brand); background: var(--brand-bg-light); }',
    '#ovPage .ov-insight .session .t { margin-bottom: 2px; font-size: 12.5px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }',
    '#ovPage .ov-insight .session .m { display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: var(--muted); }',
    '#ovPage .ov-insight .session .m b { color: var(--text); font-weight: 600; }',
    /* 会话档案专属紧凑化（信息一项不减，只收紧行距/间隙/内边距）+ 卡内滚动：
       实测原样 15 条 = 单块 >1000px，把中列拉长到整屏装不下；这里只动这一块。 */
    '#ovPage .ov-insight .session { padding: 6px 9px; gap: 9px; }',
    '#ovPage .ov-insight .session .t { font-size: 12px; line-height: 1.25; margin-bottom: 1px; }',
    '#ovPage .ov-insight .session .m { gap: 3px 9px; line-height: 1.25; }',
    '#ovPage .ov-insight .sess-scroll { max-height: 420px; overflow-y: auto; padding-right: 2px; }',
    '#ovPage .ov-insight .sess-scroll .stack { gap: 7px; }',
    /* 滚动条样式与仓库既有内滚动容器同款（short-line .sl-scroll / 消息列表） */
    '#ovPage .ov-insight .sess-scroll::-webkit-scrollbar { width: 5px; }',
    '#ovPage .ov-insight .sess-scroll::-webkit-scrollbar-track { background: transparent; }',
    '#ovPage .ov-insight .sess-scroll::-webkit-scrollbar-thumb { background: rgba(17, 24, 39, .16); border-radius: 3px; }',
    '#ovPage .ov-insight .sess-scroll::-webkit-scrollbar-thumb:hover { background: rgba(17, 24, 39, .32); }'
  ].join('\n');

  function ensureInsightStyle() {
    if (document.getElementById('ovInsightStyle')) return;
    var el = document.createElement('style');
    el.id = 'ovInsightStyle';
    el.textContent = INSIGHT_CSS;
    document.head.appendChild(el);
  }

  var ovInsight = function (html) { ensureInsightStyle(); return '<div class="ov-insight">' + html + '</div>'; };

  /* ── 空值口径（铁律 3）：缺失一律显式文案，绝不画成 0 / NaN / 破折号 ── */

  var NOT_COLLECTED = '未采集';

  /* 百分比（入参是 0-1 比值）。null（空区间 / 分母为 0）必须有明确分支 ——
     铁律 1：缓存命中率 None 时显示「未采集」，不得显示 0% / NaN / —% */
  function pct(v, digits) {
    var n = num(v);
    if (n === null) return NOT_COLLECTED;
    return (n * 100).toFixed(digits === undefined ? 1 : digits) + '%';
  }
  /* 成本三态（AGENTS.md：免费 ≠ 未计价）：None=未计价 / 0=免费 / >0=金额 */
  function costLabel(v) {
    var n = num(v);
    if (n === null) return '未计价';
    if (n === 0) return '¥0（免费）';
    return fmtCost(n, '元');
  }
  /* fmtTok 的缺失分支（这里 0 与 None 语义不同，不能混） */
  function tokLabel(v) {
    var n = num(v);
    return n === null ? NOT_COLLECTED : fmtTok(n);
  }

  /* ── 日历日工具：只按 Y/M/D 运算，不经 Date 解析字符串（避免时区/夏令时漂移） ── */

  function dayFromIso(iso) {
    var p = String(iso || '').split('-');
    if (p.length !== 3) return null;
    var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
    return isNaN(d.getTime()) ? null : d;
  }
  function isoOfDay(d) {
    var p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }
  function addDays(d, n) { var x = new Date(d.getTime()); x.setDate(x.getDate() + n); return x; }
  /* 日线标签：浏览器本地 today - (n-1-i)。桶键是 store 的**后端本地**日（本页是本地单机工具，
     浏览器与后端同机同时区）。⚠️ 绝不能用 range.since 推 —— 会整体错一天。 */
  function localDayLabels(n) {
    var out = [], d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - (n - 1));
    for (var i = 0; i < n; i++) {
      out.push(String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'));
      d = addDays(d, 1);
    }
    return out;
  }

  /* ── 上下文洞察：KPI 带 ── */

  function ovKpiBand(items) {
    return '<div class="grid g-kpi" style="margin-bottom:14px">' + items.map(function (it) {
      var cls = it[3] ? ' ' + it[3] : '';
      var sub = (it[2] === undefined || it[2] === null || it[2] === '') ? ''
        : '<div class="k-s">' + esc(it[2]) + '</div>';
      return '<div class="kpi"><div class="k-l">' + esc(it[0]) + '</div>'
        + '<div class="k-v' + cls + '">' + it[1] + '</div>' + sub + '</div>';
    }).join('') + '</div>';
  }

  /* ── 上下文洞察：近 N 天 token × 费用双柱 ──
     daily_tokens / daily_cost 是后端定长分桶（缺日补 0 = 那天确实没有调用，是实测 0，
     不是未采集 —— 不许改成「未采集」把柱子打断）。
     daily_service_cost 是**另一口径**（按次计费服务）：只进图例与 tooltip，不叠进费用柱，
     否则「费用」这根柱子的含义会随是否用过搜索而变。 */

  function ovDailyBars(tokens, costs, svcCosts) {
    var tk = (tokens || []).map(function (v) { var n = num(v); return n === null ? 0 : n; });
    if (!tk.length) return emptyState('暂无日线用量');
    var hasCost = Array.isArray(costs) && costs.length > 0;
    var cs = tk.map(function (_, i) {
      var n = hasCost ? num(costs[i]) : 0;
      return n === null ? 0 : n;
    });
    var hasSvc = Array.isArray(svcCosts) && svcCosts.length > 0;
    var ss = tk.map(function (_, i) {
      var n = hasSvc ? num(svcCosts[i]) : 0;
      return n === null ? 0 : n;
    });
    var tkMax = Math.max.apply(null, tk);
    var csMax = hasCost ? Math.max.apply(null, cs) : 0;
    var ssMax = hasSvc ? Math.max.apply(null, ss) : 0;
    var labels = localDayLabels(tk.length);
    var stride = Math.ceil(tk.length / 7);
    var bars = tk.map(function (v, i) {
      var th = tkMax > 0 ? v / tkMax * 100 : 0;
      var ch = csMax > 0 ? cs[i] / csMax * 100 : 0;
      var tip = labels[i] + ' · Token ' + fmtTok(v) + ' · 费用 ' + (hasCost ? '¥' + fmt(cs[i], 4) : NOT_COLLECTED);
      if (hasSvc) tip += ' · 搜索/抓取 ¥' + fmt(ss[i], 4);
      return '<div class="d" title="' + esc(tip) + '">'
        + '<i class="tk" style="height:' + th.toFixed(2) + '%"></i>'
        + (hasCost ? '<i class="cs" style="height:' + ch.toFixed(2) + '%"></i>' : '') + '</div>';
    }).join('');
    var xs = labels.map(function (lb, i) {
      /* 末位对齐（i % stride === stride-1）：保证最后一天一定有标签，且不与前一个标签相邻 */
      return '<span>' + ((i % stride === stride - 1 || i === tk.length - 1) ? esc(lb) : '') + '</span>';
    }).join('');
    var costPeak = !hasCost ? NOT_COLLECTED
      : (csMax > 0 ? '¥' + fmt(csMax, 4) : '¥0（区间内无已计价金额）');
    var svcPeak = !hasSvc ? NOT_COLLECTED
      : (ssMax > 0 ? '¥' + fmt(ssMax, 4) : '¥0（区间内无搜索/抓取）');
    return '<div style="margin-bottom:10px">'
      + '<div class="small" style="color:var(--muted);margin-bottom:6px">近 ' + tk.length + ' 天用量　'
      + '<span style="color:var(--brand)">■ Token 峰值 ' + fmtTok(tkMax) + '</span>　'
      + '<span style="color:var(--warning)">■ 费用 峰值 ' + costPeak + '</span>'
      + (hasSvc ? '　<span style="color:var(--text-secondary)">■ 搜索/抓取 峰值 ' + svcPeak + '</span>' : '')
      + '</div>'
      + '<div class="bars7">' + bars + '</div>'
      + '<div class="bars7-x">' + xs + '</div></div>';
  }

  /* ── 上下文洞察：Token 构成环形 ──
     输出 = 思考 + 回答，两者合计即输出（思考**不再**额外相加，否则输出被重复计数）。
     ⚠️ 跨会话聚合没有输入侧六分类（composition 只存在于单 run 快照），故只画输出段，
        输入侧构成显式标「未采集」，绝不拿模型分布冒充分类构成。 */

  function ovCompositionRing(payload) {
    var kpi = payload.kpi || {};
    var think = num(kpi.reasoning_tokens), ans = num(kpi.answer_tokens);
    if (think === null && ans === null) return '';
    think = think === null ? 0 : think;
    ans = ans === null ? 0 : ans;
    var out = think + ans;
    var C = 2 * Math.PI * 54, acc = 0;
    var SEG = [
      { nm: '思考（含在输出内）', v: think, c: 'var(--insight-think)' },
      { nm: '回答（正文）', v: ans, c: 'var(--insight-answer)' }
    ];
    var arcs = out > 0 ? SEG.map(function (s) {
      var len = C * s.v / out;
      var one = '<circle cx="66" cy="66" r="54" fill="none" stroke="' + s.c + '" stroke-width="15"'
        + ' stroke-dasharray="' + len.toFixed(2) + ' ' + (C - len).toFixed(2) + '"'
        + ' stroke-dashoffset="' + (-acc).toFixed(2) + '"/>';
      acc += len;
      return one;
    }).join('') : '';
    var rows = SEG.map(function (s) {
      var p = out > 0 ? s.v / out * 100 : null;
      return '<div class="li"><i class="dot" style="background:' + s.c + '"></i>'
        + '<span class="nm">' + esc(s.nm) + '</span>'
        + '<span class="vl">' + fmtTok(s.v) + '</span>'
        + '<span class="pc">' + (p === null ? NOT_COLLECTED : p.toFixed(1) + '%') + '</span></div>';
    }).join('');
    rows += '<div class="li"><i class="dot" style="background:var(--border)"></i>'
      + '<span class="nm" title="输出 = 思考 + 回答（思考含在输出内，不重复相加）">输出合计（思考 + 回答）</span>'
      + '<span class="vl">' + fmtTok(out) + '</span>'
      + '<span class="pc">' + (out > 0 ? '100.0%' : NOT_COLLECTED) + '</span></div>';
    var mid = '<div class="ring-mid"><div class="v">' + fmtTok(out) + '</div>'
      + '<div class="l">输出 Token</div><div class="r">'
      + (out > 0 ? '思考 ' + (think / out * 100).toFixed(1) + '%' : NOT_COLLECTED) + '</div></div>';
    var models = (payload.by_model || []).map(function (m) {
      var pair = m || [];
      return [String(pair[0] === undefined || pair[0] === null ? '' : pair[0]), num(pair[1])];
    }).filter(function (m) { return m[0] !== '' && m[1] !== null; });
    var modelHtml = '';
    if (models.length) {
      var mMax = Math.max.apply(null, models.map(function (m) { return m[1]; })) || 1;
      modelHtml = '<div style="margin-top:8px">'
        + '<div class="small" style="color:var(--muted);margin-bottom:3px">按模型（计费 Token 占比）</div>'
        + models.slice(0, 4).map(function (m) {
          return '<div class="theme-line"><span class="nm" style="flex-basis:96px" title="' + esc(m[0]) + '">'
            + esc(m[0]) + '</span><span class="theme-bar"><i style="width:' + (m[1] / mMax * 100).toFixed(0)
            + '%"></i></span><span class="n" style="flex-basis:56px">' + fmtTok(m[1]) + '</span></div>';
        }).join('') + '</div>';
    }
    return '<div style="border-top:1px dashed var(--border);padding-top:9px">'
      + '<div class="small" style="color:var(--muted);margin-bottom:8px">Token 构成 · 输出段'
      + '（输入侧六分类是单 run 快照口径，跨会话未采集）</div>'
      + '<div class="ring-wrap"><div class="ring">'
      + '<svg width="132" height="132" viewBox="0 0 132 132">'
      + '<circle cx="66" cy="66" r="54" fill="none" stroke="var(--border-light)" stroke-width="15"/>'
      + arcs + '</svg>' + mid + '</div>'
      + '<div class="legend">' + rows + '</div></div>'
      + modelHtml + '</div>';
  }

  /* ── 活跃度：8 周 × 7 天热力图 ──
     日期来自 calendar_query（对 UTC 时间戳 strftime）→ **UTC 日**，与 usage 的本地日日线
     不是同一个「日」：本块只用自己的日期做定位与 tooltip，禁止与日线共轴或互相标注。 */

  function ovHeatmap(days) {
    var rows = (days || []).filter(function (d) { return d && d.date; });
    if (!rows.length) return emptyState('暂无活跃度数据（近 8 周月库无 run 记录）');
    var byDate = {}, max = 0;
    rows.forEach(function (d) {
      var v = num(d.tokens);
      v = v === null ? 0 : v;
      byDate[String(d.date)] = v;
      if (v > max) max = v;
    });
    var keys = Object.keys(byDate).sort();
    var last = dayFromIso(keys[keys.length - 1]);
    if (!last) return emptyState('活跃度日期不可解析');
    var start = addDays(last, -(((last.getDay() + 6) % 7) + 49));   /* 该日所在周的周一往前 7 周 */
    var WD = ['一', '二', '三', '四', '五', '六', '日'];
    var head = '<span class="w"></span>';
    for (var w = 0; w < 8; w++) head += '<span class="hd">W' + (w + 1) + '</span>';
    var cells = '';
    for (var wd = 0; wd < 7; wd++) {
      cells += '<span class="w">' + WD[wd] + '</span>';
      for (var w2 = 0; w2 < 8; w2++) {
        var key = isoOfDay(addDays(start, w2 * 7 + wd));
        var known = Object.prototype.hasOwnProperty.call(byDate, key);
        var v2 = known ? byDate[key] : 0;
        var lv = 0;
        if (known && v2 > 0 && max > 0) {
          var rr = v2 / max;
          lv = rr >= 0.7 ? 4 : rr >= 0.45 ? 3 : rr >= 0.2 ? 2 : 1;
        }
        var sel = (known && state.sessionFilterDate === key) ? ' sel' : '';
        var tip = key + '（UTC 日）· ' + (known ? fmtTok(v2) + ' token' : '无 run');
        cells += '<i class="' + (known ? 'l' + lv : 'empty') + sel + '" data-heat="' + key + '"'
          + (known ? '' : ' data-heat-none="1"') + ' title="' + esc(tip) + '"></i>';
      }
    }
    var on = state.sessionFilterDate;
    var hint = on
      ? '已按 <b class="mono">' + esc(on) + '</b>（UTC 日）过滤下方会话 '
        + '<span class="chip on" data-heat-clear="1">清除筛选 ✕</span>'
      : '点某天过滤下方会话档案（日 = UTC 日）';
    return '<div class="small" style="color:var(--muted);margin-bottom:6px">近 8 周活跃度 · ' + hint + '</div>'
      + '<div class="heat">' + head + cells + '</div>';
  }

  /* ── 会话上下文档案：卡片列表 + 搜索 + 排序（按热力图选中的 UTC 日过滤） ──

     ⚠️ 时间口径必须**同源**：热力图格子是 UTC 日（calendar_query 直接对 UTC 时间戳 strftime，
     本任务不改），过滤也按 UTC 日匹配，所以卡面日期**也**用同一个表达式 utcDayOf() 取值 ——
     否则北京时间 00:00–08:00 创建的会话（UTC 日 = 本地日 − 1）会出现「点 10-14 却看到卡面写
     10-15 00:03」，用户会以为筛错了。选定做法 (a)：卡面显示 UTC 日，本地时间留在 tooltip。 */

  function utcDayOf(s) { return String(s || '').slice(0, 10); }

  function sessionList(base) {
    var data = (state.blocks.sessions && state.blocks.sessions.data) || {};
    var list = (base || data.sessions || []).slice();
    if (state.sessionFilterDate) {
      list = list.filter(function (s) {
        /* 与卡面 utcDayOf(s.created_at) 是同一个表达式 —— 点哪一格，卡面就写哪一天 */
        return utcDayOf(s.created_at) === state.sessionFilterDate;
      });
    }
    var q = String(state.sessionQuery || '').trim().toLowerCase();
    if (q) {
      list = list.filter(function (s) {
        return (String(s.title || '') + ' ' + String(s.agent_mode || '')).toLowerCase().indexOf(q) >= 0;
      });
    }
    if (state.sessionOrder === 'token') {
      list.sort(function (a, b) {
        var av = num(a.billed_tokens), bv = num(b.billed_tokens);
        if (av === null && bv === null) return 0;
        if (av === null) return 1;
        if (bv === null) return -1;
        return bv - av;
      });
    } else {
      list.sort(function (a, b) {
        return String(b.created_at || '').localeCompare(String(a.created_at || ''));
      });
    }
    return list;
  }

  /* 迷你环：思考 / 回答 两段（与大环 ovCompositionRing **同一语义、同一组色**：靛 / 青）。
     几何取原型 section#skinB 的 .mini-ring：r=17、周长 2πr≈106.8、stroke-width=6（44×44 viewBox），
     轨道 --border-light；两段用 dasharray + 逐段累加的负 dashoffset 首尾相接，均从 rotate(-90 22 22) 起。
     ⚠️ 铁律 3：output_tokens 未采集（null）时**只画轨道**，绝不画 0 长度段 ——
        小环上一段 0 长度弧等于「看不见」，会被读成「回答极少」，那是拿缺失数据编结论。 */
  function sessionRing(s, split) {
    var C = 2 * Math.PI * 17;
    var segs = '';
    if (split) {
      var acc = 0;
      /* 顺序：思考（靛）在前、回答（青）在后，与大环同向 */
      [[split.think, 'var(--insight-think)'], [split.answer, 'var(--insight-answer)']].forEach(function (seg) {
        var len = C * seg[0] / split.out;
        if (len > 0) {
          segs += '<circle cx="22" cy="22" r="17" fill="none" stroke="' + seg[1] + '" stroke-width="6"'
            + ' stroke-dasharray="' + len.toFixed(2) + ' ' + (C - len).toFixed(2) + '"'
            + ' stroke-dashoffset="' + (-acc).toFixed(2) + '"'
            + ' transform="rotate(-90 22 22)"/>';
        }
        acc += len;
      });
    }
    var tip = split
      ? '思考 ' + fmtTok(split.think) + ' / 回答 ' + fmtTok(split.answer) + '（占输出）'
      : '输出 Token ' + NOT_COLLECTED + '：只画轨道';
    return '<div class="ring mini-ring" title="' + esc(tip) + '">'
      + '<svg width="44" height="44" viewBox="0 0 44 44">'
      + '<circle cx="22" cy="22" r="17" fill="none" stroke="var(--border-light)" stroke-width="6"/>'
      + segs + '</svg></div>';
  }

  function sessionRows(list) {
    return list.map(function (s) {
      var billed = num(s.billed_tokens), reason = num(s.reasoning_tokens);
      /* 思考 / 回答 的**规范口径**（context_stats.py 铁律 2）：输出 = 思考 + 回答，
         thinking = min(reasoning, output)，answer = output − thinking（两者恒等于输出）。
         ⚠️ 分母是**输出**，不是计费 —— 原实现用 reason/billed 属于拿错分母。
         output_tokens / reasoning_tokens 任一为 null（未采集）时拒绝拆段：
         宁可写「未采集」，也不把缺失当 0 去暗示「回答极少」。 */
      var outTok = num(s.output_tokens);
      var split = (outTok !== null && outTok > 0 && reason !== null)
        ? { out: outTok, think: Math.min(reason, outTok) } : null;
      if (split) split.answer = split.out - split.think;
      var share = split ? split.think / split.out * 100 : null;
      /* 卡面日期 = 过滤依据（同一个 utcDayOf）；命中时高亮，用户点完能一眼对上 */
      var day = utcDayOf(s.created_at);
      var hit = day !== '' && day === state.sessionFilterDate;
      var dayTip = day === '' ? '创建时间未记录'
        : '创建（UTC）' + day + ' ' + String(s.created_at).slice(11, 16) + ' · 本地 ' + fmtIso(s.created_at);
      return '<div class="session"' + (s.dialog_uuid ? ' data-nav="dialog:' + esc(s.dialog_uuid) + '"' : '')
        + ' title="点击打开该会话">' + sessionRing(s, split)
        + '<div><div class="t">' + esc(s.title || '未命名') + '</div><div class="m">'
        + '<span>' + esc(s.agent_mode || '未知模式') + '</span>'
        + '<span>计费 <b>' + tokLabel(billed) + '</b></span>'
        + '<span>思考 <b>' + (split ? fmtTok(split.think) : tokLabel(reason)) + '</b></span>'
        + '<span>回答 <b>' + (outTok === null ? NOT_COLLECTED
          : (split ? fmtTok(split.answer) : tokLabel(outTok))) + '</b></span>'
        + '<span>占输出 <b>' + (share === null ? NOT_COLLECTED : share.toFixed(1) + '%') + '</b></span>'
        + '<span>' + costLabel(s.cost) + '</span>'
        + '</div></div>'
        + '<div class="small" style="white-space:nowrap;text-align:right;color:'
        + (hit ? 'var(--brand);font-weight:700' : 'var(--muted)') + '" title="' + esc(dayTip) + '">'
        + (day === '' ? dash : esc(day.slice(5)) + ' UTC') + '</div>'
        + '</div>';
    }).join('');
  }

  /* ── 会话条数的上限口径 ──
     这个数来自 list_dialogs(limit=200)：超过 200 个会从尾部静默截断，所以**不能**写成会被
     读成总数的文案（与 KPI 的 runs 用「200+」同一个理由，铁律 3）。 */

  function sessCountLabel(n) {
    return n >= 200 ? '200+ 条（后端上限 200）' : n + ' 条（后端上限 200）';
  }

  /* 列表体（只含卡片）—— 放进 .sess-scroll 内滚动，分页条另置容器外固定不滚。
     页长走 SESSION_PAGE_SIZE（默认 8 条），「展开全部」也仍在这个滚动容器里。 */
  function sessionBodyHtml(base) {
    var list = sessionList(base);
    var page = pageSlice('sessions', list, SESSION_PAGE_SIZE);
    return page.length ? '<div class="stack">' + sessionRows(page) + '</div>'
      : emptyState(state.sessionFilterDate
        ? '该 UTC 日（' + state.sessionFilterDate + '）没有会话创建记录'
        : '暂无会话记录');
  }

  function sessionPagerHtml(base) {
    var total = sessionList(base).length;
    return pagerHtml('sessions', total, sessCountLabel(total), SESSION_PAGE_SIZE);
  }

  /* ⚠️ 后端按 limit=200 取最近更新的对话，超过 200 个会从尾部静默截断 ——
     故不写「共 N 个会话」，只报本次取到的条数并在 tooltip 里说明上限。 */
  function sessionInfoHtml(total, days) {
    var bits = [];
    var d = num(days);
    var label = sessCountLabel(total);
    bits.push(d === null ? '匹配 ' + label : '近 ' + d + ' 天更新 ' + label);
    if (state.sessionFilterDate) bits.push('按 ' + state.sessionFilterDate + '（UTC 日）过滤');
    if (state.sessionQuery) bits.push('搜索「' + state.sessionQuery + '」');
    return '<span title="后端按 limit=200 取最近更新的对话，超过 200 个时会从尾部截断 ——'
      + ' 这里不是总会话数">' + esc(bits.join(' · ')) + '</span>';
  }

  function ovSessionCards(list, days) {
    var chips = [['recent', '最近'], ['token', '计费 Token']].map(function (c) {
      return '<span class="chip' + (state.sessionOrder === c[0] ? ' on' : '') + '" data-sess-order="' + c[0] + '">'
        + c[1] + '</span>';
    }).join('');
    return '<div class="toolbar"><input class="searchbox" id="ovSessSearch" type="search"'
      + ' placeholder="搜索标题 / 模式 …" value="' + esc(state.sessionQuery || '') + '">' + chips + '</div>'
      + '<div class="small" id="ovSessInfo" style="color:var(--muted);margin:6px 0 8px">'
      + sessionInfoHtml(sessionList(list).length, days) + '</div>'
      + '<div class="sess-scroll"><div id="ovSessList">' + sessionBodyHtml(list) + '</div></div>'
      + '<div id="ovSessPager">' + sessionPagerHtml(list) + '</div>';
  }

  function refreshSessionList() {
    var box = document.getElementById('ovSessList');
    if (!box) { renderBlock('sessions'); return; }
    box.innerHTML = sessionBodyHtml();
    /* 分页条在滚动容器外：页长/页号都会影响它，必须与列表同批刷新 */
    var pagerBox = document.getElementById('ovSessPager');
    if (pagerBox) pagerBox.innerHTML = sessionPagerHtml();
    var info = document.getElementById('ovSessInfo');
    if (info) {
      var data = (state.blocks.sessions && state.blocks.sessions.data) || {};
      info.innerHTML = sessionInfoHtml(sessionList().length, data.days);
    }
  }

  /* ── 三个块的渲染入口（payload 就是后端形状，无需适配层） ── */

  function renderUsage(payload) {
    var kpi = payload.kpi || {};
    var rng = payload.range || {};
    var days = num(rng.days);
    var runs = num(kpi.runs);
    var hit = num(kpi.cache_hit_ratio);
    var think = num(kpi.reasoning_tokens);
    var ansTok = num(kpi.answer_tokens);
    var out = (think === null ? 0 : think) + (ansTok === null ? 0 : ansTok);
    /* 铁律 3：runs 受后端 limit=200 约束，不是「区间内总运行数」—— 打满时必须显示 200+ */
    var runsText = runs === null ? dash : (runs >= 200 ? '200+' : String(runs));
    var runsSub = runs === null ? ''
      : (runs >= 200 ? '个（后端上限 200，真实值更大）'
        : '个' + (days === null ? '' : ' · 近 ' + days + ' 天'));
    var shareSub = (out > 0 && think !== null) ? '占输出 ' + pct(think / out) : '占输出 ' + NOT_COLLECTED;
    /* 按次计费服务（DeepSeek Search/Fetch）：与「计费 Token」并列的**独立口径**卡片。
       ⚠️ 三态必须分成三种显示，不能合并（否则「没用过」会被读成「不要钱」）：
         service_calls 缺字段（老 payload） -> 未采集
         service_calls === 0               -> 区间内未使用（不是未计价、也不是 ¥0）
         有调用但 service_cost 为 null      -> 未计价（价目里没有该服务） */
    var svcCalls = num(kpi.service_calls);
    var svcUnpriced = num(kpi.service_unpriced_calls);
    var svcVal = svcCalls === null ? NOT_COLLECTED
      : (svcCalls === 0 ? dash : costLabel(kpi.service_cost));
    var svcSub = svcCalls === null ? '未采集'
      : (svcCalls === 0 ? '区间内未使用'
        : svcCalls + ' 次按次计费'
          + (svcUnpriced ? '（' + svcUnpriced + ' 次未计价）' : ''));
    return ovInsight(ovKpiBand([
      ['活跃会话', runsText, runsSub, ''],
      /* 铁律 2：未计价与免费必须分开显示 */
      ['计费 Token', fmtTok(kpi.billed_tokens), costLabel(kpi.cost), ''],
      ['搜索/抓取', svcVal, svcSub, ''],
      /* 铁律 1：空区间 / 分母为 0 → 未采集（不是 0%） */
      ['缓存命中', pct(hit), hit === null ? '未采集（区间内无输入 token）' : '占输入 token',
        hit === null ? 'c-muted' : 'c-ok'],
      ['思考 Token', tokLabel(kpi.reasoning_tokens), shareSub, '']
    ])
      + ovDailyBars(payload.daily_tokens || [], payload.daily_cost || [],
        payload.daily_service_cost || [])
      + ovCompositionRing(payload));
  }

  function renderActivity(payload) { return ovInsight(ovHeatmap(payload.days || [])); }
  function renderSessions(payload) { return ovInsight(ovSessionCards(payload.sessions || [], payload.days)); }


  /* ════════════════════ 数据适配层：真实 SSE payload → 原型渲染器字段 ════════════════════ */

  const LEVEL_CLS = { ice: 'lv-cold', ebb: 'lv-cold', warm: 'lv-warm', hot: 'lv-hot' };
  const VERDICT_CLS = (v) => {
    const s = String(v || '');
    if (s.indexOf('不利') >= 0) return 'st-head';
    if (s.indexOf('左侧') >= 0 || s.indexOf('布局') >= 0) return 'st-left';
    if (s.indexOf('有利') >= 0 || s.indexOf('顺风') >= 0) return 'st-tail';
    return 'st-mid';
  };

  function adaptMarket(d) {
    const sent = d.sentiment || null;
    const b = d.breadth || null;
    const dist = (b && b.dist) || {};
    const up = b ? num(b.up) : null, down = b ? num(b.down) : null, flat = b ? num(b.flat) : null;
    let total = b ? num(b.total) : null;
    if (total === null && b && (up !== null || down !== null || flat !== null)) total = (up || 0) + (down || 0) + (flat || 0);
    const amount = b && num(b.amount) !== null ? b.amount / 1e8 : null;
    const prevAmount = b && num(b.prev_amount) !== null ? b.prev_amount / 1e8 : null;
    const flow = b && num(b.main_net_inflow) !== null ? b.main_net_inflow / 1e8 : null;
    return {
      score: sent ? num(sent.score) : null,
      level: sent ? (sent.level_label || sent.level || dash) : dash,
      levelCls: sent ? (LEVEL_CLS[sent.level] || 'lv-warm') : 'lv-cold',
      up, down, flat, total,
      brokenRate: sent && num(sent.break_rate) !== null ? sent.break_rate * 100 : null,
      amount, prevAmount, flow,
      amountDelta: (amount !== null && prevAmount) ? (amount / prevAmount - 1) * 100 : null,
      limitUp: sent && num(sent.limit_up_count) !== null ? sent.limit_up_count : num(dist.limit_up),
      broken: sent ? num(sent.broken_count) : null,
      limitDown: sent && num(sent.limit_down_count) !== null ? sent.limit_down_count : num(dist.limit_down),
      hasBreadth: !!(b && (b.up != null || b.amount != null || b.total != null)),
      sentiment_history: Array.isArray(d.sentiment_history)
        ? d.sentiment_history.map(v => num(v)).filter(v => v !== null) : [],
    };
  }

  function adaptShortline(d) {
    const snap = d.snapshot || null;
    if (!snap || !snap.summary) return { hasData: false, themes: [], ladder: [], staleDate: '' };
    const sum = snap.summary;
    const boards = ((snap.ladder || [])[0] || {}).boards || [];
    const byNum = {};
    boards.forEach(b => { const k = Number(b.board_num) || 0; (byNum[k] = byNum[k] || []).push(b); });
    const ladder = Object.keys(byNum).map(Number).sort((a, b) => b - a).map(k => [`${k}板`, byNum[k].length]);
    const maxBoard = num(sum.max_board);
    const topNames = maxBoard === null ? []
      : boards.filter(b => Number(b.board_num) === maxBoard).slice(0, 2).map(b => b.name).filter(Boolean);
    const prem = sum.yesterday_premium || {};
    const promo = snap.promo || [];
    const deep = promo.find(p => String(p.node || '').indexOf('2->') === 0);
    return {
      hasData: true,
      limitUp: num(sum.limit_up_count), broken: num(sum.broken_count), limitDown: num(sum.limit_down_count),
      ladderN: boards.length,
      maxBoard, maxBoardName: topNames.join('、'),
      premium: num(prem.mean),
      promo12: num(sum.promotion_rate) !== null ? sum.promotion_rate * 100 : null,
      promoDeep: deep && num(deep.rate) !== null ? deep.rate * 100 : null,
      themes: (snap.themes || []).slice(0, 5).map(t => [t.name, t.count]),
      ladder,
      staleDate: snap.stale ? (snap.stale_date || '') : '',
    };
  }

  function adaptThesis(d) {
    const alerts = d.alerts || [];
    const items = (d.items || []).map(t => {
      const total = num(t.total);
      const st = t.stages || {};
      const full = (st.drive_full != null && st.pos_full != null)
        ? (st.drive_full + st.pos_full)
        : (t.factor_count || 5) * 2;
      let hits = [];
      const sig = t.signals;
      if (Array.isArray(sig)) {
        hits = sig.map(x => typeof x === 'string' ? x : (x && (x.label || x.name)) || '').filter(Boolean);
      } else if (sig && typeof sig === 'object') {
        const pool = Array.isArray(sig.buy) ? sig.buy
          : Object.keys(sig).reduce((a, k) => a.concat(Array.isArray(sig[k]) ? sig[k] : []), []);
        hits = pool.filter(x => x && x.hit !== false).map(x => x.label || x.name || '').filter(Boolean);
      }
      const signals = hits.slice(0, 4);
      return {
        thesis_id: t.thesis_id,
        nm: t.name || t.thesis_id,
        st: VERDICT_CLS(t.verdict),
        stage: t.verdict || dash,
        score: total === null ? null : (total > 0 ? '+' + total : String(total)),
        full,
        trend: (t.history || []).map(h => num(h.total)).filter(v => v !== null),
        factors: (t.factors || []).slice(0, 3).map(f => [
          f.name || f.label || f.key || '',
          num(f.score),
          f.value == null ? '数据缺失' : `${fmtVal(f.value)}${f.unit || ''}`,
        ]),
        signals,
        signalMore: Math.max(0, hits.length - signals.length),
        alert: alerts.filter(a => a && a.thesis_id === t.thesis_id).length,
        up: (t.trade_date || '').slice(5) || dash,
      };
    });
    return { items };
  }

  function adaptPortfolio(d) {
    /* 仅总览页过滤清仓行（qty=0）：环中心计数/比例、表格、今日盈亏求和都用过滤后集合 */
    const items = (d.items || []).filter(x => (x.qty || 0) > 0).map(p => ({
      code: p.stock_code, stock_code: p.stock_code, nm: p.stock_name || p.stock_code,
      price: num(p.price), pct: num(p.pct),
      value: num(p.market_value), today: num(p.today_pnl),
      floatPnl: num(p.float_pnl),
      trend: (state.scan && state.scan.trends && state.scan.trends[p.stock_code]) || null,
    }));
    const s = d.summary || {};
    const mv = num(s.total_market_value);
    const cost = num(s.total_cost);
    const todayVals = items.map(x => x.today).filter(v => v !== null);
    const floatPnl = num(s.total_float_pnl);
    const acc = d.account || null;
    /* 今日盈亏优先用券商账户级数值：页面上只列 qty>0 的行，今天卖掉的标的
       不在 items 里，但对当日盈亏有贡献（已实现）。逐行求和会偏小
       （2026-11-06 用户实测：券商 -509.69，逐行求和 -163.44）。
       逐行求和只在账户级缺失时兜底。 */
    const accToday = acc ? num(acc.today_pnl) : null;
    const today = accToday !== null
      ? accToday
      : (todayVals.length ? todayVals.reduce((a, b) => a + b, 0) : null);
    const totalAsset = acc ? num(acc.total_asset) : null;
    const posMv = acc ? num(acc.positions_market_value) : null;
    return {
      items,
      summary: {
        mv,
        today,
        todayPct: (today !== null && mv !== null && mv - today > 0) ? today / (mv - today) * 100 : null,
        floatPnl,
        floatPct: (floatPnl !== null && cost) ? floatPnl / cost * 100 : null,
        posPct: (totalAsset && posMv !== null) ? posMv / totalAsset * 100 : null,
        avail: acc ? num(acc.avail_cash) : null,
      },
      account: acc ? {
        syncAt: acc.synced_at || acc.snapshot_at || dash,
        note: acc.market_status ? `${acc.market_status}快照` : '东财同步',
      } : null,
    };
  }

  function adaptWatchlist(d) {
    return {
      items: (d.items || []).map(x => ({
        code: x.stock_code, stock_code: x.stock_code, nm: x.stock_name || x.stock_code,
        price: num(x.price), pct: num(x.change_pct),
        trend: (state.scan && state.scan.trends && state.scan.trends[x.stock_code]) || null,
      })),
    };
  }

  function adaptMacro(d) {
    const items = (d.items || []).map(m => {
      const delta = num(m.delta);
      return {
        c: m.code || '',
        l: m.label || m.code || '',
        v: m.value == null ? dash : fmtVal(m.value),
        u: m.unit || '',
        dp: m.delta_display != null ? String(m.delta_display) : dash,
        cls: delta === null ? 'v-flat' : (delta > 0 ? 'v-up' : delta < 0 ? 'v-down' : 'v-flat'),
        dt: m.report_date || dash,
        sp: Array.isArray(m.series) ? m.series.filter(x => num(x) !== null) : [],
        stale: m.stale ? `陈旧 ${m.stale_days} 天` : '',
      };
    });
    const missingCodes = d.missing || [];
    return { items, missing: missingCodes.length, missingCodes };
  }

  function adaptQuality(d) {
    const wr = d.win_rate_after_cost || {};
    const cost = d.avg_cost_per_report || {};
    const pct = (v) => (num(v) === null ? null : v * 100);
    /* 缓存命中：A8 语义缓存优先，缺失回退本轮 trace token 口径（均 0-1 ratio） */
    const a8 = num(d.cache_hit_rate);
    const tok = num(d.token_cache_hit_rate);
    return {
      winrate: pct(wr.value),
      cacheHit: pct(a8 !== null ? a8 : tok),
      cacheSrc: a8 !== null ? 'a8' : (tok !== null ? 'token' : null),
      conclusions: num(d.verifiable_conclusions),
      wow: num(d.wow_pct),
      cost: num(cost.value),
      costUnit: cost.unit || '',
      trend: (d.trend || []).map(t => num(t.count)).filter(v => v !== null),
      week: d.week || '',
    };
  }

  function adaptReview(d) {
    const ev = d.eval || {};
    const latest = ev.latest || null;
    const trend = (ev.trend || []).map(x => num(x.avg_score)).filter(v => v !== null).reverse();
    const sig = d.signal || {};
    const cal = d.calibration || {};
    const rv = d.reviews || {};
    const rrun = rv.run || {}, rdlg = rv.dialog || {};
    // 信号校准用固定 5 日口径：旧 buy_winrate/buy_count 是「至今」，胜率不回退，
    // 否则会把至今胜率冒充 5 日胜率（仅样本数在 n_5d 缺失时回退 buy_count）。
    const sigN5 = num(sig.buy_n_5d);
    const sigWin5 = num(sig.buy_winrate_5d);
    return {
      hasEval: !!latest,
      evalName: latest ? (latest.name || latest.id || '') : '',
      evalScore: latest ? num(latest.overall_score) : null,
      evalDelta: trend.length >= 2 ? trend[trend.length - 1] - trend[trend.length - 2] : null,
      cases: latest ? `${latest.completed_cases ?? dash}/${latest.total_cases ?? dash}` : dash,
      evalAt: latest ? fmtIso(latest.created_at) : '',
      trend,
      runReviews: num(rrun.count) || 0, dlgReviews: num(rdlg.count) || 0,
      runAvg: num(rrun.avg_overall), dlgAvg: num(rdlg.avg_overall),
      rubric: rrun.rubric_version || rdlg.rubric_version || null,
      sigN: sigN5 !== null ? sigN5 : num(sig.buy_count),
      sigWin: sigWin5 !== null ? sigWin5 * 100 : null,
      exp: num(sig.expectancy),
      payoff: num(sig.payoff), ece: num(cal.ece),
      calib: (cal.buckets || []).map(b => [b.range, num(b.avg_pred) || 0, num(b.realized) || 0, b.n]),
    };
  }


  function adaptTodos(d) {
    const SEV = { critical: 'error', error: 'error', warn: 'warn', info: 'info' };
    return {
      items: (d.items || []).map(t => ({
        sev: SEV[t.severity] || 'info', tx: t.text || '', tm: t.time || dash, target: t.target || '',
      })),
    };
  }

  function adaptStorage(d) {
    const cl = d.cleanup || {};
    return {
      totalMb: num(d.total_mb), assets: num(d.assets), stale: num(d.stale),
      scanAt: fmtScanAt(d.scan_at),
      categories: (d.categories || []).map(c => [c.name || '', num(c.mb) || 0]),
      cleanupRules: num(cl.rules) || 0,
      cleanupCandidates: num(cl.items) || 0,
      cleanupMb: num(cl.size_bytes) === null ? dash : (cl.size_bytes / 1048576).toFixed(0),
      failedTasks: num(d.failed_tasks) || 0,
      queue: num(d.queue) || 0,
    };
  }

  function adaptRuntime(d) {
    if (!d.available) return { available: false };
    return {
      available: true,
      cpu: num(d.cpu_percent),
      cpuHist: Array.isArray(d.cpuHist) ? d.cpuHist.filter((v) => num(v) !== null) : [],
      memRssMb: num(d.mem_rss_mb), memSysPct: num(d.mem_sys_pct),
      threads: num(d.threads), uptime: d.uptime_display || dash,
      tasksRunning: num(d.tasks_running) || 0,
    };
  }

  /* 组合层：跨资产配比 + 共同风险。不是 thesis 定义，故单独一块 */
  function adaptThesisPortfolio(d) {
    if (d && d.ok === false) return { available: false, reason: d.reason || '读取失败' };
    var items = (d && d.items) || [];
    return {
      available: true,
      count: items.length,
      items: items.map(function (p) {
        if (p.ok === false) {
          return { ok: false, name: p.name || p.id || '未命名', reason: p.reason || '—' };
        }
        var band = p.band || {};
        var rk = p.risk || {};
        var hit = (p.risks || []).filter(function (r) { return r.triggered; });
        return {
          ok: true,
          name: p.name || p.id,
          total: num(p.combo_total),
          bandLabel: band.label || '未定档',
          weights: Object.keys(band.weights || {}).map(function (k) {
            var leg = (p.legs || []).filter(function (x) { return x.thesis_id === k; })[0] || {};
            return [leg.label || k, band.weights[k]];
          }),
          legs: (p.legs || []).map(function (l) {
            return { name: l.label || l.thesis_id, role: l.role || '', total: num(l.total),
                     verdict: l.verdict || '未刷新' };
          }),
          corr: rk.available ? num(rk.corr) : null,
          divPct: rk.available ? num(rk.diversification_pct) : null,
          upgrade: (p.bands_outline || {}).upgrade_gap || null,
          riskHit: hit.map(function (r) { return r.label || r.factor; }),
          riskPending: (p.risks || []).filter(function (r) { return !r.evaluable; })
                         .map(function (r) { return r.label || r.factor; }),
        };
      }),
    };
  }

  const ADAPTERS = {
    market: adaptMarket, shortline: adaptShortline, thesis: adaptThesis,
    portfolio: adaptPortfolio, watchlist: adaptWatchlist, macro: adaptMacro,
    quality: adaptQuality, review: adaptReview,
    todos: adaptTodos, storage: adaptStorage, runtime: adaptRuntime,
    thesis_portfolio: adaptThesisPortfolio,
  };
  function adapt(id, data) {
    const fn = ADAPTERS[id];
    return fn ? fn(data || {}) : (data || {});
  }

  /* ════════════════════ 渲染器（自原型逐段移植，数据入口经 adapt 归一） ════════════════════ */

  const R = {};

  R.market = (d) => {
    if (d.score === null && !d.hasBreadth) return emptyState('市场数据暂不可用（上游降级）');
    const total = d.total || 0;
    const p = (n) => (n !== null && total > 0 ? (n / total * 100).toFixed(1) : '0');
    return `
  <div style="display:flex;gap:16px;align-items:center;margin-bottom:9px">
    <div style="flex:0 0 auto">
      ${d.score === null ? '<div class="gauge" aria-hidden="true"></div>' : gaugeHtml(d.score)}
      <div class="gauge-zones"><span>冰点</span><span>退潮</span><span>回暖</span><span>过热</span></div>
      <div style="text-align:center;font-size:12px;margin-top:3px"><b class="mono" style="font-size:20px">${nf(d.score, 1)}</b> <span class="gauge-lv ${d.levelCls}" style="font-size:11px;vertical-align:2px">${esc(d.level)}</span></div>
    </div>
    <div style="flex:1;min-width:0">
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-secondary)"><span>涨跌分布（${total || dash} 只）</span><span>炸板率 <b class="mono">${nf(d.brokenRate, 1)}%</b></span></div>
      <div class="dist-bar" style="margin-top:5px"><i class="up" style="width:${p(d.up)}%"></i><i class="flat" style="width:${p(d.flat)}%"></i><i class="down" style="width:${p(d.down)}%"></i></div>
      <div class="dist-labels"><span class="v-up">↑ ${d.up ?? dash}</span><span class="v-flat">平 ${d.flat ?? dash}</span><span class="v-down">↓ ${d.down ?? dash}</span></div>
      <div style="display:flex;gap:16px;margin-top:10px;align-items:flex-end;flex-wrap:wrap">
        <div><div style="font-size:10.5px;color:var(--muted)">三市成交额（亿）</div><div style="display:flex;align-items:flex-end;gap:5px"><b class="mono" style="font-size:19px">${d.amount === null ? dash : Math.round(d.amount).toLocaleString()}</b>${d.amountDelta === null ? '' : `<em class="${d.amountDelta >= 0 ? 'v-up' : 'v-down'} mono" style="font-size:11px">${sign(d.amountDelta, 1)}%</em>`}${d.prevAmount !== null && d.amount !== null ? pairBars(d.prevAmount, d.amount) : ''}</div></div>
        <div><div style="font-size:10.5px;color:var(--muted)">主力净流入（亿）</div><b class="mono ${d.flow === null ? 'v-muted' : d.flow >= 0 ? 'v-up' : 'v-down'}" style="font-size:19px">${sg(d.flow, 1)}</b></div>
        <div style="margin-left:auto;text-align:right;font-size:11px;color:var(--text-secondary)">涨停 <b class="v-up mono">${d.limitUp ?? dash}</b> · 炸板 <b class="v-warn mono">${d.broken ?? dash}</b> · 跌停 <b class="v-down mono">${d.limitDown ?? dash}</b></div>
      </div>
    </div>
  </div>
  ${(d.sentiment_history || []).length >= 2 ? `
  <div style="display:flex;align-items:center;gap:10px;font-size:10.5px;color:var(--muted)">
    <span style="white-space:nowrap">情绪 10 日</span>${lineSvg(d.sentiment_history, { w: 300, h: 40 })}
  </div>` : ''}`;
  };

  R.shortline = (d) => {
    if (!d.hasData) return emptyState('暂无短线情绪数据（非交易日或上游不可达）');
    if (d.staleDate) {
      markMeta('shortline', `${metaBadge(state.blocks.shortline)} <span class="b b-degraded" title="上游不可达，回退最近快照">数据截止 ${esc(d.staleDate)}</span>`);
    }
    const maxT = d.themes.length ? Math.max(...d.themes.map(t => t[1])) : 1;
    return `
  <div style="display:grid;grid-template-columns:1.05fr 1fr;gap:16px">
    <div>
      <div style="font-size:11px;color:var(--text-secondary)">连板天梯（家数）</div>
      ${d.ladder.length ? ladderMini(d.ladder) : emptyState('今日暂无连板梯队')}
      <div class="stat-grid s3" style="margin-top:10px">
        <div class="stat"><span class="l">最高板</span><b>${d.maxBoard ?? dash}</b><span class="sub">${esc(d.maxBoardName || dash)}</span></div>
        <div class="stat"><span class="l">涨停溢价</span><b class="${d.premium !== null && d.premium < 0 ? 'v-down' : 'v-up'}">${sg(d.premium, 1)}%</b><span class="sub">昨日均值</span></div>
        <div class="stat"><span class="l">晋级率</span><b>${d.promo12 === null ? dash : Math.round(d.promo12) + '%'}</b><span class="sub">连板 ${d.promoDeep === null ? dash : Math.round(d.promoDeep) + '%'}</span></div>
      </div>
    </div>
    <div>
      <div style="font-size:11px;color:var(--text-secondary);margin-bottom:2px">题材家数</div>
      ${d.themes.length ? d.themes.map(([nm, n]) => `
        <div class="theme-line"><span class="nm">${esc(nm)}</span><span class="theme-bar"><i style="width:${(n / maxT * 100).toFixed(0)}%"></i></span><span class="n">${n}</span></div>`).join('') : emptyState('暂无题材数据')}
      <div style="display:flex;align-items:center;gap:12px;margin-top:10px">
        ${(d.limitUp || 0) + (d.broken || 0) + (d.limitDown || 0) > 0 ? poolRing(d.limitUp || 0, d.broken || 0, d.limitDown || 0) : ''}
        <div style="font-size:10.5px;line-height:1.75;color:var(--text-secondary)">
          <div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#ef8a7e;margin-right:5px"></i>涨停 <b class="mono">${d.limitUp ?? dash}</b> · 连板 <b class="mono">${d.ladderN}</b></div>
          <div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#f0b429;margin-right:5px"></i>炸板 <b class="mono">${d.broken ?? dash}</b></div>
          <div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#7fb8a9;margin-right:5px"></i>跌停 <b class="mono">${d.limitDown ?? dash}</b></div>
        </div>
      </div>
    </div>
  </div>`;
  };

  R.thesis = (d) => {
    const list = d.items || [];
    if (!list.length) return emptyState('暂无研究台账');
    return `<div class="th-grid" style="grid-template-columns:repeat(3, 1fr)">${list.map(t => `
  <div class="th-card" data-nav="thesis" style="padding:10px 12px">
    <div class="nm">${esc(t.nm)}<span class="stage ${t.st}">${esc(t.stage)}</span></div>
    <div style="display:flex;align-items:baseline;gap:6px;margin-top:3px">
      <span class="score" style="margin-top:0">${t.score === null ? dash : t.score}</span><small style="color:var(--muted);font-size:10.5px">/ ±${t.full}</small>
      <span style="margin-left:auto">${t.trend.length ? spark(t.trend, 'hot', true) : ''}</span>
    </div>
    ${t.score === null ? '' : divBar(t.score, t.full)}
    <div style="margin-top:7px">${t.factors.map(fRow).join('')}</div>
    <div class="th2-foot">
      ${t.signals.map(s => `<span class="sig-chip">${esc(s)}</span>`).join('')}
      ${t.signalMore ? `<span style="font-size:9.5px;color:var(--muted)">+${t.signalMore}</span>` : ''}
      ${t.alert ? `<span class="alert-chip">告警 ${t.alert}</span>` : '<span style="font-size:9.5px;color:var(--muted)">无新告警</span>'}
      <span style="margin-left:auto;font-size:9.5px;color:var(--muted)">更新 ${esc(t.up)}</span>
    </div>
  </div>`).join('')}</div>`;
  };

  /* 策略徽标：色相=方向（红买/绿卖），明度=时效（亮=当天/中=近1-4天/浅=≥5天前） */
  const STRAT_LEGEND = ' · 徽标：红=买入信号、绿=卖出信号；颜色越亮越近（亮=当天，中=近1-4天，浅=≥5天前）';

  /* 扫描摘要行（双向口径）；旧缓存 payload 无 sell_* 键时回退单侧命中文案 */
  function scanSummaryLine(scanSum, fallback) {
    if (num(scanSum.sell_codes) === null || num(scanSum.sell_signals) === null) return fallback;
    const strategies = num(scanSum.strategies);
    return `策略扫描：持仓∪自选 ${scanSum.codes} 只${strategies === null ? '' : ` × ${strategies} 策略`} · 买点 ${scanSum.hit_codes} 只 / ${scanSum.signals} 个信号 · 卖点 ${scanSum.sell_codes} 只 / ${scanSum.sell_signals} 个信号`;
  }

  function stratBadges(it) {
    const code = it.stock_code || it.code;
    const scans = state.scan || {};
    /* 时效基准 = 扫描块 as_of（最近已完成交易日），旧 payload 缺失时回退容器 latest_date。
       缓存尾部陈旧的标的（latest_date < as_of）会在 tooltip 头标注数据截止。 */
    const asOf = scans.as_of || '';
    /* 策略可信度：扫描块 payload 的 strategy_health（{sid: {verified, status}}）。
       undefined（旧缓存 payload 没这个键 / 该策略不在表里）与 false（明确未验证）语义不同 —— 只对 false 打黄标 */
    const healthMap = scans.strategy_health || {};
    /* 扫描块形状：hits/sells 每 code 值为容器 {hits|sells: [...], latest_date}；兼容直接给数组 */
    const unwrap = (raw, key) => {
      if (Array.isArray(raw)) return { list: raw, latest: '' };
      if (raw && Array.isArray(raw[key])) return { list: raw[key], latest: raw.latest_date || '' };
      return { list: [], latest: '' };
    };
    const rawHits = (scans.hits && scans.hits[code])
      || (it.strategy_hits && it.strategy_hits[code]) || it.strategy_hits || [];
    const rawSells = (scans.sells && scans.sells[code]) || [];
    const buySide = unwrap(rawHits, 'hits');
    const sellSide = unwrap(rawSells, 'sells');
    const entries = buySide.list.map(h => ({ h, side: 'buy', latestDate: buySide.latest }))
      .concat(sellSide.list.map(h => ({ h, side: 'sell', latestDate: sellSide.latest })));
    if (!entries.length) return '<span class="v-muted" style="font-size:10.5px">—</span>';
    entries.forEach(e => {
      e.sid = e.h.strategy_id || e.h.s || '';
      e.date = e.h.date || e.h.d || '';
      e.verified = (healthMap[e.sid] || {}).verified;
      const diff = (Date.parse(asOf || e.latestDate) - Date.parse(e.date)) / 86400000;
      e.days = Number.isFinite(diff) ? diff : null;
    });
    /* 排序：买在前卖在后；同侧按日期倒序 */
    entries.sort((a, b) => a.side !== b.side
      ? (a.side === 'buy' ? -1 : 1)
      : (a.date === b.date ? 0 : (a.date < b.date ? 1 : -1)));
    /* 档位：亮=当天 / 中=近1-4天 / 浅=≥5天前（日期缺失按最浅档兜底） */
    const tierOf = (e) => (e.days === null || e.days >= 5) ? 't2' : (e.days >= 1 ? 't1' : 't0');
    const ageOf = (e) => e.days === null ? '—' : (e.days <= 0 ? '最新' : `近${Math.round(e.days)}天`);
    /* 徽标文案优先级：条件短语（买 labels / 卖 sell_labels）→ 策略名 → 策略 id */
    const labelOf = (e) => {
      const table = e.side === 'buy' ? scans.labels : scans.sell_labels;
      return (table && table[e.sid]) || (scans.names && scans.names[e.sid]) || e.sid;
    };
    const tip = entries.map(e => {
      const label = labelOf(e);
      const nm = (scans.names && scans.names[e.sid]) || '';
      const sub = nm && nm !== label ? `${esc(nm)}（${esc(e.sid)}）` : esc(e.sid);
      const md = e.date.length >= 10 ? e.date.slice(5, 10) : e.date;
      return `<div class="tr"><span class="tb-${e.side} ${tierOf(e)}">〔${e.side === 'buy' ? '买入' : '卖出'}〕</span>` +
        `<span class="d">${esc(md)}</span>` +
        `<span>${esc(label)}</span>` +
        `<span class="d">${ageOf(e)}</span>` +
        (e.verified === false ? '<span class="sub">⚠ 未经验证</span>' : '') +
        `<span class="sub">${sub}</span></div>`;
    }).join('');
    const codeLatest = buySide.latest || sellSide.latest;
    const staleNote = (asOf && codeLatest && codeLatest < asOf)
      ? `（数据截止 ${esc(codeLatest.length >= 10 ? codeLatest.slice(5, 10) : codeLatest)}）` : '';
    const head = `<div class='tt'>${esc(it.nm)} ${esc(code || '')} · 策略信号${staleNote}</div>`
      + `<div class="tr"><span class="sub">红=买入、绿=卖出；越亮越近（亮=当天，红/绿=近1-4天，浅=≥5天）</span></div>`;
    return entries.map(e =>
      `<span class="strat-badge ${e.side === 'buy' ? 'sb-buy' : 'sb-sell'} ${tierOf(e)}${e.verified === false ? ' sb-unverified' : ''}" data-tip="${esc(head + tip)}">${e.verified === false ? '⚠' : ''}${esc(labelOf(e))}</span>`).join('');
  }

  R.portfolio = (d) => {
    if (!d.items.length) return emptyState('暂无持仓');
    const rows = pageSlice('portfolio', d.items);
    const s = d.summary;
    const colors = ['#176b5b', '#2f6f9f', '#8b5cf6', '#e0a03f'];
    const valued = d.items.filter(x => x.value !== null && x.value > 0);
    const total = valued.reduce((a, x) => a + x.value, 0);
    let acc = 0;
    const segs = valued.map((x, i) => { const a = acc; acc += x.value / total * 100; return `${colors[i % 4]} ${a.toFixed(1)}% ${acc.toFixed(1)}%`; }).join(', ');
    const hasTrend = d.items.some(x => x.trend && x.trend.length);
    const scanSum = (state.scan && state.scan.summary) || null;
    const strategies = scanSum ? num(scanSum.strategies) : null;
    const scanLine = state.scanFailed
      ? '策略扫描不可用'
      : state.scanned && scanSum
        ? scanSummaryLine(scanSum, `策略扫描：持仓∪自选 ${scanSum.codes} 只${strategies === null ? '' : ` × ${strategies} 策略`} · 命中 ${scanSum.hit_codes} 只 / ${scanSum.signals} 个信号`) + STRAT_LEGEND
        : `策略扫描：待扫描…${STRAT_LEGEND}`;
    return `
  <div style="display:flex;gap:16px;align-items:center;margin-bottom:9px">
    <div class="ring-wrap">
      <div class="ring" style="background:${total > 0 ? `conic-gradient(${segs})` : 'var(--hover)'}"><b>${d.items.length}</b></div>
      <div class="cap">持仓 ${d.items.length} 只<br>市值 ${s.mv === null ? dash : (s.mv / 10000).toFixed(1) + ' 万'}</div>
    </div>
    <div class="stat-grid s3" style="flex:1">
      <div class="stat"><span class="l">今日盈亏</span><b class="${posCls(s.today)}">${sg(s.today, 0)}</b>${s.todayPct === null ? '' : `<em class="${posCls(s.todayPct)}">${sign(s.todayPct, 2)}%</em>`}</div>
      <div class="stat"><span class="l">浮动盈亏</span><b class="${posCls(s.floatPnl)}">${sg(s.floatPnl, 0)}</b>${s.floatPct === null ? '' : `<em class="${posCls(s.floatPct)}">${sign(s.floatPct, 1)}%</em>`}</div>
      <div class="stat"><span class="l">仓位 / 可用</span><b>${s.posPct === null ? dash : s.posPct.toFixed(1) + '%'}</b><span class="sub">可用 ${s.avail === null ? dash : Math.round(s.avail).toLocaleString()}</span></div>
    </div>
  </div>
  <table class="q-table">
    <thead><tr><th>标的</th>${hasTrend ? '<th>5 日走势</th>' : ''}<th>现价</th><th>涨跌幅</th><th>今日盈亏</th><th style="text-align:left">策略触发</th></tr></thead>
    <tbody>${rows.map(it => `
      <tr data-nav="${esc(it.code)}">
        <td><span class="q-name">${esc(it.nm)}</span><span class="q-code">${esc(it.code)}</span></td>
        ${hasTrend ? `<td>${it.trend && it.trend.length ? spark(it.trend, 'hot', true) : '<span class="v-muted">—</span>'}</td>` : ''}
        <td>${nf(it.price, 2)}</td>
        <td class="${posCls(it.pct)}">${it.pct === null ? dash : sign(it.pct, 2) + '%'}</td>
        <td class="${posCls(it.today)}">${sg(it.today, 0)}</td>
        <td style="text-align:left">${stratBadges(it)}</td>
      </tr>`).join('')}</tbody>
  </table>
  ${pagerHtml('portfolio', d.items.length)}
  <div style="display:flex;gap:10px;margin-top:6px;font-size:10.5px;color:var(--muted)">
    ${d.account ? `<span>账户资金：${esc(d.account.syncAt)} · ${esc(d.account.note)}</span>` : ''}
    <span style="margin-left:auto" class="mono">${scanLine}</span>
  </div>`;
  };

  R.watchlist = (d) => {
    if (!d.items.length) return emptyState('暂无自选股');
    const rows = pageSlice('watchlist', d.items);
    const hasTrend = d.items.some(x => x.trend && x.trend.length);
    const scanSum = (state.scan && state.scan.summary) || null;
    const scanLine = state.scanFailed
      ? '策略扫描不可用'
      : state.scanned && scanSum
        ? scanSummaryLine(scanSum, `策略扫描：命中 ${scanSum.hit_codes}/${scanSum.codes} 只 · ${scanSum.signals} 个信号`) + STRAT_LEGEND
        : `策略扫描：待扫描…${STRAT_LEGEND}`;
    return `
  <table class="q-table">
    <thead><tr><th>标的</th>${hasTrend ? '<th>5 日走势</th>' : ''}<th>现价</th><th>涨跌幅</th><th style="text-align:left">策略触发</th></tr></thead>
    <tbody>${rows.map(it => `
      <tr data-nav="${esc(it.code)}">
        <td><span class="q-name">${esc(it.nm)}</span><span class="q-code">${esc(it.code)}</span></td>
        ${hasTrend ? `<td>${it.trend && it.trend.length ? spark(it.trend, 'hot', true) : '<span class="v-muted">—</span>'}</td>` : ''}
        <td>${nf(it.price, 2)}</td>
        <td class="${posCls(it.pct)}">${it.pct === null ? dash : sign(it.pct, 2) + '%'}</td>
        <td style="text-align:left">${stratBadges(it)}</td>
      </tr>`).join('')}</tbody>
  </table>
  ${pagerHtml('watchlist', d.items.length)}
  <div style="display:flex;gap:10px;margin-top:6px;font-size:10.5px;color:var(--muted)">
    <span>点击行打开 K 线并自动扫描该股信号</span>
    <span style="margin-left:auto" class="mono">${scanLine}</span>
  </div>`;
  };

  /* 宏观轻量分组（前端常量映射，后端 schema 不变）；未在映射里的指标归入「其他」放最后 */
  const MACRO_GROUPS = [
    { title: '利率', codes: ['lpr_1y', 'cn10_y', 'cn30_y', 'us10_y', 'us2_y', 'shibor_1w'] },
    { title: '汇率', codes: ['usdcny', 'dxy'] },
    { title: '通胀与增长', codes: ['cpi_yoy', 'us_cpi_yoy', 'pmi_mfg', 'gdp_yoy', 'm2_yoy'] },
    { title: '商品与储备', codes: ['au_sge', 'ag_sge', 'cu_main', 'hog_spot_price', 'cn_gold_reserve'] },
  ];

  R.macro = (d) => {
    if (d.missing > 0) {
      markMeta('macro', `${metaBadge(state.blocks.macro)} <span class="b b-info" title="${esc((d.missingCodes || []).join('、'))}">缺失 ${d.missing} 项</span>`);
    }
    if (!d.items.length) return emptyState('暂无宏观指标数据');
    const known = new Set(MACRO_GROUPS.reduce((a, g) => a.concat(g.codes), []));
    const byCode = {};
    d.items.forEach(m => { byCode[m.c] = m; });
    const sections = MACRO_GROUPS
      .map(g => ({ title: g.title, items: g.codes.map(c => byCode[c]).filter(Boolean) }))
      .concat([{ title: '其他', items: d.items.filter(m => !known.has(m.c)) }])
      .filter(sec => sec.items.length);
    return `<div>${sections.map(sec =>
      `<div class="macro-group" style="font-size:10px;color:var(--muted);margin:7px 0 3px">${esc(sec.title)}</div>`
      + sec.items.map(macroRowHtml).join('')
    ).join('')}</div>`;
  };

  R.quality = (d) => `
  <div style="display:flex;gap:14px;align-items:center">
    <div style="display:flex;gap:12px;flex:0 0 auto">
      <div class="ring-wrap">${ringHtml(d.winrate === null ? 0 : d.winrate, 'var(--brand)', d.winrate === null ? dash : d.winrate.toFixed(1) + '%')}<div class="cap">费后5日胜率<br>signal-eval</div></div>
      <div class="ring-wrap">${ringHtml(d.cacheHit === null ? 0 : d.cacheHit, '#2f6f9f', d.cacheHit === null ? dash : d.cacheHit.toFixed(1) + '%')}<div class="cap">${d.cacheSrc === 'token' ? '缓存命中（占总 Token）' : '缓存命中（占总量，A8）'}</div></div>
    </div>
    <div style="flex:1;min-width:0">
      <div style="display:flex;align-items:baseline;gap:8px">
        <span style="font-size:11px;color:var(--text-secondary)">可验证结论（本周）</span>
        <b class="mono" style="font-size:22px">${d.conclusions === null ? dash : d.conclusions}</b>
        ${d.wow === null ? '' : `<em class="${posCls(d.wow)} mono">环比 ${sign(d.wow, 0)}</em>`}
        <span style="font-size:11px;color:var(--text-secondary);margin-left:auto">单报告成本 <b class="mono" style="color:var(--text)">${fmtCost(d.cost, d.costUnit)}</b></span>
      </div>
      <div style="margin-top:7px">
        <span style="font-size:10.5px;color:var(--muted)">可验证结论 · 8 周趋势</span>
        ${d.trend.length >= 2 ? lineSvg(d.trend, { w: 240, h: 40 }) : `<div style="margin-top:6px">${emptyState('暂无周趋势数据')}</div>`}
      </div>
    </div>
  </div>`;

  R.review = (d) => `
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div>
      ${d.hasEval ? `
      <div style="font-size:11px;color:var(--text-secondary)">最新评测批次 · ${esc(d.evalName)}</div>
      <div style="display:flex;align-items:baseline;gap:8px;margin-top:2px">
        <b class="mono" style="font-size:24px">${nf(d.evalScore, 1)}</b>
        ${d.evalDelta === null ? '' : `<em class="${posCls(d.evalDelta)} mono">${sign(d.evalDelta, 1)}</em>`}
        <span class="v-muted" style="font-size:10.5px">cases ${esc(d.cases)} · ${esc(d.evalAt)}</span>
      </div>
      ${d.trend.length >= 2 ? lineSvg(d.trend, { w: 240, h: 42 }) : `<div>${emptyState('暂无趋势数据（需 ≥2 个批次）')}</div>`}` : emptyState('暂无评测批次')}
      <div style="margin-top:6px;font-size:11px;color:var(--text-secondary);display:flex;gap:10px;flex-wrap:wrap">
        <span>评审 run ${d.runReviews}${d.runAvg === null ? '' : `（均分 <b class="mono">${d.runAvg}</b>）`} · 对话 ${d.dlgReviews}${d.dlgAvg === null ? '' : `（均分 <b class="mono">${d.dlgAvg}</b>）`}${d.rubric ? `（体系 ${esc(d.rubric)}）` : ''}</span>
        <span>期望值 <b class="mono ${posCls(d.exp)}">${d.exp === null ? dash : sign(d.exp, 2) + '%'}</b> · 盈亏比 <b class="mono">${nf(d.payoff, 2)}</b></span>
      </div>
    </div>
    <div>
      <div style="font-size:11px;color:var(--text-secondary)">信号校准 · 预测置信度 vs 实际胜率</div>
      ${d.calib.length ? calibChart(d.calib) : emptyState('暂无校准样本')}
      <div class="calib-legend">
        <span><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#9db8d2;margin-right:4px"></i>预测</span>
        <span><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--brand);margin-right:4px"></i>实际</span>
        <span style="margin-left:auto">样本 ${d.sigN ?? dash} · 5日胜率 ${d.sigWin === null ? dash : d.sigWin.toFixed(1) + '%'} · ECE <b class="mono">${d.ece === null ? dash : d.ece}</b></span>
      </div>
    </div>
  </div>`;


  /* 上下文洞察 / 活跃度 / 会话档案：payload 已是渲染就绪形状（无适配层）。
     ⚠️ 旧 usage 块的 token/cost/activity/modes/recent/… 形状已被任务 11 的
     context_stats.build_context_insight 取代（range/kpi/daily_tokens/daily_cost/by_model/runs），
     adaptUsage 随之删除 —— 留着它只会把新 payload 读成破折号。 */
  R.usage = (d) => renderUsage(d);
  R.activity = (d) => renderActivity(d);
  R.sessions = (d) => renderSessions(d);
  R.todos = (d) => {
    const list = d.items || [];
    markMeta('todos', `<span class="b b-info">未处理 ${list.length}</span>`);
    if (!list.length) return emptyState('暂无待办');
    return `<div>${list.map(t => `
    <div class="todo-item">
      <span class="sev ${t.sev}"></span>
      <span class="tx">${esc(t.tx)}</span>
      <span class="tm">${esc(t.tm)}</span>
      ${t.target ? `<button class="go" data-nav="${esc(t.target)}">去处理</button>` : '<span class="v-muted" style="font-size:10.5px">—</span>'}
    </div>`).join('')}</div>`;
  };

  R.storage = (d) => {
    const maxC = d.categories.length ? Math.max(...d.categories.map(c => c[1])) : 1;
    return `
  <div class="stat-grid s2" style="margin-bottom:9px">
    <div class="stat"><span class="l">数据库总占用</span><b>${d.totalMb === null ? dash : (d.totalMb / 1024).toFixed(2) + ' GB'}</b><span class="sub">${d.assets ?? dash} 类资产</span></div>
    <div class="stat"><span class="l">异常资产</span><b><span class="${d.stale ? 'v-warn' : 'v-muted'}">${d.stale ?? dash}</span></b><span class="sub">按 data_monitor 阈值</span></div>
    <div class="stat"><span class="l">清理候选</span><b>${d.cleanupCandidates} 项</b><em class="v-muted">可释放 ${d.cleanupMb}MB</em><span class="sub">${d.cleanupRules} 条规则 dry-run</span></div>
    <div class="stat"><span class="l">最近扫描</span><b style="font-size:13px">${esc(d.scanAt)}</b><span class="sub">失败任务 ${d.failedTasks} · 队列 ${d.queue}</span></div>
  </div>
  ${d.categories.map(([nm, mb]) => `
    <div class="theme-line"><span class="nm" style="flex-basis:108px">${esc(nm)}</span><span class="theme-bar"><i style="width:${(mb / maxC * 100).toFixed(0)}%"></i></span><span class="n" style="flex-basis:58px">${mb} MB</span></div>`).join('')}
  <div style="margin-top:8px;font-size:10.5px;color:var(--muted)">口径：最近一次全量扫描快照（与数据中心「存储监控」同源）；打开总览时拉取一次，研究类节奏，不自动刷新。</div>`;
  };

  R.runtime = (d) => {
    if (!d.available) return emptyState('psutil 未安装，暂不可用');
    const cls = (v) => v < 60 ? 'low' : v < 85 ? 'mid' : 'high';
    return `
  <div style="display:flex;justify-content:space-between;align-items:baseline;font-size:11px;color:var(--text-secondary)">
    <span>CPU 使用率（进程 · 瞬时）</span><b class="mono" style="font-size:16px;color:var(--text)">${nf(d.cpu, 1)}%</b>
  </div>
  ${d.cpuHist && d.cpuHist.length >= 2 ? lineSvg(d.cpuHist, { w: 260, h: 44, color: '#2f6f9f' }) : ''}
  <div style="display:flex;justify-content:space-between;align-items:baseline;font-size:11px;color:var(--text-secondary);margin-top:8px">
    <span>内存（进程 RSS）</span><b class="mono" style="font-size:16px;color:var(--text)">${d.memRssMb === null ? dash : (d.memRssMb / 1024).toFixed(2) + ' GB'}</b>
  </div>
  <div class="meter"><i class="${d.memSysPct === null ? 'low' : cls(d.memSysPct)}" style="width:${d.memSysPct === null ? 0 : Math.min(100, d.memSysPct)}%"></i></div>
  <div style="font-size:10.5px;color:var(--muted);margin-top:3px">系统内存占用 ${nf(d.memSysPct, 1)}%</div>
  <div class="stat-grid s2" style="margin-top:10px">
    <div class="stat"><span class="l">线程 / 已运行</span><b style="font-size:13px">${d.threads ?? dash} · ${esc(d.uptime)}</b></div>
    <div class="stat"><span class="l">后台任务（采集运行中）</span><b>${d.tasksRunning}</b></div>
  </div>`;
  };

  /* 策略健康度：只看每个策略「最近一次回测」是否可信（设计文档 §11）。
     verified 口径与策略详情页同源：体检 ok + 样本达标 + ≥5 笔（server/backtest_service.py）。
     注意 coverage_pct 落库是 0-1 比值（backtest/health.py:57），展示要 ×100。 */
  R.strategy_health = (d) => {
    const rows = d.rows || [];
    if (!rows.length) return emptyState('暂无策略（先用策略回测跑一次）');
    const STATUS = {
      ok: ['✓ 有效', 'ok'],
      gap: ['⚠ 数据有缺口', 'warn'],
      weak: ['⚠ 结果偏弱', 'warn'],
      never: ['⚠ 从未回测', 'never'],
    };
    /* 两处降级标注，避免行内结论与「已验证」汇总自相矛盾：
       - 体检 ok 但样本 <5 笔 → 不能称「已验证」，黄标说明原因；
       - 有回测记录却没有体检结果（跑完/中断/旧库未迁移）→ 不能写「从未回测」。 */
    const badgeOf = (r) => {
      if (r.status === 'ok' && !r.verified) return ['⚠ 样本不足', 'warn'];
      if (r.status === 'never' && r.last_run_at) return ['⚠ 无体检结果', 'warn'];
      return STATUS[r.status] || ['—', 'never'];
    };
    const sum = d.summary || {};
    const line = `策略 ${sum.total || rows.length} 个 · 已验证 ${sum.verified || 0} · 从未回测 ${sum.never || 0}`
      + (sum.gap ? ` · 数据有缺口 ${sum.gap}` : '');
    return `<div class="sh-line">${esc(line)}</div>` + rows.map((r) => {
      const st = badgeOf(r);
      const cov = num(r.coverage_pct) === null ? dash : (Number(r.coverage_pct) * 100).toFixed(1) + '%';
      const io = num(r.sharpe_ratio_io) === null ? dash : Number(r.sharpe_ratio_io).toFixed(2);
      return `<div class="sh-row" data-nav="strategy:${esc(r.strategy_id || '')}" title="点开这个策略的详情：回测记录 / 评判 / 前瞻 / 落地">
        <span class="sh-name">${esc(r.name || r.strategy_id)}</span>
        <span class="sh-badge ${st[1]}">${esc(st[0])}</span>
        <span class="sh-meta">最近回测 ${esc((r.last_run_at || '—').slice(0, 10))} · 数据 ${cov} · 样本 ${r.trade_count || 0} 笔 · IS/OOS ${io}</span>
      </div>`;
    }).join('')
      + `<div class="sh-foot">口径：只看最近一次回测；数据覆盖率不足或样本 &lt; 5 笔的策略不算「已验证」。${esc(d.as_of || '')}</div>`;
  };

  /* 组合层：跨资产配比 + 共同风险。紧凑版（大屏节奏），完整视图在台账页「组合层」入口 */
  R.thesis_portfolio = (d) => {
    if (!d.available) return emptyState('组合层暂不可用：' + esc(d.reason || ''));
    if (!d.items.length) return emptyState('暂无组合定义（data/portfolio_map.json）');
    const maxW = 100;
    return d.items.map((p) => {
      if (!p.ok) {
        return `<div class="v-muted" style="font-size:11px;padding:6px 0">${esc(p.name)}：${esc(p.reason)}</div>`;
      }
      const legs = p.legs.map((l) => {
        const w = l.total === null ? 0 : Math.min(100, Math.abs(l.total) / 2 * 100);
        const cls = l.total === null ? '' : (l.total > 0 ? 'v-up' : 'v-down');
        return `<div class="theme-line"><span class="nm">${esc(l.role || l.name)}</span>
          <span class="theme-bar"><i style="width:${w.toFixed(0)}%;background:${l.total > 0 ? '#c0392b' : '#1e8449'}"></i></span>
          <span class="n ${cls}">${l.total === null ? dash : sg(l.total, 0)}</span></div>`;
      }).join('');
      const ws = p.weights.map(([nm, w]) => `
        <div class="theme-line"><span class="nm">${esc(nm)}</span>
          <span class="theme-bar"><i style="width:${(w[1] / maxW * 100).toFixed(0)}%;background:#c98a3f"></i></span>
          <span class="n" style="flex-basis:52px">${w[0]}~${w[1]}%</span></div>`).join('');
      const upgrade = p.upgrade
        ? `<div style="font-size:10.5px;color:var(--muted);margin-top:5px">距上一档「${esc(p.upgrade.to_label)}」还差 <b class="mono">${esc(p.upgrade.delta)}</b> 分</div>`
        : '';
      const riskLine = p.riskHit.length
        ? `<div style="margin-top:6px;font-size:11px;color:var(--warn)">⚠ 共同风险已触发：${esc(p.riskHit.join('、'))}（示例口径：内部调权为主）</div>`
        : (p.riskPending.length
          ? `<div style="margin-top:6px;font-size:10.5px;color:var(--muted)">共同风险待评估：${esc(p.riskPending.join('、'))}</div>`
          : `<div style="margin-top:6px;font-size:10.5px;color:var(--muted)">共同风险未触发</div>`);
      return `
  <div style="display:flex;align-items:baseline;gap:8px">
    <b class="mono" style="font-size:18px">${p.total === null ? dash : sg(p.total, 1)}</b>
    <span style="font-size:12px">${esc(p.name)}</span>
    <span class="mono" style="margin-left:auto;font-size:12px;color:var(--brand)">${esc(p.bandLabel)}</span>
  </div>
  ${upgrade}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:7px">
    <div><div style="font-size:10.5px;color:var(--muted);margin-bottom:2px">腿状态（总分）</div>${legs}</div>
    <div><div style="font-size:10.5px;color:var(--muted);margin-bottom:2px">建议权重</div>${ws}</div>
  </div>
  <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:7px;font-size:10.5px;color:var(--text-secondary)">
    <span>相关性 <b class="mono">${p.corr === null ? dash : p.corr}</b></span>
    <span>等权分散化降波 <b class="mono">${p.divPct === null ? dash : p.divPct + '%'}</b></span>
  </div>
  ${riskLine}`;
    }).join('');
  };

  /* 骨架 + 三栏分配：布局来自 SSE plan 事件（任务 12），zone 分列渲染；
     right 栏末尾追加静态「快捷入口」卡片（按钮与原型一致：短线情绪/研究台账/质量面板/评测系统/策略回测/数据中心/盯盘面板/配置管理） */
  let PLAN = [];
  const SPAN = {
    market: 5, shortline: 7, portfolio: 7, watchlist: 5,
    thesis: 12, quality: 6, review: 6,
    runtime: 12, todos: 12, macro: 12, usage: 12, storage: 12,
    thesis_portfolio: 12, strategy_health: 12,
  };
  const KIND_CN = { research: '研究类', market: '行情类', system: '系统' };
  /* 卡头「详情 ›」下钻 target（无映射的块不渲染按钮） */
  const HEAD_DRILL = {
    thesis: 'thesis', shortline: 'shortline', quality: 'quality',
    review: 'eval', storage: 'data',
    /* 组合层的完整视图在台账页左侧列表置顶（组合层不是 thesis 定义，
       但入口挂在那里——下钻过去一眼能看到） */
    thesis_portfolio: 'thesis',
    /* 策略健康度的下钻 = 策略回测页（策略库/账本），那里能做体检与重跑 */
    strategy_health: 'backtest',
  };

  function titleOf(id) {
    const b = PLAN.find(x => x.id === id) || state.planBlocks.find(x => x.id === id);
    return b ? b.title : id;
  }

  function page() { return $('ovPage'); }

  function openPage() {
    if (state.open) return;
    state.open = true;
    page().classList.add('open');
    const chat = document.querySelector('.chat-pane');
    if (chat) chat.style.display = 'none';
    loadInitial();
  }

  function closePage() {
    if (!state.open) return;
    state.open = false;
    page().classList.remove('open');
    const chat = document.querySelector('.chat-pane');
    if (chat) chat.style.display = '';
    abortStream();
    stopTimers();
    stopElapsed();
    state.running = false;
  }

  function renderGrid() {
    const card = (b) => `
    <section class="ov-card" data-block="${b.id}" style="grid-column: span ${b.span}">
      <header class="ov-card-head">
        <span class="t"><span class="dot"></span>${b.title}</span>
        <span class="meta" data-meta="${b.id}"><span class="ov-skel" style="width:90px"><i style="height:10px"></i></span></span>
        ${HEAD_DRILL[b.id] ? `<button class="drill" data-nav="${HEAD_DRILL[b.id]}" title="下钻到专项页">详情 ›</button>` : ''}
        <button class="ov-collapse" title="折叠/展开">▾</button>
      </header>
      <div class="ov-card-body" data-body="${b.id}">
        <div class="ov-skel"><i></i><i></i><i></i></div>
      </div>
    </section>`;
    $('colLeft').innerHTML = PLAN.filter(b => b.zone === 'left').map(card).join('');
    $('colCenter').innerHTML = PLAN.filter(b => b.zone === 'center').map(card).join('');
    const navCard = `
    <section class="ov-card" style="grid-column: span 12">
      <header class="ov-card-head">
        <span class="t"><span class="dot"></span>快捷入口</span>
        <span class="meta">全工具导航 · 点击下钻</span>
      </header>
      <div class="ov-card-body">
        <div class="nav-grid">
          <button data-nav="shortline">短线情绪</button>
          <button data-nav="thesis">研究台账</button>
          <button data-nav="quality">质量面板</button>
          <button data-nav="eval">评测系统</button>
          <button data-nav="backtest">策略回测</button>
          <button data-nav="data">数据中心</button>
          <button data-nav="monitor">盯盘面板</button>
          <button data-nav="config">配置管理</button>
        </div>
      </div>
    </section>`;
    $('colRight').innerHTML = PLAN.filter(b => b.zone === 'right').map(card).join('') + navCard;
  }

  /* ── 事件日志 / 进度条 / 卡头徽标 ── */

  function logLine(ev, msg) {
    const lines = $('ovLogLines');
    const el = document.createElement('div');
    el.className = 'log-line';
    el.innerHTML = `<span class="log-time">${nowMs()}</span><span class="log-ev ev-${ev}">${ev}</span><span class="log-msg">${msg}</span>`;
    lines.appendChild(el);
    state.logCount++;
    $('ovLogCount').textContent = state.logCount;
    lines.scrollTop = lines.scrollHeight;
  }

  function setProgress(text, pct, hasError) {
    $('ovProgressText').innerHTML = text;
    $('ovProgressFill').style.width = Math.max(0, Math.min(100, pct)) + '%';
    $('ovProgressFill').classList.toggle('has-error', !!hasError);
  }

  function startElapsed() {
    stopElapsed();
    state.runStart = Date.now();
    state.runTimer = setInterval(() => {
      const s = ((Date.now() - state.runStart) / 1000).toFixed(1);
      const total = PLAN.length || 0;
      $('ovLogStats').textContent = `已完成 ${state.completed}/${total} · 耗时 ${s}s`;
      if (!state.running) return;
      if (state.runKind === 'auto') {
        setProgress(`自动刷新中（行情类 4 块，静默更新不闪骨架屏）· <b>${s}s</b>`, 30, false);
      } else {
        const cur = lastActive();
        setProgress(`加载中 <b>${state.completed}/${total}</b>${cur ? ` · 当前：${esc(cur)}` : ''} · <b>${s}s</b>`, total ? state.completed / total * 100 : 0, false);
      }
    }, 100);
  }
  function stopElapsed() { if (state.runTimer) clearInterval(state.runTimer); state.runTimer = null; }
  function lastActive() {
    for (let i = PLAN.length - 1; i >= 0; i--) {
      const s = state.blocks[PLAN[i].id];
      if (s && s.status === 'loading') return PLAN[i].title;
    }
    return '';
  }
  function markMeta(id, html) { const el = document.querySelector(`[data-meta="${id}"]`); if (el) el.innerHTML = html; }
  function metaBadge(s) {
    if (!s) return '—';
    if (s.status === 'error') return `<span class="b b-degraded" style="border-color:#f3c1b8;background:var(--danger-bg);color:var(--error)">失败</span>`;
    if (s.status === 'loading') return `<span class="ov-skel" style="width:80px"><i style="height:10px"></i></span>`;
    if (s.status === 'idle') return '—';
    return [
      s.degraded ? `<span class="b b-degraded" title="${esc(s.degraded)}">降级</span>` : '',
      s.cache ? `<span class="b b-cache" title="服务层缓存命中（未触网）">缓存命中</span>` : `<span class="b b-force" title="直读数据源 / 重算">直读</span>`,
    ].join(' ');
  }

  /* ── 渲染调度：骨架屏 / 错误态 / done 占位（图表在任务 13 接入） ── */

  function renderBlock(id) {
    const s = state.blocks[id];
    const body = document.querySelector(`[data-body="${id}"]`);
    const card = document.querySelector(`.ov-card[data-block="${id}"]`);
    if (!s || !body || !card) return;
    card.classList.toggle('degraded', !!s.degraded);
    card.classList.toggle('error', s.status === 'error');
    markMeta(id, metaBadge(s));

    if (s.status === 'loading') { body.innerHTML = '<div class="ov-skel"><i></i><i></i><i></i></div>'; return; }
    if (s.status === 'error') {
      body.innerHTML = `<div class="ov-error"><span>⚠ ${esc(s.error)}</span><button class="retry" data-retry="${id}">重试本块</button></div>`;
      return;
    }
    if (s.status !== 'done') return;
    const fn = R[id];
    if (!fn) { body.innerHTML = '<div style="color:var(--muted);font-size:11px">暂无渲染器</div>'; return; }
    try {
      body.innerHTML = fn(adapt(id, s.data));
    } catch (e) {
      body.innerHTML = `<div class="ov-error"><span>⚠ 渲染失败：${esc(e.message || e)}</span><button class="retry" data-retry="${id}">重试本块</button></div>`;
    }
  }

  /* ── SSE 客户端：fetch + ReadableStream 手动切帧 ── */

  function abortStream() {
    if (streamAbort) { streamAbort.abort(); streamAbort = null; }
    /* 全量刷新/自动刷新开始时，残留的单块重试流也要收掉：
       否则旧的重试结果会晚于新数据落地，把卡片刷回旧值。 */
    if (state.retries && state.retries.size) {
      state.retries.forEach(ctrl => { try { ctrl.abort(); } catch (e) { /* 已结束 */ } });
      state.retries.clear();
    }
  }

  async function runStream(kind, blocks) {
    const isRetry = kind === 'force' && !!(blocks && blocks.length);
    const key = isRetry ? blocks.join(',') : '';

    /* 单块重试：不抢 state.running 这把全局锁，也不 abort 正在跑的流。
       原实现 `if (state.running) return;` 会把重试点击静默丢弃 —— 初次加载
       只要有一个慢块（如 agent 消耗统计）在跑，用户点「重试本块」就毫无反应，
       必须等全部区块跑完才生效。后端按块加锁，多开一条流代价很小，
       且同块重复点击会先 abort 掉自己上一条，不至于堆积。 */
    if (isRetry && state.retries.has(key)) {
      state.retries.get(key).abort();
      state.retries.delete(key);
    }
    if (!isRetry && state.running) return;

    const ctrl = new AbortController();
    if (isRetry) {
      state.retries.set(key, ctrl);
      runRole = 'retry';
    } else {
      state.running = true;
      abortStream();
      streamAbort = ctrl;
      runRole = 'main';
    }

    const qs = new URLSearchParams();
    if (blocks && blocks.length) qs.set('blocks', blocks.join(','));
    if (kind === 'force') qs.set('force', '1');
    beginRun(kind, blocks);
    try {
      const res = await fetch('/api/overview/stream?' + qs.toString(), { signal: ctrl.signal });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const parsed = parseSseFrame(frame);
          /* 每帧前重置角色：流之间可能交错（重试与主流并发） */
          runRole = isRetry ? 'retry' : 'main';
          if (parsed) handleEvent(parsed);
        }
      }
      runRole = 'main';
    } catch (e) {
      runRole = 'main';
      if (e.name !== 'AbortError') {
        logLine('block_error', `${isRetry ? '重试' : '连接'}中断：${esc(e.message)}`);
        if (!isRetry && kind !== 'auto') timers.push(setTimeout(() => runStream('initial', null), 3000));
      }
    } finally {
      if (isRetry) {
        if (state.retries.get(key) === ctrl) state.retries.delete(key);
        runRole = 'main';
      } else {
        state.running = false;
        streamAbort = null;
      }
    }
  }

  function parseSseFrame(frame) {
    let event = 'message';
    const dataLines = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return null;
    try { return { event, data: JSON.parse(dataLines.join('\n')) }; }
    catch { return null; }
  }

  /* ── 运行开始：重置日志/进度，目标块进入 loading（auto 只挂「刷新中」徽标） ── */

  function beginRun(kind, blocks) {
    const isRetry = kind === 'force' && !!(blocks && blocks.length);
    if (isRetry) {
      /* 单块重试：不重置全局计数 / 不清空日志 / 不重建骨架 / 不动强制刷新按钮。
         原实现对重试也跑一遍全量重置，导致摘要变成「完成 1/1」、日志被抹掉，
         且与正在跑的流互相干扰。 */
      const targets = blocks;
      targets.forEach(id => {
        if (id === 'strategy_scan') return;
        const s = state.blocks[id];
        /* 计数语义（见 block_done / block_error）：completed = 已结束处理的区块数
           （含失败），failed = 其中失败的。重试的块此刻先「退回未结算」，
           结束后由既有 handler 自然补回 —— 成功则 completed 回原值、failed 少 1；
           再失败则两个都回原值。避免直接 ++ 造成 completed 重复计数。 */
        if (s && s.status === 'error') {
          state.completed = Math.max(0, state.completed - 1);
          state.failed = Math.max(0, state.failed - 1);
        }
        state.blocks[id] = { status: 'loading', cache: false, degraded: '', error: null, data: null };
        renderBlock(id);
      });
      $('ovLog').classList.add('open');
      logLine('block_start', `<span class="dim">重试 ${targets.length} 个区块：</span>${targets.map(titleOf).join('、')}`);
      return;
    }

    stopTimers();
    state.running = true;
    state.runKind = kind;
    state.completed = 0; state.failed = 0; state.logCount = 0;
    $('ovLogLines').innerHTML = ''; $('ovLogCount').textContent = '0';
    $('ovLogRunAt').textContent = 'started ' + now();
    $('ovLog').classList.add('open');
    $('ovForceBtn').disabled = true; $('ovForceBtn').textContent = '刷新中…';

    const targets = blocks && blocks.length ? blocks : PLAN.map(b => b.id);
    /* 仅全量运行重置扫描态；单块重试（force + blocks）不清掉已完成的扫描结果 */
    if (kind === 'initial' || (kind === 'force' && !(blocks && blocks.length))) {
      state.scanned = false; state.scan = null; state.scanFailed = false;
    }
    targets.forEach(id => {
      if (id === 'strategy_scan') return;
      if (kind === 'auto') {
        const s = state.blocks[id];
        if (s) { s.refreshing = true; markMeta(id, '<span class="b b-info">刷新中…</span>'); }
        return;
      }
      state.blocks[id] = { status: 'loading', cache: false, degraded: '', error: null, data: null };
      renderBlock(id);
    });
    startElapsed();
  }

  /* ── 事件处理（自原型逐段移植，数据源改为真实 SSE payload） ── */

  function handleEvent(ev) {
    const type = ev.event;
    const d = ev.data || {};

    if (type === 'plan') {
      const raw = d.blocks || [];
      state.planBlocks = raw;
      const visible = raw.filter(b => b.id !== 'strategy_scan')
        .map(b => ({ id: b.id, title: b.title, zone: b.zone, kind: b.kind, span: SPAN[b.id] || 12 }));
      /* 全量计划才重建三栏骨架；单块重试/自动刷新的子集不重建，避免清掉其余卡片 */
      if (!PLAN.length || visible.length >= PLAN.length) {
        PLAN = visible;
        visible.forEach(b => {
          state.blocks[b.id] = { status: state.runKind === 'auto' ? 'idle' : 'loading', cache: false, degraded: '', error: null, data: null };
        });
        renderGrid();
      }
      ensureTimers({ market_refresh_s: d.market_refresh_s, runtime_refresh_s: d.runtime_refresh_s });
      const counts = ['research', 'market', 'system']
        .map(k => [k, visible.filter(b => b.kind === k).length])
        .filter(([, n]) => n > 0)
        .map(([k, n]) => `${KIND_CN[k] || k} ${n}`).join(' · ');
      if (state.runKind === 'auto') {
        logLine('plan', `<span class="dim">自动刷新</span> 仅重载行情类区块：${visible.map(b => b.title).join('、')}`);
      } else if (d.force) {
        logLine('plan', `<span class="dim">force=1</span> 载入计划：${visible.length} 个区块（${counts}）· 绕过聚合缓存，行情类直读数据源`);
      } else {
        logLine('plan', `载入计划：${visible.length} 个区块（${counts}）· 块级并发，完成即推`);
      }
      return;
    }

    if (type === 'block_start') {
      logLine('block_start', `${titleOf(d.id)} <span class="dim">开始加载</span>`);
      return;
    }

    if (type === 'block_progress') {
      /* done=0 是**扫描前补尾**阶段的进度（label 形如「补尾 3/6 000878」）。
         那段是串行网络请求、原来的实现整段静默，用户只看到「大屏卡住几分钟」
         —— 故这里优先展示 label，而不是显示误导性的「0/74」。 */
      const phase = d.done ? `策略扫描 ${d.done}/${d.total}` : '策略扫描 · 补尾中';
      logLine('block_progress', `${phase} <span class="dim">${esc(d.label || '')}</span>`);
      const badge = `<span class="b b-scan">${esc(phase)}</span>`;
      markMeta('portfolio', `${badge} ${metaBadge(state.blocks.portfolio)}`);
      markMeta('watchlist', badge);
      return;
    }

    if (type === 'block_done') {
      const id = d.id;
      state.blocks[id] = { status: 'done', cache: !!d.cache, degraded: (d.degraded || []).join('；'), error: null, data: d.data };
      if (id === 'market') updateSessionPill();
      if (id === 'strategy_scan') {
        state.scan = d.data;
        state.scanned = true;
        const sum = (d.data && d.data.summary) || {};
        logLine('block_done', `策略扫描 <span class="dim">· 命中 ${sum.hit_codes || 0}/${sum.codes || 0} 只 · ${sum.signals || 0} 个信号</span>`);
        renderBlock('portfolio'); renderBlock('watchlist');
        return;
      }
      state.completed++;
      const s = state.blocks[id];
      logLine('block_done', `${titleOf(id)} <span class="dim">· ${s.cache ? '缓存命中' : '直读'}${s.degraded ? ` · 降级（${esc(s.degraded)}）` : ''}</span>`);
      renderBlock(id);
      if (state.runKind === 'auto') {
        const bodyEl = document.querySelector(`[data-body="${id}"]`);
        if (bodyEl) { bodyEl.classList.remove('flash'); void bodyEl.offsetWidth; bodyEl.classList.add('flash'); }
      }
      return;
    }

    if (type === 'block_error') {
      const id = d.id;
      if (id === 'strategy_scan') {
        /* 扫描块在 PLAN 之外，计入 failed 会对不上计数；只标扫描不可用 */
        state.scanFailed = true;
        logLine('block_error', `策略扫描 <span class="dim">失败：${esc(d.error || '未知错误')} · 策略扫描不可用</span>`);
        renderBlock('portfolio'); renderBlock('watchlist');
        return;
      }
      const visible = !!state.blocks[id];
      state.blocks[id] = { status: 'error', cache: false, degraded: '', error: d.error || '未知错误', data: null };
      if (visible) state.completed++;
      state.failed++;
      logLine('block_error', `${titleOf(id)} <span class="dim">失败：${esc(d.error || '未知错误')}</span>`);
      if (visible) renderBlock(id);
      return;
    }

    if (type === 'final') {
      /* 独立重试流必须最先短路：进度条 / 强制刷新按钮 / ovMeta / 计时器
         全部归主流管，否则重试的 final 会把仍在跑的流显示成「已完成」，
         并顺手 stopElapsed() 把主流的耗时计时也停掉。 */
      if (runRole === 'retry') {
        logLine('final', `重试完成 <span class="dim">${blocksSummaryLine()}</span>`);
        updateSessionPill();
        return;
      }
      stopElapsed();
      const s = ((Date.now() - state.runStart) / 1000).toFixed(1);
      const total = PLAN.length || d.total || 0;
      if (!total) {
        logLine('final', '总览服务未启用');
        setProgress('总览服务未启用（OVERVIEW_ENABLED=false）', 0, true);
      } else if (state.runKind === 'auto') {
        logLine('final', `自动刷新完成（行情类 ${state.completed} 块） <span class="dim">· 总耗时 ${s}s</span>`);
        setProgress(`自动刷新完成（行情类 <b>${state.completed}</b> 块，静默更新）· 总耗时 <b>${s}s</b>`, 100, false);
      } else {
        logLine('final', `完成 ${state.completed}/${total}${state.failed ? ` · 失败 ${state.failed} 个区块（可单独重试）` : ' · 全部就绪'} <span class="dim">· 总耗时 ${s}s</span>`);
        setProgress(`完成 <b>${state.completed}/${total}</b>${state.failed ? ` · <b style="color:#b42318">失败 ${state.failed}</b>` : ''} · 总耗时 <b>${s}s</b>`, state.completed / total * 100, state.failed > 0);
      }
      $('ovForceBtn').disabled = false; $('ovForceBtn').innerHTML = '⟳ 强制刷新';
      $('ovMeta').innerHTML = `更新于 <span class="mono">${now()}</span> · ${PLAN.length} 个区块 · ${state.failed ? state.failed + ' 个失败' : '全部正常'}`;
      updateSessionPill();
      state.running = false;
      return;
    }
  }

  /* 摘要片段（供重试日志用）。口径对齐主流 final：completed 含失败块，故用「完成」不用「成功」 */
  function blocksSummaryLine() {
    return `完成 ${state.completed}/${PLAN.length}${state.failed ? ` · 失败 ${state.failed}` : ''}`;
  }

  /* ── 自适应刷新（行情静默 + 运行状态轮询）与下钻 ── */

  function tradingNow() {
    const m = state.blocks.market && state.blocks.market.data && state.blocks.market.data.trading;
    if (typeof m === 'boolean') return m;      // 以市场块返回口径为准
    const d = new Date();
    const wd = d.getDay();
    if (wd === 0 || wd === 6) return false;
    const hm = d.getHours() * 60 + d.getMinutes();
    return hm >= 570 && hm <= 900;             // 兜底 09:30–15:00
  }

  function updateSessionPill() {
    const el = $('ovSessionPill');
    if (!el) return;
    const isTrading = tradingNow();
    el.className = 'ov-session ' + (isTrading ? 'trading' : 'closed');
    el.textContent = isTrading ? '盘中' : '盘后';
  }

  function numOr(v, dft) {
    const n = Number(v);
    return (v === null || v === undefined || v === '' || !isFinite(n)) ? dft : n;
  }

  function stopTimers() {
    timers.forEach(clearTimeout); timers = [];
    if (autoInterval) { clearInterval(autoInterval); autoInterval = null; }
    if (runtimeInterval) { clearInterval(runtimeInterval); runtimeInterval = null; }
  }

  function updateAutoPill() {
    const pill = $('ovAutoPill');
    if (!pill) return;
    if (!state.autoEnabled) { pill.className = 'ov-autopill off'; $('ovAutoText').textContent = '自动刷新已关'; return; }
    if (state.runKind !== 'auto' && !state.marketRefreshS) { pill.className = 'ov-autopill off'; $('ovAutoText').textContent = '自动刷新待命'; return; }
    pill.className = 'ov-autopill on';
    $('ovAutoText').textContent = `自动刷新 ${Math.max(0, Math.ceil(autoLeft))}s`;
  }

  function ensureTimers(cfg) {
    state.marketRefreshS = numOr(cfg && cfg.market_refresh_s, 60);
    const runtimeS = numOr(cfg && cfg.runtime_refresh_s, 10);
    stopTimers();
    if (runtimeS > 0) {
      runtimeInterval = setInterval(() => {
        if (document.hidden || !state.open) return;
        refreshRuntimeCard();
      }, runtimeS * 1000);
    }
    if (state.marketRefreshS > 0) {
      autoLeft = state.marketRefreshS;
      autoInterval = setInterval(() => {
        if (document.hidden || !state.open || state.running || !state.autoEnabled) return;
        if (!tradingNow()) { updateAutoPill(); updateSessionPill(); return; }
        autoLeft -= 1;
        updateAutoPill();
        if (autoLeft <= 0) {
          autoLeft = state.marketRefreshS;
          runStream('auto', MARKET_BLOCKS);   // 静默刷新：不置 loading、不重建骨架
        }
      }, 1000);
    }
    updateAutoPill();
    updateSessionPill();
  }

  async function refreshRuntimeCard() {
    const cur = state.blocks.runtime;
    if (!cur || cur.status !== 'done') return;
    try {
      const res = await fetch('/api/overview/snapshot?blocks=runtime');
      const body = await res.json();
      const entry = body && body.data && body.data.blocks && body.data.blocks.runtime;
      if (!entry || !entry.ok) return;
      const prev = state.blocks.runtime && state.blocks.runtime.data;
      const hist = ((prev && prev.cpuHist) || []).slice(-11);
      const data = { ...entry.data, cpuHist: hist.concat([entry.data.cpu_percent]) };
      state.blocks.runtime = { status: 'done', cache: false, degraded: '', error: null, data };
      renderBlock('runtime');
      const bodyEl = document.querySelector('[data-body="runtime"]');
      if (bodyEl) { bodyEl.classList.remove('flash'); void bodyEl.offsetWidth; bodyEl.classList.add('flash'); }
    } catch (e) { /* 轮询失败静默 */ }
  }

  function drillTo(target) {
    const R = window.StockRadar || {};
    const clicks = {
      'data': '#dataCenterBtn', 'config': '#configBtn', 'memory': '#memoryManagerBtn',
      'backtest': '#backtestBtn', 'quality': '#qualityPanelBtn',
    };
    if (target === 'monitor') { closePage(); R.monitorPanel && R.monitorPanel.toggle && R.monitorPanel.toggle(); return; }
    if (target === 'thesis') { closePage(); R.thesis && R.thesis.openPage && R.thesis.openPage(); return; }
    if (target === 'shortline') { closePage(); R.shortLine && R.shortLine.openPage && R.shortLine.openPage(); return; }
    if (target === 'eval') { closePage(); R.evalManager && R.evalManager.openPage && R.evalManager.openPage(); return; }
    if (target === 'skill') { closePage(); R.skillManager && R.skillManager.openPage && R.skillManager.openPage(); return; }
    if (target && clicks[target]) { closePage(); const el = document.querySelector(clicks[target]); if (el) el.click(); return; }
    if (target && /^\d{6}$/.test(target)) { closePage(); window.openKlineChart && window.openKlineChart(target); return; }
    // strategy:<strategy_id> → 打开回测面板并直接进「策略详情」（大屏策略健康度行的下钻）
    if (target && target.indexOf('strategy:') === 0) {
      const sid = target.slice(9);
      if (!sid) return;
      closePage();
      const p = window.initBacktestPanel && window.initBacktestPanel();
      if (p) { p.show(); p.openLifecycle(sid); }
      return;
    }
    if (target && target.indexOf('dialog:') === 0) { closePage(); R._selectDialog && R._selectDialog(target.slice(7)); return; }
  }

  function loadInitial() { renderGrid(); runStream('initial', null); }

  /* ── 事件绑定（index.html 该页脚本在 body 末尾，DOM 已就绪） ── */
  $('ovForceBtn').onclick = () => runStream('force', null);
  $('ovLogBtn').onclick = () => $('ovLog').classList.toggle('open');
  $('ovProgress').onclick = () => $('ovLog').classList.toggle('open');
  const autoPill = $('ovAutoPill');
  if (autoPill) {
    autoPill.onclick = (e) => {
      e.stopPropagation();
      state.autoEnabled = !state.autoEnabled;
      if (state.autoEnabled) autoLeft = state.marketRefreshS;
      updateAutoPill();
    };
  }
  updateAutoPill();
  /* 会话档案搜索：只刷新列表区，保住输入框焦点（整块重渲染会把焦点连同输入框一起丢掉） */
  $('ovShell').addEventListener('input', (e) => {
    if (e.target && e.target.id === 'ovSessSearch') {
      state.sessionQuery = e.target.value || '';
      refreshSessionList();
    }
  });
  $('ovShell').addEventListener('click', (e) => {
    const retry = e.target.closest('[data-retry]');
    if (retry) { runStream('force', [retry.dataset.retry]); return; }
    const collapse = e.target.closest('.ov-collapse');
    if (collapse) { collapse.closest('.ov-card').classList.toggle('collapsed'); return; }
    const pager = e.target.closest('[data-pager]');
    if (pager) {
      const ps = pagerState(pager.dataset.block);
      const act = pager.dataset.pager;
      if (act === 'prev') ps.page = Math.max(1, ps.page - 1);
      else if (act === 'next') ps.page += 1;
      else if (act === 'expand') ps.expanded = true;
      else if (act === 'collapse') { ps.expanded = false; ps.page = 1; }
      renderBlock(pager.dataset.block);
      return;
    }
    /* 上下文洞察三块的本地交互（只重渲染块体，不触网、不重跑流） */
    const heat = e.target.closest('[data-heat]');
    if (heat) {
      if (heat.dataset.heatNone) return;
      const day = heat.dataset.heat || '';
      state.sessionFilterDate = (state.sessionFilterDate === day) ? '' : day;
      renderBlock('activity');
      renderBlock('sessions');
      return;
    }
    const heatClear = e.target.closest('[data-heat-clear]');
    if (heatClear) {
      state.sessionFilterDate = '';
      renderBlock('activity');
      renderBlock('sessions');
      return;
    }
    const orderChip = e.target.closest('[data-sess-order]');
    if (orderChip) {
      state.sessionOrder = orderChip.dataset.sessOrder || 'recent';
      const card = orderChip.closest('.ov-card');
      if (card) {
        card.querySelectorAll('[data-sess-order]').forEach((c) => {
          c.classList.toggle('on', c.dataset.sessOrder === state.sessionOrder);
        });
      }
      refreshSessionList();
      return;
    }
    const nav = e.target.closest('[data-nav]');
    if (nav) drillTo(nav.dataset.nav);
  });

  /* ── 策略徽标悬浮明细（原型 bind() 的 ovTip 逻辑） ── */
  const tip = $('ovTip');
  if (tip) {
    document.addEventListener('mouseover', (e) => {
      const b = e.target.closest && e.target.closest('.strat-badge');
      if (!b) { tip.style.display = 'none'; return; }
      tip.innerHTML = b.dataset.tip || '';
      tip.style.display = 'block';
    });
    document.addEventListener('mousemove', (e) => {
      if (tip.style.display !== 'block') return;
      tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 10) + 'px';
      tip.style.top = Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 10) + 'px';
    });
    document.addEventListener('mouseout', (e) => {
      if (e.target.closest && e.target.closest('.strat-badge')) tip.style.display = 'none';
    });
    $('ovShell').addEventListener('scroll', () => { tip.style.display = 'none'; }, true);
  }

  renderGrid();

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.overview = { openPage, closePage, isOpen: () => state.open };
})();
