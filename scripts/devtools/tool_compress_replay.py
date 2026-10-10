"""离线回放工具输出压缩器（规则压缩开发/验收工具）。

对语料库逐条运行压缩器，输出体积、跳过 LLM 比例、护栏召回，并可抽样做
任务导向的 LLM 判定（压缩后是否保留回答查询所需的关键信息）。

用法：
    # 仅统计语料与 LLM 基线（无需压缩器）
    python scripts/devtools/tool_compress_replay.py

    # 回放规则压缩器
    python scripts/devtools/tool_compress_replay.py --compressor agents.react.tool_compressor:compress_tool_output

    # 抽样 LLM 判定（默认判定规则输出；没有压缩器时判定 LLM 基线）
    python scripts/devtools/tool_compress_replay.py --judge 20 --judge-split test

    # 输出 markdown 报告
    python scripts/devtools/tool_compress_replay.py --out docs/tmp/replay-report.md
"""
import argparse
import importlib
import importlib.util
import json
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from eval.tool_corpus import (  # noqa: E402
    build_judge_prompt,
    compute_metrics,
    load_corpus,
    parse_judge_result,
    write_comparison_csv,
)

DEFAULT_COMPRESSOR = "agents.react.tool_compressor:compress_tool_output"


def _load_compressor(spec: str | None):
    if not spec or spec.lower() in ("none", "null"):
        return None
    if ":" in spec:
        module_part, func_name = spec.split(":", 1)
    else:
        module_part, func_name = spec.rsplit(".", 1)
    if module_part.endswith(".py") or os.sep in module_part or "/" in module_part:
        path = os.path.abspath(module_part)
        module_spec = importlib.util.spec_from_file_location("tool_compressor_plugin", path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_part)
    return getattr(module, func_name)


def _default_threshold() -> int:
    try:
        from config import Config

        return int(getattr(Config, "TOOL_OUTPUT_COMPRESS_THRESHOLD", 3000))
    except Exception:
        return 3000


def _fmt_ratio(value) -> str:
    return f"{value:.1%}" if isinstance(value, (int, float)) else "-"


def _fmt_recall(value) -> str:
    return f"{value:.1%}" if isinstance(value, (int, float)) else "-"


def _preview(text: str, limit: int = 400) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _render_samples(sample_rows: list[dict], has_rule: bool) -> list[str]:
    lines = ["## 样例对照", ""]
    for row in sample_rows:
        raw_len = row.get("raw_len") or 0
        lines.append(f"### [{row.get('tool')}] {row.get('id')}")
        lines.append(f"- 查询：{row.get('query') or '（未提供）'}")
        ratio = (row.get("rule_len") or 0) / raw_len if raw_len else 0
        skip = row.get("skip_llm")
        parts = [f"raw {raw_len:,}"]
        if row.get("rule_len") is not None:
            parts.append(f"规则 {row['rule_len']:,}（{ratio:.1%}，跳过LLM={skip}）")
        if row.get("llm_len"):
            parts.append(f"LLM基线 {row['llm_len']:,}（{row['llm_len'] / raw_len:.1%}）")
        lines.append("- 长度：" + " ｜ ".join(parts))
        lines.append("")
        lines.append("**原始输出（前 400 字）**")
        lines.append("````text")
        lines.append(_preview(row.get("raw_output") or "", 400))
        lines.append("````")
        lines.append("")
        if row.get("rule_output") is not None:
            lines.append("**规则输出（前 400 字）**")
            lines.append("````text")
            lines.append(_preview(row["rule_output"], 400))
            lines.append("````")
            lines.append("")
        if row.get("llm_compressed"):
            lines.append("**LLM 基线（前 400 字）**")
            lines.append("````text")
            lines.append(_preview(row["llm_compressed"], 400))
            lines.append("````")
            lines.append("")
        if row.get("error"):
            lines.append(f"- ⚠️ 压缩异常：{row['error']}")
            lines.append("")
    return lines


