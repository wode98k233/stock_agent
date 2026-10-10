"""清理规则引擎：每条规则 scan（dry-run 预览）与 apply（执行删除），独立 try/except 隔离。

内置规则 v1（复用现有清理脚本逻辑）：
- expired_cache_kv     market_cache.db 中 expire_at 过期记录
- small_logs           logs/ 下小于 log_min_lines 行的 .log
- orphan_dialogs       dialog 表引用不存在的日志文件
- stale_snapshots      data_monitor.db 超保留期快照
- stale_cache_files    cache/ 下超过 stale_cache_days 未修改文件
"""
import os
import sqlite3
from datetime import datetime

from utils.data_monitor import snapshot
from utils.data_monitor.config import load_config

_EST_ROW_BYTES = 128   # 过期缓存/孤儿记录预估行宽，用于"预计释放"估算


def get_market_cache_db_path():
    from utils.app_paths import get_market_cache_db_path as _f
    return _f()


def get_stock_radar_db_path():
    from utils.app_paths import get_db_path
    return get_db_path()


def get_logs_dir():
    from utils.app_paths import get_logs_dir as _f
    return _f()


def get_cache_dir():
    from utils.app_paths import get_jieba_cache_dir
    return get_jieba_cache_dir()


def _count_lines(fp: str) -> int:
    try:
        with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---------- 规则：market_cache 过期缓存 ----------
def _scan_expired_cache_kv(cfg):
    db = get_market_cache_db_path()
    items = []
    if not os.path.isfile(db):
        return items
    try:
        conn = sqlite3.connect(db)
        cnt = conn.execute("SELECT COUNT(*) FROM cache_kv WHERE expire_at IS NOT NULL AND expire_at < ?",
                           (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),)).fetchone()[0]
        conn.close()
        if cnt:
            items.append({'id': 'expired_cache_kv', 'detail': f'{cnt} 条过期记录', 'count': cnt,
                          'size': cnt * _EST_ROW_BYTES})
    except sqlite3.Error:
        pass
    return items


