"""macro_indicator 长表：宏观指标时序（fundamental_cache.db）。

职责与模式：
- DDL 照 fundamental_db.financial_metric 长表（PRIMARY KEY (code, date, source)）
- 增量同步照 market_data_db.fetch_and_store_index_daily（查库内 MAX(date) →
  增量过滤 → INSERT OR REPLACE）
- 单指标失败不阻断整批（逐指标 try/except，返回 error 标记）
- raw_json 存原始行摘要，akshare 列名漂移时可回溯

金十系指标（cpi/gdp/pmi）源数据滞后约 1 年（2026-09-05 实测），
仍入库供历史区间分析；消费方用 macro_freshness_days 判断是否标注滞后。
"""
import json
import logging
from datetime import datetime

from utils.cache.fundamental_db import get_fundamental_db

logger = logging.getLogger(__name__)

# macro_indicator 表定义单一事实源在 utils/db_schema/tables/fundamental.py（宏表归属 fundamental_cache.db）

# 指标元数据：label 供技能/前端展示，unit/period_type 落库。
# 与 tools/fetcher/akshare_ds.py::_MACRO_APIS 的指标集合保持一致（测试锁定）。
MACRO_INDICATOR_META = {
    "m2_yoy":  {"label": "M2 同比",       "unit": "%", "period_type": "monthly"},
    "m1_yoy":  {"label": "M1 同比",       "unit": "%", "period_type": "monthly"},
    "m0_yoy":  {"label": "M0 同比",       "unit": "%", "period_type": "monthly"},
    "lpr_1y":  {"label": "LPR 1年期",     "unit": "%", "period_type": "monthly"},
    "lpr_5y":  {"label": "LPR 5年期",     "unit": "%", "period_type": "monthly"},
    "cpi_yoy": {"label": "CPI 同比",      "unit": "%", "period_type": "monthly"},
    "gdp_yoy": {"label": "GDP 同比",      "unit": "%", "period_type": "quarterly"},
    "pmi_mfg": {"label": "制造业 PMI",    "unit": "指数", "period_type": "monthly"},
    "cn10_y":  {"label": "10Y 国债收益率", "unit": "%", "period_type": "daily"},
    "cn30_y":  {"label": "30Y 国债收益率", "unit": "%", "period_type": "daily"},
    "us10_y":  {"label": "10Y 美债收益率", "unit": "%", "period_type": "daily"},
    "shibor_1w": {"label": "Shibor 1周", "unit": "%", "period_type": "daily"},
    "usdcny":  {"label": "美元兑人民币中间价", "unit": "CNY/USD", "period_type": "daily"},
    "hk_hibor_1w": {"label": "港元 HIBOR 1周", "unit": "%", "period_type": "daily"},
    "dxy":     {"label": "美元指数（合成）", "unit": "指数", "period_type": "daily"},
    "cn_term_spread": {"label": "国债期限利差(10Y-2Y)", "unit": "%", "period_type": "daily"},
    # 黄金台账（gold_regime）专用：六因子中五个走宏观表，只有位置维度走指数日线。
    "us2_y":    {"label": "2Y 美债收益率",   "unit": "%", "period_type": "daily"},
    "us_cpi_yoy": {"label": "美国 CPI 同比", "unit": "%", "period_type": "monthly"},
    "gold_etf_holdings": {"label": "全球黄金ETF总库存", "unit": "吨", "period_type": "daily"},
    "cn_gold_reserve": {"label": "中国黄金储备", "unit": "万盎司", "period_type": "monthly"},
    "au_sge":   {"label": "上海金 Au99.99",  "unit": "元/克", "period_type": "daily"},
    "ag_sge":   {"label": "上海银基准价(早盘)", "unit": "元/千克", "period_type": "daily"},
    # 铜周期台账（cu_regime）专用。铜价与螺纹钢走新浪期货主连（calc 型）；
    # 美元/美债10Y/中国10Y 全部复用既有指标，不重复声明。
    "cu_main":  {"label": "沪铜主连(新浪期货)", "unit": "元/吨", "period_type": "daily"},
    "rb_main":  {"label": "螺纹钢主连(新浪期货)", "unit": "元/吨", "period_type": "daily"},
    # 猪周期台账（hog_regime）专用。生猪现货指数源一次给四列，落库拆成四个指标
    # （单值长表存不下多列）——⭐ 指数与均价是两个不同口径，台账里不可互换。
    # 注意该源是**周频**（约 589 行/11 年），period_type 仍标 daily：
    # 它决定的是「陈旧阈值 + 补数判定」，而该源按周更新、日频阈值(10天)足够宽松。
    "hog_spot_index":  {"label": "生猪现货指数", "unit": "指数", "period_type": "daily"},
    "hog_spot_price":  {"label": "生猪现货成交均价", "unit": "元/kg", "period_type": "daily"},
    "hog_spot_weight": {"label": "生猪出栏成交均重", "unit": "kg", "period_type": "daily"},
    "hog_spot_ma12":   {"label": "生猪现货12个月均线", "unit": "指数", "period_type": "daily"},
    "hog_lean_spot":   {"label": "生猪现货价(搜猪网)", "unit": "元/kg", "period_type": "daily"},
    "corn_spot":       {"label": "玉米现货价(搜猪网)", "unit": "元/kg", "period_type": "daily"},
    "hog_fut_main":    {"label": "生猪期货主连", "unit": "元/kg", "period_type": "daily"},
}

