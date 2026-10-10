"""P1 检索增强单测（2026-09-23）：R2 query 实体归一 + R3 分数感知 RRF。"""
from memory.backend.base import MemoryEntry
from memory.backend.hybrid import HybridBackend
from memory.sdk import _augment_query_with_subject


def _e(eid, score):
    return MemoryEntry(entry_id=eid, stock_code="", stock_name="", content="", score=score)


# ── R3：分数感知 RRF ──────────────────────────────────────

def test_rrf_fuse_score_aware_beats_pure_rank():
    """弱命中即使出现在两路，也不应靠数量压过单路强命中（纯排名 RRF 会）。"""
    x = _e("x", 1.0)
    y = _e("y", 0.5)
    w = _e("w", 1.0)
    out = HybridBackend._rrf_fuse([([x, y], 1.0, 0.0), ([w, y], 1.0, 0.0)], top_k=10)
    assert out[0].entry_id != "y"
    assert out[-1].entry_id == "y"


def test_rrf_fuse_context_gate_drops_weak():
    strong = _e("s", 1.0)
    weak = _e("k", 0.1)
    out = HybridBackend._rrf_fuse([([strong, weak], 0.6, 0.3)], top_k=10)
    assert [e.entry_id for e in out] == ["s"]  # norm 0.1 < 0.3 → 剔除


def test_rrf_fuse_respects_top_k():
    out = HybridBackend._rrf_fuse([([_e(str(i), 1.0) for i in range(10)], 1.0, 0.0)], top_k=3)
    assert len(out) == 3


# ── R2：query 实体归一 ────────────────────────────────────

def test_augment_no_entity_unchanged():
    assert _augment_query_with_subject("今天大盘怎么样") == "今天大盘怎么样"


def test_augment_entity_query_unchanged():
    q = "牧原股份最近走势"
    assert _augment_query_with_subject(q) == q


def test_augment_pronoun_uses_context_entity():
    out = _augment_query_with_subject(
        "这个股我怎么感觉最近疲软了啊", context="我之前分析了牧原股份 002714")
    assert "002714" in out and out.startswith("这个股")


def test_augment_without_context_unchanged():
    q = "这个股我怎么感觉最近疲软了啊"
    assert _augment_query_with_subject(q) == q


# ── retrieve 日志接进请求 logger：RadarLogger 适配 ─────────────

def test_as_std_logger_adapts_radar():
    import logging
    from utils.logger import RadarLogger
    from memory.sdk import _as_std_logger, _RadarLogAdapter

    ad = _as_std_logger(RadarLogger(logging.getLogger("t"), "req123"))
    assert isinstance(ad, _RadarLogAdapter)
    # stdlib 风格调用不应抛（%-args）
    ad.info("hits=%d chars=%d", 3, 100)
    ad.debug("x")
    ad.warning("y")
    ad.exception("boom")


def test_as_std_logger_passthrough():
    import logging
    import memory.sdk as m
    from memory.sdk import _as_std_logger

    assert _as_std_logger(None) is m._log
    lg = logging.getLogger("z")
    assert _as_std_logger(lg) is lg

