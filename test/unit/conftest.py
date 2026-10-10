"""
单元测试级 pytest fixtures
提供 mock 对象和通用测试工具
"""
import ipaddress
import os
import socket
import sys

import pytest
from unittest.mock import MagicMock, patch

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ── 网络闸门：unit 测试禁止访问外部网络 ──────────────────────────────
# `pyproject.toml` 已声明 `unit: 纯逻辑单元测试，不依赖网络和外部服务`，但此前没有机制约束：
# 实测 test_fetcher_stock_hist_falls_back_when_source_missing_close 会真连同花顺
# （429 后降级新浪），结果随外部状态漂移（本机绿、别处红）。
# 这里在 socket 层设闸：非 loopback 的连接一律失败，并给出修复指引。
# 急用时可 `SR_ALLOW_UNIT_NETWORK=1` 绕过（仅调试，勿提交依赖它的用例）。


_INPROCESS_HOSTS = {"testserver", "test", "testclient", "localhost", "localhost.localdomain"}


def _is_loopback(host) -> bool:
    if host is None:
        return True  # 无 host（相对路径/进程内）不放行也不算外网
    text = str(host).lower()
    if text in _INPROCESS_HOSTS:
        # Starlette/FastAPI TestClient 用 http://testserver 走 ASGI 进程内传输，
        # 不是可路由地址；httpx 的 MockTransport 同理。
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


# 审计：记录「哪个 unit 用例试图连外部网络」，便于一次性清理。
# `SR_NETWORK_AUDIT=1` 时会在 session 结束把清单写到 logs/unit_network_audit.txt。
_NETWORK_HITS: list = []
_NETWORK_STACKS: list = []
_CURRENT_TEST = {"nodeid": "<unknown>"}


def pytest_runtest_setup(item):
    _CURRENT_TEST["nodeid"] = item.nodeid


def pytest_sessionfinish(session, exitstatus):
    if os.getenv("SR_NETWORK_AUDIT") != "1" or not _NETWORK_HITS:
        return
    seen, lines = set(), []
    for nodeid, host in _NETWORK_HITS:
        if (nodeid, host) in seen:
            continue
        seen.add((nodeid, host))
        lines.append(f"{nodeid}\t{host}")
    path = os.path.join(PROJECT_ROOT, "logs", "unit_network_audit.txt")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n[network-audit] {len(lines)} 条「unit 试图连外部网络」记录 → {path}")
        if _NETWORK_STACKS:
            spath = os.path.join(PROJECT_ROOT, "logs", "unit_network_audit_stacks.txt")
            with open(spath, "w", encoding="utf-8") as f:
                for nodeid, host, stack in _NETWORK_STACKS[:5]:
                    f.write(f"=== {nodeid}  →  {host}\n{stack}\n\n")
            print(f"[network-audit] 调用栈样例 → {spath}")
    except Exception:
        pass


def _guard_address(addr):
    """addr 非 loopback 时记录并抛错。"""
    host = addr[0] if isinstance(addr, (tuple, list)) and addr else addr
    if _is_loopback(host):
        return
    _NETWORK_HITS.append((_CURRENT_TEST["nodeid"], str(host)))
    if os.getenv("SR_NETWORK_AUDIT") == "1":
        import traceback

        stack = "".join(traceback.format_stack()[-40:])
        _NETWORK_STACKS.append((_CURRENT_TEST["nodeid"], str(host), stack))
    raise RuntimeError(
        f"unit 测试禁止访问外部网络（目标 {host!r}）。"
        "请用 monkeypatch/mock 替掉该取数调用，或把用例移到 test/integration/ 并加 @pytest.mark.integration。"
        "（确需临时联网调试可设 SR_ALLOW_UNIT_NETWORK=1）"
    )


