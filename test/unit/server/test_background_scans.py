"""后台巡检框架测试：单飞、异常隔离、状态、调度不阻塞。

三条硬约束对应下面的测试：
1. `schedule()` 只创建 task 就返回 —— 启动不被巡检拖住（首字节时间敏感）
2. 同一作业不会并发跑两遍（手动触发撞上定时触发 → skipped，不排队）
3. 作业抛异常只落到状态与日志，绝不冒泡到调度器/请求，也不影响其它作业
"""
import asyncio
import threading
import time

import pytest

from server.background_scans import ScanJob, ScanManager, _compact


def _job(key="j1", **over):
    base = {"key": key, "name": "测试作业", "fn": lambda: {"summary": "完成"}}
    base.update(over)
    return ScanJob(**base)


async def _cancel_scan_tasks():
    """收干净常驻 task，避免 event loop 关闭时报「Task was destroyed but pending」。"""
    tasks = [t for t in asyncio.all_tasks() if t.get_name().startswith("scan-")]
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestCompact:
    def test_explicit_summary_wins(self):
        summary, metrics = _compact({"summary": "扫了 3 个", "assets": 3})
        assert summary == "扫了 3 个"
        assert metrics["assets"] == 3

    def test_scalars_become_summary_when_absent(self):
        summary, metrics = _compact({"assets": 3, "errors": 0})
        assert "assets=3" in summary
        assert metrics == {"assets": 3, "errors": 0}

    def test_none_and_non_dict_are_safe(self):
        assert _compact(None) == ("完成", {})
        assert _compact("裸字符串")[0] == "裸字符串"
        assert _compact(0) == ("0", {})

    def test_large_objects_are_not_kept_in_state(self):
        summary, metrics = _compact({"summary": "s", "items": [1] * 500, "n": 1})
        assert "items" not in metrics
        assert metrics == {"n": 1}

    def test_summary_is_truncated(self):
        summary, _ = _compact({"summary": "x" * 500})
        assert len(summary) == 300


class TestRegistration:
    def test_register_and_query(self):
        mgr = ScanManager()
        assert mgr.keys() == []
        mgr.register(_job("a"))
        mgr.register(_job("b"))
        assert mgr.keys() == ["a", "b"]
        assert mgr.has("a") is True
        assert mgr.has("zzz") is False

    def test_register_is_idempotent_and_keeps_state(self):
        mgr = ScanManager()
        mgr.register(_job("a"))
        mgr.run("a")
        mgr.register(_job("a", name="改名"))
        st = {j["key"]: j for j in mgr.status()["jobs"]}["a"]
        assert st["name"] == "改名"
        assert st["run_count"] == 1        # 重新注册不清历史状态

    def test_status_shape_before_any_run(self):
        mgr = ScanManager()
        mgr.register(_job("a", description="d", interval_minutes=5, first_delay_s=3))
        body = mgr.status()
        assert body["scheduled"] == []
        st = body["jobs"][0]
        assert st["key"] == "a" and st["enabled"] is True
        assert st["interval_minutes"] == 5 and st["first_delay_s"] == 3
        assert st["running"] is False and st["run_count"] == 0
        assert st["last_ok"] is None and st["last_summary"] is None
        assert st["last_metrics"] == {} and st["next_run_at"] is None


