"""
报告合成器单元测试

运行: pytest test/unit/analysis/test_report_builder.py -v
"""
import os
import sys
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ══════════════════════════════════════════════════════════════
# format_report
# ══════════════════════════════════════════════════════════════

def test_format_report_basic():
    """基本格式化输出"""
    from agents.analysis.report_builder import format_report
    from agents.analysis.models import ReportSection

    report = {
        "title": "标准分析报告",
        "summary": "短线看多",
        "sections": [
            ReportSection(id="summary", title="核心摘要", content="短线看多", status="ok"),
            ReportSection(id="technical", title="技术面", content="MACD金叉", status="ok"),
        ],
        "disclaimer": "仅供参考",
    }
    output = format_report(report)
    assert "标准分析报告" in output
    assert "短线看多" in output
    assert "技术面" in output
    assert "仅供参考" in output


def test_format_report_degraded_section():
    """degraded 板块带警告标记"""
    from agents.analysis.report_builder import format_report
    from agents.analysis.models import ReportSection

    report = {
        "title": "报告",
        "summary": "测试",
        "sections": [
            ReportSection(id="fundamental", title="估值", content="", status="degraded"),
        ],
        "disclaimer": "",
    }
    output = format_report(report)
    assert "⚠️" in output


def test_format_report_skip_section():
    """skip 板块不出现在输出中"""
    from agents.analysis.report_builder import format_report
    from agents.analysis.models import ReportSection

    report = {
        "title": "报告",
        "summary": "测试",
        "sections": [
            ReportSection(id="capital", title="资金面", content="", status="skip"),
            ReportSection(id="tech", title="技术面", content="MACD金叉", status="ok"),
        ],
        "disclaimer": "",
    }
    output = format_report(report)
    assert "资金面" not in output
    assert "技术面" in output


def test_format_report_color():
    """color=True 时输出包含颜色标记"""
    from agents.analysis.report_builder import format_report, COLOR_MAP, RESET
    from agents.analysis.models import ReportSection

    report = {
        "title": "报告",
        "summary": "金叉信号",
        "sections": [
            ReportSection(id="tech", title="技术面", content="MACD金叉确认", status="ok"),
        ],
        "disclaimer": "",
    }
    output = format_report(report, color=True)
    # 金叉应被着色
    if "金叉" in output:
        assert RESET in output


def test_format_report_no_color_by_default():
    """默认不加 ANSI 颜色（web/trace 干净文本）"""
    from agents.analysis.report_builder import format_report, RESET
    from agents.analysis.models import ReportSection

    report = {
        "title": "报告",
        "summary": "金叉信号",
        "sections": [
            ReportSection(id="tech", title="技术面", content="MACD金叉确认", status="ok"),
        ],
        "disclaimer": "",
    }
    output = format_report(report)
    assert "\033[" not in output


# ══════════════════════════════════════════════════════════════
# _rule_based_report
# ══════════════════════════════════════════════════════════════

def test_rule_based_report():
    """规则化报告包含板块"""
    from agents.analysis.report_builder import _rule_based_report
    from agents.analysis.models import SlotResult
    from agents.analysis.template_store import load_template

    template = load_template("standard")
    slots = {
        "rsi_status": SlotResult(
            slot="rsi_status", value=65, status="偏强",
            interpretation="RSI 65，偏强", source_tool="test",
            method="json", confidence=0.9,
        ),
    }
    report = _rule_based_report(template, slots, "分析茅台")
    assert "sections" in report
    assert len(report["sections"]) > 0
    assert "disclaimer" in report


def test_rule_based_report_degraded():
    """规则化报告 degraded 板块"""
    from agents.analysis.report_builder import _rule_based_report
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("standard")
    adjusted = evaluate(template, {})
    report = _rule_based_report(adjusted, {}, "分析茅台")

    # 应有 degraded 板块
    degraded = [s for s in report["sections"] if s.status == "degraded"]
    assert len(degraded) > 0


# ══════════════════════════════════════════════════════════════
# build (async, with mock LLM)
# ══════════════════════════════════════════════════════════════

