"""
快速失败测试

验证项目在各种异常情况下的快速失败和恢复机制：
1. 预算控制器边界情况
2. 执行层错误指纹检测
3. 数据源故障转移
4. LLM 调用重试

运行方式：
  .venv/Scripts/python.exe -m pytest test/unit/test_fail_fast.py -v
"""
import os
import sys
import time
import threading
from unittest.mock import MagicMock, patch, PropertyMock

# 确保项目根目录在 sys.path 中
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ============================================================
# 1. 预算控制器边界情况
# ============================================================

def test_budget_enable_disable_toggle():
    """验证 enable/disable 切换后预算检查正确工作"""
    from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

    limits = BudgetLimits(max_tokens_per_query=100)
    bc = BudgetController(limits)

    # 初始状态：启用
    bc.add_tokens(50)
    assert bc.check() == True

    # 禁用后不检查
    bc.disable()
    bc.add_tokens(200)  # 超限
    assert bc.check() == True  # 不抛异常

    # 重新启用后检查
    bc.enable()
    try:
        bc.check()
        assert False, "应该抛出 BudgetExceeded"
    except BudgetExceeded as e:
        assert e.reason == "tokens"


def test_budget_reset_after_exceeded():
    """验证超限后 reset 可以恢复正常"""
    from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

    limits = BudgetLimits(max_tokens_per_query=100)
    bc = BudgetController(limits)

    # 超限
    bc.add_tokens(150)
    try:
        bc.check()
        assert False
    except BudgetExceeded:
        pass

    # reset 后恢复正常
    bc.reset()
    assert bc.check() == True
    assert bc.get_tokens() == 0


def test_budget_concurrent_check():
    """验证多线程并发检查不会死锁或异常"""
    from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

    limits = BudgetLimits(max_tokens_per_query=10000, max_llm_calls_per_query=2000)
    bc = BudgetController(limits)

    errors = []

    def add_and_check(thread_id, count):
        try:
            for i in range(count):
                bc.add_tokens(1)
                bc.add_call()
                bc.check()
        except Exception as e:
            errors.append(e)

    threads = []
    for t in range(10):
        thread = threading.Thread(target=add_and_check, args=(t, 100))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    # 不应该有异常（总 token 1000、calls 1000，都在限制内）
    assert len(errors) == 0, f"并发检查出错: {errors}"


def test_budget_status_percentages():
    """验证 get_status 百分比计算正确"""
    from utils.budget import BudgetController, BudgetLimits

    limits = BudgetLimits(max_tokens_per_query=1000, max_llm_calls_per_query=100)
    bc = BudgetController(limits)

    bc.add_tokens(750)
    bc.add_call()
    bc.add_call()

    status = bc.get_status()
    assert status["tokens"] == 750
    assert status["tokens_percent"] == 75  # 750/1000 = 75%
    assert status["calls"] == 2
    assert status["calls_percent"] == 2   # 2/100 = 2%


def test_budget_status_overflow():
    """验证 token 超限时百分比不超过 100%"""
    from utils.budget import BudgetController, BudgetLimits

    limits = BudgetLimits(max_tokens_per_query=100)
    bc = BudgetController(limits)

    bc.add_tokens(500)  # 5x 超限
    status = bc.get_status()
    assert status["tokens"] == 500
    assert status["tokens_percent"] == 100  # 不超过 100%


def test_budget_factory_singleton():
    """验证 BudgetControllerFactory 单例模式"""
    from utils.budget import BudgetControllerFactory

    # 重置单例
    BudgetControllerFactory._default_instance = None

    bc1 = BudgetControllerFactory.get_default()
    bc2 = BudgetControllerFactory.get_default()
    assert bc1 is bc2, "单例模式失效"

    # create 应该返回新实例
    bc3 = BudgetControllerFactory.create()
    assert bc3 is not bc1, "create 应该返回新实例"


def test_budget_multiple_limits():
    """验证多个限制同时生效"""
    from utils.budget import BudgetController, BudgetLimits, BudgetExceeded

    limits = BudgetLimits(max_tokens_per_query=100, max_llm_calls_per_query=5, max_time_seconds=10)
    bc = BudgetController(limits)

    # 先触发 token 超限
    bc.add_tokens(150)
    try:
        bc.check()
        assert False
    except BudgetExceeded as e:
        assert e.reason == "tokens"

    # reset 后触发 calls 超限
    bc.reset()
    for _ in range(6):
        bc.add_call()
    try:
        bc.check()
        assert False
    except BudgetExceeded as e:
        assert e.reason == "calls"


# ============================================================
# 2. 执行层错误指纹检测
# ============================================================

