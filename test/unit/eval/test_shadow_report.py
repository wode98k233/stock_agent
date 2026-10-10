"""影子对照报告（eval/shadow_report.py）单元测试。"""
import json

from eval.shadow_report import (
    load_details,
    load_stats,
    render_markdown,
    render_stats_only_markdown,
    write_csv,
)


def _detail_record(tool="mx_search_news", ts="2026-09-11T10:00:00+00:00", **overrides):
    record = {
        "ts": ts,
        "date": "20260911",
        "tool": tool,
        "mode": "shadow",
        "query": "测试查询",
        "raw_len": 3000,
        "rule_len": 2000,
        "final_len": 900,
        "llm_used": True,
        "rule_would_skip_llm": True,
        "threshold": 3000,
        "raw": "原始内容",
        "rule": "规则压缩内容",
        "final": "LLM 压缩内容",
        "content_truncated": False,
    }
    record.update(overrides)
    return record


def _write_detail(log_dir, date, records):
    path = log_dir / f"tool_compress_shadow_detail-{date}.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class TestLoadDetails:
    def test_loads_and_sorts(self, tmp_path):
        _write_detail(tmp_path, "20260910", [
            _detail_record(ts="2026-09-10T10:00:00+00:00", query="早"),
        ])
        _write_detail(tmp_path, "20260911", [
            _detail_record(ts="2026-09-11T10:00:00+00:00", query="晚"),
            _detail_record(tool="iwc_query", ts="2026-09-11T09:00:00+00:00", query="中"),
        ])

        records = load_details(str(tmp_path))
        assert [r["query"] for r in records] == ["早", "中", "晚"]

    def test_filters(self, tmp_path):
        _write_detail(tmp_path, "20260911", [
            _detail_record(tool="mx_search_news"),
            _detail_record(tool="iwc_query"),
        ])
        _write_detail(tmp_path, "20260910", [_detail_record(tool="mx_search_news")])

        assert len(load_details(str(tmp_path), tool="mx_search_news")) == 2
        assert len(load_details(str(tmp_path), tool="mx_search_news", date="20260911")) == 1
        assert len(load_details(str(tmp_path), date="20260910")) == 1

    def test_skips_bad_lines(self, tmp_path):
        path = tmp_path / "tool_compress_shadow_detail-20260911.jsonl"
        path.write_text("{bad json\n" + json.dumps(_detail_record(), ensure_ascii=False) + "\n", encoding="utf-8")
        assert len(load_details(str(tmp_path))) == 1


class TestRenderMarkdown:
    def test_table_and_details(self, tmp_path):
        _write_detail(tmp_path, "20260911", [_detail_record()])
        report = render_markdown(load_details(str(tmp_path)))

        assert "# 工具压缩影子对照报告" in report
        assert "| # | 工具 | 查询 | 原长 | LLM后长度 | 规则长度 | 规则可跳过LLM | 原始摘要 | LLM摘要 | 规则摘要 |" in report
        assert "mx_search_news" in report
        assert "**原始信息（前 2000 字）**" in report
        assert "**LLM 压缩后（前 2000 字）**" in report
        assert "**规则压缩后（前 2000 字）**" in report

    def test_limit_and_empty(self, tmp_path):
        _write_detail(tmp_path, "20260911", [
            _detail_record(query=f"q{i}") for i in range(5)
        ])
        report = render_markdown(load_details(str(tmp_path)), limit=2)
        assert "其余 3 条" in report

        empty = render_markdown([])
        assert "暂无数据" in empty


class TestWriteCsv:
    def test_csv_columns_and_content(self, tmp_path):
        path = tmp_path / "shadow.csv"
        write_csv([_detail_record()], str(path))

        content = path.read_text(encoding="utf-8-sig")
        header = content.splitlines()[0]
        assert "原始信息" in header and "LLM压缩后" in header and "规则压缩后" in header
        assert "规则压缩内容" in content


class TestStatsFallback:
    def test_load_stats_and_filters(self, tmp_path):
        stats = {
            "ts": "2026-09-10T08:58:34.620913+00:00",
            "tool": "mx_xuangu_filter",
            "mode": "shadow",
            "query": "石油产业链",
            "raw_len": 16937,
            "rule_len": 7896,
            "final_len": 1666,
            "llm_used": True,
            "rule_would_skip_llm": False,
        }
        (tmp_path / "tool_compress_shadow.jsonl").write_text(
            json.dumps(stats, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        assert len(load_stats(str(tmp_path))) == 1
        assert load_stats(str(tmp_path), tool="other") == []
        assert len(load_stats(str(tmp_path), date="20260910")) == 1
        assert load_stats(str(tmp_path), date="20260911") == []

    def test_render_stats_only(self):
        report = render_stats_only_markdown([{
            "ts": "t", "tool": "mx_search_news", "query": "q",
            "raw_len": 3000, "final_len": 900, "rule_len": 2000, "rule_would_skip_llm": True,
        }])
        assert "无明细内容" in report
        assert "重启" in report
        assert "mx_search_news" in report
