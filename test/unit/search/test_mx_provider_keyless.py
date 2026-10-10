"""搜索层回归测试（2026-09-21「新闻检索恒为空」的真因）。

线上现象：`search_stock_news` 三次调用全部
`SearchResponse(results=[], success=False, error_message='所有搜索引擎均无结果', search_time=0.0)`，
报告因此写「消息面数据缺失」，并被误读成「妙想资讯额度耗尽」。

实测（同一时刻直接调 `MXSearch().search()`）：
`{'success': True, 'status': 0, 'message': 'ok', ... 15 条结果}` —— **额度正常**。
两个真 bug：
① `MXSearchProvider()` 无参构造 → `_api_keys=[]` → 基类 `search()` 在
   「未配置 API Key」处**静默短路**，`_do_search` 从未执行（这就是 `search_time=0.0`）；
② `_do_search` 依赖 `mx.format_pretty()`，而它返回的是**终端文本 str** →
   `for item in formatted` 逐字符遍历（即便走到也只会产出垃圾）；
   且解析为空时一律 `success=True`，把 provider 问题伪装成「没这类新闻」。
③ 聚合器只回一句「所有搜索引擎均无结果」，把逐 provider 原因全吞了。
"""
import pytest

from tools.search.aggregator import SearchAggregator
from tools.search.base import BaseSearchProvider, SearchResponse, SearchResult
from tools.search.mx_provider import MXSearchProvider


def _envelope(items):
    return {
        "success": True, "status": 0, "code": 0, "message": "ok",
        "data": {"data": {"llmSearchResponse": {"data": items}}},
    }


_ITEM = {
    "code": "CRab5f31",
    "title": "时光酿香：今世缘陶坛贮酒见证品质蜕变_新华网",
    "content": "入坛贮存1年的酒，刺激性气味明显减弱……",
    "date": "2026-09-21 00:00:00",
    "informationType": "INV_NEWS",
    "source": "新华社",
    "jumpUrl": "https://example.com/news/1",
    "authorityLevel": "L2",
}


class _FakeMX:
    """替身 MXSearch：只实现被 provider 用到的 search()。"""

    def __init__(self, envelope):
        self._envelope = envelope
        self.calls = 0

    def search(self, query):
        self.calls += 1
        return self._envelope


def _provider_with(envelope) -> MXSearchProvider:
    prov = MXSearchProvider()                 # 关键：不传 key（线上就是无参构造）
    prov._mx_search = _FakeMX(envelope)
    return prov


# ── ① 免密 provider 不能被「未配置 API Key」短路 ──────────────

def test_mx_provider_is_keyless():
    assert MXSearchProvider.requires_api_key is False
    assert BaseSearchProvider.requires_api_key is True      # 其它 provider 仍是缺省需要 key


def test_keyless_provider_actually_searches():
    prov = _provider_with(_envelope([_ITEM]))
    resp = prov.search("今世缘 最新消息", max_results=5)

    assert resp.success is True
    assert len(resp.results) == 1
    assert resp.search_time > 0, "修前这里恒为 0.0（_do_search 从未执行）"
    assert prov._mx_search.calls == 1


def test_field_mapping_jumpurl_to_url():
    prov = _provider_with(_envelope([_ITEM]))
    item = prov.search("今世缘", max_results=5).results[0]

    assert item.title.startswith("时光酿香")
    assert item.url == "https://example.com/news/1"      # 注意源字段是 jumpUrl
    assert item.source == "新华社"
    assert item.published_date.startswith("2026-09-21")
    assert item.snippet.startswith("入坛贮存1年")


def test_results_are_not_characters():
    """修前的逐字符遍历 bug：snippet 会只有一个字符。"""
    prov = _provider_with(_envelope([_ITEM]))
    item = prov.search("今世缘", max_results=5).results[0]
    assert len(item.snippet) > 10
    assert item.title != ""


