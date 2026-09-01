"""
Plan 包单元测试

验证 plan 包中的纯逻辑函数：
1. analyze.py — 分析工具函数
2. replanner.py — format_step_status
3. classifier.py — classifier_should_end
4. graph.py — replanner_should_end

运行方式：
  pytest test/unit/test_plan_package.py -v
"""
import os
import sys
from unittest.mock import patch

# 确保项目根目录在 sys.path 中
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ============================================================
# 1. has_useful_results
# ============================================================

def test_has_useful_results_empty():
    """空 past_steps 应返回 False"""
    from agents.plan.analyze import has_useful_results
    assert has_useful_results({"past_steps": []}) == False


def test_has_useful_results_short():
    """短结果（<=50 字符）应返回 False"""
    from agents.plan.analyze import has_useful_results
    state = {"past_steps": [("step1", "短结果")]}
    assert has_useful_results(state) == False


def test_has_useful_results_long():
    """长结果（>50 字符）应返回 True"""
    from agents.plan.analyze import has_useful_results
    long_result = "这是一个很长的结果" * 10  # 100 字符
    state = {"past_steps": [("step1", long_result)]}
    assert has_useful_results(state) == True


def test_has_useful_results_mixed():
    """混合长短结果，有长结果应返回 True"""
    from agents.plan.analyze import has_useful_results
    long_result = "很长的结果" * 20
    state = {"past_steps": [("step1", "短"), ("step2", long_result)]}
    assert has_useful_results(state) == True


def test_has_useful_results_missing_key():
    """缺少 past_steps key 应返回 False"""
    from agents.plan.analyze import has_useful_results
    assert has_useful_results({}) == False


def test_plan_executor_imports_with_installed_langchain_agent_api():
    """executor 节点可正常导入。"""
    from agents.plan.node.executor import execute_step
    assert callable(execute_step)


# ============================================================
# 2. check_termination
# ============================================================

def test_check_termination_all_completed():
    """所有步骤完成应返回终止"""
    from agents.plan.analyze import check_termination
    state = {"current_step": 3}
    done, reason = check_termination(state, original_plan_len=3)
    assert done == True
    assert reason == "all_steps_completed"


def test_check_termination_max_steps():
    """超过最大步数应返回终止"""
    from agents.plan.analyze import check_termination
    with patch("agents.plan.analyze.Config") as mock_config:
        mock_config.PLAN_MAX_STEPS = 5
        state = {"current_step": 5}
        done, reason = check_termination(state, original_plan_len=10)
        assert done == True
        assert reason == "max_steps_reached"


def test_check_termination_too_many_replans():
    """过多重新规划应返回终止"""
    from agents.plan.analyze import check_termination
    state = {"current_step": 1, "_replan_loop_count": 3}
    done, reason = check_termination(state, original_plan_len=5)
    assert done == True
    assert reason == "too_many_replans"


def test_check_termination_continue():
    """未完成且未超限应返回继续"""
    from agents.plan.analyze import check_termination
    state = {"current_step": 1, "_replan_loop_count": 0}
    done, reason = check_termination(state, original_plan_len=5)
    assert done == False
    assert reason is None


# ============================================================
# 3. extract_step_info
# ============================================================

def test_extract_step_info_valid():
    """有效索引应返回步骤信息"""
    from agents.plan.analyze import extract_step_info
    plan = [
        {"skill": "stock_query", "instruction": "查询股价", "purpose": "获取价格"},
        {"skill": "news", "instruction": "查询新闻", "purpose": "获取消息"},
    ]
    info = extract_step_info(plan, 0)
    assert info["skill_name"] == "stock_query"
    assert info["instruction"] == "查询股价"
    assert info["purpose"] == "获取价格"


def test_extract_step_info_out_of_bounds():
    """越界索引应返回 None"""
    from agents.plan.analyze import extract_step_info
    plan = [{"skill": "a"}]
    assert extract_step_info(plan, 5) is None
    assert extract_step_info(plan, -1) is None


