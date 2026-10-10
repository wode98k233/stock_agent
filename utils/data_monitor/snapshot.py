"""历史快照（data_monitor.db）存取 + 趋势查询 + 保留期清理。

去重策略：快照级去重 —— 本次报告与最近一次快照逐资产完全一致则跳过，
避免快照膨胀；趋势按天取当天最后一次有效快照聚合。
"""
import json
import sqlite3
import time
from datetime import datetime

def _db_path() -> str:
    from utils.app_paths import get_data_monitor_db_path
    return get_data_monitor_db_path()


def _connect():
    """连接 + 幂等建表（data_monitor 组，统一 schema）。"""
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    d = ALL_DBS["data_monitor"]
    ensure_schema(conn, d.tables, d.special_migrations)
    return conn


def _last_snapshot(conn) -> dict | None:
    row = conn.execute('SELECT id, created_at, created_ts FROM snapshots ORDER BY created_ts DESC LIMIT 1').fetchone()
    return dict(row) if row else None


def _gv(a, name, default=None):
    """兼容 dict 与对象两种资产表示。"""
    if isinstance(a, dict):
        return a.get(name, default)
    return getattr(a, name, default)


def save_snapshot(report: dict) -> int:
    """落一份快照（快照级去重），返回写入的资产条目数。"""
    conn = _connect()
    assets = report.get('assets', [])
    prev = _last_snapshot(conn)
    if prev:
        prev_items = {r['asset_key']: r for r in conn.execute(
            'SELECT asset_key, size_bytes, record_count, category FROM snapshot_items WHERE snapshot_id = ?',
            (prev['id'],)).fetchall()}
        if len(prev_items) == len(assets):
            same = True
            for a in assets:
                p = prev_items.get(_gv(a, 'key'))
                if not p or p['size_bytes'] != (_gv(a, 'size_bytes') or 0) \
                        or p['record_count'] != (_gv(a, 'record_count') or 0) \
                        or p['category'] != _gv(a, 'category'):
                    same = False
                    break
            if same:
                conn.close()
                return 0  # 与最近一次完全相同，跳过

    now_ts = time.time()
    now_str = datetime.fromtimestamp(now_ts).strftime('%Y-%m-%d %H:%M:%S')
    cur = conn.execute('INSERT INTO snapshots (created_at, created_ts) VALUES (?, ?)', (now_str, now_ts))
    sid = cur.lastrowid
    written = 0
    for a in assets:
        detail = None
        tables = _gv(a, 'tables', None)
        if tables:
            detail = json.dumps(tables, ensure_ascii=False)
        # OR IGNORE 防御：同 key 撞车时跳过该资产而非让整次扫描 500
        cur2 = conn.execute(
            'INSERT OR IGNORE INTO snapshot_items (snapshot_id, asset_key, category, name, size_bytes, file_count, record_count, detail_json) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (sid, _gv(a, 'key'), _gv(a, 'category'), _gv(a, 'name'),
             _gv(a, 'size_bytes') or 0, _gv(a, 'file_count') or 0, _gv(a, 'record_count') or 0, detail))
        written += cur2.rowcount if cur2.rowcount and cur2.rowcount > 0 else 0
    conn.commit()
    conn.close()
    return written


def get_trend(days: int = 30) -> list:
    """按天 × 分类聚合大小/记录数，同一天取最后一次扫描（ROW_NUMBER 窗口）。"""
    conn = _connect()
    since = time.time() - days * 86400
    rows = conn.execute(
        """
        SELECT d, category, SUM(size_bytes) AS size_bytes, SUM(record_count) AS record_count FROM (
            SELECT date(s.created_at) AS d, s.id AS sid,
                   ROW_NUMBER() OVER (PARTITION BY date(s.created_at) ORDER BY s.created_ts DESC) AS rn
            FROM snapshots s WHERE s.created_ts >= ?
        ) x JOIN snapshot_items i ON i.snapshot_id = x.sid
        WHERE x.rn = 1
        GROUP BY d, category ORDER BY d
        """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_report() -> dict:
    """最近一次完整报告（资产列表 + 表级明细），未扫描过返回空。"""
    conn = _connect()
    snap = _last_snapshot(conn)
    if not snap:
        conn.close()
        return {'scanned_at': None, 'assets': []}
    rows = conn.execute(
        'SELECT * FROM snapshot_items WHERE snapshot_id = ? ORDER BY size_bytes DESC', (snap['id'],)).fetchall()
    conn.close()
    assets = []
    for r in rows:
        assets.append({
            'key': r['asset_key'], 'category': r['category'], 'name': r['name'],
            'size_bytes': r['size_bytes'], 'file_count': r['file_count'],
            'record_count': r['record_count'],
            'tables': json.loads(r['detail_json']) if r['detail_json'] else None,
        })
    return {'scanned_at': snap['created_at'], 'assets': assets}


def prune_snapshots_peek(retention_days: int) -> int:
    """只统计超保留期的快照数（dry-run 用），不删除。"""
    conn = _connect()
    cutoff = time.time() - retention_days * 86400
    n = conn.execute('SELECT COUNT(*) FROM snapshots WHERE created_ts < ?', (cutoff,)).fetchone()[0]
    conn.close()
    return n


def prune_snapshots(retention_days: int) -> int:
    """删除超过保留期的快照（连带 items），返回删除的 snapshot 数。"""
    conn = _connect()
    cutoff = time.time() - retention_days * 86400
    cur = conn.execute('DELETE FROM snapshots WHERE created_ts < ?', (cutoff,))
    conn.execute('DELETE FROM snapshot_items WHERE snapshot_id NOT IN (SELECT id FROM snapshots)')
    conn.commit()
    n = cur.rowcount
    conn.close()
    return max(n, 0)