"""工具输出规则压缩（agents/react/tool_compressor.py）单元测试。"""
import asyncio
import json

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from agents.react import tool_compressor


def _xuangu_payload(query="今日涨幅居前个股"):
    return json.dumps(
        {
            "query": query,
            "stock_count": 2,
            "stocks": [
                {
                    "序号": "1",
                    "代码": "301669",
                    "名称": "N高特",
                    "市场代码简称": "SZ",
                    "最新价(元) 2026.06.09": "51.30",
                    "涨跌幅(%) 2026.06.09": "624.58",
                    "概念": "储能概念、融资融券",
                    "主营产品": "【储能设备】【锂电池】",
                    "东财行业总分类": "电力设备-其他电源设备",
                    "CHOICE_INNER_CODE": "123456",
                    "市盈率(动)(倍) 2026.06.09": "398.34",
                    "总市值(元) 2026.06.09": "246.24亿",
                },
                {
                    "序号": "2",
                    "代码": "920394",
                    "名称": "民士达",
                    "市场代码简称": "BJ",
                    "最新价(元) 2026.06.09": "76.71",
                    "涨跌幅(%) 2026.06.09": "29.99",
                    "概念": "专精特新、大飞机",
                    "主营产品": "【芳纶纸】",
                    "东财行业总分类": "轻工制造-造纸",
                    "CHOICE_INNER_CODE": "654321",
                    "市盈率(动)(倍) 2026.06.09": "91.72",
                    "总市值(元) 2026.06.09": "112.19亿",
                },
            ],
        },
        ensure_ascii=False,
    )


class TestPreprocess:
    def test_fix_number_breaks(self):
        raw = "北向资金净流出118亿元。6013.\n08传媒-2.\n19-11.\n79"
        fixed = tool_compressor.preprocess(raw)
        assert "6013.08" in fixed
        assert fixed.count(".\n") == 0

    def test_idempotent(self):
        once = tool_compressor.preprocess("价格1.\n23元")
        assert tool_compressor.preprocess(once) == once

    def test_plain_text_unchanged(self):
        text = "正常文本 123.45 元，无断行"
        assert tool_compressor.preprocess(text) == text


class TestXuanguRule:
    def test_unknown_tool_returns_original(self):
        content = '{"foo": 1}'
        assert tool_compressor.compress("unknown_tool", {}, content) == content

    def test_malformed_json_returns_original(self):
        content = "{not json"
        assert tool_compressor.compress("mx_xuangu_filter", {}, content) == content

    def test_empty_stocks_returns_original(self):
        content = json.dumps({"query": "x", "stocks": []}, ensure_ascii=False)
        assert tool_compressor.compress("mx_xuangu_filter", {}, content) == content

    def test_core_columns_only_by_default(self):
        out = tool_compressor.compress("mx_xuangu_filter", {"query": "涨幅榜"}, _xuangu_payload())
        assert "| 代码 |" in out and "| 名称 |" in out
        assert "最新价" in out and "涨跌幅" in out
        assert "概念" not in out
        assert "主营产品" not in out
        assert "东财行业总分类" not in out
        assert "CHOICE_INNER_CODE" not in out
        assert "序号" not in out

    def test_query_adaptive_keeps_extra_columns(self):
        out = tool_compressor.compress(
            "mx_xuangu_filter", {"query": "今日涨幅前10的概念板块"}, _xuangu_payload()
        )
        assert "| 概念 |" in out
        assert "| 东财行业总分类 |" in out

    def test_all_rows_and_values_preserved(self):
        out = tool_compressor.compress("mx_xuangu_filter", {"query": "涨幅榜"}, _xuangu_payload())
        for token in ["301669", "N高特", "51.30", "624.58", "920394", "民士达", "246.24亿"]:
            assert token in out
        assert out.count("| 301669 |") == 1
        assert out.count("| 920394 |") == 1

    def test_header_date_suffix_stripped(self):
        out = tool_compressor.compress("mx_xuangu_filter", {"query": "涨幅榜"}, _xuangu_payload())
        assert "2026.06.09" not in out
        assert "最新价(元)" in out

    def test_deterministic(self):
        first = tool_compressor.compress("mx_xuangu_filter", {"query": "涨幅榜"}, _xuangu_payload())
        second = tool_compressor.compress("mx_xuangu_filter", {"query": "涨幅榜"}, _xuangu_payload())
        assert first == second

    def test_pipe_in_value_escaped(self):
        payload = json.dumps(
            {"stocks": [{"代码": "000001", "名称": "A|B", "最新价(元) 2026.06.09": "10"}]},
            ensure_ascii=False,
        )
        out = tool_compressor.compress("mx_xuangu_filter", {}, payload)
        assert "A\\|B" in out

    def test_query_aware_metric_columns(self):
        payload = json.dumps(
            {"stocks": [{
                "代码": "601169", "名称": "北京银行",
                "最新价(元) 2026.09.03": "5.62",
                "股息率(TTM)(%) 2026.09.03": "7.36",
                "ROE(%) 2026.06.30": "5.30",
                "分红次数 2025": "1",
                "主营业务": "银行",
            }]},
            ensure_ascii=False,
        )
        out = tool_compressor.compress(
            "mx_xuangu_filter",
            {"query": "A股银行股 股息率大于4% ROE 近3年分红情况"},
            payload,
        )
        assert "股息率(TTM)" in out
        assert "ROE(%)" in out
        assert "分红次数" in out
        assert "主营业务" not in out


