"""子步执行器的模板指引领界（2026-10-01 事故）

事故：`run_react_subgraph` 把模板的「报告输出结构 + 质量门禁」一起注入子 Agent 的
system prompt，于是每个只负责一个维度的子 Agent 都写了一份完整 10 段报告
（实测 4 份同构，5146/5455/5036/4588 字符），报告节点"合并增量信息"变成"改写 4 份完整报告"。

本测试钉住两点：
  1) 子步执行器的 prompt **不含**报告级约束（报告输出结构 / 质量门禁）；
  2) 子步执行器的 prompt **含**输出边界声明，且**含**数据契约（不能连数据要求一起删掉）。

`run_react_subgraph` 被 4 个 step 执行器共用：group/subagents/base.py、pdor/node/executor.py、
plan/node/executor.py、plan/node/unified_executor.py —— 覆盖这一处即覆盖全部。

运行: pytest test/unit/agents/test_subagent_guidance_boundary.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class _CapturingGraph:
    """替换编译后的图：捕获 initial_state 里的 messages，不执行任何 LLM。"""

    def __init__(self):
        self.messages = None

    async def ainvoke(self, state, config=None):
        self.messages = state["messages"]
        return {"final_result": "子 Agent 结论"}


async def _capture_subagent_messages(template_id: str, step_purpose: str = "分析9月宏观环境"):
    with patch("agents.common_react.graph.ensure_radar", side_effect=lambda x: x):
        from agents.common_react.graph import ReactSubGraph

        graph = ReactSubGraph(llm=MagicMock(), tools=[], logger=MagicMock(),
                              template_id=template_id)

    recorder = _CapturingGraph()
    graph._graph = recorder
    await graph.ainvoke(step_purpose=step_purpose, context_messages=[("user", "占位")])
    return recorder.messages[0][1]          # system prompt


async def test_subagent_prompt_excludes_report_structure():
    """子步执行器不得收到「报告输出结构」与「质量门禁」"""
    sys_prompt = await _capture_subagent_messages("theme_trading")

    assert "报告输出结构" not in sys_prompt
    assert "质量门禁" not in sys_prompt
    assert "必须完整输出全部板块" not in sys_prompt


async def test_subagent_prompt_keeps_data_contract_and_adds_boundary():
    """数据契约必须保留；并明确告知输出边界"""
    sys_prompt = await _capture_subagent_messages("theme_trading")

    # 数据级内容保留（否则子 Agent 不知道该取什么数）
    assert "场景化数据契约" in sys_prompt
    assert "必需数据" in sys_prompt

    # 输出边界声明
    assert "你的输出边界" in sys_prompt
    for banned in ("仓位与风险预算", "交易纪律", "五类投资者适配"):
        assert banned in sys_prompt      # 出现在"禁止编写"清单里
    assert "只输出本维度的数据与判断" in sys_prompt


async def test_main_react_agent_still_gets_report_structure():
    """对照组：主 Agent（最终报告作者）仍必须拿到报告结构。

    若本用例失败，说明有人把 build_guidance 的默认值改成了 False —— 那会让最终报告
    失去结构约束（`react/nodes.py` 与两个 planner 都依赖默认 True）。
    """
    from agents.analysis.template_store import load_template, build_guidance

    t = load_template("theme_trading")
    assert "报告输出结构" in build_guidance(t, "10月主线题材")
