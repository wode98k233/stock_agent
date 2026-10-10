"""工具输出压缩语料库（eval/tool_corpus.py）单元测试。"""
import json
import sqlite3

import pytest

from eval.tool_corpus import (
    build_records,
    classify_record,
    compute_metrics,
    dedup_records,
    extract_llm_output,
    extract_leaf_values,
    extract_numbers,
    extract_stock_codes,
    extract_tool_name_from_compress_input,
    load_corpus,
    parse_judge_result,
    parse_tool_args,
    select_records,
    split_records,
    token_recall,
    value_recall,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        create table steps (
            id integer primary key autoincrement,
            run_id text,
            event_run_id text,
            parent_run_id text,
            step_type text,
            step_name text,
            input text,
            output text,
            status text default 'completed',
            error text,
            started_at text,
            finished_at text,
            duration_ms real,
            extra text
        )
        """
    )
    return conn


def _compress_input(tool: str, raw: str) -> str:
    escaped = json.dumps(raw, ensure_ascii=False)
    msg = (
        f"content='工具: {tool}\\n\\n输出内容:\\n{escaped}' "
        "additional_kwargs={} response_metadata={}"
    )
    return json.dumps([[msg]], ensure_ascii=False)


def _llm_message(content: str) -> str:
    return json.dumps([{"role": "ai", "content": content}], ensure_ascii=False)


def _insert_tool(conn, run_id, tool, raw, query="", started_at="2026-06-08T10:00:00"):
    conn.execute(
        "insert into steps (run_id, step_type, step_name, input, output, started_at) values (?,?,?,?,?,?)",
        (run_id, "tool", tool, json.dumps({"query": query}, ensure_ascii=False), raw, started_at),
    )


def _insert_compress(conn, run_id, tool, raw, summary, started_at="2026-06-08T10:00:10"):
    conn.execute(
        "insert into steps (run_id, step_type, step_name, input, output, started_at, extra) values (?,?,?,?,?,?,?)",
        (
            run_id,
            "llm",
            "TokenCompatibleChatOpenAI",
            _compress_input(tool, raw),
            _llm_message(summary),
            started_at,
            json.dumps({"node": "react-tool-compress", "label": "工具输出压缩"}, ensure_ascii=False),
        ),
    )


class TestParsers:
    def test_parse_tool_args_json(self):
        assert parse_tool_args('{"query": "茅台 最新价"}') == {"query": "茅台 最新价"}

    def test_parse_tool_args_invalid(self):
        assert parse_tool_args("not a dict") == {}
        assert parse_tool_args("") == {}

    def test_extract_tool_name_from_compress_input(self):
        raw = '{"query": "x"}'
        text = _compress_input("mx_search_news", raw)
        assert extract_tool_name_from_compress_input(text) == "mx_search_news"

    def test_extract_tool_name_from_plain_repr(self):
        text = "工具: mx_data_query\\n\\n输出内容:\\n{\\\"query\\\": 1}"
        assert extract_tool_name_from_compress_input(text) == "mx_data_query"

    def test_extract_llm_output_json_message(self):
        assert extract_llm_output(_llm_message("摘要内容")) == "摘要内容"

    def test_extract_llm_output_plain_text(self):
        assert extract_llm_output("裸文本") == "裸文本"
        assert extract_llm_output("") == ""


class TestBuildRecords:
    def test_pairing_verified(self):
        conn = _make_db()
        raw = '{"query": "贵州茅台 最新价", "result": "数据"}'
        _insert_tool(conn, "run-1", "mx_search_news", raw, query="贵州茅台 最新价")
        _insert_compress(conn, "run-1", "mx_search_news", raw, "压缩摘要")

        records, stats = build_records(conn)

        assert len(records) == 1
        record = records[0]
        assert record["tool"] == "mx_search_news"
        assert record["query"] == "贵州茅台 最新价"
        assert record["raw_output"] == raw
        assert record["llm_compressed"] == "压缩摘要"
        assert record["pairing"] == "verified"
        assert stats["verified"] == 1
        assert stats["unmatched"] == 0

    def test_pairing_nearest_when_content_mismatch(self):
        conn = _make_db()
        raw = '{"query": "A", "result": "真实输出"}'
        other = '{"query": "B", "result": "另一个输出"}'
        _insert_tool(conn, "run-1", "mx_search_news", raw)
        _insert_compress(conn, "run-1", "mx_search_news", other, "压缩摘要")

        records, stats = build_records(conn)

        assert len(records) == 1
        assert records[0]["pairing"] == "nearest"
        assert records[0]["llm_compressed"] == "压缩摘要"
        assert stats["nearest"] == 1

    def test_pairing_skips_other_run(self):
        conn = _make_db()
        raw = '{"query": "A"}'
        _insert_tool(conn, "run-2", "mx_search_news", raw)
        _insert_compress(conn, "run-1", "mx_search_news", raw, "摘要")

        records, stats = build_records(conn)

        assert len(records) == 1
        assert "llm_compressed" not in records[0]
        assert stats["unmatched"] == 1

    def test_multiple_tools_same_run_pair_by_name(self):
        conn = _make_db()
        news = '{"query": "新闻", "result": "N"}'
        xg = '{"query": "选股", "stocks": [1]}'
        _insert_tool(conn, "run-1", "mx_search_news", news, started_at="2026-06-08T10:00:00")
        _insert_tool(conn, "run-1", "mx_xuangu_filter", xg, started_at="2026-06-08T10:00:05")
        _insert_compress(conn, "run-1", "mx_xuangu_filter", xg, "选股摘要", started_at="2026-06-08T10:00:10")

        records, _ = build_records(conn)

        by_tool = {r["tool"]: r for r in records}
        assert "llm_compressed" not in by_tool["mx_search_news"]
        assert by_tool["mx_xuangu_filter"]["llm_compressed"] == "选股摘要"

    def test_dedup_prefers_llm_baseline(self):
        first = {"tool": "t", "raw_md5": "x", "raw_output": "raw", "raw_len": 3, "id": "t-1"}
        second = {"tool": "t", "raw_md5": "x", "raw_output": "raw", "raw_len": 3, "id": "t-2", "llm_compressed": "sum"}

        records, duplicates = dedup_records([first, second])

        assert duplicates == 1
        assert len(records) == 1
        assert records[0]["llm_compressed"] == "sum"


class TestClassify:
    def test_categories(self):
        assert classify_record({"raw_output": "x", "llm_compressed": "s"}) == "compressed"
        assert classify_record({"raw_output": ""}) == "empty"
        assert classify_record({"raw_output": "[]"}) == "empty"
        assert classify_record({"raw_output": '{"status": "failed"}'}) == "error"
        assert classify_record({"raw_output": "执行失败: timeout"}) == "error"
        assert classify_record({"raw_output": "x" * 100}) == "small"
        assert classify_record({"raw_output": "x" * 2000}) == "large"


class TestSelectAndSplit:
    def _records(self, n, tool="t"):
        return [
            {"id": f"{tool}-{i}", "tool": tool, "raw_output": "x" * (600 + i), "raw_len": 600 + i,
             "raw_md5": f"m{i}", "query": f"q{i}"}
            for i in range(n)
        ]

    def test_select_respects_quotas(self):
        records = self._records(50)
        selected = select_records(records, quotas={"large": 10, "small": -1, "compressed": -1, "error": -1, "empty": -1})
        assert len(selected) == 10
        assert all(r["category"] == "large" for r in selected)

    def test_select_zero_quota_means_all(self):
        records = self._records(50)
        selected = select_records(records, quotas={"large": 0, "small": -1, "compressed": -1, "error": -1, "empty": -1})
        assert len(selected) == 50

    def test_select_keeps_largest(self):
        records = self._records(50)
        selected = select_records(records, quotas={"large": 6, "small": -1, "compressed": -1, "error": -1, "empty": -1})
        assert max(r["raw_len"] for r in selected) == 649

    def test_split_hash_deterministic(self):
        records = self._records(40)
        first = split_records(records, ratio=0.8, mode="hash", seed=42)
        labels = {r["id"]: r["split"] for r in first}

        again = split_records(self._records(40), ratio=0.8, mode="hash", seed=42)
        assert {r["id"]: r["split"] for r in again} == labels

    def test_split_hash_grouped_by_query(self):
        records = self._records(40)
        for i, record in enumerate(records):
            record["query"] = f"2026-06-08 查询主题{chr(97 + i % 10)}"
        split_records(records, ratio=0.8, mode="hash", seed=42)

        by_query = {}
        for record in records:
            by_query.setdefault(record["query"], set()).add(record["split"])
        assert all(len(splits) == 1 for splits in by_query.values())

    def test_split_temporal(self):
        records = self._records(10)
        for i, record in enumerate(records):
            record["started_at"] = f"2026-06-{i + 1:02d}T10:00:00"
        split_records(records, ratio=0.8, mode="temporal")
        assert sum(1 for r in records if r["split"] == "train") == 8
        assert sum(1 for r in records if r["split"] == "test") == 2

    def test_split_none(self):
        records = self._records(3)
        split_records(records, mode="none")
        assert {r["split"] for r in records} == {"all"}


class TestExportLoad:
    def test_export_and_load_roundtrip(self, tmp_path):
        conn = _make_db()
        raw = '{"query": "茅台", "result": "数据"}'
        _insert_tool(conn, "run-1", "mx_search_news", raw, query="茅台")
        _insert_compress(conn, "run-1", "mx_search_news", raw, "摘要")
        records, _ = build_records(conn)
        for record in records:
            record["category"] = classify_record(record)
        split_records(records, ratio=0.8, mode="hash", seed=1)

        from eval.tool_corpus import export_corpus

        manifest = export_corpus(records, str(tmp_path))
        assert manifest["records"] == 1
        assert manifest["format"] == "jsonl.gz"
        assert (tmp_path / "manifest.json").exists()
        assert list(tmp_path.glob("mx_search_news.*.jsonl.gz"))

        loaded = load_corpus(str(tmp_path))
        assert len(loaded) == 1
        assert loaded[0]["raw_output"] == raw
        assert loaded[0]["llm_compressed"] == "摘要"

    def test_export_plain_and_cleanup_stale(self, tmp_path):
        from eval.tool_corpus import export_corpus

        stale = tmp_path / "mx_search_news.train.jsonl"
        stale.write_text("{}\n", encoding="utf-8")
        records = [
            {"id": "t-1", "tool": "t", "raw_output": "raw", "raw_len": 3, "raw_md5": "m",
             "query": "", "category": "small", "split": "train"}
        ]

        manifest = export_corpus(records, str(tmp_path), gzip_output=False)

        assert manifest["format"] == "jsonl"
        assert not stale.exists()
        assert (tmp_path / "t.train.jsonl").exists()
        assert load_corpus(str(tmp_path))[0]["raw_output"] == "raw"


class TestComparisonCsv:
    def _row(self, **overrides):
        row = {
            "id": "mx_search_news-1", "tool": "mx_search_news", "split": "test", "category": "compressed",
            "scrubbed": False,
            "query": "北向资金", "raw_len": 100, "rule_len": 80, "llm_len": 10,
            "skip_llm": True, "raw_output": "RAW-CONTENT", "rule_output": "RULE-CONTENT",
            "llm_compressed": "LLM-CONTENT",
        }
        row.update(overrides)
        return row

    def test_full_contents_and_headers(self, tmp_path):
        from eval.tool_corpus import write_comparison_csv

        path = tmp_path / "compare.csv"
        write_comparison_csv([self._row()], str(path))

        text = path.read_text(encoding="utf-8-sig")
        assert "已脱敏(LLM基线不可比)" in text
        assert "原始工具调用结果" in text
        assert "规则压缩结果" in text
        assert "LLM压缩结果" in text
        assert "RAW-CONTENT" in text and "RULE-CONTENT" in text and "LLM-CONTENT" in text
        assert len(text.splitlines()) == 2

    def test_rule_changed_flag(self, tmp_path):
        from eval.tool_corpus import write_comparison_csv
        import csv

        path = tmp_path / "compare.csv"
        write_comparison_csv(
            [self._row(rule_output="RAW-CONTENT"), self._row(rule_output=None)],
            str(path),
        )
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["规则有变化"] == "False"
        assert rows[1]["规则有变化"] == "False"
        assert rows[1]["规则压缩结果"] == ""


class TestMetrics:
    def test_token_helpers(self):
        assert extract_numbers("价格 1,234.5 元，涨 3.2%") == {"1234.5", "3.2%"}
        assert extract_stock_codes("600519.SH 和 000001") == {"600519", "000001"}
        assert token_recall({"a", "b"}, {"b"}) == 0.5
        assert token_recall(set(), {"b"}) is None

    def test_extract_leaf_values_filters_noise(self):
        raw = json.dumps(
            {"a": "x", "b": 1.5, "c": 0, "d": None, "e": "y" * 600, "f": ["z"]},
            ensure_ascii=False,
        )
        values = extract_leaf_values(raw)
        assert "x" in values and "1.5" in values and "z" in values
        assert "0" not in values and "None" not in values
        assert not any(len(value) > 500 for value in values)
        assert extract_leaf_values("{bad json") == set()

    def test_value_recall(self):
        assert value_recall({"x", "z"}, "包含 x 的输出") == 0.5
        assert value_recall(set(), "anything") is None

    def test_compute_metrics_with_compressor(self):
        records = [
            {
                "id": "t-1",
                "tool": "mx_search_news",
                "query": "测试",
                "tool_input": {"query": "测试"},
                "raw_output": "600519 价格 1700 元，涨幅 3.2%" * 40,
                "llm_compressed": "600519 价格 1700 元",
                "llm_len": 16,
            }
        ]

        def stub(tool, args, content):
            assert tool == "mx_search_news"
            assert args == {"query": "测试"}
            return content[:100]

        metrics = compute_metrics(records, compressor=stub, threshold=3000)

        summary = metrics["summary"]["mx_search_news"]
        assert summary["n"] == 1
        assert summary["rule_n"] == 1
        assert summary["rule_ratio"] == pytest.approx(100 / len(records[0]["raw_output"]))
        assert summary["rule_skip_rate"] == 1.0
        assert summary["llm_num_recall"] == pytest.approx(2 / 3)

    def test_compute_metrics_compressor_exception_falls_back(self):
        records = [
            {
                "id": "t-1",
                "tool": "t",
                "query": "",
                "tool_input": {},
                "raw_output": "raw",
            }
        ]

        def broken(tool, args, content):
            raise RuntimeError("boom")

        metrics = compute_metrics(records, compressor=broken)
        assert metrics["summary"]["t"]["errors"] == 1
        assert metrics["rows"][0]["rule_output"] == "raw"


class TestScrub:
    def _record(self, tool="get_positions"):
        raw = json.dumps(
            {
                "positions": [
                    {
                        "stock_code": "510300",
                        "stock_name": "沪深300ETF",
                        "qty": 2000.0,
                        "avg_cost": 3.85,
                        "source": "eastmoney",
                    }
                ]
            },
            ensure_ascii=False,
        )
        return {
            "id": f"{tool}-1",
            "tool": tool,
            "query": "查询沪深300ETF 510300 持仓",
            "tool_input": {"query": "查询沪深300ETF 510300 持仓"},
            "raw_output": raw,
            "raw_len": len(raw),
            "raw_md5": "md5",
            "llm_compressed": "沪深300ETF(510300)：2000股，成本3.85",
            "llm_len": 25,
        }

    def test_scrub_sensitive_record(self):
        from eval.tool_corpus import scrub_record

        scrubbed = scrub_record(self._record())

        assert scrubbed["scrubbed"] is True
        assert "沪深300ETF" not in scrubbed["raw_output"]
        assert "510300" not in scrubbed["raw_output"]
        assert scrubbed["query"] == "【已脱敏查询】"
        assert scrubbed["tool_input"]["query"] == "【已脱敏查询】"
        assert '"qty": 2000.0' not in scrubbed["raw_output"]
        assert '"source": "eastmoney"' in scrubbed["raw_output"]
        assert scrubbed["raw_len"] == len(scrubbed["raw_output"])
        assert scrubbed["raw_md5"] != "md5"

    def test_scrub_is_deterministic(self):
        from eval.tool_corpus import scrub_record

        first = scrub_record(self._record())
        second = scrub_record(self._record())
        assert first["raw_output"] == second["raw_output"]
        assert first["llm_compressed"] == second["llm_compressed"]

    def test_scrub_llm_output_names(self):
        from eval.tool_corpus import scrub_record

        scrubbed = scrub_record(self._record())
        assert "沪深300ETF" not in scrubbed["llm_compressed"]
        assert "示例标的" in scrubbed["llm_compressed"]

    def test_scrub_skips_non_sensitive(self):
        from eval.tool_corpus import scrub_record

        record = self._record(tool="mx_search_news")
        assert scrub_record(record) is record


class TestJudgeParsing:
    def test_parse_plain_json(self):
        result = parse_judge_result('{"retained": true, "missing": [], "comment": "ok"}')
        assert result["retained"] is True
        assert result["comment"] == "ok"

    def test_parse_code_fence(self):
        result = parse_judge_result('```json\n{"retained": false, "missing": ["价格"]}\n```')
        assert result["retained"] is False
        assert result["missing"] == ["价格"]

    def test_parse_garbage(self):
        result = parse_judge_result("完全不是 JSON")
        assert result["retained"] is None
