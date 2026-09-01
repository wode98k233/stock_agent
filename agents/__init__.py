"""
🤖 Agent 模块
延迟注册：首次调用 register_all() 时才导入各 Agent 实现
"""
import sys

# 注意：BaseAgent / AgentFactory 故意不在顶层 import —— agents.base 会经
# utils.memory 拉入 torch 全家桶（~7s）。顶层导入会让任何 `import agents.*`
# 都把 torch 链拉进来（包括 server 路由模块的构造期）。这里用 __getattr__ 懒加载，
# 仅在真正访问 agents.BaseAgent / agents.AgentFactory 时才触发 import。
def __getattr__(name):
    if name == "BaseAgent":
        from agents.base import BaseAgent
        return BaseAgent
    if name == "AgentFactory":
        from agents.factory import AgentFactory
        return AgentFactory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# 需要热重载的 agent 子模块前缀
_AGENT_MODULE_PREFIXES = (
    "agents.react.",
    "agents.plan.",
    "agents.pdor.",
    "agents.common_react.",
    "agents.shared.",
    "agents.analysis.",
    "agents.scenario",
    "agents.group.",
    "agents.common",
    "agents.base",
    "agents.agent_context",
    "agents.run_context",
    "agents.executor_callbacks",
    "agents.checkpoint_factory",
    "agents.loop_detector",
    "agents.user_decision",
)


def register_all():
    """注册所有 Agent（懒加载：只记名字和工厂函数，不 import 不实例化）。"""
    from agents.factory import AgentFactory
    AgentFactory.register(lambda: __import__("agents.react.agent", fromlist=["ReactStockAgent"]).ReactStockAgent(), name="react_stock")
    AgentFactory.register(lambda: __import__("agents.plan.plan_agents", fromlist=["PlanAndSolveAgent"]).PlanAndSolveAgent(), name="plan_solve")
    AgentFactory.register(lambda: __import__("agents.plan.plan_agents", fromlist=["UnifiedPlanAgent"]).UnifiedPlanAgent(), name="unified_plan")
    AgentFactory.register(lambda: __import__("agents.scenario_agent", fromlist=["ScenarioAgent"]).ScenarioAgent(), name="scenario")
    AgentFactory.register(lambda: __import__("agents.pdor.agent", fromlist=["PdorAgent"]).PdorAgent(), name="pdor")
    AgentFactory.register(lambda: __import__("agents.group.agent", fromlist=["GroupAgent"]).GroupAgent(), name="agent_group")


def reload_agents():
    """热重载 Agent 模块 — 清除缓存并重新注册，用于开发时代码修改即时生效。

    只清除 agents.* 下的模块，不动 tools/utils/config 等基础设施模块。
    """
    from agents.factory import AgentFactory
    # 1. 清除 AgentFactory 注册表
    AgentFactory._factories.clear()
    AgentFactory._instances.clear()

    # 2. 从 sys.modules 中清除 agents.* 子模块（保留 agents 包本身和 __init__）
    to_remove = []
    for mod_name in sys.modules:
        if mod_name == "agents" or mod_name.startswith("agents."):
            # 保留 agents 包本身，以便 register_all 可用
            if mod_name == "agents" or mod_name == "agents.__init__":
                continue
            to_remove.append(mod_name)
    for mod_name in to_remove:
        del sys.modules[mod_name]

    # 3. 重新注册
    register_all()


__all__ = ["BaseAgent", "AgentFactory", "register_all", "reload_agents"]
