"""step 门禁与重规划合并回归测试（2026-09-27）。

覆盖两类 2026-09-26 实测事故：
1. 空转步骤被判成功：子代理零工具调用 + 回「没有提供 ETF 代码，无法调取 K 线」，
   旧逻辑判定为 success，replanner 直接换方向，用户核心问题整轮被跳过。
2. 重规划追加步被静默截断：`替换 0 + 追加 3` → 8 步截断为 5 步，追加的补救步全部消失。

运行方式：pytest test/unit/plan/test_step_gates.py -v
"""
import os
import sys
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ══════════════════════════════════════════════════════════════
# 一、空转门禁 is_empty_step_result
# ══════════════════════════════════════════════════════════════

def test_empty_step_result_blocks_zero_tool_call_missing_target():
    """零工具调用 + 缺标语料 → 判为失败（本次事故原文）。"""
    from agents.plan.analyze import is_empty_step_result

    fault_text = (
        "要验证 MACD 底背离是否成立，我需要先确认具体分析的标的。"
        "目前对话中没有提供基金/ETF 代码或名称，无法直接调取 K 线数据。"
    )
    assert is_empty_step_result(fault_text, 0) is True
    assert is_empty_step_result("请提供股票代码后我再查询", 0) is True
    assert is_empty_step_result("", 0) is True


def test_empty_step_result_allows_normal_text_and_tool_calls():
    """误伤防护：有工具调用，或零调用但文本正常 → 放行。"""
    from agents.plan.analyze import is_empty_step_result

    fault_text = "目前对话中没有提供基金/ETF 代码或名称，无法直接调取 K 线数据。"
    # 有工具调用：结论里出现「无法获取」也放行
    assert is_empty_step_result(fault_text, 2) is False
    assert is_empty_step_result("MACD 底背离无法获取完整数据，结论中性", 3) is False
    # 零调用但纯推理/汇总结论 → 放行
    assert is_empty_step_result("寒武纪股价 150 元，PE 45.2，近 5 日主力净流入 1.2 亿元", 0) is False
    assert is_empty_step_result("接口返回中无 dataTableDTOList", 1) is False


def _make_mock_ctx():
    from agents.agent_context import AgentContext

    logger = MagicMock()
    for attr in ["info", "debug", "warn", "warning", "error", "phase", "step_start", "step_end", "step_retry"]:
        setattr(logger, attr, MagicMock())
    memory = MagicMock()
    memory.add_ai = MagicMock()
    registry = MagicMock()
    registry.get_tools = MagicMock(return_value=[])
    ctx = AgentContext(logger=logger, memory=memory, skill_registry=registry)
    ctx.budget = MagicMock()
    ctx.budget.check = MagicMock()
    ctx.progress_reporter = None
    return ctx


def test_executor_marks_empty_step_as_failed():
    """executor 端到端：空转步不再被记为 success（旧逻辑会记 success）。"""
    from agents.plan.node.executor import execute_step
    from config import Config

    ctx = _make_mock_ctx()
    state = {
        "input": "恒生科技ETF 现在能抄底吗",
        "plan": [{"step": 1, "skill": "fund_query", "instruction": "获取513130日K线", "purpose": "验证底背离"}],
        "past_steps": [],
        "current_step": 0,
        "response": "",
        "user_constraints": "",
        "key_data": {},
    }

    async def mock_run_subgraph(**kwargs):
        return {
            "tool_results": [],  # 零工具调用
            "final_result": "目前对话中没有提供基金/ETF 代码或名称，无法直接调取 K 线数据。",
        }

    original_retries = Config.PLAN_EXECUTOR_MAX_RETRIES
    Config.PLAN_EXECUTOR_MAX_RETRIES = 1
    try:
        with patch("utils.llm_factory.get_llm", return_value=MagicMock()), \
             patch("agents.plan.node.executor.run_react_subgraph", side_effect=mock_run_subgraph), \
             patch("agents.plan.node.executor.ask_user_decision", return_value="跳过此步，继续执行后续步骤"), \
             patch("agents.utils._confirm_step_execution", return_value=True), \
             patch("agents.utils._generate_step_summary", new_callable=AsyncMock, return_value={"stocks": []}), \
             patch("tools.skills.SkillPromptBuilder.build_tools_detail_prompt", return_value="tools_detail"), \
             patch("utils.logger.RequestContext.current", return_value=None), \
             patch("utils.logger.ensure_radar", side_effect=lambda x: x):
            result = asyncio.run(execute_step(state, ctx))
    finally:
        Config.PLAN_EXECUTOR_MAX_RETRIES = original_retries

    step_results = result.get("step_results") or []
    assert step_results, f"应返回 step_results，实际 {result}"
    assert step_results[0]["status"] == "fail", f"空转步必须判失败，实际 {step_results[0]['status']}"
    # 门禁日志必须留痕
    logged = " ".join(str(c) for c in ctx.logger.warning.call_args_list)
    assert "零工具调用" in logged, f"应记录空转告警，实际 {logged[:300]}"


