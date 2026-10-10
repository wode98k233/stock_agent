"""Parallel / Exa 免密 provider 单元测试（2026-10-07 新增）。

背景：这两个是**免 Key 托管 MCP**，国内网络实测可达（DDG 系全被墙，见 memory/2026-10-06.md）。
作为「兜底源」接入 8 源链，用 `SEARCH_FREE_MCP_ENABLED` 可整体关闭（数据出境合规开关）。

本测试**不依赖外网**：解析器用真实抓取的报文样例，`_get_client` 用替身。
"""
import pytest

from tools.search.base import SearchResponse
from tools.search.exa_provider import ExaSearchProvider, _parse_exa_payload
from tools.search.mcp_client import MCPError
from tools.search.parallel_provider import ParallelSearchProvider, _parse_parallel_payload

# ── 真实报文样例（2026-10-07 实抓）─────────────────────────────

PARALLEL_REAL = """{
  "search_id": "search_9dcfee6ac03a9651c15cc18b34617e93",
  "results": [
    {
      "url": "https://www.moutai.com.cn/mtjt/xwzx/mtxw/index.html",
      "title": "茅台新闻 - moutai",
      "publish_date": null,
      "excerpts": ["茅台又遇村超 2026-10-01 茅台又遇村超。在榕江举行的第三届贵州村超。"]
    },
    {
      "url": "https://www.toutiao.com/article/7535023151718105650/",
      "title": "茅台，最新消息来了 - 今日头条",
      "publish_date": "2026-08-15",
      "excerpts": ["贵州茅台最新消息来了！", "已累计回购超过53亿元。"]
    },
    {
      "url": "",
      "title": "无 url 的条目应被丢弃",
      "excerpts": ["x"]
    }
  ],
  "warnings": null
}"""

EXA_REAL = """Title: 公司公告 - 入网口
URL: https://www.moutaichina.com/mtgf/tzzgx/gsgg/index.html
Published: N/A
Author: N/A
Highlights:
- 贵州茅台2026年半年度报告摘要 2026-08-15
- 贵州茅台2026年半年度报告 2026-08-15

---

Title: 贵州茅台将新增广州与郑州两大发货仓 | 每经网
URL: https://www.nbd.com.cn/articles/2026-09-19/xxx.html
Published: 2026-09-19T00:00:00.000Z
Author: 每经记者
Highlights:
- 贵州茅台将新增广州与郑州两大发货仓。
"""


# ── ① 免密声明（最易踩的坑）──────────────────────────────────

def test_both_providers_are_keyless():
    """必须显式 requires_api_key=False，否则基类会短路成「未配置 API Key」。"""
    assert ParallelSearchProvider.requires_api_key is False
    assert ExaSearchProvider.requires_api_key is False


def test_keyless_provider_actually_searches(monkeypatch):
    """免密 provider 必须真的进入 _do_search（判据：search_time > 0）。"""
    prov = ParallelSearchProvider()
    prov._client = _StubClient(text=PARALLEL_REAL)
    resp = prov.search("贵州茅台", max_results=3)
    assert resp.success is True
    assert resp.search_time > 0, "修前类 bug：search_time 恒为 0.0 说明 _do_search 未执行"


# ── ② 开关 ──────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True),
    ("false", False), ("FALSE", False), ("0", False), ("no", False),
])
def test_free_mcp_kill_switch(monkeypatch, value, expected):
    monkeypatch.setenv("SEARCH_FREE_MCP_ENABLED", value)
    assert ParallelSearchProvider().is_available is expected
    assert ExaSearchProvider().is_available is expected


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("SEARCH_FREE_MCP_ENABLED", raising=False)
    assert ParallelSearchProvider().is_available is True
    assert ExaSearchProvider().is_available is True


# ── ③ Parallel 解析 ─────────────────────────────────────────

def test_parse_parallel_maps_fields():
    rs = _parse_parallel_payload(PARALLEL_REAL, 10)
    assert len(rs) == 2, "无 url 的条目应被丢弃"
    first, second = rs
    assert first.title == "茅台新闻 - moutai"
    assert first.url == "https://www.moutai.com.cn/mtjt/xwzx/mtxw/index.html"
    assert first.source == "moutai.com.cn"          # 已去掉 www.
    assert first.published_date == ""               # null → 空串，不是 "None"
    assert "村超" in first.snippet
    # excerpts 为多元素 list 时应拼接
    assert "回购" in second.snippet
    assert second.published_date == "2026-08-15"


def test_parse_parallel_respects_max_results():
    assert len(_parse_parallel_payload(PARALLEL_REAL, 1)) == 1


def test_parse_parallel_bad_input_returns_empty():
    assert _parse_parallel_payload("not json at all", 5) == []
    assert _parse_parallel_payload('{"results": "oops"}', 5) == []


# ── ④ Exa 解析 ─────────────────────────────────────────────

def test_parse_exa_splits_blocks():
    rs = _parse_exa_payload(EXA_REAL, 10)
    assert len(rs) == 2
    first, second = rs
    assert first.title == "公司公告 - 入网口"
    assert first.url == "https://www.moutaichina.com/mtgf/tzzgx/gsgg/index.html"
    assert first.source == "moutaichina.com"
    assert first.published_date == ""               # "N/A" → 空串
    assert "2026年半年度报告摘要" in first.snippet   # 头部字段被剥掉，正文保留

    assert second.title.startswith("贵州茅台将新增")
    assert second.published_date == "2026-09-19"     # ISO 时间截成日期
    assert "发货仓" in second.snippet


