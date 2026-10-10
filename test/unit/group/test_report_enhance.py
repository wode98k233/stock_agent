"""
测试目标: agents/group/nodes/report_enhance.py
覆盖范围:
  - report_enhance_node: 正常报告生成、空结果降级、报告后处理调用、dashboard 追加
  - 结果合并逻辑（accumulated_data 优先 vs plan_steps fallback）
  - 异常降级（报告后处理失败、dashboard 失败不影响主流程）
Mock 策略: mock run_report_post_processing、append_dashboard、GroupMessageLogger
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_state(**overrides):
    base = {
        "input": "分析贵州茅台",
        "plan_steps": [
            {"step": 1, "agent_names": ["technical_analyst"], "task_purpose": "技术分析",
             "input_params": {}, "status": "success", "result": "技术面看涨", "feedback": "",
             "requests": [], "retry_count": 0, "executed_at": "", "run_group": 1, "depends_on": []},
            {"step": 2, "agent_names": ["macro_analyst"], "task_purpose": "宏观分析",
             "input_params": {}, "status": "success", "result": "宏观环境良好", "feedback": "",
             "requests": [], "retry_count": 0, "executed_at": "", "run_group": 2, "depends_on": []},
        ],
        "current_step_index": 2,
        "accumulated_data": "",
        "tool_calls": [
            {"tool_name": "mx_get_kline", "tool_input": "{}", "tool_output": "data", "tool_call_id": ""},
        ],
        "template_id": None,
        "selected_skills": [],
        "dialog_uuid": "test-uuid",
        "task_id": "test-task",
        "constraints": "",
    }
    base.update(overrides)
    return base


def _make_ctx():
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.logger.phase = MagicMock()
    ctx.logger.info = MagicMock()
    ctx.logger.warning = MagicMock()
    ctx.logger.error = MagicMock()
    ctx.budget = MagicMock()
    ctx.progress_reporter = MagicMock()
    return ctx


class TestReportEnhanceNode:
    """report_enhance_node: 报告生成节点"""

    @pytest.mark.asyncio
    async def test_uses_accumulated_data_when_available(self):
        """accumulated_data 有值 → 优先使用"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(accumulated_data="累积的分析数据")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()

            result = await report_enhance_node(state, ctx)

        assert "累积的分析数据" in result["response"]

    @pytest.mark.asyncio
    async def test_falls_back_to_plan_results(self):
        """accumulated_data 为空 → 从 plan_steps 合并"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(accumulated_data="")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()

            result = await report_enhance_node(state, ctx)

        assert "技术面看涨" in result["response"]
        assert "宏观环境良好" in result["response"]

    @pytest.mark.asyncio
    async def test_no_results_returns_fallback(self):
        """无任何结果 → 返回降级消息"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(
            accumulated_data="",
            plan_steps=[{"step": 1, "agent_names": ["test"], "task_purpose": "test",
                         "status": "failed", "result": "", "input_params": {},
                         "feedback": "", "requests": [], "retry_count": 0,
                         "executed_at": "", "run_group": 1, "depends_on": []}],
        )
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()

            result = await report_enhance_node(state, ctx)

        assert "无可用" in result["response"]

    @pytest.mark.asyncio
    async def test_report_post_processing_called(self):
        """REPORT_ENABLE_ANALYSIS_ENGINE=True → 调用报告后处理"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state()
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
             patch("agents.group.nodes.report_enhance.run_report_post_processing", new_callable=AsyncMock) as mock_post:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()
            mock_post.return_value = "增强后的报告"

            result = await report_enhance_node(state, ctx)

        mock_post.assert_called_once()
        assert result["response"] == "增强后的报告"

    @pytest.mark.asyncio
    async def test_report_post_processing_failure_uses_original(self):
        """报告后处理失败 → 使用原始结果"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(accumulated_data="原始数据")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
             patch("agents.group.nodes.report_enhance.run_report_post_processing", new_callable=AsyncMock) as mock_post:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()
            mock_post.side_effect = RuntimeError("后处理失败")

            result = await report_enhance_node(state, ctx)

        assert "原始数据" in result["response"]

    @pytest.mark.asyncio
    async def test_dashboard_failure_does_not_affect_report(self):
        """仪表盘生成失败 → 不影响报告"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(accumulated_data="报告内容")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
             patch("agents.analysis.dashboard_node.append_dashboard", new_callable=AsyncMock) as mock_dash:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = True
            mock_log_cls.from_state.return_value = MagicMock()
            mock_dash.side_effect = RuntimeError("dashboard 失败")

            result = await report_enhance_node(state, ctx)

        assert "报告内容" in result["response"]

    @pytest.mark.asyncio
    async def test_tool_calls_carry_evidence_items_for_group_mode(self):
        """agent_group 模式：报告后处理收到「可下钻条目」而非空列表

        2026-10-01 契约变更：原实现传 tool_calls=[]（"报告只接收 agent 级别的总结"），
        导致报告层零原始数据、只能改写 4 份散文。现改为把 group 产物表达为条目，
        由报告层自己决定 need_tools 载入哪些全文。
        """
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state()
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
             patch("agents.group.nodes.report_enhance.run_report_post_processing", new_callable=AsyncMock) as mock_post:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()
            mock_post.return_value = "报告"

            await report_enhance_node(state, ctx)

        _, kwargs = mock_post.call_args
        items = kwargs.get("tool_calls")
        assert items, "不得再传空列表（否则报告层零原始数据）"
        assert all(it.get("tool_output") for it in items)
        assert kwargs.get("step_results") is None   # 已表达为条目，不再双通道

    @pytest.mark.asyncio
    async def test_progress_final_called(self):
        """调用 progress.final"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state()
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()

            await report_enhance_node(state, ctx)

        ctx.progress_reporter.final.assert_called_once()


