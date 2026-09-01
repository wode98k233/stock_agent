/*
   选股雷达 Web - 系统配置面板
*/

window.StockRadar = window.StockRadar || {};

window.StockRadar.ConfigPanel = class ConfigPanel {
  constructor() {
    this.panelEl = document.getElementById('configPanel');
    this.overlayEl = document.getElementById('configOverlay');
    this.formEl = document.getElementById('configForm');
    this.diffModalEl = document.getElementById('diffModal');
    this.diffOverlayEl = document.getElementById('diffOverlay');
    this.diffBodyEl = document.getElementById('diffBody');
    this.originalConfig = {};
    this.currentConfig = {};
    this.items = [];
    this.visualization = null;
    this.summaryEl = null;
    this.changeCountEl = null;
    this.notificationStatus = null;
    this.notificationPanelEl = null;
    this.notificationStatusError = '';
  }

  open() {
    // 绑定 header 中的重载按钮（只绑一次）
    if (!this._reloadBound) {
      const btn = document.getElementById('configReloadBtn');
      if (btn) btn.addEventListener('click', () => this._reloadFromEnv());
      this._reloadBound = true;
    }
    this.loadConfig().then(() => {
      this.panelEl.classList.add('open');
      this.overlayEl.classList.add('open');
    });
  }

  close() {
    this.panelEl.classList.remove('open');
    this.overlayEl.classList.remove('open');
  }

  async _reloadFromEnv() {
    const btn = document.getElementById('configReloadBtn');
    const msg = document.getElementById('configReloadMsg');
    if (btn) { btn.disabled = true; btn.textContent = '... 加载中'; }
    if (msg) { msg.textContent = ''; msg.className = 'config-reload-msg'; }
    try {
      const resp = await fetch('/api/config/reload', { method: 'POST' });
      const data = await resp.json();
      if (data.success) {
        if (msg) { msg.textContent = '已重载 ' + data.changed.length + ' 项，刷新配置...'; msg.className = 'config-reload-msg success'; }
        await this.loadConfig();
        if (msg) { msg.textContent = '配置已刷新 (' + data.changed.length + ' 项变更)'; }
      } else {
        if (msg) { msg.textContent = data.message || '重载失败'; msg.className = 'config-reload-msg error'; }
      }
    } catch (err) {
      if (msg) { msg.textContent = '请求失败: ' + err.message; msg.className = 'config-reload-msg error'; }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '↻ 从 .env 重新加载'; }
    }
  }

  async loadConfig() {
    try {
      const [data, notificationStatus] = await Promise.all([
        window.StockRadar.api.getConfig(),
        this._loadNotificationStatusSafe(),
      ]);
      this.notificationStatus = notificationStatus;
      this.items = data.items || [];
      this.originalConfig = {};
      this.currentConfig = {};
      for (const item of this.items) {
        this.originalConfig[item.key] = item.value;
        this.currentConfig[item.key] = item.value;
      }
      this.renderConfig(this.items);
    } catch (err) {
      alert('加载配置失败：' + err.message);
    }
  }

  async _loadNotificationStatusSafe() {
    this.notificationStatusError = '';
    try {
      return await window.StockRadar.api.getNotificationStatus();
    } catch (err) {
      this.notificationStatusError = err.message || '通知状态加载失败';
      return null;
    }
  }

  async _refreshNotificationStatus() {
    this.notificationStatus = await this._loadNotificationStatusSafe();
    this._updateNotificationPanel();
    this._updateSummary();
  }

  _tabMapping() {
    return {
      '模型': ['LLM'],
      'Agent': ['Agent 通用', 'ReAct', '上下文压缩', 'Plan / PDOR / Unified', 'Agent 群模式', '预算'],
      '报告': ['报告'],
      '数据': ['数据源', '缓存', '搜索引擎'],
      '记忆系统': ['长期记忆-基础', '长期记忆-Embedding', '长期记忆-Reranker'],
      '通知': ['通知'],
      '诊断': ['调试'],
    };
  }

  _tabOrder() { return ['模型', 'Agent', '报告', '数据', '记忆系统', '通知', '诊断']; }

  renderConfig(items) {
    this.formEl.innerHTML = '';
    this.formEl.className = 'config-body config-workspace';

    const groups = {};
    for (const item of items) {
      if (!groups[item.group]) groups[item.group] = [];
      groups[item.group].push(item);
    }

    const mapping = this._tabMapping();
    const tabOrder = this._tabOrder();
    const mergedTabs = {};
    for (const tabName of tabOrder) {
      mergedTabs[tabName] = [];
      for (const groupName of mapping[tabName] || []) {
        if (groups[groupName]) mergedTabs[tabName].push(...groups[groupName]);
      }
    }

    this.summaryEl = this._renderSummary();
    this.formEl.appendChild(this.summaryEl);

    const body = document.createElement('div');
    body.className = 'config-main-grid';

    const nav = document.createElement('div');
    nav.className = 'config-side-nav';

    const formArea = document.createElement('div');
    formArea.className = 'config-form-area';

    tabOrder.forEach((tabName, idx) => {
      const tabItems = mergedTabs[tabName];
      if (!tabItems || tabItems.length === 0) return;

      const navBtn = document.createElement('button');
      navBtn.type = 'button';
      navBtn.className = 'config-nav-item' + (idx === 0 ? ' active' : '');
      navBtn.innerHTML = `<span>${tabName}</span><span class="config-nav-count">${tabItems.length}</span>`;
      navBtn.addEventListener('click', () => {
        nav.querySelectorAll('.config-nav-item').forEach(t => t.classList.remove('active'));
        navBtn.classList.add('active');
        formArea.querySelectorAll('.config-tab-content').forEach(c => c.classList.remove('active'));
        formArea.querySelector(`[data-tab="${tabName}"]`).classList.add('active');
      });
      nav.appendChild(navBtn);

      const tabContent = document.createElement('div');
      tabContent.className = 'config-tab-content' + (idx === 0 ? ' active' : '');
      tabContent.dataset.tab = tabName;
      if (tabName === '通知') {
        tabContent.appendChild(this._renderNotificationPanel());
      }
      for (const card of this._renderSections(tabItems)) {
        tabContent.appendChild(card);
      }
      formArea.appendChild(tabContent);
    });

    const preview = this._renderPreview(items);
    body.appendChild(nav);
    body.appendChild(formArea);
    body.appendChild(preview);
    this.formEl.appendChild(body);

    this.visualization = new window.StockRadar.ConfigVisualization('configVizContainer');
    for (const item of items) {
      this.onParamChange(item.key, item.value, false);
    }
    this._updateSummary();
  }

  _renderSummary() {
    const summary = document.createElement('div');
    summary.className = 'config-summary';
    summary.innerHTML = `
      <div class="config-summary-card">
        <div class="config-summary-label">当前模式</div>
        <div class="config-summary-value" data-summary="mode">-</div>
      </div>
      <div class="config-summary-card">
        <div class="config-summary-label">模型</div>
        <div class="config-summary-value" data-summary="model">-</div>
      </div>
      <div class="config-summary-card">
        <div class="config-summary-label">预算</div>
        <div class="config-summary-value" data-summary="budget">-</div>
      </div>
      <div class="config-summary-card">
        <div class="config-summary-label">上下文压缩</div>
        <div class="config-summary-value" data-summary="compaction">-</div>
      </div>
      <div class="config-summary-card">
        <div class="config-summary-label">通知</div>
        <div class="config-summary-value" data-summary="notification">-</div>
      </div>
      <div class="config-summary-card">
        <div class="config-summary-label">变更</div>
        <div class="config-summary-value"><span id="configChangeCount" class="config-change-count">0 项</span></div>
      </div>`;
    return summary;
  }

  _renderSections(items) {
    const byGroup = {};
    for (const item of items) {
      const groupName = this._displayGroup(item);
      if (!byGroup[groupName]) byGroup[groupName] = [];
      byGroup[groupName].push(item);
    }

    return Object.entries(byGroup).map(([groupName, groupItems]) => {
      const card = document.createElement('section');
      card.className = 'config-section-card';

      const head = document.createElement('div');
      head.className = 'config-section-head';
      head.innerHTML = `
        <div>
          <div class="config-section-title">${this._esc(groupName)}</div>
          <div class="config-section-desc">${this._groupDescription(groupName)}</div>
        </div>
        <span class="config-section-count">${groupItems.length} 项</span>`;
      card.appendChild(head);

      for (const item of groupItems) {
        card.appendChild(this._renderRow(item));
      }
      return card;
    });
  }

  _displayGroup(item) {
    if (item.group !== '通知') return item.group;
    if (item.key.startsWith('NOTIFICATION_')) return '通知策略';
    if (item.key.startsWith('DINGTALK_') || item.key.startsWith('FEISHU_') || item.key.startsWith('WECHAT_') || item.key.startsWith('PUSHPLUS_') || item.key.startsWith('SERVERCHAN3_')) {
      return '国内 IM / 聚合推送';
    }
    if (item.key.startsWith('DISCORD_') || item.key.startsWith('SLACK_') || item.key.startsWith('TELEGRAM_') || item.key.startsWith('PUSHOVER_') || item.key.startsWith('NTFY_') || item.key.startsWith('GOTIFY_')) {
      return '海外与自建服务';
    }
    if (item.key.startsWith('EMAIL_') || item.key.startsWith('CUSTOM_WEBHOOK_')) return '邮件 / 自定义 Webhook';
    return '通知渠道';
  }

  _groupDescription(groupName) {
    const desc = {
      'LLM': '模型接入、推理参数和请求透传。',
      'Agent 通用': '影响默认执行模式和通用运行边界。',
      'ReAct': '控制思考、工具调用和循环次数。',
      '上下文压缩': '长对话保留、摘要和压缩窗口策略。',
      'Plan / PDOR / Unified': '计划型 Agent 的步骤和执行预算。',
      'Agent 群模式': '多 Agent 协作的并发数、重试和规划边界。',
      '报告': '报告生成方式、模板和模型配置。',
      '数据源': '行情、新闻和工具数据源优先级。',
      '缓存': '缓存容量、文件大小和过期策略。',
      '搜索引擎': '联网搜索、新闻检索和外部搜索服务密钥。',
      '预算': '单次查询的 token、调用次数和时间边界。',
      '通知策略': '控制通知层是否启用、发送范围、去重、冷却和静默时段。',
      '国内 IM / 聚合推送': '钉钉、飞书、企业微信、PushPlus 和 Server酱等国内常用推送。',
      '海外与自建服务': 'Discord、Slack、Telegram、Pushover、ntfy 和 Gotify。',
      '邮件 / 自定义 Webhook': 'SMTP 邮件和通用 Webhook 推送目标。',
      '调试': '日志、trace 和诊断开关。',
      '长期记忆-基础': 'FTS5 全文检索 + 分词 + 情景记忆保留与清理策略。始终生效，内存成本低。',
      '长期记忆-Embedding': '语义向量写入与检索（可选）。关闭后仅用 FTS5 文本搜索。支持远程 API / Ollama / 本地模型。',
      '长期记忆-Reranker': 'Cross-encoder 可信度精排增强（可选）。独立于 Embedding，支持远程 /v1/rerank 或本地模型。本地模型 ~2GB，可配置空闲卸载。',
    };
    return desc[groupName] || '当前分组的运行参数。';
  }

  _itemDescription(item) {
    const desc = {
      OPENAI_API_BASE: 'OpenAI 兼容接口地址，影响所有默认 LLM 请求。',
      OPENAI_MODEL_NAME: '默认模型名称，用于分类、规划、Agent 思考等主链路。',
      OPENAI_REASONING_CONTENT_POLICY: '控制是否保留或隐藏模型返回的 reasoning_content。',
      OPENAI_HEADERS: '附加到 LLM 请求的 HTTP headers，必须是 JSON 对象。',
      OPENAI_DEFAULT_PARAMS: '默认透传给模型的参数，必须是 JSON 对象。',
      EXTRA_BODY: '额外请求体字段，适合需要 vendor-specific 参数的模型服务。',
      CACHE_PREFIX_ENABLED: '启用后在多数 LLM 调用前注入固定前缀，用于命中服务商 Prompt Caching；分类、观察、调整等短 JSON 调用会跳过。',
      CACHE_PREFIX_CONTENT: '自定义注入到 LLM prompt 前面的固定前缀。留空时使用默认 A 股知识前缀。',
      AGENT_MODE: '新对话默认使用的 Agent 模式，也会同步顶部模式选择器。',
      BATCH_SIZE: '批处理任务每批处理的股票或数据条数。',
      MEMORY_MAX_TOKENS: '对话记忆保留的 token 上限，越高越保留上下文但成本更高。',
      INFO_ACCUMULATOR_COMPRESS_THRESHOLD: '信息累积超过该字符数后触发压缩，降低后续上下文体积。',
      REACT_TOOL_CALLS: 'ReAct 单轮任务允许调用工具的最大次数。',
      REACT_ENABLE_CONTEXT_COMPACTION: '启用后会把较早对话压缩成摘要，减少长对话 token 消耗。',
      REACT_CONTEXT_RECENT_ROUNDS: '保留最近多少轮完整原文，不进入摘要压缩。',
      REACT_SUMMARY_TRIGGER_ROUNDS: '对话轮数达到该值后开始考虑摘要压缩。',
      REACT_SUMMARY_PENDING_CHARS: '待压缩内容达到该字符数时触发摘要。',
      REACT_SUMMARY_MAX_CHARS: '单次摘要输出的最大字符数。',
      REACT_DEDUP_MODE: '工具调用去重策略。exact 表示完全相同请求复用结果，off 表示关闭。',
      REACT_CONTEXT_DEBUG_LOG: '输出上下文压缩诊断日志，排查摘要窗口时使用。',
      PLAN_MAX_STEPS: '计划型 Agent 的最大规划步数，越大越完整但耗时更长。',
      PLAN_EXECUTOR_MAX_RETRIES: '计划执行子步骤失败后的重试次数。',
      PLAN_EXECUTOR_TOOL_CALLS: 'Plan/PDOR 子 ReAct 执行器的工具调用上限。',
      MAX_TOKENS_PER_QUERY: '单次用户请求允许消耗的 token 总上限。',
      MAX_LLM_CALLS_PER_QUERY: '单次用户请求允许的 LLM 调用次数上限。',
      MAX_TIME_SECONDS: '单次任务最大执行秒数，超过后会进入预算处理流程。',
      BUDGET_EXEMPT_CALLS_LIMIT: '预算超限后可豁免继续的调用次数窗口。',
      BUDGET_EXEMPT_TIME_LIMIT: '预算超限后可豁免继续的时间窗口，单位秒。',
      CHECKPOINT_BACKEND: 'LangGraph checkpoint 后端，新任务生效。',
      CACHE_EXPIRE_HOURS: '通用缓存过期时间，单位小时。',
      DATASOURCE_MAX_FAILS: '数据源连续失败达到该次数后进入降级或冷却。',
      AKSHARE_RATE_LIMIT_MIN: 'AkShare 请求间隔下限，降低可提速但更容易触发限制。',
      AKSHARE_RATE_LIMIT_MAX: 'AkShare 请求间隔上限，用于随机限速区间。',
      AKSHARE_REALTIME_CACHE_TTL: '实时行情缓存秒数，越大越快但实时性越低。',
      AKSHARE_ENABLE_EASTMONEY_PATCH: '启用对东方财富接口兼容问题的补丁。',
      PYTDX_PRIORITY: 'Pytdx 数据源优先级，数值越高越优先。',
      PYTDX_CONNECTION_COOLDOWN: 'Pytdx 连接失败后的冷却秒数。',
      PYTDX_SERVERS: 'Pytdx 服务器列表，通常用 JSON 或逗号分隔配置。',
      REPORT_ENABLE_ANALYSIS_ENGINE: '启用专业报告后处理引擎，生成结构化投资报告。',
      REPORT_TEMPLATE: '报告模板强度。professional 更完整，lightweight 更快。',
      REPORT_MODE: '报告合成模式。fast 单次合成，full 更完整但成本更高。',
      REPORT_TIME_HORIZON: '报告默认时间框架：短期、中期或长期。',
      REPORT_MIN_TOKENS: '报告生成时要求的最低 token 预算。',
      REPORT_MAX_TOOL_OUTPUT_CHARS: '进入报告引擎的工具输出最大字符数。',
      REPORT_MAX_NEWS_CHARS: '进入报告引擎的新闻文本最大字符数。',
      REPORT_LLM_MODEL: '报告专用模型。留空时使用默认模型。',
      REPORT_LLM_API_BASE: '报告专用模型接口地址。留空时使用默认 API Base。',
      COMPRESS_LLM_MODEL: '工具输出压缩专用模型。留空时使用默认模型。',
      COMPRESS_LLM_API_BASE: '压缩专用模型接口地址。留空时使用默认 API Base。',
      TOOL_OUTPUT_COMPRESS_THRESHOLD: '工具输出超过该字符数时触发压缩。',
      TOOL_COMPRESS_THRESHOLDS: '按工具名覆盖压缩阈值，必须是 JSON 对象。',
      TOOL_HARD_TRUNCATE_CHARS: '工具输出硬截断上限，避免异常大结果拖垮上下文。',
      DASHBOARD_ENABLED: '启用报告中的决策仪表盘数据块。',
      DASHBOARD_LLM_ENABLED: '启用 LLM 对仪表盘指标做摘要提炼。',
      DASHBOARD_MAX_LLM_TOKENS: '仪表盘 LLM 提炼允许的 token 上限。',
      CACHE_MAX_ENTRIES: '缓存最多保存的记录数量。',
      CACHE_FILE_MAX_MB: '缓存文件最大体积，超过后会触发清理策略。',
      BOCHA_API_KEYS: '博查搜索 API 密钥，多个 key 可用于轮换。',
      BRAVE_API_KEYS: 'Brave Search API 密钥，多个 key 可用于轮换。',
      ANSPIRE_API_KEYS: 'Anspire 搜索 API 密钥。',
      MINIMAX_API_KEYS: 'MiniMax 搜索或模型相关密钥。',
      SEARXNG_URLS: '私有 SearXNG 实例地址列表。',
      SEARXNG_PUBLIC_INSTANCES: '公共 SearXNG 实例列表，稳定性通常不如私有实例。',
      NOTIFICATION_ENABLED: '总开关。关闭时手动发送和测试请求也会被后端跳过。',
      NOTIFICATION_CHANNELS: '限制发送渠道，逗号分隔；留空表示发送到所有可用渠道。',
      NOTIFICATION_DEDUP_TTL: '相同标题和类型在该秒数内只发送一次。',
      NOTIFICATION_COOLDOWN: '相同消息 key 的最小发送间隔，防止重复刷屏。',
      NOTIFICATION_QUIET_START: '静默时段开始小时，使用本机时间，0-23。',
      NOTIFICATION_QUIET_END: '静默时段结束小时，支持跨午夜。',
      DINGTALK_WEBHOOK_URL: '钉钉机器人 Webhook 地址。',
      DINGTALK_WEBHOOK_SECRET: '钉钉机器人加签密钥，敏感项需在 .env 修改。',
      FEISHU_WEBHOOK_URL: '飞书或 Lark 机器人 Webhook 地址。',
      FEISHU_WEBHOOK_SECRET: '飞书机器人签名密钥，敏感项需在 .env 修改。',
      WECHAT_WEBHOOK_URL: '企业微信机器人 Webhook 地址。',
      TELEGRAM_BOT_TOKEN: 'Telegram Bot Token，敏感项需在 .env 修改。',
      TELEGRAM_CHAT_ID: 'Telegram 目标聊天 ID 或频道 ID。',
      EMAIL_SMTP_HOST: 'SMTP 服务器地址。为空时部分邮箱会按用户名域名自动推断。',
      EMAIL_SMTP_PORT: 'SMTP 端口，常见 SSL 为 465，STARTTLS 为 587。',
      EMAIL_SMTP_USER: 'SMTP 登录用户名，通常是发件邮箱。',
      EMAIL_SMTP_PASSWORD: 'SMTP 密码或授权码，敏感项需在 .env 修改。',
      EMAIL_RECIPIENTS: '邮件收件人列表，多个地址用逗号分隔。',
      CUSTOM_WEBHOOK_URL: '通用 Webhook 地址，后端会发送标准 JSON payload。',
      DISCORD_WEBHOOK_URL: 'Discord Webhook 地址。',
      SLACK_WEBHOOK_URL: 'Slack Incoming Webhook 地址。',
      PUSHOVER_USER_KEY: 'Pushover 用户 key，敏感项需在 .env 修改。',
      PUSHOVER_APP_TOKEN: 'Pushover 应用 token，敏感项需在 .env 修改。',
      NTFY_URL: 'ntfy 服务地址，默认可用 https://ntfy.sh。',
      NTFY_TOPIC: 'ntfy 订阅主题，只有配置 topic 后渠道才可用。',
      GOTIFY_URL: 'Gotify 服务地址。',
      GOTIFY_TOKEN: 'Gotify 应用 token，敏感项需在 .env 修改。',
      PUSHPLUS_TOKEN: 'PushPlus token，敏感项需在 .env 修改。',
      SERVERCHAN3_SENDKEY: 'Server酱3 SendKey，敏感项需在 .env 修改。',
      LOG_LEVEL: '后端日志级别。DEBUG 信息最多，生产或长任务建议 INFO 以上。',
      DEBUG_STEP_CONFIRM: '调试步骤确认开关，用于开发阶段观察执行流程。',
      ENABLE_TRACE: '启用执行链路 trace，便于在详情面板和 trace viewer 中排查任务。',
      STOCK_MEMORY_BACKEND: '记忆检索后端：auto 自动选择、fts5 关键词、embedding 向量、hybrid 融合。新任务生效。',
      STOCK_MEMORY_EMBEDDING: 'Embedding 模式：空=关闭向量检索，openai/remote=远程 API，local=本地模型，ollama=本地 Ollama。修改后需重启服务生效。',
      STOCK_MEMORY_EMBEDDING_MODEL: 'Embedding 模型名，如 bge-m3。需与所选模式匹配。修改后需重启服务生效。',
      STOCK_MEMORY_LOCAL_MODEL: '本地 Embedding 模型路径/标识（local 模式使用）。',
      STOCK_MEMORY_API_KEY: 'Embedding 服务 API Key，敏感项，只展示掩码，需在 .env 直接修改。',
      STOCK_MEMORY_API_BASE: 'Embedding 服务接口地址，如 Ollama 的 http://localhost:11434/v1。修改后需重启服务生效。',
      MEMORY_RETRIEVAL_TOP_K: '每次检索返回的最大记忆条数。',
      MEMORY_MAX_PROMPT_TOKENS: '注入 LLM 上下文的记忆最大字符数。',
      MEMORY_EPISODIC_RETENTION_DAYS: '情景记忆保留天数，超过后归档清理。',
      STOCK_MEMORY_EMBEDDING_READY_TIMEOUT: '启动时等待 Embedding 服务就绪的最长秒数，超时降级 FTS5。',
      STOCK_MEMORY_EMBEDDING_READY_INTERVAL: '启动时探测 Embedding 服务的轮询间隔秒数。',
      STOCK_MEMORY_RERANKER: 'Reranker 模式：空=关闭，local=本地，openai=远程 API。',
      STOCK_MEMORY_RERANKER_MODEL: 'Reranker 模型名。',
    };
    if (item.sensitive) {
      return desc[item.key] || '敏感配置只展示掩码值，需要直接修改 .env 后重新加载。';
    }
    return desc[item.key] || `${item.label || item.key} 的运行参数。`;
  }

  _renderPreview() {
    const preview = document.createElement('aside');
    preview.className = 'config-preview-rail';

    const flowTitle = document.createElement('div');
    flowTitle.className = 'viz-section-title config-preview-title';
    flowTitle.textContent = '当前影响预览';
    preview.appendChild(flowTitle);

    const vizContainer = document.createElement('div');
    vizContainer.id = 'configVizContainer';
    preview.appendChild(vizContainer);

    const compression = document.createElement('div');
    compression.className = 'config-impact-card';
    compression.innerHTML = `
      <div class="config-impact-title">上下文压缩窗口</div>
      <div class="config-impact-text">最近轮次保留原文，旧消息压缩为结构化摘要。阈值越低，长报告任务越早进入压缩。</div>`;
    preview.appendChild(compression);

    return preview;
  }

  _renderNotificationPanel() {
    const panel = document.createElement('section');
    panel.className = 'config-notification-panel';
    this.notificationPanelEl = panel;
    this._updateNotificationPanel();
    return panel;
  }

  _updateNotificationPanel() {
    if (!this.notificationPanelEl) return;

    const catalog = this._notificationCatalog();
    const status = this.notificationStatus || {};
    const channels = catalog.map((channel) => this._mergeChannelStatus(channel));
    const configuredCount = channels.filter(ch => ch.configured).length;
    const availableCount = channels.filter(ch => ch.available).length;
    const enabled = status.enabled != null ? !!status.enabled : !!this.currentConfig.NOTIFICATION_ENABLED;
    const noise = status.noise_control || {};
    const activeChannelOptions = channels
      .filter(ch => ch.configured)
      .map(ch => `<option value="${this._esc(ch.name)}">${this._esc(ch.label)}</option>`)
      .join('');

    this.notificationPanelEl.innerHTML = `
      <div class="config-notification-head">
        <div>
          <div class="config-section-title">通知控制台</div>
          <div class="config-section-desc">查看渠道配置、断路器状态，并发送测试或手动报告通知。</div>
        </div>
        <div class="notification-head-actions">
          <button class="secondary-button notification-refresh-btn" type="button">刷新状态</button>
          <button class="send-button notification-test-all-btn" type="button"${configuredCount ? '' : ' disabled'}>测试全部</button>
        </div>
      </div>
      <div class="notification-status-grid">
        <div class="notification-stat">
          <span>通知层</span>
          <strong class="${enabled ? 'ok' : 'muted'}">${enabled ? '已启用' : '未启用'}</strong>
        </div>
        <div class="notification-stat">
          <span>已配置渠道</span>
          <strong>${configuredCount} / ${channels.length}</strong>
        </div>
        <div class="notification-stat">
          <span>当前可用</span>
          <strong class="${availableCount ? 'ok' : 'muted'}">${availableCount}</strong>
        </div>
        <div class="notification-stat">
          <span>静默时段</span>
          <strong>${this._esc(noise.quiet_hours || this._quietHoursFromConfig())}</strong>
        </div>
      </div>
      ${this.notificationStatusError ? `<div class="notification-error">${this._esc(this.notificationStatusError)}</div>` : ''}
      <div class="notification-noise-line">
        <span>去重 ${this._esc(noise.dedup_ttl ?? this.currentConfig.NOTIFICATION_DEDUP_TTL ?? '-')} 秒</span>
        <span>冷却 ${this._esc(noise.cooldown_seconds ?? this.currentConfig.NOTIFICATION_COOLDOWN ?? '-')} 秒</span>
        <span>${noise.in_quiet_hours ? '当前处于静默时段' : '当前非静默时段'}</span>
        <span>活跃去重键 ${this._esc(noise.active_dedup_keys ?? 0)}</span>
      </div>
      <div class="notification-channel-grid">
        ${channels.map(ch => this._renderNotificationChannel(ch)).join('')}
      </div>
      <div class="notification-send-form">
        <div class="notification-send-title">
          <div>
            <div class="config-section-title">手动发送</div>
            <div class="config-section-desc">用于验证报告通知格式，或把当前报告内容复制后主动推送。</div>
          </div>
          <span class="notification-send-state" data-notification-result></span>
        </div>
        <div class="notification-send-grid">
          <label class="notification-title-field">标题<input class="config-input" data-notification-title value="选股雷达通知测试"></label>
          <label class="notification-type-field">类型
            <select class="config-select" data-notification-type>
              <option value="report">report</option>
              <option value="alert">alert</option>
              <option value="system">system</option>
            </select>
          </label>
          <label class="notification-channel-field">渠道
            <select class="config-select" data-notification-channel>
              <option value="">全部可用渠道</option>
              ${activeChannelOptions}
            </select>
          </label>
          <label class="notification-force-label"><input type="checkbox" data-notification-force checked> 跳过降噪</label>
        </div>
        <label class="notification-content-label">内容<textarea class="config-input notification-content-input" data-notification-content rows="4">这是一条来自前端配置页的手动通知。</textarea></label>
        <div class="notification-form-actions">
          <button class="send-button notification-send-btn" type="button"${configuredCount ? '' : ' disabled'}>发送通知</button>
        </div>
      </div>`;

    const refreshBtn = this.notificationPanelEl.querySelector('.notification-refresh-btn');
    if (refreshBtn) refreshBtn.onclick = () => this._refreshNotificationStatus();

    const testAllBtn = this.notificationPanelEl.querySelector('.notification-test-all-btn');
    if (testAllBtn) testAllBtn.onclick = () => this._runNotificationTest();

    this.notificationPanelEl.querySelectorAll('.notification-channel-test').forEach((btn) => {
      btn.onclick = () => this._runNotificationTest(btn.dataset.channel);
    });

    const sendBtn = this.notificationPanelEl.querySelector('.notification-send-btn');
    if (sendBtn) sendBtn.onclick = () => this._runNotificationSend();
  }

  _renderNotificationChannel(ch) {
    const statusClass = ch.available ? 'ok' : (ch.configured ? 'warn' : 'muted');
    const statusText = ch.available ? '可用' : (ch.configured ? '不可用' : '未配置');
    const breaker = ch.circuit_breaker || 'unknown';
    return `
      <div class="notification-channel-card ${statusClass}">
        <div class="notification-channel-top">
          <div>
            <div class="notification-channel-name">${this._esc(ch.label)}</div>
            <div class="notification-channel-desc">${this._esc(ch.description)}</div>
          </div>
          <span class="notification-channel-status">${statusText}</span>
        </div>
        <div class="notification-channel-meta">
          <span>priority ${this._esc(ch.priority)}</span>
          <span>breaker ${this._esc(breaker)}</span>
        </div>
        <div class="notification-channel-keys">${ch.keys.map(key => `<code>${this._esc(key)}</code>`).join('')}</div>
        <button class="secondary-button notification-channel-test" type="button" data-channel="${this._esc(ch.name)}"${ch.configured ? '' : ' disabled'}>测试</button>
      </div>`;
  }

  _notificationCatalog() {
    return [
      { name: 'DingTalk', label: '钉钉', priority: 90, keys: ['DINGTALK_WEBHOOK_URL', 'DINGTALK_WEBHOOK_SECRET'], required: ['DINGTALK_WEBHOOK_URL'], description: '适合国内团队群机器人。' },
      { name: 'Feishu', label: '飞书', priority: 85, keys: ['FEISHU_WEBHOOK_URL', 'FEISHU_WEBHOOK_SECRET'], required: ['FEISHU_WEBHOOK_URL'], description: '飞书或 Lark 群机器人。' },
      { name: 'WeChat', label: '企业微信', priority: 80, keys: ['WECHAT_WEBHOOK_URL'], required: ['WECHAT_WEBHOOK_URL'], description: '企业微信群机器人 Webhook。' },
      { name: 'Telegram', label: 'Telegram', priority: 70, keys: ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'], required: ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'], description: 'Bot API 推送，支持长文本分片。' },
      { name: 'Discord', label: 'Discord', priority: 65, keys: ['DISCORD_WEBHOOK_URL'], required: ['DISCORD_WEBHOOK_URL'], description: 'Discord Incoming Webhook。' },
      { name: 'Slack', label: 'Slack', priority: 65, keys: ['SLACK_WEBHOOK_URL'], required: ['SLACK_WEBHOOK_URL'], description: 'Slack Incoming Webhook。' },
      { name: 'Email', label: 'Email', priority: 60, keys: ['EMAIL_SMTP_HOST', 'EMAIL_SMTP_PORT', 'EMAIL_SMTP_USER', 'EMAIL_SMTP_PASSWORD', 'EMAIL_RECIPIENTS'], required: ['EMAIL_SMTP_USER', 'EMAIL_SMTP_PASSWORD', 'EMAIL_RECIPIENTS'], description: 'SMTP 邮件，可自动推断部分邮箱服务器。' },
      { name: 'Pushover', label: 'Pushover', priority: 55, keys: ['PUSHOVER_USER_KEY', 'PUSHOVER_APP_TOKEN'], required: ['PUSHOVER_USER_KEY', 'PUSHOVER_APP_TOKEN'], description: '移动端推送服务，适合个人提醒。' },
      { name: 'Webhook', label: '自定义 Webhook', priority: 50, keys: ['CUSTOM_WEBHOOK_URL'], required: ['CUSTOM_WEBHOOK_URL'], description: '通用 JSON Webhook，适合自建接收端。' },
      { name: 'PushPlus', label: 'PushPlus', priority: 50, keys: ['PUSHPLUS_TOKEN'], required: ['PUSHPLUS_TOKEN'], description: '国内微信推送聚合服务。' },
      { name: 'ServerChan3', label: 'Server酱3', priority: 50, keys: ['SERVERCHAN3_SENDKEY'], required: ['SERVERCHAN3_SENDKEY'], description: 'Server酱3 微信推送。' },
      { name: 'ntfy', label: 'ntfy', priority: 45, keys: ['NTFY_URL', 'NTFY_TOPIC'], required: ['NTFY_TOPIC'], description: 'ntfy.sh 或自托管主题推送。' },
      { name: 'Gotify', label: 'Gotify', priority: 45, keys: ['GOTIFY_URL', 'GOTIFY_TOKEN'], required: ['GOTIFY_URL', 'GOTIFY_TOKEN'], description: '自托管 Gotify 推送服务。' },
    ];
  }

  _mergeChannelStatus(channel) {
    const statusChannels = this.notificationStatus?.channels || [];
    const status = statusChannels.find(ch => ch.name === channel.name);
    const configuredFromConfig = channel.required.every(key => this._hasConfigValue(key));
    return {
      ...channel,
      configured: status?.configured ?? configuredFromConfig,
      available: status?.available ?? (configuredFromConfig && this._isCircuitClosed(status?.circuit_breaker)),
      circuit_breaker: status?.circuit_breaker || 'unknown',
      priority: status?.priority ?? channel.priority,
    };
  }

  _hasConfigValue(key) {
    const value = this.currentConfig[key];
    return value !== null && value !== undefined && String(value).trim() !== '';
  }

  _isCircuitClosed(state) {
    if (!state || state === 'unknown') return true;
    return String(state).toLowerCase() === 'closed';
  }

  _quietHoursFromConfig() {
    const start = this.currentConfig.NOTIFICATION_QUIET_START ?? '-';
    const end = this.currentConfig.NOTIFICATION_QUIET_END ?? '-';
    return `${start}:00 ~ ${end}:00`;
  }

  _setNotificationResult(message, tone = '') {
    if (!this.notificationPanelEl) return;
    const el = this.notificationPanelEl.querySelector('[data-notification-result]');
    if (!el) return;
    el.className = 'notification-send-state' + (tone ? ` ${tone}` : '');
    el.textContent = message;
  }

  _formatNotificationResults(results) {
    const items = results || [];
    if (items.length === 0) return '后端未返回发送结果，通常是通知层未启用或没有可用渠道。';
    const ok = items.filter(r => r.success).length;
    const failed = items.length - ok;
    return `成功 ${ok} / ${items.length}` + (failed ? `，失败 ${failed}` : '');
  }

  async _runNotificationTest(channel) {
    this._setNotificationResult(channel ? `正在测试 ${channel}...` : '正在测试全部渠道...', 'loading');
    try {
      const data = await window.StockRadar.api.testNotification(channel);
      const message = this._formatNotificationResults(data.results);
      await this._refreshNotificationStatus();
      this._setNotificationResult(message, 'ok');
    } catch (err) {
      this._setNotificationResult('测试失败：' + err.message, 'error');
    }
  }

  async _runNotificationSend() {
    if (!this.notificationPanelEl) return;
    const title = this.notificationPanelEl.querySelector('[data-notification-title]')?.value?.trim();
    const content = this.notificationPanelEl.querySelector('[data-notification-content]')?.value?.trim();
    const type = this.notificationPanelEl.querySelector('[data-notification-type]')?.value || 'report';
    const channel = this.notificationPanelEl.querySelector('[data-notification-channel]')?.value || '';
    const force = !!this.notificationPanelEl.querySelector('[data-notification-force]')?.checked;
    if (!title || !content) {
      this._setNotificationResult('标题和内容不能为空。', 'error');
      return;
    }
    this._setNotificationResult('正在发送通知...', 'loading');
    try {
      const data = await window.StockRadar.api.sendNotification({
        title,
        content,
        type,
        channels: channel ? [channel] : null,
        force,
      });
      const message = this._formatNotificationResults(data.results);
      await this._refreshNotificationStatus();
      this._setNotificationResult(message, 'ok');
    } catch (err) {
      this._setNotificationResult('发送失败：' + err.message, 'error');
    }
  }

  _renderRow(item) {
    const row = document.createElement('div');
    row.className = 'config-row' + (item.sensitive ? ' config-row-full' : '');

    const label = document.createElement('label');
    label.className = 'config-label';
    const description = this._itemDescription(item);
    const help = description
      ? `<span class="config-help" tabindex="0" aria-label="${this._esc(description)}" data-tooltip="${this._esc(description)}">?</span>`
      : '';
    label.innerHTML = `<span class="config-label-row"><span>${this._esc(item.label)}</span>${help}</span><small>${this._esc(item.key)}</small>`;
    row.appendChild(label);

    const controlWrap = document.createElement('div');
    controlWrap.className = 'config-control';

    if (item.sensitive) {
      const input = document.createElement('input');
      input.type = 'password';
      input.value = item.value || '';
      input.disabled = true;
      input.className = 'config-input config-input-disabled';
      controlWrap.appendChild(input);
    } else if (item.type === 'bool') {
      const toggle = document.createElement('label');
      toggle.className = 'config-toggle';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = !!item.value;
      checkbox.dataset.key = item.key;
      checkbox.addEventListener('change', (e) => {
        this.currentConfig[item.key] = e.target.checked;
        this.onParamChange(item.key, e.target.checked);
      });
      const slider = document.createElement('span');
      slider.className = 'config-toggle-slider';
      toggle.appendChild(checkbox);
      toggle.appendChild(slider);
      controlWrap.appendChild(toggle);
    } else if (item.type === 'int') {
      const input = document.createElement('input');
      input.type = 'number';
      input.value = item.value ?? '';
      input.className = 'config-input';
      input.dataset.key = item.key;
      input.addEventListener('input', (e) => {
        const value = parseInt(e.target.value, 10) || 0;
        this.currentConfig[item.key] = value;
        this.onParamChange(item.key, value);
      });
      controlWrap.appendChild(input);
    } else if (item.choices) {
      const select = document.createElement('select');
      select.className = 'config-select';
      select.dataset.key = item.key;
      for (const choice of item.choices) {
        const opt = document.createElement('option');
        opt.value = choice;
        opt.textContent = choice;
        if (choice === item.value) opt.selected = true;
        select.appendChild(opt);
      }
      select.addEventListener('change', (e) => {
        this.currentConfig[item.key] = e.target.value;
        this.onParamChange(item.key, e.target.value);
      });
      controlWrap.appendChild(select);
    } else {
      const input = document.createElement('input');
      input.type = 'text';
      input.value = item.value || '';
      input.className = 'config-input';
      input.dataset.key = item.key;
      input.addEventListener('input', (e) => {
        this.currentConfig[item.key] = e.target.value;
        this.onParamChange(item.key, e.target.value);
      });
      controlWrap.appendChild(input);
    }

    const state = document.createElement('span');
    state.className = 'config-row-state';
    state.dataset.key = item.key;
    row.appendChild(controlWrap);
    row.appendChild(state);
    return row;
  }

  onParamChange(key, value, updateSummary = true) {
    if (this.visualization) {
      if (key === 'AGENT_MODE') {
        const modeSelect = document.getElementById('modeSelect');
        if (modeSelect && value) {
          modeSelect.value = value;
          const label = document.getElementById('modeLabel');
          if (label) label.textContent = this._modeLabel(value);
        }
        const vizMode = this._toVizMode(value);
        if (vizMode) this.visualization.switchMode(vizMode);
      }
      const numericVizKeys = new Set([
        'REACT_TOOL_CALLS',
        'REACT_CONTEXT_RECENT_ROUNDS',
        'REACT_SUMMARY_TRIGGER_ROUNDS',
        'REACT_SUMMARY_PENDING_CHARS',
        'REACT_SUMMARY_MAX_CHARS',
        'PLAN_EXECUTOR_TOOL_CALLS',
        'PLAN_MAX_STEPS',
        'MAX_TOKENS_PER_QUERY',
        'MEMORY_MAX_TOKENS',
        'TOOL_HARD_TRUNCATE_CHARS',
      ]);
      if (numericVizKeys.has(key)) {
        const num = parseInt(value, 10);
        if (!isNaN(num)) this.visualization.updateParam(key, num);
      } else if (key === 'REACT_ENABLE_CONTEXT_COMPACTION' || key === 'REACT_DEDUP_MODE') {
        this.visualization.updateParam(key, value);
      }
    }
    if (this._isNotificationConfigKey(key)) {
      this._updateNotificationPanel();
    }
    if (updateSummary) this._updateSummary();
  }

  _isNotificationConfigKey(key) {
    return key.startsWith('NOTIFICATION_') ||
      key.startsWith('DINGTALK_') ||
      key.startsWith('FEISHU_') ||
      key.startsWith('WECHAT_') ||
      key.startsWith('TELEGRAM_') ||
      key.startsWith('EMAIL_') ||
      key.startsWith('CUSTOM_WEBHOOK_') ||
      key.startsWith('DISCORD_') ||
      key.startsWith('SLACK_') ||
      key.startsWith('PUSHOVER_') ||
      key.startsWith('NTFY_') ||
      key.startsWith('GOTIFY_') ||
      key.startsWith('PUSHPLUS_') ||
      key.startsWith('SERVERCHAN3_');
  }

  _updateSummary() {
    if (!this.summaryEl) return;
    const setText = (name, value) => {
      const el = this.summaryEl.querySelector(`[data-summary="${name}"]`);
      if (el) el.textContent = value;
    };
    setText('mode', this._modeLabel(this.currentConfig.AGENT_MODE) || '-');
    setText('model', this.currentConfig.OPENAI_MODEL_NAME || this.currentConfig.REPORT_LLM_MODEL || '-');
    setText('budget', this.currentConfig.MAX_TOKENS_PER_QUERY ? `${this.currentConfig.MAX_TOKENS_PER_QUERY} tokens` : '-');
    setText('compaction', this.currentConfig.REACT_ENABLE_CONTEXT_COMPACTION ? 'ON' : 'OFF');
    const channels = this._notificationCatalog().map((channel) => this._mergeChannelStatus(channel));
    const notificationConfigured = channels.filter(ch => ch.configured).length;
    const notificationEnabled = this.notificationStatus?.enabled ?? !!this.currentConfig.NOTIFICATION_ENABLED;
    setText('notification', `${notificationEnabled ? 'ON' : 'OFF'} · ${notificationConfigured} 渠道`);

    const changes = this.getChanges();
    const count = Object.keys(changes).length;
    const changeCount = document.getElementById('configChangeCount');
    if (changeCount) changeCount.textContent = `${count} 项`;
    this.formEl.querySelectorAll('.config-row-state').forEach((el) => {
      const key = el.dataset.key;
      el.classList.toggle('changed', Object.prototype.hasOwnProperty.call(changes, key));
    });
  }

  _toVizMode(agentMode) {
    const map = {
      'react_stock': 'react',
      'plan_solve': 'plan',
      'unified_plan': 'unified',
      'scenario': 'scenario',
      'pdor': 'pdor',
    };
    return map[agentMode] || 'react';
  }

  _modeLabel(agentMode) {
    const map = {
      'react_stock': 'ReAct',
      'plan_solve': 'Plan & Solve',
      'unified_plan': 'Unified Plan',
      'scenario': 'Scenario',
      'pdor': 'PDOR',
    };
    return map[agentMode] || agentMode;
  }

  getChanges() {
    const changes = {};
    for (const key in this.currentConfig) {
      if (this.currentConfig[key] !== this.originalConfig[key]) changes[key] = this.currentConfig[key];
    }
    return changes;
  }

  showDiff() {
    const changes = this.getChanges();
    const keys = Object.keys(changes);
    if (keys.length === 0) {
      alert('没有变更');
      return;
    }

    this.diffBodyEl.innerHTML = '';
    for (const key of keys) {
      const oldVal = this.originalConfig[key];
      const newVal = changes[key];
      const item = this.items.find(i => i.key === key);
      const label = item ? item.label : key;

      const row = document.createElement('div');
      row.className = 'diff-row';
      row.innerHTML = `
        <div class="diff-title">${this._esc(label)}</div>
        <div class="diff-old">旧值：${this._esc(String(oldVal))}</div>
        <div class="diff-new">新值：${this._esc(String(newVal))}</div>`;
      this.diffBodyEl.appendChild(row);
    }

    this.diffModalEl.classList.add('open');
    this.diffOverlayEl.classList.add('open');
  }

  closeDiff() {
    this.diffModalEl.classList.remove('open');
    this.diffOverlayEl.classList.remove('open');
  }

  async saveConfig() {
    const changes = this.getChanges();
    if (Object.keys(changes).length === 0) {
      alert('没有变更需要保存');
      return;
    }
    try {
      await window.StockRadar.api.applyConfig(changes);
      alert('配置已保存并应用');
      this.close();
    } catch (err) {
      alert('保存失败：' + err.message);
    }
  }

  async applyConfig() {
    const changes = this.getChanges();
    if (Object.keys(changes).length === 0) {
      alert('没有变更需要应用');
      return;
    }
    try {
      await window.StockRadar.api.applyConfig(changes);
      alert('配置已应用');
      this.closeDiff();
      this.close();
    } catch (err) {
      alert('应用失败：' + err.message);
    }
  }

  _esc(value) {
    const d = document.createElement('div');
    d.textContent = value == null ? '' : String(value);
    return d.innerHTML;
  }
};