def _render_report(corpus_dir, compressor_spec, threshold, metrics, judge_rows, judge_note, sample_rows=None) -> str:
    lines = [
        "# 工具输出压缩回放报告",
        "",
        f"- 语料目录：`{corpus_dir}`",
        f"- 压缩器：`{compressor_spec or '（无，仅统计基线）'}`",
        f"- 阈值：{threshold}",
        f"- 记录数：{metrics['n']}",
        "",
        "## 指标总表",
        "",
        "| 工具 | n | raw均长 | LLM基线比 | LLM数字召回 | 规则压缩比 | 规则跳过LLM | 规则数字召回 | 规则代码召回 | 规则值保留 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for tool, s in metrics["summary"].items():
        lines.append(
            f"| {tool} | {s['n']} | {s['raw_avg']:,} | {_fmt_ratio(s['llm_ratio'])} | "
            f"{_fmt_recall(s['llm_num_recall'])} | {_fmt_ratio(s['rule_ratio'])} | "
            f"{_fmt_ratio(s['rule_skip_rate'])} | {_fmt_recall(s['rule_num_recall'])} | "
            f"{_fmt_recall(s['rule_code_recall'])} | {_fmt_recall(s.get('rule_value_recall'))} |"
        )
    lines.append("")
    lines.append("> 数字/代码召回为辅助护栏指标，不作为唯一验收口径；任务相关质量以 LLM 判定为准。")
    lines.append("")

    lines.append("## LLM 判定（任务导向）")
    lines.append("")
    if not judge_rows:
        lines.append(judge_note or "未启用。")
        lines.append("")
    else:
        passed = sum(1 for r in judge_rows if r.get("retained"))
        unparsed = sum(1 for r in judge_rows if r.get("retained") is None)
        lines.append(f"- 抽样：{len(judge_rows)} 条，通过 {passed} 条，解析失败 {unparsed} 条")
        lines.append("- 评估来源：" + judge_note)
        lines.append("")
        for row in judge_rows:
            flag = "通过" if row.get("retained") else ("解析失败" if row.get("retained") is None else "不通过")
            lines.append(f"### [{flag}] {row.get('tool')} · {row.get('id')}")
            lines.append(f"- 查询：{row.get('query') or '（未提供）'}")
            lines.append(f"- 遗漏：{', '.join(row.get('missing') or []) or '无'}")
            lines.append(f"- 评语：{row.get('comment') or ''}")
            lines.append("")

    if sample_rows:
        has_rule = any(row.get("rule_output") is not None for row in sample_rows)
        lines.extend(_render_samples(sample_rows, has_rule))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线回放工具输出压缩器")
    parser.add_argument("--corpus-dir", default=os.path.join("test", "fixtures", "tool_corpus"))
    parser.add_argument("--tools", default="", help="只回放指定工具（逗号分隔）")
    parser.add_argument("--split", default="", help="只回放指定切分（train/test/all）")
    parser.add_argument("--compressor", default="auto",
                        help="压缩器路径 module:function；auto=尝试默认规则压缩器；none=只统计基线")
    parser.add_argument("--no-trim", action="store_true",
                        help="不叠加运行时 trim（默认叠加，与 tool_node_base 管线对齐）")
    parser.add_argument("--threshold", type=int, default=0, help="跳过 LLM 的尺寸阈值（0=取 Config 默认）")
    parser.add_argument("--judge", type=int, default=0, help="抽样 LLM 判定的条数")
    parser.add_argument("--judge-split", default="", help="判定使用的切分（默认与 --split 相同）")
    parser.add_argument("--show", type=int, default=0, help="抽样展示 N 条逐条对照（原始/规则/LLM 基线）")
    parser.add_argument("--show-all", action="store_true",
                        help="展示全部「规则有压缩效果」的记录（离线逐条核对，不抽样）")
    parser.add_argument("--show-id", default="", help="展示指定记录 id（逗号分隔），优先级高于 --show")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--judge-out", default="", help="判定明细 JSONL 输出路径")
    parser.add_argument("--out", default="", help="输出文件路径（markdown 或 csv）")
    parser.add_argument("--format", choices=["markdown", "csv"], default="markdown",
                        help="输出格式；csv=导出「原始/规则/LLM」全量内容，供第三方评估（需 --out）")
    args = parser.parse_args(argv)

    tools = {t.strip() for t in args.tools.split(",") if t.strip()} or None
    splits = (args.split.strip(),) if args.split.strip() else ("train", "test", "all")
    records = load_corpus(args.corpus_dir, tools=tools, splits=splits)
    if not records:
        print(f"错误：语料为空，请先运行 scripts/devtools/export_tool_corpus.py（目录 {args.corpus_dir}）")
        return 1

    threshold = args.threshold or _default_threshold()

    compressor_spec = args.compressor
    compressor = None
    if compressor_spec and compressor_spec.lower() != "none":
        if compressor_spec == "auto":
            compressor_spec = DEFAULT_COMPRESSOR
            try:
                compressor = _load_compressor(compressor_spec)
            except Exception:
                compressor_spec = None
                print("提示：未找到默认规则压缩器，退化为仅统计 LLM 基线")
        else:
            try:
                compressor = _load_compressor(compressor_spec)
            except Exception as exc:
                print(f"错误：加载压缩器失败 {compressor_spec}: {exc}")
                return 1

    if compressor is not None and not args.no_trim:
        try:
            from agents.react.utils import _trim_tool_output

            raw_rule = compressor

            def compressor(tool_name, tool_args, content):  # noqa: F811
                return _trim_tool_output(tool_name, raw_rule(tool_name, tool_args, content))
        except Exception as exc:
            print(f"提示：未加载运行时 trim，跳过叠加（{exc}）")

    metrics = compute_metrics(records, compressor=compressor, threshold=threshold)
    metrics["n"] = len(records)
    sample_rows: list[dict] = []
    if args.show_all:
        sample_rows = [
            row for row in metrics["rows"]
            if row.get("rule_output") is not None and row["rule_output"] != row.get("raw_output")
        ]
        sample_rows.sort(key=lambda row: (row.get("tool") or "", row.get("id") or ""))
    elif args.show_id:
        wanted = {item.strip() for item in args.show_id.split(",") if item.strip()}
        sample_rows = [row for row in metrics["rows"] if row.get("id") in wanted]
    elif args.show > 0:
        sample_rows = random.Random(args.seed).sample(metrics["rows"], min(args.show, len(metrics["rows"])))

    judge_rows: list[dict] = []
    judge_note = ""
    if args.judge > 0:
        judge_split = args.judge_split.strip() or args.split.strip()
        rule_ids = {row["id"] for row in metrics["rows"] if row.get("rule_output")}
        judge_records = [
            r for r in records
            if (not judge_split or r.get("split") == judge_split)
            and (r.get("llm_compressed") or r.get("id") in rule_ids)
        ]
        rnd = random.Random(args.seed)
        sampled = rnd.sample(judge_records, min(args.judge, len(judge_records)))
        try:
            from utils.llm_factory import get_judge_llm

            judge_llm = get_judge_llm()
        except Exception as exc:
            print(f"错误：无法初始化 judge LLM：{exc}")
            return 1

        rule_outputs = {row["id"]: row.get("rule_output") for row in metrics["rows"]}
        source = "规则压缩输出" if compressor is not None else "LLM 压缩基线"
        judge_note = source
        for record in sampled:
            compressed = rule_outputs.get(record.get("id")) or record.get("llm_compressed") or ""
            prompt = build_judge_prompt(record, compressed)
            try:
                response = judge_llm.invoke(prompt)
                content = getattr(response, "content", str(response))
                result = parse_judge_result(content)
            except Exception as exc:
                result = {"retained": None, "missing": [], "comment": f"调用失败: {exc}"}
            judge_rows.append(
                {
                    "id": record.get("id"),
                    "tool": record.get("tool"),
                    "query": record.get("query"),
                    "retained": result.get("retained"),
                    "missing": result.get("missing"),
                    "comment": result.get("comment"),
                }
            )
        if args.judge_out:
            with open(args.judge_out, "w", encoding="utf-8") as handle:
                for row in judge_rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.format == "csv":
        if not args.out:
            print("错误：--format csv 需要 --out 指定文件")
            return 1
        rows = metrics["rows"]
        changed = sum(
            1 for row in rows
            if row.get("rule_output") is not None and row["rule_output"] != row.get("raw_output")
        )
        write_comparison_csv(rows, args.out)
        print(f"已写入 {args.out}（{len(rows)} 条，规则有变化 {changed} 条）")
        return 0

    report = _render_report(args.corpus_dir, compressor_spec, threshold, metrics, judge_rows, judge_note, sample_rows)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"报告已写入 {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
