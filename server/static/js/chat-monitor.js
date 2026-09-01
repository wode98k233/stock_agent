/**
 * 系统监控面板
 *
 * 两个 Tab：
 *   - 快照：手动刷新，调用 GET /api/system/health
 *   - 实时：SSE 连接，自动推送指标
 *
 * 监控指标：
 *   系统级：CPU、内存、磁盘、网络 I/O、进程（RSS/VMS/线程）
 *   应用级：Agent 任务、数据库文件大小与连接、GC 统计、打开文件数
 *
 * 挂载到 window.StockRadar.monitorPanel
 */
(function () {
  'use strict';

  var Panel = {
    _activeTab: 'snapshot',
    _es: null,
    _rendered: false,

    // ── 公共 API ──

    show: function () {
      this._ensureDOM();
      this._overlay.classList.add('visible');
      this._panel.classList.add('visible');
      if (this._activeTab === 'snapshot') {
        this._refreshSnapshot();
      } else {
        this._startLive();
      }
    },

    hide: function () {
      this._stopLive();
      if (this._overlay) this._overlay.classList.remove('visible');
      if (this._panel) this._panel.classList.remove('visible');
    },

    toggle: function () {
      if (this._panel && this._panel.classList.contains('visible')) {
        this.hide();
      } else {
        this.show();
      }
    },

    // ── DOM 构建 ──

    _ensureDOM: function () {
      if (this._rendered) return;
      var body = document.body;

      this._overlay = document.createElement('div');
      this._overlay.className = 'monitor-overlay';
      this._overlay.addEventListener('click', this.hide.bind(this));
      body.appendChild(this._overlay);

      this._panel = document.createElement('aside');
      this._panel.className = 'monitor-panel';
      this._panel.setAttribute('aria-label', '系统监控');
      this._panel.innerHTML = this._renderHTML();
      body.appendChild(this._panel);

      var self = this;
      this._panel.querySelector('.monitor-close-btn').addEventListener('click', function () { self.hide(); });
      this._panel.querySelector('[data-tab="snapshot"]').addEventListener('click', function () { self._switchTab('snapshot'); });
      this._panel.querySelector('[data-tab="live"]').addEventListener('click', function () { self._switchTab('live'); });
      this._panel.querySelector('.monitor-refresh-btn').addEventListener('click', function () {
        if (self._activeTab === 'snapshot') {
          self._refreshSnapshot();
        } else {
          self._stopLive();
          self._startLive();
        }
      });

      this._rendered = true;
    },

    _renderHTML: function () {
      return '<div class="monitor-header">'
        + '<div class="monitor-title">'
        + '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 20V10"/><path d="M12 20V4"/><path d="M6 20v-4"/><circle cx="18" cy="10" r="1.5" fill="currentColor" opacity="0.6"/><circle cx="12" cy="4" r="1.5" fill="currentColor" opacity="0.6"/></svg>'
        + '系统监控'
        + '</div>'
        + '<button class="monitor-close-btn" type="button" aria-label="关闭">&times;</button>'
        + '</div>'

        + '<div class="monitor-tabs">'
        + '<button class="monitor-tab active" data-tab="snapshot">快照</button>'
        + '<button class="monitor-tab" data-tab="live">实时</button>'
        + '</div>'

        + '<div class="monitor-body" id="monitorBody">'
        + '<div style="text-align:center;padding:40px 0;color:var(--muted);">加载中...</div>'
        + '</div>'

        + '<div class="monitor-footer">'
        + '<span class="monitor-footer-status">'
        + '<span class="monitor-dot stale" id="monitorDot"></span>'
        + '<span id="monitorStatus">等待采集</span>'
        + '</span>'
        + '<button class="monitor-refresh-btn" type="button">'
        + '<span>↻</span> <span id="monitorRefreshLabel">刷新</span>'
        + '</button>'
        + '</div>';
    },

    // ── Tab 切换 ──

    _switchTab: function (tab) {
      if (this._activeTab === tab) return;
      this._activeTab = tab;

      var tabs = this._panel.querySelectorAll('.monitor-tab');
      for (var i = 0; i < tabs.length; i++) {
        tabs[i].classList.toggle('active', tabs[i].dataset.tab === tab);
      }

      this._stopLive();

      var label = this._panel.querySelector('#monitorRefreshLabel');
      if (label) label.textContent = tab === 'live' ? '重连' : '刷新';

      if (tab === 'snapshot') {
        this._setStatus('stale', '就绪');
        this._refreshSnapshot();
      } else {
        this._startLive();
      }
    },

    // ── 快照模式 ──

    _refreshSnapshot: function () {
      var body = this._panel.querySelector('#monitorBody');
      if (!body) return;
      body.innerHTML = '<div style="text-align:center;padding:40px 0;color:var(--muted);">采集中...</div>';
      this._setStatus('stale', '采集中...');

      var self = this;
      fetch('/api/system/health')
        .then(function (resp) {
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          return resp.json();
        })
        .then(function (data) {
          self._renderSnapshot(data);
          self._setStatus('stale', '快照完成');
        })
        .catch(function (err) {
          body.innerHTML = '<div style="text-align:center;padding:40px 0;color:var(--error);">采集失败: ' + self._esc(err.message) + '</div>';
          self._setStatus('error', '采集失败');
        });
    },

    _renderSnapshot: function (data) {
      var body = this._panel.querySelector('#monitorBody');
      if (!body) return;

      var note = data._note ? '<div style="font-size:11px;color:var(--warn);margin-bottom:8px;">⚠ ' + this._esc(data._note) + '</div>' : '';

      var html = note
        + this._cardApp(data.app)
        + this._cardCPU(data.cpu)
        + this._cardMemory(data.memory)
        + this._cardProcess(data.process)
        + this._cardNetwork(data.network)
        + this._cardDisk(data.disk)
        + this._cardDB(data.db)
        + this._cardOpenFiles(data.open_files)
        + this._cardGC(data.gc)
        + '<div class="monitor-updated">采集时间: ' + new Date(data.timestamp * 1000).toLocaleTimeString('zh-CN') + ' | 运行: ' + this._fmtUptime(data.uptime_seconds) + '</div>';

      body.innerHTML = html;
    },

    // ── 实时模式 (SSE) ──

    _startLive: function () {
      var body = this._panel.querySelector('#monitorBody');
      if (!body) return;
      body.innerHTML = '<div style="text-align:center;padding:40px 0;color:var(--muted);">连接中...</div>';
      this._setStatus('stale', '连接中...');

      var self = this;
      this._es = new EventSource('/api/system/monitor/events');

      this._es.addEventListener('snapshot', function (e) {
        try {
          var data = JSON.parse(e.data);
          self._renderSnapshot(data);
          self._setStatus('live', '实时监控中 · ' + new Date(data.timestamp * 1000).toLocaleTimeString('zh-CN'));
        } catch (_) { /* ignore */ }
      });

      this._es.addEventListener('heartbeat', function () { /* 保活 */ });

      this._es.onerror = function () {
        self._stopLive();
        self._setStatus('error', '连接断开');
        var el = body.querySelector('.monitor-updated');
        if (el) el.textContent += ' | 连接已断开';
      };
    },

    _stopLive: function () {
      if (this._es) {
        this._es.close();
        this._es = null;
      }
    },

    // ── 卡片渲染 ──

    _cardApp: function (app) {
      if (!app) return '';
      var running = app.running || 0;
      var queued = app.queued || 0;
      var failed = app.failed || 0;
      if (running === 0 && queued === 0 && failed === 0) {
        return this._card('Agent 任务', '', '<span style="font-size:12px;color:var(--muted);">当前无活跃任务</span>');
      }
      var rows = '';
      if (running > 0) rows += '<div class="monitor-item"><span class="monitor-item-label">运行中</span><span class="monitor-item-value ok">' + running + '</span></div>';
      if (queued > 0) rows += '<div class="monitor-item"><span class="monitor-item-label">排队中</span><span class="monitor-item-value" style="color:var(--warn);">' + queued + '</span></div>';
      if (failed > 0) rows += '<div class="monitor-item"><span class="monitor-item-label">失败</span><span class="monitor-item-value danger">' + failed + '</span></div>';
      return this._card('Agent 任务', '', '<div class="monitor-grid">' + rows + '</div>');
    },

    _cardCPU: function (cpu) {
      if (cpu.error) return this._card('CPU', '', cpu.error);
      var pct = cpu.percent != null ? cpu.percent : 0;
      var barClass = pct > 80 ? 'danger' : pct > 50 ? 'warn' : 'ok';
      return this._card('CPU', '', ''
        + '<div style="font-size:24px;font-weight:700;color:var(--text);">' + pct.toFixed(1) + '<span style="font-size:14px;color:var(--text-secondary);font-weight:500;">%</span></div>'
        + '<div class="monitor-bar-wrap"><div class="monitor-bar ' + barClass + '" style="width:' + Math.min(pct, 100) + '%"></div></div>'
        + '<div style="font-size:11px;margin-top:4px;color:var(--text-secondary);">' + (cpu.count || '-') + ' 核心</div>'
      );
    },

    _cardMemory: function (mem) {
      if (mem.error) return this._card('内存', '', mem.error);
      var pct = mem.percent != null ? mem.percent : 0;
      var barClass = pct > 85 ? 'danger' : pct > 60 ? 'warn' : 'ok';
      var total = mem.total_gb != null ? mem.total_gb : 0;
      var avail = mem.available_gb != null ? mem.available_gb : 0;
      return this._card('内存', '', ''
        + '<div class="monitor-grid">'
        + '<div class="monitor-item"><span class="monitor-item-label">使用率</span><span class="monitor-item-value">' + pct.toFixed(1) + '%</span></div>'
        + '<div class="monitor-item"><span class="monitor-item-label">可用</span><span class="monitor-item-value">' + avail.toFixed(1) + ' GB</span></div>'
        + '<div class="monitor-item"><span class="monitor-item-label">总量</span><span class="monitor-item-value">' + total.toFixed(1) + ' GB</span></div>'
        + '<div class="monitor-item"><span class="monitor-item-label">已用</span><span class="monitor-item-value">' + (total - avail).toFixed(1) + ' GB</span></div>'
        + '</div>'
        + '<div class="monitor-bar-wrap"><div class="monitor-bar ' + barClass + '" style="width:' + Math.min(pct, 100) + '%"></div></div>'
      );
    },

    _cardProcess: function (proc) {
      if (proc.error) return this._card('进程', '', proc.error);
      var rss = proc.rss_mb != null ? proc.rss_mb : '-';
      var vms = proc.vms_mb != null ? proc.vms_mb : '-';
      var threads = proc.threads != null ? proc.threads : '-';
      var cpuPct = proc.cpu_percent != null ? proc.cpu_percent : '-';
      return this._card('进程', '', ''
        + '<div class="monitor-grid">'
        + '<div class="monitor-item"><span class="monitor-item-label">PID</span><span class="monitor-item-value">' + (proc.pid || '-') + '</span></div>'
        + '<div class="monitor-item"><span class="monitor-item-label">线程</span><span class="monitor-item-value">' + threads + '</span></div>'
        + '<div class="monitor-item"><span class="monitor-item-label">进程 CPU</span><span class="monitor-item-value">' + (typeof cpuPct === 'number' ? cpuPct.toFixed(1) + '%' : cpuPct) + '</span></div>'
        + '<div class="monitor-item"><span class="monitor-item-label">RSS</span><span class="monitor-item-value">' + (typeof rss === 'number' ? rss.toFixed(1) + ' MB' : rss) + '</span></div>'
        + '<div class="monitor-item" style="grid-column:1/-1"><span class="monitor-item-label">VMS</span><span class="monitor-item-value">' + (typeof vms === 'number' ? vms.toFixed(0) + ' MB' : vms) + '</span></div>'
        + '</div>'
      );
    },

    _cardNetwork: function (net) {
      if (net.error) return this._card('网络 I/O', '', net.error);
      var sent = net.sent_mb != null ? net.sent_mb : 0;
      var recv = net.recv_mb != null ? net.recv_mb : 0;
      return this._card('网络 I/O', '', ''
        + '<div class="monitor-grid">'
        + '<div class="monitor-item"><span class="monitor-item-label">发送</span><span class="monitor-item-value">' + sent.toFixed(1) + ' MB</span></div>'
        + '<div class="monitor-item"><span class="monitor-item-label">接收</span><span class="monitor-item-value">' + recv.toFixed(1) + ' MB</span></div>'
        + '</div>'
        + '<div style="font-size:10px;color:var(--muted);margin-top:4px;">系统级累计（含所有进程）</div>'
      );
    },

    _cardDisk: function (disk) {
      if (disk.error) return this._card('磁盘', '', disk.error);
      var pct = disk.percent != null ? disk.percent : 0;
      var barClass = pct > 90 ? 'danger' : pct > 70 ? 'warn' : 'ok';
      var free = disk.free_gb != null ? disk.free_gb : 0;
      var total = disk.total_gb != null ? disk.total_gb : 0;
      var path = disk.path || '-';
      return this._card('磁盘', '', ''
        + '<div class="monitor-grid">'
        + '<div class="monitor-item"><span class="monitor-item-label">空闲</span><span class="monitor-item-value">' + free.toFixed(1) + ' GB</span></div>'
        + '<div class="monitor-item"><span class="monitor-item-label">总量</span><span class="monitor-item-value">' + total.toFixed(1) + ' GB</span></div>'
        + '</div>'
        + '<div class="monitor-bar-wrap"><div class="monitor-bar ' + barClass + '" style="width:' + Math.min(pct, 100) + '%"></div></div>'
        + '<div style="font-size:10px;color:var(--muted);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + this._esc(path) + ' · ' + pct + '%</div>'
      );
    },

    _cardDB: function (db) {
      if (!db || !db.databases) return '';
      var dbs = db.databases || [];
      if (dbs.length === 0) return this._card('数据库', '0 MB', '<span style="font-size:12px;color:var(--muted);">未检测到数据库文件</span>');
      var total = db.total_mb != null ? db.total_mb : 0;
      var rows = '';
      for (var i = 0; i < dbs.length; i++) {
        var d = dbs[i];
        var icon = d.status === 'ok' ? '<span style="color:var(--success);">●</span>' : '<span style="color:var(--error);">●</span>';
        rows += '<div class="monitor-item" style="grid-column:1/-1;flex-direction:row;justify-content:space-between;align-items:center;">'
          + '<span class="monitor-item-label">' + icon + ' ' + this._esc(d.name) + '</span>'
          + '<span class="monitor-item-value" style="font-size:11px;">' + d.size_mb + ' MB</span>'
          + '</div>';
      }
      return this._card('数据库', total.toFixed(1) + ' MB', '<div class="monitor-grid" style="gap:4px;">' + rows + '</div>');
    },

    _cardOpenFiles: function (files) {
      if (files.error) return '';
      var count = files.count != null ? files.count : '-';
      var cls = count > 500 ? 'danger' : count > 200 ? 'warn' : 'ok';
      return this._card('打开文件数', '', '<span class="monitor-item-value ' + cls + '" style="font-size:18px;">' + count + '</span>');
    },

    _cardGC: function (gc) {
      if (!gc || gc.error) return '';
      if (!gc.generations) return '';
      var rows = '';
      for (var i = 0; i < gc.generations.length; i++) {
        var g = gc.generations[i];
        rows += '<div class="monitor-item"><span class="monitor-item-label">Gen ' + g.generation + '</span><span class="monitor-item-value">' + g.collections + ' 次 / ' + (g.collected + g.uncollectable) + ' 对象</span></div>';
      }
      var badge = gc.enabled ? '<span class="monitor-badge ok">启用</span>' : '<span class="monitor-badge warn">禁用</span>';
      return this._card('GC', badge, '<div class="monitor-grid">' + rows + '</div>');
    },

    _card: function (title, badge, bodyHTML) {
      return '<div class="monitor-card">'
        + '<div class="monitor-card-header">'
        + '<span class="monitor-card-label">' + this._esc(title) + '</span>'
        + (badge ? '<span style="font-size:11px;color:var(--text-secondary);">' + badge + '</span>' : '')
        + '</div>'
        + bodyHTML
        + '</div>';
    },

    // ── 辅助 ──

    _setStatus: function (dotClass, text) {
      var dot = this._panel && this._panel.querySelector('#monitorDot');
      var status = this._panel && this._panel.querySelector('#monitorStatus');
      if (dot) { dot.className = 'monitor-dot ' + dotClass; }
      if (status) { status.textContent = text; }
    },

    _fmtUptime: function (seconds) {
      if (seconds == null || isNaN(seconds)) return '-';
      var h = Math.floor(seconds / 3600);
      var m = Math.floor((seconds % 3600) / 60);
      var s = Math.floor(seconds % 60);
      if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
      if (m > 0) return m + 'm ' + s + 's';
      return s + 's';
    },

    _esc: function (s) {
      var div = document.createElement('div');
      div.appendChild(document.createTextNode(String(s)));
      return div.innerHTML;
    },
  };

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.monitorPanel = Panel;
})();
