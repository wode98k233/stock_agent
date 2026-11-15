"""Baostock 数据源并发/可达性降级回归测试。

背景（2026-10-18）：baostock 用**模块级全局 socket**（非线程安全），且 connect
不设 timeout；服务器不可达时每次调用白等 ~21s、把未连接 socket 写进全局 context
→ `WinError 10057` +「接收数据异常」，并在库内 print 刷屏。

⚠️ 本文件是 **单元测试**，绝不联网：
   唯一网络出口是 `baostock_ds._tcp_probe`，下方 `_no_real_network` autouse fixture
   默认把它替换成"调用即失败"，任何用例都必须显式打桩该接缝；若哪天真连了，
   会以 AssertionError 形式立刻暴露，而不是在 CI 里静默超时。

覆盖五条护栏：
  1. 预检失败 → 不调用 login（零 print）、置冷却、is_available() 返回 False
  2. 冷却期内重复调用 → 不再发起 TCP 探测
  3. 并发调用 → 全程串行（max_concurrent == 1），login/logout 成对
  4. login 失败 → 绝不 logout（否则会往未连接 socket 发送）
  5. login/logout 的 print 被吞掉；全局 socket 被设置 timeout
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools.fetcher import baostock_ds
from tools.fetcher.baostock_ds import BaostockDataSource


class _FakeSock:
    def __init__(self):
        self.timeout = None
        self.closed = False

    def close(self):
        self.closed = True

    def settimeout(self, t):
        self.timeout = t


class _FakeLogin:
    def __init__(self, error_code="0", error_msg=""):
        self.error_code = error_code
        self.error_msg = error_msg


class _FakeBS:
    """可观测的 baostock 替身：记录 login/logout 次数与最大并发度。"""

    def __init__(self, login_ok=True, login_delay=0.0, print_io=False):
        self.login_ok = login_ok
        self.login_delay = login_delay
        self.print_io = print_io
        self.logins = 0
        self.logouts = 0
        self._inflight = 0
        self.max_inflight = 0
        self._guard = threading.Lock()

    def login(self):
        with self._guard:
            self.logins += 1
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        if self.print_io:
            print("login success!")
        if self.login_delay:
            time.sleep(self.login_delay)
        return _FakeLogin() if self.login_ok else _FakeLogin("10001", "登录失败")

    def logout(self):
        with self._guard:
            self.logouts += 1
            self._inflight -= 1
        if self.print_io:
            print("logout success!")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """兜底断网：未显式打桩 `_tcp_probe` 就调用 → 立即失败（绝不真连）。"""

    def _boom(*a, **k):
        raise AssertionError("单元测试不允许真实网络连接：请打桩 baostock_ds._tcp_probe")

    monkeypatch.setattr(baostock_ds, "_tcp_probe", _boom)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例重置类级状态，避免冷却/依赖串扰。"""
    monkeypatch.setattr(BaostockDataSource, "_unreachable_until", 0.0)
    monkeypatch.setattr(BaostockDataSource, "_bs", None)
    yield


def _ok_probe(host, port, timeout):
    """连通性正常（不建真连接）。"""


def test_autouse_guard_blocks_real_probe():
    """元测试：确认断网护栏本身生效——未打桩就直接调探针会立刻失败。"""
    with pytest.raises(AssertionError, match="不允许真实网络"):
        baostock_ds._tcp_probe("www.baostock.com", 10030, 0.1)


def test_preflight_failure_fails_fast_without_login(monkeypatch):
    bs = _FakeBS(login_ok=True)
    monkeypatch.setattr(BaostockDataSource, "_bs", bs)

    def _refuse(*a, **k):
        raise ConnectionRefusedError(10061, "拒绝连接")

    monkeypatch.setattr(baostock_ds, "_tcp_probe", _refuse)

    with pytest.raises(RuntimeError, match="服务器不可达"):
        BaostockDataSource._with_connection(lambda b: "should-not-run")

    assert bs.logins == 0  # 预检失败 → 根本不 login（零 print、零 socket 泄漏）
    assert BaostockDataSource.is_available() is False  # 冷却期内不参与源排序


