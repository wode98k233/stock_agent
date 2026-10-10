"""统一 DB Schema：DDL 单一事实源 + 通用 diff 引擎。

设计见 docs/arch/2026-09-09-统一DBSchema初始化-设计.md
"""
from utils.db_schema.migrator import ChangeReport, ensure_schema
from utils.db_schema.tables import TableDef

__all__ = ["ChangeReport", "ensure_schema", "TableDef"]