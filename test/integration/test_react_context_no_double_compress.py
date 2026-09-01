"""ReAct 上下文压缩 e2e 测试：验证不会重复压缩。

模拟场景：11 轮对话 a~k，每轮消息很短 ('a'*10)，
用 recent_rounds=1, summary_trigger_rounds=3 配置压缩。

预期最终结构：[abc][def][ghi][j][k]
- abc: 第1次压缩 R1~R3
- def: 第2次压缩 R4~R6（只压缩新轮次，abc 不会再被压缩）
- ghi: 第3次压缩 R7~R9（只压缩新轮次，abc/def 不会被再压缩）
- j, k: 最近 1 轮保留原样

中途输出验证每步的压缩结果。
"""
import json

from langchain_core.messages import AIMessage, ToolMessage


def _round(letter: str, call_id: str):
    """创建一个轮次：AIMessage(tool_calls) + ToolMessage"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "mx_data_query",
                    "args": {"query": f"查询{letter}"},
                    "id": call_id,
                }
            ],
        ),
        ToolMessage(
            content=f"工具结果{letter}" + "x" * 10,  # 短输出
            name="mx_data_query",
            tool_call_id=call_id,
        ),
    ]


def test_no_double_compression_through_full_session():
    """11 轮对话 a~k 压缩全程验证：不会重复压缩已压缩内容。"""
    from agents.executor_callbacks import ExecutionState
    from agents.react.context import ReactContextBuilder

    all_summaries = []  # 记录每次压缩的结果
    summarize_call_count = [0]

    def fake_summarize(previous_summary, new_rounds_text, **kwargs):
        summarize_call_count[0] += 1
        # 从 new_rounds_text 的 tool_call 行提取轮次字母
        # 只匹配 tool_call 行中的 "query" 字段，不匹配 tool_result 行
        import re
        letters = re.findall(r'"query":\s*"查询([a-k])"', new_rounds_text)
        letters = sorted(set(letters))

        # 转换为整数索引（与真实代码一致：ReactRound.index 从 1 开始）
        letter_to_idx = {chr(ord('a') + i): i + 1 for i in range(11)}
        round_indices = [letter_to_idx[l] for l in letters if l in letter_to_idx]

        summary = {
            "summary_version": summarize_call_count[0],
            "summarized_rounds": round_indices,
            "core_facts": {"compressed": letters},
            "rounds_summary": f"压缩了 {', '.join(letters)}",
            "key_findings": "",
            "failed_queries": [],
        }
        all_summaries.append(summary)
        return summary

    exec_state = ExecutionState()
    builder = ReactContextBuilder(
        recent_rounds=1,           # 只保留最近 1 轮
        summary_trigger_rounds=3,  # 每 3 轮触发一次压缩
        summary_pending_chars=999999,  # 只按轮次触发
        summary_max_chars=500,
        summarizer=fake_summarize,
    )

    letters = list("abcdefghijk")  # 11 轮
    all_messages = [("system", "系统提示"), ("user", "当前任务")]

    for i, letter in enumerate(letters):
        all_messages.extend(_round(letter, f"call_{letter}"))

        compact = builder.build(
            messages=all_messages,
            user_input="当前任务",
            exec_state=exec_state,
            llm=None,
            logger=None,
        )

        # 打印每步的 compact 视图
        tool_ids = [msg.tool_call_id for msg in compact if isinstance(msg, ToolMessage)]
        summary_msgs = [
            msg for msg in compact
            if isinstance(msg, AIMessage) and "已压缩历史轮次摘要" in msg.content
        ]

        print(f"\n=== Round {i+1} ({letter}) ===")
        print(f"  summarize_calls: {summarize_call_count[0]}")
        print(f"  summarized_until: {exec_state.react_context_summarized_until}")
        print(f"  summaries count: {len(exec_state.react_context_summaries)}")
        print(f"  compact tool_ids: {tool_ids}")
        print(f"  summary count in compact: {len(summary_msgs)}")
        for s in summary_msgs:
            print(f"    {s.content[:80]}...")

    # ═══════════════════════════════════════════════════════════════
    # 最终验证
    # ═══════════════════════════════════════════════════════════════

    print("\n" + "=" * 60)
    print("最终验证")
    print("=" * 60)

    # 1. 共触发 3 次压缩（R1~3, R4~6, R7~9）
    assert summarize_call_count[0] == 3, f"预期 3 次压缩，实际 {summarize_call_count[0]}"

    # 2. summarized_until = 9
    assert exec_state.react_context_summarized_until == 9, (
        f"预期 summarized_until=9，实际 {exec_state.react_context_summarized_until}"
    )

    # 3. 有 3 个 summary
    assert len(exec_state.react_context_summaries) == 3, (
        f"预期 3 个 summary，实际 {len(exec_state.react_context_summaries)}"
    )

    # 4. 最终 compact 结构验证
    final_compact = builder.build(
        messages=all_messages,
        user_input="当前任务",
        exec_state=exec_state,
        llm=None,
        logger=None,
    )

    final_summary_msgs = [
        msg for msg in final_compact
        if isinstance(msg, AIMessage) and "已压缩历史轮次摘要" in msg.content
    ]
    final_tool_msgs = [msg for msg in final_compact if isinstance(msg, ToolMessage)]

    print(f"\n最终 compact:")
    print(f"  总消息数: {len(final_compact)}")
    print(f"  summary 数: {len(final_summary_msgs)}")
    print(f"  tool 数: {len(final_tool_msgs)}")
    print(f"  tool_ids: {[m.tool_call_id for m in final_tool_msgs]}")

    # 应该有 3 个 summary + 2 个 tool (j, k)
    assert len(final_summary_msgs) == 3, f"预期 3 个 summary，实际 {len(final_summary_msgs)}"
    assert len(final_tool_msgs) == 2, f"预期 2 个 tool (j, k)，实际 {len(final_tool_msgs)}"
    assert final_tool_msgs[0].tool_call_id == "call_j"
    assert final_tool_msgs[1].tool_call_id == "call_k"

    # 5. 验证每个 summary 的内容没有重叠
    all_compressed = []
    for s in exec_state.react_context_summaries:
        compressed = s.get("core_facts", {}).get("compressed", [])
        # 检查没有重复压缩
        for c in compressed:
            assert c not in all_compressed, (
                f"发现重复压缩: {c} 已在 {all_compressed} 中"
            )
            all_compressed.append(c)

    print(f"\n所有压缩的轮次: {all_compressed}")
    assert all_compressed == ["a", "b", "c", "d", "e", "f", "g", "h", "i"], (
        f"预期 a~i 各压缩一次，实际 {all_compressed}"
    )

    # 6. 验证 new_rounds_text 中没有已压缩的轮次
    # 用 core_facts.compressed 检查字母（summarized_rounds 是整数索引）
    s1_compressed = all_summaries[1].get("core_facts", {}).get("compressed", [])
    s2_compressed = all_summaries[2].get("core_facts", {}).get("compressed", [])
    assert "a" not in s1_compressed
    assert "d" in s1_compressed
    assert "g" in s2_compressed
    assert "a" not in s2_compressed

    print("\n[PASS] 所有验证通过：不会重复压缩已压缩内容")


if __name__ == "__main__":
    test_no_double_compression_through_full_session()
