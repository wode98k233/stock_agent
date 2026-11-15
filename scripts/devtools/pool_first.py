#!/usr/bin/env python
"""池子优先验证 + 组合回测 CLI（MVP 入口）。

示例（策略 JSON 来自生成器/已有结果）：
    python scripts/devtools/pool_first.py --strategy-file output/strategy.json \
        --pool POOL:default --start 2024-01-01 --end 2026-10-20 --max-positions 10

不给 --strategy-file 时，用真实生成器按想法生成一份（约 1~3 分钟）：
    python scripts/devtools/pool_first.py --idea "收盘涨幅5%到涨停之间尾盘买入，次日开盘卖出"
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.pool_first import generate_pool_strategy, resolve_pool, run_pool_first  # noqa: E402


def main():
    p = argparse.ArgumentParser(description='池子优先验证 + 组合回测')
    p.add_argument('--idea', default='', help='自然语言想法（无 strategy-file 时用于生成）')
    p.add_argument('--strategy-file', default='', help='策略 JSON 文件；给了就不调 LLM')
    p.add_argument('--pool', default='POOL:default', help='POOL:default / POOL:600519,510300')
    p.add_argument('--start', default='2024-01-01')
    p.add_argument('--end', default='2026-10-20')
    p.add_argument('--entry-timing', default='same_close', choices=['same_close', 'next_open', 'next_close'])
    p.add_argument('--exit-timing', default='next_open', choices=['same_close', 'next_open', 'next_close'])
    p.add_argument('--max-positions', type=int, default=10)
    p.add_argument('--mode', choices=['validate', 'portfolio', 'both'], default='both',
                   help='validate=逐只独立验证（不共享资金）；portfolio=共享资金组合；both=两者')
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--no-portfolio', action='store_true')
    p.add_argument('--out', default='output/pool_first_result.json')
    args = p.parse_args()

    if args.strategy_file:
        text = Path(args.strategy_file).read_text(encoding='utf-8')
    elif args.idea:
        pilot = resolve_pool(args.pool)[0]
        print(f'[pool-first] 生成策略（pilot={pilot}）...', flush=True)
        res = generate_pool_strategy(args.idea, args.pool,
                                     entry_timing=args.entry_timing,
                                     exit_timing=args.exit_timing)
        if not res.get('ok'):
            print('[pool-first] 生成失败:', res.get('error'))
            return 2
        text = res['strategy_json']
        print('[pool-first] 生成完成:', str(res.get('hypothesis') or '')[:120])
    else:
        p.error('需要 --strategy-file 或 --idea')
        return 2

    out = run_pool_first(text, pool_spec=args.pool, start=args.start, end=args.end,
                         entry_timing=args.entry_timing, exit_timing=args.exit_timing,
                         workers=args.workers, max_positions=args.max_positions,
                         with_portfolio=not args.no_portfolio, mode=args.mode)
    cs = out['cross_section']
    print('池子:', out['pool'])
    print('横截面:', json.dumps(cs['overall'], ensure_ascii=False))
    print('结论:', cs['verdict'].get('label'), '-', cs['verdict'].get('headline'))
    if out.get('portfolio'):
        print('组合:', json.dumps(out['portfolio'], ensure_ascii=False))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print('明细:', args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
