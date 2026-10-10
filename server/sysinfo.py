"""进程/系统运行状态采样（psutil 惰性导入，缺失时降级 available=False）。"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_START_TS = time.time()
_CACHE_TTL_S = 5.0

_proc = None
_sample_lock = threading.Lock()
_snapshot_cache = None  # (采样时间戳, 快照)，TTL 内直接复用，避免轮询抖动
_count_warned = False


def _counting_running_tasks() -> int:
    """数据采集运行中任务数（无任务库时降级 0，失败仅告警一次防轮询刷屏）。"""
    global _count_warned
    try:
        from utils.cache.backtest_db import get_data_tasks
        return len(get_data_tasks(status="running"))
    except Exception as e:
        if not _count_warned:
            _count_warned = True
            logger.warning("[overview] 采集任务计数失败，降级为 0: %s", e)
        return 0


def _uptime_display(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"


def _sample() -> dict:
    """实际采样一次进程与系统状态（由 build_runtime_stats 在锁内调度）。"""
    global _proc
    try:
        import psutil
    except Exception:
        return {"available": False}
    try:
        if _proc is None:
            _proc = psutil.Process(os.getpid())
            _proc.cpu_percent(None)  # 预热：首次无基线
        cpu = _proc.cpu_percent(None)
        mem_rss_mb = round(_proc.memory_info().rss / 1024 / 1024)
        uptime = int(time.time() - _START_TS)
        return {
            "available": True,
            "cpu_percent": round(cpu, 1),
            "mem_rss_mb": mem_rss_mb,
            "mem_sys_pct": round(psutil.virtual_memory().percent, 1),
            # threading.active_count()：进程内全部存活 Python 线程（含解释器/第三方后台线程），
            # 非纯本服务线程数
            "threads": threading.active_count(),
            "uptime_seconds": uptime,
            "uptime_display": _uptime_display(uptime),
            "tasks_running": _counting_running_tasks(),
        }
    except Exception as e:
        logger.warning("[overview] 运行状态采样失败: %s", e)
        return {"available": False}


def build_runtime_stats() -> dict:
    """获取运行状态快照：5 秒 TTL 缓存 + 锁，供 Web 多线程轮询安全调用。

    首次调用 cpu_percent 为基线预热（返回 0）。threads 口径为进程内全部存活
    Python 线程（threading.active_count），非纯本服务线程数。
    """
    global _snapshot_cache
    now = time.time()
    if _snapshot_cache is not None and now - _snapshot_cache[0] < _CACHE_TTL_S:
        return _snapshot_cache[1]
    with _sample_lock:
        if _snapshot_cache is not None and now - _snapshot_cache[0] < _CACHE_TTL_S:
            return _snapshot_cache[1]
        snapshot = _sample()
        _snapshot_cache = (time.time(), snapshot)
        return snapshot
