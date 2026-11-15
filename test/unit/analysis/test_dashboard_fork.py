"""仪表盘 conversation fork（PRD-A9 §14 同款形态）单测。

背景（2026-10-15 缓存命中排查）：report-fast 走 fork（前缀 = tools + agent 对话），
而 dashboard 仍从零拼 ``[system=build_shared_system, user=任务]`` —— 实测两者公共
前缀仅 635 字符（≈1 条消息），缓存只剩 provider 最小块 2048，命中率 20.1%。
report-fast 同期 91.5%。这里钉住 fork 的构造契约。
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from agents.analysis import dashboard_generator as dg  # noqa: E402
from agents.analysis.dashboard_generator import (  # noqa: E402
    _build_prompt, build_dashboard_fork_messages,
)


class TestDashboardForkMessages:
    def test_returns_none_without_conversation(self):
        """无对话 → 回落非 fork 路径（不得返回半截前缀）。"""
        assert build_dashboard_fork_messages(None, "TASK") is None
        assert build_dashboard_fork_messages([], "TASK") is None

    def test_appends_task_as_trailing_user_message(self):
        conv = [("system", "SYS"), ("human", "Q"), ("ai", "A"), ("tool", "R")]
        out = build_dashboard_fork_messages(conv, "TASK")
        assert out == [*conv, ("user", "TASK")]
        # 前缀逐条不变 —— 这正是缓存命中的前提
        assert out[:-1] == conv

    def test_tail_role_must_be_user(self):
        """尾部用 user 而非 system：agent 对话必以 tool 结果结尾，
        「tool 结果紧跟 system」在部分 provider 上是非法序列。"""
        out = build_dashboard_fork_messages([("system", "S"), ("tool", "T")], "X")
        assert out[-1][0] == "user"


class TestBuildPromptForkSwitch:
    def _args(self, **kw):
        base = dict(rule_hints={}, user_input="宁德时代股价", scenario_tag="市场决策仪表盘",
                    tool_index="")
        base.update(kw)
        return base

    def test_non_fork_shape_keeps_shared_system(self):
        msgs = _build_prompt("报告正文", **self._args())
        assert msgs[0][0] == "system"          # build_shared_system
        assert msgs[1][0] == "user"

    def test_fork_shape_drops_system_and_keeps_prefix(self):
        conv = [("system", "## 系统上下文\n你是选股雷达"), ("human", "Q"), ("tool", "R")]
        msgs = _build_prompt("报告正文", **self._args(conversation=conv))
        # ⚠️ fork 下不得再有 system 头 —— 它会插在 tool 结果之后（非法序列）
        #    且与 agent 已热前缀不同源
        assert [m[0] for m in msgs] == ["system", "human", "tool", "user"]
        assert msgs[:3] == conv
        assert "仪表盘" in msgs[-1][1]

    def test_fork_and_non_fork_share_the_same_task_payload(self):
        """两条路径的 user 任务文本必须一致 —— fork 只改前缀，不改指令内容。"""
        conv = [("system", "S"), ("human", "Q")]
        forked = _build_prompt("报告正文", **self._args(conversation=conv))[-1][1]
        plain = _build_prompt("报告正文", **self._args())[-1][1]
        assert forked == plain

    def test_fork_detection_must_not_use_first_role(self):
        """⚠️ 不能靠 messages[0][0] != "system" 判断 fork。

        fork 后的首条正是 agent 的 system，与非 fork 的 build_shared_system 同为首位
        system —— 用它判断会得出「没 fork」，于是 conversation_tools 不绑定、
        payload 位置 0 的 tools 数组缺失、整条前缀错位、缓存归零。
        实测就是这么把 dashboard 命中压到 2.2% 的。
        """
        conv = [("system", "## 系统上下文"), ("human", "Q")]
        msgs = _build_prompt("正文", **self._args(conversation=conv))
        assert msgs[0][0] == "system"       # 首条仍是 system
        assert len(msgs) > 2                # 但确实是 fork（非 [system, user] 两条）
        assert msgs[:2] == conv             # 前缀就是传进来的对话
        assert msgs[-1][0] == "user"        # 尾部才是仪表盘任务

    def test_non_fork_is_exactly_system_plus_user(self):
        msgs = _build_prompt("正文", **self._args())
        assert [m[0] for m in msgs] == ["system", "user"]

    def test_drilldown_tail_still_forks(self):
        """二阶段下钻只是尾部内容变化，前缀仍须保持可命中。"""
        conv = [("system", "S"), ("tool", "R")]
        a = _build_prompt("正文", **self._args(conversation=conv))
        b = _build_prompt("正文", **self._args(conversation=conv,
                                              tool_evidence="工具原文…"))
        assert a[:-1] == b[:-1] == conv


class TestReportForkPrompt:
    """报告 → 仪表盘 fork（PRD-A11）：仪表盘 payload 必须是**报告 payload 的逐条扩展**，
    且尾部不再重复报告正文 / 工具索引（前缀里已有，重复 = 按全价再付一遍）。
    """

    def _args(self, **kw):
        base = dict(rule_hints={}, user_input="宁德时代股价",
                    scenario_tag="板块决策仪表盘", tool_index="TOOL_INDEX_MARKER")
        base.update(kw)
        return base

    REPORT_PAYLOAD = [("system", "SYS"), ("user", "REPORT_TASK_AND_EVIDENCE")]

    def test_prefix_is_byte_identical_to_report_payload(self):
        msgs = _build_prompt("报告正文", **self._args(conversation=self.REPORT_PAYLOAD,
                                                    report_fork=True))
        assert msgs[:len(self.REPORT_PAYLOAD)] == self.REPORT_PAYLOAD
        assert msgs[-1][0] == "user"

    def test_report_fork_tail_drops_report_text_and_tool_index(self):
        msgs = _build_prompt("报告正文-应缺席", **self._args(conversation=self.REPORT_PAYLOAD,
                                                          report_fork=True))
        tail = msgs[-1][1]
        assert "报告正文-应缺席" not in tail
        assert "TOOL_INDEX_MARKER" not in tail
        assert "scenario_tag" in tail          # schema 仍在（否则模型无法产出）

    def test_plain_fork_tail_keeps_report_text_and_tool_index(self):
        """非 report_fork 的 agent fork 档行为不变。"""
        msgs = _build_prompt("报告正文X", **self._args(conversation=self.REPORT_PAYLOAD))
        tail = msgs[-1][1]
        assert "报告正文X" in tail
        assert "TOOL_INDEX_MARKER" in tail


class TestReportForkSink:
    def test_report_enhance_publishes_payload_when_no_agent_conversation(self, monkeypatch):
        """报告节点把 report payload 写进 state（plan/pdor 无 agent 对话时的唯一预热源）。"""
        import agents.common as common
        from agents.shared.report_enhance_node import ReportEnhanceNode

        async def fake_rpp(**kwargs):
            kwargs["fork_sink"]["messages"] = [("system", "S"), ("user", "U")]
            kwargs["fork_sink"]["tools"] = []
            return "增强报告"

        class _Node(ReportEnhanceNode):
            AGENT_NAME = "plan_solve"

            def extract_step_results(self, state):
                return []

            def extract_raw_result(self, state, step_results):
                return "原始报告"

        monkeypatch.setattr(common, "run_report_post_processing", fake_rpp)
        out = asyncio.run(_Node()({"input": "q", "response": "raw"}, ctx=None))
        assert out["response"] == "增强报告"
        assert out["_report_fork_messages"] == [("system", "S"), ("user", "U")]
        assert out["_report_fork_tools"] == []


class TestCallLlmForkMetadata:
    """`_call_llm` 的 fork 埋点构造（回归：非 fork 模式曾整条打挂）。

    2026-10-18 实测：`conversation_tools=None`（plan / pdor / group / scenario 等
    无 fork 路径）时，调用点用 `if prefix`（dataclass 恒真）判据把
    `(messages, None)` 当 payload 传给 `fork_metadata` → `len(None)` 抛 TypeError，
    被 `generate()` 兜底成规则版 fallback —— 仪表盘 LLM 精炼全挂，且无任何报错上浮。
    """

    def _run(self, monkeypatch, conversation_tools):
        captured = {}
        monkeypatch.setattr("utils.llm_factory.get_report_llm", lambda: None)
        monkeypatch.setattr("utils.llm_factory.get_llm", lambda: MagicMock())
        monkeypatch.setattr("utils.llm_factory.llm_json_with_retry",
                            lambda *a, **kw: captured.update(kw) or {"core_verdict": "x"})
        msgs = [("system", "SYS"), ("user", "TASK")]
        out = asyncio.run(dg._call_llm(
            msgs, None, MagicMock(), metadata={"node": "dashboard"},
            conversation_tools=conversation_tools))
        return out, captured["metadata"]

    def test_non_fork_without_tools_does_not_crash(self, monkeypatch):
        out, meta = self._run(monkeypatch, conversation_tools=None)
        assert out == {"core_verdict": "x"}
        assert meta["fork_kind"] == "standalone"   # 诚实埋点，不谎报 fork
        assert meta["fork_skip_reason"] == "no_tools"

    def test_fork_with_tools_reports_llm_fork(self, monkeypatch):
        out, meta = self._run(monkeypatch, conversation_tools=[object()])
        assert out == {"core_verdict": "x"}
        assert meta["fork_kind"] == "llm_fork"