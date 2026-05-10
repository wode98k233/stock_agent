"""
日志文件及数据库表清理脚本

1. 日志文件清理：删除行数少于 200 行的日志文件
2. 数据库同步：删除 dialog 表中引用的日志文件已不存在的记录
3. 缓存表清理：清理所有 cache_* 表中的过期数据
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utils.app_paths import get_logs_dir, get_db_path

MIN_LOG_LINES = 200


def _count_lines(filepath: str) -> int:
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def clean_small_logs():
    """删除行数少于 MIN_LOG_LINES 的日志文件"""
    logs_dir = get_logs_dir()
    if not os.path.isdir(logs_dir):
        print(f"  日志目录不存在: {logs_dir}")
        return []

    deleted_files = []
    total_freed = 0

    for dirpath, dirnames, filenames in os.walk(logs_dir):
        for fname in filenames:
            if not fname.endswith('.log'):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                line_count = _count_lines(fpath)
                if line_count < MIN_LOG_LINES:
                    size = os.path.getsize(fpath)
                    os.remove(fpath)
                    rel = os.path.relpath(fpath, logs_dir)
                    deleted_files.append(fpath)
                    total_freed += size
                    print(f"  删除小日志: {rel} ({line_count} 行, {_fmt_size(size)})")
            except Exception as e:
                rel = os.path.relpath(fpath, logs_dir)
                print(f"  删除失败: {rel} - {e}")

    print(f"  日志清理: 删除 {len(deleted_files)} 个文件, 释放 {_fmt_size(total_freed)}")
    return deleted_files


def clean_orphan_dialog_records():
    """清理 dialog 表中日志文件已不存在的记录"""
    db_path = get_db_path()
    if not os.path.isfile(db_path):
        print(f"  数据库不存在: {db_path}")
        return 0

    logs_dir = get_logs_dir()
    removed = 0

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, dialog_uuid, log_file FROM dialog").fetchall()

        for row in rows:
            log_file = row['log_file']
            if not log_file:
                continue

            if not os.path.isabs(log_file):
                abs_log = os.path.join(logs_dir, os.path.basename(log_file))
            else:
                abs_log = log_file

            if not os.path.isfile(abs_log):
                try:
                    conn.execute("DELETE FROM dialog WHERE id = ?", (row['id'],))
                    removed += 1
                    print(f"  清理孤儿记录: uuid={row['dialog_uuid'][:8]}... log={os.path.basename(log_file)}")
                except Exception as e:
                    print(f"  删除记录失败: id={row['id']} - {e}")

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  数据库操作失败: {e}")

    print(f"  dialog 表清理: 删除 {removed} 条孤儿记录")
    return removed


def clean_expired_cache():
    """清理所有 cache_* 表中的过期数据"""
    db_path = get_db_path()
    if not os.path.isfile(db_path):
        print(f"  数据库不存在: {db_path}")
        return 0

    cache_tables = [
        'cache_stock_history',
        'cache_news',
        'cache_rating',
        'cache_financial',
        'cache_board_list',
    ]
    total_removed = 0

    try:
        conn = sqlite3.connect(db_path)
        for table in cache_tables:
            try:
                count_before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                conn.execute(f"""
                    DELETE FROM {table}
                    WHERE datetime(updated_at, '+' || expire_hours || ' hours') < datetime('now', 'localtime')
                """)
                count_after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                deleted = count_before - count_after
                if deleted > 0:
                    print(f"  {table}: 清理 {deleted} 条过期记录 (剩余 {count_after})")
                total_removed += deleted
            except sqlite3.OperationalError:
                pass

        # cache_realtime: 清理非当天的记录
        try:
            from datetime import datetime
            today = datetime.now().strftime('%Y-%m-%d')
            count_before = conn.execute("SELECT COUNT(*) FROM cache_realtime").fetchone()[0]
            conn.execute("DELETE FROM cache_realtime WHERE trade_date != ?", (today,))
            count_after = conn.execute("SELECT COUNT(*) FROM cache_realtime").fetchone()[0]
            deleted = count_before - count_after
            if deleted > 0:
                print(f"  cache_realtime: 清理 {deleted} 条非今日记录 (剩余 {count_after})")
            total_removed += deleted
        except sqlite3.OperationalError:
            pass

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  数据库操作失败: {e}")

    print(f"  缓存表清理: 共清理 {total_removed} 条过期记录")
    return total_removed


def clean_dialogs_with_small_logs():
    """删除 dialog 表中对应日志文件行数少于 MIN_LOG_LINES 的记录（再删文件）"""
    db_path = get_db_path()
    if not os.path.isfile(db_path):
        return

    logs_dir = get_logs_dir()

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, dialog_uuid, log_file FROM dialog").fetchall()

        removed = 0
        for row in rows:
            log_file = row['log_file']
            if not log_file:
                continue

            if not os.path.isabs(log_file):
                abs_log = os.path.join(logs_dir, os.path.basename(log_file))
            else:
                abs_log = log_file

            if not os.path.isfile(abs_log):
                continue

            line_count = _count_lines(abs_log)
            if line_count < MIN_LOG_LINES:
                try:
                    conn.execute("DELETE FROM dialog WHERE id = ?", (row['id'],))
                    os.remove(abs_log)
                    removed += 1
                    print(f"  同步清理: uuid={row['dialog_uuid'][:8]}... ({line_count} 行)")
                except Exception as e:
                    print(f"  同步清理失败: uuid={row['dialog_uuid'][:8]}... - {e}")

        conn.commit()
        conn.close()
        if removed > 0:
            print(f"  同步清理: 删除 {removed} 条小日志记录及对应文件")
    except Exception as e:
        print(f"  数据库操作失败: {e}")


if __name__ == "__main__":
    print("=" * 50)
    print("日志文件及数据库清理")
    print("=" * 50)

    print("\n[1/4] 清理小日志文件 + 同步 dialog 记录")
    clean_dialogs_with_small_logs()

    print("\n[2/4] 清理剩余小日志文件")
    clean_small_logs()

    print("\n[3/4] 清理 dialog 表中孤儿记录")
    clean_orphan_dialog_records()

    print("\n[4/4] 清理过期缓存数据")
    clean_expired_cache()

    print("\n" + "=" * 50)
    print("清理完成")
    print("=" * 50)
