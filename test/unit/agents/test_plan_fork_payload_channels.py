"""unified_plan fork 载荷的 checkpoint 安全回归（2026-10-15 故障复盘）。

故障形态（用户日志 2026-10-14 23:54 unified_plan）：
- `_fork_tools` 装的是 StructuredTool 实例数组（动态创建的类，既不可 msgpack
  编码、也不可 pickle）。以普通 list channel 声明时，LangGraph checkpoint 的
  后台提交 task 序列化失败 —— 但提交是 fire-and-forget（`loop.submit`），异常
  延迟到**图退出时**才在 `_executor.__aexit__` 抛出，整个 run 被判失败：
  `TypeError: Type is not msgpack serializable: StructuredTool`。
- 表现极具迷惑性：fork 生效（report-fast 命中 92%）、报告与仪表盘全部正常生成，
  却在 dashboard 之后触发 summary-on-error 兜底总结 ——
  「仪表盘输出后又多了一次总结」。
- 对照组实测：普通 channel 必崩；UntrackedValue 正常（本文件回归）。

修复：两个载荷 channel 声明为 `UntrackedValue`（写入不进 checkpoint，
图内内存传递不受影响；resume 后丢失属预期 —— fork 是运行时优化，回落证据注入）。

运行: pytest test/unit/agents/test_plan_fork_payload_channels.py -v
"""
import sys
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.state import PlanExecute


def _mk_tool(name="probe_tool"):
    def fn(x: int) -> int:
        return x * 2
    return StructuredTool.from_function(fn, name=name, description="probe")


class TestForkPayloadChannels:
    """channel 声明与 checkpoint 全链路行为。"""

    def test_channels_declared_as_untracked(self):
        """fork 载荷 channel 必须解析为 UntrackedValue。

        改回普通 `list` 会让 StructuredTool 数组进入 checkpoint 序列化 ——
        崩溃会以「延迟异常」形态出现（见模块 docstring），回归防线就在这。"""
        g = StateGraph(PlanExecute)
        assert isinstance(g.channels["_fork_messages"], UntrackedValue)
        assert isinstance(g.channels["_fork_tools"], UntrackedValue)

    async def test_fork_payload_passes_checkpoint_safely(self):
        """带 StructuredTool 的载荷走真实 MemorySaver 完整图：
        不抛异常 + 下游节点读到同一份对象 + 图正常收尾。"""
        seen = {}

        async def producer(state):
            # 模拟 unified_executor 写回 fork 载荷
            return {
                "_fork_messages": [("user", "hi")],
                "_fork_tools": [_mk_tool(), _mk_tool("t2")],
                "response": "step done",
            }

        async def consumer(state):
            # 模拟 report_enhance 读取
            seen["tools"] = state.get("_fork_tools")
            seen["messages"] = state.get("_fork_messages")
            return {"response": "final"}

        g = StateGraph(PlanExecute)
        g.add_node("producer", producer)
        g.add_node("consumer", consumer)
        g.add_edge(START, "producer")
        g.add_edge("producer", "consumer")
        g.add_edge("consumer", END)
        app = g.compile(checkpointer=MemorySaver())

        # 初始 state 不含 fork 键（还原 _build_initial_state 的真实调用形态）
        result = await app.ainvoke(
            {"input": "x", "plan": [], "past_steps": [], "current_step": 0,
             "response": "", "user_constraints": "", "key_data": {},
             "budget_exempt": None, "user_approved_overrun_count": 0,
             "step_results": [], "observer_log": [], "template_id": None,
             "selected_skills": [], "tool_calls": []},
            {"configurable": {"thread_id": "probe-fork-untracked"}},
        )

        assert result.get("response") == "final", "图必须正常收尾（不能有延迟异常）"
        assert seen["tools"] is not None and len(seen["tools"]) == 2
        assert all(isinstance(t, StructuredTool) for t in seen["tools"])
        assert seen["messages"] == [("user", "hi")]

    async def test_tools_are_the_same_objects_not_serialized_copies(self):
        """下游拿到的必须是同一份对象（fork 绑定 tools 数组位置 0 逐字节一致，
        这是整段命中缓存的前提）。"""
        captured = {}

        async def producer(state):
            tool = _mk_tool("identity")
            captured["origin"] = tool
            return {"_fork_tools": [tool], "_fork_messages": [("user", "x")]}

        async def consumer(state):
            tools = state.get("_fork_tools") or []
            captured["seen"] = tools[0] if tools else None
            return {"response": "ok"}

        g = StateGraph(PlanExecute)
        g.add_node("producer", producer)
        g.add_node("consumer", consumer)
        g.add_edge(START, "producer")
        g.add_edge("producer", "consumer")
        g.add_edge("consumer", END)
        app = g.compile(checkpointer=MemorySaver())

        await app.ainvoke(
            {"input": "x", "plan": [], "past_steps": [], "current_step": 0,
             "response": "", "user_constraints": "", "key_data": {},
             "budget_exempt": None, "user_approved_overrun_count": 0,
             "step_results": [], "observer_log": [], "template_id": None,
             "selected_skills": [], "tool_calls": []},
            {"configurable": {"thread_id": "probe-fork-identity"}},
        )

        assert captured["seen"] is captured["origin"], "必须是同一对象引用"


