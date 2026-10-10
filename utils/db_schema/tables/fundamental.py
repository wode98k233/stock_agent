"""fundamental_cache.db 表定义（4 表 + 5 索引）。

DDL 逐字复制自：
- utilities/cache/fundamental_db.py::init_fundamental_tables
- utils/cache/macro_db.py::_DDL（macro_indicator，归属于本库）
"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="financial_metric", ddl="""CREATE TABLE IF NOT EXISTS financial_metric (
        code TEXT NOT NULL,
        report_date TEXT NOT NULL,
        period_type TEXT,
        report_type TEXT,
        metric_code TEXT NOT NULL,
        metric_name TEXT,
        value REAL,
        unit TEXT,
        yoy REAL,
        mom REAL,
        source TEXT NOT NULL,
        published_at TEXT,
        updated_at TEXT,
        raw_json TEXT,
        PRIMARY KEY (code, report_date, period_type, metric_code, source)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_fin_metric_code_date ON financial_metric(code, report_date DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_fin_metric_name_date ON financial_metric(metric_code, report_date DESC)"]),

    TableDef(name="financial_core", ddl="""CREATE TABLE IF NOT EXISTS financial_core (
        code TEXT NOT NULL,
        report_date TEXT NOT NULL,
        eps REAL,
        eps_diluted REAL,
        bps REAL,
        revenue REAL,
        revenue_yoy REAL,
        net_profit REAL,
        net_profit_yoy REAL,
        deduct_net_profit REAL,
        roe REAL,
        roa REAL,
        gross_margin REAL,
        net_margin REAL,
        debt_ratio REAL,
        current_ratio REAL,
        quick_ratio REAL,
        operating_cash_flow_ps REAL,
        source TEXT,
        updated_at TEXT,
        raw_json TEXT,
        PRIMARY KEY (code, report_date, source)
    )"""),

    TableDef(name="valuation_daily", ddl="""CREATE TABLE IF NOT EXISTS valuation_daily (
        code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        pe_ttm REAL,
        pe_static REAL,
        pb REAL,
        ps_ttm REAL,
        pcf_ttm REAL,
        market_cap REAL,
        float_market_cap REAL,
        total_share REAL,
        float_share REAL,
        source TEXT,
        updated_at TEXT,
        raw_json TEXT,
        PRIMARY KEY (code, trade_date, source)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_valuation_date ON valuation_daily(trade_date)",
                      "CREATE INDEX IF NOT EXISTS idx_valuation_code_date ON valuation_daily(code, trade_date DESC)"]),

    TableDef(name="macro_indicator", ddl="""CREATE TABLE IF NOT EXISTS macro_indicator (
        indicator_code TEXT NOT NULL,
        report_date    TEXT NOT NULL,
        period_type    TEXT,
        value          REAL,
        unit           TEXT,
        yoy            REAL,
        mom            REAL,
        source         TEXT NOT NULL,
        updated_at     TEXT,
        raw_json       TEXT,
        PRIMARY KEY (indicator_code, report_date, source)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_macro_code_date ON macro_indicator(indicator_code, report_date DESC)"]),
]