def test_extract_step_info_empty_plan():
    """空计划应返回 None"""
    from agents.plan.analyze import extract_step_info
    assert extract_step_info([], 0) is None


def test_extract_step_info_missing_keys():
    """缺少字段的步骤应返回默认值"""
    from agents.plan.analyze import extract_step_info
    plan = [{"skill": "test"}]
    info = extract_step_info(plan, 0)
    assert info["instruction"] == ""
    assert info["purpose"] == ""


# ============================================================
# 4. format_step_status
# ============================================================

def test_format_step_status_all_completed():
    """所有步骤完成时应全部显示 ✅"""
    from agents.plan.node.replanner import format_step_status
    plan = [
        {"step": 1, "purpose": "查询股价"},
        {"step": 2, "purpose": "查询新闻"},
    ]
    past_steps = [("Step 1: 查询股价", "result1"), ("Step 2: 查询新闻", "result2")]
    result = format_step_status(plan, past_steps, current_step=2)
    assert "✅" in result
    assert "查询股价" in result
    assert "查询新闻" in result


def test_format_step_status_current_step():
    """当前步骤应显示 🔄"""
    from agents.plan.node.replanner import format_step_status
    plan = [
        {"step": 1, "purpose": "查询股价"},
        {"step": 2, "purpose": "查询新闻"},
    ]
    past_steps = [("Step 1: 查询股价", "result1")]
    result = format_step_status(plan, past_steps, current_step=1)
    assert "🔄" in result
    assert "当前执行中" in result


def test_format_step_status_pending():
    """未执行步骤应显示 ⬜"""
    from agents.plan.node.replanner import format_step_status
    plan = [
        {"step": 1, "purpose": "查询股价"},
        {"step": 2, "purpose": "查询新闻"},
        {"step": 3, "purpose": "生成报告"},
    ]
    past_steps = [("Step 1: 查询股价", "result1")]
    result = format_step_status(plan, past_steps, current_step=1)
    assert "⬜" in result
    assert "生成报告" in result


# ============================================================
# 5. classifier_should_end / replanner_should_end
# ============================================================

def test_classifier_should_end_with_response():
    """有 response 时应返回 END"""
    from agents.plan.node.classifier import classifier_should_end
    result = classifier_should_end({"response": "答案"})
    assert result == "__end__"


def test_classifier_should_end_no_response():
    """无 response 时应返回 planner"""
    from agents.plan.node.classifier import classifier_should_end
    result = classifier_should_end({"response": ""})
    assert result == "planner"


def test_classifier_should_end_none_response():
    """None response 时应返回 planner"""
    from agents.plan.node.classifier import classifier_should_end
    result = classifier_should_end({"response": None})
    assert result == "planner"


def test_replanner_should_end_with_response():
    """有 response 时应返回 END"""
    from agents.plan.graph import replanner_should_end
    result = replanner_should_end({"response": "答案"})
    assert result == "__end__"


def test_replanner_should_end_no_response():
    """无 response 时应返回 executor"""
    from agents.plan.graph import replanner_should_end
    result = replanner_should_end({"response": ""})
    assert result == "executor"


# ============================================================
# 6. detect_loop
# ============================================================

def test_detect_loop_empty_history():
    """空历史应返回 False"""
    from agents.plan.analyze import detect_loop
    assert detect_loop([], "continue", []) == False


def test_detect_loop_different():
    """不同指纹应返回 False"""
    from agents.plan.analyze import detect_loop
    history = ["continue:[{\"skill\": \"a\"}]"]
    assert detect_loop(history, "continue", [{"skill": "b"}]) == False


def test_detect_loop_same():
    """相同指纹应返回 True"""
    from agents.plan.analyze import detect_loop
    history = ['continue:[{"skill": "a"}]']
    assert detect_loop(history, "continue", [{"skill": "a"}]) == True


def test_detect_loop_non_continue():
    """非 continue action 应返回 False"""
    from agents.plan.analyze import detect_loop
    history = ['finish:[{"skill": "a"}]']
    assert detect_loop(history, "finish", [{"skill": "a"}]) == False