class TestRun:
    def test_successful_run_records_state(self):
        mgr = ScanManager()
        mgr.register(_job("a", fn=lambda: {"summary": "好", "n": 2}, interval_minutes=10))
        out = mgr.run("a", source="manual")
        assert out["started"] is True and out["skipped"] is None and out["ok"] is True
        assert out["summary"] == "好" and out["metrics"]["n"] == 2
        assert isinstance(out["duration_ms"], int)
        st = mgr.status()["jobs"][0]
        assert st["run_count"] == 1 and st["fail_count"] == 0
        assert st["last_ok"] is True and st["last_error"] is None
        assert st["last_source"] == "manual"
        assert st["last_started_at"] and st["last_finished_at"]
        assert st["running"] is False
        assert st["next_run_at"] is not None

    def test_exception_is_captured_not_raised(self):
        def boom():
            raise RuntimeError("上游挂了")

        mgr = ScanManager()
        mgr.register(_job("a", fn=boom))
        out = mgr.run("a")                      # 不抛
        assert out["ok"] is False
        assert "上游挂了" in out["error"]
        assert "RuntimeError" in out["error"]
        st = mgr.status()["jobs"][0]
        assert st["fail_count"] == 1 and st["last_ok"] is False
        assert "执行失败" in st["last_summary"]
        assert st["running"] is False           # 失败也要释放 running

    def test_failure_does_not_block_next_run(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("第一次失败")
            return {"summary": "第二次好"}

        mgr = ScanManager()
        mgr.register(_job("a", fn=flaky))
        assert mgr.run("a")["ok"] is False
        out = mgr.run("a")
        assert out["ok"] is True and out["summary"] == "第二次好"
        st = mgr.status()["jobs"][0]
        assert st["run_count"] == 2 and st["fail_count"] == 1

    def test_unknown_key_raises(self):
        mgr = ScanManager()
        mgr.register(_job("a"))
        with pytest.raises(KeyError) as ei:
            mgr.run("nope")
        assert "nope" in str(ei.value)

    def test_single_flight_second_trigger_is_skipped(self):
        """手动触发撞上正在跑的定时扫描 → 返回 skipped，不排队、不叠加。"""
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(5)
            return {"summary": "跑完了"}

        mgr = ScanManager()
        mgr.register(_job("a", fn=slow))
        t = threading.Thread(target=mgr.run, args=("a",), daemon=True)
        t.start()
        assert entered.wait(3), "作业没起来"

        assert mgr.is_running("a") is True
        out = mgr.run("a", source="manual")
        assert out["started"] is False and out["skipped"] == "running"
        assert out["ok"] is None
        assert "仍在进行中" in out["summary"]

        release.set()
        t.join(5)
        st = mgr.status()["jobs"][0]
        assert st["running"] is False
        assert st["run_count"] == 1             # 只跑了一次
        assert st["skip_count"] == 1

    def test_release_lock_after_exception_allows_retry(self):
        """异常路径必须释放单飞锁，否则一个坏作业会永远被自己卡住。"""
        def boom():
            raise RuntimeError("x")

        mgr = ScanManager()
        mgr.register(_job("a", fn=boom))
        mgr.run("a")
        out = mgr.run("a")
        assert out["started"] is True           # 没被 lock 拒掉


class TestSchedule:
    async def test_schedule_returns_immediately_and_defers_work(self):
        mgr = ScanManager()
        calls = []
        mgr.register(_job("slow", fn=lambda: calls.append(1), first_delay_s=999))
        t0 = time.time()
        keys = mgr.schedule(asyncio.get_running_loop())
        elapsed = time.time() - t0
        assert keys == ["slow"]
        assert elapsed < 0.5                    # 不 await、不 join
        assert mgr.status()["scheduled"] == ["slow"]
        await asyncio.sleep(0.05)
        assert calls == []                      # 首次延迟内绝不跑
        assert mgr.status()["jobs"][0]["running"] is False
        await _cancel_scan_tasks()

    async def test_schedule_only_once(self):
        mgr = ScanManager()
        mgr.register(_job("a", first_delay_s=999))
        loop = asyncio.get_running_loop()
        assert mgr.schedule(loop) == ["a"]
        assert mgr.schedule(loop) == ["a"]      # 幂等
        names = [t.get_name() for t in asyncio.all_tasks()
                 if t.get_name().startswith("scan-")]
        assert names == ["scan-a"]              # 没起第二个 task
        await _cancel_scan_tasks()

    async def test_disabled_job_is_not_scheduled(self):
        mgr = ScanManager()
        mgr.register(_job("on", first_delay_s=999))
        mgr.register(_job("off", first_delay_s=999, enabled=False))
        assert mgr.schedule(asyncio.get_running_loop()) == ["on"]
        assert mgr.status()["scheduled"] == ["on"]
        await _cancel_scan_tasks()

    async def test_empty_registry_schedules_nothing(self):
        mgr = ScanManager()
        assert mgr.schedule(asyncio.get_running_loop()) == []
        await _cancel_scan_tasks()

    async def test_loop_runs_job_after_first_delay(self):
        mgr = ScanManager()
        done = threading.Event()

        def fn():
            done.set()
            return {"summary": "ok"}

        mgr.register(_job("fast", fn=fn, first_delay_s=0, interval_minutes=60))
        mgr.schedule(asyncio.get_running_loop())
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.02)
        assert done.is_set(), "首次延迟后作业没跑"
        st = mgr.status()["jobs"][0]
        assert st["run_count"] == 1 and st["last_source"] == "scheduled"
        await _cancel_scan_tasks()

    async def test_loop_survives_per_round_error(self):
        """`run` 抛错（如作业未注册）不能让常驻循环死掉。"""
        mgr = ScanManager()
        job = _job("ghost", first_delay_s=0, interval_minutes=60)   # 故意不注册
        task = asyncio.get_running_loop().create_task(mgr._loop(job))
        await asyncio.sleep(0.1)
        assert not task.done(), "异常把循环打断了"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_cancel_is_not_swallowed(self):
        """取消必须能穿透 —— 否则进程关闭时 task 收不干净。"""
        mgr = ScanManager()
        mgr.register(_job("a", first_delay_s=999))
        mgr.schedule(asyncio.get_running_loop())
        tasks = [t for t in asyncio.all_tasks() if t.get_name() == "scan-a"]
        assert tasks
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]


class TestSingleton:
    def test_get_manager_is_process_wide(self):
        from server.background_scans import get_manager
        assert get_manager() is get_manager()
