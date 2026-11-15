"""回测（体检 / 多窗口 / 涨跌停口径）（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _BacktestMixin:
    BACKTEST_MIN_COVERAGE = float(os.getenv("BACKTEST_MIN_COVERAGE", "0.95"))

    # 样本内外评判：回测完成后自动后台补跑 IS/OOS 对照（verdict 落 backtest_eval 表）

    BACKTEST_AUTO_EVAL = os.getenv("BACKTEST_AUTO_EVAL", "true").lower() == "true"

    # 多窗口稳健性评估（P1 §3）：K 段窗口 + 段间 embargo gap（交易日）

    BACKTEST_MULTI_WINDOW_N = int(os.getenv("BACKTEST_MULTI_WINDOW_N", "4"))

    BACKTEST_MULTI_WINDOW_EMBARGO = int(os.getenv("BACKTEST_MULTI_WINDOW_EMBARGO", "5"))

    # 涨跌停拦截口径（P0 撮合保真）：enforce=真实拦截（默认）/ warn=记录归因但放行 / off=关闭

    PRICE_LIMIT_MODE = os.getenv("PRICE_LIMIT_MODE", "enforce").strip().lower()

    # 全局总览大屏（研究分析 → 全局总览；默认值即行为）

