"""数据监控聚合入口

scan()          -> 全量扫描（发现 → 统计 → 组合报告 → 落快照）
overview()      -> 最近一次扫描结果
trend()         -> 趋势序列
cleanup_preview()/cleanup_run() -> 清理预览与执行
供 CLI 与 Web 复用。
"""
from .config import load_config, ensure_config_file
from .discovery import discover
from .scanner import scan as _scan_assets
from . import snapshot
from . import cleanup

__all__ = ['scan', 'overview', 'trend', 'cleanup_preview', 'cleanup_run']


def scan() -> dict:
    """执行一次完整扫描并落快照，返回报告。"""
    ensure_config_file()
    cfg = load_config()
    assets = discover(cfg)
    stats = _scan_assets(assets)
    stats.sort(key=lambda s: -(s.size_bytes or 0))  # 按占用大小降序，保证明细/快照排序一致
    report = {
        'scanned_at': None,
        'assets': [
            {
                'key': s.key, 'category': s.category, 'name': s.name, 'path': s.path,
                'size_bytes': s.size_bytes, 'file_count': s.file_count,
                'record_count': s.record_count, 'mtime': s.mtime,
                'tables': s.tables, 'meta': s.meta, 'error': s.error,
            }
            for s in stats
        ],
    }
    from datetime import datetime
    report['scanned_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    snapshot.save_snapshot(report)
    return report


def overview() -> dict:
    """最近一次扫描结果（未扫描过则返回空 assets）。"""
    return snapshot.latest_report()


def trend(days: int = 30) -> list:
    return snapshot.get_trend(days)


def cleanup_preview() -> list:
    """全部规则 dry-run，返回 [{id, name, desc, tag, count, size, items}]。"""
    out = []
    for rule in cleanup.list_rules():
        p = cleanup.run_rule_preview(rule['id'])
        out.append({**rule, 'count': p['count'], 'size': p['size'], 'items': p['items']})
    return out


def cleanup_run(rule_ids: list) -> dict:
    """执行指定规则，返回 {id: {removed_count, freed_bytes}}。"""
    results = {}
    for rid in rule_ids:
        results[rid] = cleanup.apply_rule(rid)
    return results