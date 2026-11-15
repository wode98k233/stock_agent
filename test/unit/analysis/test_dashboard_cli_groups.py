import pytest

from agents.analysis.dashboard_cli import _entry_text, format_dashboard_cli
from agents.analysis.dashboard_schema import (
    DashboardData, DashboardEntry, DashboardGroup,
)


def test_cli_renders_group_lines():
    g = DashboardGroup(key="科技", decision="buy", horizon="1-5天",
                       confidence=0.66, stance="可参与但只低吸",
                       signal_symbol="159516", signal_name="半导体设备ETF",
                       position="≤10%",
                       entry=DashboardEntry(type="pullback", low=0.46, high=0.47),
                       invalidation="破0.456", note="注意回踩不追高")
    data = DashboardData(core_verdict="三方向", decision_type="buy",
                         confidence_level=0.62, sentiment_score=68,
                         trend_prediction="bullish", groups=[g])
    out = format_dashboard_cli(data)
    assert "方向决策" in out
    assert "科技" in out and "159516" in out
    # 第一行：决策标签后带组级置信度
    assert "可参与 66%" in out
    # 第二行：对象/仓位/触发/失效，触发条件与 Web 同口径
    assert "对象 159516 半导体设备ETF · 仓位 ≤10% · 触发 回踩 0.46–0.47 · 失效 破0.456" in out
    assert "触发 回踩" in out
    # 第三行：备注
    assert "备注 注意回踩不追高" in out


@pytest.mark.parametrize("entry,expected", [
    (DashboardEntry(type="none"), "现价可执行"),
    (DashboardEntry(type="unverifiable", note="资金转正再买"), "资金转正再买"),
    (DashboardEntry(type="unverifiable"), "条件待人工确认"),
    (DashboardEntry(type="pullback", low=0.46, high=0.47), "回踩 0.46–0.47"),
    (DashboardEntry(type="pullback", high=0.5), "回踩 ≤0.5"),
    (DashboardEntry(type="pullback", low=0.5), "回踩 ≤0.5"),
    (DashboardEntry(type="breakout", low=0.69), "站稳 0.69"),
    (DashboardEntry(type="breakout", high=0.72), "站稳 0.72"),
])
def test_entry_text_matches_web(entry, expected):
    assert _entry_text(entry) == expected