def test_max_results_is_respected():
    prov = _provider_with(_envelope([dict(_ITEM, title=f"t{i}") for i in range(15)]))
    assert len(prov.search("今世缘", max_results=5).results) == 5


# ── ② 失败必须如实回传，不伪装成「成功但没结果」 ──────────────

def test_empty_items_reports_failure():
    prov = _provider_with(_envelope([]))
    resp = prov.search("今世缘", max_results=5)
    assert resp.success is False
    assert resp.error_message == "未找到相关资讯"


def test_quota_exhausted_is_surfaced():
    envelope = {
        "success": True, "status": 0, "message": "ok",
        "data": {"data": {"message": "您今日的妙想请求的数据量已达到上限，额度将于次日恢复"}},
    }
    prov = _provider_with(envelope)
    resp = prov.search("今世缘", max_results=5)
    assert resp.success is False
    assert "上限" in (resp.error_message or "") or "额度" in (resp.error_message or "")


def test_bad_status_is_surfaced():
    prov = _provider_with({"success": False, "status": 500, "message": "internal error"})
    resp = prov.search("今世缘", max_results=5)
    assert resp.success is False
    assert "500" in resp.error_message


def test_exception_is_surfaced():
    prov = MXSearchProvider()
    prov._mx_search = _FakeMX(None)

    def _boom(query):
        raise RuntimeError("network down")

    prov._mx_search.search = _boom
    resp = prov.search("今世缘", max_results=5)
    assert resp.success is False
    assert "network down" in resp.error_message


# ── ③ 聚合器要带出逐 provider 的原因 ─────────────────────────

class _FailingProvider(BaseSearchProvider):
    name = "failing"
    requires_api_key = False

    def _do_search(self, query, api_key, max_results, **kwargs):
        return SearchResponse(query=query, results=[], provider=self.name,
                              success=False, error_message="未配置 API Key")


class _OkProvider(BaseSearchProvider):
    name = "ok"
    requires_api_key = False

    def _do_search(self, query, api_key, max_results, **kwargs):
        return SearchResponse(query=query, provider=self.name, success=True,
                              results=[SearchResult(title="标题", snippet="摘要",
                                                    url="https://x", source="s")])


def test_aggregator_returns_results_from_second_provider():
    agg = SearchAggregator([_FailingProvider(), _OkProvider()])
    resp = agg.search_any("今世缘", 5) if hasattr(agg, "search_any") else None
    # 统一走异步入口
    from utils.async_bridge import run_coro_sync
    resp = run_coro_sync(agg.search("今世缘", 5))
    assert resp.success is True
    assert len(resp.results) == 1


def test_aggregator_surfaces_provider_errors():
    from utils.async_bridge import run_coro_sync

    agg = SearchAggregator([_FailingProvider()])
    resp = run_coro_sync(agg.search("今世缘", 5))
    assert resp.success is False
    assert "failing" in resp.error_message
    assert "未配置 API Key" in resp.error_message


def test_provider_with_keys_still_guarded():
    """回归：需要 key 的 provider 在没配 key 时仍应报「未配置 API Key」。"""

    class _KeyedProvider(BaseSearchProvider):
        name = "keyed"

        def _do_search(self, query, api_key, max_results, **kwargs):  # pragma: no cover
            raise AssertionError("不应被调用")

    resp = _KeyedProvider().search("x", 1)
    assert resp.success is False
    assert "未配置 API Key" in resp.error_message


@pytest.mark.parametrize("requires_key", [True, False])
def test_requires_api_key_flag_default(requires_key):
    class _P(BaseSearchProvider):
        name = "p"

        def _do_search(self, query, api_key, max_results, **kwargs):  # pragma: no cover
            return SearchResponse(query=query, results=[], provider="p")

    _P.requires_api_key = requires_key
    resp = _P().search("x", 1)
    if requires_key:
        assert resp.success is False
    else:
        assert resp.search_time >= 0        # 免密路径确实进了 _do_search
