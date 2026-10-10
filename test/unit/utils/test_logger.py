"""
Test: RadarLogger 结构化日志
验证: utils/logger.py - RadarLogger 格式和方法
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import io


def test_logger_format():
    print("=" * 60)
    print("TC-Logger-01: 日志格式 [tag] req=xxx")
    print("=" * 60)

    from utils.logger import RadarLogger

    log_stream = io.StringIO()
    raw_logger = logging.getLogger("test.logger_format")
    raw_logger.setLevel(logging.DEBUG)
    raw_logger.handlers.clear()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    raw_logger.addHandler(handler)

    logger = RadarLogger(raw_logger, "abc12345")
    logger.info("E", "Step 1 started", key="value")

    output = log_stream.getvalue()
    print(f"  输出: {output.strip()}")
    assert "[E]" in output, "Should contain [E] tag"
    assert "req=abc12345" in output, "Should contain req_id"
    assert "Step 1 started" in output, "Should contain message"
    print("[OK] 日志格式正确")


def test_logger_lifecycle_methods():
    print("\n" + "=" * 60)
    print("TC-Logger-02: 步骤生命周期方法")
    print("=" * 60)

    from utils.logger import RadarLogger

    log_stream = io.StringIO()
    raw_logger = logging.getLogger("test.lifecycle")
    raw_logger.setLevel(logging.DEBUG)
    raw_logger.handlers.clear()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    raw_logger.addHandler(handler)

    logger = RadarLogger(raw_logger, "testreq")

    try:
        logger.phase("问题分类")
        logger.step_start(1, "mx_search", "搜索资讯", "搜索今天的电池板块新闻")
        logger.llm_call("classifier", 1.5, 168, 71)
        logger.tool_call("mx_search_news", {"query": "电池板块"})
        logger.tool_result("mx_search_news", "找到 15 条结果", chars=500, items=15)
        logger.step_end(1, True, "完成")
        logger.context_size(15000)
        logger.context_size(5000)
        logger.metrics_summary(type('MockCtx', (), {
            'elapsed': lambda self: 10.5,
            'metrics': {
                'llm_calls': 3,
                'llm_tokens_in': 1000,
                'llm_tokens_out': 500,
                'tool_calls': 5,
                'steps_total': 2,
                'steps_success': 2,
                'steps_failed': 0,
                'retries': 1,
            }
        })())

        output = log_stream.getvalue()
        print("  阶段: 问题分类 - OK")
        print("  步骤开始: OK")
        print("  LLM调用: OK")
        print("  工具调用/结果: OK")
        print("  步骤结束: OK")
        print("  Context大小: OK")
        print("  汇总: OK")
        print("[OK] 生命周期方法正常")
    except Exception as e:
        print(f"  错误: {e}")
        raise


def test_child_logger_inheritance():
    print("\n" + "=" * 60)
    print("TC-Logger-03: 子 logger req_id 继承")
    print("=" * 60)

    from utils.logger import RadarLogger, RequestContext, get_child_logger

    RequestContext._current = None

    raw_logger = logging.getLogger("test.parent")
    ctx = RequestContext("parent123", raw_logger)
    RequestContext.set_current(ctx)

    child = get_child_logger("aggregator.temp")

    print(f"  父 req_id: {ctx.req_id}")
    print(f"  子 req_id: {child._req_id}")
    assert child._req_id == ctx.req_id[:8], f"Expected {ctx.req_id[:8]}, got {child._req_id}"

    RequestContext._current = None
    print("[OK] req_id 继承正确（截断为8位）")


def test_logger_req_id_prefix():
    print("\n" + "=" * 60)
    print("TC-Logger-04: req_id 只取前8位")
    print("=" * 60)

    from utils.logger import RadarLogger

    log_stream = io.StringIO()
    raw_logger = logging.getLogger("test.req8")
    raw_logger.setLevel(logging.DEBUG)
    raw_logger.handlers.clear()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    raw_logger.addHandler(handler)

    logger = RadarLogger(raw_logger, "abcdefghijklmnop")
    logger.info("R", "test message")

    output = log_stream.getvalue()
    print(f"  输出: {output.strip()}")
    assert "req=abcdefgh" in output, "Should truncate to 8 chars"
    assert "abcdefghijklmnop" not in output, "Should not contain full id"
    print("[OK] req_id 正确截断为8位")


def test_summarize_utils():
    print("\n" + "=" * 60)
    print("TC-Logger-05: _summarize 和 _summarize_params")
    print("=" * 60)

    from utils.logger import _summarize, _summarize_params

    text1 = "这是一段测试文本。" * 20
    result1 = _summarize(text1, 50)
    print(f"  原文长度: {len(text1)}, 摘要: {result1}")
    assert len(result1) <= 60

    text2 = "没有标点的文本" * 10
    result2 = _summarize(text2, 50)
    print(f"  无标点摘要: {result2}")
    assert len(result2) <= 60

    params = {"query": "电池板块今日行情分析", "limit": 10}
    result3 = _summarize_params(params)
    print(f"  参数摘要: {result3}")
    assert "电池板块" in result3

    params2 = {"stocks": ["宁德时代", "比亚迪"], "count": 2}
    result4 = _summarize_params(params2)
    print(f"  列表参数: {result4}")
    assert "list len=2" in result4

    print("[OK] 摘要工具正确")


def test_metrics_summary_cache_by_label():
    """metrics_summary 输出按 label 的缓存命中明细（ctx 与 snapshot 两种入口）。"""
    from utils.logger import RadarLogger, RequestContext

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    raw_logger = logging.getLogger("test.cache_by_label")
    raw_logger.setLevel(logging.DEBUG)
    raw_logger.handlers.clear()
    raw_logger.addHandler(_Capture())

    logger = RadarLogger(raw_logger, "cache01")
    ctx = RequestContext("cache01", raw_logger)
    ctx.record_llm(89000, 4000, 51000, 1000, label="react-sub")
    ctx.record_llm(36000, 12000, 0, 2000, label="react-tool-compress")
    logger.metrics_summary(ctx)

    details = [r for r in records if getattr(r, "_msg", "") == "缓存明细"]
    assert details, "ctx 路径应输出缓存明细行"
    detail = details[-1]._kv.get("按调用", "")
    assert "react-sub 57% (51k/89k)" in detail, f"实际: {detail}"
    assert "react-tool-compress 0% (0/36k)" in detail, f"实际: {detail}"

    records.clear()
    logger.metrics_summary({
        "elapsed_seconds": 1.0,
        "metrics": {
            "llm_calls": 2, "llm_tokens_in": 114527, "llm_tokens_out": 16000,
            "llm_tokens_reasoning": 3000, "llm_cached_tokens": 51000,
            "llm_cache_by_label": {
                "react-sub": {"in": 89000, "cached": 51000},
                "report-fast": {"in": 25527, "cached": 0},
            },
            "tool_calls": 19, "steps_total": 0, "steps_success": 0,
            "steps_failed": 0, "retries": 0,
        },
        "trace_totals": {},
        "budget": None,
        "consistency": {},
    })

    details = [r for r in records if getattr(r, "_msg", "") == "缓存明细"]
    assert details, "snapshot 路径应输出缓存明细行"
    detail = details[-1]._kv.get("按调用", "")
    assert "react-sub 57% (51k/89k)" in detail, f"应按输入量降序且紧凑格式化: {detail}"
    assert "report-fast 0% (0/25.5k)" in detail, f"应含 report-fast: {detail}"

    print("[OK] 缓存明细正确")


def _make_test_record(msg="原始消息", kv=None):
    """创建带 relpath 属性的测试 record"""
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg=msg, args=None, exc_info=None
    )
    record.relpath = "test.py"
    record._prefix = "[T]"
    record._msg = msg
    record._kv = kv or {"key": "value"}
    return record


def test_console_formatter_does_not_modify_record():
    """_ConsoleFormatter 格式化后不修改原始 record.msg"""
    from utils.logger import _ConsoleFormatter

    formatter = _ConsoleFormatter()
    record = _make_test_record()

    original_msg = record.msg
    formatter.format(record)
    assert record.msg == original_msg, \
        f"_ConsoleFormatter 不应修改原始 record.msg: 期望 '{original_msg}', 实际 '{record.msg}'"


def test_file_formatter_does_not_modify_record():
    """_FileFormatter 格式化后不修改原始 record.msg"""
    from utils.logger import _FileFormatter

    formatter = _FileFormatter()
    record = _make_test_record()

    original_msg = record.msg
    formatter.format(record)
    assert record.msg == original_msg, \
        f"_FileFormatter 不应修改原始 record.msg: 期望 '{original_msg}', 实际 '{record.msg}'"


def test_file_formatter_json_for_dict_kv():
    """_FileFormatter 对 dict/list kv 输出稳定 JSON"""
    from utils.logger import _FileFormatter

    formatter = _FileFormatter()
    record = _make_test_record("测试", {"data": {"nested": "value", "count": 42}})

    result1 = formatter.format(record)
    result2 = formatter.format(record)

    # 多次 format 应该产生一致的输出
    assert result1 == result2, "FileFormatter 多次 format 应产生一致输出"
    # dict 应该被 JSON 序列化
    assert '{"nested": "value", "count": 42}' in result1


if __name__ == "__main__":
    try:
        test_logger_format()
        test_logger_lifecycle_methods()
        test_child_logger_inheritance()
        test_logger_req_id_prefix()
        test_summarize_utils()
        test_metrics_summary_cache_by_label()
        print("\n" + "=" * 60)
        print("[PASS] All radar logger tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)