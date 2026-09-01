"""记忆检索修复单元测试（问题1 / 问题2-A / 问题2-C）。

覆盖：
- 问题2-C：置信度权重真正生效（低信记忆下沉）
- 问题2-A：incomplete 失败残片排除（零迁移，防自指循环）+ 空结果回退
- 问题1：检索拼接 session 历史（当前+历史两路 RRF 融合）
"""
import pytest
from datetime import datetime, timedelta

from memory.backend.base import MemoryEntry
from memory.backend.hybrid import HybridBackend
from memory.backend.fts5 import FTS5Backend, ensure_jieba_ready
from memory.sdk import _is_incomplete_result

# FTS5 检索依赖 jieba 分词（写入/查询都需先分词加空格），未初始化会退化为逐字匹配导致零召回。
ensure_jieba_ready()


class _StubEmbedding:
    """不可用的 embedding 后端，强制走纯 FTS5 路径，便于无 chroma 测试。"""
    def is_available(self):
        return False


def _entry(eid, content, confidence=0.5, incomplete=False, rerank_score=0.0):
    prov = {"incomplete": True} if incomplete else None
    return MemoryEntry(
        entry_id=eid, stock_code="", stock_name="",
        content=content, score=1.0, rerank_score=rerank_score,
        confidence=confidence, provenance=prov,
    )


# ── 问题2-C：置信度权重 ──

def test_confidence_weight_sinks_low_confidence():
    hi = _entry("a", "x", confidence=0.9, rerank_score=0.9)
    lo = _entry("b", "y", confidence=0.2, rerank_score=0.9)
    out = HybridBackend._apply_confidence_weight([hi, lo])
    assert out[0].entry_id == "a"
    assert out[0].score > out[1].score


def test_confidence_weight_equal_rerank_orders_by_conf():
    e1 = _entry("a", "x", confidence=0.3, rerank_score=0.8)
    e2 = _entry("b", "y", confidence=0.8, rerank_score=0.8)
    out = HybridBackend._apply_confidence_weight([e1, e2])
    assert out[0].entry_id == "b"  # 高信在前


# ── 时间新鲜度：recency 因子 ──

def _dated_entry(eid, date_str, confidence=0.5, rerank_score=0.9):
    return MemoryEntry(
        entry_id=eid, stock_code="", stock_name="", content="x",
        score=1.0, rerank_score=rerank_score, confidence=confidence,
        metadata={"date": date_str},
    )


def test_recency_factor_today_is_max():
    today = datetime.now().isoformat()
    assert HybridBackend._recency_factor(_dated_entry("a", today)) == 1.0


def test_recency_factor_old_sinks_but_floor():
    old = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
           - timedelta(days=120)).isoformat()
    assert HybridBackend._recency_factor(_dated_entry("a", old)) == 0.5


def test_recency_factor_missing_date_neutral():
    assert HybridBackend._recency_factor(_entry("a", "x")) == 0.8


def test_recency_factor_bad_date_neutral():
    assert HybridBackend._recency_factor(_dated_entry("a", "not-a-date")) == 0.8


def test_new_memory_beats_old_despite_lower_confidence():
    """用户场景：语义相近时，「今天」的新记忆应排旧记忆前面，
    即使新记忆来源置信度更低（时间新鲜度修正 conf 偏差）。"""
    new = _dated_entry("new", datetime.now().isoformat(),
                       confidence=0.3, rerank_score=0.25)
    old = _dated_entry("old", (datetime.now() - timedelta(days=40)).isoformat(),
                       confidence=0.5, rerank_score=0.14)
    out = HybridBackend._apply_confidence_weight([old, new])
    assert out[0].entry_id == "new"
    # 验证分数：new = 0.25×0.3×1.0=0.075 > old = 0.14×0.5×0.65=0.0455（40 天 → 0.65 档）
    assert abs(out[0].score - 0.075) < 1e-6
    assert abs(out[1].score - 0.0455) < 1e-6


# ── 问题2-A：incomplete 排除 ──

def test_exclude_incomplete_removes_failed_fragments():
    inc = _entry("c", "市场温度 ⚠️(数据不足) 无法分析", incomplete=True)
    ok = _entry("d", "今日大盘科技回调")
    hb = HybridBackend(fts5=None, embedding=None)
    kept = [e.entry_id for e in hb._exclude_incomplete([inc, ok])]
    assert kept == ["d"]


def test_exclude_incomplete_empty_fallback():
    inc = _entry("c", "主线与轮动 ⚠️(数据不足) 无法分析", incomplete=True)
    hb = HybridBackend(fts5=None, embedding=None)
    # 排除后为空 -> 回退保留原结果，避免把正常记忆误删
    kept = [e.entry_id for e in hb._exclude_incomplete([inc])]
    assert kept == ["c"]


def test_is_incomplete_result_markers():
    assert _is_incomplete_result("主线与轮动 ⚠️(数据不足) 无法分析") is True
    assert _is_incomplete_result("数据不足，无法分析该板块") is True
    # 正常报告偶提「数据不足」三字但不含失败标记 -> 不算残片
    assert _is_incomplete_result("报告中提到历史数据不足，已用近期数据替代") is False
    assert _is_incomplete_result("") is False


# ── 问题1：检索拼接 session 历史（两路 RRF 融合）──

def test_search_context_surfaces_history_only_match(tmp_path):
    """当前提问未点名的历史相关记忆，应通过 context 路径召回。

    注：FTS5 为隐式 AND（空格=全命中），故 context 用「内容词子集」的短句，
    纯 FTS5 路径下即可验证「context 参数触发第二次召回」的机制；
    生产环境 Embedding 语义路会对长历史上下文做语义召回。
    """
    db = tmp_path / "mem_test.db"
    fts5 = FTS5Backend(str(db))
    hb = HybridBackend(fts5, _StubEmbedding())

    e1 = MemoryEntry(
        entry_id="", stock_code="", stock_name="",
        content="今日大盘总结：CPO、PCB、半导体科技三件套集体回调，资金流出",
        metadata={"date": "2026-07-09"},
    )
    eid = fts5.add(e1)

    # 当前提问不含 e1 的任何 token
    query = "市场温度 主线 轮动 补充 数据"
    no_ctx = [e.entry_id for e in hb.search(query, top_k=5)]
    # context 为 e1 内容词子集（短句）-> FTS5 AND 可命中
    with_ctx = [e.entry_id for e in hb.search(
        query, top_k=5, context="大盘 CPO 半导体 科技",
    )]

    # 无上下文：召回不到 e1
    assert eid not in no_ctx
    # 带上下文：历史路径把 e1 召回
    assert eid in with_ctx
    # 上下文确实改变了召回集合
    assert set(with_ctx) != set(no_ctx)


def test_search_context_does_not_break_basic_query(tmp_path):
    """带 context 时，当前问题自身的召回仍正常（不被历史稀释到完全丢失）。"""
    db = tmp_path / "mem_test2.db"
    fts5 = FTS5Backend(str(db))
    hb = HybridBackend(fts5, _StubEmbedding())

    e_cur = MemoryEntry(
        entry_id="", stock_code="", stock_name="",
        content="猪肉板块今日放量上涨，牧原股份领涨",
        metadata={"date": "2026-07-07"},
    )
    eid = fts5.add(e_cur)

    # query 词均为 content 子集 -> 当前问题路径自身召回
    res = hb.search("猪肉板块 牧原 上涨", top_k=5,
                    context="昨日大盘高开跳水，科技股回调")
    assert eid in [e.entry_id for e in res]