class TestMode:
    def test_disabled_returns_off(self, monkeypatch):
        from config import Config

        monkeypatch.setattr(Config, "TOOL_RULE_COMPRESS_ENABLED", False, raising=False)
        monkeypatch.setattr(Config, "TOOL_RULE_COMPRESS_POLICY", "", raising=False)
        assert tool_compressor.get_mode("mx_xuangu_filter") == tool_compressor.MODE_OFF

    def test_policy_modes(self, monkeypatch):
        from config import Config

        monkeypatch.setattr(Config, "TOOL_RULE_COMPRESS_ENABLED", True, raising=False)
        monkeypatch.setattr(
            Config, "TOOL_RULE_COMPRESS_POLICY",
            '{"default": "off", "mx_xuangu_filter": "shadow"}', raising=False,
        )
        assert tool_compressor.get_mode("mx_xuangu_filter") == tool_compressor.MODE_SHADOW
        assert tool_compressor.get_mode("other_tool") == tool_compressor.MODE_OFF

    def test_invalid_policy_returns_off(self, monkeypatch):
        from config import Config

        monkeypatch.setattr(Config, "TOOL_RULE_COMPRESS_ENABLED", True, raising=False)
        monkeypatch.setattr(Config, "TOOL_RULE_COMPRESS_POLICY", "{bad json", raising=False)
        assert tool_compressor.get_mode("mx_xuangu_filter") == tool_compressor.MODE_OFF


class TestShadowLog:
    def test_event_written(self, monkeypatch, tmp_path):
        import utils.app_paths

        monkeypatch.setattr(utils.app_paths, "get_logs_dir", lambda: str(tmp_path))
        tool_compressor.log_shadow_event(
            "mx_xuangu_filter", {"query": "涨幅榜"}, tool_compressor.MODE_SHADOW,
            raw_len=8000, rule_len=2000, final_len=900, llm_used=True, threshold=3000,
            raw_content="RAW", rule_content="RULE", final_content="LLM",
        )
        path = tmp_path / tool_compressor.SHADOW_LOG_FILENAME
        assert path.exists()
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["tool"] == "mx_xuangu_filter"
        assert record["rule_would_skip_llm"] is True
        assert record["llm_used"] is True

        details = list(tmp_path.glob(f"{tool_compressor.SHADOW_DETAIL_PREFIX}-*.jsonl"))
        assert len(details) == 1
        detail = json.loads(details[0].read_text(encoding="utf-8").strip())
        assert detail["raw"] == "RAW"
        assert detail["rule"] == "RULE"
        assert detail["final"] == "LLM"
        assert detail["content_truncated"] is False

    def test_detail_content_truncated(self, monkeypatch, tmp_path):
        import utils.app_paths

        monkeypatch.setattr(utils.app_paths, "get_logs_dir", lambda: str(tmp_path))
        long_text = "x" * (tool_compressor.SHADOW_DETAIL_CONTENT_LIMIT + 100)
        tool_compressor.log_shadow_event(
            "mx_search_news", {"query": "q"}, tool_compressor.MODE_SHADOW,
            raw_len=len(long_text), rule_len=10, final_len=10, llm_used=False, threshold=3000,
            raw_content=long_text,
        )
        detail_path = list(tmp_path.glob(f"{tool_compressor.SHADOW_DETAIL_PREFIX}-*.jsonl"))[0]
        detail = json.loads(detail_path.read_text(encoding="utf-8").strip())
        assert len(detail["raw"]) == tool_compressor.SHADOW_DETAIL_CONTENT_LIMIT
        assert detail["content_truncated"] is True


