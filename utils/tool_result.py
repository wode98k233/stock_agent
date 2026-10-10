"""工具调用结果的失败判定（trace 层专用）。

与 `agents/executor_callbacks.is_tool_success` 的区别（**刻意不复用**）：

| | `is_tool_success`（agent 层） | `is_tool_failure`（trace 层，本模块） |
|---|---|---|
| 回答的问题 | 这次调用**有没有拿到有用数据** | 这次调用**是不是失败了** |
| 空列表 `[]` | 否（不算有用） | **不算失败**（休市日 / 当日无匹配是正常结果） |
| 只有元信息 `{"code":...}` | 否（不算有用） | **不算失败** |
| `{"status":"failed",...}` | 否 | **是失败** |
| 用途 | ReAct 去重、失败快速重试判定 | 写 `steps.status` / `steps.error` |

若 trace 层直接复用 `is_tool_success`，休市日的一切查询都会被标成 error，
把真正的缺陷淹没——这正是引入本模块的原因（2026-09-20）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Tuple

# 文本形态的显式失败特征
_FAILURE_PREFIXES = ("执行失败", "执行异常", "error:", "exception:")
# 大小写敏感的中文/固定文案
_FAILURE_SUBSTRINGS = ("使用方式错误", "请勿重试", "不要重试", "已跳过", "当前不支持")
# 小写比较的特征（与 text.lower() 比较）
_FAILURE_LOWER_SUBSTRINGS = ("traceback",)
_FAILURE_PATTERN = re.compile(r"^工具 .{0,80}?失败")

_DEFAULT_REASON_MAX_LEN = 200


def _mapping_is_failure(value: Any) -> bool:
    """结构化结果的失败判定（只认显式失败信号）。"""
    if not isinstance(value, dict):
        # 列表本身不算失败：空列表是正常空结果，非空列表由其中条目自行表达
        return False
    if str(value.get("status", "")).lower() == "failed":
        return True
    error = value.get("error")
    if error:
        return True
    for key in ("result", "data"):
        child = value.get(key)
        if isinstance(child, dict) and str(child.get("status", "")).lower() == "failed":
            return True
    return False


def is_tool_failure(tool_output: Any) -> bool:
    """仅当工具输出**显式表示失败**时返回 True。

    刻意不把「空结果」判为失败：空列表/空字典代表「调用成功但无数据」，
    属于有效结果（数据源侧也以 inconclusive 而非 failure 计）。
    """
    if tool_output is None:
        return False

    # 已是结构化的，直接判定，不做 str() 往返
    if isinstance(tool_output, (dict, list)):
        return _mapping_is_failure(tool_output)

    text = str(tool_output).strip()
    if not text:
        return False

    lower = text.lower()
    if lower.startswith(_FAILURE_PREFIXES):
        return True
    if any(token in lower for token in _FAILURE_LOWER_SUBSTRINGS):
        return True
    if any(token in text for token in _FAILURE_SUBSTRINGS):
        return True
    if _FAILURE_PATTERN.search(text[:160]):
        return True

    try:
        parsed = json.loads(text)
    except Exception:
        return False
    return _mapping_is_failure(parsed)


def tool_failure_reason(tool_output: Any, max_len: int = _DEFAULT_REASON_MAX_LEN) -> str:
    """从失败的工具输出里提取可读原因，写入 `steps.error`，便于 trace 面板直接定位。"""
    if tool_output is None:
        return "工具返回 None"

    if isinstance(tool_output, dict):
        for key in ("error", "message", "detail", "reason"):
            value = tool_output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:max_len]
            if value:
                return str(value)[:max_len]
        return "工具返回 status=failed（未提供错误字段）"[:max_len]

    text = str(tool_output).strip()
    if not text:
        return "工具返回空结果"

    try:
        parsed = json.loads(text)
    except Exception:
        return text[:max_len]

    if isinstance(parsed, dict):
        for key in ("error", "message", "detail", "reason"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:max_len]
            if value:
                return str(value)[:max_len]
        if str(parsed.get("status", "")).lower() == "failed":
            return "工具返回 status=failed（未提供错误字段）"[:max_len]
    return text[:max_len]


def judge_tool_output(tool_output: Any) -> Tuple[bool, str]:
    """返回 (是否失败, 失败原因)。成功时原因为空串。"""
    if is_tool_failure(tool_output):
        return True, tool_failure_reason(tool_output)
    return False, ""
