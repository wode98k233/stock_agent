"""
选股雷达 - 技术指标分析
15+ 技术指标，核心: MACD, MA 金叉/死叉, 成交量
"""
import json
import numpy as np
import pandas as pd

try:
    import talib
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False


# ── 手动计算（无 TA-Lib 时的降级方案）──────────────────────

def _sma(arr, period):
    return pd.Series(arr).rolling(period).mean().values

def _ema(arr, period):
    return pd.Series(arr).ewm(span=period, adjust=False).mean().values

def _rsi(close, period=14):
    s = pd.Series(close)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return (100 - 100 / (1 + rs)).values

def _macd_calc(close, fast=12, slow=26, signal=9):
    s = pd.Series(close)
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_bar = (dif - dea) * 2
    return dif.values, dea.values, macd_bar.values

def _bbands(close, period=20, nbdev=2):
    s = pd.Series(close)
    mid = s.rolling(period).mean()
    std = s.rolling(period).std()
    upper = mid + nbdev * std
    lower = mid - nbdev * std
    return upper.values, mid.values, lower.values

def _kdj(high, low, close, n=9, m1=3, m2=3):
    h = pd.Series(high)
    l = pd.Series(low)
    c = pd.Series(close)
    lowest = l.rolling(n).min()
    highest = h.rolling(n).max()
    rsv = (c - lowest) / (highest - lowest) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=m1-1, adjust=False).mean()
    d = k.ewm(com=m2-1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k.values, d.values, j.values


# ── 统一调用入口 ────────────────────────────────────────────

def _ma(close, period):
    if HAS_TALIB:
        return talib.MA(close, timeperiod=period)
    return _sma(close, period)

def _ema_talib(close, period):
    if HAS_TALIB:
        return talib.EMA(close, timeperiod=period)
    return _ema(close, period)

def _rsi_talib(close, period=14):
    if HAS_TALIB:
        return talib.RSI(close, timeperiod=period)
    return _rsi(close, period)

def _macd_talib(close):
    if HAS_TALIB:
        return talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    return _macd_calc(close)

def _bbands_talib(close):
    if HAS_TALIB:
        u, m, l = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2)
        return u, m, l
    return _bbands(close)

def _kdj_talib(high, low, close):
    if HAS_TALIB:
        k, d = talib.STOCH(high, low, close)
        j = 3 * k - 2 * d
        return k, d, j
    return _kdj(high, low, close)


# ── 核心函数 ────────────────────────────────────────────────

