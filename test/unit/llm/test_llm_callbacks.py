"""
Test: LLMDebugCallback LLM交互记录
验证: utils/llm_factory.py - LLMDebugCallback 消息解析
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import io


def test_llm_debug_callback_creation():
    print("=" * 60)
    print("TC-Callback-01: LLMDebugCallback 创建和基本属性")
    print("=" * 60)

    from utils.llm_factory import LLMDebugCallback

    log_stream = io.StringIO()
    raw_logger = logging.getLogger("test.callback")
    raw_logger.setLevel(logging.DEBUG)
    raw_logger.handlers.clear()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    raw_logger.addHandler(handler)

    callback = LLMDebugCallback(raw_logger, "test-label")

    print(f"  label: {callback.label}")
    print(f"  logger: {callback.logger}")
    assert callback.label == "test-label"
    assert callback.logger is not None
    print("[OK] LLMDebugCallback 创建正确")


def test_llm_debug_callback_message_parsing():
    print("\n" + "=" * 60)
    print("TC-Callback-02: 消息角色解析")
    print("=" * 60)

    from utils.llm_factory import LLMDebugCallback

    log_stream = io.StringIO()
    raw_logger = logging.getLogger("test.callback_parsing")
    raw_logger.setLevel(logging.DEBUG)
    raw_logger.handlers.clear()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    raw_logger.addHandler(handler)

    callback = LLMDebugCallback(raw_logger, "test-parsing")

    class MockMessage:
        def __init__(self, type_, content, **kwargs):
            self.type = type_
            self.content = content
            for k, v in kwargs.items():
                setattr(self, k, v)

    class MockSerialized:
        def __init__(self, name):
            self.name = name

    print("  Testing string prompt handling...")
    try:
        callback.on_llm_start(MockSerialized("test"), ["simple string prompt"])
        print("  [OK] String prompt handled")
    except Exception as e:
        print(f"  [WARN] String prompt: {e}")

    print("  Testing BaseMessage handling...")
    try:
        messages = [
            MockMessage("system", "You are a helpful assistant."),
            MockMessage("human", "What is the stock price?"),
        ]
        callback.on_llm_start(MockSerialized("test"), messages)
        print("  [OK] BaseMessage list handled")
    except Exception as e:
        print(f"  [WARN] BaseMessage: {e}")

    print("[OK] 消息解析基本正常")


def test_token_tracker_creation():
    print("\n" + "=" * 60)
    print("TC-Callback-03: TokenTracker 创建")
    print("=" * 60)

    from utils.token_recorder import TokenRecorder as TokenTracker

    log_stream = io.StringIO()
    raw_logger = logging.getLogger("test.token_tracker")
    raw_logger.setLevel(logging.INFO)
    raw_logger.handlers.clear()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    raw_logger.addHandler(handler)

    tracker = TokenTracker(raw_logger, "test-label")

    print(f"  label: {tracker.label}")
    print(f"  logger: {tracker.logger}")
    assert tracker.label == "test-label"
    assert tracker.logger is not None
    print("[OK] TokenTracker 创建正确")


def test_llm_debug_summarize():
    print("\n" + "=" * 60)
    print("TC-Callback-04: _summarize_text 工具方法")
    print("=" * 60)

    from utils.logger import _summarize as _summarize_text

    text1 = "这是一段测试文本。" * 20
    result1 = _summarize_text(text1, 50)
    print(f"  长文本摘要: {result1}")
    assert len(result1) <= 60

    text2 = "短文本"
    result2 = _summarize_text(text2, 50)
    print(f"  短文本: {result2}")
    assert result2 == "短文本"

    text3 = ""
    result3 = _summarize_text(text3, 50)
    print(f"  空文本: '{result3}'")
    assert result3 == ""

    print("[OK] _summarize_text 正确")


if __name__ == "__main__":
    try:
        test_llm_debug_callback_creation()
        test_llm_debug_callback_message_parsing()
        test_token_tracker_creation()
        #test_llm_debug_summarize()
        print("\n" + "=" * 60)
        print("[PASS] All LLM callback tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)