class TestPipelineIntegration:
    def _make_node(self, mock_logger):
        from agents.executor_callbacks import ExecutionState
        from agents.react.nodes import PerToolRateLimiter, ToolNode

        return ToolNode(
            exec_state=ExecutionState(),
            tools=[_fake_xuangu],
            logger=mock_logger,
            rate_limiter=PerToolRateLimiter(min_interval_seconds=0),
        )

    def test_on_mode_uses_rule_output_and_skips_llm(self, monkeypatch, mock_logger, tmp_path):
        from unittest.mock import AsyncMock
        from config import Config
        import utils.app_paths

        monkeypatch.setattr(Config, "TOOL_RULE_COMPRESS_ENABLED", True, raising=False)
        monkeypatch.setattr(
            Config, "TOOL_RULE_COMPRESS_POLICY",
            '{"default": "off", "fake_xuangu": "on"}', raising=False,
        )
        monkeypatch.setattr(Config, "TOOL_OUTPUT_COMPRESS_THRESHOLD", 3000, raising=False)
        monkeypatch.setattr(Config, "TOOL_COMPRESS_THRESHOLDS", "", raising=False)
        monkeypatch.setattr(utils.app_paths, "get_logs_dir", lambda: str(tmp_path))
        monkeypatch.setitem(tool_compressor.RULE_COMPRESSORS, "fake_xuangu",
                            lambda content, args: "RULE_OUT")
        llm_mock = AsyncMock(return_value="LLM_OUT")
        monkeypatch.setattr("agents.react.tool_node_base._compress_tool_output", llm_mock)

        async def run():
            node = self._make_node(mock_logger)
            return await node({
                "messages": [AIMessage(content="", tool_calls=[
                    {"name": "fake_xuangu", "args": {"query": "涨幅榜"}, "id": "call-1"},
                ])],
                "tool_calls_count": 0,
            })

        result = asyncio.run(run())
        message = result["messages"][0]
        assert message.content == "RULE_OUT"
        llm_mock.assert_not_called()

    def test_shadow_mode_keeps_original_and_logs(self, monkeypatch, mock_logger, tmp_path):
        from unittest.mock import AsyncMock
        from config import Config
        import utils.app_paths

        monkeypatch.setattr(Config, "TOOL_RULE_COMPRESS_ENABLED", True, raising=False)
        monkeypatch.setattr(
            Config, "TOOL_RULE_COMPRESS_POLICY",
            '{"default": "off", "fake_xuangu": "shadow"}', raising=False,
        )
        monkeypatch.setattr(Config, "TOOL_OUTPUT_COMPRESS_THRESHOLD", 100000, raising=False)
        monkeypatch.setattr(Config, "TOOL_COMPRESS_THRESHOLDS", "", raising=False)
        monkeypatch.setitem(tool_compressor.RULE_COMPRESSORS, "fake_xuangu",
                            lambda content, args: "RULE_OUT")
        monkeypatch.setattr(utils.app_paths, "get_logs_dir", lambda: str(tmp_path))
        llm_mock = AsyncMock(return_value="LLM_OUT")
        monkeypatch.setattr("agents.react.tool_node_base._compress_tool_output", llm_mock)

        async def run():
            node = self._make_node(mock_logger)
            return await node({
                "messages": [AIMessage(content="", tool_calls=[
                    {"name": "fake_xuangu", "args": {"query": "涨幅榜"}, "id": "call-1"},
                ])],
                "tool_calls_count": 0,
            })

        result = asyncio.run(run())
        message = result["messages"][0]
        assert "RULE_OUT" not in message.content
        assert '"stocks"' in message.content
        llm_mock.assert_not_called()
        path = tmp_path / tool_compressor.SHADOW_LOG_FILENAME
        assert path.exists()
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["mode"] == "shadow"
        assert record["rule_len"] == len("RULE_OUT")

    def test_on_mode_lossy_rule_falls_back_to_llm(self, monkeypatch, mock_logger, tmp_path):
        from unittest.mock import AsyncMock
        from config import Config
        import utils.app_paths

        monkeypatch.setattr(Config, "TOOL_RULE_COMPRESS_ENABLED", True, raising=False)
        monkeypatch.setattr(
            Config, "TOOL_RULE_COMPRESS_POLICY",
            '{"default": "off", "fake_xuangu": "on"}', raising=False,
        )
        monkeypatch.setattr(Config, "TOOL_OUTPUT_COMPRESS_THRESHOLD", 300, raising=False)
        monkeypatch.setattr(Config, "TOOL_COMPRESS_THRESHOLDS", "", raising=False)
        monkeypatch.setattr(utils.app_paths, "get_logs_dir", lambda: str(tmp_path))
        monkeypatch.setitem(tool_compressor.RULE_COMPRESSORS, "fake_xuangu",
                            lambda content, args: "有损输出…(另5列)")
        llm_mock = AsyncMock(return_value="LLM_OUT")
        monkeypatch.setattr("agents.react.tool_node_base._compress_tool_output", llm_mock)

        async def run():
            node = self._make_node(mock_logger)
            return await node({
                "messages": [AIMessage(content="", tool_calls=[
                    {"name": "fake_xuangu", "args": {"query": "涨幅榜"}, "id": "call-1"},
                ])],
                "tool_calls_count": 0,
            })

        result = asyncio.run(run())
        assert result["messages"][0].content == "LLM_OUT"
        llm_mock.assert_called_once()
        detail_path = list(tmp_path.glob(f"{tool_compressor.SHADOW_DETAIL_PREFIX}-*.jsonl"))[0]
        detail = json.loads(detail_path.read_text(encoding="utf-8").strip())
        assert detail["rule_lossy"] is True


