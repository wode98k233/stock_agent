"""R5（2026-09-23）：chunker 最小长度合并 + 表格语义摘要。"""
from memory.chunking import (
    chunk_report, _merge_small_chunks, _table_summary, _augment_table_chunks,
)


def _table(n_rows=8, cols=3):
    header = "| " + " | ".join(["代码", "名称", "涨跌幅"][:cols]) + " |"
    sep = "|" + "|".join("---" for _ in range(cols)) + "|"
    rows = [f"| 60000{i} | 股票{i} | {i}.0% |" for i in range(n_rows)]
    return "\n".join([header, sep] + rows)


# ── 最小长度合并 ──────────────────────────────────────────

def test_merge_small_into_prev():
    out = _merge_small_chunks(["A" * 300, "短", "B" * 300])
    assert len(out) == 2
    assert "短" in out[0]


def test_merge_first_small_into_next():
    out = _merge_small_chunks(["短", "B" * 300])
    assert len(out) == 1 and out[0].startswith("短")


def test_merge_respects_max():
    out = _merge_small_chunks(["A" * 950, "B" * 100])  # 950+1+100 > 1000 → 不合并
    assert len(out) == 2


def test_chunk_report_merges_tiny_sections():
    report = "\n".join(f"## 小标题{i}\n很短。" for i in range(40))
    chunks = chunk_report(report)
    assert len(chunks) == 1  # 40 个碎渣合并为一块（总长 < 1000）
    assert len(chunks[0]) >= 120


# ── 表格语义摘要 ──────────────────────────────────────────

def test_table_summary_extracts_cols_and_rows():
    s = _table_summary(_table(6))
    assert s.startswith("【表格】")
    assert "列：代码、名称、涨跌幅" in s
    assert "600000" in s


def test_table_summary_none_for_plain_text():
    assert _table_summary("这是一段普通文本，没有表格。") == ""


def test_augment_table_inserts_after_heading():
    ch = "## 二、候选\n" + _table(5)
    out = _augment_table_chunks([ch])[0]
    lines = out.split("\n")
    assert lines[0] == "## 二、候选"      # 标题仍在首行（不破坏既有契约）
    assert lines[1].startswith("【表格】")


def test_chunk_report_small_table_gets_summary():
    chunks = chunk_report("## 二、候选\n" + _table(6))
    assert len(chunks) == 1
    assert chunks[0].startswith("## 二、候选")
    assert "【表格】" in chunks[0]


def test_chunk_report_augments_table_with_summary():
    long_rows = "\n".join(
        f"| 60000{i} | 股票{i}名称较长填充 | {i}.0%涨跌幅数据 |" for i in range(40))
    header = "| 代码 | 名称 | 涨跌幅 |\n|---|---|---|"
    chunks = chunk_report("## 二、候选\n" + header + "\n" + long_rows)
    assert len(chunks) >= 2
    assert all(c.startswith("## 二、候选") for c in chunks)
    assert any("【表格】" in c for c in chunks)
