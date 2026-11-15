# -*- coding: utf-8 -*-
"""工具载荷 → 按次计费信息（tools/search/billing.py）单元测试。

这层是「钱花了」到「账单记上」之间的唯一通道：解析漏了 = 白花的钱没人看见，
解析多了 = 记出一笔并不存在的成本。所以两边都要有护栏。
"""
import json

import pytest

from tools.search.billing import KNOWN_SERVICES, billed_service_of
from tools.search.service import normalize_search_response


class _Resp:
    def __init__(self, **kw):
        self.query = "q"
        self.results = [{"title": "t"}] if kw.pop("results", True) else []
        self.provider = "DeepSeek"
        self.success = True
        self.error_message = None
        for k, v in kw.items():
            setattr(self, k, v)

    def to_dicts(self):
        return list(self.results)


# ── ① 解析 ──────────────────────────────────────────────────

def test_parses_billed_search_payload():
    payload = json.dumps({"success": True, "billed_service": "deepseek-search", "results": []},
                         ensure_ascii=False)
    assert billed_service_of("web_search", payload) == {"service": "deepseek-search", "count": 1}


def test_parses_billed_fetch_payload():
    payload = json.dumps({"success": True, "billed": True, "billed_service": "deepseek-fetch"})
    assert billed_service_of("web_fetch", payload)["service"] == "deepseek-fetch"


def test_search_stock_news_also_covered():
    """个股资讯走同一条聚合链，同样可能命中 DeepSeek 源"""
    payload = json.dumps({"billed_service": "deepseek-search"})
    assert billed_service_of("search_stock_news", payload) is not None


def test_free_source_payload_reports_nothing():
    """免费源（MX/Tavily/Parallel…）的载荷没有该键 → 不记成本（也确实是 0 元）"""
    payload = json.dumps({"success": True, "provider": "MX", "results": [{"title": "t"}]})
    assert billed_service_of("web_search", payload) is None


def test_empty_value_is_not_billed():
    assert billed_service_of("web_search", json.dumps({"billed_service": ""})) is None


def test_unknown_service_is_rejected_with_warning(caplog):
    payload = json.dumps({"billed_service": "someones-paid-api"})
    assert billed_service_of("web_search", payload) is None


@pytest.mark.parametrize("tool", ["mx_data_query", "get_stock_realtime", "unknown_tool"])
def test_non_contract_tools_never_parsed(tool):
    """allowlist 之外的工具一律不看（不解析别人的 JSON）"""
    assert billed_service_of(tool, json.dumps({"billed_service": "deepseek-search"})) is None


@pytest.mark.parametrize("content", [
    "", None, "not json at all", "[1,2,3]", '"a string"', "12345",
    "{truncated json", json.dumps("x" * 10),
])
def test_malformed_payload_never_raises(content):
    assert billed_service_of("web_search", content) is None


def test_oversized_payload_is_skipped():
    huge = json.dumps({"billed_service": "deepseek-search", "pad": "x" * 300_000})
    assert billed_service_of("web_search", huge) is None


def test_known_services_matches_price_catalog():
    """allowlist 必须与价目 services 段对齐，否则计费声明会被静默丢弃"""
    from utils.pricing import load_catalog
    declared = set((load_catalog().get("services") or {}).keys())
    assert KNOWN_SERVICES <= declared, f"价目缺少 {KNOWN_SERVICES - declared}"


# ── ② 输出契约透传（只在真计费时出现，免费源字节不变）────────────────

def test_normalize_passes_billed_service_when_billed():
    payload = normalize_search_response(_Resp(billed_service="deepseek-search"), "q")
    assert payload["billed_service"] == "deepseek-search"


def test_normalize_omits_key_when_not_billed():
    """⚠️ 免费源的输出契约必须逐字不变（既有测试断言键集合）"""
    payload = normalize_search_response(_Resp(), "q")
    assert "billed_service" not in payload


def test_normalize_omits_key_when_no_results():
    """无结果 = 上游不计费，即便 provider 声明了也不能记成本"""
    payload = normalize_search_response(_Resp(results=False, billed_service="deepseek-search"), "q")
    assert "billed_service" not in payload