def test_error_fingerprint_basic():
    """验证错误指纹基本功能"""
    from agents.plan.analyze import error_fingerprint

    e1 = ValueError("test error")
    e2 = ValueError("test error")
    e3 = TypeError("test error")

    fp1 = error_fingerprint(e1)
    fp2 = error_fingerprint(e2)
    fp3 = error_fingerprint(e3)

    # 同类错误指纹相同
    assert fp1 == fp2
    # 不同类错误指纹不同
    assert fp1 != fp3


def test_error_fingerprint_code_related():
    """验证包含 'code' 的错误有特殊指纹"""
    from agents.plan.analyze import error_fingerprint

    e1 = ValueError("invalid code format")
    e2 = ValueError("some other error")

    fp1 = error_fingerprint(e1)
    fp2 = error_fingerprint(e2)

    assert "code_related" in fp1
    assert "code_related" not in fp2


def test_is_abnormal_result():
    """验证异常结果检测"""
    from agents.plan.analyze import is_abnormal_result

    # 正常结果
    assert is_abnormal_result("茅台股票分析：当前价格 189.50，涨幅 2.3%") == False

    # 异常结果（太短，< 20 字符）
    assert is_abnormal_result("错误") == True

    # 异常结果（包含内置标记）
    assert is_abnormal_result('返回值是 ""') == True
    assert is_abnormal_result("Agent stopped due to max iterations") == True

    # 空字符串
    assert is_abnormal_result("") == True
    assert is_abnormal_result("   ") == True


def test_dedup_new_steps():
    """验证步骤去重功能（去重基于 past_steps 和 original_plan，不检查 new_steps 内部）"""
    from agents.plan.analyze import dedup_new_steps

    new_steps = [
        {"skill": "stock_query", "instruction": "查询茅台股价", "purpose": "获取实时价格"},
        {"skill": "news", "instruction": "查询茅台新闻", "purpose": "获取最新消息"},
    ]

    # past_steps 中已有"获取实时价格"
    past_steps = [("step1: 获取实时价格", "some result")]
    original_plan = []
    current_step = 0

    unique, reasons, replace_map = dedup_new_steps(new_steps, past_steps, original_plan, current_step)

    # "获取实时价格" 与 past_steps 中的 purpose 重叠 > 0.6，应被去重
    assert len(unique) == 1, f"期望 1 个去重后步骤，实际 {len(unique)}"
    assert len(reasons) == 1, f"期望 1 个跳过原因，实际 {len(reasons)}"


def test_detect_loop():
    """验证循环检测功能"""
    from agents.plan.analyze import detect_loop

    # 无历史
    assert detect_loop([], "continue", []) == False

    # 有历史但不同
    history = ["continue:[{'skill': 'a'}]"]
    assert detect_loop(history, "continue", [{"skill": "b"}]) == False

    # 有历史且相同（循环）
    history = ['continue:[{"skill": "a"}]']
    assert detect_loop(history, "continue", [{"skill": "a"}]) == True


# ============================================================
# 3. 数据源故障转移
# ============================================================

def test_datasource_manager_mark_failed():
    """验证数据源失败计数（通过 CircuitBreaker）"""
    from tools.fetcher.base import DataSourceManager, DataSource
    from utils.circuit_breaker import CircuitBreaker

    # 创建 mock 数据源
    source = MagicMock(spec=DataSource)
    source.name = "test_source"
    source.priority = 100
    source.enabled = True
    source.is_available.return_value = True

    # 重置状态
    DataSourceManager._sources = [source]
    DataSourceManager._circuit_breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=600)
    DataSourceManager._current_source_index = 0

    # 标记失败
    DataSourceManager.mark_source_failed("test_source")
    cb_state = DataSourceManager._circuit_breaker._get_state("test_source")
    assert cb_state["failures"] == 1

    # 继续失败
    DataSourceManager.mark_source_failed("test_source")
    assert cb_state["failures"] == 2


def test_datasource_manager_auto_disable():
    """验证连续失败达到阈值后 CircuitBreaker 熔断"""
    from tools.fetcher.base import DataSourceManager, DataSource
    from utils.circuit_breaker import CircuitBreaker

    # 创建 mock 数据源
    source = MagicMock(spec=DataSource)
    source.name = "test_source"
    source.priority = 100
    source.enabled = True
    source.is_available.return_value = True

    # 重置状态
    DataSourceManager._sources = [source]
    DataSourceManager._circuit_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=600)
    DataSourceManager._current_source_index = 0

    # 连续失败达到阈值
    for _ in range(3):
        DataSourceManager.mark_source_failed("test_source")

    # CircuitBreaker 应该处于 OPEN 状态
    assert DataSourceManager._circuit_breaker.is_available("test_source") == False


