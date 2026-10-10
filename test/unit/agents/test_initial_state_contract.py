"""Agent 初始状态契约：所有模式的 `_build_initial_state` 必须接受 runner 透传的 kwargs。

根因（2026-10-02 E2E 发现）：server/agent_runner.py 对**所有**模式统一调用
`agent.run(..., dialog_uuid=dialog_uuid)`（消息持久化 / trace 绑定依赖它），
`PlanBasedAgent.run(..., **kwargs)` 原样透传给 `_build_initial_state`。
`PdorAgent` 的签名没有跟（缺 `**kwargs`）→ pdor 模式在 Web 路径上 3.4s 即失败：
`_build_initial_state() got an unexpected keyword argument 'dialog_uuid'`，
而 react/unified/plan/group 都正常 —— 纯签名漂移，且单测全绿掩盖了它
（没有任何用例在"runner 实际调用形态"下检查过各模式）。

本测试用 `inspect.signature` 在**类级别**断言五个模式的初始状态构建函数都接受
runner 契约参数，不实例化 Agent（避免触发整条依赖树 import）。

运行: pytest test/unit/agents/test_initial_state_contract.py -v
"""
import inspect

import pytest

_MODES = ["react_stock", "unified_plan", "plan_solve", "pdor", "agent_group"]
_MODULE_OF = {
    "react_stock": ("agents.react.agent", "ReactStockAgent"),
    "unified_plan": ("agents.plan.plan_agents", "UnifiedPlanAgent"),
    "plan_solve": ("agents.plan.plan_agents", "PlanAndSolveAgent"),
    "pdor": ("agents.pdor.agent", "PdorAgent"),
    "agent_group": ("agents.group.agent", "GroupAgent"),
}
# server/agent_runner.py 实际透传的关键字（除固定位置参数外）
_RUNNER_KWARGS = {"progress_callback", "dialog_uuid"}
# 只有 PlanBasedAgent 子类走 `_build_initial_state(**kwargs)` 透传契约；
# react_stock 的 `run` 自己消化 kwargs，没有/不需要这个方法。
_PLAN_BASED = ["unified_plan", "plan_solve", "pdor", "agent_group"]


def _cls(mode: str):
    mod_name, cls_name = _MODULE_OF[mode]
    import importlib
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)


def _accepts_kwargs(func) -> bool:
    """签名里是否有 **kwargs（能兜住未来新增的透传参数）。"""
    return any(p.kind == inspect.Parameter.VAR_KEYWORD
               for p in inspect.signature(func).parameters.values())


def _missing_names(func) -> set:
    """签名里缺失的 runner 契约参数名。"""
    params = inspect.signature(func).parameters
    return {n for n in _RUNNER_KWARGS if n not in params}


@pytest.mark.parametrize("mode", _MODES)
def test_run_accepts_runner_contract(mode):
    """每个模式的 `run` 都必须吃下 runner 透传参数（dialog_uuid 等）"""
    func = _cls(mode).run
    missing = _missing_names(func)
    assert not missing or _accepts_kwargs(func), (
        f"{mode}.run 不接受 runner 透传参数 {sorted(missing)}，且没有 **kwargs 兜底"
    )


@pytest.mark.parametrize("mode", _PLAN_BASED)
def test_initial_state_accepts_runner_contract(mode):
    """PlanBasedAgent 子类：`_build_initial_state` 必须接受 **kwargs 透传契约"""
    func = _cls(mode)._build_initial_state
    missing = _missing_names(func)
    assert not missing or _accepts_kwargs(func), (
        f"{mode}._build_initial_state 不接受 runner 透传参数 {sorted(missing)}，"
        f"且没有 **kwargs 兜底 —— Web 路径会在运行期 TypeError（见 pdor 事故）"
    )


def test_pdor_signature_now_has_var_keyword():
    """回归钉死：pdor 必须有 **kwargs 兜底"""
    assert _accepts_kwargs(_cls("pdor")._build_initial_state)
