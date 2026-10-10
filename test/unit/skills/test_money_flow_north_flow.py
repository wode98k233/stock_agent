"""北向资金返回结构回归测试（2026-09-21「数据缺失」排查）。

线上现象（今世缘 run，日志 `logs/2026-09-21/2026-09-21-20-30-*.log`）：
`get_north_fund_flow` 返回的是**宽表**（指标为行、日期为列），
`tail(10).to_dict('records')` 得到
`{"date": "上证指数涨跌幅(%)", "2026-09-21": "0.97", ...}` ——
结构不可读 + 体积 55KB（被上下文截断后彻底不可用），
报告只能写「北向个股净买入数据缺失（工具仅返回大盘指数序列）」。

修复后契约：统一「一行一交易日」长表，限制条数与指标数，并显式标注 scope=market。
"""
import importlib.util
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _load_money_flow_module():
    """按文件路径加载技能 main.py（tools/skills.py 与 tools/skills/ 同名，不能直接 import）。"""
    path = Path(__file__).resolve().parents[3] / "tools" / "skills" / "money_flow" / "main.py"
    spec = importlib.util.spec_from_file_location("money_flow_main_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def skill_cls():
    return _load_money_flow_module().MoneyFlowSkill


def _wide_frame(indicators=("上证指数涨跌幅(%)", "北向资金净买入(亿元)", "北向资金成交额(亿元)"),
                dates=("2026-09-18", "2026-09-19", "2026-09-21")):
    """妙想源的实际形态：列名是日期，行是指标。"""
    data = {d: [round(0.1 * i, 2) for i in range(len(indicators))] for d in dates}
    return pd.DataFrame(data, index=list(indicators)).rename_axis("date").reset_index()


def test_wide_frame_is_transposed_to_one_row_per_date(skill_cls):
    records = skill_cls._north_flow_records(_wide_frame(), limit=20)
    assert len(records) == 3
    assert [r["date"] for r in records] == ["2026-09-18", "2026-09-19", "2026-09-21"]
    # 关键回归：不能再出现 "date": "上证指数涨跌幅(%)" 这种把指标名塞进 date 的畸形结构
    assert all(r["date"].count("-") == 2 for r in records)
    assert "北向资金净买入(亿元)" in records[-1]


def test_wide_frame_drops_non_fund_labels(skill_cls):
    """指数涨跌幅这类非资金标签应被过滤掉（避免模型误当北向数据用）。"""
    records = skill_cls._north_flow_records(_wide_frame(), limit=20)
    assert all("上证指数涨跌幅(%)" not in r for r in records)


def test_wide_frame_is_bounded(skill_cls):
    many_dates = tuple(f"2026-08-{d:02d}" for d in range(1, 29))
    records = skill_cls._north_flow_records(_wide_frame(dates=many_dates), limit=20)
    assert len(records) == 20
    assert records[-1]["date"] == "2026-08-28"


def test_wide_frame_limits_indicator_count(skill_cls):
    indicators = tuple(f"北向资金净买入-指标{i}(亿元)" for i in range(30))
    records = skill_cls._north_flow_records(_wide_frame(indicators=indicators), limit=5)
    assert records, "不应为空"
    assert all(len(r) - 1 <= 8 for r in records), "单日指标数应被限制在 8 个以内"


def test_long_frame_passthrough(skill_cls):
    df = pd.DataFrame({
        "日期": pd.date_range("2026-09-01", periods=30, freq="D"),
        "净买额": range(30),
    })
    records = skill_cls._north_flow_records(df, limit=10)
    assert len(records) == 10
    assert records[-1]["净买额"] == 29


def test_nan_and_numpy_scalars_are_json_safe(skill_cls):
    """NaN 必须剔除、numpy 标量必须转原生类型（否则工具序列化会炸 / 输出 null）。"""
    df = _wide_frame()
    df.loc[1, "2026-09-19"] = np.nan           # 资金指标缺失一天
    df.loc[0, "2026-09-18"] = np.int64(7)      # numpy 标量
    records = skill_cls._north_flow_records(df, limit=20)
    for r in records:
        for v in r.values():
            assert not isinstance(v, np.generic), f"未转原生类型: {type(v)}"
            assert not (isinstance(v, float) and np.isnan(v)), "NaN 应被剔除"


def test_empty_frame_returns_empty_list(skill_cls):
    assert skill_cls._north_flow_records(pd.DataFrame(), limit=20) == []
    assert skill_cls._north_flow_records(None, limit=20) == []


# ── 口径自检（2026-09-21 实测：源返回了「年度合计」而非日频净买入）──

@pytest.mark.parametrize("dates,expected", [
    (["2024-12-31(年)", "2025-12-31(年)"], "yearly"),
    (["2026-09-18", "2026-09-19"], "daily"),
    (["2025-12-31(年)", "2026-09-19"], "mixed"),
])
def test_granularity_is_labelled(skill_cls, monkeypatch, dates, expected):
    """工具必须在 note/granularity 里说清是日频还是年度合计，避免被误用。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "tools" / "skills" / "money_flow" / "main.py"
    spec = importlib.util.spec_from_file_location("mf_granularity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    skill = module.MoneyFlowSkill(logging.getLogger("t"))

    df = pd.DataFrame({"date": dates, "成交额(合计)": ["255.2万亿"] * len(dates)})
    monkeypatch.setattr(module.MoneyFlowSkill, "_safe_frame",
                        staticmethod(lambda fn, *a: df))
    monkeypatch.setattr(skill, "_get_north_fund_flow", lambda symbol: df)
    monkeypatch.setattr("utils.cache.get_fund_flow_cache", lambda k: None)
    monkeypatch.setattr("utils.cache.set_fund_flow_cache", lambda k, v: None)

    payload = json.loads(skill.get_north_fund_flow("北向资金"))
    assert payload["granularity"] == expected
    if expected == "yearly":
        assert "年度合计" in payload["note"]
    elif expected == "mixed":
        assert "混用" in payload["note"]
