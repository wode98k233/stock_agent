"""评测数据库 Schema 定义。

单一事实源在 utils/db_schema/tables/eval.py；本模块仅保留 get_ddl() 兼容旧调用方。
"""


def get_ddl() -> str:
    """返回完整 DDL（兼容）；真源为 utils/db_schema/tables/eval.py。"""
    from utils.db_schema.tables.eval import TABLES
    parts = []
    for td in TABLES:
        parts.extend([td.ddl] + (td.indexes or []))
    return "\n;\n".join(p.rstrip(";") for p in parts) + "\n;"