# ══════════════════════════════════════════════════════════════
# 报告引擎证据通道（2026-10-01：原传 tool_calls=[] → 报告层零原始数据）
# ══════════════════════════════════════════════════════════════

_DOC = "## 数据底版\n| 指数 | 9月涨跌幅 |\n|---|---|\n| 深证成指 | -8.044% |\n"


def _capturing_ctx():
    return _make_ctx()


async def _run_capture(state, ctx):
    """跑 report_enhance_node，捕获传给 run_report_post_processing 的实参。"""
    from agents.group.nodes.report_enhance import report_enhance_node

    captured = {}

    async def fake_post(**kwargs):
        captured.update(kwargs)
        return None

    with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
         patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls, \
         patch("agents.group.nodes.report_enhance.run_report_post_processing", side_effect=fake_post):
        mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = True
        mock_cfg.DASHBOARD_ENABLED = False
        mock_log_cls.from_state.return_value = MagicMock()
        await report_enhance_node(state, ctx)
    return captured


class TestEvidenceItemsBuilder:
    """_build_evidence_items: group 产物 → 可下钻条目"""

    def test_doc_first_and_empty_step_skipped(self):
        from agents.group.nodes.report_enhance import _build_evidence_items
        plan = _make_state()["plan_steps"]
        plan[0]["result"] = ""                       # 数据采集步被清空
        items = _build_evidence_items(plan, _DOC, [])
        assert items[0]["tool_name"] == "数据底版 · 数据采集员"
        assert items[0]["tool_output"] == _DOC
        assert all(it["tool_output"] for it in items)         # 无空条目
        assert sum(1 for it in items if it["tool_name"].startswith("分析 · ")) == 1

    def test_duplicate_of_doc_not_repeated(self):
        from agents.group.nodes.report_enhance import _build_evidence_items
        plan = _make_state()["plan_steps"]
        plan[0]["result"] = _DOC
        items = _build_evidence_items(plan, _DOC, [])
        assert len([it for it in items if it["tool_output"] == _DOC]) == 1

    def test_real_tool_calls_appended(self):
        from agents.group.nodes.report_enhance import _build_evidence_items
        tcs = [{"tool_name": "mx_data_query", "tool_input": "q", "tool_output": "raw", "tool_call_id": ""}]
        items = _build_evidence_items(_make_state()["plan_steps"], _DOC, tcs)
        assert any(it["tool_name"] == "mx_data_query" for it in items)


class TestEngineEvidenceChannel:
    """report_enhance_node → 引擎实参"""

    @pytest.mark.asyncio
    async def test_passes_items_not_empty_tool_calls(self):
        state = _make_state(data_collection_doc=_DOC)
        captured = await _run_capture(state, _capturing_ctx())

        assert captured["tool_calls"], "修复前这里是空列表 → 报告层零原始数据"
        assert captured["tool_calls"][0]["tool_name"] == "数据底版 · 数据采集员"
        # 已表达为条目，不再走 step_results 双通道
        assert captured["step_results"] is None
        # raw_result 仅作下钻种子
        assert captured["raw_result"] == _DOC
        assert any(it["tool_name"] == "mx_get_kline" for it in captured["tool_calls"])

    @pytest.mark.asyncio
    async def test_step_result_kept_when_doc_missing(self):
        state = _make_state(accumulated_data="累计结果")
        captured = await _run_capture(state, _capturing_ctx())

        assert captured["tool_calls"], "无底版时各步结果仍应作为条目"
        assert not any(it["tool_name"].startswith("数据底版") for it in captured["tool_calls"])
        assert captured["raw_result"] == "累计结果"

    @pytest.mark.asyncio
    async def test_engine_disabled_keeps_legacy_merge(self):
        """引擎关闭 → 仍返回原合并文本（含底版），不回归"""
        from agents.group.nodes.report_enhance import report_enhance_node
        state = _make_state(data_collection_doc=_DOC, accumulated_data="累计结果")
        ctx = _make_ctx()

        with patch("agents.group.nodes.report_enhance.Config") as mock_cfg, \
             patch("agents.group.nodes.report_enhance.GroupMessageLogger") as mock_log_cls:
            mock_cfg.REPORT_ENABLE_ANALYSIS_ENGINE = False
            mock_cfg.DASHBOARD_ENABLED = False
            mock_log_cls.from_state.return_value = MagicMock()
            result = await report_enhance_node(state, ctx)

        assert result["response"].startswith("## 数据底版")
        assert "累计结果" in result["response"]
