"""
选股雷达 - 风险指标计算模块
纯本地计算，不依赖外部 API
基于历史价格数据计算 Beta、波动率、最大回撤、夏普比率等
"""
import numpy as np
import pandas as pd

RISK_FREE_RATE = 0.025  # 无风险利率 2.5%
TRADING_DAYS = 252       # 年交易日


def calc_risk_metrics(df: pd.DataFrame, benchmark_df: pd.DataFrame = None, risk_free_rate: float = RISK_FREE_RATE) -> dict:
    """
    基于历史价格 DataFrame 计算风险指标

    :param df: 含 close 列的 DataFrame（按日期升序），来自 get_stock_history
    :param benchmark_df: 基准（如沪深300）的 DataFrame，含 close 列。为 None 时跳过 Beta 计算
    :param risk_free_rate: 无风险利率，默认 2.5%
    :return: 风险指标字典
    """
    if df.empty or len(df) < 10:
        return {'error': '数据不足，至少需要10个交易日'}

    close = df['close'].values.astype(float)
    close = close[~np.isnan(close)]
    if len(close) < 10:
        return {'error': '数据不足，至少需要10个交易日'}

    with np.errstate(divide='ignore', invalid='ignore'):
        daily_returns = np.diff(close) / close[:-1]
        daily_returns = np.nan_to_num(daily_returns, nan=0.0, posinf=0.0, neginf=0.0)

    # 年化收益率
    total_return = close[-1] / close[0] - 1 if close[0] != 0 else 0.0
    days = len(daily_returns)
    annualized_return = (1 + total_return) ** (TRADING_DAYS / days) - 1

    # 年化波动率
    volatility = np.std(daily_returns, ddof=1) * np.sqrt(TRADING_DAYS)

    # 最大回撤
    cummax = np.maximum.accumulate(close)
    with np.errstate(divide='ignore', invalid='ignore'):
        drawdown = np.where(cummax > 0, (cummax - close) / cummax, 0)
    drawdown = np.nan_to_num(drawdown, nan=0.0)
    max_drawdown = float(np.max(drawdown))
    max_dd_end_idx = int(np.argmax(drawdown))
    max_dd_start_idx = int(np.argmax(close[:max_dd_end_idx + 1])) if max_dd_end_idx > 0 else 0

    # 夏普比率
    sharpe_ratio = (annualized_return - risk_free_rate) / volatility if volatility > 0 else 0

    # Sortino 比率（只用下行波动率）
    negative_returns = daily_returns[daily_returns < 0]
    if len(negative_returns) >= 2:
        downside_vol = np.std(negative_returns, ddof=1) * np.sqrt(TRADING_DAYS)
    else:
        downside_vol = volatility
    sortino_ratio = (annualized_return - risk_free_rate) / downside_vol if downside_vol > 0 else 0

    # VaR 95%（日收益率的 5% 分位数）
    var_95 = float(np.percentile(daily_returns, 5))

    # Calmar 比率
    calmar_ratio = annualized_return / max_drawdown if max_drawdown > 0 else 0

    result = {
        'total_return': round(total_return * 100, 2),
        'annualized_return': round(annualized_return * 100, 2),
        'volatility': round(volatility * 100, 2),
        'max_drawdown': round(max_drawdown * 100, 2),
        'sharpe_ratio': round(sharpe_ratio, 3),
        'sortino_ratio': round(sortino_ratio, 3),
        'var_95_daily': round(var_95 * 100, 2),
        'calmar_ratio': round(calmar_ratio, 3),
        'trading_days': days,
        'risk_free_rate': round(risk_free_rate * 100, 2),
    }

    # Beta（需要基准数据）
    if benchmark_df is not None and not benchmark_df.empty:
        bench_close = benchmark_df['close'].values.astype(float)
        bench_returns = np.diff(bench_close) / bench_close[:-1]
        min_len = min(len(daily_returns), len(bench_returns))
        if min_len > 10:
            stock_ret = daily_returns[-min_len:]
            bench_ret = bench_returns[-min_len:]
            cov_matrix = np.cov(stock_ret, bench_ret)
            bench_var = np.var(bench_ret, ddof=1)
            beta = cov_matrix[0, 1] / bench_var if bench_var > 0 else 0
            alpha = annualized_return - risk_free_rate - beta * (np.mean(bench_ret) * TRADING_DAYS - risk_free_rate)
            corr = np.corrcoef(stock_ret, bench_ret)[0, 1]
            result['beta'] = round(float(beta), 3)
            result['alpha'] = round(float(alpha) * 100, 2)
            result['correlation'] = round(float(corr), 3)

    # 风险等级
    result['risk_level'] = _assess_risk_level(result)

    return result


_RISK_SCORING_RULES = {
    'volatility': {
        'weight': 0.30,
        'ascending': True,
        'ranges': [(25, 0), (35, 1), (50, 2)],
    },
    'max_drawdown': {
        'weight': 0.30,
        'ascending': True,
        'ranges': [(20, 0), (35, 1), (50, 2)],
    },
    'sharpe_ratio': {
        'weight': 0.20,
        'ascending': False,
        'ranges': [(1.0, 0), (0.5, 1), (0, 2)],
    },
    'sortino_ratio': {
        'weight': 0.10,
        'ascending': False,
        'ranges': [(1.0, 0), (0.5, 1), (0, 2)],
    },
    'var_95_daily': {
        'weight': 0.10,
        'ascending': False,
        'ranges': [(-1.5, 0), (-2.5, 1), (-4.0, 2)],
    },
}

_RISK_LEVELS = [(1.0, '低风险'), (1.5, '中低风险'), (2.5, '中高风险'), (float('inf'), '高风险')]


def _score_value(value, ranges, ascending):
    """根据评分区间计算单项风险分数

    ascending=True: 值越大风险越高（如波动率）
      ranges=[(25,0),(35,1),(50,2)] → <25→0, 25-35→1, 35-50→2, ≥50→3
    ascending=False: 值越大风险越低（如夏普比率）
      ranges=[(1.0,0),(0.5,1),(0,2)] → ≥1.0→0, 0.5-1.0→1, 0-0.5→2, <0→3
    """
    if ascending:
        for threshold, score in ranges:
            if value < threshold:
                return score
        return ranges[-1][1] + 1
    else:
        for threshold, score in ranges:
            if value >= threshold:
                return score
        return ranges[-1][1] + 1


def _assess_risk_level(metrics: dict) -> str:
    """根据指标综合评估风险等级（加权评分法）"""
    weighted_sum = 0.0
    total_weight = 0.0

    for key, rule in _RISK_SCORING_RULES.items():
        value = metrics.get(key)
        if value is None:
            continue
        score = _score_value(value, rule['ranges'], rule['ascending'])
        weighted_sum += score * rule['weight']
        total_weight += rule['weight']

    if total_weight == 0:
        return '中低风险'

    avg_score = weighted_sum / total_weight

    for threshold, level in _RISK_LEVELS:
        if avg_score < threshold:
            return level
    return _RISK_LEVELS[-1][1]
