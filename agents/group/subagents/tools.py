"""Sub-agent 向调度 Agent 请求信息的工具"""
from langchain_core.tools import tool


class RequestInfoException(Exception):
    """触发 common_react 中断，返回 need_info 状态"""
    def __init__(self, target: str, description: str, required: bool = True):
        self.target = target
        self.description = description
        self.required = required
        super().__init__(f"请求 {target}: {description}")


@tool
def request_info(target: str, description: str, required: bool = True) -> str:
    """向调度 Agent 请求信息。调用后当前执行中断，等待调度 Agent 满足请求后重试。

    Args:
        target: 目标 agent 名称（如 technical_analyst, macro_analyst, chain_analyst）
        description: 具体需要什么信息
        required: 是否必须满足才能继续（默认 True）
    """
    raise RequestInfoException(target=target, description=description, required=required)