def _news_payload():
    result = (
        "搜索结果: 共找到 12 条相关资讯，过滤后展示 10 条:\n\n"
        "--- 1. 主力资金流向日报 ---\n"
        "日期: 2026-06-08 | 类型: 新闻\n\n"
        "今日主力资金净流出990.\n80亿元，银行行业净流入13.\n08亿元。\n"
        "--- 2. 另一条很长的新闻 ---\n"
        "日期: 2026-06-09 | 类型: INV_NEWS\n\n"
        + "这是一段很长的正文内容。" * 40
    )
    return json.dumps({"query": "北向资金", "result": result}, ensure_ascii=False)


def _iwc_payload():
    return json.dumps(
        {
            "query": "指数行情",
            "skill_id": "hithink-market-query",
            "data": {
                "QTime": 668,
                "query": "指数行情",
                "columns": [
                    {"index_name": "全量指数@指数代码", "key": "指数代码", "type": "STR", "source": "index"},
                    {"index_name": "全量指数@最新价", "key": "最新价", "type": "DOUBLE", "source": "index"},
                    {"index_name": "全量指数@空字段", "key": "空字段", "type": "STR", "source": "index"},
                ],
                "datas": [
                    {"指数代码": "399006.SZ", "指数简称": "创业板指", "最新价": "4194.21", "空字段": ""},
                    {"指数代码": "000001.SH", "指数简称": "上证指数", "最新价": "3000.00", "空字段": ""},
                ],
                "row_count": 2,
                "token": "abc",
                "chunks_info": "x",
            },
        },
        ensure_ascii=False,
    )


class TestNewsRule:
    def test_digest_structure_and_number_fix(self):
        out = tool_compressor.compress("mx_search_news", {"query": "北向资金"}, _news_payload())
        assert len(out) < len(_news_payload())
        assert "[1] 主力资金流向日报 (2026-06-08 新闻)" in out
        assert "[2] 另一条很长的新闻" in out
        assert "990.80" in out
        assert ".\n" not in out
        assert "…" in out

    def test_no_result_returns_original(self):
        payload = json.dumps({"query": "x", "result": ""}, ensure_ascii=False)
        assert tool_compressor.compress("mx_search_news", {}, payload) == payload


class TestIwcRule:
    def test_table_and_metadata_dropped(self):
        raw = _iwc_payload()
        out = tool_compressor.compress("iwc_query", {}, raw)
        assert len(out) < len(raw)
        assert "| 指数代码 |" in out and "| 最新价 |" in out
        assert "399006.SZ" in out and "4194.21" in out
        assert "指数简称" in out and "创业板指" in out
        assert "全量指数@" not in out
        assert '"token"' not in out
        assert "空字段" not in out

    def test_nonzero_status_returns_original(self):
        payload = json.loads(_iwc_payload())
        payload["data"]["status_code"] = 1
        payload["data"]["status_msg"] = "查询失败"
        raw = json.dumps(payload, ensure_ascii=False)
        assert tool_compressor.compress("iwc_query", {}, raw) == raw


