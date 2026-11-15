"""A 股涨跌停 / 停牌判定（纯函数，无 IO）。

设计依据：``docs/superpowers/specs/2026-10-15-backtest-p0-matching-fidelity-design.md`` §4.1。

判定一律用**涨跌幅比例**而不是 round 到分的绝对价：

- 等比复权下比例是不变量，绝对价会随因子漂（设计 §4.3）；
- 涨停价 round 到分引入 ≤0.005 元的量化误差，用随价格缩放的比例容差吸收。

与本包 health.py 同风格：只有纯函数；取基准交易日、读行情等 IO 留在
backtest/engine.py / broker 层。

⚠️ **ST 限幅（2026 年 10 月规则变更后）**：主板风险警示股（ST/*ST/SST/S*ST）涨跌幅已与
主板统一为 **10%**，且 ST **不压缩** 20%/30% 板块的限幅（实测 `*ST航图` 688066 当日
+20.04% 精确落在 20% 涨停价）。因此 ``limit_pct`` 不再按名称降档，ST 直接走板块阈值。
本仓库另一处 ``utils/price_limit.py``（收盘涨跌停家数统计用）同日同口径，两者一致；
改任一处都要同步 ``test/unit/backtest/test_a_share_rules.py`` 与
``test/unit/tools/test_price_limit.py``。
"""
from __future__ import annotations

MAIN_LIMIT_PCT = 0.10     # 主板：600/601/603/605、000/001/002/003
GEM_LIMIT_PCT = 0.20      # 创业板：300/301/302
STAR_LIMIT_PCT = 0.20     # 科创板：688/689
BSE_LIMIT_PCT = 0.30      # 北交所：43/83/87/88 + 920（2023 年启用的新码段，同 30% 限幅）
ST_LIMIT_PCT = MAIN_LIMIT_PCT  # 2026 起风险警示股与主板一致（保留别名供历史引用），
                              # limit_pct 不再据此改判：ST 跟随板块阈值
TOL_FLOOR = 0.002         # 容差下限 0.2%
_TOL_ABS = 0.0075         # 1.5 × 半分钱（0.005 元）

_MAIN_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
_GEM_PREFIXES = ("300", "301", "302")
_STAR_PREFIXES = ("688", "689")
_BSE_PREFIXES = ("43", "83", "87", "88", "920")
_ST_NAME_PREFIXES = ("ST", "*ST", "SST", "S*ST")  # 2026 口径不再使用；保留以备规则回退


def limit_pct(code: str, name: str = "") -> float | None:
    """返回涨跌幅限制比例；``None`` = 该标的无涨跌停约束（调用方应 fail-open）。

    - 板块：创业板/科创板 20%、北交所 30%、主板 10%；
    - **ST 不参与判断**（2026-10 起风险警示股与主板同为 10%，且不压缩 20%/30% 限幅）；
    - 未知前缀（B 股 / 基金 / 异常输入）→ None，宁可诚实不拦也不假装拦对
      （ETF 的 10%/20% 无法用代码可靠区分，由调用方按 asset_type 分流）。

    ``name`` 保留为入参：一是调用方已有该信息（``a_stock_broker._lookup_stock_name``），
    二是若规则回退到 5%，只需在此处恢复「名称以 ST 开头 → ST_LIMIT_PCT」分支。
    """
    c = str(code or "").strip()
    if not c:
        return None
    if c.startswith(_BSE_PREFIXES):
        return BSE_LIMIT_PCT
    if c.startswith(_GEM_PREFIXES) or c.startswith(_STAR_PREFIXES):
        return GEM_LIMIT_PCT  # 创业板 / 科创板同幅 20%
    if c.startswith(_MAIN_PREFIXES):
        return MAIN_LIMIT_PCT
    return None


