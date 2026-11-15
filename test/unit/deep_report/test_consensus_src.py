"""研报一致预期数据源单测（离线，注入 search_fn）。"""
from __future__ import annotations

import json

from tools.fetcher.consensus_src import fetch_research_consensus

_VALID = {
    "data": {"status_code": 0, "data": [
        {"title": "银行业深度报告：红利强盾", "summary": "维持买入评级，目标价 11.2 元",
         "publish_date": "2026-10-28 00:00:00", "url": "http://x",
         "extra": {"organization": "东莞证券", "author": "吴晓彤"}},
        {"title": "银行行业深度：估值修复", "summary": "增持",
         "publish_date": "2026-10-20", "url": "http://y",
         "extra": {"organization": "开源证券", "author": "刘呈祥"}},
    ]}
}


class TestFetch:
    def test_parses_org_date_rating_target(self):
        rows = fetch_research_consensus(
            "银行板块", search_fn=lambda q, ch, n: json.dumps(_VALID, ensure_ascii=False))
        assert len(rows) == 2
        assert rows[0]["org"] == "东莞证券" and rows[0]["date"] == "2026-10-28"
        assert rows[0]["rating"] == "买入" and rows[0]["target"] == "11.2"
        assert rows[1]["rating"] in ("买入", "中性", "减持", "未标注")

    def test_error_payload_is_empty(self):
        rows = fetch_research_consensus(
            "银行板块", search_fn=lambda q, ch, n: json.dumps({"error": "invalid_json_response"}))
        assert rows == []

    def test_non_json_is_empty(self):
        rows = fetch_research_consensus("银行板块", search_fn=lambda q, ch, n: "<html>quota</html>")
        assert rows == []

    def test_raises_is_empty(self):
        def boom(q, ch, n):
            raise RuntimeError("gateway down")
        assert fetch_research_consensus("银行板块", search_fn=boom) == []

    def test_empty_keyword(self):
        assert fetch_research_consensus("") == []


class TestRatingAndDays:
    def _rows(self, dates):
        return {"data": {"data": [
            {"title": "银行深度", "summary": text, "publish_date": d, "url": "u",
             "extra": {"organization": o}} for (text, d, o) in dates]}}

    def test_rating_conservative_and_negative(self):
        import json
        payload = json.dumps(self._rows([
            ("维持谨慎推荐", "2099-01-01", "A"),
            ("不推荐", "2099-01-01", "B"),
            ("强烈推荐", "2099-01-01", "C"),
        ]), ensure_ascii=False)
        rows = fetch_research_consensus("银行", search_fn=lambda q, ch, n: payload)
        assert [r["rating"] for r in rows] == ["中性", "减持", "买入"]

    def test_days_filters_old_reports(self):
        import json
        payload = json.dumps(self._rows([
            ("买入", "2000-01-01", "OLD"),
            ("买入", "2099-01-01", "NEW"),
        ]), ensure_ascii=False)
        rows = fetch_research_consensus("银行", days=30,
                                        search_fn=lambda q, ch, n: payload)
        assert [r["org"] for r in rows] == ["NEW"]


    def test_all_old_keeps_latest_with_stale_flag(self):
        """问财常返回 1–2 年前的深度报告：不能当成「没有研报」直接丢弃。"""
        import json
        payload = json.dumps({"data": {"data": [
            {"title": "银行深度", "summary": "买入", "publish_date": "2001-01-01",
             "url": "u", "extra": {"organization": "A"}},
            {"title": "银行年度", "summary": "买入", "publish_date": "2003-01-01",
             "url": "u", "extra": {"organization": "B"}},
        ]}}, ensure_ascii=False)
        rows = fetch_research_consensus("银行", days=30, search_fn=lambda q, ch, n: payload)
        assert len(rows) == 2 and all(r.get("stale") for r in rows)
        assert [r["org"] for r in rows] == ["B", "A"]   # 最新在前