class TestPositionsRule:
    def test_table_and_account(self):
        payload = json.dumps(
            {
                "positions": [
                    {"stock_code": "510300", "stock_name": "沪深300ETF", "qty": 2000, "avg_cost": 3.85,
                     "realized_pnl": 0.0, "source": "eastmoney", "avail_qty": 2000, "updated_at": "2026-09-07"},
                ],
                "account": {"total_asset": 100000.0, "avail_cash": 35000.0},
                "meta": {"note": "同步自东财"},
            },
            ensure_ascii=False,
        )
        out = tool_compressor.compress("get_positions", {}, payload)
        assert "# 持仓 1 只" in out
        assert "510300" in out and "沪深300ETF" in out and "3.85" in out
        assert "updated_at" not in out and "source" not in out
        assert "total_asset=100000.0" in out
        assert "备注: 同步自东财" in out


class TestPlainRowsRules:
    def test_hot_stocks_drops_degenerate(self):
        payload = json.dumps(
            [
                {"rank": 1, "code": "600127", "name": "金健米业", "price": 0.0,
                 "change_pct": 0.0, "heat": "6101294", "rank_change": 0, "rank_trend": "flat"},
                {"rank": 2, "code": "600869", "name": "远东股份", "price": 0.0,
                 "change_pct": 0.0, "heat": "3310410", "rank_change": 0, "rank_trend": "flat"},
            ],
            ensure_ascii=False,
        )
        out = tool_compressor.compress("get_hot_stocks", {}, payload)
        assert "6101294" in out and "flat" in out
        assert "price" not in out and "change_pct" not in out

    def test_limit_up_pool_keeps_reason(self):
        payload = json.dumps(
            [
                {"code": "000759", "name": "中百集团", "change_pct": 10.0354, "price": 9.32,
                 "amount": 0.0, "turnover_rate": 0.0, "seal_amount": 181085110.0,
                 "consecutive_boards": 4, "limit_up_time": "09:44", "limit_up_reason": "硬折扣"},
            ],
            ensure_ascii=False,
        )
        out = tool_compressor.compress("get_limit_up_pool", {}, payload)
        assert "consecutive_boards" in out and "硬折扣" in out
        assert "| amount |" not in out and "| turnover_rate |" not in out
        assert "seal_amount" in out

    def test_etf_history_keeps_ohlcv(self):
        payload = json.dumps(
            [{"date": "2026-06-10", "open": 0.39, "high": 0.393, "low": 0.386,
              "close": 0.39, "volume": 60517102.0, "amount": 2354490709.0}],
            ensure_ascii=False,
        )
        out = tool_compressor.compress("get_etf_history", {}, payload)
        assert "| date | open | high | low | close | volume | amount |" in out
        assert "0.386" in out and "60517102.0" in out

    def test_time_series_rendered_desc(self):
        """时间序列倒序渲染：硬截断只会砍最旧的行（避免砍掉最新交易日）。"""
        payload = json.dumps(
            [
                {"date": "2026-09-15", "open": 1.2, "high": 1.21, "low": 1.19, "close": 1.2, "volume": 1.0, "amount": 2.0},
                {"date": "2026-09-16", "open": 1.19, "high": 1.2, "low": 1.18, "close": 1.19, "volume": 1.1, "amount": 2.1},
                {"date": "2026-09-17", "open": 1.18, "high": 1.19, "low": 1.17, "close": 1.18, "volume": 1.2, "amount": 2.2},
            ],
            ensure_ascii=False,
        )
        out = tool_compressor.compress("get_etf_history", {}, payload)
        assert "按时间倒序" in out
        assert 0 < out.find("2026-09-17") < out.find("2026-09-15")

    def test_empty_list_returns_original(self):
        raw = "[]"
        assert tool_compressor.compress("get_limit_break_pool", {}, raw) == raw


