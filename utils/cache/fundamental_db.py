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
    """初始化 fundamental_cache.db 表结构（DDL 统一来源 utils/db_schema）"""
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    with get_fundamental_db() as conn:
        ensure_schema(conn, ALL_DBS["fundamental"].tables)
