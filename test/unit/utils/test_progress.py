"""
Test: 流式输出 ProgressReporter
验证: utils/progress.py - ProgressReporter 事件收集
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_progress_event_collection():
    print("=" * 60)
    print("TC-Progress-01: 事件收集完整性")
    print("=" * 60)

    from utils.progress import ProgressReporter, ProgressType, ProgressEvent

    events_received = []

    def capture_callback(event):
        events_received.append(event)

    reporter = ProgressReporter(capture_callback)

    reporter.start(total_steps=5)
    reporter.classifier_result(True, "complex")
    reporter.planner_result(3)
    reporter.step_start(1, "[mx_search] 搜索资讯")
    reporter.step_complete(1, "找到 15 条相关资讯")
    reporter.tool_call("mx_search_news", "电池板块今日行情")
    reporter.tool_result("mx_search_news", "15 条结果")
    reporter.milestone("获取到关键数据", {"stocks": ["宁德时代", "比亚迪"]})
    reporter.warning("Token 消耗较高")
    reporter.error("工具执行失败")
    reporter.final("分析完成")

    print(f"  收到事件数: {len(events_received)} (预期 11)")
    assert len(events_received) == 11, f"Expected 11 events, got {len(events_received)}"

    assert events_received[0].type == ProgressType.START
    assert events_received[1].type == ProgressType.CLASSIFIER
    assert events_received[2].type == ProgressType.PLANNER
    assert events_received[3].type == ProgressType.STEP_START
    assert events_received[4].type == ProgressType.STEP_COMPLETE
    assert events_received[5].type == ProgressType.TOOL_CALL
    assert events_received[6].type == ProgressType.TOOL_RESULT
    assert events_received[7].type == ProgressType.MILESTONE
    assert events_received[8].type == ProgressType.WARNING
    assert events_received[9].type == ProgressType.ERROR
    assert events_received[10].type == ProgressType.FINAL

    print(f"  START: [event type confirmed]")
    print(f"  CLASSIFIER: [event type confirmed]")
    print(f"  PLANNER: [event type confirmed]")
    print(f"  FINAL: [event type confirmed]")

    print("[OK] 事件收集完整性验证通过")


def test_progress_callback_exception():
    print("\n" + "=" * 60)
    print("TC-Progress-02: 回调异常不影响主流程")
    print("=" * 60)

    from utils.progress import ProgressReporter, ProgressType

    def bad_callback(event):
        raise RuntimeError("Callback error!")

    reporter = ProgressReporter(bad_callback)

    try:
        reporter.start()
        reporter.step_start(1, "test")
        reporter.step_complete(1, "done")
        reporter.final("complete")

        print("  回调抛出异常后主流程继续执行")
        assert len(reporter.events) == 4, f"Expected 4 events, got {len(reporter.events)}"
        print(f"  事件数: {len(reporter.events)}")
        print("[OK] 回调异常不影响主流程")
    except RuntimeError:
        assert False, "Callback exception should be caught"


def test_progress_event_to_dict():
    print("\n" + "=" * 60)
    print("TC-Progress-03: ProgressEvent.to_dict() 序列化")
    print("=" * 60)

    from utils.progress import ProgressReporter, ProgressType, ProgressEvent

    reporter = ProgressReporter()
    reporter.start(total_steps=5)

    event = reporter.events[0]
    d = event.to_dict()

    print(f"  to_dict keys: {list(d.keys())}")
    assert "type" in d
    assert "message" in d
    assert "timestamp" in d
    assert d["type"] == "start"
    print("[OK] to_dict 序列化正确")


def test_progress_reporter_step_metadata():
    print("\n" + "=" * 60)
    print("TC-Progress-04: 步骤元数据正确传递")
    print("=" * 60)

    from utils.progress import ProgressReporter, ProgressType

    reporter = ProgressReporter()
    reporter.step_start(3, "[mx_data] 查询股票数据")
    reporter.step_complete(3, "获取到 10 只股票数据")
    reporter.milestone("数据获取完成", {"count": 10, "sector": "电池"})

    step_start_event = reporter.events[0]
    step_complete_event = reporter.events[1]
    milestone_event = reporter.events[2]

    assert step_start_event.step == 3
    assert step_complete_event.step == 3
    assert milestone_event.data == {"count": 10, "sector": "电池"}

    print(f"  Step 3 开始: [confirmed step={step_start_event.step}]")
    print(f"  Step 3 完成: [confirmed step={step_complete_event.step}]")
    print(f"  里程碑数据: [confirmed]")
    print("[OK] 步骤元数据正确传递")


if __name__ == "__main__":
    try:
        test_progress_event_collection()
        test_progress_callback_exception()
        test_progress_event_to_dict()
        test_progress_reporter_step_metadata()
        print("\n" + "=" * 60)
        print("[PASS] All progress tests passed!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)