/* ─────────────────────────────────────────────
   选股雷达 Web — 配置可视化：滑动窗口压缩动画
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.ConfigVizCompression = class ConfigVizCompression {
  constructor(container, params) {
    this.container = container;
    this.params = { ...params };
    this.currentRound = 0;
    this.isPlaying = false;
    this.speed = 800;
    this.timer = null;
    this.states = {};
    this.summaryVersion = 0;
    this.summaryRounds = [];
    this.activeRounds = [];
    this.pendingRounds = [];
    this.recentRounds = [];
    this.roundChars = {};
    this.pendingChars = 0;
    this.compressionCount = 0;
    this.compressionLog = [];
    this.longOutputRound = -1;
    this.scene = 'normal';
    this.isCompressing = false;

    this.CHARS_PER_ROUND = 800;
    this.LONG_OUTPUT_CHARS = 6500;

    this.init();
  }

  init() {
    this.container.innerHTML = '';
    this.container.className = 'comp-viz';

    this.renderHeader();
    this.renderZoneDiagram();
    this.renderTimeline();
    this.renderCharBar();
    this.renderStats();
    this.renderControls();
    this.renderLog();
  }

  renderHeader() {
    const header = document.createElement('div');
    header.className = 'comp-viz-header';

    const title = document.createElement('div');
    title.className = 'comp-viz-title';
    title.textContent = '滑动窗口压缩演示';
    header.appendChild(title);

    const desc = document.createElement('div');
    desc.className = 'comp-viz-desc';
    desc.textContent = '模拟上下文在迭代中的压缩过程（ReAct / Plan / PDOR / Unified 共用）';
    header.appendChild(desc);

    this.container.appendChild(header);
  }

  renderZoneDiagram() {
    const wrap = document.createElement('div');
    wrap.className = 'comp-viz-zone-wrap';
    this.zoneWrapEl = wrap;

    this._renderZoneContent(wrap);
    this.container.appendChild(wrap);
  }

  _renderZoneContent(wrap) {
    wrap.innerHTML = '';

    const row = document.createElement('div');
    row.className = 'comp-viz-zone-row';

    const recentRounds = this.getRecentRounds();
    const triggerRounds = this.getTriggerRounds();

    const zones = [
      { cls: 'summary', flex: 3, title: '已摘要区', content: 'R1~R7 → 摘要 v1.2', sub: '~1500 字符', color: '#2563eb' },
      { cls: 'pending', flex: 2, title: '待压缩区', content: `pending ≥ ${triggerRounds} 轮触发`, sub: `每轮 ~${this.CHARS_PER_ROUND} 字符`, color: '#92400e' },
      { cls: 'recent', flex: 1, title: '最近保留', content: `保留最近 ${recentRounds} 轮`, sub: '不参与压缩', color: '#065f46' },
    ];

    zones.forEach((z, i) => {
      const block = document.createElement('div');
      block.className = `comp-viz-zone comp-viz-zone-${z.cls}`;
      block.style.flex = z.flex;

      const title = document.createElement('div');
      title.className = 'comp-viz-zone-title';
      title.textContent = z.title;
      block.appendChild(title);

      const content = document.createElement('div');
      content.className = 'comp-viz-zone-content';
      content.style.color = z.color;
      content.textContent = z.content;
      block.appendChild(content);

      const sub = document.createElement('div');
      sub.className = 'comp-viz-zone-sub';
      sub.textContent = z.sub;
      block.appendChild(sub);

      row.appendChild(block);

      if (i < zones.length - 1) {
        const arrow = document.createElement('div');
        arrow.className = 'comp-viz-zone-arrow';
        arrow.textContent = '→';
        row.appendChild(arrow);
      }
    });

    wrap.appendChild(row);

    const rule = document.createElement('div');
    rule.className = 'comp-viz-zone-rule';
    const pendingCharsThreshold = this.getPendingCharsThreshold();
    rule.innerHTML = `<strong>触发条件（任一）：</strong>` +
      `<code>pending ≥ ${triggerRounds} 轮</code> 或 ` +
      `<code>pending ≥ ${pendingCharsThreshold.toLocaleString()} 字符</code>` +
      ` → 增量摘要为 JSON，合并到已摘要区`;
    wrap.appendChild(rule);
  }

  renderControls() {
    const controls = document.createElement('div');
    controls.className = 'comp-viz-controls';

    const playBtn = document.createElement('button');
    playBtn.className = 'comp-viz-btn comp-viz-btn-primary';
    playBtn.innerHTML = '<span class="comp-viz-btn-icon">▶</span><span>播放</span>';
    playBtn.addEventListener('click', () => this.togglePlay());
    this.playBtn = playBtn;
    controls.appendChild(playBtn);

    const resetBtn = document.createElement('button');
    resetBtn.className = 'comp-viz-btn';
    resetBtn.innerHTML = '<span class="comp-viz-btn-icon">↺</span><span>重置</span>';
    resetBtn.addEventListener('click', () => this.reset());
    controls.appendChild(resetBtn);

    const stepBtn = document.createElement('button');
    stepBtn.className = 'comp-viz-btn';
    stepBtn.innerHTML = '<span class="comp-viz-btn-icon">⏭</span><span>单步</span>';
    stepBtn.addEventListener('click', () => this.stepForward());
    controls.appendChild(stepBtn);

    const speedWrap = document.createElement('div');
    speedWrap.className = 'comp-viz-speed';
    const speedLabel = document.createElement('span');
    speedLabel.className = 'comp-viz-speed-label';
    speedLabel.textContent = '速度';
    speedWrap.appendChild(speedLabel);
    const speedSelect = document.createElement('select');
    speedSelect.className = 'comp-viz-speed-select';
    [
      { value: 1500, label: '0.5×' },
      { value: 800, label: '1×' },
      { value: 400, label: '2×' },
      { value: 200, label: '4×' },
    ].forEach(opt => {
      const o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label;
      if (opt.value === 800) o.selected = true;
      speedSelect.appendChild(o);
    });
    speedSelect.addEventListener('change', (e) => {
      this.speed = parseInt(e.target.value, 10);
      if (this.isPlaying) {
        this.stopTimer();
        this.startTimer();
      }
    });
    speedWrap.appendChild(speedSelect);
    controls.appendChild(speedWrap);

    const sceneWrap = document.createElement('div');
    sceneWrap.className = 'comp-viz-speed';
    const sceneLabel = document.createElement('span');
    sceneLabel.className = 'comp-viz-speed-label';
    sceneLabel.textContent = '场景';
    sceneWrap.appendChild(sceneLabel);
    const sceneSelect = document.createElement('select');
    sceneSelect.className = 'comp-viz-speed-select';
    [
      { value: 'normal', label: '正常流程' },
      { value: 'long', label: '超长输出' },
    ].forEach(opt => {
      const o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label;
      sceneSelect.appendChild(o);
    });
    sceneSelect.addEventListener('change', (e) => {
      this.scene = e.target.value;
      this.reset();
    });
    sceneWrap.appendChild(sceneSelect);
    controls.appendChild(sceneWrap);

    this.container.appendChild(controls);
  }

  renderTimeline() {
    const wrap = document.createElement('div');
    wrap.className = 'comp-viz-timeline-wrap';

    const legend = document.createElement('div');
    legend.className = 'comp-viz-legend';
    const items = [
      { cls: 'summarized', label: '已摘要' },
      { cls: 'pending', label: '待压缩' },
      { cls: 'recent', label: '最近保留' },
      { cls: 'compressing', label: '正在压缩' },
      { cls: 'empty', label: '未到达' },
    ];
    items.forEach(item => {
      const el = document.createElement('div');
      el.className = 'comp-viz-legend-item';
      const dot = document.createElement('span');
      dot.className = `comp-viz-legend-dot comp-viz-legend-dot-${item.cls}`;
      el.appendChild(dot);
      const text = document.createElement('span');
      text.textContent = item.label;
      el.appendChild(text);
      legend.appendChild(el);
    });
    wrap.appendChild(legend);

    this.timelineEl = document.createElement('div');
    this.timelineEl.className = 'comp-viz-timeline';
    wrap.appendChild(this.timelineEl);

    this.container.appendChild(wrap);
    this.renderRoundCells();
  }

  renderRoundCells() {
    this.timelineEl.innerHTML = '';

    const row = document.createElement('div');
    row.className = 'comp-viz-rounds-row';

    const totalRounds = this.getTotalRounds();
    for (let r = 1; r <= totalRounds; r++) {
      const cell = document.createElement('div');
      const state = this.states[r] || 'empty';
      cell.className = `comp-viz-cell comp-viz-cell-${state}`;
      cell.textContent = r;
      cell.dataset.round = r;

      if (state !== 'empty') {
        cell.title = `R${r}: ${this._stateLabel(state)}`;
      }

      row.appendChild(cell);
    }

    this.timelineEl.appendChild(row);

    const iterLabel = document.createElement('div');
    iterLabel.className = 'comp-viz-iter-label';
    iterLabel.textContent = this.currentRound > 0
      ? `当前: 第 ${this.currentRound} 轮`
      : '点击播放开始演示';
    this.iterLabelEl = iterLabel;
    this.timelineEl.appendChild(iterLabel);
  }

  _stateLabel(state) {
    return { summarized: '已摘要', pending: '待压缩', recent: '最近保留', compressing: '正在压缩...' }[state] || state;
  }

  renderCharBar() {
    const wrap = document.createElement('div');
    wrap.className = 'comp-viz-charbar-wrap';

    const label = document.createElement('div');
    label.className = 'comp-viz-charbar-label';
    label.textContent = 'Pending 字符数';
    wrap.appendChild(label);

    const barOuter = document.createElement('div');
    barOuter.className = 'comp-viz-charbar';
    this.charBarOuter = barOuter;

    const fill = document.createElement('div');
    fill.className = 'comp-viz-charbar-fill';
    fill.style.width = '0%';
    this.charBarFill = fill;
    barOuter.appendChild(fill);

    const thresholdLine = document.createElement('div');
    thresholdLine.className = 'comp-viz-charbar-threshold';
    this.charBarThresholdLine = thresholdLine;
    barOuter.appendChild(thresholdLine);

    const thresholdLabel = document.createElement('div');
    thresholdLabel.className = 'comp-viz-charbar-threshold-label';
    this.charBarThresholdLabel = thresholdLabel;
    barOuter.appendChild(thresholdLabel);

    this._updateCharBarThreshold();

    wrap.appendChild(barOuter);

    const valueLabel = document.createElement('div');
    valueLabel.className = 'comp-viz-charbar-value';
    valueLabel.textContent = '0';
    this.charBarValue = valueLabel;
    wrap.appendChild(valueLabel);

    this.container.appendChild(wrap);
  }

  _updateCharBarThreshold() {
    const threshold = this.getPendingCharsThreshold();
    const thresholdPct = Math.min(threshold / (threshold * 1.5) * 100, 100);
    this.charBarThresholdLine.style.left = `${thresholdPct}%`;
    this.charBarThresholdLabel.style.left = `${thresholdPct}%`;
    this.charBarThresholdLabel.textContent = `${threshold.toLocaleString()}`;
  }

  renderStats() {
    const wrap = document.createElement('div');
    wrap.className = 'comp-viz-stats';

    const statsConfig = [
      { key: 'summarized', label: '已摘要', value: '-' },
      { key: 'pending', label: '待压缩', value: '-' },
      { key: 'recent', label: '最近保留', value: '-' },
      { key: 'compressions', label: '压缩次数', value: '0' },
    ];

    this.statEls = {};
    statsConfig.forEach(cfg => {
      const stat = document.createElement('div');
      stat.className = 'comp-viz-stat';
      const lbl = document.createElement('div');
      lbl.className = 'comp-viz-stat-label';
      lbl.textContent = cfg.label;
      stat.appendChild(lbl);
      const val = document.createElement('div');
      val.className = `comp-viz-stat-value comp-viz-stat-${cfg.key}`;
      val.textContent = cfg.value;
      stat.appendChild(val);
      this.statEls[cfg.key] = val;
      wrap.appendChild(stat);
    });

    this.container.appendChild(wrap);
  }

  renderLog() {
    const wrap = document.createElement('div');
    wrap.className = 'comp-viz-log-wrap';

    const title = document.createElement('div');
    title.className = 'comp-viz-log-title';
    title.textContent = '压缩日志';
    wrap.appendChild(title);

    this.logEl = document.createElement('div');
    this.logEl.className = 'comp-viz-log';
    wrap.appendChild(this.logEl);

    this.container.appendChild(wrap);
  }

  updateParams(params, autoReplay) {
    this.params = { ...params };
    this._refreshParamDependentUI();
    if (autoReplay) {
      this.reset();
    } else {
      this._reclassifyRounds();
    }
  }

  _refreshParamDependentUI() {
    if (this.zoneWrapEl) {
      this._renderZoneContent(this.zoneWrapEl);
    }
    this._updateCharBarThreshold();
    this.updateCharBar();
  }

  _reclassifyRounds() {
    if (this.activeRounds.length === 0) return;

    const recentCount = this.getRecentRounds();
    if (recentCount === 0) {
      this.recentRounds = [];
      this.pendingRounds = [...this.activeRounds];
    } else {
      this.recentRounds = this.activeRounds.slice(-recentCount);
      this.pendingRounds = this.activeRounds.slice(0, -recentCount);
    }

    this.pendingChars = this.pendingRounds.reduce(
      (sum, pr) => sum + (this.roundChars[pr] || this.CHARS_PER_ROUND), 0
    );

    this.pendingRounds.forEach(pr => { this.states[pr] = 'pending'; });
    this.recentRounds.forEach(rr => { this.states[rr] = 'recent'; });

    this.checkCompression();
    this.renderRoundCells();
    this.updateCharBar();
    this.updateStats();
  }

  togglePlay() {
    if (this.isPlaying) {
      this.pause();
    } else {
      this.play();
    }
  }

  play() {
    const totalRounds = this.getTotalRounds();
    if (this.currentRound >= totalRounds) {
      this.reset();
    }
    this.isPlaying = true;
    this.playBtn.innerHTML = '<span class="comp-viz-btn-icon">⏸</span><span>暂停</span>';
    this.startTimer();
  }

  pause() {
    this.isPlaying = false;
    this.playBtn.innerHTML = '<span class="comp-viz-btn-icon">▶</span><span>播放</span>';
    this.stopTimer();
  }

  startTimer() {
    this.stopTimer();
    this.timer = setInterval(() => {
      const totalRounds = this.getTotalRounds();
      if (this.currentRound >= totalRounds) {
        this.pause();
        return;
      }
      if (this.isCompressing) return;
      this.stepForward();
    }, this.speed);
  }

  stopTimer() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  reset() {
    this.pause();
    this.currentRound = 0;
    this.states = {};
    this.summaryVersion = 0;
    this.summaryRounds = [];
    this.activeRounds = [];
    this.pendingRounds = [];
    this.recentRounds = [];
    this.roundChars = {};
    this.pendingChars = 0;
    this.compressionCount = 0;
    this.compressionLog = [];
    this.longOutputRound = -1;
    this.isCompressing = false;

    if (this.scene === 'long') {
      this.longOutputRound = 4;
    }

    this.renderRoundCells();
    this.updateCharBar();
    this.updateStats();
    this.logEl.innerHTML = '';
  }

  stepForward() {
    const totalRounds = this.getTotalRounds();
    if (this.currentRound >= totalRounds) return;
    if (this.isCompressing) return;

    this.currentRound++;
    const r = this.currentRound;

    let charsThisRound = this.CHARS_PER_ROUND;
    if (this.scene === 'long' && r === this.longOutputRound) {
      charsThisRound = this.LONG_OUTPUT_CHARS;
    }

    this.activeRounds.push(r);
    this.roundChars[r] = charsThisRound;

    const recentCount = this.getRecentRounds();
    if (recentCount === 0) {
      this.recentRounds = [];
      this.pendingRounds = [...this.activeRounds];
    } else {
      this.recentRounds = this.activeRounds.slice(-recentCount);
      this.pendingRounds = this.activeRounds.slice(0, -recentCount);
    }

    this.pendingChars = this.pendingRounds.reduce((sum, pr) => sum + (this.roundChars[pr] || this.CHARS_PER_ROUND), 0);

    this.pendingRounds.forEach(pr => { this.states[pr] = 'pending'; });
    this.recentRounds.forEach(rr => { this.states[rr] = 'recent'; });

    this.checkCompression();
    this.renderRoundCells();
    this.updateCharBar();
    this.updateStats();

    this._animateNewCell(r);
    this._scrollToCurrentRound(r);

    if (this.currentRound >= totalRounds) {
      this.pause();
    }
  }

  _animateNewCell(roundNum) {
    const cell = this.timelineEl.querySelector(`[data-round="${roundNum}"]`);
    if (cell) {
      cell.classList.add('comp-viz-cell-enter');
      setTimeout(() => cell.classList.remove('comp-viz-cell-enter'), 400);
    }
  }

  _scrollToCurrentRound(roundNum) {
    const wrap = this.container.querySelector('.comp-viz-timeline-wrap');
    const cell = this.timelineEl.querySelector(`[data-round="${roundNum}"]`);
    if (!wrap || !cell) return;

    const wrapRect = wrap.getBoundingClientRect();
    const cellRect = cell.getBoundingClientRect();

    const cellLeft = cellRect.left - wrapRect.left + wrap.scrollLeft;
    const cellRight = cellLeft + cellRect.width;
    const wrapWidth = wrapRect.width;

    const padding = 60;
    let targetScroll = wrap.scrollLeft;

    if (cellRight > wrap.scrollLeft + wrapWidth - padding) {
      targetScroll = cellRight - wrapWidth + padding;
    } else if (cellLeft < wrap.scrollLeft + padding) {
      targetScroll = cellLeft - padding;
    }

    if (targetScroll !== wrap.scrollLeft) {
      wrap.scrollTo({ left: targetScroll, behavior: 'smooth' });
    }
  }

  getRecentRounds() {
    const val = this.params.REACT_CONTEXT_RECENT_ROUNDS;
    if (val === undefined || val === null) return 2;
    const num = parseInt(val, 10);
    return Number.isNaN(num) ? 2 : Math.max(num, 0);
  }

  getTotalRounds() {
    const val = this.params.REACT_TOOL_CALLS;
    if (val === undefined || val === null) return 20;
    const num = parseInt(val, 10);
    return Number.isNaN(num) ? 20 : Math.max(num, 1);
  }

  getTriggerRounds() {
    const val = this.params.REACT_SUMMARY_TRIGGER_ROUNDS;
    if (val === undefined || val === null) return 3;
    const num = parseInt(val, 10);
    return Number.isNaN(num) ? 3 : Math.max(num, 1);
  }

  getPendingCharsThreshold() {
    const val = this.params.REACT_SUMMARY_PENDING_CHARS;
    if (val === undefined || val === null) return 8000;
    const num = parseInt(val, 10);
    return Number.isNaN(num) ? 8000 : Math.max(num, 1);
  }

  checkCompression() {
    const triggerRounds = this.getTriggerRounds();
    const pendingCharsThreshold = this.getPendingCharsThreshold();

    const shouldTriggerByRounds = this.pendingRounds.length >= triggerRounds;
    const shouldTriggerByChars = this.pendingChars >= pendingCharsThreshold;

    if (shouldTriggerByRounds || shouldTriggerByChars) {
      this.triggerCompression(shouldTriggerByChars && !shouldTriggerByRounds);
    }
  }

  triggerCompression(charTriggered) {
    this.isCompressing = true;
    const roundsToCompress = [...this.pendingRounds];

    roundsToCompress.forEach((r, i) => {
      setTimeout(() => {
        this.states[r] = 'compressing';
        this.renderRoundCells();
      }, i * 60);
    });

    this.compressionCount++;
    this.summaryVersion++;

    const triggerType = charTriggered ? '字符阈值' : '轮数阈值';
    const logEntry = {
      version: this.summaryVersion,
      rounds: roundsToCompress,
      trigger: triggerType,
      pendingCharsBefore: this.pendingChars,
    };
    this.compressionLog.push(logEntry);

    this._showCompressionFlash(roundsToCompress);

    const compressDelay = roundsToCompress.length * 60 + 350;
    setTimeout(() => {
      roundsToCompress.forEach((r, i) => {
        setTimeout(() => {
          this.states[r] = 'summarized';
          this.summaryRounds.push(r);
        }, i * 40);
      });

      this.activeRounds = this.activeRounds.filter(r => !roundsToCompress.includes(r));
      this.pendingRounds = [];
      this.pendingChars = 0;

      const recentCount = this.getRecentRounds();
      if (recentCount === 0) {
        this.recentRounds = [];
      } else {
        this.recentRounds = this.activeRounds.slice(-recentCount);
      }
      this.recentRounds.forEach(rr => { this.states[rr] = 'recent'; });

      this.renderRoundCells();
      this.updateCharBar();
      this.updateStats();
      this.addLogEntry(logEntry);
      this.isCompressing = false;
    }, compressDelay);
  }

  _showCompressionFlash(rounds) {
    const flash = document.createElement('div');
    flash.className = 'comp-viz-flash';
    flash.textContent = `✦ 压缩 v${this.summaryVersion}`;
    this.timelineEl.appendChild(flash);

    requestAnimationFrame(() => {
      flash.classList.add('comp-viz-flash-active');
    });

    setTimeout(() => {
      flash.remove();
    }, 1200);
  }

  updateCharBar() {
    const threshold = this.getPendingCharsThreshold();
    const maxDisplay = threshold * 1.5;
    const pct = Math.min(this.pendingChars / maxDisplay * 100, 100);
    this.charBarFill.style.width = `${pct}%`;

    const isOver = this.pendingChars >= threshold;
    const isNear = this.pendingChars >= threshold * 0.7;
    this.charBarFill.className = 'comp-viz-charbar-fill';
    if (isOver) {
      this.charBarFill.classList.add('comp-viz-charbar-fill-over');
    } else if (isNear) {
      this.charBarFill.classList.add('comp-viz-charbar-fill-near');
    }

    this.charBarValue.textContent = `${this.pendingChars.toLocaleString()} / ${threshold.toLocaleString()}`;
    this.charBarValue.className = 'comp-viz-charbar-value';
    if (isOver) {
      this.charBarValue.classList.add('comp-viz-charbar-value-over');
    } else if (isNear) {
      this.charBarValue.classList.add('comp-viz-charbar-value-near');
    }
  }

  updateStats() {
    const summaryCount = Object.values(this.states).filter(s => s === 'summarized').length;
    const pendingCount = Object.values(this.states).filter(s => s === 'pending').length;
    const recentCount = Object.values(this.states).filter(s => s === 'recent').length;

    this.statEls.summarized.textContent = summaryCount > 0 ? `${summaryCount} 轮` : '-';
    this.statEls.pending.textContent = pendingCount > 0 ? `${pendingCount} 轮` : '-';
    this.statEls.recent.textContent = recentCount > 0 ? `${recentCount} 轮` : '-';
    this.statEls.compressions.textContent = this.compressionCount;
  }

  addLogEntry(entry) {
    const el = document.createElement('div');
    el.className = 'comp-viz-log-entry';
    el.innerHTML = `<span class="comp-viz-log-ver">v${entry.version}</span>` +
      `<span class="comp-viz-log-detail">R${entry.rounds[0]}~R${entry.rounds[entry.rounds.length - 1]} → 摘要</span>` +
      `<span class="comp-viz-log-trigger">${entry.trigger}</span>` +
      `<span class="comp-viz-log-chars">${entry.pendingCharsBefore.toLocaleString()} 字符</span>`;
    this.logEl.appendChild(el);
    this.logEl.scrollTop = this.logEl.scrollHeight;
  }

  destroy() {
    this.stopTimer();
    this.container.innerHTML = '';
  }
};
