"""chunking 工具函数测试：dashboard JSON 提炼 + 装饰线清理 + 切分。"""
import pytest

from memory.chunking import (
    dashboard_to_text, strip_report_decor, chunk_report, strip_chart_markers,
)


# ── dashboard_to_text：dashboard JSON 提炼为可读文本 ──

def test_dashboard_to_text_full_json():
    text = (
        "正文内容……\n"
        '@@DASHBOARD_START@@{"core_verdict":"市场处于弱势震荡筑底阶段，建议观望为主。",'
        '"decision_type":"hold","key_points":["超卖反弹需求已现","外围风险构成压力"],'
        '"next_watch":["外盘走势","主力资金动向"]}@@DASHBOARD_END@@'
    )
    out = dashboard_to_text(text)
    assert "核心结论：市场处于弱势震荡筑底阶段" in out
    assert "操作建议：持有/观望" in out
    assert "要点：超卖反弹需求已现；外围风险构成压力" in out
    assert "关注：外盘走势；主力资金动向" in out
    assert "@@DASHBOARD_START@@" not in out
    assert "core_verdict" not in out  # JSON 语法噪音已去除
    assert "正文内容" in out  # 正文保留


def test_dashboard_to_text_decision_mapping():
    text = '@@DASHBOARD_START@@{"core_verdict":"强势上攻","decision_type":"buy"}@@DASHBOARD_END@@'
    out = dashboard_to_text(text)
    assert "操作建议：买入" in out
    text2 = '@@DASHBOARD_START@@{"core_verdict":"见顶回落","decision_type":"sell"}@@DASHBOARD_END@@'
    assert "操作建议：卖出" in dashboard_to_text(text2)


def test_dashboard_to_text_truncated_fragment_removed():
    """截断碎片（如被 chunk 切断的 JSON）解析失败 → 整块移除，不污染正文。"""
    text = "正常分析内容……\n@@DASHBOARD_START@@{\"core_verdict\":\"市场处于弱势震荡筑底阶段\""
    out = dashboard_to_text(text)
    assert "DASHBOARD" not in out
    assert "正常分析内容" in out


def test_dashboard_to_text_dangling_end_removed():
    """孤立 END 碎片（无 START 的 JSON 尾巴）→ 整行移除。"""
    text = '正文内容……"scenario_tag":"市场决策仪表盘","dashboard_id":"market"}@@DASHBOARD_END@@'
    out = dashboard_to_text(text)
    assert "DASHBOARD" not in out
    assert "scenario_tag" not in out


def test_dashboard_to_text_no_marker_unchanged():
    text = "没有仪表盘的普通报告"
    assert dashboard_to_text(text) == text


def test_dashboard_to_text_empty():
    assert dashboard_to_text("") == ""
    assert dashboard_to_text(None) == ""


# ── strip_report_decor：装饰线清理 ──

def test_strip_report_decor_removes_lines():
    text = "═══ 市场日报 ═══\n核心内容\n─── 完 ───"
    out = strip_report_decor(text)
    assert "═" not in out
    assert "核心内容" in out


# ── chunk_report：切分 ──

def test_chunk_report_short_text_single():
    assert chunk_report("短文本") == ["短文本"]


def test_chunk_report_splits_long_text():
    long = "## 第一段\n" + "内容" * 300 + "\n## 第二段\n" + "更多" * 300
    chunks = chunk_report(long)
    assert len(chunks) >= 2
    assert all(len(c) > 0 for c in chunks)


# ── strip_chart_markers：CHART 占位符清理 ──

def test_strip_chart_markers_removes_uuid():
    text = "核心结论：持有观察\n@@CHART:5a2fc542-7063-4b2a-bd4b-c31af55df9f6@@ @@CHART:eb6096a3-88b1-4fa7-b2bb-77694ef31699@@"
    out = strip_chart_markers(text)
    assert "@@CHART" not in out
    assert "核心结论：持有观察" in out


def test_strip_chart_markers_noop_without_marker():
    text = "没有图表的普通报告"
    assert strip_chart_markers(text) == text


def test_strip_chart_markers_empty():
    assert strip_chart_markers("") == ""
    assert strip_chart_markers(None) == ""


# ── chunk_report：结构感知切分 ──

def _table(n_rows=8, cols=4):
    """构造 markdown 表格：表头 2 行 + n_rows 数据行。"""
    header = "| " + " | ".join(f"列{i}" for i in range(cols)) + " |"
    sep = "|" + "|".join("---" for _ in range(cols)) + "|"
    rows = ["| " + " | ".join(f"数据{i}{j}内容填充" for j in range(cols)) + " |"
            for i in range(n_rows)]
    return "\n".join([header, sep] + rows)


def test_long_table_split_keeps_header_in_each_chunk():
    """超长表格按行分组，且每个切片自带表头，不再在表格行中间硬切断。"""
    report = "## 二、候选标的排序\n\n" + _table(n_rows=30)
    chunks = chunk_report(report)
    assert len(chunks) >= 2
    for c in chunks:
        # 每个切片都携带章节标题 + 表头（自包含）
        assert c.startswith("## 二、候选标的排序"), f"切片缺少章节标题: {c[:60]}"
        assert "| 列0 |" in c, f"切片缺少表头: {c[:80]}"
        # 不得出现被拦腰切断的行（表格行首尾都是 |）
        for line in c.split("\n"):
            if line.startswith("|"):
                assert line.endswith("|")


def test_section_subchunks_carry_section_title():
    """超长章节切出的子块统一携带章节标题，单独看也知道在讲什么。"""
    report = "## 三、关键证据\n\n" + "这是一段很长的证据描述。" * 300
    chunks = chunk_report(report)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.startswith("## 三、关键证据")


def test_prelude_before_first_heading_preserved():
    """第一个标题之前的导言区（报告标题/数据截止/风险前置）不再被丢弃。"""
    report = (
        "# 北京银行持仓分析报告\n**数据截止：2026-09-03 10:33**\n\n"
        "> ⚠️ 风险前置：本报告仅作分析参考。\n\n"
        "## 一、核心结论\n结论内容" * 40
    )
    chunks = chunk_report(report)
    joined = "\n".join(chunks)
    assert "北京银行持仓分析报告" in joined
    assert "数据截止：2026-09-03" in joined
    assert "风险前置" in joined


def test_short_paragraphs_not_dropped():
    """无标题长文本切分时，短段落不应被整体丢弃。"""
    report = ("短段落一。\n\n" + "长段落内容。" * 400 + "\n\n短尾巴。") * 2
    chunks = chunk_report(report)
    joined = "\n".join(chunks)
    assert "短段落一" in joined
    assert "短尾巴" in joined
