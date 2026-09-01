"""系统监控路由：/api/system/health, /api/system/monitor/events

性能设计：
  - DB 路径模块级缓存（路径不变，无需每次解析）
  - Windows 跳过 open_files（极慢，735ms+）
  - 线程池并行采集 psutil 各项，总耗时从串行 2.5s 降到 ~60ms
"""
import asyncio
import gc
import json
import logging
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic, time as _time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from config import Config
from server.deps import get_web_state

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

logger = logging.getLogger(__name__)
router = APIRouter()

_START_TIME = _time()
_PID = os.getpid()
_PROC = psutil.Process(_PID) if _PSUTIL_AVAILABLE else None
_IS_WINDOWS = sys.platform == "win32"

# 线程池：最多并行采集项 = 7（cpu/mem/disk/net/proc/files/gc）
_EXECUTOR = ThreadPoolExecutor(max_workers=7, thread_name_prefix="monitor")

# ── DB 路径缓存（进程生命周期不变） ──

_DB_ALIASES = {
    "get_db_path": "stock_radar",
    "get_checkpoint_db_path": "checkpoint",
    "get_market_data_db_path": "market_data",
    "get_fundamental_db_path": "fundamental",
    "get_market_cache_db_path": "market_cache",
    "get_backtest_db_path": "backtest",
}


def _resolve_db_paths():
    """模块加载时调用一次，之后全部走缓存。"""
    paths = {}
    for attr, alias in _DB_ALIASES.items():
        try:
            fn = getattr(Config, attr, None)
            if fn:
                p = fn()
                if p and os.path.exists(p):
                    paths[alias] = p
        except Exception:
            pass
    try:
        from utils.agent_trace.db import resolve_trace_db
        trace_p = resolve_trace_db()
        if trace_p and os.path.exists(trace_p):
            paths["trace"] = trace_p
    except Exception:
        pass
    return paths


_DB_PATHS: dict[str, str] = {}


def _get_db_paths_cached():
    if not _DB_PATHS:
        _DB_PATHS.update(_resolve_db_paths())
    return _DB_PATHS


# ── 并行采集 ──

def _collect_cpu() -> dict:
    try:
        return {"percent": psutil.cpu_percent(interval=0.02), "count": psutil.cpu_count()}
    except Exception:
        return {"error": "采集失败"}


def _collect_memory() -> dict:
    try:
        mem = psutil.virtual_memory()
        return {
            "total_gb": round(mem.total / (1024 ** 3), 1),
            "available_gb": round(mem.available / (1024 ** 3), 1),
            "percent": mem.percent,
        }
    except Exception:
        return {"error": "采集失败"}


def _collect_process() -> dict:
    try:
        with _PROC.oneshot():
            pmem = _PROC.memory_info()
            return {
                "pid": _PID,
                "rss_mb": round(pmem.rss / (1024 ** 2), 1),
                "vms_mb": round(pmem.vms / (1024 ** 2), 1),
                "threads": _PROC.num_threads(),
                "cpu_percent": round(_PROC.cpu_percent(), 1),
            }
    except Exception:
        return {"pid": _PID, "error": "采集失败"}


def _collect_disk() -> dict:
    try:
        app_dir = os.getcwd()
        disk = psutil.disk_usage(app_dir)
        return {
            "path": app_dir,
            "total_gb": round(disk.total / (1024 ** 3), 1),
            "used_gb": round(disk.used / (1024 ** 3), 1),
            "free_gb": round(disk.free / (1024 ** 3), 1),
            "percent": disk.percent,
        }
    except Exception:
        return {"error": "采集失败"}


def _collect_network() -> dict:
    try:
        nio = psutil.net_io_counters()
        return {
            "sent_mb": round(nio.bytes_sent / (1024 ** 2), 1),
            "recv_mb": round(nio.bytes_recv / (1024 ** 2), 1),
            "packets_sent": nio.packets_sent,
            "packets_recv": nio.packets_recv,
        }
    except Exception:
        return {"error": "采集失败"}


def _collect_open_files() -> dict:
    # Windows 上 Process.open_files() 极慢（700ms+），跳过
    if _IS_WINDOWS:
        return {"count": -1, "_note": "Windows 不支持"}
    try:
        return {"count": len(_PROC.open_files())}
    except Exception:
        return {"error": "采集失败"}


