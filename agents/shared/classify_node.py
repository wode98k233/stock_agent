"""共享 Classifier 节点 — 统一三种模式的输入分类逻辑

所有模式的核心分类逻辑完全相同：调用 classify_input()，
根据返回的 response 字段判断是否股票相关。
差异仅在于返回格式和附加操作（进度上报等）。
"""
from agents.agent_context import AgentContext
from utils.logger import ensure_radar


class ClassifyNode:
    """共享分类节点（class + __call__ 模式）

    返回标准化格式：
    - 非股票相关: {"is_stock_related": False, "response": "..."}
    - 股票相关:   {"is_stock_related": True, "response": None}
    """

    def __init__(self, llm, memory, budget=None, logger=None):
        self.llm = llm
        self.memory = memory
        self.budget = budget
        self.logger = ensure_radar(logger)

    async def __call__(self, user_input: str, run_config=None) -> dict:
        """执行分类，返回标准化结果"""
        from agents.common import classify_input

        result = await classify_input(
            user_input, self.memory, self.llm, self.logger,
            budget=self.budget, run_config=run_config,
        )
        if result.get("response"):
            return {"is_stock_related": False, "response": result["response"]}
        return {"is_stock_related": True, "response": None}


def make_classify_from_ctx(ctx: AgentContext):
    """从 AgentContext 创建 ClassifyNode 实例（Plan/PDOR 共用）"""
    from utils.llm_factory import get_llm
    llm = get_llm()
    return ClassifyNode(llm=llm, memory=ctx.memory, logger=ctx.logger)
