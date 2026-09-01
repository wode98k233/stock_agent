/* ─────────────────────────────────────────────
   选股雷达 Web — 预算分配估算（紧凑版）
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.ConfigVizBudget = class ConfigVizBudget {
  constructor(params) {
    this.params = params;
  }

  updateParams(params) {
    this.params = params;
  }

  _isEnabled(key) {
    const value = this.params[key];
    return value === true || value === 'true' || value === 'True' || value === 1 || value === '1';
  }

  calcBudgetAllocation(mode) {
    const MAX = this.params.MAX_TOKENS_PER_QUERY || 50000;
    const MEM = this.params.MEMORY_MAX_TOKENS || 8000;

    // 更合理的估算基准（input + output 合计，单位 token）
    const COLORS = {
      classify: '#2563eb',
      select_skills: '#0891b2',
      agent: '#dc2626',
      planner: '#7c3aed',
      executor: '#ea580c',
      replanner: '#be185d',
      observer: '#059669',
      adjuster: '#d97706',
      reviewer: '#4f46e5',
      context: '#0f766e',
      summary: '#9333ea',
      report: '#0d9488',
      handler: '#65a30d',
      regex_match: '#64748b',
      unified_executor: '#0369a1',
    };

    let nodes = [];
    let steps = [];
    let totalCalls = 0;

    if (mode === 'react' || mode === 'react_stock') {
      const N = this.params.REACT_TOOL_CALLS || 8;
      const compactEnabled = this._isEnabled('REACT_ENABLE_CONTEXT_COMPACTION');
      const dedupFactor = this.params.REACT_DEDUP_MODE === 'exact' ? 0.9 : 1;

      // 分类 + 选技能：各 1 次调用，~1500 token
      nodes.push({ label: '意图分类', tokens: 1500, color: COLORS.classify, calls: 1 });
      nodes.push({ label: '选择技能', tokens: 2000, color: COLORS.select_skills, calls: 1 });
      totalCalls += 2;

      // Agent 执行：每轮 ~2000 token（含工具输出），输入随轮次增长
      const recentRounds = Math.max(this.params.REACT_CONTEXT_RECENT_ROUNDS || 4, 0);
      const triggerRounds = Math.max(this.params.REACT_SUMMARY_TRIGGER_ROUNDS || 5, 1);
      let agentTokens;
      let contextTokens = 0;
      let summaryCalls = 0;

      if (compactEnabled) {
        // 压缩模式：稳态输入 = system + summary + 窗口
        const perRound = 2000;
        agentTokens = N * perRound * dedupFactor;
        summaryCalls = Math.floor(Math.max(N - recentRounds, 0) / triggerRounds);
        contextTokens = summaryCalls * 1500;
      } else {
        // 非压缩：输入线性增长
        const perRound = 2000;
        agentTokens = (N * perRound + N * (N - 1) / 2 * 500) * dedupFactor;
      }

      nodes.push({ label: 'Agent × ' + N, tokens: Math.round(agentTokens), color: COLORS.agent, calls: N });
      totalCalls += N;

      if (contextTokens > 0) {
        nodes.push({ label: '上下文摘要', tokens: Math.round(contextTokens), color: COLORS.summary, calls: summaryCalls });
        totalCalls += summaryCalls;
      }

      // 报告：~5000 token
      nodes.push({ label: '报告生成', tokens: 5000, color: COLORS.report, calls: 1 });
      totalCalls += 1;

    } else if (mode === 'plan' || mode === 'plan_solve') {
      const S = this.params.PLAN_MAX_STEPS || 5;
      const T = this.params.PLAN_EXECUTOR_TOOL_CALLS || 6;

      nodes.push({ label: '意图分类', tokens: 1500, color: COLORS.classify, calls: 1 });
      nodes.push({ label: 'Planner', tokens: 3000, color: COLORS.planner, calls: 1 });
      totalCalls += 2;

      let stepTotal = 0;
      for (let i = 0; i < S; i++) {
        const stepTokens = T * 2000 + 1500; // T 轮工具 + 总结
        steps.push({ label: `Step ${i + 1}`, tokens: stepTokens, calls: T + 1, color: COLORS.executor });
        stepTotal += stepTokens;
        totalCalls += T + 1;
      }
      nodes.push({ label: 'Executor × ' + S, tokens: stepTotal, color: COLORS.executor, calls: 0 });

      const replannerCount = Math.max(S - 1, 1);
      nodes.push({ label: 'Replanner × ' + replannerCount, tokens: replannerCount * 2500, color: COLORS.replanner, calls: replannerCount });
      totalCalls += replannerCount;

      nodes.push({ label: '报告生成', tokens: 5000, color: COLORS.report, calls: 1 });
      totalCalls += 1;

    } else if (mode === 'unified' || mode === 'unified_plan') {
      const T = this.params.PLAN_EXECUTOR_TOOL_CALLS || 6;

      nodes.push({ label: '意图分类', tokens: 1500, color: COLORS.classify, calls: 1 });
      nodes.push({ label: 'Planner', tokens: 3000, color: COLORS.planner, calls: 1 });
      totalCalls += 2;

      const execTokens = T * 2000 + 1500;
      nodes.push({ label: '统一执行器', tokens: execTokens, color: COLORS.unified_executor, calls: T });
      totalCalls += T;

      nodes.push({ label: '报告生成', tokens: 5000, color: COLORS.report, calls: 1 });
      totalCalls += 1;

    } else if (mode === 'scenario') {
      nodes.push({ label: '正则匹配', tokens: 0, color: COLORS.regex_match, calls: 0 });
      nodes.push({ label: '场景处理器', tokens: 2000, color: COLORS.handler, calls: 1 });
      totalCalls += 1;
      nodes.push({ label: '报告生成', tokens: 5000, color: COLORS.report, calls: 1 });
      totalCalls += 1;

    } else if (mode === 'pdor') {
      const S = this.params.PLAN_MAX_STEPS || 5;
      const T = this.params.PLAN_EXECUTOR_TOOL_CALLS || 6;

      nodes.push({ label: '意图分类', tokens: 1500, color: COLORS.classify, calls: 1 });
      nodes.push({ label: 'Planner', tokens: 3000, color: COLORS.planner, calls: 1 });
      totalCalls += 2;

      let stepTotal = 0;
      for (let i = 0; i < S; i++) {
        const stepTokens = T * 2000 + 1500 + 600; // executor + observer
        steps.push({ label: `Step ${i + 1}`, tokens: stepTokens, calls: T + 1, color: COLORS.executor });
        stepTotal += stepTokens;
        totalCalls += T + 1;
      }
      nodes.push({ label: 'Executor × ' + S, tokens: stepTotal, color: COLORS.executor, calls: 0 });

      nodes.push({ label: 'Observer', tokens: Math.round(S * 0.3 * 800), color: COLORS.observer, calls: Math.round(S * 0.3) });
      nodes.push({ label: 'Adjuster', tokens: Math.round(S * 0.3 * 1200), color: COLORS.adjuster, calls: Math.round(S * 0.3) });
      totalCalls += Math.round(S * 0.6);

      nodes.push({ label: 'Reviewer', tokens: 4000, color: COLORS.reviewer, calls: 1 });
      totalCalls += 1;

      nodes.push({ label: '报告生成', tokens: 5000, color: COLORS.report, calls: 1 });
      totalCalls += 1;
    }

    if (mode === 'agent_group') {
      const maxConcurrent = this.params.GROUP_MAX_CONCURRENT_AGENTS || 3;
      const maxSteps = this.params.GROUP_MAX_STEPS || 10;
      const toolCallsPerAgent = this.params.PLAN_EXECUTOR_TOOL_CALLS || 8;

      // 分类：1 次调用
      nodes.push({ label: '分类', tokens: 1500, color: COLORS.classify, calls: 1 });
      totalCalls += 1;

      // Supervisor 规划 + 路由：每步 1 次调用
      const supervisorTokens = maxSteps * 1500;
      nodes.push({ label: `Supervisor × ${maxSteps}`, tokens: supervisorTokens, color: COLORS.planner, calls: maxSteps });
      totalCalls += maxSteps;

      // Agent 执行：每个 agent 有 tool_calls_per_agent 轮，每轮 ~2000 token
      const agentTokensPerAgent = toolCallsPerAgent * 2000;
      const totalAgentTokens = maxSteps * agentTokensPerAgent;
      nodes.push({ label: `Agent × ${maxSteps}`, tokens: totalAgentTokens, color: COLORS.agent, calls: maxSteps * toolCallsPerAgent });
      totalCalls += maxSteps * toolCallsPerAgent;

      // 报告 + 仪表盘
      nodes.push({ label: '报告生成', tokens: 5000, color: COLORS.report, calls: 1 });
      nodes.push({ label: '仪表盘', tokens: 3000, color: COLORS.reviewer, calls: 1 });
      totalCalls += 2;
    }

    nodes = nodes.filter(n => n.tokens > 0);
    const total = nodes.reduce((s, n) => s + n.tokens, 0);
    return { nodes, steps, total, totalCalls };
  }

  /* ── 紧凑预算条 ── */
  renderCompactBudget(container, mode) {
    container.innerHTML = '';
    const budget = this.calcBudgetAllocation(mode);
    const MAX = this.params.MAX_TOKENS_PER_QUERY || 50000;
    const usedPct = Math.min(budget.total / MAX, 1);

    // 标题
    const title = document.createElement('div');
    title.className = 'viz-section-title';
    title.textContent = '预算分配估算';
    container.appendChild(title);

    // 水平堆叠条
    const barWrap = document.createElement('div');
    barWrap.className = 'budget-bar-wrap';
    for (const node of budget.nodes) {
      const pct = (node.tokens / MAX) * 100;
      if (pct < 0.5) continue;
      const seg = document.createElement('div');
      seg.className = 'budget-bar-seg';
      seg.style.cssText = `background:${node.color};width:${pct}%;`;
      seg.title = `${node.label}: ${(node.tokens / 1000).toFixed(1)}k (${Math.round(pct)}%)`;
      barWrap.appendChild(seg);
    }
    container.appendChild(barWrap);

    // 数值行
    const stats = document.createElement('div');
    stats.className = 'budget-stats-row';
    const usedK = (budget.total / 1000).toFixed(1);
    const maxK = (MAX / 1000).toFixed(0);
    const color = usedPct > 0.9 ? '#ef4444' : usedPct > 0.7 ? '#f59e0b' : '#10b981';
    stats.innerHTML = `<span style="font-weight:600;color:${color}">${usedK}k / ${maxK}k tokens</span>` +
      `<span style="color:#94a3b8">${budget.totalCalls} 次调用 · ${Math.round(usedPct * 100)}%</span>`;
    container.appendChild(stats);

    // 紧凑图例
    const legend = document.createElement('div');
    legend.className = 'budget-legend';
    for (const node of budget.nodes) {
      const item = document.createElement('span');
      item.className = 'budget-legend-item';
      const pct = MAX > 0 ? Math.round(node.tokens / MAX * 100) : 0;
      const tk = node.tokens >= 1000 ? (node.tokens / 1000).toFixed(1) + 'k' : node.tokens;
      item.innerHTML = `<span class="budget-legend-dot" style="background:${node.color};"></span>${node.label} ${tk}`;
      legend.appendChild(item);
    }
    container.appendChild(legend);

    // 超预算警告
    if (budget.total > MAX) {
      const warn = document.createElement('div');
      warn.style.cssText = 'font-size:10px;color:#ef4444;font-weight:600;margin-top:4px;';
      warn.textContent = '⚠ 预算不足，可能中途截断';
      container.appendChild(warn);
    }
  }
};
