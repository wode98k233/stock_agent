"""生成工具压缩影子对照报告（原始 / LLM 压缩后 / 规则压缩后）。

数据来自 `logs/tool_compress_shadow_detail-*.jsonl`（影子/on 模式运行时自动写入）。

用法：
    python scripts/devtools/tool_compress_shadow_report.py
    python scripts/devtools/tool_compress_shadow_report.py --tool mx_search_news --out shadow.md
    python scripts/devtools/tool_compress_shadow_report.py --date 20260910 --out shadow.md
    python scripts/devtools/tool_compress_shadow_report.py --format csv --out shadow.csv
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from eval.shadow_report import (  # noqa: E402
    load_details,
    load_stats,
    render_markdown,
    render_stats_only_markdown,
    write_csv,
)
from utils.app_paths import get_logs_dir  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="工具压缩影子对照报告")
    parser.add_argument("--log-dir", default=get_logs_dir(), help="日志目录（默认项目 logs/）")
    parser.add_argument("--tool", default="", help="只查看指定工具")
    parser.add_argument("--date", default="", help="按日期过滤（YYYYMMDD）")
    parser.add_argument("--limit", type=int, default=100, help="明细最多展示条数（0=全部）")
    parser.add_argument("--format", choices=["markdown", "csv"], default="markdown")
    parser.add_argument("--out", default="", help="输出文件（markdown/csv）；留空则打印 markdown")
    args = parser.parse_args(argv)

    records = load_details(args.log_dir, tool=args.tool or None, date=args.date or None)
    used_stats = False
    if not records:
        stats = load_stats(args.log_dir, tool=args.tool or None, date=args.date or None)
        if stats:
            records = stats
            used_stats = True
    if args.format == "csv":
        if not args.out:
            print("错误：--format csv 需要 --out 指定文件")
            return 1
        write_csv(records, args.out)
        suffix = "（统计，无内容明细）" if used_stats else ""
        print(f"已写入 {args.out}（{len(records)} 条）{suffix}")
        return 0

    report = render_stats_only_markdown(records) if used_stats else render_markdown(records, limit=args.limit)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        suffix = "（统计，无内容明细）" if used_stats else ""
        print(f"已写入 {args.out}（{len(records)} 条）{suffix}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
