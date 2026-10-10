"""`_trim_tool_output` 对 mx_data_query 的处理回归测试。

背景（2026-09-21 实测 run 0353cd31）：原实现对 `mx_data_query` 直接 `data.pop("tables")`，
只留 `terminal_output`——而后者来自 `format_terminal()`，**只渲染第一张表的前 20 行预览**。
实测一次查询 `tables` 有 230 行、terminal_output 仅渲染 7 行；技术指标表 17 行只给 2 行，
报告因此写「均线、MACD 未返回」「份额未返回」（数据其实取到了，是在这里被丢的）。

改为把 `tables` 渲染成**紧凑 markdown**（每表最多 N 行 / 最多 M 张表，超出显式标注），
体积交给下游的硬截断（保留两头）+ LLM 压缩（分三类）处理。
"""
import json

import pytest

from agents.react.utils import _trim_tool_output

pytestmark = pytest.mark.unit


def _mx_payload(n_rows: int, n_tables: int = 1) -> dict:
    rows = [{"date": "2026-09-%02d" % (i % 28 + 1), "MA5": 1.4 + i * 0.01, "MACD": 0.01 * i}
            for i in range(n_rows)]
    return {
        "query": "159570 技术指标 MA5 MA10 MACD",
        "tables_count": n_tables,
        "total_rows": n_rows * n_tables,
        "tables": [
            {"sheet_name": "表%d" % t, "fieldnames": ["date", "MA5", "MACD"], "rows": rows}
            for t in range(n_tables)
        ],
        "terminal_output": "**查询结果**: 1 个表\n\n**表0** (前20行预览):\n| date |\n| --- |\n| 2026-09-01 |",
    }


class TestMxDataQueryTrim:
    def test_tables_not_dropped(self):
        out = _trim_tool_output("mx_data_query", json.dumps(_mx_payload(17), ensure_ascii=False))
        d = json.loads(out)
        assert "tables" in d, "不得再丢弃 tables（否则 LLM 只能看到 terminal_output 的前 20 行预览）"

    def test_rows_beyond_terminal_preview_are_kept(self):
        """terminal_output 只预览 2 行；trim 后必须能看到全部 17 行。"""
        d = json.loads(_trim_tool_output("mx_data_query", json.dumps(_mx_payload(17), ensure_ascii=False)))
        rendered = str(d["tables"])
        assert "2026-09-01" in rendered            # 预览里有的
        assert "2026-09-17" in rendered            # 预览里没有的（第 17 行）
        assert rendered.count("| 2026-09-") == 17  # 17 行明细全在

    def test_markdown_rendering_is_compact(self):
        """渲染应为 markdown 表格，且明显小于原始嵌套 JSON。"""
        raw = json.dumps(_mx_payload(120), ensure_ascii=False)
        out = _trim_tool_output("mx_data_query", raw)
        d = json.loads(out)
        assert d["tables"].startswith("### ")
        assert "| date | MA5 | MACD |" in d["tables"]
        assert len(out) < len(raw)

    def test_rows_capped_with_explicit_marker(self):
        """超过单表行上限时截断，并显式标注省略了多少行（时序表 → 省略较早）。"""
        from agents.react.utils import _TRIM_MX_MAX_ROWS_PER_TABLE

        d = json.loads(_trim_tool_output(
            "mx_data_query", json.dumps(_mx_payload(_TRIM_MX_MAX_ROWS_PER_TABLE + 30), ensure_ascii=False)))
        assert "已省略较早" in d["tables"]

    def test_tables_capped_with_explicit_marker(self):
        from agents.react.utils import _TRIM_MX_MAX_TABLES

        d = json.loads(_trim_tool_output(
            "mx_data_query", json.dumps(_mx_payload(3, n_tables=_TRIM_MX_MAX_TABLES + 5), ensure_ascii=False)))
        assert "已省略后" in d["tables"]

    def test_meta_fields_kept(self):
        d = json.loads(_trim_tool_output("mx_data_query", json.dumps(_mx_payload(5), ensure_ascii=False)))
        assert d["tables_count"] == 1
        assert d["total_rows"] == 5
        assert d["query"]

    def test_non_dict_and_other_tools_untouched(self):
        # 非 dict（列表）原样返回
        assert _trim_tool_output("mx_data_query", "[1,2,3]") == "[1,2,3]"
        # 其他工具只做「解析→重新序列化」，内容等价（空白会变，这是原有行为）
        assert json.loads(_trim_tool_output("some_other_tool", '{"a":1}')) == {"a": 1}


def _monotonic_series(n: int, ascending: bool) -> dict:
    dates = ["2025-%02d-%02d" % (m, d) for m in range(1, 13) for d in range(1, 25)][:n]
    if not ascending:
        dates = list(reversed(dates))
    rows = [{"date": dates[i], "close": 1.0 + i * 0.01} for i in range(n)]
    return {
        "query": "kline", "tables_count": 1, "total_rows": n,
        "tables": [{"sheet_name": "K线", "fieldnames": ["date", "close"], "rows": rows}],
        "terminal_output": "",
    }


class TestMxTableRowCapKeepsRecent:
    def test_ascending_keeps_recent_tail(self):
        """升序(早→新) 超上限时保留**最近**的行（尾部），省略较早的。"""
        from agents.react.utils import _TRIM_MX_MAX_ROWS_PER_TABLE as CAP

        payload = _monotonic_series(CAP + 30, ascending=True)
        rendered = json.loads(_trim_tool_output(
            "mx_data_query", json.dumps(payload, ensure_ascii=False)))["tables"]
        rows = payload["tables"][0]["rows"]
        assert rows[-1]["date"] in rendered        # 最近保留
        assert rows[0]["date"] not in rendered      # 最早省略
        assert "已省略较早" in rendered

    def test_descending_keeps_recent_head(self):
        """降序(新→早) 超上限时也保留**最近**的行（头部），省略较早的。"""
        from agents.react.utils import _TRIM_MX_MAX_ROWS_PER_TABLE as CAP

        payload = _monotonic_series(CAP + 30, ascending=False)
        rendered = json.loads(_trim_tool_output(
            "mx_data_query", json.dumps(payload, ensure_ascii=False)))["tables"]
        rows = payload["tables"][0]["rows"]
        assert rows[0]["date"] in rendered          # 最新保留
        assert rows[-1]["date"] not in rendered      # 最早省略
        assert "已省略较早" in rendered

    def test_non_time_table_keeps_head(self):
        """无日期列（榜单/成分股）超上限时保留前 N 行（头部即最重要）。"""
        from agents.react.utils import _TRIM_MX_MAX_ROWS_PER_TABLE as CAP

        rows = [{"code": "600%03d" % i, "name": "股%d" % i} for i in range(CAP + 20)]
        payload = {"query": "rank", "tables_count": 1, "total_rows": len(rows),
                   "tables": [{"sheet_name": "榜单", "fieldnames": ["code", "name"], "rows": rows}],
                   "terminal_output": ""}
        rendered = json.loads(_trim_tool_output(
            "mx_data_query", json.dumps(payload, ensure_ascii=False)))["tables"]
        assert rows[0]["code"] in rendered
        assert rows[-1]["code"] not in rendered
        assert "已省略其余" in rendered