@pytest.fixture(autouse=True, scope="session")
def _block_external_network():
    if os.getenv("SR_ALLOW_UNIT_NETWORK") == "1":
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def _connect(self, address):
        _guard_address(address)
        return real_connect(self, address)

    def _connect_ex(self, address):
        _guard_address(address)
        return real_connect_ex(self, address)

    def _create_connection(address, *a, **kw):
        _guard_address(address)
        return real_create_connection(address, *a, **kw)

    # ── HTTP 层兜底 ──────────────────────────────────────────────
    # 只堵 socket 还不够：不同库的客户端构造路径不同（实测 openai → siliconflow
    # 走 utils/llm/backends/embed.py::_embed_openai，绕过了 gateway.embed；
    # 东财走 requests.post）。在 send 层再堵一道，与客户端实现无关。
    import urllib.parse

    def _host_of(url):
        try:
            return urllib.parse.urlparse(str(url)).hostname
        except Exception:
            return None

    patchers = []

    try:
        import requests

        _real_req_send = requests.Session.send

        def _req_send(self, request, **kw):
            _guard_address((_host_of(getattr(request, "url", "")),))
            return _real_req_send(self, request, **kw)

        patchers.append(patch.object(requests.Session, "send", _req_send))
    except Exception:
        pass

    try:
        import httpx

        def _is_inprocess_transport(transport) -> bool:
            """进程内传输（FastAPI TestClient 的 ASGI / WSGI）不走真实网络，放行。"""
            name = type(transport).__name__ if transport is not None else ""
            return any(k in name for k in ("ASGI", "WSGI", "Mock", "MockTransport"))

        _real_httpx_send = httpx.Client.send

        def _httpx_send(self, request, **kw):
            if _is_inprocess_transport(getattr(self, "_transport", None)):
                return _real_httpx_send(self, request, **kw)
            _guard_address((getattr(getattr(request, "url", None), "host", None),))
            return _real_httpx_send(self, request, **kw)

        patchers.append(patch.object(httpx.Client, "send", _httpx_send))

        _real_httpx_asend = httpx.AsyncClient.send

        async def _httpx_asend(self, request, **kw):
            if _is_inprocess_transport(getattr(self, "_transport", None)):
                return await _real_httpx_asend(self, request, **kw)
            _guard_address((getattr(getattr(request, "url", None), "host", None),))
            return await _real_httpx_asend(self, request, **kw)

        patchers.append(patch.object(httpx.AsyncClient, "send", _httpx_asend))
    except Exception:
        pass

    for p in patchers:
        p.start()
    try:
        with patch.object(socket.socket, "connect", _connect), \
                patch.object(socket.socket, "connect_ex", _connect_ex), \
                patch.object(socket, "create_connection", _create_connection):
            yield
    finally:
        for p in patchers:
            p.stop()


@pytest.fixture(autouse=True)
def _fast_fetcher_retry(monkeypatch):
    """把 fetcher 重试的指数退避压到 ~0。

    `tools/fetcher/base.py::retry` 默认 `base_delay=2.0, max_retries=5` → 2+4+8+16≈30s。
    用它的用例会白等真实时间：实测
    `test/unit/datasource/test_history_short_series_guards.py::test_ak_stock_hist_raises_on_single_row`
    单例 35.3s（占全量 unit 的 21%）。

    只包 `tools.fetcher.base` 模块内的 `time`（其它模块仍用真 time），
    避免影响依赖真实 sleep 的用例（如 async_bridge 的超时）。
    """
    try:
        from tools.fetcher import base as _fb
    except Exception:
        yield
        return
    real_time = _fb.time

    class _CappedTime:
        def __getattr__(self, name):
            return getattr(real_time, name)

        def sleep(self, seconds):
            # 保留一次线程让出，但不真的等待退避时长
            real_time.sleep(min(float(seconds), 0.001))

    monkeypatch.setattr(_fb, "time", _CappedTime(), raising=False)
    yield