def test_build_prompt_rich_raw_adds_audit_review_guidance():
    """rich raw 模式注入审计式市场复盘软约束"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("sector_timing")
    adjusted = evaluate(template, {})
    raw_result = "已有分析：" + "市场高开低走，CPO抗跌，白酒冲高回落。" * 40

    messages = _build_prompt(
        adjusted,
        {},
        "今天A股高开跳水，CPO和白酒为什么分化？",
        raw_result=raw_result,
        tool_evidence="mx_xuangu_filter 返回白酒代表股包含物产中大；mx_data_query 返回 NoneType 错误",
    )
    # 消息按类型拆分：system[0]=角色+输出结构, system[1]=核心要求, user=数据
    sys0 = messages[0][1]  # 角色 + 输出结构
    sys1 = messages[1][1]  # 核心要求 + 交叉验证 + qa_rules
    all_text = " ".join(msg[1] for msg in messages)

    assert "输出结构" in sys0
    assert "核心要求" in sys1
    assert "数据缺失" in sys1
    # 动态数据在用户消息中
    assert "原始工具数据" in all_text
    assert "用户问题" in all_text


def test_build_prompt_includes_current_template_qa_rules():
    """最终报告 prompt 注入当前模板 qa_rules 作为软约束"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("theme_trading")
    adjusted = evaluate(template, {})

    messages = _build_prompt(
        adjusted,
        {},
        "机器人题材炒作到什么阶段了",
        raw_result="",
    )
    sys_msg = messages[0][1]

    # QA 规则现在在系统消息中（提升缓存命中率）
    assert "本模板质量要求（软约束）" in sys_msg
    assert "必须区分题材级别" in sys_msg
    assert "龙头辨识必须有量化依据" in sys_msg


def test_report_min_tokens_uses_config_value(monkeypatch):
    """报告最低 token 目标从 Config.REPORT_MIN_TOKENS 读取"""
    from agents.analysis import report_builder

    monkeypatch.setattr(report_builder.Config, "REPORT_MIN_TOKENS", 4200, raising=False)

    assert report_builder._min_report_tokens() == 4200


def _mock_utils_module():
    """创建 mock 的 utils.llm_factory 模块，避免 langchain 依赖"""
    mock_module = MagicMock()
    mock_module.get_llm = MagicMock(return_value=MagicMock())
    mock_resp = MagicMock()
    mock_resp.content = "## 核心摘要\n短线看多\n\n## 技术面\nMACD金叉\n\n## 综合研判\n建议观望\n\n## 风险提示\n市场有风险"
    mock_module.tracked_invoke = MagicMock(return_value=mock_resp)
    mock_module.llm_json_with_retry = MagicMock(return_value=None)
    return mock_module


@pytest.mark.asyncio
async def test_build_with_mock_llm():
    """build 使用 mock LLM 合成报告"""
    from agents.analysis.report_builder import build
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("standard")
    adjusted = evaluate(template, {})

    mock_mod = _mock_utils_module()
    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        report = await build(adjusted, {}, "分析茅台")

    assert "sections" in report
    assert "title" in report
    assert "disclaimer" in report


@pytest.mark.asyncio
async def test_build_budget_insufficient():
    """预算不足 → 规则化报告"""
    from agents.analysis.report_builder import build
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("standard")
    adjusted = evaluate(template, {})

    budget = MagicMock()
    budget.get_status.return_value = {"calls_percent": 90}

    mock_mod = _mock_utils_module()
    mock_mod.tracked_invoke = MagicMock(return_value=MagicMock(content="## 核心结论\n短线看多"))
    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        report = await build(adjusted, {}, "分析茅台", budget=budget)
    assert "sections" in report


@pytest.mark.asyncio
async def test_build_llm_failure_fallback():
    """LLM 失败 → 规则化报告"""
    from agents.analysis.report_builder import build
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("standard")
    adjusted = evaluate(template, {})

    mock_mod = _mock_utils_module()
    mock_mod.tracked_invoke = MagicMock(side_effect=Exception("API error"))
    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        report = await build(adjusted, {}, "分析茅台")

    assert "sections" in report


# ══════════════════════════════════════════════════════════════
# _parse_report_sections — 中文序号前缀兼容性
# ══════════════════════════════════════════════════════════════

