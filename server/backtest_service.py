"""策略一生聚合服务（设计文档 §9）。

硬约束：
- 只调用 utils/cache/backtest_db.py 与 server/signal_eval_service.py 的既有函数，
  不在这里新写查询口径（沿用 server/overview_service.py:4 的同款约定）；
- 全部只读：不写库、不发网络请求、不触发补数。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger('radar.backtest_service')

RUN_LIMIT = 50
SIGNAL_LIMIT = 200
# 「这条策略被验证过」的口径：体检 ok 且样本够（health 由 Plan 1 落列）
VERIFIED_HEALTH = ('ok',)
MIN_VERIFIED_TRADES = 5


def _win_rate_fraction(value: Any, win_count: Any = None) -> Optional[float]:
    """胜率归一到 0~1 小数；>1 时区分「历史百分数刻度」与「旧算式假象」。

    ⚠️ 2026-11-07 复核：全库 `win_rate>1` 的行**全部恰好等于 `win_count`**（旧公式
    `won/effective(=1)` 的产物），盲除 100 会把 23.0 显示成 23%（真实 50.56%）。
    不等于 win_count 的 >1 值仍按百分数刻度折算，保留原有保护。
    """
    if value is None or value == '':
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 1:
        return n
    if win_count is not None:
        try:
            if abs(n - float(win_count)) < 1e-9:
                return None
        except (TypeError, ValueError):
            pass
    return n / 100.0


def _eval_block(run_ids: List[str]) -> Dict[str, Any]:
    """评判块：逐 run 读 backtest_eval（Plan 3 任务 2 的表）。"""
    from utils.cache.backtest_db import get_backtest_eval
    rows: List[Dict[str, Any]] = []
    for rid in run_ids:
        try:
            ev = get_backtest_eval(rid)
        except Exception:
            logger.warning('[lifecycle] 读取评判失败 run_id=%s', rid, exc_info=True)
            continue
        if not ev:
            continue
        rows.append({
            'run_id': rid,
            'verdict': ev.get('verdict') or '',
            'is_sharpe': ev.get('is_sharpe'),
            'oos_sharpe': ev.get('oos_sharpe'),
            'sharpe_ratio_io': ev.get('sharpe_ratio_io'),
            'overfit_flag': bool(ev.get('overfit_flag')),
            'split_date': ev.get('split_date') or '',
            'error_message': ev.get('error_message') or '',
        })
    counts: Dict[str, int] = {}
    for r in rows:
        key = r['verdict'] or 'unknown'
        counts[key] = counts.get(key, 0) + 1
    return {'evaluated': len(rows), 'counts': counts,
            'latest_verdict': rows[0]['verdict'] if rows else '', 'rows': rows}


def _monitor_block(codes: List[str]) -> Dict[str, Any]:
    """落地块。口径见计划偏差 1：source='strategy_research' 且 stock_code ∈ 该策略回测过的 code。"""
    try:
        from server.monitoring.store import MonitorStore
        rules = MonitorStore().list_rules(enabled_only=False)
    except Exception:
        logger.warning('[lifecycle] 盯盘规则读取失败', exc_info=True)
        return {'rules': [], 'matched': 0}
    codeset = {c for c in codes if c}
    matched = []
    for r in rules:
        if (r.get('source') or '') != 'strategy_research':
            continue
        if (r.get('stock_code') or '') not in codeset:
            continue
        matched.append({
            'id': r.get('id'), 'stock_code': r.get('stock_code'),
            'stock_name': r.get('stock_name') or '', 'rule_type': r.get('rule_type') or '',
            'threshold': r.get('threshold'), 'enabled': bool(r.get('enabled')),
            'remark': r.get('remark') or '', 'created_at': r.get('created_at') or '',
        })
    return {'rules': matched, 'matched': len(matched)}


def build_strategy_lifecycle(strategy_id: str, *, run_limit: int = RUN_LIMIT) -> Optional[Dict[str, Any]]:
    """一个策略的一生：回测记录 / 评判 / 前瞻跟踪 / 落地。策略不存在返回 None。"""
    from utils.cache.backtest_db import (get_strategy, get_backtest_runs,
                                         get_result_meta_map, get_strategy_iters_by_hash)
    strategy = get_strategy(strategy_id)
    if not strategy:
        return None

    rows, total = get_backtest_runs(strategy_id=strategy_id, limit=run_limit, offset=0)
    run_ids = [r['run_id'] for r in rows]
    meta = get_result_meta_map(run_ids)
    runs: List[Dict[str, Any]] = []
    for r in rows:
        m = meta.get(r['run_id']) or {}
        runs.append({
            'run_id': r.get('run_id'), 'code': r.get('code') or '',
            'stock_name': r.get('stock_name') or '', 'status': r.get('status') or '',
            'origin': r.get('origin') or 'manual', 'ref_id': r.get('ref_id') or '',
            'start_date': r.get('start_date') or '', 'end_date': r.get('end_date') or '',
            'created_at': r.get('created_at') or '',
            'total_return': r.get('total_return'), 'annual_return': r.get('annual_return'),
            'sharpe_ratio': r.get('sharpe_ratio'), 'max_drawdown': r.get('max_drawdown'),
            'win_rate': _win_rate_fraction(r.get('win_rate'), r.get('win_count')), 'trade_count': r.get('trade_count'),
            'health': m.get('health') or '', 'health_detail': m.get('health_detail') or '',
            'coverage_pct': m.get('coverage_pct'), 'trade_sample_ok': m.get('trade_sample_ok'),
            'error_message': r.get('error_message') or '',
        })

    verified = any((x['health'] in VERIFIED_HEALTH)
                   and (x['trade_sample_ok'] in (1, True))
                   and ((x['trade_count'] or 0) >= MIN_VERIFIED_TRADES)
                   for x in runs)

    codes = sorted({x['code'] for x in runs if x['code']})
    strategy_hash = strategy.get('strategy_hash') or ''
    iterations: List[Dict[str, Any]] = []
    if strategy_hash:
        try:
            iterations = get_strategy_iters_by_hash(strategy_hash)
        except Exception:
            logger.warning('[lifecycle] 迭代史反查失败 hash=%s', strategy_hash, exc_info=True)

    try:
        from server.signal_eval_service import build_by_strategy
        forward = build_by_strategy(strategy_id)
    except Exception:
        logger.warning('[lifecycle] 前瞻统计失败 strategy_id=%s', strategy_id, exc_info=True)
        forward = {'signal_count': 0, 'mature_count': 0, 'pending_count': 0,
                   'win_rate': None, 'wilson_ci': None, 'days_tracked': 0,
                   'signals': [], 'error': '前瞻统计不可用'}

    return {
        'strategy': {'strategy_id': strategy_id, 'name': strategy.get('name') or '',
                     'description': strategy.get('description') or '',
                     'category': strategy.get('category') or '',
                     'is_builtin': bool(strategy.get('is_builtin')),
                     'strategy_hash': strategy_hash},
        'runs': runs, 'runs_total': total, 'verified': verified,
        'evals': _eval_block(run_ids),
        'iterations': iterations,
        'forward': forward,
        'monitor': _monitor_block(codes),
    }


def build_backtest_readiness(days: int = 30) -> Dict[str, Any]:
    """数据同步页「回测数据就绪度」小节（设计文档 §12）。"""
    from utils.cache.backtest_db import get_backtest_readiness
    return get_backtest_readiness(days)