def test_datasource_manager_reset():
    """验证手动重置数据源"""
    from tools.fetcher.base import DataSourceManager, DataSource
    from utils.circuit_breaker import CircuitBreaker

    # 创建 mock 数据源
    source = MagicMock(spec=DataSource)
    source.name = "test_source"
    source.priority = 100
    source.enabled = False

    # 重置状态
    DataSourceManager._sources = [source]
    DataSourceManager._circuit_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=600)
    # 模拟连续失败后熔断
    for _ in range(3):
        DataSourceManager._circuit_breaker.record_failure("test_source")

    # 手动重置
    DataSourceManager.reset_source("test_source")

    assert source.enabled == True
    assert DataSourceManager._circuit_breaker.is_available("test_source") == True


def test_datasource_manager_switch():
    """验证数据源切换"""
    from tools.fetcher.base import DataSourceManager, DataSource
    from utils.circuit_breaker import CircuitBreaker

    # 创建两个 mock 数据源
    source1 = MagicMock(spec=DataSource)
    source1.name = "source1"
    source1.priority = 100
    source1.enabled = True
    source1.is_available.return_value = True

    source2 = MagicMock(spec=DataSource)
    source2.name = "source2"
    source2.priority = 90
    source2.enabled = True
    source2.is_available.return_value = True

    # 重置状态
    DataSourceManager._sources = [source1, source2]
    DataSourceManager._circuit_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=600)
    DataSourceManager._current_source_index = 0

    # 切换
    result = DataSourceManager.switch_source()
    assert result == True
    assert DataSourceManager._current_source_index == 1


def test_datasource_manager_status():
    """验证数据源状态查询"""
    from tools.fetcher.base import DataSourceManager, DataSource
    from utils.circuit_breaker import CircuitBreaker

    # 创建 mock 数据源
    source = MagicMock(spec=DataSource)
    source.name = "test_source"
    source.priority = 100
    source.enabled = True
    source.is_available.return_value = True

    # 重置状态
    DataSourceManager._sources = [source]
    DataSourceManager._circuit_breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=600)
    DataSourceManager._circuit_breaker.record_failure("test_source")
    DataSourceManager._circuit_breaker.record_failure("test_source")

    status = DataSourceManager.status()
    assert "test_source" in status
    assert status["test_source"]["priority"] == 100
    assert status["test_source"]["enabled"] == True
    assert status["test_source"]["circuit_breaker"] == "closed"


def test_retry_decorator_no_sources():
    """验证没有可用数据源时快速失败"""
    from tools.fetcher.base import retry, DataSourceManager
    from utils.circuit_breaker import CircuitBreaker

    # 重置状态，无数据源
    DataSourceManager._sources = []
    DataSourceManager._circuit_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=600)

    @retry(max_retries=3, base_delay=0.01, max_delay=0.05, switch_on_fail=False)
    def test_func():
        return "success"

    try:
        test_func()
        assert False, "应该抛出 RuntimeError"
    except RuntimeError as e:
        assert "没有可用的数据源" in str(e)


def test_retry_request_failed_sources_reset_between_calls(monkeypatch):
    """普通连续调用之间不应共享 request 级失败数据源缓存。"""
    from tools.fetcher.base import DataSource, DataSourceManager, retry, _request_failed_sources
    from utils.circuit_breaker import CircuitBreaker

    calls = {"bad": 0, "good": 0}

    class BadSource(DataSource):
        name = "bad"
        priority = 100
        enabled = True

    class GoodSource(DataSource):
        name = "good"
        priority = 90
        enabled = True

    monkeypatch.setattr(DataSourceManager, "_sources", [BadSource, GoodSource])
    monkeypatch.setattr(DataSourceManager, "_circuit_breaker", CircuitBreaker(failure_threshold=5, cooldown_seconds=600))
    monkeypatch.setattr(DataSourceManager, "_current_source_index", 0)

    @retry(max_retries=2, base_delay=0, max_delay=0)
    def read_value():
        source = DataSourceManager.get_current_source()
        if source.name == "bad":
            calls["bad"] += 1
            raise RuntimeError("bad source failed")
        calls["good"] += 1
        return "ok"

    token = _request_failed_sources.set(None)
    try:
        assert read_value() == "ok"
        assert read_value() == "ok"
    finally:
        _request_failed_sources.reset(token)

    assert calls == {"bad": 2, "good": 2}


# ============================================================
# 4. LLM 调用重试
# ============================================================

def test_extract_json_direct():
    """验证直接 JSON 解析"""
    from utils.llm_factory import extract_json

    # 直接 JSON
    result = extract_json('{"key": "value"}')
    assert result == {"key": "value"}

    # 带空格
    result = extract_json('  {"key": "value"}  ')
    assert result == {"key": "value"}


