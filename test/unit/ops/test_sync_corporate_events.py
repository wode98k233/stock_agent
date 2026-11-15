"""公司事件同步脚本的纯解析函数（不联网）。"""
import pandas as pd

from scripts.ops import sync_corporate_events as sync


def test_parse_unlock_in_window():
    df = pd.DataFrame({
        "股票代码": ["002209", "000001"], "股票简称": ["达意隆", "平安银行"],
        "解禁时间": [pd.Timestamp("2026-11-09"), pd.Timestamp("2026-12-20")],
        "限售股类型": ["股权激励限售股份", "首发原股东限售股份"],
        "解禁数量": [112000.0, 1000.0], "实际解禁市值": [1309280.0, 1.0],
        "占解禁前流通市值比例": [0.0007, 0.01],
    })
    rows = sync.parse_unlock(df, "2026-11-01", "2026-11-30")
    assert len(rows) == 1
    assert rows[0]["code"] == "002209"
    assert rows[0]["event_type"] == "unlock"
    assert rows[0]["event_date"] == "2026-11-09"
    assert "股权激励" in rows[0]["title"]


def test_parse_dividend_skips_nat():
    df = pd.DataFrame({
        "代码": ["600519", "601899"], "名称": ["贵州茅台", "紫金矿业"],
        "除权除息日": [pd.Timestamp("2026-11-11"), pd.NaT],
        "股权登记日": [pd.Timestamp("2026-11-10"), pd.NaT],
        "现金分红-现金分红比例": [238.82, None],
        "方案进度": ["实施", "预披露"],
    })
    rows = sync.parse_dividend(df, "2026-11-01", "2026-11-30")
    assert [r["code"] for r in rows] == ["600519"]


def test_parse_disclosure_uses_latest_change_and_skips_disclosed():
    df = pd.DataFrame({
        "股票代码": ["600519", "300750", "000001"],
        "股票简称": ["贵州茅台", "宁德时代", "平安银行"],
        "首次预约": [pd.Timestamp("2026-11-10"), pd.Timestamp("2026-11-13"),
                     pd.Timestamp("2026-11-12")],
        "初次变更": [pd.NaT, pd.Timestamp("2026-11-15"), pd.NaT],
        "二次变更": [pd.NaT, pd.NaT, pd.NaT],
        "三次变更": [pd.NaT, pd.NaT, pd.NaT],
        "实际披露": [pd.NaT, pd.NaT, pd.Timestamp("2026-11-12")],
    })
    rows = sync.parse_disclosure(df, "2026-11-01", "2026-11-30", period="2026三季")
    assert [r["event_date"] for r in rows] == ["2026-11-10", "2026-11-15"]


def test_parse_meeting_and_forecast():
    meet = pd.DataFrame({
        "代码": ["600372"], "简称": ["中航机载"],
        "股东大会名称": ["2026年第3次临时股东大会"],
        "召开开始日": [pd.Timestamp("2026-11-24")],
        "股权登记日": [pd.Timestamp("2026-11-17")],
        "提案": ["1、关于审议增补公司非独立董事的议案"],
    })
    rows = sync.parse_meeting(meet, "2026-11-01", "2026-11-30")
    assert rows[0]["event_type"] == "meeting" and rows[0]["event_date"] == "2026-11-24"

    fore = pd.DataFrame({
        "股票代码": ["688827"], "股票简称": ["钶锐锶"],
        "公告日期": [pd.Timestamp("2026-11-04")],
        "预告类型": ["预增"], "业绩变动": ["预计增长 37%~57%"], "业绩变动幅度": [47.77],
    })
    rows = sync.parse_forecast(fore, "2026-11-01", "2026-11-30")
    assert rows[0]["event_type"] == "forecast" and "预增" in rows[0]["title"]


def test_event_id_is_stable_and_distinct():
    a = sync.event_id_for("600519", "unlock", "2026-11-09", "k1")
    b = sync.event_id_for("600519", "unlock", "2026-11-09", "k1")
    c = sync.event_id_for("600519", "unlock", "2026-11-09", "k2")
    assert a == b and a != c and len(a) == 16
