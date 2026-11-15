"""条件单触发判定：回踩/突破/未触发/待定/不可判定。"""
from signal_eval.trigger import trigger_status


def _kline(closes, lows=None):
    return [
        {"date": f"2026-11-{i + 1:02d}", "close": c,
         "low": lows[i] if lows else c}
        for i, c in enumerate(closes)
    ]


def test_pullback_triggered_at_first_touch():
    k = _kline([0.50, 0.49, 0.48, 0.465, 0.47],
               lows=[0.50, 0.48, 0.47, 0.46, 0.46])
    r = trigger_status(k, "2026-11-01", "pullback", 0.46, 0.47, window=5)
    assert r["status"] == "triggered"
    assert r["days_to_trigger"] == 2
    assert r["trigger_date"] == "2026-11-03"


def test_trigger_scan_starts_next_trading_day():
    """信号日当天触达不算：按纪律当天还没买。"""
    k = _kline([0.40, 0.60, 0.60], lows=[0.40, 0.60, 0.60])
    r = trigger_status(k, "2026-11-01", "pullback", 0.46, 0.47, window=2)
    assert r["status"] == "not_triggered"
    assert r["trigger_date"] is None
    assert r["days_to_trigger"] is None


def test_trigger_scan_starts_next_trading_day_when_signal_date_has_no_bar():
    """信号日非交易日/缺 bar：下一根 K 线就是第一个可扫描交易日，不能跳过。"""
    k = [
        {"date": "2026-11-02", "close": 0.50, "low": 0.39},
        {"date": "2026-11-03", "close": 0.50, "low": 0.45},
        {"date": "2026-11-04", "close": 0.50, "low": 0.45},
    ]
    r = trigger_status(k, "2026-11-01", "pullback", 0.40, 0.40, window=2)
    assert r["status"] == "triggered"
    assert r["trigger_date"] == "2026-11-02"
    assert r["days_to_trigger"] == 1


def test_breakout_triggered_by_close():
    # 第 3 根 low 0.68 已低于阈值 0.69，只有 close 0.70 能触发
    k = _kline([0.60, 0.65, 0.70], lows=[0.60, 0.64, 0.68])
    r = trigger_status(k, "2026-11-01", "breakout", 0.69, None, window=3)
    assert r["status"] == "triggered"
    assert r["days_to_trigger"] == 2


def test_breakout_triggers_when_close_equals_level():
    """突破是 >=：收盘价恰好等于阈值也算站上。"""
    k = _kline([0.60, 0.69])
    r = trigger_status(k, "2026-11-01", "breakout", 0.69, None, window=2)
    assert r["status"] == "triggered"
    assert r["trigger_date"] == "2026-11-02"
    assert r["days_to_trigger"] == 1


def test_pullback_single_side_uses_low_as_upper_level():
    """回踩只给 low（high=None）：用 low 当上沿。"""
    k = _kline([0.50, 0.50, 0.50], lows=[0.50, 0.50, 0.455])
    r = trigger_status(k, "2026-11-01", "pullback", 0.46, None, window=3)
    assert r["status"] == "triggered"
    assert r["trigger_date"] == "2026-11-03"
    assert r["days_to_trigger"] == 2


def test_breakout_single_side_uses_high_as_level():
    """突破只给 high（low=None）：用 high 当阈值。"""
    k = _kline([0.60, 0.65, 0.70])
    r = trigger_status(k, "2026-11-01", "breakout", None, 0.69, window=3)
    assert r["status"] == "triggered"
    assert r["trigger_date"] == "2026-11-03"
    assert r["days_to_trigger"] == 2


def test_pullback_ignores_bar_missing_low():
    """回踩只看 low；缺 low 的 bar 不能拿 close 兜底。"""
    k = [
        {"date": "2026-11-01", "close": 0.50, "low": 0.50},
        {"date": "2026-11-02", "close": 0.39},  # 缺 low，close 已跌破上沿
        {"date": "2026-11-03", "close": 0.50, "low": 0.50},
    ]
    r = trigger_status(k, "2026-11-01", "pullback", 0.40, 0.40, window=2)
    assert r["status"] == "not_triggered"