def test_parse_report_sections_chinese_numbering():
    """LLM 在标题前加 '一、二、' 中文序号时，章节解析不应失败。

    回归测试：修复前 LLM 输出 '## 一、核心结论' 会导致
    _parse_report_sections 匹配失败 → sections=[] → format_report
    只输出标题+免责声明，正文全部丢失。
    """
    from agents.analysis.report_builder import _parse_report_sections, _normalize_heading

    # _normalize_heading 去序号
    assert _normalize_heading("## 一、核心结论") == "## 核心结论"
    assert _normalize_heading("## 二、位置判断") == "## 位置判断"
    assert _normalize_heading("### 3.技术面分析") == "### 技术面分析"
    assert _normalize_heading("## 十、关键证据") == "## 关键证据"
    # 无序号的不受影响
    assert _normalize_heading("## 核心结论") == "## 核心结论"

    template = {"sections": [
        {"id": "s1", "title": "核心结论", "required": True, "prompt": ""},
        {"id": "s2", "title": "技术面分析", "required": True, "prompt": ""},
    ]}

    llm_output = (
        "## 一、核心结论\n\n看多。\n\n---\n\n"
        "## 二、技术面分析\n\nMACD金叉。\n"
    )
    sections = _parse_report_sections(llm_output, template)
    assert len(sections) == 2, f"应解析出 2 个板块，实际 {len(sections)}"
    assert sections[0].id == "s1"
    assert "看多" in sections[0].content
    assert sections[1].id == "s2"
    assert "MACD金叉" in sections[1].content


def test_build_mode_b_fallback_when_parse_yields_zero_sections():
    """模式 B 解析出 0 个板块但 LLM 内容充足时，应回退为完整报告输出。

    回归测试：确保不会因解析失败导致正文全部丢失。
    """
    from agents.analysis.report_builder import build
    from agents.analysis.template_store import load_template, evaluate

    template = load_template("standard")
    adjusted = evaluate(template, {})

    # 构造一段足够长的内容（>5000字符 ≈ >3500 token，跳过 _expand_report_if_short），
    # 且标题与 standard 模板块完全不匹配，使 _parse_report_sections 返回 []
    long_body = "这是一段与模板标题无关的报告正文内容，用于验证解析失败时的兜底逻辑。" * 200
    mock_resp = MagicMock()
    mock_resp.content = f"## 一、完全自定义标题\n\n{long_body}"

    mock_mod = _mock_utils_module()
    mock_mod.tracked_invoke = MagicMock(return_value=mock_resp)
    mock_mod.get_report_llm = MagicMock(return_value=MagicMock())

    # 使用 mock logger，避免标准 logger 对 ("A", msg) 双参数格式报错
    mock_logger = MagicMock()

    with patch.dict(sys.modules, {"utils.llm_factory": mock_mod}):
        import asyncio
        report = asyncio.new_event_loop().run_until_complete(build(
            adjusted, {}, "测试问题", raw_result="", tool_calls=None,
            logger_obj=mock_logger,
        ))

    sections = report.get("sections", [])
    assert len(sections) == 1, f"应回退为 1 个 full_report 板块，实际 {len(sections)}"
    assert sections[0].id == "full_report"
    assert len(sections[0].content) > 200


if __name__ == "__main__":
    import traceback
    tests = [
        test_format_report_basic, test_format_report_degraded_section,
        test_format_report_skip_section, test_format_report_color,
        test_rule_based_report, test_rule_based_report_degraded,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*50}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed > 0 else 0)


# ══════════════════════════════════════════════════════════════
# Prompt 缓存契约测试：已合规路径
# ══════════════════════════════════════════════════════════════

def test_build_prompt_stable_system_before_dynamic_user():
    """_build_prompt: 稳定 system 在前，动态 user 在后。"""
    from agents.analysis.report_builder import _build_prompt

    template = {
        "role": "STATIC_ROLE_MARKER",
        "time_horizon": {"default": "short", "presets": {}},
        "sections": [
            {"title": "摘要", "prompt": "DYNAMIC_SECTION_MARKER", "max_words": 100, "requiraw": False},
        ],
    }
    messages = _build_prompt(template, {}, "DYNAMIC_INPUT_MARKER")

    # system 消息在前
    assert messages[0][0] == "system"
    assert "STATIC_ROLE_MARKER" in messages[0][1]

    # 动态内容（section prompt、user input）不应在 system 消息中
    assert "DYNAMIC_INPUT_MARKER" not in messages[0][1]

    # 动态内容在后续 user 消息中
    dynamic_contents = " ".join(m[1] for m in messages if m[0] == "user")
    assert "DYNAMIC_INPUT_MARKER" in dynamic_contents
