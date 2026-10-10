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


# ══════════════════════════════════════════════════════════════
# 原始数据通道（2026-10-01：raw_result 与 step_results 曾硬二选一，
#   导致 group 的 step_results 把含数据底版的 raw_result 整体挤掉）
# ══════════════════════════════════════════════════════════════

def _rich_raw() -> str:
    return "数据底版：深证成指 -8.044%，全A主力净流出 4294亿。" * 20


def test_build_prompt_keeps_raw_result_when_no_evidence():
    """无工具/条目证据 → raw_result 作为原始素材注入（保底通道）"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template("theme_trading"), {})
    raw = _rich_raw()
    messages = _build_prompt(template, {}, "9月市场总结", raw_result=raw)
    all_text = " ".join(m[1] for m in messages)

    assert "## 原始素材" in all_text          # 消息头只在真正注入时出现
    assert all_text.count(raw) == 1


def test_build_prompt_does_not_duplicate_raw_result_with_seed_flag():
    """raw_result_is_seed=True（仅 group）且条目证据已载入 → raw_result 不再重复注入"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template("theme_trading"), {})
    raw = _rich_raw()
    evidence = "### 数据底版 · 数据采集员\n输出:\n" + raw
    messages = _build_prompt(
        template, {}, "9月市场总结",
        raw_result=raw, tool_evidence=evidence, raw_result_is_seed=True,
    )
    all_text = " ".join(m[1] for m in messages)

    assert all_text.count(raw) == 1           # 只来自 evidence
    assert "## 原始素材" not in all_text      # 未重复注入 raw_result


def test_build_prompt_seed_flag_still_injects_without_evidence():
    """raw_result_is_seed=True 但没有任何条目证据 → 仍注入（保底，不能变成零数据）"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template("theme_trading"), {})
    raw = _rich_raw()
    messages = _build_prompt(template, {}, "9月市场总结",
                             raw_result=raw, raw_result_is_seed=True)
    all_text = " ".join(m[1] for m in messages)
    assert "## 原始素材" in all_text
    assert all_text.count(raw) == 1


# ── 跨模式矩阵：raw_result 注入规则是五个模式共享的判断（2026-10-01）──
# 背景：曾把判断写成 `elif not tool_evidence`，结果 React / Unified 模式
# （step_results 为空、tool_calls 非空、raw_result 是报告草稿）的 raw_result 被整段挡掉。
# 这条矩阵把每个模式的输入形状与期望行为钉死，防止再犯。

def test_raw_result_injection_matrix_across_modes():
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template("theme_trading"), {})
    raw = _rich_raw()
    evidence = "### mx_data_query\n输出:\n" + _rich_raw()
    one_step = [{"step": 1, "skill": "mx_data", "purpose": "取数", "result": "步骤结果" * 30}]

    cases = [
        # (模式, step_results, tool_evidence, raw_result_is_seed, 期望注入)
        ("react",   [],      evidence, False, True),   # 报告草稿必须注入
        ("unified", [],      evidence, False, True),   # 同上
        ("plan",    one_step, "",      False, False),  # 走 ai Step 通道
        ("pdor",    one_step, "",      False, False),  # 同上
        ("group",   None,    evidence, True,  False),  # 原始数据走条目通道
        # 兜底：即使声明是种子，没有条目证据也必须注入，否则报告零数据
        ("group_无证据", None, "", True, True),
    ]

    for mode, steps, ev, is_seed, expect_raw in cases:
        messages = _build_prompt(
            template, {}, "9月市场总结",
            raw_result=raw, tool_evidence=ev, step_results=steps,
            raw_result_is_seed=is_seed,
        )
        all_text = " ".join(m[1] for m in messages)
        got = "## 原始素材" in all_text
        assert got is expect_raw, f"{mode}: 期望 raw_result 注入={expect_raw}，实际={got}"

        if steps:
            assert any(m[0] == "ai" and m[1].startswith("### Step 1") for m in messages), \
                f"{mode}: 有 step_results 时必须走 ai Step 通道"
        if ev:
            assert "## 原始工具数据" in all_text, f"{mode}: 有证据时必须注入条目通道"


def test_build_prompt_req_mentions_raw_material():
    """核心要求 1 的措辞需覆盖「原始素材」，否则 tool_evidence 为空时自相矛盾"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    template = evaluate(load_template("theme_trading"), {})
    messages = _build_prompt(template, {}, "9月市场总结", raw_result=_rich_raw())
    assert "原始素材" in messages[1][1]


# ══════════════════════════════════════════════════════════════
# 模板级 min_report_tokens（2026-10-01：轻量模板 quick_lookup）
# 背景：REPORT_MIN_TOKENS=3500 会让「只要股价」这类请求的报告被
# `_expand_report_if_short` 主动扩写拉长（实测 7283 字符）。
# ══════════════════════════════════════════════════════════════

def test_min_report_tokens_template_override():
    """模板可用 min_report_tokens 覆盖全局；0=轻量；非法值回退全局"""
    from agents.analysis.report_builder import _min_report_tokens
    assert _min_report_tokens(None) >= 1000                       # 全局默认
    assert _min_report_tokens({}) >= 1000
    assert _min_report_tokens({"min_report_tokens": 0}) == 0      # 轻量模板
    assert _min_report_tokens({"min_report_tokens": 800}) == 800
    assert _min_report_tokens({"min_report_tokens": "abc"}) >= 1000   # 非法回退
    assert _min_report_tokens({"min_report_tokens": None}) >= 1000


def test_quick_template_skips_expand(monkeypatch):
    """轻量模板不得触发扩写 LLM 调用（否则报告又被拉长、多花一次调用）"""
    from agents.analysis import report_builder as rb
    from agents.analysis.template_store import load_template, evaluate

    t = evaluate(load_template("quick_lookup"), {})
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("轻量模板不应触发扩写 LLM 调用")

    # 注意：tracked_invoke 是在 _expand_report_if_short **函数体内**导入的，
    # 必须 patch 源头 utils.llm_factory.tracked_invoke 才拦得住。
    monkeypatch.setattr("utils.llm_factory.tracked_invoke", _boom)
    short = "今世缘(603369) 2026-09-30 收盘 28.09 元，涨跌幅 +3.54%。"
    out = rb._expand_report_if_short(short, t, "给我获取下今世缘最新的股价，只需要这个就行了",
                                    MagicMock(), MagicMock())
    assert out == short                       # 原样返回，未扩写
    assert calls["n"] == 0


def test_quick_template_prompt_requests_brevity():
    """轻量模板的提示词必须反向要求「简洁优先」，且不带长文目标"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    t = evaluate(load_template("quick_lookup"), {})
    messages = _build_prompt(t, {}, "给我获取下今世缘最新的股价，只需要这个就行了", raw_result="")
    sys_all = " ".join(c for r, c in messages if r == "system")

    assert "简洁优先" in sys_all
    assert "禁止补充用户没要的板块" in sys_all
    assert "长文与版式要求" not in sys_all      # 长文要求不得出现


def test_normal_template_keeps_long_form_requirement():
    """对照组：普通模板仍保留长文要求（防止改坏既有行为）"""
    from agents.analysis.report_builder import _build_prompt
    from agents.analysis.template_store import load_template, evaluate

    t = evaluate(load_template("sector_timing"), {})
    messages = _build_prompt(t, {}, "板块还能追吗", raw_result="")
    sys_all = " ".join(c for r, c in messages if r == "system")
    assert "长文与版式要求" in sys_all
    assert "全文目标" in sys_all
