"""AgentFactory 并发安全测试。

验证：并发首调用只触发一次实例化（import 重依赖），
其余线程等待后取同一缓存实例，绝不重复 import 导致竞态。
详见 agents/factory.py AgentFactory.get 的锁 + 在途事件实现，
以及 server/deps.py require_agent_ready 的 await 改造。
"""
import threading
import time

from agents.factory import AgentFactory


_UNIQUE = "__threadsafe_test_agent__"


def _register_slow_factory():
    calls = {"n": 0}
    lock = threading.Lock()

    def slow_factory():
        with lock:
            calls["n"] += 1
        time.sleep(0.5)  # 模拟 import 重依赖

        class _A:
            name = _UNIQUE

        return _A()

    AgentFactory._factories[_UNIQUE] = slow_factory
    AgentFactory._instances.pop(_UNIQUE, None)
    AgentFactory._ready.pop(_UNIQUE, None)
    return calls


def _cleanup():
    AgentFactory._factories.pop(_UNIQUE, None)
    AgentFactory._instances.pop(_UNIQUE, None)
    AgentFactory._ready.pop(_UNIQUE, None)


def test_concurrent_first_call_imports_once():
    calls = _register_slow_factory()
    try:
        results = []
        threads = [threading.Thread(target=lambda: results.append(AgentFactory.get(_UNIQUE)))
                   for _ in range(8)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dt = time.time() - t0

        # 只 import 一次（无重复 import 竞态）
        assert calls["n"] == 1, f"import 调用了 {calls['n']} 次，应为 1"
        # 所有线程拿到同一实例
        assert len({id(r) for r in results}) == 1
        # 并发总耗时约等于单次 import 时间（在途等待），远小于 8*0.5
        assert dt < 3.0, f"并发耗时 {dt:.2f}s 异常（疑似重复 import）"
    finally:
        _cleanup()


def test_cached_get_no_lock_contention():
    """已缓存实例无锁直接返回，不阻塞。"""
    _register_slow_factory()
    try:
        inst1 = AgentFactory.get(_UNIQUE)  # 首次：触发 import
        t0 = time.time()
        inst2 = AgentFactory.get(_UNIQUE)  # 二次：应无锁秒回
        assert inst2 is inst1
        assert time.time() - t0 < 0.1
    finally:
        _cleanup()