def _apply_expired_cache_kv():
    db = get_market_cache_db_path()
    if not os.path.isfile(db):
        return 0
    conn = sqlite3.connect(db)
    cur = conn.execute("DELETE FROM cache_kv WHERE expire_at IS NOT NULL AND expire_at < ?",
                       (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
    conn.commit()
    conn.close()
    return cur.rowcount


# ---------- 规则：小日志文件 ----------
def _scan_small_logs(cfg):
    items = []
    logs = get_logs_dir()
    if not os.path.isdir(logs):
        return items
    limit = cfg.get('log_min_lines', 200)
    for dirpath, _d, files in os.walk(logs):
        for f in files:
            if not f.endswith('.log'):
                continue
            fp = os.path.join(dirpath, f)
            if _count_lines(fp) < limit:
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    size = 0
                items.append({'id': 'small_logs', 'path': fp, 'detail': f'{f} 行数过少',
                              'count': 1, 'size': size})
    return items


def _apply_small_logs():
    removed = 0
    for item in _scan_small_logs(load_config()):
        try:
            os.remove(item['path'])
            removed += 1
        except OSError:
            pass
    return removed


# ---------- 规则：dialog 孤儿记录 ----------
def _scan_orphan_dialogs(cfg):
    items = []
    db = get_stock_radar_db_path()
    logs = get_logs_dir()
    if not (os.path.isfile(db) and os.path.isdir(logs)):
        return items
    try:
        conn = sqlite3.connect(db)
        rows = conn.execute('SELECT id, log_file FROM dialog').fetchall()
        conn.close()
        n = 0
        for _rid, log_file in rows:
            if not log_file:
                continue
            if os.path.isabs(log_file):
                if not os.path.isfile(log_file):
                    n += 1
            elif not os.path.isfile(os.path.join(logs, os.path.basename(log_file))):
                n += 1
        if n:
            items.append({'id': 'orphan_dialogs', 'detail': f'{n} 条孤儿记录', 'count': n,
                          'size': n * _EST_ROW_BYTES})
    except sqlite3.Error:
        pass
    return items


def _apply_orphan_dialogs():
    db = get_stock_radar_db_path()
    logs = get_logs_dir()
    if not (os.path.isfile(db) and os.path.isdir(logs)):
        return 0
    try:
        conn = sqlite3.connect(db)
        rows = conn.execute('SELECT id, log_file FROM dialog').fetchall()
        ids = []
        for rid, log_file in rows:
            if not log_file:
                continue
            if os.path.isabs(log_file):
                if not os.path.isfile(log_file):
                    ids.append(rid)
            elif not os.path.isfile(os.path.join(logs, os.path.basename(log_file))):
                ids.append(rid)
        for rid in ids:
            conn.execute('DELETE FROM dialog WHERE id = ?', (rid,))
        conn.commit()
        conn.close()
        return len(ids)
    except sqlite3.Error:
        return 0


# ---------- 规则：过期快照 ----------
def _scan_stale_snapshots(cfg):
    n = snapshot.prune_snapshots_peek(cfg.get('snapshot_retention_days', 90))
    if not n:
        return []
    return [{'id': 'stale_snapshots', 'detail': f'{n} 条过期快照', 'count': n, 'size': n * 1024}]


# ---------- 规则：过期缓存文件 ----------
def _scan_stale_cache_files(cfg):
    items = []
    cache_dir = get_cache_dir()
    if not os.path.isdir(cache_dir):
        return items
    days = cfg.get('stale_cache_days', 30)
    cutoff = datetime.now().timestamp() - days * 86400
    for f in os.listdir(cache_dir):
        fp = os.path.join(cache_dir, f)
        try:
            if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                items.append({'id': 'stale_cache_files', 'detail': f'{f}', 'count': 1,
                              'size': os.path.getsize(fp)})
        except OSError:
            continue
    return items


def _apply_stale_cache_files():
    removed = 0
    cfg = load_config()
    cache_dir = get_cache_dir()
    cutoff = datetime.now().timestamp() - cfg.get('stale_cache_days', 30) * 86400
    for f in os.listdir(cache_dir):
        fp = os.path.join(cache_dir, f)
        try:
            if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                removed += 1
        except OSError:
            pass
    return removed


# ---------- 规则注册表 ----------
_RULES = [
    {'id': 'expired_cache_kv', 'name': 'market_cache 过期缓存', 'desc': 'cache_kv 表 expire_at 已过期的记录',
     'tag': 'DB 记录', 'scan': _scan_expired_cache_kv, 'apply': _apply_expired_cache_kv},
    {'id': 'small_logs', 'name': '小日志文件', 'desc': 'logs/ 下少于 log_min_lines 行的日志文件',
     'tag': '文件', 'scan': _scan_small_logs, 'apply': _apply_small_logs},
    {'id': 'orphan_dialogs', 'name': 'dialog 孤儿记录', 'desc': '引用的日志文件已不存在的对话记录',
     'tag': 'DB 记录', 'scan': _scan_orphan_dialogs, 'apply': _apply_orphan_dialogs},
    {'id': 'stale_snapshots', 'name': '过期监控快照', 'desc': 'data_monitor.db 中超过保留期的历史快照',
     'tag': 'DB 记录', 'scan': _scan_stale_snapshots, 'apply': lambda: snapshot.prune_snapshots(
        load_config().get('snapshot_retention_days', 90))},
    {'id': 'stale_cache_files', 'name': '过期缓存文件', 'desc': 'cache/ 下超过 stale_cache_days 天未修改的临时文件',
     'tag': '文件', 'scan': _scan_stale_cache_files, 'apply': _apply_stale_cache_files},
]


def list_rules() -> list:
    return [{'id': r['id'], 'name': r['name'], 'desc': r.get('desc', ''), 'tag': r.get('tag', '')} for r in _RULES]


def run_rule_preview(rule_id: str, config: dict | None = None) -> dict:
    """dry-run：只扫描不删除。返回 {count, size, items}。"""
    cfg = config or load_config()
    rule = next((r for r in _RULES if r['id'] == rule_id), None)
    if not rule:
        return {'count': 0, 'size': 0, 'items': []}
    items = rule['scan'](cfg)
    return {'count': sum(i['count'] for i in items), 'size': sum(i['size'] for i in items), 'items': items}


def apply_rule(rule_id: str) -> dict:
    """执行单条规则，返回 {removed_count, freed_bytes}（freed 依据执行前 dry-run 估算）。"""
    rule = next((r for r in _RULES if r['id'] == rule_id), None)
    if not rule:
        return {'removed_count': 0, 'freed_bytes': 0}
    before = run_rule_preview(rule_id)
    removed = 0
    try:
        removed = rule['apply']()
    except Exception:  # noqa: BLE001
        pass
    return {'removed_count': removed, 'freed_bytes': before['size'] if removed else 0}