def test_extract_json_code_block():
    """验证代码块中的 JSON 解析"""
    from utils.llm_factory import extract_json

    # markdown 代码块
    result = extract_json('```json\n{"key": "value"}\n```')
    assert result == {"key": "value"}

    # 不带语言标记
    result = extract_json('```\n{"key": "value"}\n```')
    assert result == {"key": "value"}


def test_extract_json_nested():
    """验证嵌套 JSON 解析"""
    from utils.llm_factory import extract_json

    # 嵌套对象
    result = extract_json('{"outer": {"inner": "value"}}')
    assert result == {"outer": {"inner": "value"}}

    # 嵌套数组
    result = extract_json('[{"key": "value"}, {"key2": "value2"}]')
    assert result == [{"key": "value"}, {"key2": "value2"}]


def test_extract_json_invalid():
    """验证无效 JSON 返回 None"""
    from utils.llm_factory import extract_json

    # 非 JSON
    result = extract_json("这不是 JSON")
    assert result is None

    # 空字符串
    result = extract_json("")
    assert result is None


def test_extract_json_recovers_respond_with_unescaped_quotes():
    """replanner 最终报告里有未转义引号时，仍应保留完整 response。"""
    from utils.llm_factory import extract_json

    raw = (
        '{\n'
        '  "action": "respond",\n'
        '  "response": "# 经济日报\\n\\n'
        '## 一句话总结\\n'
        '**今日A股呈现"内强外扰"格局**：工业利润改善。\\n\\n'
        '| 指标 | 数值 |\\n| --- | --- |\\n| 上证指数 | -1.25% |"\n'
        '}'
    )

    result = extract_json(raw)

    assert result["action"] == "respond"
    assert '# 经济日报\n\n## 一句话总结' in result["response"]
    assert '呈现"内强外扰"格局' in result["response"]
    assert '上证指数 | -1.25%' in result["response"]


def test_llm_json_with_retry_success():
    """验证 LLM JSON 重试成功"""
    from utils.llm_factory import llm_json_with_retry

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = '{"result": "success"}'
    mock_llm.invoke.return_value = mock_response

    mock_logger = MagicMock()

    result = llm_json_with_retry(mock_llm, [], mock_logger, max_retries=3)
    assert result == {"result": "success"}


def test_llm_json_with_retry_failure():
    """验证 LLM JSON 重试最终失败"""
    from utils.llm_factory import llm_json_with_retry

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "这不是 JSON"  # 始终返回无效 JSON
    mock_llm.invoke.return_value = mock_response

    mock_logger = MagicMock()

    result = llm_json_with_retry(mock_llm, [], mock_logger, max_retries=3)
    assert result is None
    # 应该记录错误日志
    mock_logger.error.assert_called()


def test_llm_json_with_retry_recovery():
    """验证 LLM JSON 重试后恢复"""
    from utils.llm_factory import llm_json_with_retry

    mock_llm = MagicMock()
    # 第一次返回无效 JSON，第二次返回有效 JSON
    mock_response1 = MagicMock()
    mock_response1.content = "无效输出"
    mock_response2 = MagicMock()
    mock_response2.content = '{"result": "recovered"}'
    mock_llm.invoke.side_effect = [mock_response1, mock_response2]

    mock_logger = MagicMock()

    result = llm_json_with_retry(mock_llm, [], mock_logger, max_retries=3)
    assert result == {"result": "recovered"}


if __name__ == "__main__":
    import traceback
    tests = [
        # 1. 预算控制器边界
        test_budget_enable_disable_toggle,
        test_budget_reset_after_exceeded,
        test_budget_concurrent_check,
        test_budget_status_percentages,
        test_budget_status_overflow,
        test_budget_factory_singleton,
        test_budget_multiple_limits,
        # 2. 错误指纹
        test_error_fingerprint_basic,
        test_error_fingerprint_code_related,
        test_is_abnormal_result,
        test_dedup_new_steps,
        test_detect_loop,
        # 3. 数据源故障转移
        test_datasource_manager_mark_failed,
        test_datasource_manager_auto_disable,
        test_datasource_manager_reset,
        test_datasource_manager_switch,
        test_datasource_manager_status,
        test_retry_decorator_no_sources,
        # 4. LLM 调用重试
        test_extract_json_direct,
        test_extract_json_code_block,
        test_extract_json_nested,
        test_extract_json_invalid,
        test_extract_json_recovers_respond_with_unescaped_quotes,
        test_llm_json_with_retry_success,
        test_llm_json_with_retry_failure,
        test_llm_json_with_retry_recovery,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"[PASS] {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {test.__name__}: {e}")
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("[OK] 所有快速失败测试通过!")
    print(f"{'='*60}")

    sys.exit(1 if failed > 0 else 0)