def _collect_db_stats() -> dict:
    """数据库大小 + 连接状态。路径已缓存，这里只做文件检查和连接测试。"""
    db_paths = _get_db_paths_cached()
    total_size = 0
    dbs = []
    for name, path in sorted(db_paths.items()):
        try:
            size = os.path.getsize(path)
            total_size += size
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            conn.execute("SELECT 1")
            conn.close()
            status = "ok"
        except Exception:
            size = 0
            status = "error"
        dbs.append({
            "name": name,
            "path": path,
            "size_mb": round(size / (1024 * 1024), 2),
            "status": status,
        })
    return {
        "databases": dbs,
        "total_mb": round(total_size / (1024 * 1024), 2),
        "count": len(dbs),
    }


def _collect_gc() -> dict:
    try:
        counts = gc.get_count()
        stats = []
        for i, gen_stats in enumerate(gc.get_stats()):
            stats.append({
                "generation": i,
                "collections": gen_stats.get("collections", 0),
                "collected": gen_stats.get("collected", 0),
                "uncollectable": gen_stats.get("uncollectable", 0),
            })
        return {
            "enabled": gc.isenabled(),
            "thresholds": list(gc.get_threshold()),
            "counts": list(counts),
            "generations": stats,
        }
    except Exception:
        return {"error": "采集失败"}


def _get_active_tasks(web) -> dict:
    """从 TaskRuntime 获取当前活跃任务数。"""
    try:
        runtime = getattr(web, "runtime", None)
        if runtime:
            tasks = getattr(runtime, "_tasks", {})
            total = len(tasks)
            running = sum(1 for t in tasks.values() if getattr(t, "status", "") == "running")
            queued = sum(1 for t in tasks.values() if getattr(t, "status", "") == "queued")
            failed = sum(1 for t in tasks.values() if getattr(t, "status", "") == "failed")
            return {
                "total": total,
                "running": running,
                "queued": queued,
                "failed": failed,
            }
    except Exception:
        pass
    return {"total": 0, "running": 0, "queued": 0, "failed": 0}


def _collect_snapshot(web=None) -> dict:
    """并行采集系统快照。总耗时 = 最慢的单项（~60ms）而非累加。"""
    snapshot = {
        "timestamp": _time(),
        "uptime_seconds": round(_time() - _START_TIME, 1),
    }

    if _PSUTIL_AVAILABLE and _PROC:
        # 并行提交所有采集任务（复用模块级线程池）
        futures = {}
        for key, fn in [
            ("cpu", _collect_cpu),
            ("memory", _collect_memory),
            ("process", _collect_process),
            ("disk", _collect_disk),
            ("network", _collect_network),
            ("open_files", _collect_open_files),
            ("db", _collect_db_stats),
            ("gc", _collect_gc),
        ]:
            futures[_EXECUTOR.submit(fn)] = key

        for future in as_completed(futures):
            key = futures[future]
            try:
                snapshot[key] = future.result()
            except Exception:
                snapshot[key] = {"error": "采集失败"}
    else:
        snapshot["_note"] = "psutil 未安装，仅基础信息可用"
        snapshot["cpu"] = {}
        snapshot["memory"] = {}
        snapshot["disk"] = {}
        snapshot["network"] = {}
        snapshot["open_files"] = {}
        snapshot["process"] = {"pid": _PID}
        snapshot["db"] = _collect_db_stats()
        snapshot["gc"] = _collect_gc()

    # 应用层指标（无 psutil 依赖，直接读 TaskRuntime）
    if web:
        snapshot["app"] = _get_active_tasks(web)
    else:
        snapshot["app"] = {}

    return snapshot


@router.get("/api/system/health")
async def health(request: Request):
    """即时系统健康快照（并行采集，<100ms）。"""
    web = get_web_state(request)
    return await asyncio.to_thread(_collect_snapshot, web=web)


@router.get("/api/system/monitor/events")
async def monitor_events(request: Request):
    """SSE 持续监控流。

    客户端断开后自动停止采集，不留后台任务。
    """
    web = get_web_state(request)
    interval = max(1, min(10, getattr(Config, "MONITOR_INTERVAL", 2)))

    async def event_stream():
        try:
            while True:
                snapshot = await asyncio.to_thread(_collect_snapshot, web=web)
                body = json.dumps(snapshot, ensure_ascii=False, default=str)
                yield f"event: snapshot\ndata: {body}\n\n"
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
