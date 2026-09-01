"""fundamental_cache.db 连接管理和表初始化

职责：
- financial_metric: 财务指标（长表）
- financial_core: 财务核心指标（宽表）
- valuation_daily: 估值日线
"""
from utils.cache.db_utils import make_db_context
from config import Config

get_fundamental_db = make_db_context(lambda: Config.get_fundamental_db_path())


def init_fundamental_tables():
    """初始化 fundamental_cache.db 表结构"""
    with get_fundamental_db() as conn:
        c = conn.cursor()

        # 财务指标（长表）
        c.execute('''CREATE TABLE IF NOT EXISTS financial_metric (
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
        )''')

        # 财务核心指标（宽表）
        c.execute('''CREATE TABLE IF NOT EXISTS financial_core (
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
        )''')

        # 估值日线
        c.execute('''CREATE TABLE IF NOT EXISTS valuation_daily (
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
        )''')

        # 创建索引
        c.execute('CREATE INDEX IF NOT EXISTS idx_fin_metric_code_date ON financial_metric(code, report_date DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_fin_metric_name_date ON financial_metric(metric_code, report_date DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_valuation_date ON valuation_daily(trade_date)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_valuation_code_date ON valuation_daily(code, trade_date DESC)')
