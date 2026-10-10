"""SearXNG 免密 provider 回归测试（2026-10-04）。

## 现象

`.env` 配 `SEARXNG_PUBLIC_INSTANCES=true`（无自建实例）时，搜索恒返回
`SearchResponse(results=[], success=False, error_message='SearXNG 未配置 API Key')`，
且 `search_time=0.0` —— 即 `_do_search` **从未执行**（实测确认）。

## 真因

`SearXNGSearchProvider` **只覆盖了 `is_available`**（返回 True），**漏了 `requires_api_key`**
（继承基类默认 `True`）→ 基类 `search()` 判 `keyless=False` → `_get_next_key()` 在
`_base_urls` 为空时返回 `None` → 在「未配置 API Key」处短路，`_do_search` 里实现好的
公共实例发现逻辑永远走不到。

与 2026-09-21 修的 `MXSearchProvider` 是**同一个坑**（见 `test_mx_provider_keyless.py`），
当时漏改了 SearXNG。

## 本测试锁住的契约

1. `SearXNGSearchProvider.requires_api_key is False`（SearXNG 的凭据是实例 URL，不是 Key）；
2. 公共实例模式下 `_do_search` **确实被执行**（会被短路 = 回归）；
3. 自建实例 URL 正常轮转；
4. 拿不到实例时如实报「无可用 SearXNG 实例」，而不是「未配置 API Key」。
"""
import pytest

from tools.search.base import BaseSearchProvider
from tools.search.searxng_provider import SearXNGSearchProvider


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeRequests:
    """按 URL 分派的假 requests：实例清单 vs 搜索请求。"""

    def __init__(self, instances=None, search_payload=None, search_status=200):
        self._instances = instances if instances is not None else {}
        self._search_payload = search_payload or {"results": []}
        self._search_status = search_status
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, params))
        if url == SearXNGSearchProvider.PUBLIC_INSTANCES_URL:
            return _FakeResp({"instances": self._instances})
        return _FakeResp(self._search_payload, self._search_status)


_SEARCH_OK = {
    "results": [
        {"title": "贵州茅台三季报", "url": "https://example.com/a",
         "content": "营收同比增长……", "publishedDate": "2026-10-01T00:00:00Z"},
        {"title": "白酒动销回暖", "url": "https://example.com/b", "content": "渠道反馈……"},
    ]
}


@pytest.fixture(autouse=True)
def _clear_public_cache():
    """公共实例是**类级缓存**，跨测试必须清掉，否则互相污染。"""
    SearXNGSearchProvider._public_instances_cache = None
    yield
    SearXNGSearchProvider._public_instances_cache = None


# ── ① 结构：免密声明 ─────────────────────────────────────────

def test_searxng_is_keyless():
    assert SearXNGSearchProvider.requires_api_key is False
    # 其它 provider 仍是缺省「需要 key」，防止误伤
    assert BaseSearchProvider.requires_api_key is True


def test_public_mode_is_available_without_urls():
    prov = SearXNGSearchProvider(base_urls=[], use_public_instances=True)
    assert prov.is_available is True


# ── ② 公共实例模式：_do_search 必须被执行 ────────────────────

def test_public_instance_mode_actually_searches(monkeypatch):
    """修前这里恒报「SearXNG 未配置 API Key」且 search_time=0.0（短路未进 _do_search）。"""
    import tools.search.searxng_provider as mod

    fake = _FakeRequests(
        instances={"https://searx.test": {"network_type": "normal",
                                         "http": {"status_code": 200}}},
        search_payload=_SEARCH_OK,
    )
    monkeypatch.setattr(mod, "requests", fake)

    prov = SearXNGSearchProvider(base_urls=[], use_public_instances=True)
    resp = prov.search("贵州茅台", max_results=5)

    assert resp.success is True
    assert len(resp.results) == 2
    assert resp.search_time > 0, "修前恒为 0.0 —— 说明 _do_search 从未执行"
    assert resp.results[0].title == "贵州茅台三季报"
    assert resp.results[0].published_date == "2026-10-01"
    # 确实打到了实例的 /search 端点
    assert any("/search" in url for url, _ in fake.calls)


def test_public_mode_uses_discovered_instance_url(monkeypatch):
    import tools.search.searxng_provider as mod

    fake = _FakeRequests(
        instances={"https://searx.abc": {"network_type": "normal",
                                         "http": {"status_code": 200}}},
        search_payload=_SEARCH_OK,
    )
    monkeypatch.setattr(mod, "requests", fake)

    prov = SearXNGSearchProvider(base_urls=[], use_public_instances=True)
    prov.search("白酒", max_results=3)

    search_urls = [u for u, _ in fake.calls if "/search" in u]
    assert search_urls and search_urls[0].startswith("https://searx.abc")


# ── ③ 自建实例模式：URL 轮转仍然正确 ────────────────────────

def test_self_hosted_url_used(monkeypatch):
    import tools.search.searxng_provider as mod

    fake = _FakeRequests(search_payload=_SEARCH_OK)
    monkeypatch.setattr(mod, "requests", fake)

    prov = SearXNGSearchProvider(base_urls=["https://my-searx.local"], use_public_instances=True)
    # 自建 URL 优先：use_public_instances 被忽略
    assert prov._use_public_instances is False

    resp = prov.search("贵州茅台", max_results=3)
    assert resp.success is True
    assert any(u.startswith("https://my-searx.local") for u, _ in fake.calls)


# ── ④ 无实例时如实报错（而非「未配置 API Key」）──────────────

def test_no_instances_reports_instance_error_not_key_error(monkeypatch):
    import tools.search.searxng_provider as mod

    fake = _FakeRequests(instances={})   # 清单为空 → 拿不到任何实例
    monkeypatch.setattr(mod, "requests", fake)

    prov = SearXNGSearchProvider(base_urls=[], use_public_instances=True)
    resp = prov.search("贵州茅台", max_results=3)

    assert resp.success is False
    assert "无可用 SearXNG 实例" in resp.error_message
    assert "未配置 API Key" not in resp.error_message, \
        "回归：又被基类短路了（说明 requires_api_key 改回 True 了）"


def test_time_range_mapping(monkeypatch):
    """days → time_range 的档位映射（SearXNG 支持时间过滤）。"""
    import tools.search.searxng_provider as mod

    fake = _FakeRequests(
        instances={"https://searx.test": {"network_type": "normal",
                                         "http": {"status_code": 200}}},
        search_payload=_SEARCH_OK,
    )
    monkeypatch.setattr(mod, "requests", fake)

    prov = SearXNGSearchProvider(base_urls=[], use_public_instances=True)
    prov.search("白酒", max_results=3, days=30)

    search_params = [p for u, p in fake.calls if "/search" in u]
    assert search_params and search_params[0].get("time_range") == "month"
