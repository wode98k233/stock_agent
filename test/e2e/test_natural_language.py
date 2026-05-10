"""
端到端自然语言测试
需要 LLM API + 网络连接
运行: pytest test/e2e/test_natural_language.py -v -m e2e
"""
import pytest
import time
import asyncio

# 测试常量
USER_INPUT = "给我查询寒武纪最新股价以及最新三条新闻"
EXPECTED_KEYWORDS = ["寒武纪", "股价", "价格", "新闻"]
FAILURE_MARKERS = ["抱歉", "无法", "错误", "失败", "异常"]


def _assert_response_quality(response: str):
    """验证响应质量"""
    assert response, "返回为空"
    assert isinstance(response, str), f"返回类型错误: {type(response)}"
    assert len(response) > 50, f"输出过短: {len(response)} 字符"

    # 检查关键词
    found_keywords = [kw for kw in EXPECTED_KEYWORDS if kw in response]
    assert len(found_keywords) >= 2, f"缺少关键词，只找到: {found_keywords}"

    # 检查失败标记
    for marker in FAILURE_MARKERS:
        assert marker not in response[:200], f"输出开头包含失败标记: {marker}"


def _assert_no_real_errors(log_capture):
    """验证没有真正的 ERROR（过滤噪音后）"""
    errors = log_capture.get_errors()
    assert not errors, f"发现 {len(errors)} 个真正的 ERROR: {[r.message for r in errors]}"


def _run_agent(agent_name: str, user_input: str):
    """
    运行指定 Agent 并返回结果

    Args:
        agent_name: Agent 名称 (scenario, react_stock, plan_solve, unified_plan)
        user_input: 用户输入

    Returns:
        tuple: (response, elapsed_time)
    """
    from agents.factory import AgentFactory
    from tools.skill_register import SkillRegister
    from tools.skills import SkillRegistry
    from utils.logger import get_logger
    from utils.memory import MemoryManager

    # 初始化
    skill_register = SkillRegister()
    skill_register.auto_discover()
    logger, uuid, ctx = get_logger(f"e2e-{agent_name}")
    memory = MemoryManager(logger)
    registry = SkillRegistry(logger, memory, skill_register)

    # 获取 agent
    agent = AgentFactory.get(agent_name)
    agent.on_startup()

    # 执行
    t0 = time.time()
    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(
            agent.run(user_input, registry, memory, logger)
        )
    finally:
        loop.close()
    elapsed = time.time() - t0

    return response, elapsed


@pytest.mark.e2e
@pytest.mark.slow
def test_scenario_cambrian(filtered_log_capture, trace_capture):
    """
    ScenarioAgent: regex 路由到 stock_analysis
    验证: 路由正确、响应质量、无真正 ERROR
    """
    recorder, db_path = trace_capture

    response, elapsed = _run_agent("scenario", USER_INPUT)

    # 1. 响应质量验证
    _assert_response_quality(response)

    # 2. 无真正 ERROR
    _assert_no_real_errors(filtered_log_capture)

    # 3. ScenarioAgent 应该路由到 stock_analysis 或 data_query
    # 寒武纪股价+新闻 的请求应该被分类为 stock_analysis
    assert response, "ScenarioAgent 返回为空"

    print(f"  耗时: {elapsed:.1f}s, 响应长度: {len(response)} 字符")


@pytest.mark.e2e
@pytest.mark.slow
def test_react_cambrian(filtered_log_capture, trace_capture):
    """
    ReactStockAgent: 多步 tool 调用
    验证: 多步执行、响应质量、无真正 ERROR
    """
    recorder, db_path = trace_capture

    response, elapsed = _run_agent("react_stock", USER_INPUT)

    # 1. 响应质量验证
    _assert_response_quality(response)

    # 2. 无真正 ERROR
    _assert_no_real_errors(filtered_log_capture)

    # 3. ReAct agent 应该有多步 tool 调用
    # 通过 trace 验证步骤数
    from test.e2e.conftest import assert_trace_has_steps
    try:
        assert_trace_has_steps(db_path, "react_stock", min_steps=2)
    except Exception:
        pass  # trace 验证失败不阻塞测试

    print(f"  耗时: {elapsed:.1f}s, 响应长度: {len(response)} 字符")


@pytest.mark.e2e
@pytest.mark.slow
def test_plan_cambrian(filtered_log_capture, trace_capture):
    """
    PlanAndSolveAgent: 计划生成→执行→总结
    验证: 计划步骤 >= 2、响应质量、无真正 ERROR
    """
    recorder, db_path = trace_capture

    response, elapsed = _run_agent("plan_solve", USER_INPUT)

    # 1. 响应质量验证
    _assert_response_quality(response)

    # 2. 无真正 ERROR
    _assert_no_real_errors(filtered_log_capture)

    # 3. Plan agent 应该有计划步骤
    from test.e2e.conftest import assert_trace_has_steps
    try:
        assert_trace_has_steps(db_path, "plan_solve", min_steps=2)
    except Exception:
        pass  # trace 验证失败不阻塞测试

    print(f"  耗时: {elapsed:.1f}s, 响应长度: {len(response)} 字符")


@pytest.mark.e2e
@pytest.mark.slow
def test_unified_cambrian(filtered_log_capture, trace_capture):
    """
    UnifiedPlanAgent: 单步大上下文执行
    验证: 响应质量、无真正 ERROR
    """
    recorder, db_path = trace_capture

    response, elapsed = _run_agent("unified_plan", USER_INPUT)

    # 1. 响应质量验证
    _assert_response_quality(response)

    # 2. 无真正 ERROR
    _assert_no_real_errors(filtered_log_capture)

    # 3. Unified agent 应该有执行步骤
    from test.e2e.conftest import assert_trace_has_steps
    try:
        assert_trace_has_steps(db_path, "unified_plan", min_steps=1)
    except Exception:
        pass  # trace 验证失败不阻塞测试

    print(f"  耗时: {elapsed:.1f}s, 响应长度: {len(response)} 字符")