def calc_indicators(df: pd.DataFrame) -> dict:
    """
    计算全部技术指标
    输入: 含 close, high, low, volume 列的 DataFrame（按日期升序）
    返回: 指标字典
    """
    df = df.sort_index()
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    volume = df['volume'].values.astype(float)

    curr = close[-1]

    # ── 1. 均线 MA ──
    ma5 = _ma(close, 5)
    ma10 = _ma(close, 10)
    ma20 = _ma(close, 20)
    ma60 = _ma(close, 60) if len(close) >= 60 else _ma(close, len(close))

    ma_cross = _detect_cross(ma5, ma10)

    # ── 2. MACD ──
    dif, dea, macd_bar = _macd_talib(close)
    macd_cross = _detect_cross(dif, dea)

    # ── 3. 布林带 ──
    bb_upper, bb_mid, bb_lower = _bbands_talib(close)
    bb_pos = _bb_position(curr, bb_upper[-1], bb_mid[-1], bb_lower[-1])

    # ── 4. RSI ──
    rsi = _rsi_talib(close)
    rsi_val = rsi[-1]
    rsi_status = _level(rsi_val, 70, 30)

    # ── 5. 成交量 ──
    vol_ma5 = _ma(volume, 5)
    vol_ratio = volume[-1] / vol_ma5[-1] if vol_ma5[-1] > 0 else 1
    vol_status = _vol_status(vol_ratio)

    # ── 6. KDJ ──
    k, d, j = _kdj_talib(high, low, close)
    kdj_status = _kdj_signal(k, d)

    # ── 7. OBV（能量潮）──
    obv = _obv(close, volume)
    obv_trend = "上升" if obv[-1] > obv[-6] else "下降"

    # ── 8. ATR（真实波幅）──
    atr = _atr(high, low, close)

    # ── 9. CCI（顺势指标）──
    cci = _cci(high, low, close)

    # ── 10. WR（威廉指标）──
    wr = _wr(high, low, close)

    # ── 11. DMI（动向指标）──
    plus_di, minus_di, adx = _dmi(high, low, close)

    # ── 12. PSY（心理线）──
    psy = _psy(close)

    # ── 13. VR（成交量比率）──
    vr = _vr(close, volume)

    # ── 14. 涨跌幅 ──
    pct_1d = (close[-1] / close[-2] - 1) * 100 if len(close) >= 2 else 0
    pct_5d = (close[-1] / close[-6] - 1) * 100 if len(close) >= 6 else 0
    pct_20d = (close[-1] / close[-21] - 1) * 100 if len(close) >= 21 else 0

    # ── 15. 价格位置 ──
    high_20 = np.nanmax(high[-20:]) if len(high) >= 20 else np.nanmax(high)
    low_20 = np.nanmin(low[-20:]) if len(low) >= 20 else np.nanmin(low)
    price_position = (curr - low_20) / (high_20 - low_20) * 100 if high_20 != low_20 else 50

    # ── 趋势字段（5日前值 + 方向）──
    rsi_5d_ago = round(float(rsi[-6]), 2) if len(rsi) >= 6 else None
    macd_bar_5d_ago = round(float(macd_bar[-6]), 4) if len(macd_bar) >= 6 else None
    k_5d_ago = round(float(k[-6]), 2) if len(k) >= 6 else None

    return {
        # 价格
        'current_price': round(curr, 2),
        'pct_1d': round(pct_1d, 2),
        'pct_5d': round(pct_5d, 2),
        'pct_20d': round(pct_20d, 2),
        # 均线
        'ma5': round(ma5[-1], 2),
        'ma10': round(ma10[-1], 2),
        'ma20': round(ma20[-1], 2),
        'ma60': round(float(ma60[-1]), 2),
        'ma_cross': ma_cross,
        'ma_cross_days': _days_since_cross(ma5, ma10),
        # MACD
        'dif': round(dif[-1], 4),
        'dea': round(dea[-1], 4),
        'macd_bar': round(macd_bar[-1], 4),
        'macd_cross': macd_cross,
        'macd_cross_days': _days_since_cross(dif, dea),
        'macd_bar_5d_ago': macd_bar_5d_ago,
        'macd_bar_trend': '放大' if macd_bar_5d_ago is not None and abs(macd_bar[-1]) > abs(macd_bar_5d_ago) else '缩小',
        # 布林带
        'bb_upper': round(bb_upper[-1], 2),
        'bb_middle': round(bb_mid[-1], 2),
        'bb_lower': round(bb_lower[-1], 2),
        'bb_position': bb_pos,
        # RSI
        'rsi': round(rsi_val, 2),
        'rsi_status': rsi_status,
        'rsi_5d_ago': rsi_5d_ago,
        'rsi_trend': '上升' if rsi_5d_ago is not None and rsi_val > rsi_5d_ago else '下降',
        # 成交量
        'current_volume': int(volume[-1]),
        'volume_ma5': int(vol_ma5[-1]),
        'volume_ratio': round(vol_ratio, 2),
        'volume_status': vol_status,
        # KDJ
        'k': round(k[-1], 2),
        'd': round(d[-1], 2),
        'j': round(j[-1], 2),
        'kdj_status': kdj_status,
        'k_5d_ago': k_5d_ago,
        'k_trend': '上升' if k_5d_ago is not None and k[-1] > k_5d_ago else '下降',
        # 其他
        'obv_trend': obv_trend,
        'atr': round(atr, 2),
        'cci': round(cci, 2),
        'wr': round(wr, 2),
        'plus_di': round(plus_di, 2),
        'minus_di': round(minus_di, 2),
        'adx': round(adx, 2),
        'psy': round(psy, 2),
        'vr': round(vr, 2),
        'high_20d': round(high_20, 2),
        'low_20d': round(low_20, 2),
        'price_position': round(price_position, 1),
    }



# ── 辅助函数 ────────────────────────────────────────────────

def _detect_cross(fast, slow):
    if len(fast) < 3 or len(slow) < 3:
        return "无交叉"
    prev_f, prev_s = fast[-2], slow[-2]
    curr_f, curr_s = fast[-1], slow[-1]
    if np.isnan(prev_f) or np.isnan(prev_s) or np.isnan(curr_f) or np.isnan(curr_s):
        return "无交叉"
    if prev_f <= prev_s and curr_f > curr_s:
        return "金叉"
    elif prev_f >= prev_s and curr_f < curr_s:
        return "死叉"
    return "无交叉"


