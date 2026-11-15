# -*- coding: utf-8 -*-
"""DeepSeek 搜索 Provider（官方自研搜索引擎）

接口：`POST https://api.deepseek.com/v1/tools/search`
文档：https://api-docs.deepseek.com/zh-cn/guides/search

## 为什么值得单独接一个源（2026-10-18）

官方自研引擎，返回**带排序分与权威度**（`rerank_score` 0~3 / `authority` 0~4）的结构化结果，
并且 `summary` 字段是 600~800 字符的「长摘要」——文档原话「适用于给模型阅读」，这正是本项目
多源聚合链路一直缺的东西（其他源给的是 100~200 字符的短摘要，模型经常看不出所以然）。
国内网络直达，无需像 Parallel/Exa 那样走境外托管 MCP。

## 计费与限流（接手前必读）

- **按请求次数**计费：¥5 / 1000 次成功请求 ≈ ¥0.005/次。只有 HTTP 200 且 `results` 非空才计费，
  无结果/失败/超时**不计费** —— 所以「无结果」这条路径必须如实回传，别谎报成有结果（那会白花钱），
  也别把它记成 Key 故障（见 `_do_search` 里那段关于 `success` 语义的注释）。
- `count` 只控制返回条数，**不影响单价** → 固定多要（`max_results * 2`，上限 50）。
- QPS 5，且是**账号级**（文档明确「与使用哪个 API Key 无关」）→ 多 Key 轮换绕不过去，
  必须靠令牌桶限流；桶是模块级单例，多个 provider 实例共享。
- ⚠️ 拿到令牌失败时**快速失败让位**给下一个源，不要在这里阻塞等待 —— 聚合器是 first-good-wins，
  超时/阻塞会拖垮整条链（`run_web_search` 给整条链的总超时只有 30s）。

## 不做客户端过滤（有意为之）

`rerank_score < 1.5` 的结果质量偏低，但**不能丢**：丢了会让本次返回条数不足 `max_results`，
聚合器便继续往下游要，反而多花钱。质量筛选交给消费方（报告层）看分数自己判断。
"""
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from .base import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger("radar.search")

# 文档：query 长度上限 128 个 Unicode 字符（追加上时间过滤后也不能超）
MAX_QUERY_CHARS = 128
# 文档：count 取值 1~50
MAX_COUNT = 50
# 文档：Search 接口账号级 5 QPS
QPS_LIMIT = 5.0
# 单次搜索超时。**必须明显小于** `run_web_search` 给整条链的总超时（30s）：
# 本源排在链首，它若占满 20s，后面的兜底源就没时间跑了。
SEARCH_TIMEOUT_S = 15

_LIMITER = None
_LIMITER_LOCK = threading.Lock()


def _get_limiter():
    """账号级 5 QPS 令牌桶（模块级单例，跨 provider 实例共享）。"""
    global _LIMITER
    if _LIMITER is None:
        with _LIMITER_LOCK:
            if _LIMITER is None:
                from tools.fetcher.rate_limiter import TokenBucketRateLimiter
                _LIMITER = TokenBucketRateLimiter(rate=QPS_LIMIT, capacity=5)
    return _LIMITER


def _normalize_base(raw: str) -> str:
    """把用户填的 base 归一成 `https://api.deepseek.com` 这种「不含 /v1 的前缀」。

    用户可能填的东西五花八门：官方 Base URL（`https://api.deepseek.com`）、
    带版本号的（`.../v1`）、甚至直接把 LLM 的 OpenAI 兼容端点（`.../v1/chat/completions`）
    复制过来。这里统一剥掉尾部路径，避免拼出 `/v1/chat/completions/v1/tools/search`。
    """
    base = (raw or "").strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base or "https://api.deepseek.com"


def _default_base() -> str:
    return os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")


def _search_endpoint() -> str:
    return f"{_normalize_base(_default_base())}/v1/tools/search"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("false", "0", "no")


