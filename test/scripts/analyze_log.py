"""Agent 日志分析脚本

用法：python test/scripts/analyze_log.py <log_path>

分析维度：
1. 执行概况：模式、问题、总耗时、LLM 调用次数、总 token
2. 执行流程：各阶段耗时占比、是否正常走完全流程
3. 工具调用：每个工具调用次数、是否有重复调用模式
4. 工具输出压缩：哪些工具被压缩、压缩比、是否有未压缩的大结果
5. 上下文压缩：compact 触发次数、窗口滑动 vs LLM summary、消息数增长趋势
6. 循环检测：是否触发、触发时机、对报告流程的影响
7. Token 趋势：每轮 token 增长（检测上下文膨胀）
8. 报告生成：report/dashboard 是否正常触发、是否被跳过
9. 异常：WARNING/ERROR、截断、失败
"""
import re
import sys
from collections import defaultdict
from pathlib import Path


def parse_log(log_path: str) -> dict:
    """解析日志文件，提取各维度数据。"""
    lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()

    result = {
        "meta": {},
        "phases": [],
        "llm_calls": [],
        "tool_calls_raw": [],       # 原始 tool_calls 日志行
        "tool_results": [],         # 工具执行结果（output_len）
        "compressions": [],         # 工具输出压缩
        "context_compacts": [],     # compact 日志
        "context_summaries": [],    # react-context-summary LLM 调用
        "loop_detections": [],
        "report_flow": [],          # report 相关节点
        "warnings": [],
        "errors": [],
    }

    # ── 基本信息 ──
    for line in lines:
        m = re.search(r"req=(\w+).*新对话开始", line)
        if m:
            result["meta"]["req_id"] = m.group(1)
        m = re.search(r"req=\w+\s+问题:\s*(.+)", line)
        if m:
            result["meta"]["question"] = m.group(1).strip()
        m = re.search(r"选中报告模板:\s*(\S+)", line)
        if m:
            result["meta"]["template"] = m.group(1)
        m = re.search(r"ReAct Graph 开始执行.*最大迭代\s*(\d+)", line)
        if m:
            result["meta"]["mode"] = "react"
            result["meta"]["max_iterations"] = int(m.group(1))
        # 时间范围
        ts_m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)", line)
        if ts_m:
            result["meta"].setdefault("first_ts", ts_m.group(1))
            result["meta"]["last_ts"] = ts_m.group(1)

    # ── 阶段 ──
    for line in lines:
        m = re.search(r"═══ 阶段:\s*(.+?)\s*═══", line)
        if m:
            ts = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)", line)
            result["phases"].append({"name": m.group(1), "time": ts.group(1) if ts else ""})

    # ── LLM 调用 ──
    for line in lines:
        m = re.search(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+).*LLM 调用\s+(\S+)\s*│\s*duration=([\d.]+)s\s*│\s*tokens=(\d+)\+(\d+)=(\d+)(?:.*cached=(\d+))?(?:.*hit=([\d.]+)%)?",
            line,
        )
        if m:
            result["llm_calls"].append({
                "time": m.group(1),
                "label": m.group(2),
                "duration": float(m.group(3)),
                "input_tokens": int(m.group(4)),
                "output_tokens": int(m.group(5)),
                "total_tokens": int(m.group(6)),
                "cached_tokens": int(m.group(7)) if m.group(7) else 0,
                "cache_hit_pct": float(m.group(8)) if m.group(8) else 0,
            })

    # ── 工具调用 ──
    for line in lines:
        m = re.search(r"LLM 返回\s+(\d+)\s+个 tool_calls:\s*\[(.+?)\]", line)
        if m:
            tools = [t.strip().strip("'\"") for t in m.group(2).split(",")]
            ts = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)", line)
            for t in tools:
                result["tool_calls_raw"].append({"name": t, "time": ts.group(1) if ts else ""})

    # ── 工具执行结果（检测大输出） ──
    for line in lines:
        m = re.search(r"← 工具完成 │ output_len=(\d+)", line)
        if m:
            ts = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)", line)
            result["tool_results"].append({
                "output_len": int(m.group(1)),
                "time": ts.group(1) if ts else "",
            })

    # ── 工具输出压缩 ──
    for line in lines:
        m = re.search(r"工具输出压缩:\s+(\S+)\s+(\d+)→(\d+)字符\s*\(([\d.]+)x\)", line)
        if m:
            result["compressions"].append({
                "tool": m.group(1),
                "before": int(m.group(2)),
                "after": int(m.group(3)),
                "ratio": float(m.group(4)),
            })

    # ── 上下文压缩（compact） ──
    for line in lines:
        m = re.search(
            r"react\.context\.compact\s+before_messages=(\d+)\s+after_messages=(\d+)\s+"
            r"rounds=(\d+)\s+recent_rounds=(\d+)\s+summaries=(\d+)\s+summary_chars=(\d+)",
            line,
        )
        if m:
            result["context_compacts"].append({
                "before": int(m.group(1)),
                "after": int(m.group(2)),
                "rounds": int(m.group(3)),
                "recent_rounds": int(m.group(4)),
                "summaries": int(m.group(5)),
                "summary_chars": int(m.group(6)),
            })

    # ── react-context-summary LLM 调用（区分于 compact 滑动窗口） ──
    for c in result["llm_calls"]:
        if c["label"] == "react-context-summary":
            result["context_summaries"].append(c)

    # ── 循环检测 ──
    for line in lines:
        if "循环检测" in line:
            ts = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)", line)
            result["loop_detections"].append({"time": ts.group(1) if ts else "", "line": line.strip()})

    # ── 报告流程 ──
    report_labels = ["report-fast", "report-expand", "dashboard", "sentiment-extract"]
    for c in result["llm_calls"]:
        if c["label"] in report_labels:
            result["report_flow"].append(c)

    # ── 确定模式 ──
    if not result["meta"].get("mode"):
        labels = {c["label"] for c in result["llm_calls"]}
        if "planner" in labels and "react-sub" in labels:
            result["meta"]["mode"] = "unified_plan"
        elif "pdor-planner" in labels:
            result["meta"]["mode"] = "pdor"
        elif "skill-selector" in labels:
            result["meta"]["mode"] = "react"
        else:
            result["meta"]["mode"] = "unknown"

    # ── WARNING / ERROR ──
    for line in lines:
        if "│ WARNING" in line:
            result["warnings"].append(line.strip())
        if "│ ERROR" in line:
            result["errors"].append(line.strip())

    return result


