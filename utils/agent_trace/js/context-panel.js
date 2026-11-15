/* ── 上下文面板（Trace Viewer「上下文」tab）──────────────────────────────
   视觉基线：docs/prototypes/2026-10-17-context-insight.html 的 section#skinA（暗色），
             逐卡对照移植、class 名与原型一致；颜色只用 css/base.css 已有变量
             （见 css/context-panel.css 顶部 --c1..--c8 别名声明）。
   口径见 utils/agent_trace/context_stats.py 顶部四条铁律：
     1) 双口径不混：billed（逐次求和）!= current（最后一次调用的 prompt）
     2) 输出 = 思考 + 回答：reasoning_tokens 含在 output_tokens 内，不并列相加、不参与计价
     3) 未采集 != 0：null 显示「未采集」、cost 为 null 显示「未计价」，绝不显示 0
     4) 耗时只报 Σ，不建父子树；Σ 超墙钟时显式报 overlap，绝不当占用率
   数据来源：GET /api/runs/{id}/context（build_run_context 快照，steps 已由服务端剔除）。
   字段按计划任务 10 的绑定表取，不自行找字段。 */
window.ContextPanel = (function () {
  'use strict';

  var C = 339.292;      // r=54 的周长（环形 dasharray 用）
  var MAX_EVENTS = 24;  // 事件表最多渲染条数（超出只报总数，避免长 run 淹没面板）
  var loaded = {};      // runId -> 快照（null = 已拉取但不可用）；同一 run 只拉一次
  var trendState = { x: 'call', y: 'total' };   // x: call=调用序号 / phase=阶段；y: total / delta

  var KCLS = { compress: 'k-compress', inject: 'k-memory', skill: 'k-skill', fork: 'k-fork' };
  var KNAME = { compress: '压缩', inject: '注入', skill: '技能', fork: 'fork' };
  // detail.event 子类型：core.record_skill_event 落库的 kind（含 reselect，core.py 的 label 表里没有它，UI 就地补中文）
  var SKILL_EVENT = {
    select: '初次选择', activate: '激活', switch: '主技能切换',
    reselect: '只换工具批', gap_reselect: '技能缺口重选'
  };
  // 六分类：与 context_stats.CATEGORIES 同名（tools 结构性来自网关埋点，未埋点时为 null）
  var CATS = [
    { k: 'system',       nm: '系统提示词', c: '--c1', src: '按 role=system 的消息字符数估算（1.6 字符/token）' },
    { k: 'tools',        nm: '工具定义',   c: '--c2', src: '来源：gateway bind_tools 拦截的工具 schema 字符数 ÷ 1.6；未埋点的历史 run 为未采集' },
    { k: 'user',         nm: '用户消息',   c: '--c3', src: '按 role=human 且未命中注入标记的部分估算' },
    { k: 'injected',     nm: '注入内容',   c: '--c4', src: '命中注入标记的部分（[市场状态]/[相关记忆片段]/[用户持仓账本]/【已压缩历史轮次摘要）——已知是下界' },
    { k: 'assistant',    nm: '助手消息',   c: '--c5', src: '按 role=ai 的正文估算（不含 reasoning，思考走输出拆分）' },
    { k: 'tool_results', nm: '工具结果',   c: '--c6', src: '按 role=tool 的消息字符数估算' }
  ];
  var C_THINK = '--c7', C_ANSWER = '--c8';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function ms(v) {
    if (v == null) return '—';
    var s = Math.round(v / 1000);
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (h) return h + 'h' + String(m).padStart(2, '0') + 'm';
    if (m) return m + 'm' + String(s % 60).padStart(2, '0') + 's';
    return s + 's';
  }

  function tok(v) {
    if (v == null) return '未采集';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'k';
    return String(v);
  }

  /* 刻度用短记法（原型是 250k / 500k / 1.0M，tok() 的 250.0k 太碎） */
  function tokShort(v) {
    if (v == null) return '—';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return Math.round(v / 1e3) + 'k';
    return String(v);
  }

  function pct(v) {
    if (v == null || !isFinite(v)) return '未采集';
    return (v * 100).toFixed(1) + '%';
  }

  /* 占比：分母为 0 / 值未采集时给「—」而不是 0%（铁律 3） */
  function share(v, total) {
    if (v == null || !total) return '—';
    return (v / total * 100).toFixed(1) + '%';
  }

  function ratioTxt(v) {
    if (v == null || !isFinite(v)) return '—';
    return (v * 100).toFixed(1) + '%';
  }

  /* 费用：null = 未计价（不折成 ¥0），0 是「计价为 0」；小额保留 4 位小数 */
  function fmtCost(v) {
    if (v == null) return '未计价';
    return '¥' + (v !== 0 && Math.abs(v) < 0.01 ? v.toFixed(4) : v.toFixed(2));
  }

  /* KPI：value 是**已拼好的 HTML**（调用方负责转义来自 DB 的字符串），label/sub 一律转义 */
  function kpi(label, value, sub, cls) {
    return '<div class="kpi"><div class="k-l">' + esc(label) + '</div>'
      + '<div class="k-v' + (cls ? ' ' + cls : '') + '">' + (value == null ? '未采集' : value) + '</div>'
      + '<div class="k-s">' + esc(sub || '') + '</div></div>';
  }

  function card(title, hair, body, style) {
    return '<div class="card"' + (style ? ' style="' + style + '"' : '') + '>'
      + '<div class="card-h">' + title + '<span class="spacer"></span>'
      + (hair ? '<span class="hair">' + hair + '</span>' : '') + '</div>'
      + body + '</div>';
  }

  /* 环形图：segs = [{v, c}]，c 是 CSS 变量名（--c1..--c8）。v <= 0 / null 的段不画。
     stroke 走 inline style（不用 presentation attribute <circle stroke="var(--c1)">）——
     内联样式对 var() 的支持是确定的，视觉与原型一致。 */
  function ring(segs, mid) {
    var total = 0, i;
    for (i = 0; i < segs.length; i++) { if (segs[i].v > 0) total += segs[i].v; }
    var svg = '<svg width="132" height="132" viewBox="0 0 132 132">'
      + '<circle cx="66" cy="66" r="54" fill="none" stroke-width="15" style="stroke:var(--surface3)"/>';
    var off = 0;
    for (i = 0; i < segs.length; i++) {
      var s = segs[i];
      if (!(s.v > 0) || !total) continue;
      var len = C * s.v / total;
      svg += '<circle cx="66" cy="66" r="54" fill="none" stroke-width="15"'
        + ' style="stroke:var(' + s.c + ')"'
        + ' stroke-dasharray="' + len.toFixed(2) + ' ' + (C - len).toFixed(2) + '"'
        + ' stroke-dashoffset="' + (-off).toFixed(2) + '"/>';
      off += len;
    }
    svg += '</svg>';
    return '<div class="ring">' + svg + '<div class="ring-mid">' + mid + '</div></div>';
  }

  /* ── 1. KPI 条 ──────────────────────────────────────────────────────── */
  function renderKpis(snap) {
    var t = snap.totals || {}, w = snap.window || {};
    // 占用率分母 = 窗口上限；缓存命中分母 = input_tokens —— 没有分母一律 null（铁律 3）
    var occ = (w.limit && t.current_input_tokens != null) ? t.current_input_tokens / w.limit : null;
    var cache = t.input_tokens ? t.cached_tokens / t.input_tokens : null;
    return '<div class="grid g-kpi" style="margin-bottom:10px">'
      + kpi('计费 Token（逐次求和）', tok(t.billed_tokens),
            '↑' + tok(t.input_tokens) + ' / ↓' + tok(t.output_tokens), 'c-accent')
      + kpi('当前上下文占用', pct(occ), tok(t.current_input_tokens) + ' / ' + tok(w.limit), 'c-warn')
      + kpi('缓存命中', pct(cache), '缓存 ' + tok(t.cached_tokens) + ' / 输入 ' + tok(t.input_tokens), 'c-ok')
      + kpi('LLM 调用 / 步骤', t.llm_calls + ' <span class="dim">/ ' + t.steps + '</span>',
            '轮次未采集（round_no 未埋点）')
      + kpi('工具调用', t.tool_calls, 'step_type=tool 计数', 'c-purple')
      + kpi('费用（记录时计价）', fmtCost(t.cost), t.cost == null ? '无价目快照或未埋点' : '峰谷已折')
      + '</div>';
  }

  /* ── 2. Token 统计环形（六分类 + 思考/回答两段）────────────────────── */
  function renderTokenRing(snap) {
    var comp = snap.composition || {}, sp = snap.output_split || {};
    var rows = [], i;
    for (i = 0; i < CATS.length; i++) {
      rows.push({ nm: CATS[i].nm, c: CATS[i].c, v: comp[CATS[i].k] == null ? null : comp[CATS[i].k] });
    }
    rows.push({ nm: '思考（含在输出内）', c: C_THINK, v: sp.thinking });
    rows.push({ nm: '回答（正文）', c: C_ANSWER, v: sp.answer });
    var total = 0, missing = 0, segs = [], html = '';
    for (i = 0; i < rows.length; i++) {
      if (rows[i].v == null) { missing++; } else { total += rows[i].v; segs.push({ v: rows[i].v, c: rows[i].c }); }
    }
    for (i = 0; i < rows.length; i++) {
      var r = rows[i];
      html += '<div class="li"><i class="dot" style="background:var(' + r.c + ')"></i>'
        + '<span class="nm">' + esc(r.nm)
        + (r.v == null ? ' <span class="badge-null">未采集</span>' : '') + '</span>'
        + '<span class="vl">' + tok(r.v) + '</span>'
        + '<span class="pc">' + (r.v == null ? '—' : share(r.v, total)) + '</span></div>';
    }
    var out = (sp.thinking || 0) + (sp.answer || 0);
    var mid = '<div class="v">' + tok(total) + '</div><div class="l">环形合计（估算）</div>'
      + (out ? '<div class="r">思考占输出 ' + pct((sp.thinking || 0) / out) + '</div>' : '');
    /* ⚠️ 口径标注必须直白地写在 UI 上（用户实测误读点）：「六分类」是**最后一次调用那一次
       prompt 的完整构成**（六个桶全在里面），**不是「本次新增的增量」**；而上方 KPI 的
       「计费 Token」是全部调用逐次求和 —— 上下文每次调用都会重发，两者天然差几倍、不可直接比。
       实测样本：末次桶合计 42,015 / 18 次 Σ桶 279,267 / provider 报的 Σ输入 231,118。 */
    var hairTip = '口径：六分类 = 最后一次 LLM 调用发出时整段 prompt 的完整构成'
      + '（系统提示词 / 工具定义 / 用户消息 / 注入内容 / 助手消息 / 工具结果，六桶全在）。'
      + '① 不是「本次新增的增量」；② 不是全 run 求和 —— 每次调用都会重发整个上下文，'
      + '这里只是「那一次」的快照；③ 与上方 KPI「计费 Token（逐次求和）」不是同一口径，'
      + '不可直接比（实测样本：末次桶合计 42,015，18 次 Σ桶 279,267，provider 报的 Σ输入 231,118）。'
      + ' 右侧「思考 / 回答」才是全 run Σ 口径。';
    var hair = '<span title="' + esc(hairTip) + '"'
      + ' style="border-bottom:1px dotted currentColor;cursor:help">末次调用的 prompt 完整构成'
      + '（非增量 / 非全 run 求和）</span>'
      + (missing ? ' · 未采集段不入环（占比按已知段归一）' : '');
    return card('Token 统计', hair,
      '<div class="ring-wrap">' + ring(segs, mid) + '<div class="legend">' + html + '</div></div>');
  }

  /* ── 3. 耗时统计环形（Σ 口径）──────────────────────────────────────── */
  function renderDurationRing(snap) {
    var tm = snap.timing || {};
    var sum = (tm.llm_ms || 0) + (tm.tool_ms || 0);
    var segs = [{ v: tm.llm_ms || 0, c: '--c1' }, { v: tm.tool_ms || 0, c: '--c6' }];
    var over = tm.sum_ratio != null && tm.sum_ratio > 1;
    var legend = ''
      + '<div class="li"><i class="dot" style="background:var(--c1)"></i><span class="nm">模型生成 Σ</span>'
      + '<span class="vl">' + ms(tm.llm_ms) + '</span><span class="pc">' + share(tm.llm_ms, sum) + '</span></div>'
      + '<div class="li"><i class="dot" style="background:var(--c6)"></i><span class="nm">工具执行 Σ</span>'
      + '<span class="vl">' + ms(tm.tool_ms) + '</span><span class="pc">' + share(tm.tool_ms, sum) + '</span></div>'
      + '<div class="li" style="margin-top:4px"><span class="nm dim">墙钟</span>'
      + '<span class="vl">' + ms(tm.wall_ms) + '</span><span class="pc">' + (tm.wall_ms ? '100%' : '—') + '</span></div>'
      + '<div class="li"><span class="nm dim">Σ / 墙钟</span>'
      + '<span class="vl">' + ratioTxt(tm.sum_ratio) + '</span>'
      + '<span class="pc"' + (over ? ' style="color:var(--warning)"' : '') + '>'
      + (tm.sum_ratio == null ? '墙钟未采集' : (over ? '超了' : '未超')) + '</span></div>';
    var mid = '<div class="v">' + ms(tm.wall_ms) + '</div><div class="l">墙钟</div>';
    // ⚠️ 调用内部的分段口径本 run 未采集：这里只说明缺口在哪，不给数值、不给空槽位（口径标记见设计 §4.6）
    var honest = '<div class="honest"><span class="badge-null">单次调用内部分段未采集</span>'
      + ' 别混淆：<b>思考 token 是能统计的</b>（见下方「思考统计」）。做不了的是'
      + ' <b>单次调用内部的时间分段</b> —— 那需要流式埋点，本 run 未采集，所以这里不画任何槽位。'
      + ' <b>跨步骤的耗时归因是能做的</b>，见下方「耗时归因」。</div>';
    return card('耗时统计', 'Σ 口径，按 Σ 归一',
      '<div class="ring-wrap">' + ring(segs, mid) + '<div class="legend">' + legend + '</div></div>' + honest);
  }

  /* ── 4. 耗时归因：Σ 口径 + 口径标记 + overlap 显式上报（不建父子树）── */
  function renderTiming(tm, slowest) {
    var wall = tm.wall_ms;
    var ratio = tm.sum_ratio;
    var ratioText = ratio == null ? '—' : (ratio * 100).toFixed(1) + '%';
    var sub = tm.overlap_ms == null
      ? '墙钟未采集，无法归因'
      : (tm.overlap_ms > 0 ? ('含并发/重试重叠 ' + ms(tm.overlap_ms)) : '无重叠');
    var html = '<div class="card" style="margin-bottom:10px">';
    html += '<div class="card-h">耗时归因'
      + '<span class="spacer"></span>'
      + '<span class="hair">Sigma 口径，不建父子树</span></div>';
    html += '<div class="grid g-kpi" style="margin-bottom:10px">';
    html += kpi('墙钟（分母）', ms(tm.wall_ms), 'runs.duration_ms');
    // ⚠️ 口径标记，**必须保留这半句字样**（设计 §4.6 / §8b：「LLM 响应耗时（含首 token 等待，未拆分）」
    //    必须出现在耗时归因区）。同一条口径**只能有标记、不能有数值** —— 本区内不得出现任何
    //    「首 token 时延」或「等待 / 生成分段」的数值与空槽位（agent 主路径走非流式，本来就没拆）。
    html += kpi('Sigma LLM 生成', '<span style="color:var(--c1)">' + ms(tm.llm_ms) + '</span>',
                '响应耗时（含首 token 等待，未拆分）');
    html += kpi('Sigma 工具执行', '<span style="color:var(--c6)">' + ms(tm.tool_ms) + '</span>', '按 step_type 求和');
    html += kpi('Sigma / 墙钟', ratioText, sub);
    html += '</div>';
    // 各口径相对墙钟的横向条（每条都按 100% = 墙钟画）。Σ 可超 100%：条封顶 100%，文字报真值
    html += '<div class="small dim" style="margin-bottom:5px">各口径相对墙钟（每条都按 100% = 墙钟 画，因此可以这样横向对比）</div>';
    html += '<div class="barrows">';
    var bars = [['LLM 生成', tm.llm_ms, '--c1'], ['工具执行', tm.tool_ms, '--c6'], ['数据采集', tm.collector_ms, '--c2']];
    for (var i = 0; i < bars.length; i++) {
      var v = bars[i][1];
      var wpx = (wall > 0 && v != null) ? Math.min(100, v / wall * 100) : 0;
      html += '<div class="br"><span class="br-l">' + bars[i][0] + '</span>'
        + '<span class="br-t"><i style="width:' + wpx.toFixed(2) + '%;background:var(' + bars[i][2] + ')"></i></span>'
        + '<span class="br-v">' + ms(v) + ' · ' + (wall > 0 ? share(v, wall) : '未采集') + '</span></div>';
    }
    html += '</div>';
    // 最慢的 LLM 调用（找优化切入点：prompt 越大越慢）
    html += '<div class="small" style="margin:12px 0 5px;color:var(--text-dim)">最慢的 LLM 调用 · 按单次耗时排序（<b>prompt 越大越慢 → 切入点在这</b>）</div>';
    html += '<div class="ev">';
    html += '<div class="row" style="grid-template-columns:1fr 74px 68px 78px">'
      + '<span class="d" style="color:var(--text-dim)">标签 / 节点</span>'
      + '<span class="j">input_tokens</span><span class="j">耗时</span><span class="j">ms/千token</span></div>';
    var rows = slowest || [];
    if (!rows.length) {
      html += '<div class="row" style="grid-template-columns:1fr"><span class="d dim">本 run 没有真实 LLM 调用（cache_hit 补记的伪步骤不计）</span></div>';
    }
    for (var j = 0; j < rows.length; j++) {
      var r = rows[j];
      var nm = r.label ? (r.node ? r.label + ' · ' + r.node : r.label) : (r.node || '—');
      html += '<div class="row" style="grid-template-columns:1fr 74px 68px 78px">'
        + '<span class="d" title="' + esc(nm) + '">' + esc(nm) + '</span>'
        + '<span class="j">' + tok(r.input_tokens) + '</span>'
        + '<span class="j">' + ms(r.duration_ms) + '</span>'
        + '<span class="j">' + (r.ms_per_1k == null ? '—' : r.ms_per_1k) + '</span></div>';
    }
    html += '</div>';
    html += '<div class="honest"><b>为什么不画父子归因树</b>：实测 <span class="mono">parent_run_id</span> 只有 <b>79.6%</b> 非空，'
      + '一个 253 步的 run 里有 <b>35 个孤儿「根」</b>；平铺求和是墙钟的 <b>4.24 倍</b>，'
      + '而「自身 − 子区间」也会高估（实测 216 万 ms vs 根 107 万 ms）。因此本轮只用'
      + ' <span class="mono">step_type</span> + 时间区间，<b>不宣称独占时间</b>；要精确到节点自耗时，'
      + '得先把 <span class="mono">parent_run_id</span> 的覆盖面补上（独立议题）。另：'
      + '<b>Σ 可以超过 100%</b>（并发 / 重试），所以这个比值当「下界」看，别当占用率。</div>';
    html += '</div>';
    return html;
  }

  /* ── 5. 思考统计 ────────────────────────────────────────────────────── */
  function renderThinking(snap) {
    var t = snap.totals || {};
    var out = (t.thinking_tokens || 0) + (t.answer_tokens || 0);
    // 分子优先直读快照现成字段 calls_with_thinking；只有它缺失时才用覆盖率 × 调用数反推
    // （反推要过比值再取整，双重舍入会漂 —— 别当默认路径）
    var calls;
    if (typeof t.calls_with_thinking === 'number') {
      calls = t.calls_with_thinking;
    } else if (t.thinking_calls_coverage == null) {
      calls = null;
    } else {
      calls = Math.round(t.thinking_calls_coverage * (t.llm_calls || 0));
    }
    var html = '<div class="grid g-kpi" style="margin-bottom:9px">';
    html += kpi('思考 Token', '<span style="color:var(--c7)">' + tok(t.thinking_tokens) + '</span>',
                '占输出 ' + pct(out ? (t.thinking_tokens || 0) / out : null));
    html += kpi('回答 Token', '<span style="color:var(--c8)">' + tok(t.answer_tokens) + '</span>',
                '占输出 ' + pct(out ? (t.answer_tokens || 0) / out : null));
    html += kpi('有思考的调用', calls == null ? '未采集'
                : calls + ' <span class="dim">/ ' + (t.llm_calls || 0) + '</span>',
                pct(t.thinking_calls_coverage) + ' 覆盖');
    html += kpi('思考内容条数', '未采集', 'messages.reasoning 未随快照返回');
    html += '</div>';
    html += '<div class="small dim" style="margin-bottom:2px">逐次调用：'
      + '<span style="color:var(--c7)">■ 思考</span> + <span style="color:var(--c8)">■ 回答</span>'
      + '（与上下文趋势同 x 轴）</div>';
    var reqs = snap.requests || [];
    var max = 0, i, bars = '';
    for (i = 0; i < reqs.length; i++) {
      var v = (reqs[i].thinking_tokens || 0) + (reqs[i].answer_tokens || 0);
      if (v > max) max = v;
    }
    for (i = 0; i < reqs.length && max > 0; i++) {
      var r = reqs[i], th = r.thinking_tokens || 0, an = r.answer_tokens || 0;
      var tip = '第 ' + (i + 1) + ' 次调用 · 思考 ' + tok(th) + ' + 回答 ' + tok(an);
      bars += '<div class="trend-col" title="' + esc(tip) + '">'
        + '<i style="height:' + (th / max * 100).toFixed(1) + '%;background:var(--c7)"></i>'
        + '<i style="height:' + (an / max * 100).toFixed(1) + '%;background:var(--c8)"></i></div>';
    }
    html += '<div class="trend" style="height:62px">' + bars + '</div>';
    if (!bars) html += '<div class="small dim">逐次思考 / 回答均为 0 或未采集</div>';
    return card('思考统计', 'reasoning_tokens · 含在 output_tokens 内，不额外计价', html, 'margin-bottom:10px');
  }

  /* ── 6. 当前上下文分段条（六色 + 两段掩码 + 图例）──────────────────── */
  function renderContext(snap) {
    var w = snap.window || {}, comp = snap.composition || {}, t = snap.totals || {};
    var limit = w.limit, cur = t.current_input_tokens;
    var occ = (limit && cur != null) ? cur / limit : null;
    var sum = 0, missing = 0, i, segs = '', legend = '';
    for (i = 0; i < CATS.length; i++) {
      if (comp[CATS[i].k] == null) missing++; else sum += comp[CATS[i].k];
    }
    for (i = 0; i < CATS.length; i++) {
      var v = comp[CATS[i].k];
      if (v == null) continue;   // 未采集不画段：不假装它是 0（铁律 3）
      // ⚠️ 硬约束：段宽是「占窗口」比（分类 token ÷ window.limit × 100），不是占上下文比
      var wpct = limit ? (v / limit * 100) : 0;
      segs += '<i style="width:' + wpct.toFixed(3) + '%;background:var(' + CATS[i].c + ')"></i>';
      legend += '<div class="li"><i class="dot" style="background:var(' + CATS[i].c + ')"></i>'
        + '<span class="nm">' + esc(CATS[i].nm) + '</span>'
        + '<span class="vl">' + tok(v) + '</span>'
        + '<span class="pc">' + share(v, sum) + '</span></div>';
    }
    // 两段掩码：window 里没有 reserve_headroom / reserve_output（任务 9 未装配，见设计 §4.2 的目标形态注）
    // —— 无数据不画段、不填 0，只把槽位显式标成未采集（铁律 3）
    legend += '<div class="li"><i class="dot dot-mask-reserve"></i>'
      + '<span class="nm">压缩预留 headroom <span class="badge-null">未采集</span></span>'
      + '<span class="vl">未采集</span><span class="pc">—</span></div>';
    legend += '<div class="li"><i class="dot dot-mask-out"></i>'
      + '<span class="nm">预留输出窗口 <span class="badge-null">未采集</span></span>'
      + '<span class="vl">未采集</span><span class="pc">—</span></div>';
    var tip = '占用 ' + tok(cur) + ' (' + pct(occ) + ')｜压缩预留 headroom 未采集｜预留输出窗口 未采集';
    var head = '<b style="white-space:nowrap">当前上下文</b><span class="spacer"></span>'
      + '<span class="hair">最后一次请求的 prompt（非逐次求和）· 条形按窗口绘制，百分比为占上下文比</span>'
      + '<span style="font-size:12px;font-weight:700;color:var(--text)">' + tok(cur) + '</span>'
      + '<span class="hair">/ ' + tok(limit) + ' tokens</span>'
      + '<span style="font-size:12px;font-weight:700;color:var(--warning)">' + pct(occ) + '</span>';
    var body = '<div class="occ" title="' + esc(tip) + '">' + segs + '</div>';
    body += '<div class="occ-scale"><span>0</span><span>' + tokShort(limit / 4) + '</span>'
      + '<span>' + tokShort(limit / 2) + '</span><span>' + tokShort(limit * 0.75) + '</span>'
      + '<span>' + tokShort(limit) + ' 窗口</span></div>';
    // 把「已画合计 vs 占用率」的差额说清楚 —— ⚠️ 差额**方向不固定**，两个方向的原因不同：
    //   * 已画 < 占用率：主要是未采集的分类不画段（本库历史 run 的工具定义全部未埋点），另含估算偏差；
    //   * 已画 > 占用率：只可能是**字符→token 估算偏差**（六分类按消息字符估算，占用率来自 provider 的
    //     input_tokens，两者本来就不必逐 token 相等）—— 实测真 run 里两个方向都常见（本库 13 < / 3 >），
    //     把它一律说成「未采集」就是把因果说反。
    // 两个数本身都是实测值（不折算），改的只是这半句解释；差额小于显示精度（0.01pp）时不刷这行。
    var drawnPct = limit ? sum / limit * 100 : null;
    var occPct = (occ == null) ? null : occ * 100;
    if (drawnPct != null && occPct != null && Math.abs(drawnPct - occPct) >= 0.005) {
      var why = drawnPct > occPct
        ? '已画合计已超占用率 —— 字符→token 估算偏差（六分类按消息字符估算，占用率来自 provider 的 input_tokens）'
        : (missing
          ? '差额来自未采集的分类（不画段、也不填 0），另含字符→token 估算偏差'
          : '六分类按消息字符估算，占用率来自 provider 的 input_tokens，两者不必逐 token 相等');
      body += '<div class="small dim" style="margin-top:4px">已画 ' + (CATS.length - missing) + ' 段合计 <b>'
        + drawnPct.toFixed(2) + '%</b>（占窗口）；占用率 <b>' + occPct.toFixed(2) + '%</b> —— ' + why + '</div>';
    }
    body += '<div class="legend" style="grid-template-columns:repeat(auto-fit,minmax(152px,1fr));margin-top:8px">'
      + legend + '</div>';
    return card(head, '', body, 'margin-bottom:10px');
  }

  /* ── 7. 上下文趋势（x：调用序号 / 阶段 × y：总量 / 增幅）────────────── */
  function trendHtml(snap) {
    var reqs = snap.requests || [];
    if (!reqs.length) return '<div class="small dim">本 run 没有真实 LLM 调用</div>';
    var i, j, rows = [], max = 0;
    for (i = 0; i < reqs.length; i++) {
      var r = reqs[i], tk = r.tokens || {}, sum = 0;
      for (j = 0; j < CATS.length; j++) { if (tk[CATS[j].k] != null) sum += tk[CATS[j].k]; }
      var think = r.thinking_tokens || 0, ans = r.answer_tokens || 0;
      var row = { sum: sum, think: think, ans: ans, tot: sum + think + ans, r: r, n: i + 1 };
      row.delta = i ? (row.tot - rows[i - 1].tot) : null;
      if (row.tot > max) max = row.tot;
      rows.push(row);
    }
    var usePhase = trendState.x === 'phase';
    var phaseSeen = false;
    for (i = 0; i < rows.length; i++) { if (rows[i].r.phase) phaseSeen = true; }
    var maxAbs = 0;
    for (i = 0; i < rows.length; i++) { if (rows[i].delta != null && Math.abs(rows[i].delta) > maxAbs) maxAbs = Math.abs(rows[i].delta); }
    var cols = '<div class="trend-grid"></div>', xhtml = '';
    var step = Math.max(1, Math.ceil(rows.length / 12));
    for (i = 0; i < rows.length; i++) {
      var row2 = rows[i], r2 = row2.r;
      var name = r2.label ? (r2.node ? r2.label + ' · ' + r2.node : r2.label) : (r2.node || ('#' + row2.n));
      var bar = '';
      if (trendState.y === 'delta') {
        if (row2.delta != null && maxAbs > 0) {
          var dh = Math.abs(row2.delta) / maxAbs * 100;
          var col = row2.delta < 0 ? 'var(--error)' : 'var(--c5)';
          bar = '<i style="height:' + dh.toFixed(1) + '%;background:' + col + '"></i>';
        }
      } else {
        var vals = [];
        for (j = 0; j < CATS.length; j++) { vals.push({ v: r2.tokens ? r2.tokens[CATS[j].k] : null, c: CATS[j].c }); }
        vals.push({ v: row2.think, c: C_THINK });
        vals.push({ v: row2.ans, c: C_ANSWER });
        if (max > 0) {
          for (j = 0; j < vals.length; j++) {
            if (vals[j].v == null || vals[j].v <= 0) continue;
            bar += '<i style="height:' + (vals[j].v / max * 100).toFixed(2) + '%;background:var(' + vals[j].c + ')"></i>';
          }
        }
      }
      var tip2 = '第 ' + row2.n + ' 次调用 · ' + esc(name) + '｜六分类 ' + tok(row2.sum)
        + ' + 思考 ' + tok(row2.think) + ' + 回答 ' + tok(row2.ans) + ' = ' + tok(row2.tot)
        + '（占窗口 ' + (snap.window && snap.window.limit ? (row2.tot / snap.window.limit * 100).toFixed(1) + '%' : '—') + '）'
        + (row2.delta == null ? '' : '｜较上次 ' + (row2.delta >= 0 ? '+' : '') + tok(row2.delta));
      var marked = (row2.delta != null && row2.tot < rows[i - 1].tot * 0.9) ? ' mark' : '';
      if (marked) tip2 += '｜比上次下降 ≥10%（压缩 / 裁剪可见）';
      cols += '<div class="trend-col' + marked + '" title="' + tip2 + '">' + bar + '</div>';
      var label = '';
      if (usePhase && phaseSeen) label = r2.phase || '';
      else if (i % step === 0 || i === rows.length - 1) label = String(row2.n);
      xhtml += '<span>' + esc(label) + '</span>';
    }
    var html = '<div class="trend">' + cols + '</div>';
    html += '<div class="trend-x">' + xhtml + '</div>';
    if (usePhase && !phaseSeen) {
      html += '<div class="small dim" style="margin-top:5px">阶段（phase）未采集：全仓只有 strategy 步写这个键，而 llm 步不带它 —— 已按调用序号显示</div>';
    }
    return html;
  }

  function renderTrend(snap) {
    var on = function (kind, val, text, tip) {
      return '<span class="chip' + (trendState[kind] === val ? ' on' : '')
        + '" data-tchip="' + val + '" data-tchip-kind="' + kind + '"'
        + (tip ? ' title="' + esc(tip) + '"' : '') + '>' + text + '</span>';
    };
    // ⚠️ x 轴实际是「调用序号」，不是轮次：round_no / phase 在快照里恒为 null（context_stats 已登记的边界，
    //    全仓只有 strategy 步写这两个键，而它们落在被过滤掉的非 llm 步上）。文案与 KPI 的
    //    「轮次未采集（round_no 未埋点）」保持一致，不假装有轮次数据。
    var head = '上下文趋势<span class="spacer"></span>'
      + on('x', 'call', '调用', '按调用序号（round_no 未埋点，快照里恒为 null）')
      + on('x', 'phase', '阶段', 'phase 未埋点（全仓只有 strategy 步写该键）；选中时按调用序号显示并给出提示')
      + '<span style="width:10px"></span>'
      + on('y', 'total', '总量') + on('y', 'delta', '增幅');
    return card(head, '', '<div id="ctxTrendBox">' + trendHtml(snap) + '</div>', 'margin-bottom:10px');
  }

  function bindTrend(root, snap) {
    root.querySelectorAll('[data-tchip]').forEach(function (chip) {
      chip.addEventListener('click', function () {
        trendState[chip.getAttribute('data-tchip-kind')] = chip.getAttribute('data-tchip');
        var box = document.getElementById('ctxTrendBox');
        if (box) box.innerHTML = trendHtml(snap);
        root.querySelectorAll('[data-tchip]').forEach(function (c) {
          c.classList.toggle('on', trendState[c.getAttribute('data-tchip-kind')] === c.getAttribute('data-tchip'));
        });
      });
    });
  }

  /* ── 8. 上下文事件 ─────────────────────────────────────────────────── */
  function hhmmss(at) {
    if (!at) return '—';
    var d = new Date(at);
    if (isNaN(d.getTime())) return String(at).slice(11, 19) || String(at);
    return d.toTimeString().slice(0, 8);
  }

  function eventDetail(e) {
    var d = e.detail || {};
    if (e.kind === 'compress') {
      var raw = d.raw_len, fin = d.final_len;
      var len = (raw == null || fin == null) ? '（长度未采集）' : ' ' + raw + '→' + fin + ' 字符';
      return '工具结果压缩 ' + (d.compress_method || 'compress') + ' · ' + (e.label || '') + len;
    }
    if (e.kind === 'inject') {
      return '该次 prompt 带 ' + (d.messages == null ? '?' : d.messages) + ' 条注入消息（逐次事件，不是注入次数）';
    }
    if (e.kind === 'skill') {
      var sub = SKILL_EVENT[d.event] || d.event || '';
      return '技能变更 · ' + (d.skill || '') + (sub ? '（' + sub + '）' : '');
    }
    if (e.kind === 'fork') {
      return '副调用 fork 复用热前缀' + (d.prefix_msgs == null ? '' : ' · 前缀 ' + d.prefix_msgs + ' 条消息');
    }
    return e.label || '';
  }

  function renderEvents(snap) {
    var evs = (snap.events || []).slice();
    // 与原型一致：新的在上（step 序与时间序同向，按时间倒序排）
    evs.sort(function (a, b) { return String(b.at || '').localeCompare(String(a.at || '')); });
    var counts = { compress: 0, skill: 0, fork: 0, inject: 0 }, i;
    for (i = 0; i < evs.length; i++) { counts[evs[i].kind] = (counts[evs[i].kind] || 0) + 1; }
    var head = '上下文事件<span class="spacer"></span><span class="hair">变更：</span>'
      + '<span class="small" style="color:var(--warning)">压缩 ' + counts.compress + '</span>'
      + '<span class="small" style="color:var(--accent)">fork ' + counts.fork + '</span>'
      + '<span class="small" style="color:var(--purple)">技能 ' + counts.skill + '</span>'
      + '<span class="small" style="color:var(--c4)" title="inject 是逐次事件：同一条含注入的消息会在此后每次调用各报一条，不能读成「注入了 N 次上下文」">注入 '
      + counts.inject + '（逐次）</span>';
    var html = '<div class="ev">';
    for (i = 0; i < evs.length && i < MAX_EVENTS; i++) {
      var e = evs[i], kind = KNAME[e.kind] ? e.kind : '';
      var delta = (e.delta_chars == null) ? '—' : (e.delta_chars > 0 ? '+' + e.delta_chars : String(e.delta_chars));
      html += '<div class="row"><span class="t">' + hhmmss(e.at) + '</span>'
        + '<span class="k' + (KCLS[kind] ? ' ' + KCLS[kind] : '') + '">' + esc(KNAME[e.kind] || e.kind) + '</span>'
        + '<span class="d" title="' + esc(eventDetail(e)) + '">' + esc(eventDetail(e)) + '</span>'
        + '<span class="j">' + esc(delta) + '</span></div>';
    }
    if (!evs.length) html += '<div class="row" style="grid-template-columns:1fr"><span class="d dim">本次 run 没有可识别的上下文变更事件</span></div>';
    html += '</div>';
    if (evs.length > MAX_EVENTS) {
      html += '<div class="small dim" style="margin-top:6px">…共 ' + evs.length + ' 条事件，此处只显示最近 ' + MAX_EVENTS + ' 条</div>';
    }
    return card(head, '', html);
  }

  /* ── 9. 上下文浏览器（六分类树；助手消息内思考与回答分列）──────────── */
  function renderBrowser(snap) {
    var comp = snap.composition || {}, t = snap.totals || {};
    var reqs = snap.requests || [], last = reqs.length ? reqs[reqs.length - 1] : null;
    var sum = 0, i;
    for (i = 0; i < CATS.length; i++) { if (comp[CATS[i].k] != null) sum += comp[CATS[i].k]; }
    // ⚠️ 差异登记：原型的 .cnt 列是「N 段 / N 个工具 / N 条」（消息条数），而快照里没有按类的消息条数
    //    （server.py:146 已 pop steps，build_run_context 也不返条数）—— 属数据不可得的合理降级，
    //    故这一列改显「占已知分类合计」的占比，不是口径变更。
    var rows = '';
    for (i = 0; i < CATS.length; i++) {
      var cat = CATS[i], v = comp[cat.k];
      var lines = '';
      if (cat.k === 'assistant') {
        lines += '<div class="ln">思考 ' + tok(t.thinking_tokens) + '（占输出 '
          + pct((t.thinking_tokens || 0) + (t.answer_tokens || 0) ? (t.thinking_tokens || 0) / ((t.thinking_tokens || 0) + (t.answer_tokens || 0)) : null)
          + '）· messages.reasoning 未随快照返回</div>';
        lines += '<div class="ln">回答（正文）' + tok(t.answer_tokens) + ' + tool_calls 载荷</div>';
      } else {
        lines += '<div class="ln">' + esc(cat.src) + '</div>';
      }
      rows += '<details><summary><i class="dot" style="background:var(' + cat.c + ')"></i>'
        + '<span class="nm">' + esc(cat.nm)
        + (v == null ? ' <span class="badge-null">未采集</span>' : '') + '</span>'
        + '<span class="cnt">' + (v == null ? '—' : share(v, sum)) + '</span>'
        + '<span class="tok">' + tok(v) + '</span></summary>'
        + '<div class="body">' + lines + '</div></details>';
    }
    var hair = '按最后一次请求逐段拆开';
    if (last) {
      var nm = last.label ? (last.node ? last.label + ' · ' + last.node : last.label) : (last.node || '');
      hair += ' · ' + esc(nm) + ' · input ' + tok(last.input_tokens) + ' · ' + ms(last.duration_ms);
    }
    return card('上下文浏览器', hair, '<div class="browser">' + rows + '</div>');
  }

  /* ── 装配 ──────────────────────────────────────────────────────────── */
  function renderInto(el, snap) {
    el.innerHTML = renderKpis(snap)
      + '<div class="grid g-auto" style="margin-bottom:10px">' + renderTokenRing(snap) + renderDurationRing(snap) + '</div>'
      + renderTiming(snap.timing || {}, snap.slowest_llm || [])
      + renderThinking(snap)
      + renderContext(snap)
      + renderTrend(snap)
      + '<div class="grid g-auto">' + renderEvents(snap) + renderBrowser(snap) + '</div>';
    bindTrend(el, snap);
  }

  function paint(el, snap) {
    if (!snap) { el.innerHTML = '<div class="small dim">上下文数据不可用（run 不存在或接口未返回）</div>'; return; }
    try {
      renderInto(el, snap);
    } catch (e) {
      // 单卡出错不该让整个 run 视图白屏：兜底成文案并留日志
      console.error('context panel render failed:', e);
      el.innerHTML = '<div class="small dim">上下文渲染失败：' + esc(e && e.message) + '</div>';
    }
  }

  function load(runId) {
    var el = document.getElementById('ctxPaneCtx');
    if (!el) return;
    if (runId in loaded) { paint(el, loaded[runId]); return; }   // 同一 run 只拉一次
    loadRunContext(runId).then(function (snap) {
      loaded[runId] = snap || null;
      var box = document.getElementById('ctxPaneCtx');
      if (box) paint(box, loaded[runId]);
    });
  }

  function mount(leftPanel, overviewHtml, runId) {
    leftPanel.innerHTML = '<div class="tabbar">'
      + '<label data-ctab="ov">概览（现状）</label>'
      + '<label data-ctab="ctx">上下文</label></div>'
      + '<div class="pane" id="ctxPaneOv">' + overviewHtml + '</div>'
      // ⚠️ 上下文 pane 必须初始 display:none —— 否则两个 pane 会同时显示（原型的 .pane{display:none} 由勾选器驱动）
      + '<div class="pane" id="ctxPaneCtx" style="display:none">加载中…</div>';
    function select(which) {
      leftPanel.querySelectorAll('[data-ctab]').forEach(function (x) {
        x.classList.toggle('on', x.getAttribute('data-ctab') === which);
      });
      var ov = document.getElementById('ctxPaneOv'), ctx = document.getElementById('ctxPaneCtx');
      if (ov) ov.style.display = which === 'ov' ? '' : 'none';
      if (ctx) ctx.style.display = which === 'ctx' ? '' : 'none';
      if (which === 'ctx') load(runId);
    }
    leftPanel.querySelectorAll('[data-ctab]').forEach(function (label) {
      label.addEventListener('click', function () { select(label.getAttribute('data-ctab')); });
    });
    // 每次 mount 都从「概览（现状）」起步 —— 保证概览 pane 可见时概览里的 sparkline 才按真实宽度绘制
    select('ov');
  }

  return { mount: mount, renderInto: renderInto, renderTiming: renderTiming, ms: ms, tok: tok };
})();
