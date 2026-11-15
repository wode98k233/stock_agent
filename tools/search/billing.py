# -*- coding: utf-8 -*-
"""工具返回载荷 → 按次计费信息（纯函数，供工具节点回填成本用）。

## 契约

`web_search` / `search_stock_news` / `web_fetch` 的返回 payload 带 `billed_service`
（值 = 价目 `services` 段的键，如 `deepseek-search`）。**非空即表示本次调用已让上游按次计费**：
搜索走聚合链路时只有 DeepSeek 源收费（MX/Parallel/Exa/SearXNG 免 Key 免费），
若命中的是免费源，`billed_service` 为空 → 不记成本（也确实是 0 元）。

## 为什么在工具节点解析载荷，而不是让 provider 自己记账

provider 只知道自己发了一次 HTTP 请求，**拿不到**「这是哪次工具调用、属于哪个 run/step」；
而工具节点已经有一整套「按 tool_call_id 回填 step extra」的机制（压缩埋点就在同一处）。
把「计费事实」顺着返回值带出来，是这两层之间唯一不需要新增全局状态的通道。

## 为什么要有 allowlist

载荷解析不能撒网：其它工具的返回值也可能是 JSON，随口 `json.loads` 既慢又容易误判。
这里只认下面的工具名，且值必须命中 `KNOWN_SERVICES`。
"""
import json
import logging
from typing import Optional

logger = logging.getLogger("radar.search")

# 载荷带 `billed_service` 契约的工具（其余工具一律不解析）
PAYLOAD_TOOLS = frozenset({"web_search", "search_stock_news", "web_fetch"})

# 已知的按次计费服务（价目 `services` 段的键）；未登记的值不放行
KNOWN_SERVICES = frozenset({"deepseek-search", "deepseek-fetch"})

# 载荷长度上限：正常载荷几 KB；远超说明不是我们的契约载荷（或已被压缩/截断），直接放弃解析
_MAX_PAYLOAD_CHARS = 200_000


def billed_service_of(tool_name: str, content) -> Optional[dict]:
    """从工具返回值里解析出「本次已计费」的服务。

    Returns:
        `{"service": "deepseek-search", "count": 1}`；未计费/不是契约载荷 → None
    """
    if str(tool_name or "") not in PAYLOAD_TOOLS:
        return None
    text = content if isinstance(content, str) else str(content or "")
    if not text or len(text) > _MAX_PAYLOAD_CHARS:
        return None
    # 廉价的快速路径：99% 的工具输出不含这个键，不必付 json.loads 的代价
    if "billed_service" not in text:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    service = str(payload.get("billed_service") or "").strip()
    if not service:
        return None
    if service not in KNOWN_SERVICES:
        logger.warning(f"未知的按次计费服务「{service}」，本次不记成本"
                       f"（价目 services 段里没有它）")
        return None
    return {"service": service, "count": 1}