# ══════════════════════════════════════════════════════════════
# 二、重规划合并：替换优先 / 插位 / 截断有声
# ══════════════════════════════════════════════════════════════

def _plan_5():
    """本轮事故的真实计划（step 1 已执行）。"""
    return [
        {"step": 1, "skill": "fund_query", "instruction": "获取513130日K线并算MACD", "purpose": "验证底背离"},
        {"step": 2, "skill": "macro_data", "instruction": "查美债10Y", "purpose": "验证美债收益率"},
        {"step": 3, "skill": "mx_data", "instruction": "查恒生科技估值", "purpose": "估值与权重股"},
        {"step": 4, "skill": "mx_search", "instruction": "搜南向资金", "purpose": "资金面验证"},
        {"step": 5, "skill": "stock_analysis", "instruction": "综合结论", "purpose": "汇总"},
    ]


def _apply(new_steps, plan, current_step=1, logger=None):
    from agents.plan.node.replanner import _apply_new_steps
    from config import Config

    # 锁死 2026-09-26 事故场景的默认 5 步上限：本组断言（含 `<= 5`、裁掉明细）
    # 只在 5 步下成立，本地 .env 把 PLAN_MAX_STEPS 调大时不得让用例静默失效。
    with patch.object(Config, "PLAN_MAX_STEPS", 5):
        return _apply_new_steps(
            new_steps, plan, [("Step 1: 验证底背离", "无法调取K线")],
            current_step=current_step, step_results=[], logger=logger or MagicMock(),
        )


def test_replan_same_skill_replaces_instead_of_appending():
    """skill 相同即替换：避免措辞差异导致全部退化成「追加」再被截断。"""
    logger = MagicMock()
    merged = _apply([
        {"skill": "macro_data", "instruction": "查询美国10年期国债收益率最新值", "purpose": "验证美债担忧"},
        {"skill": "mx_search", "instruction": "搜索美债对港股流动性影响", "purpose": "资金面与机构观点"},
    ], _plan_5(), logger=logger)

    skills = [s["skill"] for s in merged]
    assert skills[0] == "fund_query", "已执行步不允许被替换"
    assert skills.count("macro_data") == 1
    assert skills.count("mx_search") == 1
    assert len(merged) == 5, "同 skill 替换不应使计划变长"


def test_replan_inserts_new_capability_at_current_step():
    """全新能力追加到「当前位置」，而不是排到尾部等被截掉。"""
    logger = MagicMock()
    merged = _apply([
        {"skill": "overseas_env", "instruction": "跟踪美股与美元指数走势", "purpose": "外部环境补证"},
    ], _plan_5(), logger=logger)

    assert merged[0]["skill"] == "fund_query"
    assert merged[1]["skill"] == "overseas_env", f"追加步应紧跟已执行段，实际 {[s['skill'] for s in merged]}"


