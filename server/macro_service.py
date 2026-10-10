"""宏观指标看板薄服务：只读 macro_indicator 长表（fundamental_cache.db）。

数据路径与 thesis 引擎完全一致（utils/cache/macro_db.py），不新增查询口径。
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date

from utils.cache.macro_db import (
    MACRO_INDICATOR_META,
    get_macro_series,
    stale_threshold_days,
)

logger = logging.getLogger(__name__)

# 默认展示项（顺序即前端渲染顺序）；由调用方传入 indicators 覆盖（后续任务接入 OVERVIEW_MACRO_INDICATORS 配置）
DEFAULT_INDICATORS = [
    "cn10_y", "us10_y", "shibor_1w", "lpr_1y", "usdcny", "cpi_yoy",
    "pmi_mfg", "gdp_yoy", "au_sge", "cu_main", "hog_spot_price", "cn_gold_reserve",
    "cn30_y", "us2_y", "dxy", "m2_yoy", "us_cpi_yoy", "ag_sge",
]

_DATE_RE = re.compile(r"(\d{4})[.\-](\d{1,2})(?:[.\-](\d{1,2}))?")


def _days_since(report_date: str, today: date) -> int | None:
    """报告期距今天数；缺「日」的月度格式（如 2026.8）按月末解析。

    与 tools/fetcher/akshare_ds.py::_normalize_report_date 口径一致；无法解析返回 None。
    """
    m = _DATE_RE.match(str(report_date or ""))
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    d = int(m.group(3)) if m.group(3) else None
    try:
        if d is None:
            d = calendar.monthrange(y, mo)[1]
        return (today - date(y, mo, d)).days
    except ValueError:
        return None


def build_macro_board(indicators: list[str] | None = None) -> dict:
    """聚合宏观看板。单指标失败不阻塞整块，计入 missing。"""
    codes = DEFAULT_INDICATORS if indicators is None else list(dict.fromkeys(indicators))
    items: list[dict] = []
    missing: list[str] = []
    today = date.today()
    for code in codes:
        meta = MACRO_INDICATOR_META.get(code)
        if not meta:
            missing.append(code)
            continue
        try:
            rows = get_macro_series(code, periods=13)
        except Exception as e:
            logger.warning("[overview] 宏观指标 %s 读取失败: %s", code, e)
            missing.append(code)
            continue
        # pandas NaN 可落库为 NULL（value REAL 可空），先过滤再判空
        rows = [r for r in rows if r.get("value") is not None]
        if not rows:
            missing.append(code)
            continue
        values = [float(r["value"]) for r in rows]
        prev_value = values[-2] if len(values) >= 2 else None
        delta = round(values[-1] - prev_value, 4) if prev_value is not None else None
        days = _days_since(rows[-1]["report_date"], today)
        items.append({
            "code": code,
            "label": meta.get("label", code),
            "unit": meta.get("unit", ""),
            "value": values[-1],
            "report_date": rows[-1]["report_date"],
            "prev_value": prev_value,
            "delta": delta,
            "delta_display": f"{delta:+g}" if delta is not None else "—",
            "series": values[-12:],
            "stale": days is not None and days > stale_threshold_days(code),
            "stale_days": days,
        })
    return {"items": items, "missing": missing}
