"""
单元测试级 pytest fixtures
提供 mock 对象和通用测试工具
"""
import ipaddress
import os
import socket
import sys
import types

import pytest

# ── 导入加速：让 langchain_core 的可选 transformers 依赖快速失败（2026-10-16）──
#
# 背景：`langchain_core/language_models/base.py:41` 有一段**可选**依赖：
#     try:    from transformers import GPT2TokenizerFast     # 为 token 计数兜底
#     except ImportError: _HAS_TRANSFORMERS = False
# 本环境装了 transformers（记忆系统要用），于是这个 try 走了"成功"路径，
# 而它能拖进 `transformers` + `torch` + `sentence_transformers` ≈ **9~10 秒**。
#
# 代价有多大：`import langchain_core.language_models.chat_models` 实测 11.85s，
# 其中 9.7s 是这个可选导入。而它**每个进程都要付一次** —— 单条测试 15.6s、
# `pytest test/unit/agents --collect-only` 25s；xdist 下 8 个 worker 各付一次，
# 加 worker 完全不提速（实测 -n 4 与 -n 8 都是 ~117s，-n 16 反而 197s）。
# 拦截后：-n 8 从 116s → 81s，且并行度恢复（-n 8 明显快于 -n 4）。
#
# 为什么在单测里拦掉是安全的：
#   · 全仓 unit 测试**没有任何一处**真的用 transformers / GPT2TokenizerFast /
#     sentence_transformers（grep 可复验）；
#   · ML 路径（embedding / rerank）本来就被 `_offline_llm_retrieval` 断网拦截；
#   · 这只是把"可选的、单测用不到的"依赖变回它设计时的失败分支，**不改变任何
#     被测逻辑** —— langchain_core 自己就支持 `_HAS_TRANSFORMERS = False`。
#
# ⚠️ **安装时机很关键，不能放模块级**：`pytest test/` 会在收集阶段加载**所有**
# conftest，模块级安装会污染同一进程里的 e2e / integration 用例（它们要真跑 ML）。
# 因此挪进 `pytest_configure`，并只在「本次会话**只**跑 unit」时才装。
# 需要真跑 ML 时设 `SR_ALLOW_UNIT_REAL_ML=1` 跳过。


def pytest_configure(config):
    if os.getenv("SR_ALLOW_UNIT_REAL_ML") == "1":
        return
    if "transformers" in sys.modules:
        return
    args = [str(a).replace("\\", "/") for a in (getattr(config, "args", None) or [])]
    # 无参（走 rootdir 收集，可能含 e2e）或混跑 → 不拦截，宁可慢也不破坏 e2e
    if not args or not all("test/unit" in a for a in args):
        return
    # 空模块：`from transformers import X` 会抛 ImportError（正是我们要的分支），
    # 而不是让 langchain_core 真的去加载 9 秒。
    sys.modules["transformers"] = types.ModuleType("transformers")


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


# ── LLM 闸门：unit 测试禁止真实调用大模型 ──────────────────────────────
# 与网络闸门配套。只堵 socket 时，llm_factory 的重试链会把「Connection error」
# 当成瞬时错误 → 11 次指数退避 ≈ 140s：实测
# test/unit/backtest/test_strategy_research.py::TestRedLineAndBest::test_step_marks_overfit_and_low_sample
# 单例 142.20s（用例还「通过」——意图核对失败后 fail-open），并会让 pytest -n 4 整套卡死。
# 这里在「调用层」直接抛错：只堵 invoke/_generate，不堵构造，避免误伤只构造不调用的用例。
# 急用时可 SR_ALLOW_UNIT_LLM=1 绕过（仅调试，勿提交依赖它的用例）。

_LLM_CALL_METHODS = ("invoke", "ainvoke", "stream", "astream", "_generate", "_agenerate")


_LLM_GUARD_INSTALLED = False


def _install_llm_guard() -> None:
    """给 ChatOpenAI 的调用方法装闸门（幂等）。

    ⚠️ **不能在这里 `from langchain_openai import ChatOpenAI`** —— 实测导入它要
    **10.9 秒**：它会顺带拉进 `transformers`（3.5s）+ `torch`（1.5s）+ azure 适配层。
    而本 fixture 曾经是 session 级 autouse，于是**每个 xdist worker 启动都白付
    10.7 秒**，哪怕那批测试根本不碰 LLM（`-n 8` 下实测 setup 占了单 worker 的有效时间）。

    代价对比：改成「已导入才装」后，不碰 LLM 的 worker 直接跳过（只花一次 dict 查）；
    碰 LLM 的 worker 本来就会导入 langchain_openai（`utils/llm_factory` 是模块级导入），
    这里只是复用已导入的类，**既不增加成本、也不放过任何真实调用**。
    """
    global _LLM_GUARD_INSTALLED
    if _LLM_GUARD_INSTALLED:
        return
    try:
        from langchain_openai import ChatOpenAI
    except Exception:
        return

    def _guard(name):
        def _raise(self, *a, **kw):
            raise RuntimeError(
                f"unit 测试禁止真实调用 LLM（{type(self).__name__}.{name}）。"
                "请 monkeypatch 掉该调用（如 mock backtest.intent_fidelity.check_intent、"
                "utils.llm_factory.llm_json_with_retry），或把用例移到 test/integration|e2e 并加标记。"
                "（确需临时真调可设 SR_ALLOW_UNIT_LLM=1）"
            )
        return _raise

    for n in _LLM_CALL_METHODS:
        if hasattr(ChatOpenAI, n):
            patch.object(ChatOpenAI, n, _guard(n)).start()
    _LLM_GUARD_INSTALLED = True


