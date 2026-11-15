# -*- coding: utf-8 -*-
"""DeepSeek 网页抓取（Fetch API）

接口：`POST https://api.deepseek.com/v1/tools/fetch` → `{title, content}`
文档：https://api-docs.deepseek.com/zh-cn/guides/search

## 为什么需要它（2026-10-18）

在此之前全项目**没有任何「URL → 正文」的能力**：`web_search` / `search_stock_news` 只能
拿到 100~200 字符的短摘要，报告里凡是「原文怎么说」「公告/政策原文要点」都得靠模型脑补。
搜索返回 `url` + 本模块返回正文，两者配合才是完整的「检索 → 阅读」闭环。

## 计费与限流

与其他源一样按次计费（¥5 / 1000 次成功请求），**只有 HTTP 200 且拿到正文才算钱**，
因此同 URL 必须缓存（同一轮里模型常会重复抓同一篇），且 404/403 这类拿不到正文的失败不计费。
QPS 5 且与 Search 额度**互相独立** → 本模块自己一个令牌桶，不复用搜索那个。

## SSRF 口径（有意只做轻量预检）

真正的网络连接是 **DeepSeek 的服务器**发起的，不是我们这台机器 —— 所以「打到内网」这个
风险的暴露面在对方，不在我们。我们做预检只为两件事：① 别把明显不合法的 URL 花一次钱送出去；
② 别让被注入文案操纵的模型去指挥上游抓 `127.0.0.1` / 云元数据地址这类东西。
**因此不做 DNS 解析级别的 SSRF 防御**（那要引入网络依赖与测试 mock，收益却落在别人的边界上）；
上游拦下来了会返回 403，如实回传即可。
"""
import ipaddress
import logging
import os
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger("radar.search")

FETCH_ENDPOINT_SUFFIX = "/v1/tools/fetch"
FETCH_TIMEOUT_S = 30
# 文档：url 长度不超过 8192
MAX_URL_CHARS = 8192
# 正文截断的下限/上限（超过上限的请求一律按上限处理，避免把上下文一次塞爆）
MIN_MAX_CHARS = 500
HARD_MAX_CHARS = 20000
DEFAULT_MAX_CHARS = 8000
CACHE_TTL_S = 600
CACHE_MAX_ENTRIES = 64

# 明确拒绝的主机名（云元数据、本机名）；IP 字面量另有私有网段判定
_BLOCKED_HOSTS = {
    "localhost", "metadata", "metadata.google.internal", "instance-data",
}

FETCH_FALLBACK_HINT = (
    "本次未取到网页正文（error 给出真实原因：URL 不合法 / 被站点策略拦截 / 404 / 限流 / 余额不足）。"
    "请按顺序做两件事：① 若是「换一篇」能解决（404、被拦截），用 web_search 的 summary 或换一个来源链接，"
    "不要对同一 URL 反复重试；② 若 error 指向配额/余额/限流，按提示等待或充值，"
    "并在报告里**只标注一次**「该来源未取到原文」，不要用推测填充原文内容。"
)

_LIMITER = None
_LIMITER_LOCK = threading.Lock()
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _get_limiter():
    """Fetch 接口账号级 5 QPS 令牌桶（与 Search 额度独立，故各自一个桶）。"""
    global _LIMITER
    if _LIMITER is None:
        with _LIMITER_LOCK:
            if _LIMITER is None:
                from tools.fetcher.rate_limiter import TokenBucketRateLimiter
                _LIMITER = TokenBucketRateLimiter(rate=5.0, capacity=5)
    return _LIMITER


def _normalize_base(raw: str) -> str:
    base = (raw or "").strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base or "https://api.deepseek.com"


def _fetch_endpoint() -> str:
    return f"{_normalize_base(os.getenv('DEEPSEEK_API_BASE', 'https://api.deepseek.com'))}{FETCH_ENDPOINT_SUFFIX}"


def _api_keys() -> list:
    raw = os.getenv("DEEPSEEK_API_KEYS", "") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def _resolve_max_chars(max_chars) -> int:
    """正文上限：显式入参 > 环境变量 > 默认；统一夹到 [MIN, HARD_MAX]。"""
    raw = max_chars
    if raw is None:
        raw = os.getenv("DEEPSEEK_FETCH_MAX_CHARS", "") or DEFAULT_MAX_CHARS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_CHARS
    return max(MIN_MAX_CHARS, min(n, HARD_MAX_CHARS))


