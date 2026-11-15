"""回测覆盖率巡检（设计文档 §12）。

回答一个问题：**哪些回测是在数据不全的情况下得出结论的。**

口径与边界：
- 只读 `backtest.db`（`backtest_runs` × `backtest_results`），不写任何库、不发网络请求；
- 结果作为 `/api/data-monitor/overview` 的独立小节 `backtest_coverage` 返回，
  **不塞进 `assets`**（assets 是文件/库级 AssetStat，形状不同，塞进去会污染 hero/排行/趋势三处渲染）；
- 覆盖率列由 Plan 1 落库；列不存在或全为 NULL 时返回空结果并给出原因，不抛异常。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

LOW_COVERAGE_PCT = 95.0
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 50


def collect_coverage_check(days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """扫近期回测里覆盖率低于阈值的那些（默认 30 天 / 95%）。"""
    from utils.cache.backtest_db import get_backtest_db
    span = max(1, int(days))
    since = (datetime.now() - timedelta(days=span)).strftime('%Y-%m-%d %H:%M:%S')
    out: Dict[str, Any] = {
        'days': span, 'threshold_pct': LOW_COVERAGE_PCT, 'checked': 0, 'low_count': 0,
        'low_runs': [], 'hint': '', 'error': '',
    }
    try:
        with get_backtest_db() as conn:
            out['checked'] = conn.execute('''
                SELECT COUNT(*) FROM backtest_runs r
                JOIN backtest_results res ON r.run_id = res.run_id
                WHERE r.created_at >= ?
            ''', (since,)).fetchone()[0]
            rows: List[sqlite3.Row] = conn.execute('''
                SELECT r.run_id, r.code, r.stock_name, r.strategy_id, r.origin, r.created_at,
                       res.coverage_pct, res.health, res.data_bars, res.expected_bars
                FROM backtest_runs r
                JOIN backtest_results res ON r.run_id = res.run_id
                WHERE r.created_at >= ? AND res.coverage_pct IS NOT NULL
                      AND res.coverage_pct * 100 < ?
                      -- 资金不足/执行出错的行 coverage 恒为 0，那是「没跑起来」，
                      -- 不是「数据不全就下结论」，列出来会误导（巡检只报后者）
                      AND IFNULL(res.health, '') NOT IN ('invalid_params', 'error')
                ORDER BY res.coverage_pct ASC, r.created_at DESC
                LIMIT ?
            ''', (since, LOW_COVERAGE_PCT, max(1, int(limit)))).fetchall()
    except sqlite3.OperationalError as e:
        out['error'] = f'缺列或表不存在：{e}'
        out['hint'] = '覆盖率列尚未落库（需先完成 Plan 1 的体检改造）'
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning('[backtest-coverage] 巡检失败: %s', e, exc_info=True)
        out['error'] = str(e)
        out['hint'] = '巡检失败，详见日志'
        return out

    out['low_runs'] = [{
        'run_id': r['run_id'], 'code': r['code'] or '', 'stock_name': r['stock_name'] or '',
        'strategy_id': r['strategy_id'] or '', 'origin': r['origin'] or 'manual',
        'created_at': r['created_at'] or '',
        # 口径：backtest_results.coverage_pct 存的是 0–1 比值（backtest/health.py），
        # 本巡检对外统一用百分数（阈值 95.0 也是百分数）。
        'coverage_pct': (round(float(r['coverage_pct']) * 100, 1)
                         if r['coverage_pct'] is not None else None),
        'health': r['health'] or '', 'data_bars': r['data_bars'], 'expected_bars': r['expected_bars'],
    } for r in rows]
    out['low_count'] = len(out['low_runs'])
    if not out['checked']:
        out['hint'] = f'近 {span} 天没有回测记录'
    elif not out['low_count']:
        out['hint'] = f'近 {span} 天 {out["checked"]} 次回测，没有覆盖率低于 {LOW_COVERAGE_PCT:g}% 的'
    else:
        out['hint'] = (f'近 {span} 天 {out["checked"]} 次回测中，{out["low_count"]} 次的'
                       f'数据覆盖率低于 {LOW_COVERAGE_PCT:g}%——这些结论要打折看')
    return out