# ============================================================
# 7. match_pending_steps
# ============================================================

def test_match_pending_empty():
    """无未执行步骤时返回空映射"""
    from agents.plan.analyze import match_pending_steps
    result = match_pending_steps([{"instruction": "a"}], [], 0)
    assert result == {}


def test_match_pending_basic():
    """新步骤与未执行步骤匹配"""
    from agents.plan.analyze import match_pending_steps
    new_steps = [{"instruction": "查询茅台股价"}]
    plan = [{"instruction": "查询茅台股价数据"}]
    result = match_pending_steps(new_steps, plan, 0)
    assert result == {0: 0}


def test_match_pending_no_match():
    """新步骤与未执行步骤不匹配（overlap < threshold）"""
    from agents.plan.analyze import match_pending_steps
    new_steps = [{"instruction": "查询新闻资讯"}]
    plan = [{"instruction": "分析K线走势"}]
    result = match_pending_steps(new_steps, plan, 0)
    assert result == {}


def test_match_pending_multi_to_one_best():
    """多个新步骤匹配同一老步骤 → 取 overlap 最高的"""
    from agents.plan.analyze import match_pending_steps
    new_steps = [
        {"instruction": "查询茅台实时股价"},  # overlap 较高
        {"instruction": "获取茅台股价信息"},  # overlap 也较高但稍低
    ]
    plan = [{"instruction": "查询茅台股价"}]
    result = match_pending_steps(new_steps, plan, 0)
    assert 0 in result  # 第一个匹配
    assert 1 not in result  # 第二个不匹配（已被占用）


def test_match_pending_threshold_boundary():
    """overlap 边界值测试 — 完全不同的指令不匹配"""
    from agents.plan.analyze import match_pending_steps
    # 构造完全不同的指令
    new_steps = [{"instruction": "分析K线走势形态"}]
    plan = [{"instruction": "获取新闻舆情数据"}]
    result = match_pending_steps(new_steps, plan, 0, threshold=0.6)
    assert result == {}


# ============================================================
# 8. dedup_new_steps
# ============================================================

def test_dedup_no_past():
    """无历史步骤时不应去重"""
    from agents.plan.analyze import dedup_new_steps
    new_steps = [{"skill": "a", "instruction": "do a", "purpose": "get a"}]
    unique, reasons, replace_map = dedup_new_steps(new_steps, [], [], 0)
    assert len(unique) == 1
    assert replace_map == {}


def test_dedup_by_purpose():
    """与已完成步骤目的相似应被去重"""
    from agents.plan.analyze import dedup_new_steps
    new_steps = [{"skill": "a", "instruction": "查询茅台股价", "purpose": "获取实时价格"}]
    past_steps = [("step1: 获取实时价格", "done")]
    unique, reasons, replace_map = dedup_new_steps(new_steps, past_steps, [], 0)
    assert len(unique) == 0
    assert len(reasons) == 1
    assert replace_map == {}


def test_dedup_by_pending_replaces():
    """与待执行步骤指令相似的新步骤应被保留用于替换（而非过滤）"""
    from agents.plan.analyze import dedup_new_steps
    new_steps = [{"skill": "a", "instruction": "查询茅台股价", "purpose": "获取价格"}]
    original_plan = [{"instruction": "查询茅台股价", "purpose": "获取价格"}]
    unique, reasons, replace_map = dedup_new_steps(new_steps, [], original_plan, 0)
    # 新逻辑：与未执行步骤匹配 → 保留用于替换，不过滤
    assert len(unique) == 1
    assert len(reasons) == 0
    assert replace_map == {0: 0}  # new_idx 0 → old_plan_idx 0


