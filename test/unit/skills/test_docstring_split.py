"""docstring 拆分（PRD-A9 §2.3.2）单元测试

tools 数组是 prompt 缓存前缀的**位置 0**。description 越长，这个位置的固定成本越高，
而每次换批换绑工具会让它整条作废——所以要把「数据源回落 / 口径边界 / 返回结构」这类
业务口径从 description 挪走，改由 usage_guide 在激活技能时按需投递。

约定：**docstring 第一段 → description，其余段 → usage_guide**。
收益是 64 处装饰器语法零改动，只需重排空行分段。
"""
import pytest

from tools.skill_builder import DESC_SOFT_LIMIT, get_usage_guide, skill_tool, split_docstring


# ── 纯函数 ──────────────────────────────────────────────

@pytest.mark.parametrize("doc,expect_desc", [
    ("获取个股实时行情", "获取个股实时行情"),
    ("获取北向资金历史数据（市场级口径）。", "获取北向资金历史数据（市场级口径）。"),
    ("""获取北向资金历史数据。

    返回近 20 个交易日的长表。""", "获取北向资金历史数据。"),
])
def test_first_paragraph_becomes_description(doc, expect_desc):
    desc, _ = split_docstring(doc)
    assert desc == expect_desc


def test_remaining_paragraphs_become_guide():
    _, guide = split_docstring("""首段说明。

        第二段。

        第三段。""")
    assert "第二段。" in guide
    assert "第三段。" in guide
    assert "首段说明。" not in guide


@pytest.mark.parametrize("doc", ["", None])
def test_empty_docstring_is_safe(doc):
    assert split_docstring(doc) == ("", "")


def test_info_zero_loss():
    """拆分只做「移动」不做「改写」：原文所有实质内容都能在 desc+guide 里找到。"""
    doc = """获取个股资金流向（主力/散户/净流入）。

    主源 AKShare；取不到时回落同花顺问财。

    注：问财返回的是个股净买入额排行，语义不同构。"""
    desc, guide = split_docstring(doc)
    for token in ("AKShare", "同花顺问财", "净买入额排行", "语义不同构"):
        assert token in desc + guide, f"拆分丢失了内容：{token}"


# ── 装饰器接线 ───────────────────────────────────────────

def test_decorator_trims_wrapper_doc_to_first_paragraph():
    @skill_tool
    def demo(self, x: int) -> str:
        """一句话描述。

        业务口径第二段。"""
        return str(x)

    assert demo.__doc__ == "一句话描述。"
    assert get_usage_guide(demo) == "业务口径第二段。"


def test_decorator_without_guide_keeps_full_doc():
    @skill_tool
    def demo2(self, x: int) -> str:
        """只有一段。"""
        return str(x)

    assert demo2.__doc__ == "只有一段。"
    assert get_usage_guide(demo2) == ""


def test_decorator_guide_is_readable_from_wrapped_attr():
    """装饰器可能套多层（如 eastmoney），任一层都应能取到 guide。"""
    @skill_tool
    def inner(self, x: int) -> str:
        """首段。

        口径段。"""
        return str(x)

    assert get_usage_guide(inner) == "口径段。"


# ── 与真实工具面的一致性（需要构建真实技能，开销较大）───

def test_real_tool_descriptions_are_below_soft_limit_on_average():
    """真实工具面的 description 平均长度应远低于拆分前。

    拆分前基线：51 个工具 / 6135 字符（均值 ~120）。
    拆分后首段均值应显著下降。**不做硬断言 60** —— 参数枚举类工具
    （get_macro_series / calc_technical_indicators 等）按设计 §1.3 反例 B
    必须把取值留在 description 里，强行压短会降低工具调用准确率。
    """
    from utils.logger import get_logger
    from utils.memory import MemoryManager
    from tools.skill_register import SkillRegister

    logger, _, _, _ = get_logger("doc-split-test")
    reg = SkillRegister()
    reg.auto_discover()
    tools = reg.build_all_langchain_tools(logger, MemoryManager(logger))
    if not tools:
        pytest.skip("无可用工具")

    lengths = [len(getattr(t, "description", "") or "") for t in tools]
    avg = sum(lengths) / len(lengths)
    assert avg < 80, f"description 平均长度 {avg:.0f} 字，拆分未生效"
    # 拆分前共 6135 字
    assert sum(lengths) < 6135, f"总字符 {sum(lengths)} 未低于拆分前基线 6135"


def test_desc_soft_limit_is_sane():
    """软上限常量存在且为正，避免后续被误设成 0 导致全量报警。"""
    assert 0 < DESC_SOFT_LIMIT <= 120
