/**
 * 存储监控 tab — 数据中心面板内渲染，对齐 docs/prototypes/data-monitor.html 原型
 * 依赖 chart.js（index.html 已引入）与 /api/data-monitor/* API
 */
(function () {
  'use strict';

  function fmtSize(b) {
    b = b || 0;
    if (b >= 1e9) return (b / 1e9).toFixed(2) + ' GB';
    if (b >= 1e6) return (b / 1e6).toFixed(1) + ' MB';
    if (b >= 1e3) return (b / 1e3).toFixed(0) + ' KB';
    return b + ' B';
  }
  function fmtBytesSplit(b) {
    const s = fmtSize(b).split(' ');
    return { n: s[0], unit: s[1] };
  }
  const CAT_LABEL = { db: 'DB', vector: '向量', log: '日志', json: 'JSON', cache: '缓存', data: '数据', skill: '技能', other: '其他' };
  const CAT_ICON = {
    db: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>',
    vector: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="12" cy="18" r="3"/><path d="M8.5 7.5 10.5 15.5"/><path d="M15.5 7.5 13.5 15.5"/></svg>',
    log: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6"/><path d="M9 17h4"/></svg>',
    json: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H7a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h1"/><path d="M16 3h1a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-1"/></svg>',
    cache: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.27 6.96 12 12.01l8.73-5.05"/><path d="M12 22.08V12"/></svg>',
    data: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
    skill: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>',
    other: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/></svg>',
  };

  class StorageMonitor {
    constructor() {
      this._chart = null;
      this._clean = [];
      this._bind();
    }

    _bind() {
      document.getElementById('dmBtnScan')?.addEventListener('click', () => this.scan());
      document.getElementById('dmBtnClean')?.addEventListener('click', () => this.runClean());
      document.getElementById('dmTrendToggle')?.addEventListener('click', function () {
        const legend = document.getElementById('dmChartLegend');
        if (!legend) return;
        const hidden = legend.style.display === 'none';
        legend.style.display = hidden ? 'flex' : 'none';
        this.textContent = hidden ? '▼ 收起图例' : '▲ 展开图例';
      });
      document.getElementById('dmRefreshClean')?.addEventListener('click', () => this.loadClean());
      this._bindConfig();
    }

    /* ── 扫描配置（GET/POST /api/data-monitor/config） ───────── */
    _bindConfig() {
      const toggle = document.getElementById('dmCfgToggle');
      const body = document.getElementById('dmCfgBody');
      toggle?.addEventListener('click', () => {
        const show = body.style.display === 'none';
        body.style.display = show ? 'block' : 'none';
        toggle.textContent = show ? '收起' : '展开';
        if (show && !this._cfgLoaded) this.loadConfig();
      });
      document.getElementById('dmCfgSave')?.addEventListener('click', () => this.saveConfig());
    }

    async loadConfig() {
      try {
        const res = await fetch('/api/data-monitor/config');
        const cfg = await res.json();
        const lines = a => (Array.isArray(a) ? a : []).join('\n');
        document.getElementById('dmCfgDirs').value = lines(cfg.extra_scan_dirs);
        document.getElementById('dmCfgFiles').value = lines(cfg.extra_files);
        document.getElementById('dmCfgExcludes').value = (cfg.exclude_patterns || []).join(', ');
        document.getElementById('dmCfgRetention').value = cfg.snapshot_retention_days ?? 90;
        document.getElementById('dmCfgLogLines').value = cfg.log_min_lines ?? 200;
        document.getElementById('dmCfgStaleDays').value = cfg.stale_cache_days ?? 30;
        this._cfgLoaded = true;
      } catch (e) { console.error('加载扫描配置失败:', e); }
    }

    async saveConfig() {
      const splitLines = v => v.split('\n').map(s => s.trim()).filter(Boolean);
      const splitCsv = v => v.split(',').map(s => s.trim()).filter(Boolean);
      const num = (id, fallback) => {
        const n = parseInt(document.getElementById(id).value, 10);
        return Number.isFinite(n) && n > 0 ? n : fallback;
      };
      const data = {
        extra_scan_dirs: splitLines(document.getElementById('dmCfgDirs').value),
        extra_files: splitLines(document.getElementById('dmCfgFiles').value),
        exclude_patterns: splitCsv(document.getElementById('dmCfgExcludes').value),
        snapshot_retention_days: num('dmCfgRetention', 90),
        log_min_lines: num('dmCfgLogLines', 200),
        stale_cache_days: num('dmCfgStaleDays', 30),
      };
      const msg = document.getElementById('dmCfgMsg');
      try {
        const res = await fetch('/api/data-monitor/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data }),
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        if (msg) msg.textContent = '已保存，重新扫描后生效 ✓';
        setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
      } catch (e) {
        if (msg) msg.textContent = '保存失败: ' + (e.message || e);
      }
    }

    async load() {
      try {
        const res = await fetch('/api/data-monitor/overview');
        const data = await res.json();
        this._renderAll(data);
        await Promise.all([this._loadTrend(), this.loadClean()]);
        this.loadScans();
      } catch (e) { console.error('加载存储监控失败:', e); }
    }

    /* ── 后台巡检状态（/api/scans/status） ─────────────────────
     * 台账刷新 / 存储扫描 / 拥挤度扫描 都是启动后异步跑的，
     * 这里把它们「上次跑了什么、下次什么时候」摊开，避免后台在偷偷干活却没人知道。
     */
    async loadScans() {
      const el = document.getElementById('dmScans');
      if (!el) return;
      try {
        const res = await fetch('/api/scans/status');
        const st = await res.json();
        this._renderScans((st.data && st.data.jobs) || []);
      } catch (e) {
        el.innerHTML = '<span class="dm-scans-muted">巡检状态不可用</span>';
      }
    }

    _renderScans(jobs) {
      const el = document.getElementById('dmScans');
      if (!el) return;
      if (!jobs.length) {
        el.innerHTML = '<span class="dm-scans-muted">后台巡检未注册（BACKGROUND_SCANS_ENABLED=false？）</span>';
        return;
      }
      el.innerHTML = jobs.map(j => {
        let cls = 'idle', text = '待首次运行';
        if (!j.enabled) { cls = 'off'; text = '未启用'; }
        else if (j.running) { cls = 'run'; text = '进行中…'; }
        else if (j.last_ok === true) { cls = 'ok'; text = '正常'; }
        else if (j.last_ok === false) { cls = 'bad'; text = '上次失败'; }
        const next = (j.next_run_at || '').slice(11, 16);
        const dur = (j.last_duration_ms != null) ? (j.last_duration_ms / 1000).toFixed(1) + 's' : '';
        const tail = [j.enabled && next ? '下次 ' + next : '', dur].filter(Boolean).join(' · ');
        return `<div class="dm-scan-item" title="${j.description || ''}">
          <span class="dm-scan-dot ${cls}"></span>
          <span class="dm-scan-name">${j.name}</span>
          <span class="dm-scan-sum">${j.last_summary || text}</span>
          <span class="dm-scan-time">${tail}</span>
        </div>`;
      }).join('');
    }

    /** 当前 storage_scan 的完成时间戳（用于判断「这一轮」是否跑完）。 */
    async _scanStamp() {
      try {
        const res = await fetch('/api/scans/status');
        const st = await res.json();
        const job = ((st.data && st.data.jobs) || []).find(j => j.key === 'storage_scan');
        return job ? (job.last_finished_at || '') : '';
      } catch (e) { return ''; }
    }

    /** 轮询巡检状态，等 storage_scan 这一轮结束。 */
    async _awaitScan(before, timeoutMs = 300000) {
      const t0 = Date.now();
      while (Date.now() - t0 < timeoutMs) {
        await new Promise(r => setTimeout(r, 1500));
        let job = null;
        try {
          const res = await fetch('/api/scans/status');
          const st = await res.json();
          job = ((st.data && st.data.jobs) || []).find(j => j.key === 'storage_scan');
        } catch (e) { continue; }
        if (!job) return false;
        if (job.running) continue;
        if ((job.last_finished_at || '') !== before) return true;
      }
      return false;
    }

    /**
     * 重新扫描。后端默认**后台执行**（全量扫描要遍历十几个库逐表 COUNT，
     * 同步等会让请求挂十几秒），所以这里改为：触发 → 轮询巡检状态 → 刷新面板。
     * 传 wait=1 的老同步路径仍兼容（返回体里直接带 assets）。
     */
    async scan() {
      const btn = document.getElementById('dmBtnScan');
      const wrap = document.getElementById('dmProgressWrap');
      const fill = document.getElementById('dmProgressFill');
      const label = document.getElementById('dmProgressLabel');
      btn.classList.add('scanning'); btn.disabled = true;
      wrap.classList.add('show'); fill.style.width = '0%';
      const steps = ['发现数据资产…', '扫描 db/ 目录…', '读取表级明细…', '统计日志 / 向量 / 缓存…', '落历史快照…'];
      let i = 0;
      const timer = setInterval(() => {
        i = Math.min(i + 1, 4);
        fill.style.width = Math.min(8 + i * 18 + Math.random() * 6, 92) + '%';
        label.textContent = steps[i];
      }, 180);
      const before = await this._scanStamp();
      try {
        const res = await fetch('/api/data-monitor/scan', { method: 'POST' });
        const data = await res.json();
        if (data && data.accepted) {
          label.textContent = '扫描已在后台启动，等待完成…';
          const ok = await this._awaitScan(before);
          clearInterval(timer);
          if (!ok) {
            label.textContent = '后台扫描未在预期时间内完成，可稍后刷新查看';
          } else {
            fill.style.width = '100%'; label.textContent = '扫描完成 ✓';
            await this.load();
          }
        } else {
          clearInterval(timer);
          fill.style.width = '100%'; label.textContent = '扫描完成 ✓';
          this._renderAll(data);
          await Promise.all([this._loadTrend(), this.loadClean()]);
          this.loadScans();
        }
      } catch (e) {
        clearInterval(timer);
        label.textContent = '扫描失败: ' + (e.message || e);
        console.error('扫描失败:', e);
      }
      setTimeout(() => {
        wrap.classList.remove('show');
        btn.classList.remove('scanning'); btn.disabled = false;
      }, 600);
    }

    _renderAll(data) {
      document.getElementById('dmLastScan').textContent = '上次扫描：' + (data.scanned_at || '—');
      const st = (data.scanned_at || '').trim();
      this._lastScanTime = st ? (st.split(' ')[1] || st).slice(0, 5) : '';
      const assets = (data.assets || []).slice().sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0));
      const agg = this._aggregate(assets);
      this._renderHero(agg);
      this._renderAssets(assets);
      this._renderRank(assets);
      document.getElementById('dmAssetsHint').textContent = `共 ${assets.length} 项 · 点击行展开表级明细`;
    }

    _aggregate(assets) {
      const agg = {};
      for (const a of assets) {
        agg[a.category] = agg[a.category] || { size: 0, record: 0, n: 0 };
        agg[a.category].size += a.size_bytes || 0;
        agg[a.category].record += a.record_count || 0;
        agg[a.category].n += 1;
      }
      const total = Object.values(agg).reduce((s, x) => s + x.size, 0);
      const order = ['db', 'vector', 'log', 'json', 'cache', 'data', 'skill', 'other'];
      return order.filter(c => agg[c]).map(c => ({ cat: c, ...agg[c], pct: ((agg[c].size / (total || 1)) * 100).toFixed(1) }));
    }

    _renderHero(cards) {
      const el = document.getElementById('dmHero');
      if (!el) return;
      el.innerHTML = cards.map((c, idx) => {
        const v = fmtBytesSplit(c.size);
        return `<div class="dm-hero-card" data-cat="${c.cat}">
          <div class="dm-row1">
            <span class="dm-cat">${CAT_ICON[c.cat] || ''}${CAT_LABEL[c.cat] || c.cat}</span>
            <span class="dm-delta flat">…</span>
          </div>
          <div><span class="dm-value">${v.n}</span><span class="dm-unit">${v.unit}</span></div>
          <div class="dm-sub">${c.n} 项 · ${c.record.toLocaleString()} 记录 · 占 ${c.pct}%</div>
        </div>`;
      }).join('');
    }

    _renderAssets(assets) {
      const el = document.getElementById('dmAssets');
      if (!el) return;
      const maxSize = Math.max(...assets.map(a => a.size_bytes || 0), 1);
      const head = `<thead><tr>
        <th style="width:88px;">分类</th><th>资产</th>
        <th style="width:80px;text-align:right;">占用</th><th style="width:92px;text-align:right;">记录数</th>
        <th style="width:110px;text-align:right;">最后更新</th><th style="width:110px;">占比</th><th style="width:22px;"></th>
      </tr></thead>`;
      const rows = assets.map((a, i) => {
        const hasTables = !!(a.tables && a.tables.length && a.tables.some(t => t.name));
        const pct = ((a.size_bytes || 0) / maxSize * 100).toFixed(0);
        const miniTot = (a.tables || []).reduce((s, t) => s + (t.count || 0), 0);
        const miniRows = (a.tables || []).map(t => {
          const tp = miniTot ? ((t.count || 0) / miniTot * 100) : 0;
          return `<tr><td class="dm-tname">${t.name}</td><td>${(t.count || 0).toLocaleString()}</td><td>${t.latest || '—'}</td>
            <td><div style="display:flex;align-items:center;gap:8px;"><div class="dm-bar-track" style="flex:1;min-width:60px;"><div class="dm-bar-fill" style="width:${Math.max(tp, 1.5)}%;"></div></div><span class="dm-cell-muted">${tp.toFixed(1)}%</span></div></td></tr>`;
        }).join('');
        return `<tr class="dm-row" data-i="${i}">
          <td><span class="dm-cat-chip ${a.category}">${CAT_ICON[a.category] || ''}${CAT_LABEL[a.category] || a.category}</span></td>
          <td><span class="dm-cell-num" style="font-weight:550;font-size:12px;">${a.name}</span>
              <span class="dm-cell-muted" style="font-size:11px;"> ${a.key || ''}</span>
              ${a.error ? '<span style="color:var(--danger);font-size:10px;margin-left:6px;">扫描失败</span>' : ''}</td>
          <td style="text-align:right;"><span class="dm-cell-num" style="font-weight:700;">${fmtSize(a.size_bytes)}</span></td>
          <td style="text-align:right;"><span class="dm-cell-num">${(a.record_count || 0).toLocaleString()}</span></td>
          <td style="text-align:right;"><span class="dm-cell-num dm-cell-muted">${a.mtime || '—'}</span></td>
          <td><div class="dm-bar-track"><div class="dm-bar-fill" style="width:${pct}%;"></div></div></td>
          <td><span class="dm-expand-icon">${hasTables ? '▸' : ''}</span></td>
        </tr>
        ${hasTables ? `<tr class="dm-detail-row" style="display:none;" data-i="${i}">
          <td colspan="7"><div class="dm-detail-inner">
            <div class="dm-tbl-title">表级明细 · ${a.tables.length} 张表 · 动态扫描（不写死 SQL）</div>
            <table class="dm-tbl-mini"><thead><tr><th style="width:38%;">表名</th><th>记录数</th><th>最新日期</th><th>占比</th></tr></thead>
            <tbody>${miniRows}</tbody></table>
          </div></td>
        </tr>` : ''}`;
      }).join('');
      el.innerHTML = head + `<tbody>${rows}</tbody>`;
      el.querySelectorAll('tr.dm-row').forEach(tr => {
        tr.addEventListener('click', () => {
          const i = tr.dataset.i;
          tr.classList.toggle('open');
          const detail = el.querySelector(`tr.dm-detail-row[data-i="${i}"]`);
          if (detail) detail.style.display = detail.style.display === 'none' ? 'table-row' : 'none';
        });
      });
    }

    _renderRank(assets) {
      const el = document.getElementById('dmRank');
      const hint = document.getElementById('dmRankHint');
      if (!el) return;
      const top = assets.slice(0, 5);
      const total = assets.reduce((s, a) => s + (a.size_bytes || 0), 0);
      const topTotal = top.reduce((s, a) => s + (a.size_bytes || 0), 0);
      if (hint) hint.textContent = total ? `占总量 ${((topTotal / total) * 100).toFixed(0)}%` : '';
      const maxSize = Math.max(...top.map(a => a.size_bytes || 0), 1);
      el.innerHTML = top.length
        ? top.map((a, i) => `<div class="dm-rank-row">
            <span class="dm-rk">${i + 1}</span>
            <span class="dm-rk-n" title="${a.name}">${a.name}</span>
            <span class="dm-rk-bar" title="${fmtSize(a.size_bytes)}"><span class="dm-rk-fill" style="display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,#2f8a74,var(--brand,#176b5b));width:${Math.max(((a.size_bytes || 0) / maxSize * 100), 3)}%;"></span></span>
            <span class="dm-rk-size">${fmtSize(a.size_bytes)}</span>
          </div>`).join('')
        : '<div style="color:var(--muted);font-size:12px;padding:8px 0;">暂无数据，请先扫描</div>';
    }

    /** 渲染趋势图例（DB/向量/日志/缓存 分类色块），幂等。 */
    _renderLegend() {
      const el = document.getElementById('dmChartLegend');
      if (!el || el.dataset.rendered) return;
      const defs = [['#176b5b', 'DB'], ['#1d4ed8', '向量'], ['#b45309', '日志'], ['#94a3b8', '缓存']];
      el.innerHTML = defs.map(([c, n]) =>
        `<span class="dm-legend-item"><span class="dm-legend-dot" style="background:${c};"></span>${n}</span>`).join('');
      el.dataset.rendered = '1';
    }

    /**
     * 趋势图：单实例。首次建 Chart，后续仅 update 数据；
     * 仅 1 个日期点（今天）时也显示数据点并支持悬停。
     */
    async _loadTrend() {
      const canvas = document.getElementById('dmTrend');
      if (!window.Chart || !canvas) return;
      this._renderLegend();
      let rows;
      try {
        const res = await fetch('/api/data-monitor/trend?days=30');
        rows = await res.json();
      } catch (e) { console.error('加载趋势失败:', e); return; }
      const byDay = {};
      for (const r of rows) {
        byDay[r.d] = byDay[r.d] || { db: null, vector: null, log: null, cache: null };
        byDay[r.d][r.category] = (r.size_bytes || 0) / 1e6;
      }
      const labels = Object.keys(byDay).sort();
      this._applyHeroDeltas(rows);
      const single = labels.length <= 1;
      // 对数轴：DB 与日志/缓存差几十~几百倍时，线性轴会挤扁小值；log 让多量级差异都可见。
      // 0/负值在 log 轴上无意义，转 null（Chart.js 跳过该点，不连线）。
      const series = k => labels.map(d => {
        const v = byDay[d][k];
        return (v == null || v <= 0) ? null : v;
      });
      const datasets = [
        { label: 'DB', data: series('db'), borderColor: '#176b5b', backgroundColor: 'rgba(23,107,91,.08)', fill: true, tension: .35, pointRadius: single ? 5 : 0, pointHoverRadius: 6 },
        { label: '向量', data: series('vector'), borderColor: '#1d4ed8', fill: false, tension: .35, pointRadius: single ? 4 : 0, pointHoverRadius: 6 },
        { label: '日志', data: series('log'), borderColor: '#b45309', fill: false, tension: .35, pointRadius: single ? 4 : 0, pointHoverRadius: 6 },
        { label: '缓存', data: series('cache'), borderColor: '#94a3b8', fill: false, tension: .35, pointRadius: single ? 4 : 0, pointHoverRadius: 6 },
      ];
      const opts = {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 10, font: { size: 9 } } },
          y: { type: 'logarithmic', grid: { color: '#eef3f0' }, ticks: { font: { size: 9 } } },
        },
      };
      if (!this._chart) {
        const ctx = canvas.getContext('2d');
        this._chart = new Chart(ctx, { type: 'line', data: { labels, datasets }, options: opts });
      } else {
        this._chart.data.labels = labels;
        this._chart.data.datasets.forEach((ds, i) => { ds.data = datasets[i].data; });
        this._chart.update();
      }
    }

    /** 依据趋势数据计算各分类最近两天的增减量，刷新总览卡片上的 delta 徽章。 */
    _applyHeroDeltas(rows) {
      // rows: [{d, category, size_bytes, record_count}]（按 d 升序）
      const lastTwo = {};
      for (const r of rows) {
        const arr = lastTwo[r.category] || (lastTwo[r.category] = []);
        arr.push({ d: r.d, size: r.size_bytes || 0 });
      }
      const cards = document.querySelectorAll('.dm-hero-card');
      cards.forEach(card => {
        const cat = card.dataset.cat;
        const seq = (lastTwo[cat] || []).slice(-2);
        const span = card.querySelector('.dm-delta');
        if (!span) return;
        if (seq.length < 2) {
          span.textContent = '持平';
          span.className = 'dm-delta flat';
          return;
        }
        const prev = seq[0].size, cur = seq[1].size;
        const diff = cur - prev;
        if (Math.abs(diff) < 1024) {
          span.textContent = '持平';
          span.className = 'dm-delta flat';
          return;
        }
        span.textContent = (diff > 0 ? '+' : '-') + fmtSize(Math.abs(diff));
        span.className = 'dm-delta ' + (diff > 0 ? 'up' : 'down');
      });
    }

    async loadClean() {
      try {
        const res = await fetch('/api/data-monitor/cleanup/preview');
        this._clean = await res.json();
        this._renderClean();
      } catch (e) { console.error('加载可清理项失败:', e); }
    }

    _renderClean() {
      const el = document.getElementById('dmClean');
      if (!el) return;
      const scanTime = this._lastScanTime || '';
      el.innerHTML = this._clean.map(it => {
        const unit = it.tag === '文件' ? '个' : '条';
        const metaCount = `${(it.count || 0).toLocaleString()} ${unit}` + (scanTime ? ` · 上次扫描 ${scanTime}` : '');
        return `<div class="dm-clean-item">
          <input type="checkbox" data-id="${it.id}" ${(it.count || 0) === 0 ? 'disabled' : ''}>
          <div class="dm-ci-main">
            <div class="dm-ci-title">${it.name} <span class="dm-rule-tag">${it.tag || ''}</span></div>
            <div class="dm-ci-desc">${it.desc || ''}</div>
            <div class="dm-ci-meta">${metaCount}</div>
          </div>
          <div class="dm-ci-free">${it.size > 0 ? fmtSize(it.size) : '—'}</div>
        </div>`;
      }).join('');
      el.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        cb.addEventListener('change', () => {
          cb.closest('.dm-clean-item').classList.toggle('checked', cb.checked);
          this._updateCleanFooter();
        });
      });
      this._updateCleanFooter();
    }

    _updateCleanFooter() {
      let total = 0;
      this._clean.forEach((it, idx) => {
        const cb = document.querySelectorAll('#dmClean input[type="checkbox"]')[idx];
        if (cb && cb.checked) total += it.size || 0;
      });
      document.getElementById('dmFree').textContent = fmtSize(total);
      document.getElementById('dmBtnClean').disabled = total === 0;
    }

    async runClean() {
      const ids = [];
      document.querySelectorAll('#dmClean input[type="checkbox"]:not(:disabled):checked').forEach(cb => ids.push(cb.dataset.id));
      if (!ids.length) return;
      const total = ids.reduce((s, id) => s + (this._clean.find(x => x.id === id)?.size || 0), 0);
      if (!confirm(`确认执行清理 ${ids.length} 条规则？预计释放 ${fmtSize(total)}。此操作不可撤销。`)) return;
      try {
        await fetch('/api/data-monitor/cleanup/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rule_ids: ids }) });
        this.scan();
      } catch (e) { alert('清理失败: ' + (e.message || e)); }
    }
  }

  window._storageMonitor = null;
  window.initStorageMonitor = function () {
    if (!window._storageMonitor) window._storageMonitor = new StorageMonitor();
    return window._storageMonitor;
  };
})();