@pytest.fixture(autouse=True)
def _block_real_llm():
    """函数级 autouse：**只做一次 sys.modules 查表**，不主动导入。

    真正的补丁在 langchain_openai 出现后才装（见 _install_llm_guard 的说明）。
    本 codebase 的 LLM 入口 `utils/llm_factory` 是模块级导入 langchain_openai，
    所以任何「能调到 LLM」的用例在执行前该模块必然已在 sys.modules 里 —— 闸门不漏。
    """
    if os.getenv("SR_ALLOW_UNIT_LLM") == "1":
        return
    if "langchain_openai" in sys.modules:
        _install_llm_guard()

# ── 运行期状态隔离：unit 测试不碰真实对话库 / 真实日志目录 ─────────────
# utils/logger.py::get_logger 会向真实对话库 INSERT dialog 行，并在真实
# logs/<日期>/ 下落对话日志文件。xdist 并发时 4 个 worker 争抢同一 SQLite，
# 实测出现 sqlite3.OperationalError: database is locked（utils/logger.py:843）；
# 同时真实日志目录会被测试产物污染（历史上 test_clean_requires_confirm 还
# 误删过真实日志文件）。这里把对话库与日志目录整体改到临时目录。
# 急用时可 SR_ALLOW_UNIT_REAL_STATE=1 绕过（仅调试，勿提交依赖它的用例）。


@pytest.fixture(autouse=True, scope="session")
def _isolate_runtime_state(tmp_path_factory):
    if os.getenv("SR_ALLOW_UNIT_REAL_STATE") == "1":
        yield
        return
    import sqlite3
    import config as config_mod
    from utils.db_schema import tables as schema_tables
    from utils.db_schema.migrator import ensure_schema

    tmp = tmp_path_factory.mktemp("unit_state")
    logs_dir = tmp / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp / "stock_radar.db"
    # 用统一 DDL 源建表（AGENTS.md：DDL 单一事实源 utils/db_schema/tables/），
    # 保证 get_logger 的 INSERT INTO dialog 有表可写。
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn, schema_tables.stock_radar.TABLES)
    finally:
        conn.close()

    patchers = [
        patch.object(config_mod, "get_db_path", lambda: str(db_path)),
        patch.object(config_mod, "get_logs_dir", lambda: str(logs_dir)),
    ]
    for p in patchers:
        p.start()
    try:
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


_OFFLINE_PATCHED = False


def _install_offline_retrieval() -> None:
    """把 embedding / rerank 的**叶节点**替换成抛错（幂等）。

    ⚠️ **不能主动 `from utils.llm import gateway`** —— 实测 11.3 秒：
        utils.llm 包 __init__ → gateway → utils.llm_factory
          → `from langchain_openai import ChatOpenAI`
          → langchain_openai.chat_models.azure → transformers(5s) + torch(1.5s)
    它是本仓库 unit 套件最大的单点固定成本：曾是 session 级 autouse，
    于是 **每个 xdist worker 启动都白付一次**。

    改成「已导入才装补丁」后：不碰 embedding 的 worker 零成本跳过；
    碰 embedding 的用例必然经由 `memory/sdk.py` → `utils.llm.gateway`
    （模块级导入），执行前它已在 sys.modules 里 —— 补丁不会漏。
    """
    global _OFFLINE_PATCHED
    if _OFFLINE_PATCHED:
        return
    import unittest.mock as _mock

    def _offline(*a, **k):
        raise RuntimeError("unit 测试离线：embedding/rerank 禁止外呼，请走 FTS5-only。")

    patchers = []
    try:
        from utils.llm import gateway as _gw
        for _name in ("embed", "aembed", "rerank", "arerank"):
            if hasattr(_gw, _name):
                patchers.append(_mock.patch.object(_gw, _name, _offline))
    except Exception:
        return
    try:
        from utils.llm.backends.embed import EmbeddingProvider as _EP
        if hasattr(_EP, "_embed_openai"):
            patchers.append(_mock.patch.object(_EP, "_embed_openai", _offline))
    except Exception:
        pass
    for p in patchers:
        p.start()
    _OFFLINE_PATCHED = True


@pytest.fixture(autouse=True)
def _offline_llm_retrieval():
    """函数级 autouse：只做一次 sys.modules 查表，不主动导入（见 _install_offline_retrieval）。"""
    if "utils.llm.gateway" in sys.modules:
        _install_offline_retrieval()


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


# ── 标的别名库（红线：名称→代码是数据，不是代码）────────────────────────
# 别名唯一事实源是 `data/symbol_aliases.json`（运营直接改），由
# `scripts/ops/seed_symbol_aliases.py --apply` 灌进 market_data.db.symbol_alias。
# 单测不读本机真库（db/ 不进 git、各机状态不同），而是把**那个 JSON** 灌进临时库，
# 再把查询层（backtest/symbol_alias.py）指过去 —— 这样既确定，也顺带守住
# 「数据文件能被灌库」这条链路。


@pytest.fixture
def symbol_alias_db(tmp_path, monkeypatch):
    """用 data/symbol_aliases.json 建临时 symbol_alias 库，并把查询层指过去。"""
    import json
    import sqlite3

    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.tables import market_data as market_data_tables

    seed_path = os.path.join(PROJECT_ROOT, "data", "symbol_aliases.json")
    with open(seed_path, encoding="utf-8") as f:
        rows = json.load(f).get("aliases") or []

    db_path = tmp_path / "market_data_alias.db"
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn, market_data_tables.TABLES, [])
        for r in rows:
            conn.execute(
                "INSERT INTO symbol_alias (alias, code, kind, source, updated_at) "
                "VALUES (?,?,?,?,?)",
                (str(r["alias"]), str(r["code"]), str(r.get("kind") or "etf"),
                 "seed", "2026-11-01 00:00:00"))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr("backtest.symbol_alias._db_path", lambda: str(db_path))
    return str(db_path)
