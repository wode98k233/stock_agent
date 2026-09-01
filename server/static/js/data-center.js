/**
 * 数据中心面板 — 对齐原型 docs/prototypes/data-center.html
 */
(function () {
  'use strict';

  class DataCenterPanel {
    constructor() {
      this.overlay = document.getElementById('dcOverlay');
      this._bindEvents();
    }

    _bindEvents() {
      // Tab 切换
      document.querySelectorAll('.dc-tab').forEach(tab => {
        tab.addEventListener('click', () => {
          document.querySelectorAll('.dc-tab').forEach(t => t.classList.remove('active'));
          document.querySelectorAll('.dc-panel').forEach(p => p.classList.remove('active'));
          tab.classList.add('active');
          document.getElementById(tab.dataset.tab)?.classList.add('active');
          // 切换到概览时加载统计和指数列表
          if (tab.dataset.tab === 'dcOverview') {
            this._loadStats();
            this._loadIndices();
          }
          // 切换到数据总览时刷新树
          if (tab.dataset.tab === 'dcBrowse') {
            this._loadBrowseTree();
          }
        });
      });

      // 任务类型选择
      document.querySelectorAll('.dc-task-type').forEach(card => {
        card.addEventListener('click', () => {
          document.querySelectorAll('.dc-task-type').forEach(c => c.classList.remove('selected'));
          card.classList.add('selected');
          // 显示/隐藏配置选项
          const type = card.dataset.type;
          const klineOpts = document.getElementById('dcKlineOptions');
          const sourceOpts = document.getElementById('dcSourceOptions');
          const boardTypeOpts = document.getElementById('dcBoardTypeOptions');
          const boardOpts = document.getElementById('dcBoardOptions');
          // 股票代码+天数：日K线/指数/复权因子
          if (klineOpts) klineOpts.style.display = ['daily_kline', 'index_kline', 'adjust_factor', 'hithink_sync'].includes(type) ? 'block' : 'none';
          // 数据源选择：所有数据采集任务
          if (sourceOpts) sourceOpts.style.display = !['full_sync', 'hithink_sync'].includes(type) ? 'block' : 'none';
          // 板块类型选择：板块列表
          if (boardTypeOpts) boardTypeOpts.style.display = type === 'board_list' ? 'block' : 'none';
          // 板块多选：板块成分/板块K线
          if (boardOpts) {
            const showBoard = ['board_member', 'board_kline'].includes(type);
            boardOpts.style.display = showBoard ? 'block' : 'none';
            if (showBoard) this._loadBoardList();
          }
        });
      });
    }

    async show() {
      if (this.overlay) this.overlay.classList.add('visible');
      await this._loadBrowseTree();
      await this._loadTasks();
      // 自动刷新：每 5 秒刷新任务列表
      this._stopAutoRefresh();
      this._refreshTimer = setInterval(() => {
        const tasksPanel = document.getElementById('dcTasks');
        if (tasksPanel?.classList.contains('active')) this._loadTasks();
      }, 5000);
    }

    hide() {
      if (this.overlay) this.overlay.classList.remove('visible');
      this._stopAutoRefresh();
      this._closeAllSSE();
    }

    _stopAutoRefresh() {
      if (this._refreshTimer) { clearInterval(this._refreshTimer); this._refreshTimer = null; }
    }

    // ========== 数据总览 ==========

    async _loadBrowseTree() {
      try {
        const res = await fetch('/api/data/browse');
        const data = await res.json();
        const tree = data.tree || [];
        const el = document.getElementById('dcBrowseTree');
        if (!el) return;

        el.innerHTML = tree.map(node => {
          const icon = { stock: '📈', etf: '📦', index: '📊', board: '🗂️' }[node.id] || '📁';
          let html = `<div class="dc-tree-node" data-id="${node.id}" onclick="_dcPanel._selectBrowseNode('${node.id}')" style="cursor:pointer;padding:6px 8px;border-radius:6px;margin-bottom:2px;display:flex;align-items:center;gap:6px;">
            <span>${icon}</span>
            <span style="flex:1;font-weight:500;">${node.label}</span>
            <span style="font-size:11px;color:var(--muted);">${node.count || 0}</span>
          </div>`;
          if (node.children?.length > 0) {
            html += `<div style="padding-left:20px;">`;
            html += node.children.map(c =>
              `<div class="dc-tree-node" data-id="${c.id}" onclick="_dcPanel._selectBrowseNode('${c.id}')" style="cursor:pointer;padding:4px 8px;border-radius:6px;margin-bottom:1px;font-size:12px;display:flex;align-items:center;gap:6px;">
                <span style="flex:1;">${c.label}</span>
                <span style="font-size:11px;color:var(--muted);">${c.count || 0}</span>
              </div>`
            ).join('');
            html += `</div>`;
          }
          return html;
        }).join('');
      } catch (e) { console.error('加载数据总览失败:', e); }
    }

    async _selectBrowseNode(id) {
      // 高亮选中
      document.querySelectorAll('.dc-tree-node').forEach(n => n.style.background = '');
      const node = document.querySelector(`.dc-tree-node[data-id="${id}"]`);
      if (node) node.style.background = 'var(--brand-bg)';

      const title = document.getElementById('dcBrowseTitle');
      if (title) title.textContent = { stock_sh: '上证A股', stock_sz: '深证A股', stock: '沪深A股', etf: 'ETF', index: '指数', board_industry: '行业板块', board_concept: '概念板块', board: '板块' }[id] || id;

      try {
        const res = await fetch(`/api/data/browse/${id}?limit=200`);
        const data = await res.json();
        const items = data.items || [];
        const el = document.getElementById('dcBrowseList');
        if (!el) return;

        if (items.length === 0) {
          el.innerHTML = '<div style="text-align:center;color:var(--muted);padding:40px;">暂无数据，请先采集</div>';
          return;
        }

        el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px;">
          <thead><tr style="border-bottom:1px solid var(--border);text-align:left;">
            <th style="padding:8px 6px;">代码</th><th style="padding:8px 6px;">名称</th><th style="padding:8px 6px;">数据条数</th><th style="padding:8px 6px;">最新日期</th><th style="padding:8px 6px;">操作</th>
          </tr></thead>
          <tbody>${items.map(item => {
            const code = item.code || '';
            const name = item.name || '';
            const dataCount = item.data_count || 0;
            const latest = item.latest_date || '—';
            const isBoard = id.startsWith('board');
            const assetType = isBoard ? 'board' : (id === 'etf' ? 'etf' : 'stock');
            return `<tr style="border-bottom:1px solid var(--border-light);">
              <td style="padding:6px;font-family:var(--font-mono);">${code}</td>
              <td style="padding:6px;">${name}</td>
              <td style="padding:6px;">${dataCount.toLocaleString()}</td>
              <td style="padding:6px;">${latest}</td>
              <td style="padding:6px;">
                <button class="dc-btn dc-btn-sm" onclick="_dcPanel._browseToBacktest('${code}','${assetType}')">回测</button>
                <button class="dc-btn dc-btn-sm" onclick="_dcPanel._browseToKline('${code}')">K线</button>
                <button class="dc-btn dc-btn-sm" onclick="_dcPanel._browseToNews('${code}','${name}')">新闻</button>
              </td>
            </tr>`;
          }).join('')}</tbody>
        </table>`;
      } catch (e) { console.error('加载列表失败:', e); }
    }

    _browseToBacktest(code, assetType) {
      // 关闭数据中心，打开回测面板，填入代码和资产类型
      this.hide();
      const btOverlay = document.getElementById('backtestOverlay');
      if (btOverlay) btOverlay.classList.add('visible');
      const codeInput = document.getElementById('btInputCode');
      if (codeInput) codeInput.value = code;
      const assetSelect = document.getElementById('btAssetType');
      if (assetSelect) assetSelect.value = assetType;
    }

    _browseToKline(code) {
      // 关闭数据中心，打开K线图（如果有全局函数）
      this.hide();
      if (window.StockRadar?.openKline) {
        window.StockRadar.openKline(code);
      } else {
        console.log('K线图功能:', code);
      }
    }

    _browseToNews(code, name) {
      // 关闭数据中心，打开新闻面板，加载个股新闻
      this.hide();
      window.StockRadar?.newsPanel?.showStockNews(code, name);
    }

    async _loadStats() {
      try {
        const stats = await window.StockRadar.api.getDataStats();
        const dr = stats.date_range || {};

        // 统计卡片
        const el = document.getElementById('dcStats');
        if (el) {
          el.innerHTML = `
            <div class="dc-stat highlight"><div class="label">股票数量</div><div class="value">${(stats.stock_count || 0).toLocaleString()}</div><div class="sub">覆盖沪深A股</div></div>
            <div class="dc-stat"><div class="label">日K线记录</div><div class="value">${this._formatNum(stats.daily_count || 0)}</div></div>
            <div class="dc-stat"><div class="label">数据覆盖</div><div class="value" style="font-size:16px">${dr.min || '—'} ~ ${dr.max || '—'}</div></div>
            <div class="dc-stat"><div class="label">复权因子</div><div class="value">${(stats.adjust_factor_count || 0).toLocaleString()}</div></div>
            <div class="dc-stat"><div class="label">回测策略</div><div class="value">${stats.backtest?.strategies || 0}</div></div>
            <div class="dc-stat"><div class="label">回测记录</div><div class="value">${stats.backtest?.backtest_runs || 0}</div></div>`;
        }

        // 数据库表概览
        const tablesEl = document.getElementById('dcTableOverview');
        if (tablesEl && stats.tables?.length > 0) {
          tablesEl.innerHTML = `<table class="dc-table">
            <thead><tr><th>表名</th><th>记录数</th><th>覆盖范围</th><th>最后更新</th><th>数据源</th><th>状态</th></tr></thead>
            <tbody>${stats.tables.map(t => `<tr>
              <td><code style="font-family:var(--font-mono);font-size:12px">${t.name}</code></td>
              <td>${(t.count || 0).toLocaleString()}</td>
              <td>${t.range || '—'}</td>
              <td>${t.updated || '—'}</td>
              <td>${t.source || '—'}</td>
              <td><span class="dc-badge ${t.status === '完整' ? 'completed' : 'pending'}"><span class="dot"></span>${t.status}</span></td>
            </tr>`).join('')}</tbody></table>`;
        }

        // 增量采集状态
        const incEl = document.getElementById('dcIncremental');
        if (incEl && stats.incremental?.length > 0) {
          incEl.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;">
            ${stats.incremental.map(inc => {
              const isOk = inc.status === 'ok';
              const color = isOk ? 'var(--brand)' : 'var(--warning)';
              const label = isOk ? '已就绪' : '未开始';
              const width = isOk ? '100%' : '0%';
              return `<div style="border:1px solid var(--border);border-radius:var(--radius);padding:14px;">
                <div style="font-size:12px;font-weight:600;margin-bottom:8px;">${inc.name}</div>
                <div style="font-size:11px;color:var(--text-secondary);margin-bottom:4px;">已有数据: ${inc.existing}</div>
                <div style="font-size:11px;color:var(--text-secondary);margin-bottom:8px;">${inc.latest !== '—' ? '最新: ' + inc.latest : ''}</div>
                <div style="display:flex;align-items:center;gap:8px;">
                  <div style="flex:1;height:6px;background:var(--border);border-radius:3px;">
                    <div style="width:${width};height:100%;background:${color};border-radius:3px;"></div>
                  </div>
                  <span style="font-size:11px;font-weight:600;color:${color}">${label}</span>
                </div>
                <div style="font-size:10px;color:var(--muted);margin-top:4px;">${inc.message}</div>
              </div>`;
            }).join('')}
          </div>`;
        }

        // 数据质量
        const qEl = document.getElementById('dcQuality');
        if (qEl && stats.quality?.length > 0) {
          qEl.innerHTML = `<table class="dc-table">
            <thead><tr><th>检查项</th><th>状态</th><th>详情</th></tr></thead>
            <tbody>${stats.quality.map(q => {
              const cls = q.status === '正常' || q.status === '完整' ? 'completed' : q.status === '未采集' ? 'pending' : 'failed';
              return `<tr>
                <td>${q.item}</td>
                <td><span class="dc-badge ${cls}"><span class="dot"></span>${q.status}</span></td>
                <td style="font-size:12px;color:var(--text-secondary)">${q.detail}</td>
              </tr>`;
            }).join('')}</tbody></table>`;
        }
      } catch (e) { console.error('加载统计失败:', e); }
    }

    _formatNum(n) {
      if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
      if (n >= 1000) return (n / 1000).toFixed(0) + 'K';
      return n.toLocaleString();
    }

    async _loadIndices() {
      try {
        const res = await window.StockRadar.api.getDataIndices();
        const indices = res.indices || [];
        const el = document.getElementById('dcIndices');
        if (!el) return;
        if (indices.length === 0) {
          el.innerHTML = '<div style="color:var(--muted);padding:16px;">暂无指数数据</div>';
          return;
        }
        el.innerHTML = `<table class="dc-table">
          <thead><tr><th>代码</th><th>名称</th><th>分类</th><th>数据条数</th><th>最新日期</th><th>状态</th></tr></thead>
          <tbody>${indices.map(idx => {
            const status = idx.collected ? 'completed' : 'pending';
            const label = idx.collected ? '已采集' : '未采集';
            return `<tr>
              <td><code style="font-family:var(--font-mono);font-size:12px">${idx.code}</code></td>
              <td>${idx.name}</td>
              <td>${idx.category || '—'}</td>
              <td>${idx.collected ? idx.count.toLocaleString() : '—'}</td>
              <td>${idx.collected ? idx.latest : '—'}</td>
              <td><span class="dc-badge ${status}"><span class="dot"></span>${label}</span></td>
            </tr>`;
          }).join('')}</tbody></table>`;
      } catch (e) { console.error('加载指数列表失败:', e); }
    }

    async _loadBoardList() {
      const el = document.getElementById('dcBoardList');
      if (!el) return;
      // 如果已加载，不重复请求
      if (el.dataset.loaded === 'true') return;
      try {
        const res = await window.StockRadar.api.getDataStats();
        // 从数据库获取板块列表
        const boardRes = await fetch('/api/data/stats');
        const data = await boardRes.json();
        // 用 API 获取板块列表
        const boardsRes = await fetch('/api/data/boards');
        const boardsData = await boardsRes.json();
        const boards = boardsData.boards || [];
        if (boards.length === 0) {
          el.innerHTML = '<div style="color:var(--muted);font-size:12px;">暂无板块数据，请先采集板块列表</div>';
          return;
        }
        el.innerHTML = boards.map(b =>
          `<label style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border:1px solid var(--border);border-radius:100px;font-size:12px;cursor:pointer;user-select:none;">
            <input type="checkbox" value="${b.board_name}" data-type="${b.board_type}" style="accent-color:var(--brand);">
            <span>${b.board_name}</span>
          </label>`
        ).join('');
        el.dataset.loaded = 'true';
      } catch (e) {
        el.innerHTML = '<div style="color:var(--muted);font-size:12px;">加载失败</div>';
      }
    }

    selectAllBoards() {
      document.querySelectorAll('#dcBoardList input[type="checkbox"]').forEach(cb => cb.checked = true);
    }

    deselectAllBoards() {
      document.querySelectorAll('#dcBoardList input[type="checkbox"]').forEach(cb => cb.checked = false);
    }

    _getSelectedBoards() {
      const checked = document.querySelectorAll('#dcBoardList input[type="checkbox"]:checked');
      return Array.from(checked).map(cb => [cb.value, cb.dataset.type]);
    }

    async _loadTasks() {
      try {
        const res = await window.StockRadar.api.getDataTasks(null, 20);
        const tasks = res.tasks || [];
        const tbody = document.getElementById('dcTaskList');
        if (!tbody) return;
        if (tasks.length === 0) {
          tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px;">暂无采集任务</td></tr>';
          return;
        }
        tbody.innerHTML = tasks.map(t => {
          const pct = t.total_count > 0 ? Math.round(t.processed_count / t.total_count * 100) : 0;
          const detail = t.status === 'running' ? `正在处理: ${t.current_code || ''} ${t.current_name || ''}` :
                         t.status === 'completed' ? `耗时 ${(t.duration_seconds || 0).toFixed(0)}s` :
                         t.status === 'failed' ? (t.error_message || '失败') :
                         t.status === 'paused' ? '已暂停 · 可恢复' : '';
          return `<tr data-task-id="${t.task_id}">
            <td><code style="font-family:var(--font-mono);font-size:12px">${t.task_id}</code></td>
            <td>${this._taskTypeName(t.task_type)}</td>
            <td><span class="dc-badge ${t.status}"><span class="dot"></span>${this._statusName(t.status)}</span></td>
            <td style="min-width:180px;"><div class="dc-task-progress" style="font-size:12px;color:var(--text-secondary)">${t.processed_count}/${t.total_count} · ${pct}%</div><div class="dc-progress"><div class="fill" style="width:${pct}%"></div></div></td>
            <td class="dc-task-detail" style="font-size:12px;color:var(--text-secondary)">${detail}</td>
            <td style="font-size:12px;color:var(--muted)">${t.created_at || ''}</td>
            <td>${this._taskActions(t)}</td>
          </tr>`;
        }).join('');

        // 对运行中的任务连接 SSE
        this._connectRunningTasks(tasks);
      } catch (e) { console.error('加载任务失败:', e); }
    }

    _connectRunningTasks(tasks) {
      // 按 task_id 持久化 SSE 连接，避免每次轮询全部重建（重复发起 /events 请求）
      if (!this._taskSSE) this._taskSSE = new Map();  // task_id -> EventSource
      const running = new Set(tasks.filter(t => t.status === 'running').map(t => t.task_id));

      // 关闭已不在运行中的连接
      for (const [id, es] of this._taskSSE) {
        if (!running.has(id)) { es.close(); this._taskSSE.delete(id); }
      }

      // 仅为新出现的运行中任务建立连接（已存在的复用，不重建）
      tasks.filter(t => t.status === 'running' && !this._taskSSE.has(t.task_id)).forEach(t => {
        try {
          const es = new EventSource(`/api/data/tasks/${t.task_id}/events`);
          es.onmessage = (event) => {
            try {
              const data = JSON.parse(event.data);
              if (data.task) this._updateTaskRow(data.task);
              const st = data.task && data.task.status;
              // 收到终态：主动关闭，避免浏览器对已结束的流自动重连
              if (data.type === 'final' || ['completed', 'failed', 'cancelled'].includes(st)) {
                es.close();
                this._taskSSE.delete(t.task_id);
                this._loadTasks();  // 刷新一次列表反映终态
              }
            } catch (e) { /* ignore */ }
          };
          es.onerror = () => { es.close(); this._taskSSE.delete(t.task_id); };
          this._taskSSE.set(t.task_id, es);
        } catch (e) { /* ignore */ }
      });
    }

    _closeAllSSE() {
      if (this._taskSSE) {
        for (const es of this._taskSSE.values()) {
          try { es.close(); } catch (e) { /* ignore */ }
        }
        this._taskSSE.clear();
      }
    }

    _updateTaskRow(task) {
      const row = document.querySelector(`tr[data-task-id="${task.task_id}"]`);
      if (!row) return;
      const pct = task.total_count > 0 ? Math.round(task.processed_count / task.total_count * 100) : 0;
      const progressEl = row.querySelector('.dc-task-progress');
      if (progressEl) progressEl.textContent = `${task.processed_count}/${task.total_count} · ${pct}%`;
      const fillEl = row.querySelector('.dc-progress .fill');
      if (fillEl) fillEl.style.width = pct + '%';
      const detailEl = row.querySelector('.dc-task-detail');
      if (detailEl && task.status === 'running') {
        detailEl.textContent = `正在处理: ${task.current_code || ''} ${task.current_name || ''}`;
      }
    }

    _taskTypeName(type) {
      const map = { stock_list: '股票列表', daily_kline: '日K线数据', index_kline: '指数日K线', adjust_factor: '复权因子', board_list: '板块列表', board_kline: '板块K线', board_member: '板块成分', full_sync: '全量同步', hithink_sync: '同花顺灌库' };
      return map[type] || type;
    }

    _statusName(status) {
      const map = { pending: '等待中', running: '运行中', paused: '已暂停', completed: '已完成', failed: '失败', cancelled: '已取消' };
      return map[status] || status;
    }

    _taskActions(t) {
      if (t.status === 'running') return `<div class="dc-btn-group"><button class="dc-btn dc-btn-sm" onclick="_dcPanel.pauseTask('${t.task_id}')">⏸ 暂停</button><button class="dc-btn dc-btn-sm dc-btn-danger" onclick="_dcPanel.cancelTask('${t.task_id}')">✕ 取消</button></div>`;
      if (t.status === 'paused') return `<div class="dc-btn-group"><button class="dc-btn dc-btn-sm" onclick="_dcPanel.resumeTask('${t.task_id}')">▶ 恢复</button><button class="dc-btn dc-btn-sm dc-btn-danger" onclick="_dcPanel.cancelTask('${t.task_id}')">✕ 取消</button></div>`;
      if (t.status === 'completed') return `<button class="dc-btn dc-btn-sm" onclick="_dcPanel.viewTask('${t.task_id}')">查看详情</button>`;
      if (t.status === 'failed') return `<div class="dc-btn-group"><button class="dc-btn dc-btn-sm" onclick="_dcPanel.retryTask('${t.task_id}')">↻ 重试</button><button class="dc-btn dc-btn-sm" onclick="_dcPanel.viewTask('${t.task_id}')">详情</button></div>`;
      return '';
    }

    async createTask() {
      const type = document.querySelector('.dc-task-type.selected')?.dataset.type;
      if (!type) { alert('请选择任务类型'); return; }
      const config = {};
      // 股票代码+天数：日K线/指数/复权因子
      if (['daily_kline', 'index_kline', 'adjust_factor'].includes(type)) {
        const codes = document.getElementById('dcInputCodes')?.value;
        const days = document.getElementById('dcInputDays')?.value;
        if (codes) config.codes = codes.split(',').map(s => s.trim()).filter(Boolean);
        config.days = parseInt(days) || 500;
      }
      // 数据源：所有任务类型
      const source = document.getElementById('dcInputSource')?.value;
      if (source && source !== 'auto') config.source = source;
      // 强制覆盖
      const force = document.getElementById('dcForceOverwrite')?.checked;
      if (force) config.force = true;

      // 板块类型：板块列表
      if (type === 'board_list') {
        const boardType = document.getElementById('dcBoardType')?.value;
        if (boardType && boardType !== 'all') config.board_type = boardType;
      }
      // 板块多选：板块成分/板块K线
      if (['board_member', 'board_kline'].includes(type)) {
        const selected = this._getSelectedBoards();
        if (selected.length > 0) config.boards = selected;
      }
      try {
        await window.StockRadar.api.createDataTask(type, config);
        this.hideCreatePanel();
        this._loadTasks();
      } catch (e) { alert('创建任务失败: ' + (e.message || e)); }
    }

    showCreatePanel() { const el = document.getElementById('dcCreatePanel'); if (el) el.style.display = 'block'; }
    hideCreatePanel() { const el = document.getElementById('dcCreatePanel'); if (el) el.style.display = 'none'; }

    async pauseTask(id) { await window.StockRadar.api.pauseDataTask(id); this._loadTasks(); }
    async resumeTask(id) { await window.StockRadar.api.resumeDataTask(id); this._loadTasks(); }
    async cancelTask(id) { if (!confirm('确定取消任务？')) return; await window.StockRadar.api.cancelDataTask(id); this._loadTasks(); }
    async retryTask(id) { alert('重试功能开发中'); }
    async viewTask(id) {
      try {
        const task = await window.StockRadar.api.getDataTask(id);
        this._showTaskDetail(task);
      } catch (e) { console.error(e); }
    }

    _showTaskDetail(task) {
      const el = document.getElementById('dcTaskDetail');
      if (!el) return;
      el.classList.add('visible');
      const info = el.querySelector('.dc-task-info-grid');
      if (info) {
        info.innerHTML = `
          <div><div class="label">任务类型</div><div class="value">${this._taskTypeName(task.task_type)}</div></div>
          <div><div class="label">数据源</div><div class="value">${task.source || 'auto'}</div></div>
          <div><div class="label">开始时间</div><div class="value">${task.started_at || '—'}</div></div>
          <div><div class="label">已处理</div><div class="value">${task.processed_count}/${task.total_count}</div></div>
          <div><div class="label">成功/失败</div><div class="value">${task.success_count || 0} / ${task.failed_count || 0}</div></div>
          <div><div class="label">耗时</div><div class="value">${task.duration_seconds ? task.duration_seconds.toFixed(1) + 's' : '—'}</div></div>`;
      }
      const log = el.querySelector('.dc-task-log');
      if (log && task.log_text) {
        log.innerHTML = task.log_text.split('\n').filter(Boolean).map(line => {
          // 着色: [INFO], [OK], [WARN], [ERROR]
          let colored = line
            .replace(/\[INFO\]/g, '<span class="log-info">[INFO]</span>')
            .replace(/\[OK\]/g, '<span class="log-success">[OK]</span>')
            .replace(/\[WARN\]/g, '<span class="log-warn">[WARN]</span>')
            .replace(/\[ERROR\]/g, '<span class="log-error">[ERROR]</span>');
          return `<div>${colored}</div>`;
        }).join('');
      }
    }

    closeDetail() { const el = document.getElementById('dcTaskDetail'); if (el) el.classList.remove('visible'); }
    refresh() { this._loadTasks(); this._loadStats(); }
  }

  window._dcPanel = null;
  window.initDataCenterPanel = function () {
    if (!window._dcPanel) window._dcPanel = new DataCenterPanel();
    return window._dcPanel;
  };
})();
