/**
 * Agent 群模式 — 悬浮对话面板
 *
 * 在 assistant 气泡的 action-bar 中添加「群聊」按钮（WeChat 风格图标），
 * 点击后弹出悬浮面板，展示调度 Agent 与子 Agent 的对话记录。
 * 对话气泡支持 markdown 渲染，与主对话风格统一。
 *
 * Agent 图标和名称从 /api/agents?mode=agent_group 动态获取，
 * 不硬编码，新增 agent 时自动生效。
 */
(function () {
  'use strict';

  // 默认图标（API 返回的 agent 没有 icon 字段时使用）
  const DEFAULT_ICONS = {
    technical_analyst: '📈',
    macro_analyst: '📊',
    risk_analyst: '⚠️',
    chain_analyst: '🔗',
  };

  // 缓存从 API 获取的 agent 信息
  let _agentCache = null;

  async function _loadAgents() {
    if (_agentCache) return _agentCache;
    try {
      const resp = await fetch('/api/agents?mode=agent_group');
      const data = await resp.json();
      _agentCache = {};
      for (const a of (data.sub_agents || [])) {
        _agentCache[a.name] = {
          icon: DEFAULT_ICONS[a.name] || '🔧',
          label: a.label || a.name,
          description: a.description || '',
          skills: a.skills || [],
        };
      }
      return _agentCache;
    } catch {
      return {};
    }
  }

  function getAgentIcon(agentName) {
    return (DEFAULT_ICONS[agentName]) || '🔧';
  }

  async function getAgentDisplayName(agentName) {
    const agents = await _loadAgents();
    if (agents[agentName]) return agents[agentName].label;
    return agentName;
  }

  // ── markdown 渲染（使用 marked.js，与主对话一致） ──

  function renderMarkdown(text) {
    if (!text) return '';
    if (typeof marked !== 'undefined') {
      try { return marked.parse(text); }
      catch { /* fallback */ }
    }
    // fallback：简单转义
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
  }

  function renderRequests(requests, agents) {
    if (!requests || requests.length === 0) return '';
    return requests
      .map(
        (r) => {
          const label = (agents && agents[r.target]) ? agents[r.target].label : r.target;
          return `<div class="group-request-badge">🔍 ${label}: ${r.description}</div>`;
        }
      )
      .join('');
  }

  // ── 渲染单条消息 ──

  function setButtonDone(button, className = 'copied', delay = 1500) {
    button.classList.add(className);
    setTimeout(() => button.classList.remove(className), delay);
  }

  function renderMessage(msg, agents) {
    const isDispatcher = msg.role === 'dispatcher';
    const agentInfo = agents && agents[msg.agent_name];
    const icon = isDispatcher ? '🤖' : (agentInfo ? agentInfo.icon : getAgentIcon(msg.agent_name));
    const name = isDispatcher ? '调度Agent' : (agentInfo ? agentInfo.label : (msg.agent_name || '未知'));

    let extraHtml = '';
    if (msg.extra) {
      try {
        const extra = typeof msg.extra === 'string' ? JSON.parse(msg.extra) : msg.extra;
        if (extra && extra.requests) {
          extraHtml = renderRequests(extra.requests, agents);
        }
      } catch {}
    }

    const div = document.createElement('div');
    div.className = `group-msg ${isDispatcher ? 'right' : 'left'}`;

    const contentHtml = renderMarkdown(msg.content);

    // 复制按钮（与主对话 msg-action-bar 风格统一）
    const actionBarHtml = `
      <div class="group-action-bar">
        <button class="group-copy-btn" title="复制文本">
          <svg class="icon-copy" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          <svg class="icon-check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        </button>
        <button class="group-copy-image-btn" title="复制为图片">
          <svg class="icon-image" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
          <svg class="icon-check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        </button>
        <div class="msg-export-wrap">
          <button class="group-export-btn" title="导出为图片">
            <svg class="icon-download" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <svg class="icon-check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          </button>
          <div class="msg-export-menu">
            <button class="msg-notify-menu-item" type="button" data-export-single>
              <span>导出单张图片</span>
              <small>整条消息一张高清 PNG（1080px）</small>
            </button>
            <button class="msg-notify-menu-item" type="button" data-export-sliced>
              <span>分割导出（9:16 竖屏）</span>
              <small>按段落/表格整块切分，多张存文件夹/zip</small>
            </button>
          </div>
        </div>
      </div>`;

    div.innerHTML = `
      <div class="group-avatar">${icon}</div>
      <div class="group-msg-content">
        <div class="group-bubble-header">
          <span class="group-bubble-name">${name}</span>
          <span class="group-bubble-type">${msg.msg_type}</span>
        </div>
        <div class="bubble md-content">${contentHtml}</div>
        ${extraHtml}
        ${actionBarHtml}
      </div>
    `;

    // 绑定复制事件
    const copyBtn = div.querySelector('.group-copy-btn');
    const copyImageBtn = div.querySelector('.group-copy-image-btn');
    const bubbleBody = div.querySelector('.bubble');

    if (copyBtn) {
      copyBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          await navigator.clipboard.writeText(msg.content || '');
          setButtonDone(copyBtn);
        } catch {}
      });
    }

    if (copyImageBtn && bubbleBody) {
      copyImageBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          if (window.StockRadar?.chatExport?.copyBubbleAsImage) {
            await window.StockRadar.chatExport.copyBubbleAsImage(bubbleBody);
          }
          setButtonDone(copyImageBtn);
        } catch (err) {
          console.error('图片复制失败:', err);
          setButtonDone(copyImageBtn, 'failed');
        }
      });
    }

    // 导出为图片（单张高清 / 内容感知分割）
    const exportBtn = div.querySelector('.group-export-btn');
    const exportMenu = div.querySelector('.msg-export-menu');
    if (exportBtn && exportMenu && bubbleBody) {
      // 全局关闭：点击任意位置收起导出菜单（幂等，只绑一次）
      if (!window.__groupExportCloserBound) {
        window.__groupExportCloserBound = true;
        document.addEventListener('click', () => {
          document.querySelectorAll('.msg-export-menu.open').forEach(m => m.classList.remove('open'));
        });
      }
      const closeExportMenu = () => {
        document.querySelectorAll('.msg-export-menu.open').forEach(m => {
          if (m !== exportMenu) m.classList.remove('open');
        });
      };

      exportBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeExportMenu();
        exportMenu.classList.toggle('open');
      });

      exportMenu.querySelector('[data-export-single]').addEventListener('click', async (e) => {
        e.stopPropagation();
        exportMenu.classList.remove('open');
        exportBtn.title = '正在导出高清图片...';
        try {
          const r = await window.StockRadar.chatExport.exportBubbleAsImage(bubbleBody);
          exportBtn.title = `已导出 ${r.count} 张图片`;
          setButtonDone(exportBtn);
        } catch (err) {
          console.error('图片导出失败:', err);
          exportBtn.title = '导出失败: ' + (err.message || '未知错误');
          setButtonDone(exportBtn, 'failed');
        }
        setTimeout(() => { exportBtn.title = '导出为图片'; }, 3000);
      });

      exportMenu.querySelector('[data-export-sliced]').addEventListener('click', async (e) => {
        e.stopPropagation();
        exportMenu.classList.remove('open');
        exportBtn.title = '正在分割导出...';
        try {
          const r = await window.StockRadar.chatExport.exportBubbleSliced(bubbleBody, {
            onProgress: (i, n) => { exportBtn.title = `分割导出中 ${i}/${n}...`; },
          });
          const modeText = {
            single: '单张',
            dir: `已存文件夹（${r.count} 张）`,
            zip: `已存 zip（${r.count} 张）`,
            multi: `已下载 ${r.count} 张`,
          }[r.mode] || `已导出 ${r.count} 张`;
          exportBtn.title = modeText;
          setButtonDone(exportBtn);
        } catch (err) {
          console.error('分割导出失败:', err);
          exportBtn.title = (err && err.name === 'AbortError')
            ? '已取消导出'
            : ('导出失败: ' + (err.message || '未知错误'));
          setButtonDone(exportBtn, 'failed');
        }
        setTimeout(() => { exportBtn.title = '导出为图片'; }, 3000);
      });
    }

    return div;
  }

  // ── 创建悬浮面板 ──

  function createPanel(dialogUuid) {
    // 遮罩
    const overlay = document.createElement('div');
    overlay.className = 'group-overlay';

    // 面板
    const panel = document.createElement('div');
    panel.className = 'group-panel';

    // 头部 — 复用主对话 topbar 风格
    const header = document.createElement('div');
    header.className = 'group-panel-header';
    header.innerHTML = `
      <span class="group-panel-title">
        <span class="group-title-icon">💬</span>
        Agent 群调度详情
      </span>
      <button class="group-panel-close" title="关闭">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    `;

    // 内容区
    const body = document.createElement('div');
    body.className = 'group-panel-body';
    body.innerHTML = '<div class="group-empty">加载中...</div>';

    panel.appendChild(header);
    panel.appendChild(body);

    document.body.appendChild(overlay);
    document.body.appendChild(panel);

    // 关闭逻辑
    const close = () => {
      panel.classList.remove('visible');
      overlay.classList.remove('visible');
      setTimeout(() => {
        overlay.remove();
        panel.remove();
      }, 220);
    };

    header.querySelector('.group-panel-close').onclick = close;
    overlay.onclick = close;

    // 动画入场
    requestAnimationFrame(() => {
      overlay.classList.add('visible');
      panel.classList.add('visible');
    });

    return { body, close };
  }

  // ── 加载并渲染消息 ──

  async function loadAndRender(dialogUuid, body) {
    try {
      // 并行加载 agent 信息和消息
      const [agents, resp] = await Promise.all([
        _loadAgents(),
        fetch(`/api/dialogs/${dialogUuid}/group-messages`),
      ]);

      if (!resp.ok) {
        body.innerHTML = '<div class="group-error">请求失败</div>';
        return;
      }
      const data = await resp.json();
      const messages = data.messages || [];

      if (messages.length === 0) {
        body.innerHTML = '<div class="group-empty">暂无调度记录</div>';
        return;
      }

      body.innerHTML = '';
      messages.forEach((msg) => {
        body.appendChild(renderMessage(msg, agents));
      });

      // 滚动到顶部（最新的消息在下方，打开时看最早的上下文）
      body.scrollTop = 0;
    } catch (e) {
      body.innerHTML = `<div class="group-error">加载失败: ${e.message}</div>`;
    }
  }

  // ── 创建 action-bar 按钮 ──

  function createGroupChatButton(dialogUuid) {
    const btn = document.createElement('button');
    btn.className = 'msg-group-btn';
    btn.title = '查看调度详情';
    btn.innerHTML = `
      <svg class="icon-group-chat" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
        <circle cx="9" cy="10" r="1" fill="currentColor"/>
        <circle cx="12" cy="10" r="1" fill="currentColor"/>
        <circle cx="15" cy="10" r="1" fill="currentColor"/>
      </svg>
    `;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const { body } = createPanel(dialogUuid);
      loadAndRender(dialogUuid, body);
    });
    return btn;
  }

  // ── 导出 ──

  window.StockRadar = window.StockRadar || {};
  window.StockRadar.groupChat = {
    createGroupChatButton,
    renderMessage,
    renderMarkdown,
  };
})();