def test_pullback_all_window_lows_missing_is_not_triggered():
    k = [
        {"date": "2026-11-01", "close": 0.50, "low": 0.50},
        {"date": "2026-11-02", "close": 0.39},
        {"date": "2026-11-03", "close": 0.38},
    ]
    r = trigger_status(k, "2026-11-01", "pullback", 0.40, 0.40, window=2)
    assert r["status"] == "not_triggered"


def test_non_positive_or_invalid_window_defaults_to_ten():
    """窗口非正/非法按 10 处理：第 6 根才触达，window=1 会漏判。"""
    lows = [0.50] * 12
    lows[6] = 0.39  # 信号后第 6 根（全表索引 6）才触达
    k = _kline([0.50] * 12, lows=lows)
    for window in (-1, 0, None, "abc"):
        r = trigger_status(k, "2026-11-01", "pullback", 0.40, 0.40, window=window)
        assert r["status"] == "triggered", window
        assert r["days_to_trigger"] == 6, window
        assert r["trigger_date"] == "2026-11-07", window


def test_window_cap_excludes_bar_beyond_window():
    """窗口上限：信号日 11-01 + 4 根 K 线，window=3 只扫 11-02/03/04。

    第 4 根 11-05 才跌破上沿；若 end 误取 len(kline) 或 start+window+1，
    窗口外的触达会被算成 triggered，直接抬高触发率。
    """
    k = [
        {"date": "2026-11-02", "close": 0.50, "low": 0.50},
        {"date": "2026-11-03", "close": 0.50, "low": 0.50},
        {"date": "2026-11-04", "close": 0.50, "low": 0.50},
        {"date": "2026-11-05", "close": 0.50, "low": 0.39},  # 窗口外才触达
    ]
    r = trigger_status(k, "2026-11-01", "pullback", 0.46, 0.47, window=3)
    assert r["status"] == "not_triggered"
    assert r["trigger_date"] is None
    assert r["days_to_trigger"] is None
    # 对照：window=4 时同一根会触发，证明不是数据本身够不着上沿
    r4 = trigger_status(k, "2026-11-01", "pullback", 0.46, 0.47, window=4)
    assert r4["status"] == "triggered"
    assert r4["trigger_date"] == "2026-11-05"
    assert r4["days_to_trigger"] == 4


def test_pending_when_kline_not_enough():
    k = _kline([0.60, 0.60])
    r = trigger_status(k, "2026-11-01", "breakout", 0.69, None, window=5)
    assert r["status"] == "pending"
    assert r["trigger_date"] is None
    assert r["days_to_trigger"] is None


def test_pending_when_all_kline_before_signal_date():
    """没有任何晚于信号日的 K 线 → 还没有可扫描的交易日。"""
    k = _kline([0.60, 0.60])
    r = trigger_status(k, "2026-11-05", "breakout", 0.50, None, window=2)
    assert r["status"] == "pending"
    assert r["trigger_date"] is None
    assert r["days_to_trigger"] is None


def test_unverifiable_without_bounds_or_type():
    k = _kline([0.60, 0.60])
    assert trigger_status(k, "2026-11-01", "unverifiable", None, None, 5)["status"] == "unverifiable"
    assert trigger_status(k, "2026-11-01", "pullback", None, None, 5)["status"] == "unverifiable"


def test_unverifiable_with_string_bounds():
    """字符串边界不能参与数值比较 → unverifiable，不能静默当未触发。"""
    k = _kline([0.60, 0.60])
    r = trigger_status(k, "2026-11-01", "pullback", "0.46", "0.47", window=5)
    assert r["status"] == "unverifiable"
    assert r["trigger_date"] is None
    assert r["days_to_trigger"] is None


def test_unverifiable_with_unknown_entry_type():
    """不认识的 entry_type，即使边界有效也不能判定。"""
    k = _kline([0.60, 0.60])
    r = trigger_status(k, "2026-11-01", "hold", 0.46, 0.47, window=5)
    assert r["status"] == "unverifiable"
    assert r["trigger_date"] is None
    assert r["days_to_trigger"] is None