def _days_since_cross(fast, slow):
    """从后往前扫描，找到最近一次金叉或死叉的位置，返回距今天数。无交叉返回 -1。"""
    if len(fast) < 3 or len(slow) < 3:
        return -1
    for i in range(len(fast) - 1, 1, -1):
        f_curr, s_curr = fast[i], slow[i]
        f_prev, s_prev = fast[i - 1], slow[i - 1]
        if any(np.isnan(v) for v in [f_curr, s_curr, f_prev, s_prev]):
            continue
        # 金叉：fast从下穿上
        if f_prev <= s_prev and f_curr > s_curr:
            return len(fast) - 1 - i
        # 死叉：fast从上穿下
        if f_prev >= s_prev and f_curr < s_curr:
            return len(fast) - 1 - i
    return -1

def _bb_position(price, upper, mid, lower):
    if np.isnan(upper):
        return "数据不足"
    if price > upper:
        return "上轨之上(超买)"
    elif price > mid:
        return "中轨之上"
    elif price > lower:
        return "中轨之下"
    else:
        return "下轨之下(超卖)"

def _level(val, overbought, oversold):
    if np.isnan(val):
        return "数据不足"
    if val > overbought:
        return "超买"
    elif val < oversold:
        return "超卖"
    return "中性"

def _vol_status(ratio):
    if ratio > 2:
        return "大幅放大"
    elif ratio > 1.5:
        return "温和放大"
    elif ratio < 0.5:
        return "大幅缩量"
    elif ratio < 0.8:
        return "温和缩量"
    return "正常"

def _kdj_signal(k, d):
    if len(k) < 3:
        return "中性"
    ck, cd = k[-1], d[-1]
    pk, pd_ = k[-2], d[-2]
    if np.isnan(ck) or np.isnan(cd):
        return "中性"
    if ck > 80 and cd > 80:
        return "超买"
    elif ck < 20 and cd < 20:
        return "超卖"
    if pk <= pd_ and ck > cd:
        return "金叉"
    elif pk >= pd_ and ck < cd:
        return "死叉"
    return "中性"

def _obv(close, volume):
    obv = np.zeros(len(close))
    for i in range(1, len(close)):
        if close[i] > close[i-1]:
            obv[i] = obv[i-1] + volume[i]
        elif close[i] < close[i-1]:
            obv[i] = obv[i-1] - volume[i]
        else:
            obv[i] = obv[i-1]
    return obv

def _atr(high, low, close, period=14):
    if len(high) < period + 1:
        return 0
    tr = np.zeros(len(high))
    for i in range(1, len(high)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    return np.nanmean(tr[-period:])

def _cci(high, low, close, period=20):
    if len(close) < period:
        return 0
    tp = (high + low + close) / 3
    s = pd.Series(tp)
    sma = s.rolling(period).mean()
    mad = s.rolling(period).apply(lambda x: np.nanmean(np.abs(x - np.nanmean(x))), raw=True)
    cci = (tp - sma) / (0.015 * mad)
    last_cci = cci.iloc[-1] if not np.isnan(cci.iloc[-1]) else 0
    return float(last_cci) if not np.isnan(last_cci) else 0

def _wr(high, low, close, period=14):
    if len(close) < period:
        return -50
    h_max = np.nanmax(high[-period:])
    l_min = np.nanmin(low[-period:])
    if h_max == l_min:
        return -50
    return (h_max - close[-1]) / (h_max - l_min) * -100

def _dmi(high, low, close, period=14):
    if len(close) < period + 1:
        return 0, 0, 0
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    tr = np.zeros(len(close))
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = pd.Series(tr).rolling(period).mean().values
    plus_di = 100 * pd.Series(plus_dm).rolling(period).mean().values[-1] / atr[-1] if atr[-1] > 0 else 0
    minus_di = 100 * pd.Series(minus_dm).rolling(period).mean().values[-1] / atr[-1] if atr[-1] > 0 else 0
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
    return float(plus_di), float(minus_di), float(dx)

def _psy(close, period=12):
    if len(close) < period + 1:
        return 50
    up_days = sum(1 for i in range(-period, 0) if close[i] > close[i-1])
    return up_days / period * 100

def _vr(close, volume, period=26):
    if len(close) < period + 1:
        return 100
    rv = bv = cv = 0
    for i in range(-period, 0):
        if close[i] > close[i-1]:
            rv += volume[i]
        elif close[i] < close[i-1]:
            bv += volume[i]
        else:
            cv += volume[i]
    half_cv = cv / 2
    if bv + half_cv == 0:
        return 200
    return (rv + half_cv) / (bv + half_cv) * 100