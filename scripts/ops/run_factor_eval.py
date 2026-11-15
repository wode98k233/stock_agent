"""批量因子评估：数据 → 面板 → 评估 → 报告（CLI）。

用法
----
    # 全量（36 因子 × 沪深300 × 2 年）
    python scripts/ops/run_factor_eval.py --start 2024-10-01 --end 2026-10-16

    # 单因子快速验证
    python scripts/ops/run_factor_eval.py --factors rsi_14,macd_dif --top 2

    # 只出 JSON（供 API/面板消费）
    python scripts/ops/run_factor_eval.py --no-html

产物
----
    output/factor_report_<index>_<date>.html   单文件报告（内联 ECharts）
    output/factor_eval_<index>_<date>.json     完整评估结果（API 同构）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="批量因子评估")
    ap.add_argument("--index", default="000300")
    ap.add_argument("--start", default="2024-10-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--ret-period", type=int, default=5, help="前向收益周期（交易日）")
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--lag", type=int, default=1, help="因子滞后（1=次日建仓）")
    ap.add_argument("--min-names", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=90, help="预热交易日数")
    ap.add_argument("--factors", default="", help="逗号分隔；空=全部 36 个")
    ap.add_argument("--neutralize", default="none",
                    choices=["none", "industry", "size", "both"],
                    help="中性化模式（默认 none）")
    ap.add_argument("--top", type=int, default=8, help="报告展开详情的因子数")
    ap.add_argument("--out", default="", help="HTML 输出路径")
    ap.add_argument("--json-out", default="", help="JSON 输出路径")
    ap.add_argument("--no-html", action="store_true")
    args = ap.parse_args()

    from backtest.factor_data import load_panel_inputs
    from backtest.factor_eval import EvalConfig, build_panel, evaluate_factors

    t0 = time.time()
    frames, meta = load_panel_inputs(
        index_code=args.index, start_date=args.start, end_date=args.end,
        warmup_bars=args.warmup)
    print(f"[1/4] 数据：{meta['loaded']}/{meta['universe_size']} 只，"
          f"缺 {len(meta['missing'])} 只（{time.time()-t0:.1f}s）")
    if not frames:
        print("无可用数据（先跑 scripts/ops/backfill_index_universe.py）")
        return 1

    ids = [x.strip() for x in args.factors.split(",") if x.strip()] or None
    t1 = time.time()
    panel = build_panel(frames, ids, min_history=args.warmup // 2,
                        meta_map=meta.get("meta_map"))
    print(f"[2/4] 面板：{len(panel):,} 行 / {panel['code'].nunique()} 票 / "
          f"{panel['trade_date'].nunique()} 日（{time.time()-t1:.1f}s）")

    cfg = EvalConfig(ret_period=args.ret_period, quantiles=args.quantiles,
                     lag=args.lag, min_names=args.min_names,
                     neutralize=args.neutralize)
    t2 = time.time()
    payload = evaluate_factors(panel, ids, cfg, meta=meta)
    print(f"[3/4] 评估：{payload['coverage']['evaluated']} 个因子"
          f"（失败 {payload['coverage']['failed']}，{time.time()-t2:.1f}s）")

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    json_path = Path(args.json_out) if args.json_out else \
        out_dir / f"factor_eval_{args.index}_{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    html_path = None
    if not args.no_html:
        from backtest.factor_report import build_report_html
        html_path = Path(args.out) if args.out else \
            out_dir / f"factor_report_{args.index}_{stamp}.html"
        html_path.write_text(build_report_html(payload, meta, detail_top=args.top),
                             encoding="utf-8")

    # 终端摘要（按 IC 绝对值）
    print(f"[4/4] 输出：{json_path.name}"
          + (f" / {html_path.name}" if html_path else ""))
    print()

    def _num(v, d: int = 3) -> str:
        return "—" if v is None else f"{v:.{d}f}"

    def _pct(v) -> str:
        return "—" if v is None else f"{v:.0%}"

    header = (f"{'因子':<14}{'RankIC':>9}{'IC':>9}{'ICIR':>8}"
              f"{'t':>7}{'胜率':>7}{'多空夏普':>10}{'DSR':>7}")
    print(header)
    print("-" * 72)
    for r in payload["ranking"][:15]:
        print(f"{r['name']:<14}{_num(r['rank_ic_mean'], 4):>9}"
              f"{_num(r['ic_mean'], 4):>9}{_num(r['icir'], 2):>8}"
              f"{_num(r['t_value'], 1):>7}{_pct(r['ic_win_rate']):>7}"
              f"{_num(r['long_short_sharpe'], 2):>10}{_num(r['dsr']):>7}")
    print()
    print(f"总耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
