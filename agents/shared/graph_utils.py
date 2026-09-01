"""共享 graph 工具函数"""


def wrap_node(fn, ctx):
    """将 async 函数包装为 LangGraph 节点，注入 AgentContext

    Plan/PDOR 的 graph.py 都需要这个包装器，
    因为 LangGraph 节点签名是 (state, config)，而业务函数需要 ctx。
    """
    async def wrapper(state, config=None):
        return await fn(state, ctx, config)
    return wrapper
