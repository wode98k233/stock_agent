/* ─────────────────────────────────────────────
   选股雷达 Web — API 调用封装
   ───────────────────────────────────────────── */

window.StockRadar = window.StockRadar || {};

window.StockRadar.api = (() => {
  const API = "";

  async function fetchJSON(url, options) {
    const res = await fetch(`${API}${url}`, options);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const detail = body.detail;
      const err = new Error(
        detail && typeof detail === "object" ? (detail.message || `HTTP ${res.status}`) : (detail || `HTTP ${res.status}`)
      );
      err.detail = detail;
      throw err;
    }
    return res.json();
  }

  function getDialogs() {
    return fetchJSON("/api/dialogs").then((d) => d.items || d || []);
  }

  function createDialog(title, mode) {
    return fetchJSON("/api/dialogs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title || "新对话", mode: mode || "react_stock" }),
    });
  }

  function getMessages(dialogUuid) {
    return fetchJSON(`/api/dialogs/${dialogUuid}/messages`).then((d) => d.items || d || []);
  }

  function sendMessage(dialogUuid, content, mode) {
    return fetchJSON(`/api/dialogs/${dialogUuid}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, mode }),
    });
  }

  function getHealth() {
    return fetchJSON("/api/health");
  }

  function getModes() {
    return fetchJSON("/api/modes");
  }

  function getDataSources() {
    return fetchJSON("/api/datasources");
  }

  function getTask(taskId) {
    return fetchJSON(`/api/tasks/${taskId}`);
  }

  function getTraceSteps(runId) {
    return fetchJSON(`/api/traces/runs/${runId}/steps`).then((d) => d.items || d || []);
  }

  function getTraceChain(runId) {
    return fetchJSON(`/api/traces/runs/${runId}/chain`).then((d) => d.items || []);
  }

  function getDialogTrace(dialogUuid) {
    return fetchJSON(`/api/dialogs/${dialogUuid}/trace`);
  }

  function getCorrelation(q) {
    return fetchJSON(`/api/logs/correlate?q=${encodeURIComponent(q || "")}`);
  }

  function getStepDetail(stepId) {
    return fetchJSON(`/api/traces/steps/${stepId}/detail`);
  }

  function getConfig() {
    return fetchJSON("/api/config");
  }

  function saveConfig(changes) {
    return fetchJSON("/api/config/diff", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ changes }),
    });
  }

  function applyConfig(changes) {
    return fetchJSON("/api/config/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ changes }),
    });
  }

  function getNotificationStatus() {
    return fetchJSON("/api/notification/status");
  }

  function testNotification(channel) {
    return fetchJSON("/api/notification/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(channel ? { channel } : {}),
    });
  }

  function sendNotification(payload) {
    return fetchJSON("/api/notification/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }

  function getReportTemplates() {
    return fetchJSON("/api/report-templates");
  }

  function getReportTemplate(templateId) {
    return fetchJSON(`/api/report-templates/${encodeURIComponent(templateId)}`);
  }

  function saveReportTemplate(templateId, template) {
    return fetchJSON(`/api/report-templates/${encodeURIComponent(templateId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template }),
    });
  }

  function getDashboards() {
    return fetchJSON("/api/dashboards");
  }

  function getDashboard(dashboardId) {
    return fetchJSON(`/api/dashboards/${encodeURIComponent(dashboardId)}`);
  }

  function saveDashboard(dashboardId, definition) {
    return fetchJSON(`/api/dashboards/${encodeURIComponent(dashboardId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ definition }),
    });
  }

  function getDashboardAssociations() {
    return fetchJSON("/api/dashboards/associations");
  }

  function saveDashboardAssociations(data) {
    return fetchJSON("/api/dashboards/associations", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    });
  }

  function importDashboard(definition) {
    return fetchJSON("/api/dashboards/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ definition }),
    });
  }

  function deleteDialog(dialogUuid) {
    return fetchJSON(`/api/dialogs/${dialogUuid}`, {
      method: "DELETE",
    });
  }

  function getCalendar(year, month) {
    return fetchJSON(`/api/calendar?year=${year}&month=${month}`);
  }

  function getWatchlist(tag) {
    const q = tag ? `?tag=${encodeURIComponent(tag)}` : '';
    return fetchJSON(`/api/watchlist${q}`).then((d) => d.items || []);
  }

  function addWatchlist(stock_code, stock_name, market) {
    return fetchJSON("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stock_code, stock_name: stock_name || "", market: market || "cn" }),
    });
  }

  function deleteWatchlist(stock_code, market) {
    return fetchJSON(`/api/watchlist/${encodeURIComponent(stock_code)}?market=${market || 'cn'}`, {
      method: "DELETE",
    });
  }

  function syncWatchlist() {
    return fetchJSON("/api/watchlist/sync", { method: "POST" });
  }

  function getWatchlistQuotes(symbols, source) {
    return fetchJSON("/api/watchlist/quotes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols, source: source || "auto" }),
    });
  }

  function getWatchlistQuote(symbol, source) {
    return fetchJSON("/api/watchlist/quote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols: [symbol], source: source || "auto" }),
    });
  }

  function getKline(code, days, market) {
    let url = `/api/chart/kline/${encodeURIComponent(code)}?days=${days || 120}`;
    if (market) url += `&market=${encodeURIComponent(market)}`;
    return fetchJSON(url);
  }

  function syncKline(code, days, source) {
    return fetchJSON("/api/chart/kline/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, days: days || 250, source: source || "auto" }),
    });
  }

  function getSignals(code, days) {
    return fetchJSON(`/api/chart/signals/${encodeURIComponent(code)}?days=${days || 120}`);
  }

  function searchChartStock(keyword) {
    return fetchJSON(`/api/chart/search?keyword=${encodeURIComponent(keyword)}`);
  }

  /** 扫描策略买点信号（无状态评估，K 线图集成回测） */
  function scanStrategySignals(payload) {
    return fetchJSON("/api/chart/strategy-signals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }

  function sendBudgetDecision(taskId, decision) {
    return fetchJSON(`/api/tasks/${taskId}/budget_decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    });
  }

  async function getRouteRules() {
    return fetchJSON("/api/report-templates/route-rules");
  }

  async function saveRouteRules(templateId, keywords) {
    return fetchJSON(`/api/report-templates/${encodeURIComponent(templateId)}/route-rules`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keywords }),
    });
  }

  async function routeTest(text) {
    return fetchJSON("/api/report-templates/route-test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  }

  // ============================================================
  // 回测 API
  // ============================================================
  async function getBacktestStrategies(category) {
    const qs = category ? `?category=${encodeURIComponent(category)}` : '';
    return fetchJSON(`/api/backtest/strategies${qs}`);
  }

  async function saveBacktestStrategy(data) {
    return fetchJSON("/api/backtest/strategies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  }

  async function deleteBacktestStrategy(id) {
    return fetchJSON(`/api/backtest/strategies/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  }

  async function runBacktest(config) {
    return fetchJSON("/api/backtest/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
  }

  async function getBacktestRuns(code, strategyId, page = 1, pageSize = 20) {
    const params = new URLSearchParams();
    if (code) params.set('code', code);
    if (strategyId) params.set('strategy_id', strategyId);
    params.set('page', page);
    params.set('page_size', pageSize);
    return fetchJSON(`/api/backtest/runs?${params}`);
  }

  async function getBacktestRunDetail(runId) {
    return fetchJSON(`/api/backtest/runs/${encodeURIComponent(runId)}`);
  }

  async function getBacktestKline(runId) {
    return fetchJSON(`/api/backtest/runs/${encodeURIComponent(runId)}/kline`);
  }

  async function deleteBacktestRun(runId) {
    return fetchJSON(`/api/backtest/runs/${encodeURIComponent(runId)}`, {
      method: "DELETE",
    });
  }

  function streamCleanFailed(onMessage, onError) {
    let done = false;
    const es = new EventSource("/api/backtest/clean-failed");
    es.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.phase === 'done') done = true;
        onMessage(msg);
      } catch (err) { onError && onError(err); }
    };
    es.onerror = () => {
      es.close();
      if (!done && onError) onError(new Error('SSE 连接异常'));
    };
    return es;
  }

  // ============================================================
  // 批量回测 API
  // ============================================================
  async function runBatchBacktest(config) {
    return fetchJSON("/api/backtest/batch-run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
  }

  async function listBatches(page = 1, pageSize = 20) {
    return fetchJSON(`/api/backtest/batches?page=${page}&page_size=${pageSize}`);
  }

  async function getBatch(batchId) {
    return fetchJSON(`/api/backtest/batches/${encodeURIComponent(batchId)}`);
  }

  async function deleteBatch(batchId) {
    return fetchJSON(`/api/backtest/batches/${encodeURIComponent(batchId)}`, {
      method: "DELETE",
    });
  }

  async function rerunBatchItem(batchId, code) {
    return fetchJSON(`/api/backtest/batches/${encodeURIComponent(batchId)}/rerun`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  }

  /** 批量重试所有失败 item（复用原 batch，成功数据保留） */
  async function rerunBatchFailed(batchId, concurrency = 3) {
    return fetchJSON(`/api/backtest/batches/${encodeURIComponent(batchId)}/rerun-failed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ concurrency }),
    });
  }

  /** SSE 订阅批量进度，返回 EventSource 实例（调用方负责 close） */
  function streamBatchProgress(batchId, onMessage, onError) {
    let done = false;
    const url = `/api/backtest/batches/${encodeURIComponent(batchId)}/progress`;
    const es = new EventSource(url);
    es.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.status === 'completed' || msg.status === 'failed') done = true;
        onMessage(msg);
      } catch (err) { if (!done && onError) onError(err); }
    };
    es.onerror = () => { es.close(); if (!done && onError) onError(new Error('SSE 连接异常')); };
    return es;
  }

  // ============================================================
  // 多策略批量回测
  // ============================================================
  async function runMultiBacktest(config) {
    return fetchJSON("/api/backtest/multi-run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
  }

  async function getMulti(multiId) {
    return fetchJSON(`/api/backtest/multi/${encodeURIComponent(multiId)}`);
  }

  async function listMulti(page = 1, pageSize = 20) {
    return fetchJSON(`/api/backtest/multi-list?page=${page}&page_size=${pageSize}`);
  }

  async function deleteMulti(multiId) {
    return fetchJSON(`/api/backtest/multi/${encodeURIComponent(multiId)}`, {
      method: "DELETE",
    });
  }

  function streamMultiProgress(multiId, onMessage, onError) {
    let done = false;
    const url = `/api/backtest/multi/${encodeURIComponent(multiId)}/progress`;
    const es = new EventSource(url);
    es.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.status === 'completed' || msg.status === 'failed') done = true;
        onMessage(msg);
      } catch (err) { if (!done && onError) onError(err); }
    };
    es.onerror = () => { es.close(); if (!done && onError) onError(new Error('SSE 连接异常')); };
    return es;
  }

  // ============================================================
  // 测试集 CRUD
  // ============================================================
  async function listTestSets() {
    return fetchJSON("/api/backtest/test-sets");
  }

  async function createTestSet(data) {
    return fetchJSON("/api/backtest/test-sets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  }

  async function getTestSet(id) {
    return fetchJSON(`/api/backtest/test-sets/${id}`);
  }

  async function updateTestSet(id, data) {
    return fetchJSON(`/api/backtest/test-sets/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  }

  async function deleteTestSet(id) {
    return fetchJSON(`/api/backtest/test-sets/${id}`, { method: "DELETE" });
  }

  // ============================================================
  // 数据采集 API
  // ============================================================
  async function createDataTask(type, config) {
    return fetchJSON("/api/data/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, ...config }),
    });
  }

  async function getDataTasks(status, limit) {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (limit) params.set('limit', limit);
    return fetchJSON(`/api/data/tasks?${params}`);
  }

  async function getDataTask(taskId) {
    return fetchJSON(`/api/data/tasks/${encodeURIComponent(taskId)}`);
  }

  async function pauseDataTask(taskId) {
    return fetchJSON(`/api/data/tasks/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
  }

  async function resumeDataTask(taskId) {
    return fetchJSON(`/api/data/tasks/${encodeURIComponent(taskId)}/resume`, { method: "POST" });
  }

  async function cancelDataTask(taskId) {
    return fetchJSON(`/api/data/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
  }

  async function getDataStats() {
    return fetchJSON("/api/data/stats");
  }

  async function getDataIndices() {
    return fetchJSON("/api/data/indices");
  }

  async function getHotNews(category, limit) {
    let url = `/api/news/hot?limit=${limit || 50}`;
    if (category && category !== '全部') url += `&category=${encodeURIComponent(category)}`;
    return fetchJSON(url);
  }

  async function getStockNews(code, limit) {
    return fetchJSON(`/api/news/stock/${encodeURIComponent(code)}?limit=${limit || 20}`);
  }

  // ============================================================
  // 资金流向 API
  // ============================================================
  async function getSectorFlow(limit, indicator) {
    const params = new URLSearchParams();
    if (limit) params.set('limit', limit);
    if (indicator) params.set('indicator', indicator);
    return fetchJSON(`/api/flow/sector?${params}`);
  }

  async function getStockFlow(code) {
    return fetchJSON(`/api/flow/stock/${encodeURIComponent(code)}`);
  }

  async function getNorthFlow(limit) {
    return fetchJSON(`/api/flow/north?limit=${limit || 30}`);
  }

  // ============================================================
  // 回答溯源 · 执行归因（attribution-panel）
  // ============================================================
  async function getRuns(dialogUuid) {
    return fetchJSON(`/api/dialogs/${encodeURIComponent(dialogUuid)}/runs`);
  }

  async function getAttribution(dialogUuid, runId) {
    const params = new URLSearchParams();
    if (runId) params.set("run_id", runId);
    return fetchJSON(`/api/dialogs/${encodeURIComponent(dialogUuid)}/attribution?${params}`);
  }

  return { fetchJSON, getDialogs, createDialog, getMessages, sendMessage, getHealth, getModes, getDataSources, getTask, getTraceSteps, getTraceChain, getDialogTrace, getCorrelation, getStepDetail, getConfig, saveConfig, applyConfig, getNotificationStatus, testNotification, sendNotification, getReportTemplates, getReportTemplate, saveReportTemplate, getDashboards, getDashboard, saveDashboard, getDashboardAssociations, saveDashboardAssociations, importDashboard, deleteDialog, sendBudgetDecision, getCalendar, getWatchlist, addWatchlist, deleteWatchlist, syncWatchlist, getWatchlistQuotes, getWatchlistQuote, getKline, syncKline, getSignals, searchChartStock, scanStrategySignals, getRouteRules, saveRouteRules, routeTest, getBacktestStrategies, saveBacktestStrategy, deleteBacktestStrategy, runBacktest, getBacktestRuns, getBacktestRunDetail, getBacktestKline, deleteBacktestRun, streamCleanFailed, runBatchBacktest, listBatches, getBatch, deleteBatch, rerunBatchItem, rerunBatchFailed, streamBatchProgress, runMultiBacktest, getMulti, listMulti, deleteMulti, streamMultiProgress, listTestSets, createTestSet, getTestSet, updateTestSet, deleteTestSet, createDataTask, getDataTasks, getDataTask, pauseDataTask, resumeDataTask, cancelDataTask, getDataStats, getDataIndices, getHotNews, getStockNews, getSectorFlow, getStockFlow, getNorthFlow, getRuns, getAttribution };
})();
