"""工具压缩影子日志报告：把「原始 / LLM 压缩后 / 规则压缩后」渲染成逐条对照表。

数据来源：`logs/tool_compress_shadow_detail-YYYYMMDD.jsonl`
（由 agents/react/tool_compressor.py::log_shadow_event 写入，内容各截 4000 字）。

用法见 scripts/devtools/tool_compress_shadow_report.py。
"""
import csv
import glob
import json
import os

DETAIL_PREFIX = "tool_compress_shadow_detail"


def load_details(log_dir: str, tool: str | None = None, date: str | None = None) -> list[dict]:
    """读取明细 JSONL；tool 按工具过滤，date 形如 20260910（匹配文件名）。"""
    records = []
    pattern = os.path.join(log_dir, f"{DETAIL_PREFIX}-*.jsonl")
    for path in sorted(glob.glob(pattern)):
        if date and date not in os.path.basename(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if tool and record.get("tool") != tool:
                    continue
                records.append(record)
    records.sort(key=lambda item: item.get("ts") or "")
    return records


def load_stats(log_dir: str, tool: str | None = None, date: str | None = None) -> list[dict]:
    """读取统计 JSONL（旧格式/无明细时的回退数据源）。"""
    path = os.path.join(log_dir, "tool_compress_shadow.jsonl")
    records = []
    if not os.path.isfile(path):
        return records
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if tool and record.get("tool") != tool:
                continue
            if date and not str(record.get("ts") or "").startswith(f"{date[:4]}-{date[4:6]}-{date[6:]}"):
                continue
            records.append(record)
    records.sort(key=lambda item: item.get("ts") or "")
    return records


def _one_line(text: str, limit: int = 80) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"


def _fmt_len(value) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else "-"


def render_markdown(records: list[dict], limit: int = 100, detail_chars: int = 2000) -> str:
    """渲染 markdown：对照总表 + 逐条明细。"""
    lines = [
        "# 工具压缩影子对照报告",
        "",
        f"- 记录数：{len(records)}",
        "- 说明：影子模式下 agent 实际使用「LLM 压缩后」；「规则压缩后」仅为对比候选。",
        "",
    ]
    if not records:
        lines.append("暂无数据。请确认 TOOL_RULE_COMPRESS_POLICY 对目标工具设为 shadow/on，并已产生工具调用。")
        return "\n".join(lines)

    shown = records[:limit] if limit > 0 else records

    lines.extend([
        "## 对照总表",
        "",
        "| # | 工具 | 查询 | 原长 | LLM后长度 | 规则长度 | 规则可跳过LLM | 原始摘要 | LLM摘要 | 规则摘要 |",
        "|---:|---|---|---:|---:|---:|---|---|---|---|",
    ])
    for index, record in enumerate(shown, 1):
        lines.append(
            f"| {index} | {record.get('tool', '')} | {_one_line(record.get('query'), 40)} | "
            f"{_fmt_len(record.get('raw_len'))} | {_fmt_len(record.get('final_len'))} | "
            f"{_fmt_len(record.get('rule_len'))} | "
            f"{'是' if record.get('rule_would_skip_llm') else '否'} | "
            f"{_one_line(record.get('raw'), 60)} | {_one_line(record.get('final'), 60)} | "
            f"{_one_line(record.get('rule'), 60)} |"
        )
    if len(records) > len(shown):
        lines.append(f"| … | 其余 {len(records) - len(shown)} 条见明细或调大 --limit | | | | | | | | |")

    lines.extend(["", "## 逐条明细", ""])
    for index, record in enumerate(shown, 1):
        lines.append(f"### {index}. [{record.get('tool', '?')}] {record.get('ts', '')}")
        lines.append(f"- 查询：{record.get('query') or '（未提供）'}")
        lines.append(
            f"- 长度：原始 {_fmt_len(record.get('raw_len'))} ｜ LLM后 {_fmt_len(record.get('final_len'))}"
            f"（{'已调用' if record.get('llm_used') else '未调用'}）｜ 规则 {_fmt_len(record.get('rule_len'))}"
            f"（可跳过LLM={'是' if record.get('rule_would_skip_llm') else '否'}）"
        )
        if record.get("content_truncated"):
            lines.append("- 注：以下内容按 4000 字截断存储")
        for label, key in (("原始信息", "raw"), ("LLM 压缩后", "final"), ("规则压缩后", "rule")):
            lines.append("")
            lines.append(f"**{label}（前 {detail_chars} 字）**")
            lines.append("````text")
            lines.append((record.get(key) or "")[:detail_chars])
            lines.append("````")
        lines.append("")
    return "\n".join(lines)


def render_stats_only_markdown(records: list[dict]) -> str:
    """无明细内容时的回退视图：只展示长度与是否可跳过。"""
    lines = [
        "# 工具压缩影子统计（无明细内容）",
        "",
        f"- 记录数：{len(records)}",
        "- 说明：以下记录没有采集内容明细（升级前旧数据，或进程未重启仍运行旧代码）。",
        "- 重启 agent 后重新触发工具调用，即可生成「原始 / LLM / 规则」内容对照。",
        "",
        "| # | 时间 | 工具 | 查询 | 原长 | LLM后长度 | 规则长度 | 规则可跳过LLM |",
        "|---:|---|---|---|---:|---:|---:|---|",
    ]
    for index, record in enumerate(records, 1):
        lines.append(
            f"| {index} | {record.get('ts', '')} | {record.get('tool', '')} | "
            f"{_one_line(record.get('query'), 40)} | {_fmt_len(record.get('raw_len'))} | "
            f"{_fmt_len(record.get('final_len'))} | {_fmt_len(record.get('rule_len'))} | "
            f"{'是' if record.get('rule_would_skip_llm') else '否'} |"
        )
    return "\n".join(lines)


def write_csv(records: list[dict], path: str) -> None:
    """导出 CSV（utf-8-sig，Excel 可直接打开）。"""
    columns = [
        ("ts", "时间"),
        ("tool", "工具"),
        ("mode", "模式"),
        ("query", "查询"),
        ("raw_len", "原始长度"),
        ("final_len", "LLM后长度"),
        ("rule_len", "规则长度"),
        ("llm_used", "LLM已调用"),
        ("rule_would_skip_llm", "规则可跳过LLM"),
        ("raw", "原始信息"),
        ("final", "LLM压缩后"),
        ("rule", "规则压缩后"),
    ]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in columns])
        for record in records:
            writer.writerow([record.get(key, "") for key, _ in columns])