def test_dedup_all_unique():
    """完全不同步骤不应被去重"""
    from agents.plan.analyze import dedup_new_steps
    new_steps = [
        {"skill": "a", "instruction": "查询茅台股价", "purpose": "获取价格"},
        {"skill": "b", "instruction": "查询新闻", "purpose": "获取消息"},
    ]
    unique, reasons, replace_map = dedup_new_steps(new_steps, [], [], 0)
    assert len(unique) == 2
    assert replace_map == {}


def test_dedup_multi_replace():
    """多个新步骤：2个匹配未执行步骤（替换），1个全新（追加）"""
    from agents.plan.analyze import dedup_new_steps
    new_steps = [
        {"skill": "a", "instruction": "查询茅台股价", "purpose": "获取价格"},
        {"skill": "b", "instruction": "分析茅台K线", "purpose": "技术分析"},
        {"skill": "c", "instruction": "查询新闻资讯", "purpose": "获取消息"},
    ]
    original_plan = [
        {"instruction": "查询茅台股价", "purpose": "获取价格"},
        {"instruction": "分析茅台K线", "purpose": "技术分析"},
        {"instruction": "查询行业排名", "purpose": "行业对比"},
    ]
    unique, reasons, replace_map = dedup_new_steps(new_steps, [], original_plan, 0)
    assert len(unique) == 3  # 全部保留
    assert len(replace_map) == 2  # 2个替换映射
    assert 0 in replace_map and 1 in replace_map  # 前两个匹配
    assert 2 not in replace_map  # 第三个是全新步骤


def test_dedup_completed_priority_over_pending():
    """新步骤同时匹配已完成和未执行步骤时，已完成优先（过滤）"""
    from agents.plan.analyze import dedup_new_steps
    new_steps = [{"skill": "a", "instruction": "查询茅台股价", "purpose": "获取价格"}]
    past_steps = [("step1: 获取茅台实时价格", "done")]
    original_plan = [{"instruction": "查询茅台股价数据", "purpose": "获取股价"}]
    unique, reasons, replace_map = dedup_new_steps(new_steps, past_steps, original_plan, 0)
    # 已完成步骤优先 → 过滤
    assert len(unique) == 0
    assert len(reasons) == 1
    assert replace_map == {}


# ============================================================
# 9. error_fingerprint（补充覆盖）
# ============================================================

def test_error_fingerprint_timeout():
    """timeout 相关错误应有特殊指纹"""
    from agents.plan.analyze import error_fingerprint
    fp = error_fingerprint(TimeoutError("connection timeout"))
    assert "timeout" in fp


def test_error_fingerprint_recursion():
    """recursion 相关错误应有特殊指纹"""
    from agents.plan.analyze import error_fingerprint
    fp = error_fingerprint(RecursionError("maximum recursion depth"))
    assert "recursion" in fp


if __name__ == "__main__":
    import traceback
    tests = [
        test_has_useful_results_empty,
        test_has_useful_results_short,
        test_has_useful_results_long,
        test_has_useful_results_mixed,
        test_has_useful_results_missing_key,
        test_check_termination_all_completed,
        test_check_termination_max_steps,
        test_check_termination_too_many_replans,
        test_check_termination_continue,
        test_extract_step_info_valid,
        test_extract_step_info_out_of_bounds,
        test_extract_step_info_empty_plan,
        test_extract_step_info_missing_keys,
        test_format_step_status_all_completed,
        test_format_step_status_current_step,
        test_format_step_status_pending,
        test_classifier_should_end_with_response,
        test_classifier_should_end_no_response,
        test_classifier_should_end_none_response,
        test_replanner_should_end_with_response,
        test_replanner_should_end_no_response,
        test_detect_loop_empty_history,
        test_detect_loop_different,
        test_detect_loop_same,
        test_detect_loop_non_continue,
        test_dedup_no_past,
        test_dedup_by_purpose,
        test_dedup_by_pending,
        test_dedup_all_unique,
        test_error_fingerprint_timeout,
        test_error_fingerprint_recursion,
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
        print("[OK] 所有 Plan 包测试通过!")
    print(f"{'='*60}")
    sys.exit(1 if failed > 0 else 0)