@pytest.fixture(autouse=True, scope="session")
def _offline_llm_retrieval():
    """会话级：unit 全程 embedding / rerank 不联网（含**后台线程**）。

    实测链路（全量 unit 曾跑出 58 处联网、单例耗时 26s+）：
        agents/base.py::_enrich_user_input
          → memory sdk.retrieve → backend/hybrid.search → embedding.search
          → memory/sdk.py 的 _embed 闭包 → `utils.llm.gateway.embed(purpose=MEMORY)`
          → utils/llm/backends/embed.py::_embed_openai → api.siliconflow.cn
    这是 `_enrich_user_input` 的**自动副作用**（不是被测对象），却让大量无关用例
    （thesis / trace / scoring / watchlist …）隐式联网，既慢又随外部状态漂移。

    **必须是会话级**：后台预热线程（reranker-warm / memory reaper）会跨测试存活，
    函数级补丁在测试结束就被撤销，线程随后又会调真方法（实测残留 13 处散点命中）。

    只封 embedding 的**叶节点**（`_embed_openai`）与网关入口；`EmbeddingProvider.embed`
    的分发本身保留，`test_backends.py` 用实例级 monkeypatch 覆盖叶节点仍可测。
    """
    import unittest.mock as _mock

    def _offline(*a, **k):
        raise RuntimeError("unit 测试离线：embedding/rerank 不联网（记忆检索降级 FTS5-only）")

    patchers = []
    try:
        from utils.llm import gateway as _gw
        for _name in ("embed", "aembed", "rerank", "arerank"):
            if hasattr(_gw, _name):
                patchers.append(_mock.patch.object(_gw, _name, _offline))
    except Exception:
        pass
    try:
        from utils.llm.backends.embed import EmbeddingProvider as _EP
        for _name in ("_embed_openai",):
            if hasattr(_EP, _name):
                patchers.append(_mock.patch.object(_EP, _name, _offline))
    except Exception:
        pass

    for p in patchers:
        p.start()
    try:
        yield
    finally:
        for p in patchers:
            p.stop()


@pytest.fixture
def mock_llm():
    """Mock LLM 实例"""
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="测试响应")
    llm.ainvoke.return_value = MagicMock(content="测试响应")
    return llm


@pytest.fixture
def temp_dir(tmp_path):
    """自动清理的临时目录"""
    return tmp_path


@pytest.fixture
def mock_logger():
    """Mock logger 实例"""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.debug = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


@pytest.fixture
def mock_memory():
    """Mock memory 实例"""
    memory = MagicMock()
    memory.chat_memory = MagicMock()
    memory.chat_memory.messages = []
    memory.save_context = MagicMock()
    memory.load_memory_variables = MagicMock(return_value={"history": ""})
    return memory


@pytest.fixture
def mock_skill_registry():
    """Mock skill registry 实例"""
    registry = MagicMock()
    registry.get_tools = MagicMock(return_value=[])
    registry.execute_tool = MagicMock(return_value="工具执行结果")
    return registry


@pytest.fixture
def load_scenario_module():
    """
    加载 agents.scenarios 下的模块，避免 agents/__init__.py 重依赖
    用于 test_scenario_*.py 测试
    """
    import importlib.util

    def _load(module_name, relative_path):
        """
        加载指定模块

        Args:
            module_name: 模块名 (如 "common", "stock_analysis")
            relative_path: 相对于项目根目录的路径 (如 "agents/scenarios/common.py")
        """
        full_path = os.path.join(PROJECT_ROOT, relative_path)
        spec = importlib.util.spec_from_file_location(module_name, full_path)
        mod = importlib.util.module_from_spec(spec)

        # 预注册父包到 sys.modules 避免导入错误
        parts = relative_path.replace("/", ".").replace(".py", "").split(".")
        for i in range(len(parts) - 1):
            parent = ".".join(parts[:i+1])
            if parent not in sys.modules:
                sys.modules[parent] = MagicMock()

        spec.loader.exec_module(mod)
        return mod

    return _load


@pytest.fixture
def isolated_cache(tmp_path):
    """
    隔离的缓存数据库，避免测试间干扰
    """
    import sqlite3
    db_path = str(tmp_path / "test_cache.db")
    conn = sqlite3.connect(db_path)
    yield conn, db_path
    conn.close()
