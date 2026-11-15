/* 池子研究面板（2026-10-18）
 *
 * 背景：池子级研究此前**没有任何页面**能看到 —— 结果只出现在对话结果卡里
 * 点一下按钮的当下，切走就没了，服务一重启连内存都没了。用户提出：
 * 「你这个池子在哪看，目前是不是没有页面可以看到？就是没页面」
 *
 * 职责：列历史研究（新→旧）+ 看单条结论。
 * - 报告正文复用 strategy-card.css 的 .sc-pool-* 类，不另起一套样式；
 * - 正在跑的研究显示进度并轮询，跑完自动换成结论；
 * - 列表为空 / 请求失败都给出明确文案，不留白屏。
 */
(function () {
  'use strict';

  const API = '/api/strategy-research';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function pct(v, digits) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    if (!isFinite(n)) return '—';
    return (n * 100).toFixed(digits === undefined ? 1 : digits) + '%';
  }

  function num(v, digits) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    return isFinite(n) ? n.toFixed(digits === undefined ? 2 : digits) : '—';
  }

  const VERDICT_BADGE = {
    works: 'ps-badge-works', fails: 'ps-badge-fails',
    conditional: 'ps-badge-conditional', insufficient: 'ps-badge-insufficient'
  };

  class PoolStudyPanel {
    constructor() {
      this.overlay = document.getElementById('poolStudyOverlay');
      this.studies = null;        // null = 还没加载回来（区别于空数组）
      this.current = null;
      this._bound = false;
      this._timer = null;
    }

    /* 注意：节点每次现查，不用构造时缓存的那份。
       2026-10-18：「点了没反应」的一类根因就是 show() 在 this.overlay 为 null 时
       **静默 return** —— 用户侧完全没有任何反馈，无从判断是没点上还是坏了。
       现在查不到就明确报错（控制台 + 顶部提示），留痕而不是变成空气。 */
    _el() {
      if (!this.overlay) this.overlay = document.getElementById('poolStudyOverlay');
      return this.overlay;
    }

    show() {
      const el = this._el();
      if (!el) {
        console.error('[池子研究] 找不到 #poolStudyOverlay，面板无法打开（index.html 可能是旧版）');
        try { alert('池子研究面板未加载：请强制刷新页面（Ctrl+F5）后重试'); } catch (e) { /* noop */ }
        return;
      }
      el.classList.add('visible');
      if (!this._bound) this._bind();
      this.refresh();
    }

    hide() {
      const el = this._el();
      if (el) el.classList.remove('visible');
      this._stopPoll();
    }

    /* openPage(studyId)：从对话结果卡直接跳到某一条（对齐 strategyResearchPanel.openPage） */
    openPage(studyId) {
      this.show();
      if (studyId) this._open(String(studyId));
    }
    // 兼容旧调用名，别再出现「函数不存在 → 静默无反应」
    open(studyId) { this.openPage(studyId); }

    _bind() {
      this._bound = true;
      const close = document.getElementById('psClose');
      if (close) close.onclick = () => this.hide();
      const refresh = document.getElementById('psRefresh');
      if (refresh) refresh.onclick = () => this.refresh();
      const clean = document.getElementById('psCleanFailed');
      if (clean) clean.onclick = () => this.cleanFailed();
      const el = this._el();
      if (el) {
        el.addEventListener('click', (e) => {
          if (e.target === el) this.hide();
        });
      }
    }

    /* 删除一条记录：成功/失败都可删（失败记录很容易是测试残留，用户要能自己清） */
    async _deleteStudy(studyId) {
      if (!studyId) return;
      if (!confirm('删除这条池子研究记录？研究结论会一起删掉，不可恢复。')) return;
      try {
        const res = await fetch(API + '/pool-studies/' + encodeURIComponent(studyId),
                                { method: 'DELETE' });
        if (!res.ok) {
          let detail = 'HTTP ' + res.status;
          try {
            const j = await res.json();
            if (j && j.detail) detail = j.detail;
          } catch (e) { /* noop */ }
          throw new Error(detail);
        }
      } catch (e) {
        this._setSub('删除失败：' + (e && e.message ? e.message : e), 'failed');
        return;
      }
      if (this.current === studyId) {
        this.current = null;
        const main = document.getElementById('psMain');
        if (main) main.innerHTML = '<div class="ps-empty">左侧选一条研究记录查看结论</div>';
      }
      await this.refresh();
    }

    /* 批量清理失败记录：单测/试验留下的失败条目一键清掉，成功的记录不受影响 */
    async cleanFailed() {
      const failed = (this.studies || []).filter((s) => s.status === 'failed');
      if (!failed.length) {
        this._setSub('没有失败记录可清理', '');
        return;
      }
      if (!confirm('清理 ' + failed.length + ' 条失败记录？（成功的研究不受影响）')) return;
      this._setSub('正在清理失败记录…', 'pending');
      let ok = 0;
      let bad = 0;
      for (const s of failed) {
        try {
          const res = await fetch(API + '/pool-studies/' + encodeURIComponent(s.study_id),
                                  { method: 'DELETE' });
          if (res.ok) ok += 1;
          else bad += 1;
        } catch (e) {
          bad += 1;
        }
      }
      if (this.current && failed.some((s) => s.study_id === this.current)) {
        this.current = null;
        const main = document.getElementById('psMain');
        if (main) main.innerHTML = '<div class="ps-empty">左侧选一条研究记录查看结论</div>';
      }
      await this.refresh();
      this._setSub('已清理 ' + ok + ' 条失败记录' + (bad ? '（' + bad + ' 条删除失败）' : ''),
                   bad ? 'failed' : '');
    }

    async _get(url, timeoutMs) {
      let timer = null;
      let ctrl = null;
      const opts = {};
      if (timeoutMs) {
        ctrl = new AbortController();
        opts.signal = ctrl.signal;
        timer = setTimeout(() => ctrl.abort(), timeoutMs);
      }
      try {
        const res = await fetch(url, opts);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const j = await res.json();
        return j && j.data !== undefined ? j.data : j;
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    /* 注意：刷新期间**必须先出占位**。
       2026-10-18 实测：池子研究跑 300 只时是纯 Python 计算（backtrader），握着 GIL，
       这个列表请求要等 5 秒才回。此前占位是空白的，用户看到的就是「点了没反应」——
       面板明明开了，却什么都没有。现在立刻渲染「正在读取…」，并给请求加超时。 */
    async refresh() {
      if (this._inflight) return;          // 防叠加请求
      this._inflight = true;
      this._setSub(this.studies ? '刷新中…' : '加载中…', 'pending');
      this._renderList();                  // 立刻出占位，不等网络
      try {
        const d = await this._get(API + '/pool-studies?limit=50', 25000);
        this.studies = (d && d.studies) || [];
        this._setSub(this.studies.length ? this.studies.length + ' 条记录' : '暂无记录', '');
      } catch (e) {
        const msg = (e && e.message) ? e.message : String(e);
        // 超时/被掐：保留上次结果，不要把它清空成「加载失败」
        if (/abort|timeout/i.test(msg)) {
          this._setSub('服务繁忙，稍后点「刷新」重试', 'failed');
        } else {
          if (!this.studies) this.studies = [];
          this._setSub('列表加载失败：' + msg, 'failed');
        }
      } finally {
        this._inflight = false;
      }
      this._renderList();
      this._syncPoll();
    }

    _setSub(text, cls) {
      const el = document.getElementById('psSub');
      if (!el) return;
      el.classList.remove('pending', 'failed');
      if (cls) el.classList.add(cls);
      el.textContent = text;
    }

    _renderList() {
      const box = document.getElementById('psList');
      if (!box) return;
      const list = this.studies;
      if (list === null) {
        // 与「加载失败」区分：这时请求可能还在路上（服务忙时会等几秒）
        box.innerHTML = '<div class="ps-item pending"><div class="m">正在读取研究记录…</div></div>';
        return;
      }
      if (!list.length) {
        box.innerHTML = '<div class="ps-item"><div class="m">还没有池子研究。</div>' +
          '<div class="m">去对话里跑一个策略，在结果卡底部点「池子级研究」。</div></div>';
        return;
      }
      box.innerHTML = list.map((s) => {
        const running = s.status === 'running' || s.status === 'pending';
        const failed = s.status === 'failed';
        const v = s.verdict || {};
        let badge;
        if (running) badge = '<span class="badge ps-badge-running">进行中</span>';
        else if (failed) badge = '<span class="badge ps-badge-failed">失败</span>';
        else badge = '<span class="badge ' + (VERDICT_BADGE[v.key] || 'ps-badge-insufficient') + '">' +
          esc(v.label || '—') + '</span>';
        const uni = s.universe || {};
        const line2 = running
          ? '已跑 ' + ((s.progress && s.progress.done) || 0) + ' / ' + ((s.progress && s.progress.total) || '?') + ' 只'
          : esc(s.index_code || '') + ' · ' + (uni.total || '—') + ' 只' +
            (s.elapsed_sec ? ' · ' + Math.round(s.elapsed_sec) + 's' : '');
        // 每条都可删（运行中的不允许，服务端会 409）；失败记录尤其需要能自己清
        const del = running ? '' :
          '<button class="ps-del" type="button" data-del="' + esc(s.study_id) +
          '" title="删除这条记录" aria-label="删除">×</button>';
        return '<div class="ps-item' + (s.study_id === this.current ? ' on' : '') +
          '" data-study="' + esc(s.study_id) + '">' + del +
          '<div class="t">' + badge + esc(s.study_id) + '</div>' +
          '<div class="m">' + line2 + '</div>' +
          '<div class="m">' + esc(s.finished_at || s.created_at || '') + '</div></div>';
      }).join('');
      box.querySelectorAll('[data-study]').forEach((el) => {
        el.onclick = () => this._open(el.dataset.study);
      });
      box.querySelectorAll('[data-del]').forEach((el) => {
        el.onclick = (ev) => {
          ev.stopPropagation();          // 别顺手把详情也打开
          this._deleteStudy(el.dataset.del);
        };
      });
    }

    async _open(studyId) {
      this.current = studyId;
      this._renderList();
      const main = document.getElementById('psMain');
      if (!main) return;
      main.innerHTML = '<div class="ps-empty">读取 ' + esc(studyId) + '…</div>';
      try {
        const d = await this._get(API + '/pool-studies/' + encodeURIComponent(studyId));
        this._renderDetail(main, d);
      } catch (e) {
        main.innerHTML = '<div class="ps-empty">读取失败：' +
          esc(e && e.message ? e.message : e) + '</div>';
      }
    }

    /* 详情：一句话结论在最上面（用户要能直接复述），再给覆盖/三档/行业/局限 */
    _renderDetail(main, d) {
      d = d || {};
      const rep = d.report || {};
      const v = rep.verdict || d.verdict || {};
      const uni = rep.universe || d.universe || {};
      const period = rep.period || d.period || {};
      let html = '';

      html += '<div class="sc-card sc-pool">';
      html += '<div class="sc-head"><div class="sc-title">池子级研究：' +
        esc(rep.index_code || d.index_code || '') + ' · ' + (uni.total || 0) + ' 只 · ' +
        (uni.industries || 0) + ' 个行业</div>';
      html += '<div class="sc-sub">' + esc(period.start || '') + ' ~ ' +
        esc(period.end || '') + (period.days ? '（' + period.days + ' 天）' : '') + '</div></div>';

      if (d.status === 'running' || d.status === 'pending') {
        const p = d.progress || {};
        const pct = p.total ? Math.max(2, Math.round(p.done / p.total * 100)) : 2;
        html += '<div class="ps-prog-wrap"><div class="ps-prog">' +
          '<div class="ps-prog-fill" style="width:' + pct + '%"></div></div>' +
          '<div class="ps-prog-meta"><span>正在跑 ' + (p.done || 0) + ' / ' + (p.total || '?') +
          ' 只（失败 ' + (p.fail || 0) + '）</span><span>' + pct + '%</span></div></div>';
        html += '<div class="sc-note">全池 300 只约 4-7 分钟，跑完自动刷新。</div>';
      } else if (d.status === 'failed') {
        html += '<div class="sc-note">研究失败：' + esc(d.error || '原因未记录') + '</div>';
      } else if (v.headline) {
        html += '<div class="sc-pool-verdict sc-pool-' + esc(v.key || 'unknown') + '">' +
          '<span class="sc-pool-label">' + esc(v.label || '—') + '</span>' +
          '<div class="sc-pool-headline">' + esc(v.headline) + '</div></div>';
      }

      const o = rep.overall || d.overall || {};
      if (o.total) {
        const kpis = [['有效 / 可评判', (o.valid || 0) + ' / ' + (o.scored || 0)],
          ['胜率中位', pct(o.win_rate_median)], ['收益中位', pct(o.total_return_median)],
          ['夏普中位', num(o.sharpe_median)],
          ['盈亏比中位', num(o.profit_factor_median)],
          ['平均持有', o.avg_hold_days_median == null ? '—'
            : num(o.avg_hold_days_median, 1) + ' 天'],
          ['回撤中位', pct(o.max_drawdown_median)]];
        html += '<div class="sc-kpis">';
        kpis.forEach((k) => {
          html += '<div class="sc-kpi"><div class="sc-kpi-k">' + k[0] + '</div>' +
            '<div class="sc-kpi-v">' + k[1] + '</div></div>';
        });
        html += '</div>';
      }

      /* 组合口径：共享资金、等权、最多 N 仓 —— 单票研究有的质量指标这里也要有 */
      const pfd = rep.portfolio || d.portfolio || {};
      if (pfd.total_return !== undefined && pfd.total_return !== null) {
        const pk = [['期末资金', Number(pfd.final_equity || 0)
          .toLocaleString('zh-CN', {maximumFractionDigits: 0})],
          ['总收益', pct(pfd.total_return)], ['年化', pct(pfd.cagr)],
          ['夏普', num(pfd.sharpe)], ['最大回撤', pct(pfd.max_drawdown)],
          ['胜率', pct(pfd.win_rate)], ['盈亏比', num(pfd.profit_factor)],
          ['平均持有', pfd.avg_hold_days == null ? '—'
            : num(pfd.avg_hold_days, 1) + ' 天'],
          ['Calmar', num(pfd.calmar)], ['交易', (pfd.closed_trades || 0) + ' 笔']];
        html += '<div class="sc-metrics-title">组合（共享资金，' +
          (pfd.max_positions || '?') + ' 仓等权）</div>';
        html += '<div class="sc-kpis">';
        pk.forEach((k) => {
          html += '<div class="sc-kpi"><div class="sc-kpi-k">' + k[0] + '</div>' +
            '<div class="sc-kpi-v">' + k[1] + '</div></div>';
        });
        html += '</div>';
      }

      /* 持有期 × 卖出口径矩阵：只改 hold_bars 与卖出口径，其余完全相同 */
      const sweep = rep.sweep || {};
      const swRows = sweep.rows || {};
      const swHolds = sweep.holds || [];
      const swModes = sweep.modes || [];
      if (swHolds.length && swModes.length) {
        const swNames = {next_open: '次日开盘卖', next_close: '次日收盘卖', same_close: '当日收盘卖'};
        html += '<div class="sc-metrics-title">持有期 × 卖出口径矩阵</div>';
        html += '<table class="sc-pool-table"><thead><tr>' +
          '<th>持有期</th><th>卖出口径</th><th>胜率中位</th><th>收益中位</th>' +
          '<th>夏普中位</th><th>组合收益</th><th>最大回撤</th></tr></thead><tbody>';
        swHolds.forEach((h) => {
          swModes.forEach((m) => {
            const row = swRows[String(h) + '_' + m] || {};
            const oo = row.overall || {};
            const pp = row.portfolio || {};
            html += '<tr><td>持有 ' + h + ' 根</td><td>' + esc(swNames[m] || m) + '</td>' +
              '<td>' + pct(oo.win_rate_median) + '</td><td>' + pct(oo.total_return_median) + '</td>' +
              '<td>' + num(oo.sharpe_median) + '</td>' +
              '<td>' + pct(pp.total_return) + '</td><td>' + pct(pp.max_drawdown) + '</td></tr>';
          });
        });
        html += '</tbody></table>';
      }

      /* 卖出口径对比：同一策略/区间/池子，开盘卖 vs 收盘卖各跑一遍 */
      const cmp = rep.compare || {};
      const cmpRows = cmp.rows || {};
      const cmpModes = cmp.modes || [];
      if (cmpModes.length >= 2) {
        const names = {next_open: '次日开盘卖', next_close: '次日收盘卖', same_close: '当日收盘卖'};
        html += '<div class="sc-metrics-title">卖出口径对比</div>';
        html += '<table class="sc-pool-table"><thead><tr>' +
          '<th>卖出口径</th><th>有效占比</th><th>胜率中位</th><th>收益中位</th>' +
          '<th>夏普中位</th><th>组合收益</th><th>最大回撤</th></tr></thead><tbody>';
        cmpModes.forEach((m) => {
          const row = cmpRows[m] || {};
          const oo = row.overall || {};
          const pp = row.portfolio || {};
          html += '<tr><td>' + esc(row.label || names[m] || m) + '</td>' +
            '<td>' + pct(oo.valid_ratio) + '</td><td>' + pct(oo.win_rate_median) + '</td>' +
            '<td>' + pct(oo.total_return_median) + '</td><td>' + num(oo.sharpe_median) + '</td>' +
            '<td>' + pct(pp.total_return) + '</td>' +
            '<td>' + pct(pp.max_drawdown) + '</td></tr>';
        });
        html += '</tbody></table>';
      }

      const inds = rep.by_industry || [];
      if (inds.length) {
        html += '<table class="sc-pool-table"><thead><tr>' +
          '<th>行业</th><th>只数</th><th>可评判</th><th>有效占比</th>' +
          '<th>胜率中位</th><th>收益中位</th></tr></thead><tbody>';
        inds.filter((r) => (r.scored || 0) >= 3).slice(0, 15).forEach((r) => {
          html += '<tr><td>' + esc(r.industry || '') + '</td>' +
            '<td>' + (r.n || 0) + '</td><td>' + (r.scored || 0) + '</td>' +
            '<td>' + pct(r.valid_ratio) + '</td><td>' + pct(r.win_rate_median) + '</td>' +
            '<td>' + pct(r.total_return_median) + '</td></tr>';
        });
        html += '</tbody></table>';
      }

      const items = rep.items || [];
      if (items.length) {
        html += '<table class="sc-pool-table"><thead><tr>' +
          '<th>标的</th><th>行业</th><th>判定</th><th>笔数</th><th>胜率</th>' +
          '<th>区间收益</th><th>夏普</th><th>盈亏比</th><th>平均持有</th><th>回撤</th>' +
          '</tr></thead><tbody>';
        items.forEach((it) => {
          html += '<tr><td>' + esc(String(it.code || '') + ' ' + String(it.name || '')) + '</td>' +
            '<td>' + esc(it.industry || '—') + '</td><td>' + esc(it.bucket || '') + '</td>' +
            '<td>' + (it.trades === null || it.trades === undefined ? '—' : it.trades) + '</td>' +
            '<td>' + pct(it.win_rate) + '</td><td>' + pct(it.total_return) + '</td>' +
            '<td>' + num(it.sharpe) + '</td><td>' + num(it.profit_factor) + '</td>' +
            '<td>' + (it.avg_hold_days == null ? '—' : num(it.avg_hold_days, 1) + ' 天') + '</td>' +
            '<td>' + pct(it.max_drawdown) + '</td></tr>';
        });
        html += '</tbody></table>';
      }

      /* 组合近期交易：单票研究有逐笔明细，池子也至少给最近 10 笔 */
      const rt = pfd.recent_trades || [];
      if (rt.length) {
        html += '<div class="sc-metrics-title">组合近期交易</div>';
        html += '<table class="sc-pool-table"><thead><tr>' +
          '<th>标的</th><th>买入</th><th>卖出</th><th>收益</th><th>持有</th></tr></thead><tbody>';
        rt.slice(-10).reverse().forEach((t) => {
          html += '<tr><td>' + esc(t.code || '') + '</td><td>' + esc(t.buy_date || '—') +
            '</td><td>' + esc(t.sell_date || '—') + '</td><td>' +
            (t.pnl_pct == null ? '—' : num(t.pnl_pct) + '%') + '</td><td>' +
            (t.hold_days == null ? '—' : t.hold_days + ' 天') + '</td></tr>';
        });
        html += '</tbody></table>';
      }

      const cav = rep.caveats || [];
      if (cav.length) {
        html += '<div class="sc-pool-caveats">' +
          '<div class="sc-metrics-title">这份结论的边界</div>';
        cav.forEach((t) => { html += '<div class="sc-note">' + esc(t) + '</div>'; });
        html += '</div>';
      }

      if (d.run_uuid) {
        html += '<div class="ps-meta-row">来源研究：' + esc(d.run_uuid) + '</div>';
      }
      html += '</div>';
      main.innerHTML = html;
    }

    /* 有进行中的研究才轮询；没有就停，别空转 */
    _syncPoll() {
      const running = (this.studies || []).some(
        (s) => s.status === 'running' || s.status === 'pending');
      if (running) this._startPoll(); else this._stopPoll();
    }

    _startPoll() {
      if (this._timer) return;
      this._timer = setInterval(() => this.refresh(), 4000);
    }

    _stopPoll() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    }
  }

  window.StockRadar = window.StockRadar || {};
  let inst = null;
  window.initPoolStudyPanel = function () {
    if (!inst) inst = new PoolStudyPanel();
    return inst;
  };
  window.StockRadar.poolStudy = {
    openPage: function (id) { window.initPoolStudyPanel().openPage(id); },
    open: function (id) { window.initPoolStudyPanel().openPage(id); }
  };
})();
