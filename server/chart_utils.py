"""图表技术指标计算工具函数

从 server/routes/charts.py 提取，供 charts 路由和 backtest 路由共用。
"""
import numpy as np


def round_list(arr, decimals=2) -> list:
    """四舍五入并转为列表，NaN 转 None"""
    return [round(float(v), decimals) if not np.isnan(v) else None for v in arr]


def ma(data: np.ndarray, period: int) -> np.ndarray:
    """简单移动平均（NaN 安全：窗口内含 NaN 时自动跳过，仅当有效样本不足 period 才返回 NaN）

    注意：原实现直接用 np.cumsum，遇到前导 NaN（如 DPO 的 warmup 区）会把 NaN 传播到全部结果。
    这里改为同时累计有效样本数，窗口内 NaN 不参与求和；纯数值输入行为与旧版完全一致。
    """
    result = np.full_like(data, np.nan, dtype=float)
    if len(data) < period:
        return result
    arr = np.asarray(data, dtype=float)
    mask = ~np.isnan(arr)
    cumsum = np.cumsum(np.where(mask, arr, 0.0))
    count = np.cumsum(mask.astype(float))
    window_sum = cumsum[period - 1:] - np.concatenate([[0.0], cumsum[:-period]])
    window_cnt = count[period - 1:] - np.concatenate([[0.0], count[:-period]])
    valid = window_cnt >= period
    result[period - 1:][valid] = window_sum[valid] / window_cnt[valid]
    return result


def ema(data: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均"""
    result = np.full_like(data, np.nan, dtype=float)
    if len(data) < period:
        return result
    multiplier = 2 / (period + 1)
    result[period-1] = np.mean(data[:period])
    for i in range(period, len(data)):
        result[i] = (data[i] - result[i-1]) * multiplier + result[i-1]
    return result


def macd(close: np.ndarray, fast: int, slow: int, signal: int):
    """MACD 指标，返回 (dif, dea, hist)"""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif[~np.isnan(dif)], signal)

    full_dea = np.full_like(close, np.nan, dtype=float)
    start_idx = len(close) - len(dea)
    full_dea[start_idx:] = dea

    hist = 2 * (dif - full_dea)
    return dif, full_dea, hist


def rsi(close: np.ndarray, period: int) -> np.ndarray:
    """RSI 指标"""
    result = np.full_like(close, np.nan, dtype=float)
    if len(close) < period + 1:
        return result

    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        result[period] = 100
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100 - (100 / (1 + rs))

    return result


def kdj(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        n: int = 9, m1: int = 3, m2: int = 3):
    """KDJ 指标，返回 (k, d, j)"""
    length = len(close)
    k = np.full(length, 50.0)
    d = np.full(length, 50.0)
    j = np.full(length, 50.0)

    for i in range(n - 1, length):
        period_high = np.max(high[i-n+1:i+1])
        period_low = np.min(low[i-n+1:i+1])
        if period_high == period_low:
            rsv = 50
        else:
            rsv = (close[i] - period_low) / (period_high - period_low) * 100

        if i == n - 1:
            k[i] = rsv
            d[i] = rsv
        else:
            k[i] = (2/3) * k[i-1] + (1/3) * rsv
            d[i] = (2/3) * d[i-1] + (1/3) * k[i]

        j[i] = 3 * k[i] - 2 * d[i]

    return k, d, j


def boll(close: np.ndarray, period: int = 20, std_dev: int = 2):
    """布林带，返回 (upper, mid, lower)"""
    mid = ma(close, period)
    result_std = np.full_like(close, np.nan, dtype=float)

    for i in range(period - 1, len(close)):
        result_std[i] = np.std(close[i-period+1:i+1], ddof=0)

    upper = mid + std_dev * result_std
    lower = mid - std_dev * result_std

    return upper, mid, lower


def dpo(close: np.ndarray, period: int = 20, smooth: int = 6):
    """区间震荡线 DPO，返回 (dpo, madpo)

    公式: DPO[t] = close[t] - SMA(close, period)[t - (period//2 + 1)]
          MADPO  = SMA(DPO, smooth)
    平移量 shift = period // 2 + 1，把长期趋势拉直为 0 轴，仅保留短期偏离。
    """
    length = len(close)
    sma = ma(close, period)
    shift = period // 2 + 1

    dpo_arr = np.full(length, np.nan, dtype=float)
    for i in range(shift, length):
        past_sma = sma[i - shift]
        if not np.isnan(past_sma):
            dpo_arr[i] = close[i] - past_sma

    madpo_arr = ma(dpo_arr, smooth)
    return dpo_arr, madpo_arr


def build_chart_indicators(df, decls: list) -> dict:
    """按策略声明，用 charts.py 的算法算出 K 线叠加指标。

    df: 含 close/high/low 列的 DataFrame（复权后）
    decls: 策略 get_chart_indicators 返回的声明列表
    返回 {'main': [{name, data}], 'sub': [{type, ...}]}
    """
    main, sub = [], []
    if df is None or df.empty or not decls:
        return {'main': main, 'sub': sub}

    close = df['close'].values
    high = df['high'].values if 'high' in df.columns else close
    low = df['low'].values if 'low' in df.columns else close

    for d in decls:
        t = d.get('type')
        if t == 'MA':
            p = int(d.get('period', 5))
            main.append({'name': f'MA{p}', 'data': round_list(ma(close, p))})
        elif t == 'BOLL':
            upper, mid, lower = boll(close, int(d.get('period', 20)), float(d.get('std', 2)))
            main.append({'name': 'BOLL上轨', 'data': round_list(upper)})
            main.append({'name': 'BOLL中轨', 'data': round_list(mid)})
            main.append({'name': 'BOLL下轨', 'data': round_list(lower)})
        elif t == 'MACD':
            dif, dea, hist = macd(close, int(d.get('fast', 12)), int(d.get('slow', 26)), int(d.get('signal', 9)))
            sub.append({'type': 'MACD', 'dif': round_list(dif), 'dea': round_list(dea), 'hist': round_list(hist)})
        elif t == 'RSI':
            p = int(d.get('period', 14))
            sub.append({'type': 'RSI', 'period': p, 'data': round_list(rsi(close, p))})
        elif t == 'KDJ':
            k, dd, j = kdj(high, low, close)
            sub.append({'type': 'KDJ', 'k': round_list(k), 'd': round_list(dd), 'j': round_list(j)})
        elif t == 'DPO':
            p = int(d.get('period', 20))
            m = int(d.get('smooth', 6))
            dpo_arr, madpo_arr = dpo(close, p, m)
            sub.append({
                'type': 'DPO',
                'dpo': round_list(dpo_arr),
                'madpo': round_list(madpo_arr),
                'period': p,
                'smooth': m,
            })
    return {'main': main, 'sub': sub}