def _apply_days(query: str, days) -> str:
    """把通用 `days` 时效参数映射成 DeepSeek 的 `after:` 搜索语法。

    档位与其它源保持一致（SKILL.md 口径：≤1→24小时 / ≤7→一周 / ≤30→一月 / 更大→一年）。
    query 有 128 字符上限，追加会超长时**放弃过滤而不是截断用户的词**。
    """
    if not days:
        return query
    try:
        n = int(days)
    except (TypeError, ValueError):
        return query
    if n <= 0:
        return query
    if n <= 1:
        n = 1
    elif n <= 7:
        n = 7
    elif n <= 30:
        n = 30
    elif n <= 365:
        n = 365
    else:
        return query                      # 「一年以上」= 不加时间过滤，与 SearXNG year 档一致

    since = (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")
    op = f" after:{since}"
    if len(query) + len(op) > MAX_QUERY_CHARS:
        logger.debug("[DeepSeek] query 接近长度上限，放弃 after: 时间过滤")
        return query
    return query + op


def _parse_error(response) -> str:
    """把 DeepSeek 的 OpenAI 兼容错误体翻成给模型看的中文原因。

    文档口径：400 请求体不合法、401 Key 无效、402 余额不足、403 安全策略拒绝（Fetch）、
    404 抓不到内容（Fetch）、408 读体超时、413 体过大、429 超 QPS、5xx/504 服务端问题。
    """
    detail = ""
    try:
        payload = response.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or err.get("code") or ""
        elif isinstance(payload, dict):
            detail = payload.get("message") or ""
    except Exception:
        detail = ""

    code = response.status_code
    hint = {
        400: "请求参数不合法（不要重试同一请求）",
        401: "DeepSeek API Key 缺失或无效",
        402: "DeepSeek 账号余额不足（请充值后重试）",
        403: "请求被 DeepSeek 安全策略拒绝",
        404: "未能获取可用的网页内容",
        408: "读取请求体超时（可重试）",
        413: "请求体超过大小限制",
        429: "超过 DeepSeek 账号级 QPS 限制（Search/Fetch 各 5 QPS，可重试）",
    }.get(code)
    if not hint:
        hint = "DeepSeek 服务端错误（可重试）" if code >= 500 else f"HTTP {code}"

    suffix = f"：{detail}" if detail else ""
    return f"{hint}{suffix}"


class DeepSeekSearchProvider(BaseSearchProvider):
    """DeepSeek 官方搜索（Search API）"""

    name = "DeepSeek"

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys)

    def _do_search(self, query: str, api_key: str, max_results: int, **kwargs) -> SearchResponse:
        limiter = _get_limiter()
        if not limiter.acquire():
            wait = limiter.wait_time()
            return SearchResponse(
                query=query, results=[], provider=self.name, success=False,
                error_message=f"触发 DeepSeek 账号级 QPS 限流（约 {wait:.1f}s 后恢复），本次跳过",
            )

        count = max(1, min(int(max_results or 5) * 2, MAX_COUNT))
        payload = {
            "query": _apply_days(query, kwargs.get("days")),
            "count": count,
            # 正文交给 web_fetch 按需抓（search 里带正文又贵又占上下文，且条数多时必然超预算）
            "need_content": _bool_env("DEEPSEEK_SEARCH_NEED_CONTENT", False),
        }
        ori_question = kwargs.get("ori_question")
        if ori_question:
            payload["ori_question"] = str(ori_question)[:128]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                _search_endpoint(),
                headers=headers,
                json=payload,
                timeout=SEARCH_TIMEOUT_S,
            )
        except requests.exceptions.Timeout:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message="请求超时（可重试一次）",
            )
        except requests.exceptions.RequestException as e:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=f"网络请求失败: {e}",
            )

        if response.status_code != 200:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=_parse_error(response),
            )

        try:
            data = response.json() or {}
        except Exception as e:
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=False, error_message=f"响应不是合法 JSON: {e}",
            )

        items = data.get("results") or []
        results = [r for r in (self._to_result(it) for it in items[:count]) if r.url]

        if not results:
            # ⚠️ 「搜索成功但无匹配」必须回 `success=True`（与 Tavily 同款，与 MX 刻意不同）：
            # 基类 `search()` 用 `success` 判定**Key 健康度**，连续 3 次 False 会把 Key 打入冷宫。
            # 空结果是上游的正常结局（也不计费），不是 Key 坏 —— 若回 False，用户搜几个冷门词
            # 就会看到「DeepSeek 源突然消失」，正是本项目最忌讳的那种静默失效。
            # 失败原因仍通过 `error_message` 带出，聚合器照旧把它计入逐源原因清单。
            return SearchResponse(
                query=query, results=[], provider=self.name,
                success=True, error_message="搜索正常结束但无匹配结果（未计费）",
            )

        logger.info(f"[DeepSeek] 命中 {len(results)} 条（含长摘要），已计费 1 次搜索请求")
        return SearchResponse(query=query, results=results, provider=self.name, success=True,
                              billed_service="deepseek-search")

    def _to_result(self, item: dict) -> SearchResult:
        """字段映射。

        `snippet` 取 **summary（600~800 字符长摘要）而非 snippet（100~200 字符短摘要）**：
        本源的卖点就是「给模型读的长摘要」，短摘要与其它源无差别。与其它源一致截到 500 字符，
        避免单条结果挤爆上下文（报告层另有总预算）。
        """
        if not isinstance(item, dict):
            return SearchResult(title="", snippet="", url="", source="")
        url = str(item.get("url") or "")
        text = str(item.get("summary") or item.get("snippet") or "")
        return SearchResult(
            title=str(item.get("title") or "").strip(),
            snippet=text[:500],
            url=url,
            source=str(item.get("site_name") or self._extract_domain(url) or "DeepSeek"),
            published_date=self._normalize_date(item.get("date")),
        )

    @staticmethod
    def _normalize_date(raw) -> Optional[str]:
        """发布时间归一成 `YYYY-MM-DD`（上游是 UTC RFC3339），缺失给 None。

        保留完整 ISO 串会把 `+00:00` 一路带进报告正文；报告只需要「哪天发布的」。
        """
        if not raw:
            return None
        text = str(raw).strip()
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
        return m.group(1) if m else (text or None)
