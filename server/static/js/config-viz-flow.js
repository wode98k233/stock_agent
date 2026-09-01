/* ─────────────────────────────────────────────
   选股雷达 Web — 配置可视化：SVG 流程图（紧凑版）
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.ConfigVisualization = class ConfigVisualization {
  constructor(containerId) {
    this.container = document.getElementById(containerId);
    if (!this.container) return;
    this.currentMode = 'plan';
    this.params = {
      REACT_TOOL_CALLS: 8,
      REACT_ENABLE_CONTEXT_COMPACTION: true,
      REACT_CONTEXT_RECENT_ROUNDS: 4,
      REACT_SUMMARY_TRIGGER_ROUNDS: 5,
      REACT_SUMMARY_PENDING_CHARS: 6000,
      REACT_SUMMARY_MAX_CHARS: 2500,
      REACT_DEDUP_MODE: 'exact',
      PLAN_EXECUTOR_TOOL_CALLS: 6,
      PLAN_MAX_STEPS: 5,
      MAX_TOKENS_PER_QUERY: 50000,
      MEMORY_MAX_TOKENS: 8000,
      TOOL_HARD_TRUNCATE_CHARS: 10000,
    };
    this.visualParamKeys = new Set(Object.keys(this.params));
    this._budget = new window.StockRadar.ConfigVizBudget(this.params);
    this._compressionViz = null;
    this.init();
  }

  init() {
    this.container.innerHTML = '';
    this.container.className = 'config-viz';

    // 模式切换（动态获取）
    this.tabsWrap = document.createElement('div');
    this.tabsWrap.className = 'viz-tabs';
    this.container.appendChild(this.tabsWrap);
    this._loadModes();

    // 流程图
    this.graphWrap = document.createElement('div');
    this.graphWrap.className = 'viz-graph-wrap';
    this.container.appendChild(this.graphWrap);

    // 预算摘要
    this.budgetWrap = document.createElement('div');
    this.container.appendChild(this.budgetWrap);

    // 压缩可视化（可折叠）
    this.compressionWrap = document.createElement('div');
    this.container.appendChild(this.compressionWrap);

    // 流程图
    this.graphWrap = document.createElement('div');
    this.graphWrap.className = 'viz-graph-wrap';
    this.container.appendChild(this.graphWrap);

    // 预算摘要
    this.budgetWrap = document.createElement('div');
    this.container.appendChild(this.budgetWrap);

    // 压缩可视化（可折叠）
    this.compressionWrap = document.createElement('div');
    this.container.appendChild(this.compressionWrap);

    this.renderFlowGraph();
    this.renderTokenChart();
    this.renderCompressionViz();
  }

  async _loadModes() {
    try {
      const resp = await fetch('/api/modes');
      const data = await resp.json();
      this._modes = data.modes || [];
      this.tabsWrap.innerHTML = '';
      for (const m of this._modes) {
        const btn = document.createElement('button');
        btn.className = 'viz-tab' + (m.name === this.currentMode ? ' active' : '');
        btn.textContent = m.label || m.name;
        btn.dataset.mode = m.name;
        btn.addEventListener('click', () => this.switchMode(m.name));
        this.tabsWrap.appendChild(btn);
      }
    } catch {}
  }

  modeLabel(mode) {
    if (this._modes) {
      const m = this._modes.find(x => x.name === mode);
      if (m) return m.label;
    }
    return mode;
  }

  switchMode(mode) {
    this.currentMode = mode;
    for (const btn of this.container.querySelectorAll('.viz-tab')) {
      btn.classList.toggle('active', btn.dataset.mode === mode);
    }
    this.renderFlowGraph();
    this.renderTokenChart();
    this.renderCompressionViz();
  }

  updateParam(key, value) {
    if (!this.visualParamKeys.has(key)) return;
    this.params[key] = value;
    this.renderFlowGraph();
    this.renderTokenChart();

    const compressionKeys = new Set([
      'REACT_CONTEXT_RECENT_ROUNDS',
      'REACT_SUMMARY_TRIGGER_ROUNDS',
      'REACT_SUMMARY_PENDING_CHARS',
      'REACT_SUMMARY_MAX_CHARS',
      'REACT_TOOL_CALLS',
    ]);
    if (this._compressionViz) {
      this._compressionViz.updateParams(this.params, compressionKeys.has(key));
    }

    const badge = this.graphWrap.querySelector(`[data-param="${key}"]`);
    if (badge) {
      badge.classList.add('param-highlight');
      setTimeout(() => badge.classList.remove('param-highlight'), 800);
    }
  }

  _isEnabled(key) {
    const value = this.params[key];
    return value === true || value === 'true' || value === 'True' || value === 1 || value === '1';
  }

  _badgeLabel(key) {
    const value = this.params[key];
    if (typeof value === 'boolean') return value ? 'ON' : 'OFF';
    if (value === 'exact') return 'EX';
    if (value === 'off') return 'OFF';
    const num = Number(value);
    if (!Number.isNaN(num) && Math.abs(num) >= 1000) {
      return `${(num / 1000).toFixed(num % 1000 === 0 ? 0 : 1)}k`;
    }
    return String(value ?? '-');
  }

  _badgeWidth(key) {
    const label = this._badgeLabel(key);
    return Math.max(22, label.length * 6 + 10);
  }

  renderTokenChart() {
    this._budget.updateParams(this.params);
    this._budget.renderCompactBudget(this.budgetWrap, this.currentMode);
  }

  renderCompressionViz() {
    if (this._compressionViz) {
      this._compressionViz.destroy();
      this._compressionViz = null;
    }
    this.compressionWrap.innerHTML = '';

    if (!this._isEnabled('REACT_ENABLE_CONTEXT_COMPACTION')) return;

    // 折叠头
    const header = document.createElement('div');
    header.className = 'comp-viz-collapse-header';
    header.innerHTML = '<span class="comp-viz-collapse-arrow">&#9654;</span> 滑动窗口压缩演示';
    const body = document.createElement('div');
    body.className = 'comp-viz-collapse-body';
    body.style.display = 'none';

    header.addEventListener('click', () => {
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : 'block';
      header.querySelector('.comp-viz-collapse-arrow').innerHTML = open ? '&#9654;' : '&#9660;';
    });

    this.compressionWrap.appendChild(header);
    this.compressionWrap.appendChild(body);

    this._compressionViz = new window.StockRadar.ConfigVizCompression(body, this.params);
  }

  renderFlowGraph() {
    const defs = this.svgDefs();
    const { nodes, edges, loops, badges } = this._getLayout();
    const nodeMap = {};
    for (const n of nodes) nodeMap[n.id] = n;

    // 计算边界
    const padding = 12;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;

    for (const n of nodes) {
      const w = n.shape === 'oval' ? 72 : 80;
      const h = n.shape === 'oval' ? 28 : 32;
      minX = Math.min(minX, n.x - w / 2);
      maxX = Math.max(maxX, n.x + w / 2);
      minY = Math.min(minY, n.y - h / 2);
      maxY = Math.max(maxY, n.y + h / 2);
    }
    for (const lp of loops) {
      minX = Math.min(minX, lp.x - lp.w / 2);
      maxX = Math.max(maxX, lp.x + lp.w / 2);
      minY = Math.min(minY, lp.y - lp.h / 2);
      maxY = Math.max(maxY, lp.y + lp.h / 2 + 12);
    }
    for (const b of badges) {
      const n = nodeMap[b.nodeId];
      if (!n) continue;
      const bx = n.x + b.offset.x;
      const bw = this._badgeWidth(b.key);
      minX = Math.min(minX, bx - bw / 2);
      maxX = Math.max(maxX, bx + bw / 2);
      minY = Math.min(minY, n.y + b.offset.y - 10);
      maxY = Math.max(maxY, n.y + b.offset.y + 10);
    }

    const vbW = maxX - minX + padding * 2;
    const vbH = maxY - minY + padding * 2;

    let svg = `<svg viewBox="${minX - padding} ${minY - padding} ${vbW} ${vbH}" class="viz-svg" preserveAspectRatio="xMidYMid meet">${defs}`;

    for (const lp of loops) {
      const dash = lp.dashed ? 'stroke-dasharray="4,3"' : '';
      svg += `<rect x="${lp.x - lp.w / 2}" y="${lp.y - lp.h / 2}" width="${lp.w}" height="${lp.h}" rx="6" fill="none" stroke="var(--line-strong)" stroke-width="1" ${dash} class="viz-loop-rect"/>`;
      if (lp.label) {
        svg += `<text x="${lp.x}" y="${lp.y + lp.h / 2 + 11}" text-anchor="middle" font-size="9" fill="var(--muted)">${lp.label}</text>`;
      }
    }

    for (const [from, to, side] of edges) {
      const f = nodeMap[from];
      const t = nodeMap[to];
      if (!f || !t) continue;
      const path = this.edgePath(f, t, side);
      svg += `<path d="${path}" class="viz-edge-bg" />`;
      svg += `<path d="${path}" class="viz-edge" marker-end="url(#arrowhead)"/>`;
      svg += `<path d="${path}" class="viz-edge-flow" />`;
    }

    for (const n of nodes) {
      const w = n.shape === 'oval' ? 72 : 80;
      const h = n.shape === 'oval' ? 28 : 32;
      const rx = n.shape === 'oval' ? h / 2 : 6;
      const cls = n.id === 'START' || n.id === 'END' ? 'viz-node-terminal' : 'viz-node';
      svg += `<rect x="${n.x - w / 2}" y="${n.y - h / 2}" width="${w}" height="${h}" rx="${rx}" class="${cls}"/>`;
      svg += `<text x="${n.x}" y="${n.y + 4}" text-anchor="middle" font-size="10" fill="var(--text)">${n.label}</text>`;
    }

    for (const b of badges) {
      const n = nodeMap[b.nodeId];
      if (!n) continue;
      const val = this._badgeLabel(b.key);
      const bw = this._badgeWidth(b.key);
      const bx = n.x + b.offset.x;
      const by = n.y + b.offset.y;
      svg += `<g class="viz-badge" data-param="${b.key}">`;
      svg += `<rect x="${bx - bw / 2}" y="${by - 9}" width="${bw}" height="18" rx="9" fill="var(--accent)"/>`;
      svg += `<text x="${bx}" y="${by + 3}" text-anchor="middle" font-size="9" fill="#fff" font-weight="700">${val}</text>`;
      svg += `</g>`;
    }

    svg += '</svg>';
    this.graphWrap.innerHTML = svg;
  }

  _getLayout() {
    // 紧凑布局：所有模式的节点都压缩到更小的画布
    if (this.currentMode === 'react' || this.currentMode === 'react_stock') {
      const compactEnabled = this._isEnabled('REACT_ENABLE_CONTEXT_COMPACTION');
      const contextLabel = compactEnabled ? '压缩' : '上下文';
      const nodes = [
        { id: 'START', x: 170, y: 20, label: 'START' },
        { id: 'classify', x: 170, y: 60, label: '分类' },
        { id: 'select_skills', x: 170, y: 100, label: '选技能' },
        { id: 'context', x: 170, y: 150, label: contextLabel },
        { id: 'summary', x: 60, y: 190, label: '摘要', shape: 'oval' },
        { id: 'window', x: 280, y: 190, label: '窗口', shape: 'oval' },
        { id: 'agent', x: 170, y: 230, label: 'Agent' },
        { id: 'tool', x: 280, y: 230, label: 'Tool', shape: 'oval' },
        { id: 'report', x: 170, y: 290, label: 'Report' },
        { id: 'END', x: 170, y: 330, label: 'END' },
      ];
      return {
        nodes,
        edges: [
          ['START', 'classify'], ['classify', 'select_skills'], ['select_skills', 'context'],
          ['summary', 'context', 'right'], ['window', 'context', 'left-in'],
          ['context', 'agent'], ['agent', 'tool', 'right'], ['tool', 'context', 'left-in'],
          ['agent', 'report', 'bottom'], ['report', 'END'],
        ],
        loops: [{ x: 280, y: 230, w: 90, h: 70, label: '工具循环' }],
        badges: [
          { nodeId: 'agent', key: 'REACT_TOOL_CALLS', offset: { x: 0, y: -20 } },
          { nodeId: 'context', key: 'REACT_ENABLE_CONTEXT_COMPACTION', offset: { x: 0, y: -20 } },
          { nodeId: 'window', key: 'REACT_CONTEXT_RECENT_ROUNDS', offset: { x: 38, y: 0 } },
        ],
      };
    }

    if (this.currentMode === 'plan' || this.currentMode === 'plan_solve') {
      return {
        nodes: [
          { id: 'START', x: 170, y: 20, label: 'START' },
          { id: 'classify', x: 170, y: 60, label: '分类' },
          { id: 'planner', x: 170, y: 100, label: 'Planner' },
          { id: 'executor', x: 170, y: 160, label: 'Executor' },
          { id: 'llm', x: 280, y: 140, label: 'LLM', shape: 'oval' },
          { id: 'tool', x: 280, y: 190, label: 'Tool', shape: 'oval' },
          { id: 'replanner', x: 170, y: 230, label: 'Replanner' },
          { id: 'report', x: 170, y: 280, label: 'Report' },
          { id: 'END', x: 170, y: 320, label: 'END' },
        ],
        edges: [
          ['START', 'classify'], ['classify', 'planner'], ['planner', 'executor'],
          ['executor', 'llm', 'right'], ['llm', 'tool'], ['tool', 'llm'],
          ['llm', 'executor', 'right'], ['executor', 'replanner'],
          ['replanner', 'report', 'bottom'], ['replanner', 'planner', 'left'],
          ['report', 'END'],
        ],
        loops: [
          { x: 280, y: 165, w: 90, h: 70, label: 'ReAct 循环' },
          { x: 170, y: 195, w: 140, h: 60, label: '重规划', dashed: true },
        ],
        badges: [
          { nodeId: 'llm', key: 'PLAN_EXECUTOR_TOOL_CALLS', offset: { x: 36, y: 0 } },
          { nodeId: 'replanner', key: 'PLAN_MAX_STEPS', offset: { x: 44, y: 0 } },
        ],
      };
    }

    if (this.currentMode === 'unified' || this.currentMode === 'unified_plan') {
      return {
        nodes: [
          { id: 'START', x: 170, y: 20, label: 'START' },
          { id: 'classify', x: 170, y: 60, label: '分类' },
          { id: 'executor', x: 170, y: 120, label: '统一执行' },
          { id: 'llm', x: 280, y: 100, label: 'LLM', shape: 'oval' },
          { id: 'tool', x: 280, y: 150, label: 'Tool', shape: 'oval' },
          { id: 'report', x: 170, y: 210, label: 'Report' },
          { id: 'END', x: 170, y: 260, label: 'END' },
        ],
        edges: [
          ['START', 'classify'], ['classify', 'executor'],
          ['executor', 'llm', 'right'], ['llm', 'tool'], ['tool', 'llm'],
          ['llm', 'executor', 'right'], ['executor', 'report'], ['report', 'END'],
        ],
        loops: [{ x: 280, y: 125, w: 90, h: 70, label: 'ReAct 循环' }],
        badges: [
          { nodeId: 'llm', key: 'PLAN_EXECUTOR_TOOL_CALLS', offset: { x: 36, y: 0 } },
        ],
      };
    }

    if (this.currentMode === 'scenario') {
      return {
        nodes: [
          { id: 'START', x: 140, y: 16, label: 'START' },
          { id: 'regex', x: 140, y: 52, label: '正则匹配' },
          { id: 'handler', x: 140, y: 88, label: '处理器' },
          { id: 'report', x: 140, y: 124, label: 'Report' },
          { id: 'END', x: 140, y: 160, label: 'END' },
        ],
        edges: [
          ['START', 'regex'], ['regex', 'handler'], ['handler', 'report'], ['report', 'END'],
        ],
        loops: [],
        badges: [],
      };
    }

    if (this.currentMode === 'agent_group') {
      return {
        nodes: [
          { id: 'START', x: 170, y: 20, label: 'START' },
          { id: 'classify', x: 170, y: 60, label: '分类' },
          { id: 'supervisor', x: 170, y: 110, label: 'Supervisor' },
          { id: 'tech', x: 60, y: 180, label: '技术分析' },
          { id: 'macro', x: 170, y: 180, label: '宏观分析' },
          { id: 'risk', x: 280, y: 180, label: '风险分析' },
          { id: 'chain', x: 390, y: 180, label: '传导链' },
          { id: 'report', x: 170, y: 260, label: 'Report' },
          { id: 'END', x: 170, y: 310, label: 'END' },
        ],
        edges: [
          ['START', 'classify'], ['classify', 'supervisor'],
          ['supervisor', 'tech', 'left'], ['supervisor', 'macro'], ['supervisor', 'risk'], ['supervisor', 'chain', 'right'],
          ['tech', 'supervisor', 'left'], ['macro', 'supervisor'], ['risk', 'supervisor'], ['chain', 'supervisor', 'right'],
          ['supervisor', 'report', 'bottom'], ['report', 'END'],
        ],
        loops: [
          { x: 225, y: 180, w: 380, h: 60, label: 'Agent 并行/串行', dashed: true },
        ],
        badges: [
          { nodeId: 'supervisor', key: 'GROUP_MAX_CONCURRENT_AGENTS', offset: { x: 0, y: -20 } },
        ],
      };
    }

    // pdor
    return {
      nodes: [
        { id: 'START', x: 170, y: 20, label: 'START' },
        { id: 'classify', x: 170, y: 60, label: '分类' },
        { id: 'planner', x: 170, y: 100, label: 'Planner' },
        { id: 'executor', x: 170, y: 160, label: 'Executor' },
        { id: 'llm', x: 280, y: 140, label: 'LLM', shape: 'oval' },
        { id: 'tool', x: 280, y: 190, label: 'Tool', shape: 'oval' },
        { id: 'observer', x: 170, y: 230, label: 'Observer' },
        { id: 'adjuster', x: 70, y: 230, label: 'Adjust', shape: 'oval' },
        { id: 'reviewer', x: 170, y: 280, label: 'Reviewer' },
        { id: 'END', x: 170, y: 330, label: 'END' },
      ],
      edges: [
        ['START', 'classify'], ['classify', 'planner'], ['planner', 'executor'],
        ['executor', 'llm', 'right'], ['llm', 'tool'], ['tool', 'llm'],
        ['llm', 'executor', 'right'], ['executor', 'observer'],
        ['observer', 'adjuster', 'left'], ['adjuster', 'planner', 'left'],
        ['observer', 'reviewer', 'bottom'], ['reviewer', 'END'],
      ],
      loops: [
        { x: 280, y: 165, w: 90, h: 70, label: 'ReAct 循环' },
        { x: 120, y: 195, w: 140, h: 60, label: '调整', dashed: true },
      ],
      badges: [
        { nodeId: 'llm', key: 'PLAN_EXECUTOR_TOOL_CALLS', offset: { x: 36, y: 0 } },
        { nodeId: 'observer', key: 'PLAN_MAX_STEPS', offset: { x: 44, y: 0 } },
      ],
    };
  }

  edgePath(from, to, side) {
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const hw = (n) => n.shape === 'oval' ? 36 : 40;
    const hh = (n) => n.shape === 'oval' ? 14 : 16;

    if (side === 'right') {
      const sx = from.x + hw(from), ex = to.x - hw(to);
      const mx = (sx + ex) / 2;
      return `M ${sx} ${from.y} C ${mx} ${from.y}, ${mx} ${to.y}, ${ex} ${to.y}`;
    }
    if (side === 'left') {
      const sx = from.x - hw(from), ex = to.x - hw(to);
      const mx = (sx + ex) / 2;
      return `M ${sx} ${from.y} C ${mx} ${from.y}, ${mx} ${to.y}, ${ex} ${to.y}`;
    }
    if (side === 'left-in') {
      const sx = from.x - hw(from), ex = to.x + hw(to);
      const mx = (sx + ex) / 2;
      return `M ${sx} ${from.y} C ${mx} ${from.y}, ${mx} ${to.y}, ${ex} ${to.y}`;
    }
    if (side === 'bottom') {
      const sy = from.y + hh(from), ey = to.y - hh(to);
      const my = (sy + ey) / 2;
      return `M ${from.x} ${sy} C ${from.x} ${my}, ${to.x} ${my}, ${to.x} ${ey}`;
    }
    const sy = from.y + hh(from), ey = to.y - hh(to);
    if (Math.abs(dx) < 10) return `M ${from.x} ${sy} L ${to.x} ${ey}`;
    const my = (sy + ey) / 2;
    return `M ${from.x} ${sy} C ${from.x} ${my}, ${to.x} ${my}, ${to.x} ${ey}`;
  }

  svgDefs() {
    return `<defs>
      <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
        <polygon points="0 0, 8 3, 0 6" fill="var(--brand)" opacity="0.5"/>
      </marker>
      <filter id="glow"><feGaussianBlur stdDeviation="1.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      <linearGradient id="flowGrad" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" style="stop-color:var(--brand);stop-opacity:0"/><stop offset="50%" style="stop-color:var(--brand);stop-opacity:0.7"/><stop offset="100%" style="stop-color:var(--brand);stop-opacity:0"/>
      </linearGradient>
    </defs>`;
  }
};
