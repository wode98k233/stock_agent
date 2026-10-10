"""CLI 业务逻辑（db 路径可注入，便于测试）。

设计见 docs/arch/2026-09-09-统一DBSchema初始化-设计.md §5.3
"""
import sqlite3
import sys
from typing import Optional

from utils.db_schema.migrator import ChangeReport, diff_schema, ensure_schema
from utils.db_schema.registry import ALL_DBS, list_dbs


def run_init(only: Optional[list[str]] = None, apply: bool = True,
             paths: Optional[dict[str, str]] = None,
             verbose: bool = False) -> dict[str, ChangeReport]:
    """对指定库执行 check/apply。

    Args:
        only: 库 key 列表，None 表示全部。
        apply: True 写入（缺表建/缺列补/迁移/种子）；False 只读报告差异。
        paths: 覆盖 db 路径（测试注入用），key → 文件路径。
    """
    keys = only or list(ALL_DBS)
    results: dict[str, ChangeReport] = {}
    for key in keys:
        d = ALL_DBS[key]
        db_path = (paths or {}).get(key) or d.path_func()
        conn = sqlite3.connect(db_path)
        try:
            if apply:
                rep = ensure_schema(conn, d.tables, d.special_migrations)
                for seed in d.seeds:
                    seed(conn)
                conn.commit()
            else:
                rep = diff_schema(conn, d.tables)
            results[key] = rep
            if verbose and (rep.created or rep.added_columns):
                changed = list(rep.added_columns.items())
                print(f"[{key}] created={rep.created} added_cols={changed}")
        finally:
            conn.close()
    return results


def _has_diff(rep: ChangeReport) -> bool:
    return bool(rep.created or rep.added_columns)


def parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="一键对齐全部业务库表结构（加性幂等，不删数据）")
    p.add_argument("--check", action="store_true", help="只报告差异，不修改；有差异时退出码 1")
    p.add_argument("--db", default="", help="逗号分隔的库 key（如 backtest,stock_radar），默认全部")
    p.add_argument("--list", action="store_true", dest="show_list", help="列出全部注册库")
    p.add_argument("--verbose", action="store_true", help="打印每库变更明细")
    return p.parse_args(argv or sys.argv[1:])


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.show_list:
        for key, path, n in list_dbs():
            print(f"{key:<16} {path}  ({n} 表)")
        return 0

    only = [k.strip() for k in args.db.split(",") if k.strip()] or None
    results = run_init(only=only, apply=not args.check, verbose=args.verbose)

    dirty = [(k, r) for k, r in results.items() if _has_diff(r)]
    if dirty:
        for k, r in dirty:
            added = {t: cols for t, cols in r.added_columns.items()}
            print(f"差异 [{k}]: 将建表={r.created} 将补列={added}")
    else:
        print("无差异，全部库表结构已是最新")

    return 1 if (args.check and dirty) else 0