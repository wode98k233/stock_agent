"""AgentFactory 重复注册回归（离线）。

背景：`register_all()` 每次调用都会重新 register 各 agent。旧实现只清
`_instances`、不清 `_ready` 事件，导致第二次 `get()` 走「等待初始化」分支、
拿不到实例，误报 `Agent 'xxx' 初始化失败`（e2e 多测试复用同一模式时实测）。
"""
from agents.factory import AgentFactory


def test_reregister_then_get_reinitializes():
    old_factories = dict(AgentFactory._factories)
    old_instances = dict(AgentFactory._instances)
    old_ready = dict(AgentFactory._ready)
    try:
        AgentFactory._factories.clear()
        AgentFactory._instances.clear()
        AgentFactory._ready.clear()

        calls = []
        AgentFactory.register(lambda: calls.append(1) or object(), name="t_agent")
        a1 = AgentFactory.get("t_agent")
        assert a1 is not None and len(calls) == 1

        # 模拟 register_all 二次调用：重新注册后应能重新实例化
        AgentFactory.register(lambda: calls.append(2) or object(), name="t_agent")
        a2 = AgentFactory.get("t_agent")
        assert a2 is not None and len(calls) == 2
        assert a2 is not a1
    finally:
        AgentFactory._factories.clear()
        AgentFactory._factories.update(old_factories)
        AgentFactory._instances.clear()
        AgentFactory._instances.update(old_instances)
        AgentFactory._ready.clear()
        AgentFactory._ready.update(old_ready)
