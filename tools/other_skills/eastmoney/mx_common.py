"""妙想（MX）API 公共请求层：全局节流 + 限流退避重试。

背景（2026-09-17 实测）：同一 run 内多个查询并发/密集调用会触发
`状态码 122 - 操作过于频繁`；原实现为单次 POST、失败即返回错误，
导致 agent 反复换词补查仍拿不到数据（trace run 5db7e884）。

策略：
- 进程级最小间隔（默认 1.5s）：mx_data / mx_search / mx_xuangu 共享同一后端配额；
- HTTP 429 与业务层「操作过于频繁」退避重试（等待 2s、5s，最多 2 次）；
- **额度耗尽（122 的另一子类）不重试**：额度按日/周恢复，重试必然失败，
  只会额外打 2 次请求并空等 7s 退避（2026-09-20 实测：5 次调用打 15 次请求，
  单 run 因此浪费 96.6s，占 run 时长 66%）；
- 其他 HTTP 异常维持原行为（立即抛出，由调用方降级）。

**2026-09-20 二次实测修正（额度判定的真实形态）**
-----------------------------------------------
额度耗尽的响应**并不抛错、顶层 status 也是 0**，形态是：

    {"status": 0, "message": "ok",
     "data": {"message": "OK",
              "data": {"message": "您本周的…数据量已达到上限，额度将于次周恢复…",
                       "searchDataResultDTO": {"dataTableDTOList": []}}}}

因此：
- 额度原文只在 **`data.data.message`**，只看顶层 `message`（"ok"）会漏判
  → 判定函数必须逐层下钻（见 `_payload_texts`）；
- 上层若只按 `status != 0` 分支判断，会把额度场景错报成「查无数据」；
- 该漏判会让本模块上面的「额度耗尽不重试」失效（白打 2 次请求 + 空等 7s）。
"""
from __future__ import annotations

import threading
import time

import requests

MIN_INTERVAL_S = 1.5
MAX_RETRIES = 2
RETRY_SLEEPS = (2.0, 5.0)

# 额度耗尽特征（不可重试）
_QUOTA_MARKERS = (
    "数据量已达到上限",
    "次数已达到上限",
    "额度已达到上限",
    "已达到上限",
    "额度将于",          # 「额度将于次周恢复」/「额度将于次日恢复」
    "额度已用完",
)

# 频控特征（可重试）
_RATE_LIMIT_MARKERS = (
    "操作过于频繁",
    "请求频率过高",
    "请求过于频繁",
    "too many requests",
)

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def as_dict(value) -> dict:
    """安全取字典，用于收敛妙想响应的链式访问。

    `dict.get(key, {})` 的默认值**只在键缺失时生效**；妙想 API 在
    「查不出 / 无结果 / 额度末期」时会显式返回 `"data": null` 或
    `"data": {"data": null}` —— 键存在、值是 None，于是下一级 `.get`
    抛 `AttributeError: 'NoneType' object has no attribute 'get'`。

    该异常被各 skill 的 except 捕获后塞进 `{"status": "failed"}` 正常返回，
    既不触发 LangChain 的 on_tool_error，也无法在 trace 上定位（见
    docs/prd/2026-09-20-agent-harness扩展方案建议.md §2.3/§2.5）。
    统一用本函数替代链式 `.get`。
    """
    return value if isinstance(value, dict) else {}


def _throttle() -> None:
    """进程级最小请求间隔，抑制突发（并发批次逐个放行）。"""
    global _last_request_at
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_request_at + MIN_INTERVAL_S - now
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _payload_texts(payload) -> list:
    """取出响应里可能承载错误文案的字段（**逐层下钻 `data`**）。

    妙想真实的额度提示落在 **`data.data.message`**：顶层 `message` 是 `"ok"`、
    `data.message` 是 `"OK"`，只有最内层才带「数据量已达到上限」原文。
    实测（2026-09-20）额度耗尽的响应形态：

        {"status": 0, "message": "ok",
         "data": {"message": "OK",
                  "data": {"message": "您本周的…数据量已达到上限，额度将于次周恢复…",
                           "searchDataResultDTO": {"dataTableDTOList": []}}}}

    只取顶层会让 `is_quota_exhausted` 漏判为 False → 额度耗尽仍重试 2 次并空等 7s，
    把本模块 docstring 里那处修复（5 次调用打 15 次请求 / 浪费 96.6s）架空了。
    """
    if not isinstance(payload, dict):
        return [str(payload or "")]

    texts = [
        str(payload.get("message") or ""),
        str(payload.get("status_msg") or ""),
    ]
    node = payload
    for _ in range(3):  # data / data.data / data.data.data
        node = node.get("data") if isinstance(node, dict) else None
        if not isinstance(node, dict):
            break
        texts.append(str(node.get("message") or ""))
        texts.append(str(node.get("status_msg") or ""))
    return texts


def is_quota_exhausted(payload) -> bool:
    """识别「额度 / 数据量耗尽」（status=122 的另一子类），**不可重试**。

    与「操作过于频繁」的区别：额度按日（次日）/ 周（次周）恢复，重试不会成功，
    只会多打请求并空等退避。post_json 在重试判定前先检查本函数。

    注意：额度耗尽的响应**可能顶层 `status == 0`**（只在 `data.data.message` 里带提示），
    所以不能只看 status，必须扫描各层文案。
    """
    return any(
        any(marker in text for marker in _QUOTA_MARKERS)
        for text in _payload_texts(payload)
    )


def quota_message(payload) -> str:
    """返回命中的额度提示原文（未命中返回空串），供上层拼进可读错误。"""
    for text in _payload_texts(payload):
        if any(marker in text for marker in _QUOTA_MARKERS):
            return text.strip()
    return ""


def is_rate_limited(payload) -> bool:
    """识别妙想限流响应（status=122 / "操作过于频繁"）。

    注意：status=122 同时覆盖「操作过于频繁」（可重试）与「额度耗尽」（不可重试），
    调用方应先用 `is_quota_exhausted` 排除后者再决定是否重试。
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("status") == 122:
        return True
    data = payload.get("data")
    if isinstance(data, dict) and data.get("status_code") == 122:
        return True
    return any(
        any(marker in text for marker in _RATE_LIMIT_MARKERS)
        for text in _payload_texts(payload)
    )


def _retry_sleep(attempt: int) -> None:
    time.sleep(RETRY_SLEEPS[min(attempt, len(RETRY_SLEEPS) - 1)])


def post_json(url: str, headers: dict, payload: dict, timeout: int = 30):
    """POST JSON 并解析：全局节流 + 429/频控退避重试；仍失败则抛出。"""
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code == 429 and attempt < MAX_RETRIES:
            _retry_sleep(attempt)
            continue
        response.raise_for_status()
        result = response.json()
        # 额度耗尽：直接返回，重试无意义（省下 2 次请求 + 7s 空等）
        if is_quota_exhausted(result):
            return result
        if is_rate_limited(result) and attempt < MAX_RETRIES:
            _retry_sleep(attempt)
            continue
        return result