def _parse_ts(ts_str: str) -> float:
    """时间戳转秒数（用于计算耗时）。"""
    try:
        parts = ts_str.split(" ")
        h, m, s = parts[1].split(":")
        return float(h) * 3600 + float(m) * 60 + float(s)
    except Exception:
        return 0


def format_report(data: dict) -> str:
    """格式化分析报告。"""
    lines = []
    meta = data["meta"]
    llm = data["llm_calls"]

    # ═══════════════════════════════════════════════════
    # 1. 执行概况
    # ═══════════════════════════════════════════════════
    lines.append("=" * 60)
    lines.append("1. 执行概况")
    lines.append("=" * 60)
    lines.append(f"  模式:     {meta.get('mode', '?')}")
    lines.append(f"  问题:     {meta.get('question', '?')}")
    lines.append(f"  模板:     {meta.get('template', '?')}")
    if meta.get("max_iterations"):
        lines.append(f"  最大迭代: {meta['max_iterations']}")

    if llm:
        total_duration = sum(c["duration"] for c in llm)
        total_input = sum(c["input_tokens"] for c in llm)
        total_output = sum(c["output_tokens"] for c in llm)
        total_cached = sum(c["cached_tokens"] for c in llm)
        lines.append(f"  LLM 调用: {len(llm)} 次")
        lines.append(f"  总耗时:   {total_duration:.1f}s")
        lines.append(f"  总 token: {total_input + total_output:,} (输入 {total_input:,} + 输出 {total_output:,})")
        if total_cached and total_input:
            lines.append(f"  缓存命中: {total_cached:,} tokens ({total_cached / total_input * 100:.1f}%)")

    # ═══════════════════════════════════════════════════
    # 2. 执行流程（各阶段耗时）
    # ═══════════════════════════════════════════════════
    lines.append("")
    lines.append("=" * 60)
    lines.append("2. 执行流程")
    lines.append("=" * 60)
    by_label = defaultdict(list)
    for c in llm:
        by_label[c["label"]].append(c)

    # 按时间顺序排列 label
    seen = []
    for c in llm:
        if c["label"] not in seen:
            seen.append(c["label"])

    for label in seen:
        calls = by_label[label]
        dur = sum(c["duration"] for c in calls)
        tok = sum(c["total_tokens"] for c in calls)
        lines.append(f"  {label:30s}  {len(calls):2d} 次  {dur:7.1f}s  {tok:>8,} tokens")

    # 检查报告流程完整性
    report_labels_present = {c["label"] for c in data["report_flow"]}
    expected_reports = {"sentiment-extract", "dashboard"}
    if meta.get("mode") == "react":
        expected_reports.add("report-fast")
    missing = expected_reports - report_labels_present
    if missing:
        lines.append(f"  ⚠️ 缺失报告节点: {', '.join(sorted(missing))}")

    # ═══════════════════════════════════════════════════
    # 3. 工具调用分析
    # ═══════════════════════════════════════════════════
    lines.append("")
    lines.append("=" * 60)
    lines.append("3. 工具调用")
    lines.append("=" * 60)
    tool_counts = defaultdict(int)
    for tc in data["tool_calls_raw"]:
        tool_counts[tc["name"]] += 1
    for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        flag = " ⚠️ 重复调用过多" if count > 10 else ""
        lines.append(f"  {name:30s}  {count} 次{flag}")

    # 检测重复调用模式（同一工具连续调用）
    consecutive = []
    prev = None
    streak = 0
    for tc in data["tool_calls_raw"]:
        if tc["name"] == prev:
            streak += 1
        else:
            if streak >= 3:
                consecutive.append((prev, streak + 1))
            prev = tc["name"]
            streak = 0
    if streak >= 3:
        consecutive.append((prev, streak + 1))
    if consecutive:
        lines.append("  ⚠️ 连续重复调用:")
        for name, cnt in consecutive:
            lines.append(f"    {name} x{cnt}")

    # ═══════════════════════════════════════════════════
    # 4. 工具输出压缩
    # ═══════════════════════════════════════════════════
    if data["compressions"]:
        lines.append("")
        lines.append("=" * 60)
        lines.append("4. 工具输出压缩")
        lines.append("=" * 60)
        for c in data["compressions"]:
            lines.append(f"  {c['tool']:25s}  {c['before']:>6} → {c['after']:>5} 字符  ({c['ratio']:.1f}x)")

    # 检测未压缩的大输出（>3000 字符且没有对应的压缩记录）
    big_outputs = [r for r in data["tool_results"] if r["output_len"] > 3000]
    compressed_tools = {(c["tool"], c["before"]) for c in data["compressions"]}
    uncompressed_big = []
    for r in big_outputs:
        # 简单检查：如果 output_len > 3000 且没有对应的压缩记录
        has_compress = any(c["before"] == r["output_len"] for c in data["compressions"])
        if not has_compress and r["output_len"] > 5000:
            uncompressed_big.append(r)
    if uncompressed_big:
        lines.append("  ⚠️ 未压缩的大输出:")
        for r in uncompressed_big:
            lines.append(f"    output_len={r['output_len']} @ {r['time']}")

    # ═══════════════════════════════════════════════════
    # 5. 上下文压缩分析
    # ═══════════════════════════════════════════════════
    if data["context_compacts"]:
        lines.append("")
        lines.append("=" * 60)
        lines.append("5. 上下文压缩 (compact)")
        lines.append("=" * 60)

        for i, c in enumerate(data["context_compacts"]):
            # 计算 pending = rounds - recent_rounds - 已总结轮数
            # 已总结轮数 = 前一条 compact 的 rounds（如果 summaries 增加了）
            pending_approx = c["rounds"] - c["recent_rounds"]
            summary_flag = ""
            if c["summaries"] > 0:
                summary_flag = f" ✓summary#{c['summaries']}"

            lines.append(
                f"  #{i+1:2d}  msgs {c['before']:>3}→{c['after']:>3}  "
                f"rounds={c['rounds']}  pending≈{pending_approx}  "
                f"summaries={c['summaries']}({c['summary_chars']}chars){summary_flag}"
            )

        # 区分：窗口滑动 vs LLM summary
        llm_summary_count = len(data["context_summaries"])
        compact_count = len(data["context_compacts"])
        summary_in_compact = data["context_compacts"][-1]["summaries"] if data["context_compacts"] else 0

        lines.append(f"  ---")
        lines.append(f"  compact 次数:     {compact_count}")
        lines.append(f"  窗口滑动:         {compact_count} 次（每次 compact 都会滑动窗口）")
        lines.append(f"  LLM summary 生成: {summary_in_compact} 个（compact 日志中的 summaries 字段）")
        lines.append(f"  LLM summary 调用: {llm_summary_count} 次（react-context-summary 标签）")

        if llm_summary_count > 0 and summary_in_compact == 0:
            lines.append(f"  ⚠️ LLM summary 被调用但 summaries=0：summary 生成可能失败")

        # 消息数趋势
        afters = [c["after"] for c in data["context_compacts"]]
        if len(afters) >= 3:
            # 检查 summary 触发后消息是否骤降
            drops = []
            for i in range(1, len(afters)):
                if afters[i] < afters[i-1] - 3:
                    drops.append((i, afters[i-1], afters[i]))
            if drops:
                lines.append(f"  summary 触发后消息骤降: ", )
                for idx, before, after in drops:
                    lines.append(f"    #{idx+1}: {before}→{after}")

            # 检查压缩后消息是否持续膨胀
            # 取每个 summary 之后的第一个 after 值
            summary_afters = []
            prev_summaries = 0
            for c in data["context_compacts"]:
                if c["summaries"] > prev_summaries:
                    summary_afters.append(c["after"])
                    prev_summaries = c["summaries"]
            if len(summary_afters) >= 2:
                growth = summary_afters[-1] - summary_afters[0]
                if growth > 3:
                    lines.append(f"  ⚠️ summary 后消息数逐轮增长: {' → '.join(map(str, summary_afters))}")

    # ═══════════════════════════════════════════════════
    # 6. 循环检测
    # ═══════════════════════════════════════════════════
    lines.append("")
    lines.append("=" * 60)
    lines.append("6. 循环检测")
    lines.append("=" * 60)
    if data["loop_detections"]:
        for det in data["loop_detections"]:
            lines.append(f"  ⚠️ 触发 @ {det['time']}")
            # 判断循环检测是否导致跳过报告
            if "report-fast" not in {c["label"] for c in data["report_flow"]}:
                lines.append(f"  ⚠️ 循环检测导致 report-fast 被跳过，报告由强制总结生成")
    else:
        lines.append("  ✅ 未触发")

    # ═══════════════════════════════════════════════════
    # 7. Token 趋势
    # ═══════════════════════════════════════════════════
    if llm:
        lines.append("")
        lines.append("=" * 60)
        lines.append("7. Token 趋势")
        lines.append("=" * 60)
        for c in llm:
            bar_len = min(c["total_tokens"] // 500, 40)
            bar = "█" * bar_len
            cache_tag = f" cache={c['cache_hit_pct']:.0f}%" if c["cache_hit_pct"] else ""
            lines.append(f"  {c['label']:25s} {c['total_tokens']:>7,} {bar}{cache_tag}")

        # 检测 agent token 膨胀
        agent_labels = {"react-sub", "skill-selector", "unified"}
        agent_calls = [c for c in llm if c["label"] in agent_labels]
        if len(agent_calls) >= 3:
            first_tok = agent_calls[0]["total_tokens"]
            last_tok = agent_calls[-1]["total_tokens"]
            if last_tok > first_tok * 2:
                lines.append(f"  ⚠️ Agent token 膨胀: {first_tok:,} → {last_tok:,} ({last_tok/first_tok:.1f}x)")

    # ═══════════════════════════════════════════════════
    # 8. 报告质量
    # ═══════════════════════════════════════════════════
    lines.append("")
    lines.append("=" * 60)
    lines.append("8. 报告生成")
    lines.append("=" * 60)
    for label in ["report-fast", "report-expand", "sentiment-extract", "dashboard"]:
        calls = [c for c in llm if c["label"] == label]
        if calls:
            for c in calls:
                lines.append(f"  {label:25s}  {c['duration']:6.1f}s  {c['total_tokens']:>7,} tokens")
        else:
            lines.append(f"  {label:25s}  (未调用)")

    # ═══════════════════════════════════════════════════
    # 9. 异常
    # ═══════════════════════════════════════════════════
    issues = []
    if data["warnings"]:
        for w in data["warnings"][:5]:
            issues.append(("WARNING", w[:200]))
    if data["errors"]:
        for e in data["errors"][:5]:
            issues.append(("ERROR", e[:200]))

    if issues:
        lines.append("")
        lines.append("=" * 60)
        lines.append("9. 异常")
        lines.append("=" * 60)
        for tag, msg in issues:
            lines.append(f"  [{tag}] {msg}")

    # ═══════════════════════════════════════════════════
    # 10. 综合评估
    # ═══════════════════════════════════════════════════
    lines.append("")
    lines.append("=" * 60)
    lines.append("10. 综合评估")
    lines.append("=" * 60)

    findings = []

    # 循环检测
    if data["loop_detections"]:
        findings.append(("❌", f"循环检测触发 {len(data['loop_detections'])} 次，可能导致信息不完整"))

    # 报告流程完整性
    report_labels_present = {c["label"] for c in data["report_flow"]}
    if "report-fast" not in report_labels_present and meta.get("mode") == "react":
        findings.append(("⚠️", "report-fast 未调用，报告由强制总结生成"))

    # 重复工具调用
    if tool_counts:
        max_tool = max(tool_counts.items(), key=lambda x: x[1])
        if max_tool[1] > 10:
            findings.append(("⚠️", f"{max_tool[0]} 被调用 {max_tool[1]} 次，可能存在重复查询"))

    # 上下文 summary 失败
    if data["context_summaries"] and data["context_compacts"]:
        last_compact = data["context_compacts"][-1]
        if last_compact["summaries"] == 0 and len(data["context_summaries"]) > 0:
            findings.append(("⚠️", "LLM summary 被调用但 compact 日志显示 summaries=0，summary 可能未保存"))

    # Token 膨胀
    if llm:
        agent_labels = {"react-sub", "skill-selector", "unified"}
        agent_calls = [c for c in llm if c["label"] in agent_labels]
        if len(agent_calls) >= 3 and agent_calls[-1]["total_tokens"] > agent_calls[0]["total_tokens"] * 3:
            findings.append(("⚠️", "Agent token 严重膨胀 (>3x)，上下文可能过大"))

    # 总体判断
    if not findings:
        findings.append(("✅", "执行正常，无明显问题"))

    for icon, desc in findings:
        lines.append(f"  {icon} {desc}")

    lines.append("")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("用法: python test/scripts/analyze_log.py <log_path> [log_path2 ...]")
        sys.exit(1)

    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    for log_path in sys.argv[1:]:
        if not Path(log_path).exists():
            print(f"文件不存在: {log_path}")
            continue

        print(f"\n{'#' * 60}")
        print(f"# 日志: {Path(log_path).name}")
        print(f"{'#' * 60}")

        data = parse_log(log_path)
        report = format_report(data)
        print(report)


if __name__ == "__main__":
    main()
