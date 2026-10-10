/**
 * 拥挤度排查 tab — 数据中心面板内渲染。
 *
 * 数据源：GET /api/crowding/latest（最近一轮）/ /api/crowding/pool（池定义）
 * 触发：POST /api/crowding/scan（后台跑）→ 轮询 GET /api/scans/status 直到 crowding_scan 结束。
 *
 * 为什么扫全市场要放后台：板块池有 496 个标的，上游每次调用有 2–5s 随机限流，
 * 首轮补数要跑十几分钟；挂在请求上就是拿浏览器当进度条。
 */
(function () {
  'use strict';

  var LEVEL_CLASS = {
    '舒适': 'lv-0', '中性': 'lv-1', '偏挤': 'lv-2', '拥挤': 'lv-3', '过热': 'lv-4'
  };
  var CAT_LABEL = { index: '指数', board: '板块' };

  function num(v, digits, suffix) {
    if (v === null || v === undefined || v === '' || !isFinite(v)) return '—';
    return Number(v).toFixed(digits) + (suffix || '');
  }
  function signed(v, digits, suffix) {
    if (v === null || v === undefined || v === '' || !isFinite(v)) return '—';
    var n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(digits) + (suffix || '');
  }
  function scoreClass(level) { return LEVEL_CLASS[level] || 'lv-na'; }

  class CrowdingPanel {
    constructor() {
      this._data = null;
      this._bind();
    }

    _bind() {
      var self = this;
      var refresh = document.getElementById('crBtnRefresh');
      if (refresh) refresh.addEventListener('click', function () { self.load(); });
      var scan = document.getElementById('crBtnScan');
      if (scan) scan.addEventListener('click', function () { self.scan(); });
      var cat = document.getElementById('crFilterCat');
      if (cat) cat.addEventListener('change', function () { self._renderTable(); });
      var lv = document.getElementById('crFilterLevel');
      if (lv) lv.addEventListener('change', function () { self._renderTable(); });
    }

    async load() {
      try {
        var res = await fetch('/api/crowding/latest?limit=800');
        var body = await res.json();
        this._data = (body && body.data) || null;
        this._renderAll();
        // 必须 await：否则调用方（页签切换、扫描后回读）拿到的是「半渲染」的面板，
        // 按钮的 scanning 态要等下一个微任务才生效，看起来像没反应。
        await this._refreshScanState();
      } catch (e) {
        console.error('加载拥挤度失败:', e);
        this._setMeta('加载失败');
      }
    }

    _setMeta(text) {
      var el = document.getElementById('crMeta');
      if (el) el.textContent = text;
    }

    _renderAll() {
      var d = this._data;
      if (!d) { this._setMeta('—'); return; }
      if (!d.available) {
        this._setMeta('尚未扫描');
        this._renderCards(null);
        this._renderStrip(null);
        this._renderWarn(d.pool_problems || [], []);
        this._renderTable();
        var foot = document.getElementById('crFoot');
        if (foot) foot.textContent = '还没有扫描记录。点「后台扫描」跑一轮（首轮要补几百个标的的日线，会跑十几分钟，期间可关闭页面）。';
        return;
      }
      this._setMeta('数据日期 ' + (d.trade_date || '—') + ' · 扫描于 ' + (d.scanned_at || '—'));
      this._renderCards(d);
      this._renderStrip(d);
      this._renderWarn(d.pool_problems || [], d.warnings || []);
      this._renderTable();
      var f = document.getElementById('crFoot');
      if (f) {
        f.textContent = '共 ' + (d.item_total || 0) + ' 个标的 · 已算分 ' + (d.computed_count || 0)
          + ' · 待补数 ' + (d.insufficient_count || 0) + ' · 数据陈旧 ' + (d.stale_count || 0)
          + ' · 本轮补数 ' + (d.collected_count || 0) + ' 个 · 耗时 ' + ((d.elapsed_ms || 0) / 1000).toFixed(1) + 's';
      }
    }

    _renderCards(d) {
      var el = document.getElementById('crCards');
      if (!el) return;
      if (!d) { el.innerHTML = ''; return; }
      var cards = [
        ['标的池', d.pool_size, '个（指数 ' + ((d.pool && d.pool.indices) || 0) + ' + 板块 ' + ((d.pool && d.pool.boards) || 0) + '）'],
        ['已算分', d.computed_count, '有足够历史且不陈旧'],
        ['拥挤 + 过热', d.hot_count, '分数 ≥ ' + num(d.hot_score, 0)],
        ['待补数', d.insufficient_count, '历史不足或无本地日线'],
      ];
      el.innerHTML = cards.map(function (c) {
        return '<div class="cr-card"><div class="cr-card-label">' + c[0] + '</div>'
          + '<div class="cr-card-value">' + (c[1] === null || c[1] === undefined ? '—' : c[1]) + '</div>'
          + '<div class="cr-card-sub">' + c[2] + '</div></div>';
      }).join('');
    }

    _renderStrip(d) {
      var el = document.getElementById('crStrip');
      if (!el) return;
      if (!d || !d.available) { el.innerHTML = ''; return; }
      var hist = d.counts_by_level || {};
      var order = d.levels_order || Object.keys(hist);
      var total = order.reduce(function (s, k) { return s + (hist[k] || 0); }, 0) || 1;
      el.innerHTML = order.map(function (k) {
        var n = hist[k] || 0;
        return '<div class="cr-strip-seg ' + scoreClass(k) + '" style="flex:' + Math.max(n, 0.4) + ';" title="' + k + ' ' + n + ' 个">'
          + '<span class="cr-strip-name">' + k + '</span><span class="cr-strip-n">' + n + '</span>'
          + '<span class="cr-strip-pct">' + (n / total * 100).toFixed(0) + '%</span></div>';
      }).join('');
    }

    _renderWarn(poolProblems, warnings) {
      var el = document.getElementById('crWarn');
      if (!el) return;
      var lines = [];
      (poolProblems || []).forEach(function (p) { lines.push('池配置：' + p); });
      (warnings || []).forEach(function (w) { lines.push(w); });
      if (!lines.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
      el.style.display = 'block';
      el.innerHTML = lines.slice(0, 6).map(function (l) {
        return '<div class="cr-warn-line">• ' + l + '</div>';
      }).join('');
    }

    _filtered() {
      var d = this._data;
      if (!d || !d.available) return [];
      var catEl = document.getElementById('crFilterCat');
      var lvEl = document.getElementById('crFilterLevel');
      var cat = (catEl && catEl.value) || 'all';
      var lv = (lvEl && lvEl.value) || 'all';
      var hot = Number(d.hot_score || 75);
      return (d.items || []).filter(function (it) {
        if (cat !== 'all' && it.category !== cat) return false;
        if (lv === 'hot') return it.status === 'ok' && (it.score || 0) >= hot;
        if (lv === 'ok') return it.status === 'ok';
        if (lv === 'pending') return it.status !== 'ok';
        return true;
      });
    }

    _renderTable() {
      var el = document.getElementById('crTable');
      if (!el) return;
      var rows = this._filtered();
      var head = '<thead><tr>'
        + '<th style="width:38px;">#</th>'
        + '<th>标的</th>'
        + '<th style="width:52px;">类型</th>'
        + '<th style="width:64px;text-align:right;">拥挤分</th>'
        + '<th style="width:56px;">档位</th>'
        + '<th style="width:78px;text-align:right;">均线延伸</th>'
        + '<th style="width:70px;text-align:right;">20日</th>'
        + '<th style="width:70px;text-align:right;">60日</th>'
        + '<th style="width:70px;text-align:right;">放量比</th>'
        + '<th style="width:92px;">数据日期</th>'
        + '</tr></thead>';
      if (!rows.length) {
        el.innerHTML = head + '<tbody><tr><td colspan="10" class="cr-empty">没有符合条件的标的（可切换筛选，或先跑一轮扫描）</td></tr></tbody>';
        return;
      }
      var body = rows.map(function (it, i) {
        var ok = it.status === 'ok';
        var scoreCell = ok ? num(it.score, 1) : '<span class="cr-cell-muted">' + (it.status === 'stale' ? '陈旧' : '待补数') + '</span>';
        var lvCell = ok ? '<span class="cr-level ' + scoreClass(it.level) + '">' + (it.level || '—') + '</span>' : '<span class="cr-cell-muted">—</span>';
        return '<tr class="cr-row ' + (ok ? '' : 'cr-row-pending') + '" title="' + (it.detail || '') + '">'
          + '<td class="cr-cell-muted">' + (i + 1) + '</td>'
          + '<td><span class="cr-name">' + (it.name || it.code) + '</span>'
          + '<span class="cr-code">' + (it.code || '') + '</span></td>'
          + '<td class="cr-cell-muted">' + (CAT_LABEL[it.category] || it.category) + '</td>'
          + '<td style="text-align:right;"><span class="cr-score">' + scoreCell + '</span></td>'
          + '<td>' + lvCell + '</td>'
          + '<td style="text-align:right;">' + signed(it.ext_dev, 1, '%') + '</td>'
          + '<td style="text-align:right;">' + signed(it.ret20, 1, '%') + '</td>'
          + '<td style="text-align:right;">' + signed(it.ret60, 1, '%') + '</td>'
          + '<td style="text-align:right;">' + num(it.amount_ratio, 2) + '</td>'
          + '<td class="cr-cell-muted">' + (it.asof || '—') + '</td>'
          + '</tr>';
      }).join('');
      el.innerHTML = head + '<tbody>' + body + '</tbody>';
    }

    /* ── 触发后台扫描 ─────────────────────────────────────── */

    async _scanState() {
      try {
        var res = await fetch('/api/scans/status');
        var st = await res.json();
        var jobs = (st && st.data && st.data.jobs) || [];
        for (var i = 0; i < jobs.length; i++) {
          if (jobs[i].key === 'crowding_scan') return jobs[i];
        }
        return null;
      } catch (e) { return null; }
    }

    async _refreshScanState() {
      var btn = document.getElementById('crBtnScan');
      if (!btn) return;
      var job = await this._scanState();
      if (job && job.running) {
        btn.disabled = true;
        btn.classList.add('scanning');
        var note = document.getElementById('crFoot');
        if (note) note.textContent = '扫描进行中…' + (job.last_summary || '');
      } else {
        btn.disabled = false;
        btn.classList.remove('scanning');
      }
    }

    async scan() {
      var btn = document.getElementById('crBtnScan');
      var foot = document.getElementById('crFoot');
      var before = await this._scanState();
      var stamp = before ? (before.last_finished_at || '') : '';
      if (btn) { btn.disabled = true; btn.classList.add('scanning'); }
      if (foot) foot.textContent = '已提交后台扫描，等待完成…（首轮补数要十几分钟，可关闭页面）';
      try {
        var res = await fetch('/api/crowding/scan', { method: 'POST' });
        var body = await res.json();
        if (body && body.data && body.data.accepted === false) {
          if (foot) foot.textContent = body.data.msg || '扫描已在进行中';
        }
        await this._awaitScan(stamp);
        await this.load();
      } catch (e) {
        console.error('触发拥挤度扫描失败:', e);
        if (foot) foot.textContent = '触发失败: ' + (e.message || e);
      }
      if (btn) { btn.disabled = false; btn.classList.remove('scanning'); }
    }

    async _awaitScan(stamp, timeoutMs) {
      var limit = timeoutMs || 1800000;   // 首轮补数上限按 30 分钟算
      var t0 = Date.now();
      while (Date.now() - t0 < limit) {
        await new Promise(function (r) { setTimeout(r, 3000); });
        var job = await this._scanState();
        if (!job) return false;
        if (job.running) continue;
        if ((job.last_finished_at || '') !== stamp) return true;
        return true;
      }
      return false;
    }
  }

  window._crowdingPanel = null;
  window.initCrowdingScan = function () {
    if (!window._crowdingPanel) window._crowdingPanel = new CrowdingPanel();
    return window._crowdingPanel;
  };
})();
