"""A 股涨跌停价解析（纯函数，无 IO）。

## 为什么不能用「涨跌幅 >= 9.8%」这类阈值

涨停价 = ``round(昨收 × (1 + 限幅), 2)``，**四舍五入到分**后实际涨幅不再是整 10%：

| 板块 | 限幅 | 实测涨停幅度区间（2026-10-30） |
|---|---|---|
| 主板 | 10% | 9.615% ~ 12.000%（低价股误差最大：1.04 元 → 9.615%，0.25 元 → 12.000%） |
| 创业板 / 科创板 | 20% | 19.737% ~ 20.207% |
| 北交所 | 30% | 29.688% ~ 30.189% |

所以两种阈值都会错：
- ``>= 9.8`` 会把**创业板 +12% 的非涨停股**当成涨停（今日误报 11 只），
  又会**漏掉所有 ST 5% 板**（`+4.81%` 的 ST 易购被封板却不算）；
- ``<= -9.8`` 更严重：今日实测跌停 precision 仅 **43.4%**（189 家 vs 官方 82 家），
  因为创业板/科创板 −10%~−20% 的普通下跌全被计成跌停。

## ST 口径（2026 年实测）

2026 年主板风险警示股涨跌幅已与主板统一为 **10%**（不再是 5%）：
- `*ST闻泰` 10.36 → 11.40 = +10.04%，恰好等于 `round(10.36×1.10, 2)`；
- `ST新华锦` 6.18 → 5.56 = −10.03%，恰好等于 `round(6.18×0.90, 2)`；
- 197 只 ST 股里 30 只 |涨跌幅| > 5.2%，若限幅仍为 5% 不可能出现。

而 **ST 不会压缩 20%/30% 板块的限幅**：`*ST航图`（688066）当日 4.64 → 5.57 = +20.04%，
恰好等于 20% 涨停价。所以「按板块定限幅」已覆盖 ST 的全部情形，无需单独降档；
若未来主板 ST 回退到 5%，只需在 ``limit_rate`` 里对主板 ST 返回 ``ST_LIMIT_RATE``。
（``backtest/a_share_rules.py`` 里仍是 5%，那是回测成交拦截口径，两者暂未合并，
已在各自 docstring 互相注明。）

## 新股的自我保护

价格匹配天然免疫「新股上市首日/前 5 日不设限幅」：首日 +44% 或 +200% 的收盘价
不会恰好落在 10%/20%/30% 的涨停价上，不会被误计为涨停。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# 2026 年口径：主板风险警示股与主板一致（证据见模块 docstring）
ST_LIMIT_RATE = 0.10

_MAIN_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
_GEM_PREFIXES = ("300", "301", "302")
_STAR_PREFIXES = ("688", "689")
_BSE_PREFIXES = ("43", "83", "87", "88", "920")

# 涨跌停价与收盘价的判定容差（元）：半分钱。价格匹配是精确比对，
# 容差只为吸收浮点误差；不要放大到 0.01（会把「差一分钱」算成封板）。
PRICE_TOL = 0.005


def limit_rate(code: str, name: str = "") -> float | None:
    """返回涨跌幅限制比例；``None`` = 无约束/未知标的（调用方应 fail-open）。

    顺序：北交所 30% → 创业板/科创板 20% → 主板 10%。ST 不参与判断——2026 年主板 ST
    与主板同为 10%，而 20%/30% 板块的 ST 仍是 20%/30%（见模块 docstring 的证据）。
    ``name`` 仅为将来规则回退保留，当前不影响结果。
    未知前缀（B 股 / ETF / 基金 / 异常输入）返回 ``None``，宁可诚实不判。
    """
    c = str(code or "").strip()
    if not c or len(c) < 6:
        return None
    if c.startswith(_BSE_PREFIXES):
        return 0.30
    if c.startswith(_GEM_PREFIXES) or c.startswith(_STAR_PREFIXES):
        return 0.20
    if c.startswith(_MAIN_PREFIXES):
        return 0.10
    return None


def round_price(value: float) -> float:
    """交易所口径的四舍五入到分（``ROUND_HALF_UP``）。

    ⚠️ 不能用内置 ``round()``：它走银行家舍入（``round(0.125, 2) == 0.12``），
    与交易所的“见五进一”差一分钱，低价股的涨停判定会系统性偏移。
    """
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_prices(pre_close: float, rate: float | None) -> tuple[float, float] | None:
    """由昨收与限幅算出 ``(涨停价, 跌停价)``；输入无效或 ``rate is None`` 返回 ``None``。"""
    if rate is None:
        return None
    try:
        pc = float(pre_close)
    except (TypeError, ValueError):
        return None
    if pc <= 0:
        return None
    return round_price(pc * (1 + rate)), round_price(pc * (1 - rate))


def classify(close: float, pre_close: float, code: str, name: str = "",
             tol: float = PRICE_TOL) -> str:
    """返回 ``'limit_up'`` / ``'limit_down'`` / ``''``（未封板或无法判定）。

    判定 = 收盘价与「按本板块限幅算出的涨停价/跌停价」相差不超过 ``tol`` 元。
    无昨收、无限幅、价格非法 → ``''``（fail-open，不猜）。
    """
    rate = limit_rate(code, name)
    prices = limit_prices(pre_close, rate)
    if prices is None:
        return ""
    try:
        px = float(close)
    except (TypeError, ValueError):
        return ""
    if px <= 0:
        return ""
    up, down = prices
    if abs(px - up) <= tol:
        return "limit_up"
    if abs(px - down) <= tol:
        return "limit_down"
    return ""


def limit_pct_range(pre_close: float, rate: float) -> tuple[float, float]:
    """该价位下涨停幅度的理论区间（仅供文档/校验用）。"""
    prices = limit_prices(pre_close, rate)
    if prices is None:
        return 0.0, 0.0
    up, down = prices
    return round((up / pre_close - 1) * 100, 4), round((down / pre_close - 1) * 100, 4)
