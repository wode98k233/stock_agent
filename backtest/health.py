"""回测体检（设计文档 §4）

把「这次回测可信吗」变成可查的字段，而不是只留一句 error_message：
- data_gap        数据覆盖不足：回测跑完了，但结论不可用，应当补数后再看
- no_signal       跑完了但零交易：统计无意义（胜率、夏普都是 0 笔算出来的）
- insufficient_bars 窗口内 K 线根数 < 策略指标预热所需：指标根本没成形，跑完也没信号
- bad_strategy    有条件引擎表达不了（畸形叶子被静默丢弃）：指标不可信
- invalid_params  初始资金 < 1 手成本：这是参数问题，不是策略问题
- error           引擎抛了未归类异常

本模块只有纯函数；取基准交易日、读行情等 IO 留在 backtest/engine.py。
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

HEALTH_OK = 'ok'
HEALTH_NO_SIGNAL = 'no_signal'
HEALTH_INSUFFICIENT_BARS = 'insufficient_bars'
HEALTH_DATA_GAP = 'data_gap'
HEALTH_BAD_STRATEGY = 'bad_strategy'
HEALTH_INVALID_PARAMS = 'invalid_params'
HEALTH_ERROR = 'error'

MIN_TRADE_SAMPLE = 5
DEFAULT_MIN_COVERAGE = 0.95


def merge_missing_gaps(missing_dates: Sequence[str],
                       baseline_dates: Sequence[str]) -> List[Dict[str, Any]]:
    """把缺失交易日按基准序列的连续性合并成区间（前端「补这段数据」的粒度单位）"""
    missing = set(missing_dates)
    gaps: List[Dict[str, Any]] = []
    current: List[str] = []
    for day in baseline_dates:
        if day in missing:
            current.append(day)
        elif current:
            gaps.append({'start': current[0], 'end': current[-1], 'missing': len(current)})
            current = []
    if current:
        gaps.append({'start': current[0], 'end': current[-1], 'missing': len(current)})
    return gaps


def calc_coverage(actual_dates: Sequence[str],
                  baseline_dates: Sequence[str]) -> Dict[str, Any]:
    """算数据覆盖率与缺口区间（只统计基准交易日；基准之外多出来的 bar 不计入分子）"""
    baseline = list(baseline_dates)
    if not baseline:
        return {'data_bars': len(actual_dates), 'expected_bars': 0,
                'coverage_pct': 0.0, 'gaps': []}
    actual = set(actual_dates)
    missing = [day for day in baseline if day not in actual]
    covered = len(baseline) - len(missing)
    return {
        'data_bars': covered,
        'expected_bars': len(baseline),
        'coverage_pct': round(covered / len(baseline), 4),
        'gaps': merge_missing_gaps(missing, baseline),
    }


def resolve_health(coverage_pct: float, trade_count: int,
                   unsupported: Optional[Sequence[str]] = None,
                   gap_count: int = 0,
                   min_coverage: float = DEFAULT_MIN_COVERAGE,
                   bars: int = 0, min_bars: int = 0) -> Tuple[str, str]:
    """按判定链给出 (health, health_detail)

    优先级：bad_strategy > insufficient_bars > data_gap > no_signal > ok。
    条件没被执行时任何指标都不可信，所以 bad_strategy 排最前；
    窗口连指标预热都不够时，后面三条判据都无从谈起。
    """
    if unsupported:
        head = str(unsupported[0])[:120]
        return (HEALTH_BAD_STRATEGY,
                f'有 {len(unsupported)} 条条件引擎表达不了、未参与回测：{head}')
    if min_bars and bars and bars < min_bars:
        return (HEALTH_INSUFFICIENT_BARS,
                f'K 线 {bars} 根 < 策略预热所需 {min_bars} 根，指标未成形')
    if coverage_pct < min_coverage:
        detail = f'数据覆盖 {coverage_pct:.0%}'
        if gap_count:
            detail += f'，缺 {gap_count} 段'
        return HEALTH_DATA_GAP, detail
    if trade_count == 0:
        return HEALTH_NO_SIGNAL, '未产生任何买卖信号'
    return HEALTH_OK, ''


def _pct_label(pct) -> str:
    """涨跌幅阈值 → 板块文案（涨停归因标注用）。"""
    if pct is None:
        return ''
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return ''
    # 2026-10 起主板 ST 与主板同为 10%；0.05 仅历史配置/旧回测记录会出现
    for val, label in ((0.10, '主板 10%'), (0.20, '双创 20%'),
                       (0.30, '北交所 30%'), (0.05, 'ST 5%(历史)')):
        if abs(p - val) < 1e-9:
            return label
    return f'{p * 100:.0f}%'


def build_execution_attribution(blocked: dict, signals: dict = None,
                                limit_pct: float = None,
                                slippage_cost: float = 0.0) -> List[str]:
    """把撮合拒单 / 滑点统计翻成可读归因（P0 设计 §4.6）；无任何事件时返回 ``[]``。

    Args:
        blocked: ``AStockBroker.blocked`` 计数（limit_up/limit_down/suspended/expired）
        signals: 下单尝试次数 ``{'buy': n, 'sell': m}``（``strategy.get_order_attempts()``）
        limit_pct: 涨跌幅阈值（用于标注板块口径，如「主板 10% 限制」）
        slippage_cost: 滑点累计金额（``broker.slippage_cost``）
    """
    out: List[str] = []
    blocked = blocked or {}
    sig = signals or {}
    label = _pct_label(limit_pct)

    limit_up = int(blocked.get('limit_up', 0) or 0)
    if limit_up:
        buy_n = int(sig.get('buy') or 0)
        head = f'触发 {buy_n} 次买入信号，' if buy_n else ''
        tail = f'（{label} 限制）' if label else ''
        out.append(f'{head}{limit_up} 次因涨停未成交{tail}')

    limit_down = int(blocked.get('limit_down', 0) or 0)
    if limit_down:
        sell_n = int(sig.get('sell') or 0)
        head = f'触发 {sell_n} 次卖出信号，' if sell_n else ''
        out.append(f'{head}{limit_down} 次因跌停未成交')

    suspended = int(blocked.get('suspended', 0) or 0)
    if suspended:
        out.append(f'{suspended} 笔委托因停牌（成交量为 0）未能成交')

    expired = int(blocked.get('expired', 0) or 0)
    if expired:
        out.append(f'{expired} 笔委托当日未成交已自动作废')

    try:
        cost = float(slippage_cost or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost > 0:
        out.append(f'滑点累计吃掉 ¥{cost:,.2f}')

    return out


def classify_failure(exc: BaseException) -> Tuple[str, str]:
    """失败路径的 health 归类：参数问题 / 数据缺口 / 其他异常"""
    message = str(exc) or exc.__class__.__name__
    if isinstance(exc, ValueError) and '资金不足' in message:
        return HEALTH_INVALID_PARAMS, message
    if '无数据' in message:
        return HEALTH_DATA_GAP, message
    return HEALTH_ERROR, message
