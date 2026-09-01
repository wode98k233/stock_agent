"""模板续问继承单元测试（问题3-A / 问题3-B）。

覆盖：
- 问题3-A：session 内追问应继承上一轮模板
  - 含追问标记 + 无强场景词 -> 续问，继承上一轮模板
  - 含强场景词（独立提问）-> 不继承
  - 短消息但无标记 -> 不误判为续问
- 问题3-B + 开关：REPORT_TEMPLATE_CONTINUATION_MODE=llm 时 LLM 失败回退 keyword
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agents.analysis import template_store
from agents.analysis.template_store import (
    select_template_for_input,
    _is_continuation,
    _last_human_message,
)


PREV_MARKET = "今日大盘总结，这个7月份的整轮科技调整，是不是可以说明科技结束了，CPO，PCB，半导体"
PREV_STOCK = "帮我看看贵州茅台还能持有吗"


def test_is_continuation_true_for_followup():
    hist = [("human", PREV_MARKET)]
    assert _is_continuation("市场温度 和 主线与轮动 你补充下数据", hist) is True


def test_is_continuation_false_for_scene_keyword():
    # 当前含强场景词「大盘」-> 独立提问，不继承
    hist = [("human", PREV_MARKET)]
    assert _is_continuation("明天大盘还会继续跌吗", hist) is False


def test_is_continuation_false_short_standalone():
    # 短消息但无追问标记 -> 不误判为续问
    hist = [("human", PREV_MARKET)]
    assert _is_continuation("茅台", hist) is False


def test_is_continuation_no_history():
    assert _is_continuation("补充下数据", None) is False
    assert _is_continuation("补充下数据", []) is False


def test_last_human_message_picks_recent():
    hist = [
        ("human", "第一轮问题"),
        ("ai", "回答"),
        ("human", "第二轮问题"),
    ]
    assert _last_human_message(hist) == "第二轮问题"


def test_last_human_message_accepts_base_messages():
    hist = [
        HumanMessage(content="first question"),
        AIMessage(content="first answer"),
        HumanMessage(content="second question"),
    ]

    assert _last_human_message(hist) == "second question"


def test_select_template_inherits_prev_on_followup():
    """复现 log2：追问返回 market_daily（原 bug 错配 sector_landscape）。"""
    hist = [("human", PREV_MARKET)]
    assert select_template_for_input("市场温度 和 主线与轮动 你补充下数据", history=hist) == "market_daily"


def test_select_template_no_history_uses_current():
    """无历史时，相同追问文本走原（有 bug）路径，验证历史是关键修复点。"""
    assert select_template_for_input("市场温度 和 主线与轮动 你补充下数据") == "sector_landscape"


def test_select_template_standalone_not_inherited():
    """独立提问不被误继承上一轮模板。"""
    hist = [("human", PREV_STOCK)]
    assert select_template_for_input("明天大盘还会继续跌吗", history=hist) != "portfolio_advice"


def test_select_template_followup_inherits_stock_template():
    hist = [("human", PREV_STOCK)]
    assert select_template_for_input("再给我补充下数据", history=hist) == "portfolio_advice"


def test_llm_mode_fallback_on_error(monkeypatch):
    """REPORT_TEMPLATE_CONTINUATION_MODE=llm 且 LLM 不可用时应回退 keyword 逻辑。"""
    monkeypatch.setattr(template_store.Config, "REPORT_TEMPLATE_CONTINUATION_MODE", "llm")
    # 让 get_llm 抛异常 -> _select_template_via_llm 捕获并返回 None -> 回退 keyword
    import utils.llm_factory as lf
    monkeypatch.setattr(lf, "get_llm", lambda: (_ for _ in ()).throw(RuntimeError("no llm")))

    hist = [("human", PREV_MARKET)]
    # 应与 keyword 模式结果一致（market_daily）
    assert select_template_for_input("市场温度 和 主线与轮动 你补充下数据", history=hist) == "market_daily"
