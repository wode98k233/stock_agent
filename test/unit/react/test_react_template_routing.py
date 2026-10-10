"""ReAct 模板路由回归：必须用**原始输入**（raw_user_input），不被 enrich 的画像/记忆污染。

事故（2026-09-23）：react 先 enrich 再进图，SelectTemplateNode 用富化文本打分；
「持仓」问句因富化文本里历史的 ETF 关键词被误判为 etf_analysis（plan 家族早已用 raw）。
"""
import asyncio


class _Mem:
    enabled = True

    def get_history(self):
        return []


def _run(state):
    import logging
    from agents.react.nodes import SelectTemplateNode
    node = SelectTemplateNode(memory=_Mem(), logger=logging.getLogger("t"))
    return asyncio.run(node(state))


def test_routes_on_raw_input_not_enriched():
    raw = "帮我分析下持仓，给一点建议"          # 含"持仓" → portfolio_advice
    polluted = raw + ("\n[相关记忆片段] 恒生互联网ETF 基金 大盘 板块 早盘总结" * 6)
    out = _run({"user_input": polluted, "raw_user_input": raw})
    assert out["selected_template_id"] == "portfolio_advice"


def test_falls_back_to_user_input_when_raw_missing():
    out = _run({"user_input": "给我生成今天大盘的总结"})
    assert out["selected_template_id"] == "market_daily"