class TestLianbanRule:
    def test_group_by_board_num(self):
        payload = json.dumps(
            [{"date": "2026-09-10", "boards": [
                {"code": "002377", "name": "国创高新", "board_num": 2},
                {"code": "600359", "name": "新农开发", "board_num": 2},
                {"code": "000001", "name": "平安银行", "board_num": 3},
            ]}],
            ensure_ascii=False,
        )
        out = tool_compressor.compress("get_lianban_ladder", {}, payload)
        assert "# 连板天梯 3 只" in out
        assert "3板(1): 平安银行(000001)" in out
        assert "2板(2): 国创高新(002377), 新农开发(600359)" in out


class TestIwcSearchRule:
    def test_title_and_budget_summary(self):
        payload = json.dumps(
            {"query": "公告", "data": {"data": [
                {"title": "业绩预亏公告", "summary": "一、本期业绩预告情况。" * 40, "url": "https://x.com/a.pdf"},
            ]}},
            ensure_ascii=False,
        )
        out = tool_compressor.compress("iwc_search", {}, payload)
        assert "[1] 业绩预亏公告" in out
        assert "https://x.com" not in out
        assert "…" in out


def _mx_data_payload(table_count=1, cols=20, rows=3):
    def table_block(ti):
        header = "| " + " | ".join(["date"] + [f"t{ti}c{i}" for i in range(cols)]) + " |"
        separator = "|" + "---|" * (cols + 1)
        body = "\n".join(
            "| " + " | ".join([f"t{ti}r{r}"] + [f"t{ti}v{r}_{i}" for i in range(cols)]) + " |"
            for r in range(rows)
        )
        return f"**表{ti}标题(2026-06-09)** (前20行预览):\n\n{header}\n{separator}\n{body}\n"
    terminal = "\n".join(["**查询证券**:", "- 测试 (000001) - A股", "", f"**查询结果**: {table_count} 个表，共 {rows} 行数据", ""])
    terminal += "\n" + "\n".join(table_block(ti) for ti in range(table_count))
    return json.dumps(
        {
            "query": "测试",
            "tables_count": table_count,
            "total_rows": rows,
            "tables": [{"sheet_name": f"表{ti}", "fieldnames": [], "rows": []} for ti in range(table_count)],
            "terminal_output": terminal,
        },
        ensure_ascii=False,
    )


class TestMxDataQueryRule:
    def test_wide_table_truncated_columns(self):
        raw = _mx_data_payload(table_count=1, cols=20, rows=3)
        out = tool_compressor.compress("mx_data_query", {}, raw)
        assert len(out) < len(raw)
        assert "…(另5列)" in out
        assert "t0c14" in out and "t0c15" not in out
        assert "t0v0_14" in out and "t0v0_15" not in out
        assert "t0r2" in out

    def test_multi_table_capped(self):
        raw = _mx_data_payload(table_count=7, cols=4, rows=2)
        out = tool_compressor.compress("mx_data_query", {}, raw)
        assert "表0标题" in out and "表4标题" in out
        assert "其余 2 个表已省略" in out
        assert "表5标题" in out and "表6标题" in out
        assert "t5v0_0" not in out and "t6v0_0" not in out

    def test_narrow_table_unchanged(self):
        raw = _mx_data_payload(table_count=1, cols=4, rows=2)
        assert tool_compressor.compress("mx_data_query", {}, raw) == raw

    def test_no_terminal_returns_original(self):
        raw = json.dumps({"query": "x", "error": "失败", "status": "failed"}, ensure_ascii=False)
        assert tool_compressor.compress("mx_data_query", {}, raw) == raw


class TestLossy:
    def test_markers_detected(self):
        assert tool_compressor.is_lossy_output("mx_data_query", "| a | b |…(另5列)|") is True
        assert tool_compressor.is_lossy_output("mx_data_query", "（其余 2 个表已省略：x；y）") is True
        assert tool_compressor.is_lossy_output("mx_search_news", "[1] 标题\n正文") is False
        assert tool_compressor.is_lossy_output("mx_data_query", "") is False


class TestRegistry:
    def test_expected_tools_registered(self):
        expected = {
            "mx_xuangu_filter", "mx_search_news", "mx_data_query", "iwc_query", "iwc_search",
            "get_positions", "get_hot_stocks", "get_limit_up_pool",
            "get_limit_break_pool", "get_etf_history", "get_lianban_ladder",
        }
        assert expected <= set(tool_compressor.RULE_COMPRESSORS)


@tool("fake_xuangu")
async def _fake_xuangu(query: str) -> str:
    """假选股工具（返回预置 JSON）。"""
    return _xuangu_payload(query)
