"""多期序列解析单测（全部离线；fixture 字符串按实测 payload 形态裁剪）。

实测形态来自 `scripts/devtools/probe_series_feasibility.py`：
iwc_query 的 `data.columns` 是**元数据字典**列表（key + unit + timestamp），
`data.datas` 是**行字典**列表（按列名取值）；「银行板块」类问句会返回多行标的。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from agents.deep_report.series import parse_wide_series


# ── fixture 构造 ──
def _col(key: str, unit: str = "点", **extra) -> dict:
    col = {"index_name": f"全量指数@{key}", "key": key, "feKey": key,
           "type": "DOUBLE", "unit": unit, "label": ""}
    col.update(extra)
    return col


def _wide_payload(columns, datas, skill_id="hithink-market-query") -> str:
    """iwc_query 的真实外壳：{query, skill_id, data:{columns, datas, ...}}。"""
    return json.dumps({
        "query": "中证银行指数近60个交易日的收盘点位",
        "skill_id": skill_id,
        "data": {"QTime": 87, "columns": columns, "datas": datas,
                 "row_count": len(datas), "code_count": len(datas)},
    }, ensure_ascii=False)


def _close_series(n: int = 60) -> str:
    """近 n 个交易日收盘价（列名 YYYYMMDD，最后一天 2026-10-30）。"""
    last = date(2026, 10, 30)
    days = [(last - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]
    cols = [_col("指数简称", ""), _col("最新价", "点")]
    cols += [_col(f"收盘价[{d}]") for d in days]
    row = {"指数简称": "中证银行", "最新价": "7962.26"}
    for i, d in enumerate(days):
        row[f"收盘价[{d}]"] = 7900.0 + i
    return _wide_payload(cols, [row])


# ── 列名模式 ──
class TestColumnPatterns:
    def test_yyyymmdd(self):
        cols = [_col("收盘价[20261030]"), _col("收盘价[20261029]")]
        payload = _wide_payload(cols, [{"收盘价[20261030]": 100.5,
                                        "收盘价[20261029]": 99.5}])
        items = parse_wide_series(payload, "收盘价")
        assert [it["period"] for it in items] == ["2026-10-29", "2026-10-30"]
        assert [it["value"] for it in items] == [99.5, 100.5]
        assert {it["metric"] for it in items} == {"收盘价"}
        assert {it["unit"] for it in items} == {"点"}

    def test_dashed_yyyymmdd(self):
        cols = [_col("市净率[2026-06-30]"), _col("市净率[2026-09-30]")]
        payload = _wide_payload(cols, [{"市净率[2026-06-30]": 0.55,
                                        "市净率[2026-09-30]": 0.64}], )
        items = parse_wide_series(payload, "市净率")
        assert [it["period"] for it in items] == ["2026-06-30", "2026-09-30"]
        assert items[0]["value"] == 0.55

    def test_parenthesised_date(self):
        cols = [_col("成交额(20261028)")]
        payload = _wide_payload(cols, [{"成交额(20261028)": 123.0}])
        assert parse_wide_series(payload, "成交额")[0]["period"] == "2026-10-28"

    def test_plain_string_columns(self):
        """列名退化成纯字符串（无元数据）也要能解析。"""
        payload = _wide_payload(["收盘价[20261030]", "收盘价[20261029]"],
                                [{"收盘价[20261030]": 2.0, "收盘价[20261029]": 1.0}])
        items = parse_wide_series(payload, "收盘价")
        assert [it["value"] for it in items] == [1.0, 2.0]
        assert items[0]["unit"] == ""

    def test_row_as_positional_list(self):
        """datas 是「按列位置」的列表（另一种工具口径）也要能对齐。"""
        cols = [_col("指数代号", ""), _col("收盘价[20261029]"), _col("收盘价[20261030]")]
        payload = _wide_payload(cols, [["399986.SZ", 11.0, 12.0]])
        items = parse_wide_series(payload, "收盘价")
        assert [it["value"] for it in items] == [11.0, 12.0]

    def test_invalid_calendar_date_skipped(self):
        cols = [_col("收盘价[20261332]"), _col("收盘价[20261030]")]
        stats: dict = {}
        payload = _wide_payload(cols, [{"收盘价[20261332]": 1.0, "收盘价[20261030]": 2.0}])
        items = parse_wide_series(payload, "收盘价", stats=stats)
        assert [it["period"] for it in items] == ["2026-10-30"]
        assert stats["skipped_columns"] == 1, "非法日期列必须留痕，不能静默少点"


# ── 排序 / 期数 ──
class TestOrdering:
    def test_ascending_and_full_length(self):
        items = parse_wide_series(_close_series(60), "收盘价")
        assert len(items) == 60
        periods = [it["period"] for it in items]
        assert periods == sorted(periods), "必须按日期升序（折线 x 轴）"

    def test_enough_points_for_series_shape(self):
        """chart_candidates 要求 ≥4 个不同 period，60 期足够。"""
        items = parse_wide_series(_close_series(10), "收盘价")
        assert len({it["period"] for it in items}) >= 4

    def test_duplicate_period_collapsed(self):
        cols = [_col("收盘价[20261030]"), _col("收盘价[20261030]")]
        payload = _wide_payload(cols, [{"收盘价[20261030]": 3.0}])
        assert len(parse_wide_series(payload, "收盘价")) == 1


# ── 数值清洗 / 脏数据 ──
class TestDirtyData:
    def test_commas_percent_and_scale_suffix(self):
        cols = [_col("成交额[20261028]", "元"), _col("净息差[20260930]", "%"),
                _col("营收[20260630]", "元")]
        payload = _wide_payload(cols, [{
            "成交额[20261028]": "41,223,795,000",
            "净息差[20260930]": "1.42%",
            "营收[20260630]": "2.3亿",
        }])
        stats: dict = {}
        items = parse_wide_series(payload, "成交额", stats=stats)
        assert items[0]["value"] == 41223795000.0
        # 「万」量级要换算到基准值，否则同列混量级画不出图
        yi = parse_wide_series(payload, "营收")[0]
        assert yi["value"] == pytest.approx(2.3e8)
        wan = _wide_payload([_col("营收[20260630]")], [{"营收[20260630]": "8336万"}])
        assert parse_wide_series(wan, "营收")[0]["value"] == pytest.approx(8336e4)
        pct = parse_wide_series(payload, "净息差")[0]
        assert pct["value"] == 1.42 and pct["unit"] == "%"

    def test_null_tokens_skipped_and_counted(self):
        cols = [_col(f"收盘价[2026102{i}]") for i in range(1, 6)]
        row = {"收盘价[20261021]": "—", "收盘价[20261022]": "N/A",
               "收盘价[20261023]": None, "收盘价[20261024]": "",
               "收盘价[20261025]": "100.0"}
        stats: dict = {}
        items = parse_wide_series(_wide_payload(cols, [row]), "收盘价", stats=stats)
        assert len(items) == 1 and items[0]["value"] == 100.0
        assert stats["skipped"] == 4, "解析失败必须计数，不能静默丢"

    def test_bool_and_nan_not_treated_as_numbers(self):
        cols = [_col("收盘价[20261024]"), _col("收盘价[20261025]")]
        stats: dict = {}
        payload = _wide_payload(cols, [{"收盘价[20261024]": True,
                                        "收盘价[20261025]": float("nan")}])
        assert parse_wide_series(payload, "收盘价", stats=stats) == []
        assert stats["skipped"] == 2

    def test_no_fill_zero(self):
        """取不到就是取不到：不得用 0 填充（报告口径红线）。"""
        cols = [_col("收盘价[20261024]")]
        items = parse_wide_series(_wide_payload(cols, [{"收盘价[20261024]": "—"}]), "收盘价")
        assert items == []


# ── 形态边界 ──
class TestShapes:
    def test_interval_aggregate_rejected(self):
        """`区间成交额[20260928-20261030]` 是区间合计单值，不是序列。

        这正是「近20个交易日的成交额」出不了折线的原因（要问「每日成交额」）。
        """
        cols = [_col("指数@区间成交额[20260928-20261030]", "元")]
        stats: dict = {}
        payload = _wide_payload(cols, [{"指数@区间成交额[20260928-20261030]": 8.8e11}])
        assert parse_wide_series(payload, "成交额", stats=stats) == []
        assert stats["dated_columns"] == 0

    def test_invalid_json(self):
        stats: dict = {}
        assert parse_wide_series("not-json", "收盘价", stats=stats) == []
        assert stats["error"] == "invalid_json"

    def test_empty_and_non_dict(self):
        assert parse_wide_series("[]", "收盘价") == []
        assert parse_wide_series("", "收盘价") == []

    def test_multiple_rows_picks_richest(self):
        """多标的 payload（实测「银行板块」返回 3 行）只取数值最全的那行。"""
        cols = [_col("指数简称", ""), _col("收盘价[20261029]"), _col("收盘价[20261030]")]
        payload = _wide_payload(cols, [
            {"指数简称": "空壳", "收盘价[20261029]": None, "收盘价[20261030]": None},
            {"指数简称": "中证银行", "收盘价[20261029]": 7918.19, "收盘价[20261030]": 7962.26},
        ])
        items = parse_wide_series(payload, "收盘价")
        assert [it["value"] for it in items] == [7918.19, 7962.26]

    def test_multi_metric_hint_selection(self):
        """一份 payload 同时有收盘价 + 成交额（实测「收盘点位和成交额」）。"""
        cols = [_col("收盘价[20261029]"), _col("收盘价[20261030]"),
                _col("成交额[20261029]", "元"), _col("成交额[20261030]", "元")]
        payload = _wide_payload(cols, [{
            "收盘价[20261029]": 7918.19, "收盘价[20261030]": 7962.26,
            "成交额[20261029]": 3.9e10, "成交额[20261030]": 4.1e10,
        }])
        picks = parse_wide_series(payload, "成交额")
        assert {it["metric"] for it in picks} == {"成交额"}
        assert [it["value"] for it in picks] == [3.9e10, 4.1e10]
        assert {it["metric"] for it in parse_wide_series(payload, "收盘价")} == {"收盘价"}

    def test_hint_miss_falls_back_to_largest_family(self):
        cols = [_col("收盘价[20261029]"), _col("收盘价[20261030]"),
                _col("成交量[20261029]", "股")]
        payload = _wide_payload(cols, [{"收盘价[20261029]": 1.0, "收盘价[20261030]": 2.0,
                                        "成交量[20261029]": 5.0}])
        items = parse_wide_series(payload, "净息差")
        assert {it["metric"] for it in items} == {"收盘价"}, "hint 不匹配取期数最多的一组"

    def test_no_hint_takes_largest(self):
        cols = [_col("收盘价[20261029]"), _col("收盘价[20261030]"),
                _col("成交量[20261029]", "股")]
        payload = _wide_payload(cols, [{"收盘价[20261029]": 1.0, "收盘价[20261030]": 2.0,
                                        "成交量[20261029]": 5.0}])
        assert {it["metric"] for it in parse_wide_series(payload, "")} == {"收盘价"}

    def test_flat_payload_without_data_wrapper(self):
        cols = [_col("收盘价[20261029]")]
        payload = json.dumps({"columns": cols, "datas": [{"收盘价[20261029]": 7.0}]},
                             ensure_ascii=False)
        assert parse_wide_series(payload, "收盘价")[0]["value"] == 7.0

    def test_metric_prefix_stripped(self):
        cols = [_col("指数@区间收盘价[20261029]")]
        payload = _wide_payload(cols, [{"指数@区间收盘价[20261029]": 1.0}])
        assert parse_wide_series(payload, "收盘价")[0]["metric"] == "区间收盘价"

    def test_accepts_already_parsed_dict(self):
        cols = [_col("收盘价[20261029]")]
        payload = {"columns": cols, "datas": [{"收盘价[20261029]": 7.0}]}
        assert parse_wide_series(payload, "收盘价")[0]["value"] == 7.0


# ── 长表（mx_data_query 口径） ──
class TestLongTable:
    """mx_data_query 的 tables 是「一行一日期」长表（按 mx_data.table_to_rows 口径）。"""

    def test_long_table_parsed(self):
        payload = json.dumps({
            "query": "招商银行近12个季度的净息差",
            "tables_count": 1, "total_rows": 3,
            "tables": [{"sheet_name": "净息差",
                        "fieldnames": ["date", "净息差"],
                        "rows": [{"date": "2026-03-31", "净息差": "1.38"},
                                 {"date": "2025-12-31", "净息差": "1.42"},
                                 {"date": "2025-09-30", "净息差": "1.45"}]}],
        }, ensure_ascii=False)
        items = parse_wide_series(payload, "净息差")
        assert [it["period"] for it in items] == ["2025-09-30", "2025-12-31", "2026-03-31"]
        assert [it["value"] for it in items] == [1.45, 1.42, 1.38]
        assert items[0]["source"] == "mx_data_query"

    def test_long_table_without_date_column_ignored(self):
        payload = json.dumps({"tables": [{"fieldnames": ["指标", "数值"],
                                          "rows": [{"指标": "PB", "数值": "0.7"}]}]},
                             ensure_ascii=False)
        assert parse_wide_series(payload, "PB") == []


# ── 来源名 ──
class TestSource:
    def test_source_inferred_iwc(self):
        assert parse_wide_series(_close_series(5), "收盘价")[0]["source"] == "iwc_query"

    def test_source_override(self):
        items = parse_wide_series(_close_series(5), "收盘价", source="iwc_query")
        assert {it["source"] for it in items} == {"iwc_query"}

    def test_source_never_empty(self):
        """items 的 source 为空会被 _norm_items 丢掉（卡片契约）。"""
        payload = _wide_payload([_col("收盘价[20261029]")], [{"收盘价[20261029]": 1.0}])
        assert parse_wide_series(payload, "收盘价")[0]["source"]
        bare = json.dumps({"columns": [_col("收盘价[20261029]")],
                           "datas": [{"收盘价[20261029]": 1.0}]}, ensure_ascii=False)
        assert parse_wide_series(bare, "收盘价")[0]["source"], "无外壳时也不能空"


# ── 统计口径 ──
class TestStats:
    def test_stats_fields(self):
        stats: dict = {}
        items = parse_wide_series(_close_series(12), "收盘价", stats=stats)
        assert stats["columns"] >= 12
        assert stats["dated_columns"] == 12
        assert stats["rows"] == 1
        assert stats["families"] == 1
        assert stats["picked"] == "收盘价"
        assert stats["periods"] == len(items) == 12
        assert stats["skipped"] == 0

    def test_stats_not_shared_between_calls(self):
        a: dict = {}
        b: dict = {}
        parse_wide_series(_close_series(4), "收盘价", stats=a)
        parse_wide_series("bad", "收盘价", stats=b)
        assert a.get("error") is None and b.get("error") == "invalid_json"

    def test_stats_optional(self):
        assert parse_wide_series(_close_series(4), "收盘价")


class TestCleanNumberEdgeCases:
    def test_fullwidth_and_negative_forms(self):
        from agents.deep_report.series import _clean_number
        assert _clean_number("１．５") == 1.5          # 全角 → 半角
        assert _clean_number("−1.2") == -1.2          # unicode 负号
        assert _clean_number("(1.2)") == -1.2         # 会计负数
        assert _clean_number("(1.2)亿") == -1.2e8     # 负数 + 量级