def test_parse_exa_strips_header_lines():
    rs = _parse_exa_payload(EXA_REAL, 10)
    body = rs[0].snippet
    for key in ("Title:", "URL:", "Published:", "Author:", "Highlights:"):
        assert key not in body, f"头部字段 {key} 应被剥离"


def test_parse_exa_empty_and_respects_limit():
    assert _parse_exa_payload("", 5) == []
    assert _parse_exa_payload("no url here", 5) == []
    assert len(_parse_exa_payload(EXA_REAL, 1)) == 1


# ── ⑤ 失败必须如实回传 ──────────────────────────────────────

class _StubClient:
    def __init__(self, text="", raise_exc=None):
        self._text = text
        self._raise = raise_exc
        self.calls = []

    def call_tool(self, name, args, timeout=None):
        self.calls.append({"name": name, "args": args, "timeout": timeout})
        if self._raise:
            raise self._raise
        return self._text


@pytest.mark.parametrize("cls,payload,provider_label", [
    (ParallelSearchProvider, PARALLEL_REAL, "Parallel"),
    (ExaSearchProvider, EXA_REAL, "Exa"),
])
def test_mcp_error_is_surfaced(cls, payload, provider_label):
    prov = cls()
    prov._client = _StubClient(raise_exc=MCPError("rate limit exceeded"))
    resp = prov.search("x", max_results=3)
    assert resp.success is False
    assert "rate limit" in resp.error_message


def test_unexpected_exception_does_not_escape():
    """provider 层不得把异常抛给聚合器。"""
    prov = ExaSearchProvider()
    prov._client = _StubClient(raise_exc=RuntimeError("boom"))
    resp = prov.search("x", max_results=3)
    assert resp.success is False
    assert "boom" in resp.error_message


@pytest.mark.parametrize("cls", [ParallelSearchProvider, ExaSearchProvider])
def test_unparsable_output_reports_failure_not_silent_success(cls):
    """返回体解析不出结果时必须报失败，不能伪装成「成功但没结果」。"""
    prov = cls()
    prov._client = _StubClient(text="<<<garbage>>>")
    resp = prov.search("x", max_results=3)
    assert resp.success is False
    assert resp.results == []
    assert "未返回可解析结果" in resp.error_message


@pytest.mark.parametrize("cls,tool", [
    (ParallelSearchProvider, "web_search"),
    (ExaSearchProvider, "web_search_exa"),
])
def test_calls_expected_tool(cls, tool):
    prov = cls()
    stub = _StubClient(text=PARALLEL_REAL if cls is ParallelSearchProvider else EXA_REAL)
    prov._client = stub
    prov.search("贵州茅台", max_results=2)
    assert stub.calls[0]["name"] == tool


def test_parallel_passes_required_args():
    """Parallel 的 inputSchema 要求 objective + search_queries 都必填。"""
    prov = ParallelSearchProvider()
    stub = _StubClient(text=PARALLEL_REAL)
    prov._client = stub
    prov.search("贵州茅台 最新消息", max_results=3)
    args = stub.calls[0]["args"]
    assert args["search_queries"] == ["贵州茅台 最新消息"]
    assert args["objective"]


def test_exa_passes_required_args():
    """Exa 的 inputSchema 要求 query + objective 都必填。"""
    prov = ExaSearchProvider()
    stub = _StubClient(text=EXA_REAL)
    prov._client = stub
    prov.search("贵州茅台", max_results=3)
    args = stub.calls[0]["args"]
    assert args["query"] == "贵州茅台"
    assert args["objective"]
    assert args["numResults"] == 3


# ── ⑥ 工厂接入与顺序 ────────────────────────────────────────

def test_factory_includes_free_mcp_providers():
    from tools.search.factory import SearchProviderFactory
    names = [p.name for p in SearchProviderFactory.create_providers()]
    assert "Parallel" in names and "Exa" in names


def test_factory_excludes_them_when_disabled(monkeypatch):
    from tools.search.factory import SearchProviderFactory
    monkeypatch.setenv("SEARCH_FREE_MCP_ENABLED", "false")
    names = [p.name for p in SearchProviderFactory.create_providers()]
    assert "Parallel" not in names and "Exa" not in names


def test_free_mcp_sources_rank_as_fallback_before_searxng():
    """免 Key 境外源应排在需要 Key 的付费源之后（作兜底），SearXNG 之前。"""
    from tools.search.factory import SearchProviderFactory
    names = [p.name for p in SearchProviderFactory.create_providers()]
    if "SearXNG" in names:            # 只有配了实例才会出现
        assert names.index("Parallel") < names.index("SearXNG")
    if "Tavily" in names:
        assert names.index("Tavily") < names.index("Parallel")


def test_available_providers_includes_free_sources(monkeypatch):
    from tools.search.service import list_available_providers
    monkeypatch.setenv("SEARCH_FREE_MCP_ENABLED", "true")
    avail = list_available_providers()
    assert "Parallel" in avail and "Exa" in avail
