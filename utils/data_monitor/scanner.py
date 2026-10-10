"""资产统计：文件类聚合大小/数量/mtime；db 类动态取全部表逐表 COUNT + 时间列探测。

并发（ThreadPoolExecutor）按资产粒度执行；单表超时降级为 skipped。
"""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import closing
from datetime import datetime

from utils.data_monitor.discovery import Asset

_TABLE_TIMEOUT = 5.0
# 表级统计的 worker 数。每个 worker **各自持有独立只读连接**（见 _scan_db 的并发纪律注释）
_TABLE_WORKERS = 4
_TIME_COLS = (
    'updated_at', 'update_time', 'updated', 'created_at', 'create_time',
    'create_date', 'trade_date', 'as_of_date', 'insert_time', 'end_date', 'date',
)


class AssetStat:
    def __init__(self, asset: Asset):
        self.key = asset.key
        self.category = asset.category
        self.name = asset.name
        self.path = asset.path
        self.size_bytes = 0
        self.file_count = 0
        self.record_count = 0
        self.mtime = None
        self.tables = None    # db 才有
        self.meta = None
        self.error = None


def _fmt_mtime(ts: float) -> str | None:
    try:
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
    except (OSError, ValueError):
        return None


def _scan_dir(asset: Asset, stat: AssetStat) -> AssetStat:
    total = 0
    n = 0
    latest = 0.0

    def _onerror(_e: OSError):
        pass  # 忽略无权限子目录/文件，其余照常统计

    for dirpath, dirnames, filenames in os.walk(asset.path, onerror=_onerror):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for f in filenames:
            if f.startswith('.'):
                continue
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
                latest = max(latest, os.path.getmtime(fp))
                n += 1
            except OSError:
                continue
    stat.size_bytes = total
    stat.file_count = n
    stat.mtime = _fmt_mtime(latest)
    return stat


def _open_ro(path: str) -> sqlite3.Connection:
    """打开只读连接（每条连接只归创建它的线程使用）。

    用 `utils.sqlite_guard.connect` 而不是裸 `sqlite3.connect`：
    顺手挡住「`del conn.row_factory` → 下次查询以 NULL 为可调用对象 → 原生崩溃」这个 CPython 坑。
    **不传 `check_same_thread=False`**——默认严格模式会在「连接被别的线程使用」时直接抛
    `ProgrammingError`，把这类误用变成可捕获的 Python 异常，而不是进程静默消失。
    """
    from utils import sqlite_guard
    conn = sqlite_guard.connect(
        f'file:{path}?mode=ro', uri=True, timeout=2.0, row_factory=sqlite3.Row,
    )
    return conn


def _scan_db(asset: Asset, stat: AssetStat) -> AssetStat:
    r"""统计单个 db 文件：表清单 + 每表行数/最新时间。

    ⚠️ 并发纪律（2026-09-21 崩溃修复）
    ---------------------------------
    原实现是：**一个只读连接 + `ThreadPoolExecutor(4)` 里 4 个 worker 共用它**，
    并且 `executor.shutdown(wait=False)` 之后**立刻 `conn.close()`**。
    `check_same_thread=False` 只关掉 Python 层校验，**不会让连接线程安全**：

    - 4 个线程在同一连接上并发 `execute/fetchall` 会互相踩；
    - `shutdown(wait=False)` 不等超时的 worker；
    - 紧接着的 `conn.close()` 就落在「worker 仍在取行」的窗口里
      —— 取行时要回调 `row_factory`，此时连接/语句已被释放 → 空指针调用 → 原生访问违例。

    线上实证：`logs/native_crash.log`（faulthandler）里崩溃线程的第一帧就是
    `scanner.py:_table_stat`（即本函数内 ThreadPoolExecutor 的 worker），
    CrashDumps 里 dump 的目标地址恒为 `0x8`（对 NULL 取 `ob_type`）
    = "调用了一个 NULL 可调用对象"。

    现在：**每个 worker 线程自己开连接、在自己线程内用 `closing()` 关**，
    不再有任何跨线程共享的连接；超时的 worker 继续跑也只是用自己的连接，安全。
    """
    try:
        stat.size_bytes = os.path.getsize(asset.path)
        stat.mtime = _fmt_mtime(os.path.getmtime(asset.path))

        with closing(_open_ro(asset.path)) as conn:
            rows = [r['name'] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()]

        def _table_stat(name: str) -> dict:
            # 每次调用都用自己的连接（调用线程 = 使用线程 = 关闭线程）
            try:
                with closing(_open_ro(asset.path)) as tconn:
                    count = tconn.execute(f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()['c']
                    latest = None
                    cols = [r['name'] for r in tconn.execute(f'PRAGMA table_info("{name}")').fetchall()]
                    tcol = next((c for c in cols if c in _TIME_COLS), None)
                    if tcol:
                        val = tconn.execute(f'SELECT MAX("{tcol}") AS m FROM "{name}"').fetchone()['m']
                        latest = val if val is not None else None
                    return {'name': name, 'count': count, 'latest': latest, 'status': 'ok'}
            except Exception:  # noqa: BLE001
                return {'name': name, 'count': None, 'latest': None, 'status': 'skipped'}

        executor = ThreadPoolExecutor(max_workers=_TABLE_WORKERS)
        try:
            futures = {executor.submit(_table_stat, name): name for name in rows}
            tables = []
            for fut in futures:
                try:
                    t = fut.result(timeout=_TABLE_TIMEOUT)
                except FuturesTimeout:
                    t = {'name': futures[fut], 'count': None, 'latest': None, 'status': 'skipped'}
                tables.append(t)
        finally:
            # wait=False 保留「超时不阻塞整体扫描」的语义；
            # 因为已无共享连接，未完成的 worker 继续跑不会伤到别人（各自管自己的连接）。
            executor.shutdown(wait=False)

        tables.sort(key=lambda t: -(t['count'] or 0))
        stat.tables = tables
        stat.record_count = sum(t['count'] or 0 for t in tables)
    except Exception as e:  # noqa: BLE001
        stat.error = str(e)
        stat.tables = None
        stat.record_count = 0
    return stat


def scan(assets: list) -> list:
    """并发扫描资产列表，返回 AssetStat 列表（顺序与输入一致）。"""
    stats = []

    def _one(asset: Asset) -> AssetStat:
        stat = AssetStat(asset)
        if asset.category == 'db' and not asset.is_dir:
            return _scan_db(asset, stat)
        return _scan_dir(asset, stat)

    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2) * 2)) as pool:
        for s in pool.map(_one, assets):
            stats.append(s)
    return stats