# 源数据滞后阈值（天）：超过则消费方须标注「数据截止 X」而非当作当日读数。
# 按周期给默认值（日频 10 天可覆盖长假，月频 45 天，季频 135 天），
# 金十系（cpi/gdp/pmi）源本身滞后约 1 年（2026-09-05 实测），单独覆盖。
MACRO_STALE_DAYS_BY_PERIOD = {"daily": 10, "monthly": 45, "quarterly": 135}
MACRO_STALE_DAYS_OVERRIDE = {"cpi_yoy": 400, "gdp_yoy": 400, "pmi_mfg": 400}


def stale_threshold_days(indicator: str) -> int:
    """某指标判定「源数据滞后」的天数阈值。

    日频指标（如国债收益率）必须比金十系宽松阈值严得多，否则源断供数月
    也会被当成当日读数——台账类消费方对此零容忍。
    """
    if indicator in MACRO_STALE_DAYS_OVERRIDE:
        return MACRO_STALE_DAYS_OVERRIDE[indicator]
    meta = MACRO_INDICATOR_META.get(indicator) or {}
    return MACRO_STALE_DAYS_BY_PERIOD.get(meta.get("period_type"), 400)


def init_macro_tables():
    """初始化 macro_indicator 表（幂等，fundamental_cache.db；DDL 统一来源 utils/db_schema）"""
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    with get_fundamental_db() as conn:
        ensure_schema(conn, ALL_DBS["fundamental"].tables)


def _default_fetcher(indicator: str):
    """默认拉取通道：fetcher 体系出口（多源降级 + 限流）。"""
    from tools.fetcher import ak_macro_indicator
    return ak_macro_indicator(indicator)


def fetch_and_store_macro(indicators: list[str] | None = None,
                          fetcher=None) -> dict:
    """逐指标增量同步宏观时序到 macro_indicator 表。

    Args:
        indicators: 指标代码列表，None 时同步全部 MACRO_INDICATOR_META
        fetcher: 拉取函数 (indicator) -> DataFrame[report_date,value]，
                 测试注入用；默认走 fetcher 出口

    Returns:
        {indicator: {"added": N} | {"error": "原因"}}——单指标失败不阻断整批。
    """
    fetch = fetcher or _default_fetcher
    codes = indicators or list(MACRO_INDICATOR_META)
    result = {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for code in codes:
        meta = MACRO_INDICATOR_META.get(code)
        if meta is None:
            result[code] = {"error": f"未知指标: {code}"}
            continue
        try:
            df = fetch(code)
            added = _upsert_indicator(code, meta, df, now)
            result[code] = {"added": added}
        except Exception as e:
            logger.warning("[Macro] 同步 %s 失败（不阻断整批）: %s", code, e)
            result[code] = {"error": str(e)}
    return result


def upsert_indicator_series(code: str, meta: dict, df, source: str = "akshare") -> int:
    """把任一时序 DataFrame 增量写入 macro_indicator（公开入口）。

    供非宏观源的指标复用本长表——例如中证指数公司的红利低波股息率/市盈率
    （indicator_code 形如 `idx_h30269_dy`），其源只给近 20 个交易日、
    必须逐期累积才能算历史分位。

    Args:
        code: 指标代码（写入 indicator_code 列）
        meta: 至少含 unit / period_type
        df: 需有 report_date、value 两列（升序或乱序均可）
        source: 数据来源标记，默认 akshare
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _upsert_indicator(code, meta, df, now, source=source)


def _upsert_indicator(code: str, meta: dict, df, now: str, source: str = "akshare") -> int:
    """单指标增量落库：库内 MAX(report_date) 之后的行 INSERT OR REPLACE。"""
    if df is None or df.empty:
        return 0
    with get_fundamental_db() as conn:
        row = conn.execute(
            "SELECT MAX(report_date) FROM macro_indicator WHERE indicator_code = ?",
            (code,)).fetchone()
        max_date = row[0] if row and row[0] else ""

        added = 0
        for _, r in df.iterrows():
            report_date = str(r["report_date"])
            if report_date <= max_date:  # 增量过滤（已存在的不重写）
                continue
            conn.execute(
                "INSERT OR REPLACE INTO macro_indicator"
                " (indicator_code, report_date, period_type, value, unit,"
                "  source, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (code, report_date, meta["period_type"], float(r["value"]),
                 meta["unit"], source, now,
                 json.dumps({"report_date": report_date, "value": float(r["value"])},
                            ensure_ascii=False)),
            )
            added += 1
        conn.commit()
    return added


def get_macro_series(indicator: str, periods: int = 12) -> list[dict]:
    """取最近 N 期时序（升序返回：旧 → 新）。"""
    with get_fundamental_db() as conn:
        rows = conn.execute(
            "SELECT indicator_code, report_date, period_type, value, unit"
            " FROM macro_indicator WHERE indicator_code = ?"
            " ORDER BY report_date DESC LIMIT ?",
            (indicator, max(1, periods))).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_latest_indicator(indicator: str) -> dict | None:
    """最新一期（供摘要/dashboard 指标卡）。"""
    with get_fundamental_db() as conn:
        row = conn.execute(
            "SELECT indicator_code, report_date, period_type, value, unit"
            " FROM macro_indicator WHERE indicator_code = ?"
            " ORDER BY report_date DESC LIMIT 1", (indicator,)).fetchone()
    return dict(row) if row else None