def test_replan_cross_skill_generic_instruction_not_silently_replaced():
    """决策 A 回归：泛化短 instruction 的老步不允许被跨 skill 新步静默吃掉。

    2026-09-26 实测事故形态：老步 stock_analysis instruction=「综合结论」（4 字），
    新步 hang_seng_env instruction 含「综合结论」→ 关键词重叠 4/4=1.0 → 被替换且无任何日志。
    现在必须变成「追加」路径：要么保留 stock_analysis，要么在截断 WARNING 里被点名。
    """
    logger = MagicMock()
    plan = _plan_5()
    plan[4]["instruction"] = "综合结论"          # 泛化短 instruction
    merged = _apply([
        {"skill": "hang_seng_env", "purpose": "环境因子",
         "instruction": "获取恒生科技环境因子综合结论（七因子：美债10Y分母端、内外双轴、价格位置…）"},
    ], plan, logger=logger)

    skills = [s["skill"] for s in merged]
    infos = " ".join(str(c) for c in logger.info.call_args_list)
    warns = " ".join(str(c) for c in logger.warning.call_args_list)

    assert "hang_seng_env" in skills, "新能力必须进入计划"
    # 不允许「静默消失」：要么还在，要么被截断告警点名
    assert ("stock_analysis" in skills) or ("stock_analysis" in warns), \
        f"stock_analysis 既不在计划里也没被告警点名 → 静默丢失。plan={skills} warns={warns[:200]}"
    assert "stock_analysis" not in infos, "不得作为被替换对象出现在替换日志里"


def test_replan_replacement_is_logged():
    """同 skill 替换必须留痕（可审计）。"""
    logger = MagicMock()
    merged = _apply([
        {"skill": "macro_data", "instruction": "查询美债收益率最新值", "purpose": "验证美债担忧"},
    ], _plan_5(), logger=logger)

    infos = " ".join(str(c) for c in logger.info.call_args_list)
    assert "替换 Step 2" in infos, f"替换应打明细日志，实际 {infos[:300]}"
    assert "macro_data" in infos
    assert len([s for s in merged if s["skill"] == "macro_data"]) == 1


def test_replan_truncation_keeps_appends_and_executed_prefix():
    """超限时：保追加补救步 + 保已执行前缀（executor 以 current_step 作下标），裁撤必须有告警明细。"""
    logger = MagicMock()
    merged = _apply([
        {"skill": "sentiment_flow", "instruction": "统计雪球情绪指数", "purpose": "情绪面补证"},
        {"skill": "overseas_env", "instruction": "跟踪美股与美元指数", "purpose": "外部环境补证"},
    ], _plan_5(), logger=logger)

    skills = [s["skill"] for s in merged]
    assert skills[0] == "fund_query", "已执行前缀被裁会导致后续步骤被跳过"
    assert "sentiment_flow" in skills and "overseas_env" in skills, "追加补救步不允许被截断"
    assert len(merged) <= 5
    warns = " ".join(str(c) for c in logger.warning.call_args_list)
    assert "裁掉" in warns, "裁撤必须打 WARNING（禁止静默丢步）"
    assert "stock_analysis" in warns, "WARNING 应列出被裁步骤的 skill"


def test_limit_plan_steps_protect_and_verbose_log():
    """limit_plan_steps：protect 保序优先，被裁步骤明细入日志。"""
    from agents.shared.analysis_utils import limit_plan_steps

    steps = [{"step": i, "skill": f"s{i}", "purpose": f"p{i}"} for i in range(1, 7)]
    protected = [steps[4]]  # 第 5 步是补救步
    logger = MagicMock()

    limited = limit_plan_steps(steps, max_steps=5, logger=logger, tag="R", protect=protected)

    assert len(limited) == 5
    assert limited[4]["skill"] == "s5", f"protect 必须保留，实际 {[s['skill'] for s in limited]}"
    assert [s["step"] for s in limited] == [1, 2, 3, 4, 5]
    warns = " ".join(str(c) for c in logger.warning.call_args_list)
    assert "s6" in warns, "被裁步骤必须出现在 WARNING 中"


def test_limit_plan_steps_without_protect_keeps_prefix():
    """无 protect 时行为与旧版一致：保序取前 N 步（planner 路径）。"""
    from agents.shared.analysis_utils import limit_plan_steps

    steps = [{"step": i, "skill": f"s{i}", "purpose": f"p{i}"} for i in range(1, 7)]
    limited = limit_plan_steps(steps, max_steps=3, logger=MagicMock(), tag="P")

    assert [s["skill"] for s in limited] == ["s1", "s2", "s3"]
    assert [s["step"] for s in limited] == [1, 2, 3]
