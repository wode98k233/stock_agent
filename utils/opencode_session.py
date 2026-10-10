"""
选股雷达 - OpenCode Go 会话路由（x-opencode-session）

背景：OpenCode Go（opencode.ai "Console Go" 套餐）的 OpenAI 兼容端点要求每个请求
携带 ``x-opencode-session`` 请求头（UUID v4）：
- 同一会话内多轮请求带同一个 ID → 路由到同一台 GPU、复用 prompt 缓存；
- 不同会话用不同 ID → 避免互相串会话 / 触发风控；
- 服务端只认不验，与 Authorization（Bearer）互不冲突。

本项目落地方式：
- 每个 agent run（用户的一个题目）开始时调用 :func:`new_session` 生成新 UUID，
  该次 run 内所有 LLM 请求共用同一个 ID（ContextVar 隔离线程/协程）；
- 仅当 API base_url 指向 opencode.ai 时才注入该请求头，其余 provider 不打扰。
"""
import contextvars
import uuid
from typing import Optional

_session_uid: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "opencode_session_uid", default=None
)

# opencode 域后缀（endpoint host 以此结尾即判定为 opencode）
_OPENCODE_HOST_SUFFIX = "opencode.ai"


def get_or_create_session_id() -> str:
    """返回当前会话 UUID；未设置时惰性生成并绑定到当前 context。"""
    sid = _session_uid.get()
    if not sid:
        sid = str(uuid.uuid4())
        _session_uid.set(sid)
    return sid


def new_session() -> str:
    """开启新会话：生成新 UUID（新题目/新对话时调用）。"""
    sid = str(uuid.uuid4())
    _session_uid.set(sid)
    return sid


def reset_session() -> None:
    """清除当前会话 UUID（下次调用会重新生成）。"""
    _session_uid.set(None)


def is_opencode_endpoint(base_url: Optional[str]) -> bool:
    """判断 API base_url 是否为 opencode（Console Go）端点。"""
    if not base_url:
        return False
    host = str(base_url).lower()
    stripped = host.split("://", 1)[-1] if "://" in host else host
    return stripped.startswith(_OPENCODE_HOST_SUFFIX) or stripped.endswith("." + _OPENCODE_HOST_SUFFIX)


def add_session_header(payload: dict, base_url: Optional[str]) -> dict:
    """为 opencode 端点的请求 payload 注入 x-opencode-session 请求头。

    - 非 opencode 端点原样返回（零改动）；
    - 用户显式配置了该请求头时尊重用户值，不覆盖。
    返回新的 payload dict（不原地修改）。
    """
    if not is_opencode_endpoint(base_url):
        return payload
    headers = dict(payload.get("extra_headers") or {})
    if "x-opencode-session" not in headers:
        headers["x-opencode-session"] = get_or_create_session_id()
    return {**payload, "extra_headers": headers}