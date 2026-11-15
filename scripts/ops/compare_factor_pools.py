"""跨标的池稳健性对照：同一批因子在两个指数池上的表现是否一致。

回答的问题
----------
「ADX 在沪深300 上有效」——那在中证500 上还成立吗？如果不成立，它大概率是
某个池子的特异性噪声，而不是可用的因子。这是因子研究里最便宜的一次「外部验证」。

输出
----
- 终端：逐因子对照表 + 跨池 RankIC 相关
- ``output/factor_pool_compare_<stamp>.md``：可读报告
- ``output/factor_pool_compare_<stamp>.json``：机读结果

用法
----
    python scripts/ops/compare_factor_pools.py --pools 000300,000905 \
        --start 2024-10-01 --end 2026-10-16 --neutralize both
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 3:
        return None
    ra = pd.Series(a).rank().to_numpy(dtype=float)
    rb = pd.Series(b).rank().to_numpy(dtype=float)
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    return float((ra * rb).sum() / denom) if denom > 0 else None


def run_pool(index_code: str, cfg, start: str, end: str,
             warmup: int) -> tuple[dict, dict]:
    from backtest.factor_data import load_panel_inputs
    from backtest.factor_eval import build_panel, evaluate_factors

    frames, meta = load_panel_inputs(index_code=index_code, start_date=start,
                                     end_date=end, warmup_bars=warmup)
    if not frames:
        raise RuntimeError(f"{index_code} 无数据（先跑 backfill_index_universe.py）")
    panel = build_panel(frames, None, min_history=warmup // 2,
                        meta_map=meta.get("meta_map"))
    payload = evaluate_factors(panel, None, cfg, meta=meta)
    return payload, meta


def compare(a: dict, b: dict, name_a: str, name_b: str) -> dict:
    """对照两个 payload 的因子排名与 IC。"""
    ra = {r["factor_id"]: r for r in a.get("ranking", [])}
    rb = {r["factor_id"]: r for r in b.get("ranking", [])}
    common = sorted(set(ra) & set(rb))

    rows = []
    xs, ys = [], []
    for fid in common:
        x, y = ra[fid], rb[fid]
        if x["rank_ic_mean"] is None or y["rank_ic_mean"] is None:
            continue
        xs.append(x["rank_ic_mean"])
        ys.append(y["rank_ic_mean"])
        rows.append({
            "factor_id": fid,
            "name": x["name"],
            "category": x["category"],
            "rankic_a": x["rank_ic_mean"],
            "rankic_b": y["rank_ic_mean"],
            "delta": y["rank_ic_mean"] - x["rank_ic_mean"],
            "sign_agree": (x["rank_ic_mean"] > 0) == (y["rank_ic_mean"] > 0),
            "dsr_a": x.get("dsr"),
            "dsr_b": y.get("dsr"),
            "icir_a": x.get("icir"),
            "icir_b": y.get("icir"),
        })

    rho = _spearman(np.array(xs), np.array(ys)) if len(xs) >= 3 else None
    agree = sum(1 for r in rows if r["sign_agree"])
    both_pass = [r for r in rows
                 if (r["dsr_a"] or 0) >= 0.95 and (r["dsr_b"] or 0) >= 0.95]

    rows.sort(key=lambda r: abs(r["rankic_a"] or 0), reverse=True)
    return {
        "pools": [name_a, name_b],
        "n_common": len(rows),
        "rankic_spearman": rho,
        "sign_agree_ratio": (agree / len(rows)) if rows else None,
        "both_pass_dsr": [r["factor_id"] for r in both_pass],
        "rows": rows,
        "coverage": {"a": a.get("coverage"), "b": b.get("coverage")},
    }


def to_markdown(cmp: dict, meta_a: dict, meta_b: dict) -> str:
    a, b = cmp["pools"]

    def num(v, digits: int = 3) -> str:
        return "—" if v is None else f"{v:.{digits}f}"

    lines = [
        f"# 因子跨池稳健性对照：{a} vs {b}",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 标的池：{a}（{meta_a.get('loaded')} 只） / {b}（{meta_b.get('loaded')} 只）",
        f"- 区间：{meta_a.get('data_range')}",
        f"- 共同因子：{cmp['n_common']} 个",
        f"- **跨池 RankIC 秩相关：{num(cmp['rankic_spearman'])}**",
        f"- 符号一致率："
        + (f"{cmp['sign_agree_ratio']:.0%}" if cmp["sign_agree_ratio"] is not None else "—"),
        f"- 两池均通过 DSR≥0.95 的因子：{len(cmp['both_pass_dsr'])} 个"
        + (f"（{', '.join(cmp['both_pass_dsr'])}）" if cmp["both_pass_dsr"] else ""),
        "",
        "## 逐因子对照（按 |RankIC(A)| 降序）",
        "",
        f"| 因子 | 类别 | RankIC({a}) | RankIC({b}) | 差值 | 符号一致 | DSR({a}) | DSR({b}) |",
        "|---|---|---:|---:|---:|:---:|---:|---:|",
    ]
    for r in cmp["rows"]:
        lines.append(
            f"| {r['name']} | {r['category']} | {r['rankic_a']:+.4f} | "
            f"{r['rankic_b']:+.4f} | {r['delta']:+.4f} | "
            f"{'是' if r['sign_agree'] else '否'} | "
            f"{num(r['dsr_a'])} | {num(r['dsr_b'])} |")
    lines += [
        "",
        "## 读法",
        "",
        "- **跨池秩相关高（> 0.6）**：因子结构在两个池子一致，可以当作可复用的信号。",
        "- **秩相关低（< 0.3）**：排名受标的池影响很大，单池结论不可外推。",
        "- **符号相反**：因子在一个池子是正向、另一个是反向 —— 通常是市值/行业结构差异，"
        "中性化后可再对照一次。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="因子跨池稳健性对照")
    ap.add_argument("--pools", default="000300,000905")
    ap.add_argument("--start", default="2024-10-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--ret-period", type=int, default=5)
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--lag", type=int, default=1)
    ap.add_argument("--min-names", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=90)
    ap.add_argument("--neutralize", default="both",
                    choices=["none", "industry", "size", "both"])
    args = ap.parse_args()

    from backtest.factor_eval import EvalConfig
    cfg = EvalConfig(ret_period=args.ret_period, quantiles=args.quantiles,
                     lag=args.lag, min_names=args.min_names,
                     neutralize=args.neutralize)

    pools = [p.strip() for p in args.pools.split(",") if p.strip()]
    if len(pools) != 2:
        print("--pools 需要两个指数代码")
        return 1

    payloads, metas = {}, {}
    for p in pools:
        print(f"[1/2] 评估 {p} …")
        payloads[p], metas[p] = run_pool(p, cfg, args.start, args.end, args.warmup)
        cov = payloads[p]["coverage"]
        print(f"      {cov['n_codes']} 票 / {cov['n_dates']} 日 / "
              f"{cov['evaluated']} 因子（失败 {cov['failed']}）")

    cmp = compare(payloads[pools[0]], payloads[pools[1]], pools[0], pools[1])

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    md_path = out_dir / f"factor_pool_compare_{stamp}.md"
    json_path = out_dir / f"factor_pool_compare_{stamp}.json"
    md_path.write_text(to_markdown(cmp, metas[pools[0]], metas[pools[1]]),
                       encoding="utf-8")
    json_path.write_text(json.dumps({"compare": cmp,
                                     "meta": {k: {kk: vv for kk, vv in v.items()
                                                  if kk != "meta_map"}
                                              for k, v in metas.items()}},
                                    ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"跨池 RankIC 秩相关：{cmp['rankic_spearman']:.3f}"
          if cmp["rankic_spearman"] is not None else "跨池秩相关：—")
    print(f"符号一致率：{cmp['sign_agree_ratio']:.0%}"
          if cmp["sign_agree_ratio"] is not None else "符号一致率：—")
    print(f"两池均通过 DSR：{cmp['both_pass_dsr'] or '无'}")
    print()
    print(f"{'因子':<14}{'RankIC_A':>10}{'RankIC_B':>10}{'一致':>6}")
    for r in cmp["rows"][:15]:
        print(f"{r['name']:<14}{r['rankic_a']:>10.4f}{r['rankic_b']:>10.4f}"
              f"{('  是' if r['sign_agree'] else '  否'):>6}")
    print()
    print(f"输出：{md_path.name} / {json_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
