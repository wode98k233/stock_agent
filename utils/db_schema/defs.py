"""db_schema 数据定义（与 migrator/tables 隔离，避免循环导入）。"""
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class TableDef:
    name: str
    ddl: str
    indexes: Optional[list[str]] = field(default_factory=list)
    virtual: bool = False
    migrate: Optional[Callable | list[Callable]] = None  # 表级特殊迁移（可多个）