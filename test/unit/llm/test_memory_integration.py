"""
记忆系统接入 LLM 网关的集成测试
================================

验证两件事（对应 LLM 网关设计规格「memory 走网关」一节）：

1. memory 的 embedding / rerank **经过**统一网关（``utils.llm.gateway``），
   因此享受限流 / 重试 / 熔断 / 追踪一体化，不再是绕过 tracked_invoke 的旁路。
2. 网关**不可用**时（如 ``utils.llm`` 未安装 / 导入失败），memory 静默回退到旧的
   本地构造逻辑，不抛异常、不阻断启动。

运行：``python -m pytest test/unit/llm/test_memory_integration.py -v``
"""

import sys
from unittest.mock import MagicMock

import pytest

import utils.llm as llm_pkg
from utils.llm.types import Purpose


def _make_entries():
    from memory.backend.base import MemoryEntry

    return [
        MemoryEntry(entry_id="e1", stock_code="", stock_name="", content="医药板块估值修复"),
        MemoryEntry(entry_id="e2", stock_code="", stock_name="", content="半导体国产替代加速"),
        MemoryEntry(entry_id="e3", stock_code="", stock_name="", content="新能源装机高增"),
    ]


# ------------------------------------------------------------
# 1) 经过网关
# ------------------------------------------------------------
def test_embedding_routes_through_gateway(monkeypatch):
    """memory 的 embedding 应调用 gateway.embed，且 purpose=MEMORY。"""
    mock_gateway = MagicMock()
    mock_gateway._get_embed_provider.return_value = MagicMock()  # 探测不抛
    mock_gateway.embed.return_value = [[0.11, 0.22, 0.33]]
    monkeypatch.setattr(llm_pkg, "gateway", mock_gateway)
    monkeypatch.setattr("config.Config.STOCK_MEMORY_EMBEDDING", "openai")

    from memory.sdk import _build_embedding_fn

    fn = _build_embedding_fn()
    assert callable(fn)

    vec = fn("hello")
    assert vec == [0.11, 0.22, 0.33]

    # 确认确实走了网关，且用途被标记为 MEMORY
    assert mock_gateway.embed.called
    call = mock_gateway.embed.call_args
    assert call.args[0] == ["hello"]
    assert call.kwargs.get("purpose") is Purpose.MEMORY


def test_rerank_routes_through_gateway(monkeypatch):
    """memory 的 rerank 应返回 _GatewayRerankAdapter，并调用 gateway.rerank。"""
    mock_gateway = MagicMock()
    provider = MagicMock()
    provider.is_available.return_value = True
    mock_gateway._get_rerank_provider.return_value = provider
    mock_gateway.rerank.return_value = [0.1, 0.9, 0.5]  # e1<e3<e2
    monkeypatch.setattr(llm_pkg, "gateway", mock_gateway)
    monkeypatch.setattr("config.Config.STOCK_MEMORY_RERANKER", "local")

    from memory.backend.hybrid import _GatewayRerankAdapter, _build_reranker

    reranker = _build_reranker()
    assert isinstance(reranker, _GatewayRerankAdapter)

    entries = _make_entries()
    out = reranker.rerank("买入信号", entries)

    # 按分数降序：e2(0.9) > e3(0.5) > e1(0.1)
    assert [e.entry_id for e in out] == ["e2", "e3", "e1"]
    assert mock_gateway.rerank.called
    assert mock_gateway.rerank.call_args.kwargs.get("purpose") is Purpose.MEMORY


# ------------------------------------------------------------
# 2) 网关缺失时回退，不抛异常
# ------------------------------------------------------------
def test_embedding_falls_back_when_gateway_missing(monkeypatch):
    """utils.llm 不存在时，_build_embedding_fn 应回退、不抛异常。"""
    # 把 utils.llm 从 sys.modules 摘掉 → `from utils.llm import ...` 抛 ImportError
    monkeypatch.setitem(sys.modules, "utils.llm", None)
    monkeypatch.setattr("config.Config.STOCK_MEMORY_EMBEDDING", "openai")
    # 清空 key，确保 openai 分支回退为 None（而非真的去建 client）
    monkeypatch.setattr("config.Config.STOCK_MEMORY_API_KEY", "")
    monkeypatch.setattr("config.Config.OPENAI_API_KEY", "")

    from memory.sdk import _build_embedding_fn

    fn = _build_embedding_fn()  # 不应抛
    assert fn is None


def test_rerank_falls_back_when_gateway_missing(monkeypatch):
    """utils.llm 不存在时，_build_reranker 应回退到原生 Reranker。"""
    monkeypatch.setitem(sys.modules, "utils.llm", None)
    monkeypatch.setattr("config.Config.STOCK_MEMORY_RERANKER", "local")

    from memory.backend.hybrid import _GatewayRerankAdapter, _build_reranker

    reranker = _build_reranker()  # 不应抛
    # 网关缺失 → 不再是 _GatewayRerankAdapter，而是原生 Reranker（兼容旧行为）
    assert reranker is not None
    assert not isinstance(reranker, _GatewayRerankAdapter)
