/* ─────────────────────────────────────────────
   选股雷达 Web — 单次对话的成本 chip（成本 / 计价 §8②）

   数据来源有两条，口径完全一致（都来自 steps.extra.cost 的 SUM）：
   - 实时：SSE `final` 事件的 `cost` 字段；
   - 刷新/切回：web_messages.extra 里 type='cost' 的那一条。

   ⚠️ 「免费」与「未计价」必须区分显示：免费是 0 元且已计价，未计价是价目里没有它
   （显示 ¥0.00 会被误读成「算过了，不要钱」）。

   ⚠️ 两个口径**分别求和、合并展示**（2026-10-18 加）：`total`/`calls` 是 LLM token 成本与
   调用次数，`service_cost`/`service_calls` 是 Search/Fetch 这类**按请求次数**计费的服务。
   合并只发生在展示层（合计 + 明细），后端两个口径始终分开 —— 把服务费并进 `calls`
   会让「调用 N 次」「平均每次多少钱」全部失真。
   ───────────────────────────────────────────── */
window.StockRadar = window.StockRadar || {};

window.StockRadar.chatCost = (() => {
  function _fmt(cost) {
    if (!cost || typeof cost !== 'object') return '';
    const calls = Number(cost.calls || 0);
    const svcCalls = Number(cost.service_calls || 0);
    if (!calls && !svcCalls) return '';
    const unpriced = Number(cost.unpriced_calls || 0);
    const free = Number(cost.free_calls || 0);
    const svcUnpriced = Number(cost.service_unpriced_calls || 0);
    const total = Number(cost.total || 0) + Number(cost.service_cost || 0);

    /* ⚠️ 混合场景（有金额 + 未计价/免费）必须显式写出，否则「¥0.2721」
       会被读成「全部算过了」，实际有几次根本没进金额口径。
       原实现只在 total=0 时才提示，total>0 时把 unpriced/free 全藏进
       hover tooltip，等于静默漏报（实测 sensenova 免费源 6 次被吞）。 */
    if (!total && unpriced) return '本次 未计价（价目里没有该供应商/模型）';
    if (!total && free) return '本次 免费';
    /* 只有搜索/抓取、且它没进金额口径时，也要说清是哪一类没计价 */
    if (!total && svcCalls) return svcUnpriced ? '本次 未计价（搜索/抓取服务不在价目里）' : '本次 免费';

    const notes = [];
    if (unpriced) notes.push(`${unpriced} 次未计价`);
    if (free) notes.push(`${free} 次免费`);
    if (svcUnpriced) notes.push(`${svcUnpriced} 次搜索/抓取未计价`);
    const suffix = notes.length ? `（含 ${notes.join('、')}）` : '';
    return '本次 ≈ ¥' + total.toFixed(4) + suffix;
  }

  function _tip(cost) {
    const parts = [];
    const providers = cost.by_provider || {};
    Object.keys(providers).forEach((k) => {
      const v = Number(providers[k] || 0);
      /* 免费源会进 by_provider 但金额为 0，显示 ¥0.0000 会被误读成「有单价但算成 0」 */
      parts.push(k + (v ? ' ¥' + v.toFixed(4) : ' 免费'));
    });
    /* 按次计费的服务（搜索/抓取）：单列，别和 token 成本混成一项 */
    const services = cost.service_by_name || {};
    Object.keys(services).forEach((k) => {
      const v = services[k] || {};
      const amt = Number(v.cost || 0);
      parts.push(k + (amt ? ' ¥' + amt.toFixed(4) : ' 未计价') + ' ×' + Number(v.calls || 0) + ' 次');
    });
    const saved = Number(cost.cached_saved || 0);
    if (saved > 0) parts.push('缓存省 ¥' + saved.toFixed(4));
    if (cost.free_calls) parts.push('免费调用 ' + cost.free_calls + ' 次');
    if (cost.unpriced_calls) parts.push('未计价 ' + cost.unpriced_calls + ' 次');
    if (Number(cost.calls || 0)) parts.push('LLM 调用 ' + Number(cost.calls || 0) + ' 次');
    return parts.join(' · ');
  }

  /** 从 web_messages.extra（JSON 数组）里取成本条目 */
  function fromExtra(extra) {
    try {
      const list = typeof extra === 'string' ? JSON.parse(extra) : extra;
      if (!Array.isArray(list)) return null;
      return list.find((ev) => ev && ev.type === 'cost') || null;
    } catch (e) {
      return null;
    }
  }

  /** 在消息元素底部渲染成本 chip（幂等：同一条消息只渲染一次） */
  function render(target, cost) {
    if (!target || !cost) return null;
    const text = _fmt(cost);
    if (!text) return null;
    if (target.querySelector('.cost-chip')) return target.querySelector('.cost-chip');
    const chip = document.createElement('div');
    chip.className = 'cost-chip';
    chip.title = _tip(cost);
    chip.textContent = text;
    target.appendChild(chip);
    return chip;
  }

  /** 恢复路径：给第 idx 条消息渲染 chip（消息与 DOM 节点一一对应） */
  function renderForMessage(node, extra) {
    const cost = fromExtra(extra);
    if (cost) render(node, cost);
  }

  return { render, renderForMessage, fromExtra };
})();
