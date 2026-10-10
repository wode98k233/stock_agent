"""数据资产动态发现：固定扫描根 + 配置扩展 + 排除规则，不硬编码资产清单。

在 `app_dir`（默认 get_app_dir()）下按规则分类：
- db 文件（*.db/*.sqlite/*.sqlite3）各自独立为资产（data_monitor.db 自身被排除）
- db/ 目录的一级子目录（分表目录，如 agent_trace/）向下再探一层收集 db 文件
- 已知数据目录（logs/cache/chroma_data/data/user_skills）整目录为一个资产
- 未知目录向下寻找 db 文件；extra_scan_dirs / extra_files 追加扫描
- exclude_patterns 全局排除（glob 匹配绝对路径）
"""
import fnmatch
import os

_DB_EXTS = ('.db', '.sqlite', '.sqlite3')
_KNOWN_DIRS = {   # 目录名 -> 分类
    'logs': 'log', 'cache': 'cache', 'chroma_data': 'vector',
    'data': 'data', 'user_skills': 'skill',
}


class Asset:
    """一个待统计的数据资产（文件或目录）。"""

    def __init__(self, key: str, category: str, name: str, path: str, is_dir: bool = False):
        self.key = key
        self.category = category
        self.name = name
        self.path = path
        self.is_dir = is_dir


def get_app_dir() -> str:
    from utils.app_paths import get_app_dir as _g
    return _g()


def _excluded(path: str, cfg: dict) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in cfg.get('exclude_patterns', []))


def discover(config: dict | None = None, app_dir: str | None = None) -> list:
    """发现全部数据资产。app_dir 缺省取 get_app_dir()（测试可显式传入）。"""
    cfg = config or {}
    root = app_dir or get_app_dir()
    assets = []
    seen_paths = set()
    seen_keys = set()

    def _add(a: Asset):
        # 双层去重：path 防同文件重复发现；key 防不同路径同名文件
        # （如根目录游离的 xx.db 与 db/xx.db 共享 key 'db/xx.db'，
        #  快照表 snapshot_items 以 asset_key 唯一，撞 key 会炸落库）。
        if a.path in seen_paths or a.key in seen_keys:
            return
        seen_paths.add(a.path)
        seen_keys.add(a.key)
        assets.append(a)

    def _add_prefer_db_dir(a: Asset):
        """key 冲突时优先保留 db/ 规范目录下的资产（根目录游离副本让位）。"""
        if a.key in seen_keys:
            existing = next(x for x in assets if x.key == a.key)
            if os.path.dirname(a.path) == os.path.join(get_app_dir(), 'db') \
                    and os.path.dirname(existing.path) != os.path.dirname(a.path):
                seen_paths.discard(existing.path)
                assets.remove(existing)
                _add(a)
            return
        _add(a)

    def _file_asset(path: str):
        name = os.path.basename(path)
        if name in ('data_monitor.db', 'data_monitor_config.json'):
            return None
        if name.endswith(_DB_EXTS):
            return Asset(f'db/{name}', 'db', name, path)
        if name.endswith('.json'):
            return Asset(f'json/{name}', 'json', name, path)
        return None

    def _scan_dir(dir_path: str):
        if not os.path.isdir(dir_path):
            return
        for entry in sorted(os.listdir(dir_path)):
            p = os.path.join(dir_path, entry)
            if entry.startswith('.') or entry == '__pycache__' or _excluded(p, cfg):
                continue
            if os.path.isfile(p):
                a = _file_asset(p)
                if a:
                    _add(a)
            elif os.path.isdir(p):
                if entry == 'db':
                    for f in sorted(os.listdir(p)):
                        fp = os.path.join(p, f)
                        if f.startswith('.') or _excluded(fp, cfg):
                            continue
                        if os.path.isdir(fp):
                            # 分表子目录（如 db/agent_trace/xx.db）：向下探一层收 db 文件
                            for g in sorted(os.listdir(fp)):
                                gp = os.path.join(fp, g)
                                if not (g.endswith(_DB_EXTS) and not g.startswith('.')):
                                    continue
                                if g == 'data_monitor.db' or _excluded(gp, cfg):
                                    continue
                                _add(Asset(f'db/{f}/{g}', 'db', f'{f}/{g}', gp))
                        if not (f.endswith(_DB_EXTS) and not f.startswith('.')):
                            continue
                        if f == 'data_monitor.db' or _excluded(fp, cfg):
                            continue
                        _add_prefer_db_dir(Asset(f'db/{f}', 'db', f, fp))
                elif entry in _KNOWN_DIRS:
                    _add(Asset(f'{_KNOWN_DIRS[entry]}/{entry}', _KNOWN_DIRS[entry], entry + '/', p, is_dir=True))
                # 未知目录不递归：仅统计已知数据目录 + db 文件 + 根散落 json，
                # 其余（代码/资源/临时目录）不属于运行数据，避免误扫与权限问题；
                # 自定义数据目录可通过 extra_scan_dirs 加入。
                else:
                    continue

    _scan_dir(root)
    for d in cfg.get('extra_scan_dirs', []):
        _scan_dir(d)
    for f in cfg.get('extra_files', []):
        if os.path.isfile(f):
            a = _file_asset(f)
            if a:
                _add(a)
    assets.sort(key=lambda a: (a.category, a.name))
    return assets