def test_cooldown_skips_repeated_probe(monkeypatch):
    bs = _FakeBS()
    monkeypatch.setattr(BaostockDataSource, "_bs", bs)
    probes = {"n": 0}

    def _refuse(*a, **k):
        probes["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(baostock_ds, "_tcp_probe", _refuse)

    with pytest.raises(RuntimeError):
        BaostockDataSource._with_connection(lambda b: None)
    assert probes["n"] == 1

    # 冷却期内再调 → 立即失败，不再探测
    with pytest.raises(RuntimeError, match="冷却中"):
        BaostockDataSource._with_connection(lambda b: None)
    assert probes["n"] == 1
    assert bs.logins == 0


def test_concurrent_calls_are_serialized(monkeypatch):
    bs = _FakeBS(login_delay=0.03)
    monkeypatch.setattr(BaostockDataSource, "_bs", bs)
    monkeypatch.setattr(baostock_ds, "_tcp_probe", _ok_probe)

    def _query(_bs):
        time.sleep(0.02)
        return "ok"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: BaostockDataSource._with_connection(_query), range(4)))

    assert results == ["ok"] * 4
    assert bs.max_inflight == 1  # 全局 socket 绝不被并发踩踏
    assert bs.logins == 4 and bs.logouts == 4  # 成对


def test_login_failure_does_not_logout(monkeypatch):
    bs = _FakeBS(login_ok=False)
    monkeypatch.setattr(BaostockDataSource, "_bs", bs)
    monkeypatch.setattr(baostock_ds, "_tcp_probe", _ok_probe)

    with pytest.raises(RuntimeError, match="登录失败"):
        BaostockDataSource._with_connection(lambda b: "nope")

    assert bs.logins == 1
    assert bs.logouts == 0  # 登录失败不 logout（否则 10057 二次噪音）


def test_library_prints_suppressed_and_socket_timeout_applied(monkeypatch, capsys):
    bs_ctx = pytest.importorskip("baostock.common.context")  # 只 import，不联网

    fake_sock = _FakeSock()
    monkeypatch.setattr(bs_ctx, "default_socket", fake_sock, raising=False)
    bs = _FakeBS(print_io=True)
    monkeypatch.setattr(BaostockDataSource, "_bs", bs)
    monkeypatch.setattr(baostock_ds, "_tcp_probe", _ok_probe)

    assert BaostockDataSource._with_connection(lambda b: "ok") == "ok"

    captured = capsys.readouterr()
    assert "login success" not in captured.out
    assert "logout success" not in captured.out
    assert fake_sock.timeout == BaostockDataSource._socket_timeout


def test_lock_timeout_skips_instead_of_queueing(monkeypatch):
    bs = _FakeBS()
    monkeypatch.setattr(BaostockDataSource, "_bs", bs)
    monkeypatch.setattr(BaostockDataSource, "_lock_timeout", 0.15)
    monkeypatch.setattr(baostock_ds, "_tcp_probe", _ok_probe)

    holder_ready = threading.Event()
    release = threading.Event()

    def _hold():
        BaostockDataSource._lock.acquire()
        holder_ready.set()
        release.wait(2)
        BaostockDataSource._lock.release()

    th = threading.Thread(target=_hold, daemon=True)
    th.start()
    assert holder_ready.wait(1)

    t0 = time.perf_counter()
    with pytest.raises(RuntimeError, match="尚未结束"):
        BaostockDataSource._with_connection(lambda b: "never")
    elapsed = time.perf_counter() - t0

    release.set()
    th.join(2)
    assert elapsed < 1.0  # 不排队堆死请求，快速让位给下一个数据源
    assert bs.logins == 0
