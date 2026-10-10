"""Web 端会话标题回传的 ContextVar（仿 budget_ctx 模式）。

Web 首轮对话时由 server/agent_runner.py 注入 title_request：
- first_question: 会话首轮提问（标题生成依据，重试轮次仍取首轮文本）
- callback: 标题生成后回调（server 侧负责落库 + SSE 推送）

放 agents/shared/ 避免 server/ 与 agents/ 的循环依赖。
"""
import contextvars
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class TitleRequest:
    first_question: str
    callback: Callable[[str], None]


title_request_ctx: contextvars.ContextVar[Optional[TitleRequest]] = contextvars.ContextVar(
    'sr_title_request', default=None
)


def set_title_request(request: Optional[TitleRequest]):
    return title_request_ctx.set(request)


def reset_title_request(token) -> None:
    title_request_ctx.reset(token)


def get_title_request() -> Optional[TitleRequest]:
    return title_request_ctx.get()
