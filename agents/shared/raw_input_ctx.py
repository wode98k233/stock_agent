"""Web 请求的「原始用户提问」ContextVar。

agent.run 内部会把画像/记忆 enrich 进输入（state["input"]），但澄清反问
「都不对，我重新描述」需要回填**用户原话**到输入框；本 ContextVar 由
server.agent_runner 在 worker 内注入，agent 层（clarify）读取，拿不到时
回退到传入文本（CLI/TUI/测试天然兼容）。
"""
import contextvars
from typing import Optional

raw_input_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "raw_input_ctx", default=None
)


def set_raw_input(text: str):
    """设置原始提问，返回 token 供 reset。"""
    return raw_input_ctx.set(text)


def reset_raw_input(token) -> None:
    raw_input_ctx.reset(token)


def get_raw_input(fallback: str = "") -> str:
    return raw_input_ctx.get() or fallback