def etf_limit_pct(code: str) -> float | None:
    """ETF 涨跌幅限制比例的**显式解析入口**；当前恒返回 ``None`` = 不拦（默认）。

    ⚠️ 为什么不做前缀判断：ETF 的限幅并不只由 6 位代码前缀决定——股票型 ETF 多为
    10%，而科创/创业板类 ETF、跨境 ETF 等可达 20%（例如 510300 与 588000 同属
    51/58 段，限幅可能不同）。单靠前缀硬编码 10% 会误伤 20% 的 ETF，把可成交的单
    错判成「涨停/跌停」——那是比不拦更严重的保真问题。

    因此本入口保持 ``None``（fail-open），并由 ``backtest/engine.py`` 在
    ``warnings`` / ``health_detail`` 中显式声明「ETF 涨跌停未模拟（10%/20% 无法
    可靠区分）」。宁可诚实不拦，也不假装拦对。

    Args:
        code: 标的代码（保留入参，便于将来接入可区分的限幅数据源后按码返回）
    """
    return None


def limit_tolerance(pre_close: float, floor: float = TOL_FLOOR) -> float:
    """随价格缩放的判定容差：吸收「涨停价 round 到分」的量化误差。

    0.0075 元 ≈ 1.5 × 半分钱（0.005），除以昨收得比例，再与下限取大。
    ``pre_close`` 无效（≤0 / 非数）时退回下限。
    """
    try:
        pc = float(pre_close)
    except (TypeError, ValueError):
        return floor
    if pc <= 0:
        return floor
    return max(_TOL_ABS / pc, floor)


def is_fill_blocked(side: str, exec_price: float, pre_close: float,
                    pct: float | None, tolerance: float | None = None) -> str:
    """返回 ``''``（可成交）| ``'limit_up'`` | ``'limit_down'``。

    ``chg = exec_price / pre_close - 1``；
    buy 且 ``chg >= pct - tol`` → ``'limit_up'``；
    sell 且 ``chg <= -(pct - tol)`` → ``'limit_down'``；
    ``pre_close <= 0`` / ``exec_price <= 0`` / ``pct is None`` → ``''``（数据不可用，fail-open）。

    ⚠️ 容差是「放宽」而不是「收紧」：``pre_close=10.03``、10% 限制的涨停价
    ``round(11.033, 2)=11.03``，实际涨幅 9.97% < 10%；若按 ``chg >= pct`` 判会
    漏判真涨停。故取 ``pct - tol``。
    """
    if pct is None:
        return ""
    try:
        pc = float(pre_close)
        px = float(exec_price)
    except (TypeError, ValueError):
        return ""
    if pc <= 0 or px <= 0:
        return ""
    tol = tolerance if tolerance is not None else limit_tolerance(pc)
    chg = px / pc - 1
    if side == "buy" and chg >= pct - tol:
        return "limit_up"
    if side == "sell" and chg <= -(pct - tol):
        return "limit_down"
    return ""


def is_suspended(volume) -> bool:
    """停牌 / 无成交：成交量为 0 或缺失。

    ``trade_status`` / ``is_st`` 是死列（设计 §1.3），不可用。停牌在库里多表现为
    **缺行**，缺行由 backtrader 的 bar 调度自然跳过（无 bar 即无成交尝试），
    本函数只负责 ``volume <= 0`` 的占位行。
    """
    if volume is None:
        return True
    try:
        return float(volume) <= 0
    except (TypeError, ValueError):
        return True


def resolve_mode(config_value, default: str = '') -> str:
    """归一化涨跌停拦截口径：``off`` / ``warn`` / ``enforce``。

    取法：单次请求值 > 全局配置值 > ``enforce``（非法值静默回落）。
    纯函数——全局配置由调用方读出后作为 ``default`` 传入。
    """
    m = str(config_value or '').strip().lower()
    if m in ('off', 'warn', 'enforce'):
        return m
    d = str(default or '').strip().lower()
    return d if d in ('off', 'warn', 'enforce') else 'enforce'