class TestForkSourceIsActualPayload:
    """fork 载荷必须来自「最后一次 LLM 调用实际发出的 messages」，不是 state 原始版。

    2026-10-15 修复：内层子图原先交出的 `_fork_messages` 是
    `final_state["messages"]`（未经 `_prepare_messages` 处理）。上下文压缩器的
    `_merge_leading_system` 会把头部连续 system 合并为 1 条（例：7 条 → 1 条，
    JSON 骨架差 ~162B），两条链路的前缀指纹必然不同 → fork 整段前缀失效，
    report-fast 只命中 tools 段（unified 实测 7%、e2e 13.3%）。
    修复后与 react 主图同源：`exec_state.last_llm_messages`（该路径实测 92%+）。
    """

    async def test_fork_messages_are_the_actual_sent_messages(self, monkeypatch):
        from agents.common_react.graph import ReactSubGraph
        from agents.executor_callbacks import ExecutionState
        from config import Config

        monkeypatch.setattr(Config, "REACT_ENABLE_CONTEXT_COMPACTION", True)

        received = []

        class _FakeLLM:
            def bind_tools(self, tools):
                return self

            async def ainvoke(self, messages, config=None, **kw):
                received.append(list(messages))
                return AIMessage(content="done")

        class _FakeBudget:
            def check(self, logger):
                return None

            def add_tokens(self, n):
                pass

            def add_call(self):
                pass

        class _SilentLogger:
            """静默 logger 桩：不进真日志（单测规约）。"""

            def isEnabledFor(self, level):
                return False

            def __getattr__(self, name):
                def _noop(*a, **k):
                    return None
                return _noop

        exec_state = ExecutionState()
        graph = ReactSubGraph(
            llm=_FakeLLM(),
            budget=_FakeBudget(),
            exec_state=exec_state,
            tools=[_mk_tool()],
            logger=_SilentLogger(),
        )
        result = await graph.ainvoke(
            step_purpose="获取大盘数据",
            context_messages=[
                ("system", "可用工具详情：A"),
                ("system", "可用工具详情：B"),
                ("system", "[用户约束] 只看技术面"),
                ("user", "今天大盘总结"),
            ],
        )

        # 1) fork 载荷 == 最后一次实际发出的 messages（内容级一致）
        assert received, "LLM 必须被调用过"
        assert result["_fork_messages"] == received[-1]
        assert result["_fork_messages"] == exec_state.last_llm_messages
        assert result["_fork_tools"] == exec_state.last_llm_tools

        # 2) 头部连续 system 已被压缩器合并为 1 条 —— 证明用的是「处理后」的
        #    版本（state 原版此时是 4 条：system_prompt + 2×工具详情 + 约束）
        def _role(m):
            return m[0] if isinstance(m, tuple) else getattr(m, "type", "")

        leading = 0
        for m in result["_fork_messages"]:
            if _role(m) != "system":
                break
            leading += 1
        assert leading == 1, f"头部 system 应已合并为 1 条（_merge_leading_system），实际 {leading} 条"
