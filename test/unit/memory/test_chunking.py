"""chunking 工具函数测试：dashboard JSON 提炼 + 装饰线清理 + 切分。"""
import pytest

from memory.chunking import dashboard_to_text, strip_report_decor, chunk_report


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
