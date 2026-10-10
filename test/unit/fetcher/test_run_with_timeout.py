"""超时执行器回归：超时必须**立即返回**，不得 join 挂死线程。

事故（2026-09-23）：`_run_with_timeout` 用 `with ThreadPoolExecutor()`，退出时
`shutdown(wait=True)` join 挂死的 akshare 线程 → 请求卡死（get_stock_history weekly）。
"""
import time

import pytest

from tools.fetcher.base import run_with_timeout


def test_returns_value():
    assert run_with_timeout(lambda: 42, 1) == 42


def test_raises_timeout_without_blocking():
    t0 = time.time()
    with pytest.raises(TimeoutError):
        run_with_timeout(lambda: time.sleep(5), 0.2)
    assert time.time() - t0 < 2, "超时后不应等待挂死线程"


def test_propagates_exception():
    def boom():
        raise ValueError("x")
    with pytest.raises(ValueError):
        run_with_timeout(boom, 1)


def test_akshare_run_with_timeout_raises_runtimeerror_promptly():
    from tools.fetcher import akshare_ds
    t0 = time.time()
    with pytest.raises(RuntimeError):
        akshare_ds._run_with_timeout(lambda: time.sleep(5), timeout=0.2)
    assert time.time() - t0 < 2


def test_run_with_timeout_propagates_contextvar():
    """跨线程执行必须保留 ContextVar（否则 _current_source / _get_source 取不到）。"""
    import contextvars
    from tools.fetcher.base import run_with_timeout

    var = contextvars.ContextVar("t_prop", default="none")
    tok = var.set("hello")
    try:
        assert run_with_timeout(lambda: var.get(), 1) == "hello"
    finally:
        var.reset(tok)


def test_retry_times_out_hanging_source_and_switches(monkeypatch):
    """某个源挂死时，retry 循环应在其超时后换下一个源，而不是无限等待。"""
    import pandas as pd
    import tools.fetcher.base as base
    from tools.fetcher.base import DataSourceManager

    monkeypatch.setattr(base.Config, "FETCH_SOURCE_TIMEOUT", 0.3, raising=False)
    monkeypatch.setattr(base, "_validate_retry_result", lambda name, r: r)
    monkeypatch.setattr("tools.fetcher._ensure_initialized", lambda: None)

    calls = []

    class _Hang:
        name = "hang"
        def _enforce_rate_limit(self):
            pass
        def get_stock_hist(self, *a, **k):
            calls.append("hang"); time.sleep(5); return pd.DataFrame()

    class _OK:
        name = "ok"
        def _enforce_rate_limit(self):
            pass
        def get_stock_hist(self, *a, **k):
            calls.append("ok"); return pd.DataFrame({"x": [1]})

    srcs = [_Hang(), _OK()]
    monkeypatch.setattr(
        DataSourceManager, "get_available_sources",
        classmethod(lambda cls, capability=None: list(srcs)))

    @base.retry(capability="hist")
    def ak_stock_hist(symbol, period="daily", start="", end=""):
        return base._current_source.get().get_stock_hist(symbol)

    t0 = time.time()
    ak_stock_hist("X")
    assert "hang" in calls and "ok" in calls
    assert time.time() - t0 < 3, "应在挂死源超时后换源，而非无限等待"