def _is_private_ip(host: str) -> bool:
    """IP 字面量是否落在私有/回环/链路本地/保留段（含云元数据 169.254.169.254）。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def check_url(url) -> Optional[str]:
    """预检 URL；合法返回 None，不合法返回中文原因（纯函数，可单测）。"""
    if not url or not isinstance(url, str):
        return "url 不能为空"
    text = url.strip()
    if not text:
        return "url 不能为空"
    if len(text) > MAX_URL_CHARS:
        return f"url 超长（上限 {MAX_URL_CHARS} 字符）"

    try:
        parsed = urlparse(text)
    except Exception as e:
        return f"url 解析失败: {e}"

    if parsed.scheme not in ("http", "https"):
        return f"仅支持 http/https，收到 {parsed.scheme or '空'} 协议"
    host = (parsed.hostname or "").strip()
    if not host:
        return "url 缺少主机名"
    if parsed.username or parsed.password:
        return "url 不能携带用户名或密码"
    if host.lower() in _BLOCKED_HOSTS or host.lower().endswith(".localhost"):
        return f"拒绝抓取本机/元数据地址（{host}）"
    if _is_private_ip(host):
        return f"拒绝抓取内网/链路本地地址（{host}）"
    return None


def _cache_get(url: str) -> Optional[dict]:
    with _CACHE_LOCK:
        item = _CACHE.get(url)
        if not item:
            return None
        ts, payload = item
        if time.time() - ts >= CACHE_TTL_S:
            _CACHE.pop(url, None)
            return None
        return payload


def _cache_put(url: str, payload: dict) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= CACHE_MAX_ENTRIES:
            # 简单地淘汰最旧的一批，避免无界增长
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: CACHE_MAX_ENTRIES // 4]:
                _CACHE.pop(k, None)
        _CACHE[url] = (time.time(), payload)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _parse_error(response) -> str:
    detail = ""
    try:
        payload = response.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or err.get("code") or ""
    except Exception:
        detail = ""

    code = response.status_code
    hint = {
        400: "请求体不合法（URL 不合法时上游亦返回 400）",
        401: "DeepSeek API Key 缺失或无效",
        402: "DeepSeek 账号余额不足（请充值后重试）",
        403: "被 DeepSeek 安全策略拒绝（该来源不可抓）",
        404: "未能获取可用的网页正文（URL 不可用/被站点拦截/无可提取内容）",
        408: "读取请求体超时（可重试）",
        413: "请求体超过大小限制",
        429: "超过 DeepSeek 账号级 QPS 限制（Fetch 5 QPS，可重试）",
    }.get(code)
    if not hint:
        hint = "DeepSeek 服务端错误（可重试）" if code >= 500 else f"HTTP {code}"
    return f"{hint}：{detail}" if detail else hint


def _failure(url: str, error: str, billed: bool = False) -> dict:
    return {
        "url": url,
        "success": False,
        "title": "",
        "content": "",
        "char_count": 0,
        "truncated": False,
        "billed": billed,
        "billed_service": "",
        "error": error,
        "hint": FETCH_FALLBACK_HINT,
    }


def fetch_web_page(url: str, max_chars: int = None, api_key: str = None) -> dict:
    """抓取单个网页正文（Markdown）。

    Args:
        url: 目标页面地址（http/https）
        max_chars: 正文上限，默认取 `DEEPSEEK_FETCH_MAX_CHARS`（8000），夹在 [500, 20000]
        api_key: 可选注入（测试用）；默认取 `DEEPSEEK_API_KEYS` 第一个

    Returns:
        `{url, success, title, content, char_count, truncated, billed, error?, hint?}`
        - `billed=True` 表示这一次**已向上游计费**（HTTP 200 且拿到正文）；
        - 失败/空正文不计费，`billed=False`；
        - 任何异常都吞成失败载荷，工具层永远拿到结构化 dict。
    """
    bad = check_url(url)
    if bad:
        return _failure(str(url or ""), bad)
    target = url.strip()

    cached = _cache_get(target)
    if cached is not None:
        # 缓存命中：本次没有发生上游调用，`billed` 与 `billed_service` 都归零（否则会重复记账）
        payload = dict(cached)
        payload["billed"] = False
        payload["billed_service"] = ""
        payload["cached"] = True
        return payload

    key = api_key or (lambda ks: ks[0] if ks else "")(_api_keys())
    if not key:
        return _failure(target, "DEEPSEEK_API_KEYS 未配置，抓取功能不可用")

    limiter = _get_limiter()
    if not limiter.acquire():
        return _failure(target, f"触发 DeepSeek Fetch 账号级 QPS 限流（约 {limiter.wait_time():.1f}s 后恢复）")

    try:
        response = requests.post(
            _fetch_endpoint(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"url": target},
            timeout=FETCH_TIMEOUT_S,
        )
    except requests.exceptions.Timeout:
        return _failure(target, "请求超时（可重试一次）")
    except requests.exceptions.RequestException as e:
        return _failure(target, f"网络请求失败: {e}")

    if response.status_code != 200:
        return _failure(target, _parse_error(response))

    try:
        data = response.json() or {}
    except Exception as e:
        return _failure(target, f"响应不是合法 JSON: {e}")

    content = str(data.get("content") or "")
    if not content.strip():
        # 上游只在「返回网页正文」时计费 → 空正文是未计费的成功空响应，别谎报成已计费
        return _failure(target, "页面没有可提取的 Markdown 正文（未计费）")

    limit = _resolve_max_chars(max_chars)
    truncated = len(content) > limit
    payload = {
        "url": target,
        "success": True,
        "title": str(data.get("title") or ""),
        "content": content[:limit],
        "char_count": len(content),
        "truncated": truncated,
        "billed": True,
        "billed_service": "deepseek-fetch",
    }
    _cache_put(target, payload)
    logger.info(f"[DeepSeek] 抓取成功 {target} → {len(content)} 字符"
                f"{'（已截断）' if truncated else ''}，已计费 1 次 Fetch 请求")
    return dict(payload)
