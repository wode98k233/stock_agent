window.StockRadar = window.StockRadar || {};

window.StockRadar.utils = (() => {
  const _scriptPromises = {};

  const TYPE_LABELS = {
    start: "启动", step_start: "步骤", step_complete: "完成",
    tool_call: "工具", tool_result: "结果", milestone: "里程碑",
    warning: "警告", error: "错误", final: "结束",
    classifier: "分类", planner: "规划", replanner: "重规划",
    llm_call: "LLM",
    group_plan: "调度规划", group_step_start: "调度执行",
    group_step_result: "调度结果", group_observe: "调度观察",
    group_adjust: "调度调整", group_replan: "调度重规划",
    group_resolve: "调度完成",
    memory_context: "记忆上下文",
  };

  const TRACE_ICONS = {
    llm: "🤖", tool: "🔧", chain: "🔗", retriever: "📄",
    agent: "🤖", tools: "🔧", planner: "📋", executor: "⚡",
    replanner: "🔄", classifier: "🏷️", graph: "🌳",
    unified_executor: "🚀",
  };

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function loadScriptOnce(src, globalName) {
    if (globalName && window[globalName]) {
      return Promise.resolve(window[globalName]);
    }
    if (_scriptPromises[src]) return _scriptPromises[src];
    _scriptPromises[src] = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = () => resolve(globalName ? window[globalName] : true);
      script.onerror = () => {
        delete _scriptPromises[src];
        reject(new Error(`script load failed: ${src}`));
      };
      document.head.appendChild(script);
    });
    return _scriptPromises[src];
  }

  return { esc, TYPE_LABELS, TRACE_ICONS, loadScriptOnce };
})();
