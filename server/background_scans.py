"""后台巡检框架：启动后异步跑「台账刷新 / 存储监控扫描 / 拥挤度扫描 / 短线情绪预热」。

三条硬约束（本次需求的核心）：
1. **不阻塞启动** —— `schedule()` 只创建 asyncio task 就返回；真正的活儿丢进 `to_thread`。
   bootstrap 里注册时**不 await、不 join**，uvicorn 的首行日志与首字节时间不受影响。
2. **首次延迟错峰** —— 启动期路由导入 + skill/agent/memory 三个 warmup 都在抢 GIL，
   巡检若立刻开跑会把首字节拖慢。每个作业有自己的 `first_delay_s`（默认 45/90/150/180 秒）。
3. **单飞 + 异常隔离** —— 同一作业不会并发跑两遍（手动触发撞上定时触发时返回 `skipped`）；
   作业抛异常只落到状态与日志，绝不影响主服务，也不影响其它作业（各自的 task 独立）。

**状态只存内存**（进程级）：重启后由首次延迟重新跑一轮即可，不需要持久化。
真正的结果在各自领域库里（`thesis.db` / `data_monitor.db` / `crowding.db`），
状态只回答「上次跑没跑、跑成没跑成、多久、下一次什么时候」。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _now_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ScanJob:
    """一个可调度的巡检作业。fn 返回 dict（含可选 `summary` 文案）。"""

    key: str
    name: str
    fn: Callable[[], Any]
    interval_minutes: int = 1440
    first_delay_s: int = 120
    description: str = ""
    enabled: bool = True


def _compact(result: Any) -> tuple[str, dict]:
    """把作业返回值压成 (摘要文案, 小指标字典)——状态里不放大对象。"""
    if not isinstance(result, dict):
        return (str(result)[:200] if result is not None else "完成"), {}
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary:
        scalars = {k: v for k, v in result.items()
                   if isinstance(v, (int, float, str, bool)) and len(str(v)) <= 80}
        summary = "；".join(f"{k}={v}" for k, v in list(scalars.items())[:4]) or "完成"
    metrics = {k: v for k, v in result.items()
               if isinstance(v, (int, float, str, bool)) and k != "summary" and len(str(v)) <= 80}
    return summary[:300], metrics


class ScanManager:
    """巡检作业注册表 + 调度 + 状态。线程安全；不依赖 event loop（`schedule` 除外）。"""

    def __init__(self):
        self._jobs: "dict[str, ScanJob]" = {}
        self._state: "dict[str, dict]" = {}
        self._locks: "dict[str, threading.Lock]" = {}
        self._lock = threading.RLock()
        self._scheduled: list[str] = []

    # ── 注册 ─────────────────────────────────────────────────────

    def register(self, job: ScanJob) -> None:
        with self._lock:
            self._jobs[job.key] = job
            self._locks[job.key] = threading.Lock()
            self._state.setdefault(job.key, {
                "running": False, "run_count": 0, "fail_count": 0, "skip_count": 0,
                "last_started_at": None, "last_finished_at": None, "last_duration_ms": None,
                "last_ok": None, "last_error": None, "last_summary": None,
                "last_source": None, "last_metrics": {}, "next_run_at": None,
            })

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._jobs

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._jobs)

    def is_running(self, key: str) -> bool:
        """只读窥视：给「手动触发」用，避免把必然被单飞拦掉的请求送进线程池。"""
        with self._lock:
            st = self._state.get(key)
            return bool(st and st["running"])

    # ── 状态 ─────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            jobs = []
            for key, job in self._jobs.items():
                st = self._state[key]
                jobs.append({
                    "key": key, "name": job.name, "description": job.description,
                    "enabled": job.enabled, "interval_minutes": job.interval_minutes,
                    "first_delay_s": job.first_delay_s,
                    "running": st["running"], "run_count": st["run_count"],
                    "fail_count": st["fail_count"], "skip_count": st["skip_count"],
                    "last_started_at": st["last_started_at"],
                    "last_finished_at": st["last_finished_at"],
                    "last_duration_ms": st["last_duration_ms"],
                    "last_ok": st["last_ok"], "last_error": st["last_error"],
                    "last_summary": st["last_summary"], "last_source": st["last_source"],
                    "last_metrics": st["last_metrics"], "next_run_at": st["next_run_at"],
                })
            return {"jobs": jobs, "scheduled": list(self._scheduled)}

    # ── 执行 ─────────────────────────────────────────────────────

    def run(self, key: str, source: str = "manual") -> dict:
        """单飞执行一个作业（同步阻塞调用者）。正在跑时返回 skipped，不排队。"""
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                raise KeyError(f"未知巡检作业: {key}（可选：{sorted(self._jobs)}）")
            st = self._state[key]
            lock = self._locks[key]

        if not lock.acquire(blocking=False):
            with self._lock:
                st["skip_count"] += 1
            logger.info("[Scans] %s 已在运行，本次触发跳过（source=%s）", key, source)
            return {"key": key, "started": False, "skipped": "running", "ok": None,
                    "summary": "上一次扫描仍在进行中，已跳过本次触发"}

        started = time.time()
        try:
            with self._lock:
                st["running"] = True
                st["run_count"] += 1
                st["last_source"] = source
                st["last_started_at"] = _now_str(started)
            ok, error, summary, metrics = True, None, "完成", {}
            try:
                result = job.fn()
                summary, metrics = _compact(result)
            except Exception as e:  # noqa: BLE001 —— 作业异常绝不能冒泡到调度器/请求
                ok = False
                error = f"{type(e).__name__}: {e}"
                summary = "执行失败：" + error[:200]
                logger.warning("[Scans] %s 执行失败", key, exc_info=True)
            finished = time.time()
            with self._lock:
                st["last_ok"] = ok
                st["last_error"] = error
                st["last_summary"] = summary
                st["last_metrics"] = metrics
                st["last_finished_at"] = _now_str(finished)
                st["last_duration_ms"] = int((finished - started) * 1000)
                if not ok:
                    st["fail_count"] += 1
                st["next_run_at"] = _now_str(finished + max(60, job.interval_minutes * 60))
            logger.info("[Scans] %s 完成 ok=%s 用时 %.1fs（source=%s）",
                        key, ok, finished - started, source)
            return {"key": key, "started": True, "skipped": None, "ok": ok,
                    "error": error, "summary": summary,
                    "duration_ms": st["last_duration_ms"], "metrics": metrics}
        finally:
            with self._lock:
                st["running"] = False
            lock.release()

    # ── 调度 ─────────────────────────────────────────────────────

    def schedule(self, loop) -> list[str]:
        """为每个 enabled 作业创建一个常驻 asyncio task。幂等：只生效一次。"""
        with self._lock:
            if self._scheduled:
                return list(self._scheduled)
            started = []
            for key, job in self._jobs.items():
                if not job.enabled:
                    logger.info("[Scans] %s 已禁用，跳过调度", key)
                    continue
                loop.create_task(self._loop(job), name=f"scan-{key}")
                started.append(key)
            self._scheduled = started
        if started:
            logger.info("[Scans] 后台巡检已注册 %d 个作业（首次延迟 %s 秒）：%s",
                        len(started),
                        "/".join(str(self._jobs[k].first_delay_s) for k in started),
                        ", ".join(started))
        return started

    async def _loop(self, job: ScanJob) -> None:
        """单个作业的常驻循环：先等首次延迟，之后按间隔跑。任何异常都不终止循环。"""
        try:
            await asyncio.sleep(max(0, job.first_delay_s))
        except asyncio.CancelledError:
            raise
        interval = max(60, job.interval_minutes * 60)
        while True:
            try:
                await asyncio.to_thread(self.run, job.key, "scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("[Scans] %s 调度轮次异常（已忽略）", job.key, exc_info=True)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise


_manager: "ScanManager | None" = None
_manager_lock = threading.Lock()


def get_manager() -> ScanManager:
    """进程级单例。Web 侧与 CLI 共用一套状态。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ScanManager()
        return _manager
