"""一键初始化/对齐全部业务 SQLite 库表结构。

设计见 docs/arch/2026-09-09-统一DBSchema初始化-设计.md

用法：
    python scripts/ops/init_dbs.py                # 全库 apply（缺表建/缺列补，加性幂等）
    python scripts/ops/init_dbs.py --check        # 只报告差异（有差异退出码 1）
    python scripts/ops/init_dbs.py --db backtest,stock_radar
    python scripts/ops/init_dbs.py --list
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utils.db_schema.cli_core import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())