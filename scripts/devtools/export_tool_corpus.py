"""导出工具输出压缩语料库（训练集/测试集）。

从 agent_trace.db 读取历史工具调用（含 LLM 压缩基线），去重、按类配额采样、
按查询分组切分 train/test，落盘到 test/fixtures/tool_corpus/。

用法：
    python scripts/devtools/export_tool_corpus.py --stats-only          # 只看统计，不写文件
    python scripts/devtools/export_tool_corpus.py                       # 默认导出
    python scripts/devtools/export_tool_corpus.py --quota-compressed 200 --split-ratio 0.75
    python scripts/devtools/export_tool_corpus.py --db path/to/agent_trace.db --out some/dir
    python scripts/devtools/export_tool_corpus.py --tools mx_search_news,mx_xuangu_filter
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from eval.tool_corpus import (  # noqa: E402
    DEFAULT_SELECTION,
    build_records,
    classify_record,
    dedup_records,
    export_corpus,
    scrub_record,
    select_records,
    split_records,
)


def _print_stats(title: str, per_tool: dict) -> None:
    print(f"\n=== {title} ===")
    print(f"{'tool':28s} {'n':>5s} {'raw_chars':>10s} {'raw_avg':>8s} {'llm基线':>8s}")
    total = 0
    for tool in sorted(per_tool):
        items = per_tool[tool]
        raw_chars = sum(r["raw_len"] for r in items)
        with_llm = sum(1 for r in items if r.get("llm_compressed"))
        avg = raw_chars // max(1, len(items))
        print(f"{tool:28s} {len(items):5d} {raw_chars:10,d} {avg:8,d} {with_llm:8d}")
        total += len(items)
    print(f"{'合计':28s} {total:5d}")


def _resolve_dbs(db_arg: str) -> list[str]:
    """解析待导出的 trace 库：显式 --db 单库；默认主库 + 全部月库。"""
    if db_arg:
        return [db_arg] if os.path.isfile(db_arg) else []
    from utils.agent_trace.store import store
    paths = [store.main_db_path()] + store.list_month_dbs()
    return [p for p in paths if os.path.isfile(p)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="导出工具输出压缩语料库（配额语义：负数=丢弃，0=全部，正数=上限）")
    parser.add_argument("--db", default="", help="agent_trace.db 路径（默认主库+全部月库）")
    parser.add_argument("--out", default=os.path.join("test", "fixtures", "tool_corpus"), help="输出目录")
    parser.add_argument("--tools", default="", help="只导出指定工具（逗号分隔）")
    parser.add_argument("--quota-compressed", type=int, default=DEFAULT_SELECTION["compressed"],
                        help="每个工具保留的 LLM 基线样本上限（0=全部）")
    parser.add_argument("--quota-large", type=int, default=DEFAULT_SELECTION["large"], help="大输出样本上限")
    parser.add_argument("--quota-small", type=int, default=DEFAULT_SELECTION["small"], help="小输出样本上限")
    parser.add_argument("--quota-error", type=int, default=DEFAULT_SELECTION["error"], help="错误样本上限")
    parser.add_argument("--quota-empty", type=int, default=DEFAULT_SELECTION["empty"], help="空结果样本上限")
    parser.add_argument("--split-ratio", type=float, default=0.8, help="训练集比例")
    parser.add_argument("--split-mode", choices=["hash", "temporal", "none"], default="hash")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-scrub", action="store_true", help="关闭持仓/账户等敏感数据脱敏（默认开启）")
    parser.add_argument("--plain", action="store_true", help="输出未压缩 .jsonl（默认 .jsonl.gz）")
    parser.add_argument("--stats-only", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args(argv)

    dbs = _resolve_dbs(args.db)
    if not dbs:
        print("错误：找不到可导出的 trace 数据库（主库/月库均不存在）")
        return 1

    records = []
    pair_stats = {}
    for db_path in dbs:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            part_records, pair_stats = build_records(conn)
        finally:
            conn.close()
        records.extend(part_records)

    if args.tools:
        tools = {t.strip() for t in args.tools.split(",") if t.strip()}
        records = [r for r in records if r["tool"] in tools]

    records, duplicates = dedup_records(records)
    scrubbed = 0
    if not args.no_scrub:
        for index, record in enumerate(records):
            cleaned = scrub_record(record)
            if cleaned is not record:
                records[index] = cleaned
                scrubbed += 1
    for record in records:
        record["category"] = classify_record(record)

    all_by_tool: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        all_by_tool[record["tool"]].append(record)

    print("=== 配对统计 ===")
    print(pair_stats)
    print(f"去重前={pair_stats['tool_steps']} 去重后={len(records)} 重复丢弃={duplicates} 脱敏={scrubbed}")

    quotas = {
        "compressed": args.quota_compressed,
        "large": args.quota_large,
        "small": args.quota_small,
        "error": args.quota_error,
        "empty": args.quota_empty,
    }
    selected = select_records(records, quotas=quotas, seed=args.seed)

    selected_by_tool: dict[str, list[dict]] = defaultdict(list)
    for record in selected:
        selected_by_tool[record["tool"]].append(record)

    print("\n=== 去重后全量（未采样）按类别 ===")
    cat_total: dict[str, int] = defaultdict(int)
    for items in all_by_tool.values():
        for record in items:
            cat_total[record["category"]] += 1
    print(dict(sorted(cat_total.items())))
    _print_stats("全量", all_by_tool)
    _print_stats("采样后", selected_by_tool)

    split_records(selected, ratio=args.split_ratio, mode=args.split_mode, seed=args.seed)
    split_total: dict[str, int] = defaultdict(int)
    for record in selected:
        split_total[record["split"]] += 1
    print(f"\n切分（mode={args.split_mode}, ratio={args.split_ratio}）: {dict(sorted(split_total.items()))}")

    if args.stats_only:
        print("\n--stats-only：未写文件")
        return 0

    manifest = export_corpus(
        selected,
        args.out,
        extra_manifest={
            "source_db": os.path.abspath(args.db),
            "pairing": pair_stats,
            "dedup": {"before": pair_stats["tool_steps"], "after": len(records), "dropped": duplicates},
            "scrub": {"enabled": not args.no_scrub, "records": scrubbed},
            "selection": quotas,
            "split": {"mode": args.split_mode, "ratio": args.split_ratio, "seed": args.seed},
        },
        gzip_output=not args.plain,
    )
    total_chars = sum(r["raw_len"] for r in selected)
    print(f"\n已导出 {manifest['records']} 条到 {args.out}（原始输出共 {total_chars:,} 字符）")
    for name, count in manifest["files"].items():
        print(f"  {name:40s} {count:4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
