"""`web_search` 核心技能 单元测试（2026-10-04 新增，2026-10-18 扩到两个工具）。

技能把 `tools/search/service.py` 的通用网搜能力与 `tools/search/deepseek_fetch.py`
的抓取能力暴露为两个工具。本测试锁住：
1. 技能目录结构被正确识别（`build_tools` 存在，工具 = `web_search` + `web_fetch`）；
2. 工具**委托给服务层**（不得绕过 `service.run_web_search` / `deepseek_fetch.fetch_web_page` 自行实现）；
3. 参数（`max_results` / `days` / `max_chars`）正确透传；
4. 已登记在 `agents/report_templates/index.json::skills` 且启用。
"""
import importlib.util
import json
import logging
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_skill_module():
    """按路径加载技能 main.py（与 test_stock_analysis_news_search.py 同法）。"""
    path = _REPO_ROOT / "tools" / "skills" / "web_search" / "main.py"
    spec = importlib.util.spec_from_file_location("web_search_main_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_skill():
    return _load_skill_module().WebSearchSkill(logging.getLogger("test-web-search"))


def _tool(name):
    """按名字取工具（`build_langchain_tools` 按名排序，不能靠下标）。"""
    for t in _make_skill().build_langchain_tools():
        if t.name == name:
            return t
    raise AssertionError(f"技能没暴露 {name}")


# ── ① 技能结构 ───────────────────────────────────────────────

def test_skill_files_exist():
    assert (_REPO_ROOT / "tools/skills/web_search/SKILL.md").exists()
    assert (_REPO_ROOT / "tools/skills/web_search/main.py").exists()


def test_build_tools_exposes_search_and_fetch():
    """联网检索技能 = 搜索（给摘要）+ 抓取（给原文），两个工具成对出现。"""
    names = sorted(t.name for t in _make_skill().build_langchain_tools())
    assert names == ["web_fetch", "web_search"]


def test_tool_docstring_documents_params():
    """LangChain 用 docstring 生成工具描述，参数必须写清楚（否则 LLM 不会传）。"""
    doc = _make_skill().web_search.__doc__ or ""
    for token in ("query", "max_results", "days"):
        assert token in doc, f"docstring 缺少参数说明: {token}"


def test_fetch_docstring_documents_params():
    doc = _make_skill().web_fetch.__doc__ or ""
    for token in ("url", "max_chars"):
        assert token in doc, f"docstring 缺少参数说明: {token}"


# ── ② 委托给服务层（唯一装配点）───────────────────────────────

def test_web_search_delegates_to_service(monkeypatch):
    calls = {}
    import tools.search.service as service_mod

    def _fake_run(query, max_results=5, timeout=30, **kwargs):
        calls.update(query=query, max_results=max_results, timeout=timeout, kwargs=kwargs)
        return {"query": query, "success": True, "result_count": 1,
                "results": [{"title": "A"}], "provider": "Tavily"}

    monkeypatch.setattr(service_mod, "run_web_search", _fake_run)
    payload = json.loads(_make_skill().web_search("新国九条 分红 新规", 4, 30))

    assert payload["success"] is True
    assert payload["provider"] == "Tavily"
    assert calls["query"] == "新国九条 分红 新规"
    assert calls["max_results"] == 4
    assert calls["kwargs"] == {"days": 30}      # days 必须透传到服务层
    assert calls["timeout"] == 30


def test_default_days_is_seven(monkeypatch):
    calls = {}
    import tools.search.service as service_mod

    def _fake_run(query, max_results=5, timeout=30, **kwargs):
        calls.update(kwargs)
        return {"query": query, "success": True, "result_count": 0, "results": [],
                "provider": "aggregator"}

    monkeypatch.setattr(service_mod, "run_web_search", _fake_run)
    _make_skill().web_search("贵州茅台")
    assert calls["days"] == 7


def test_tool_invocation_returns_json_string(monkeypatch):
    """走真实 LangChain 工具入口（校验参数 schema 可被模型填充）。"""
    import tools.search.service as service_mod

    monkeypatch.setattr(service_mod, "run_web_search",
                        lambda query, max_results=5, timeout=30, **kw:
                        {"query": query, "success": True, "result_count": 1,
                         "results": [{"title": "T"}], "provider": "MX"})

    raw = _tool("web_search").invoke({"query": "白酒 动销", "max_results": 2, "days": 7})
    payload = json.loads(raw)
    assert payload["result_count"] == 1


# ── ②b 抓取工具委托给 Fetch 层 ───────────────────────────────

def test_web_fetch_delegates_to_loader(monkeypatch):
    calls = {}
    import tools.search.deepseek_fetch as fetch_mod

    def _fake_fetch(url, max_chars=None, api_key=None):
        calls.update(url=url, max_chars=max_chars)
        return {"url": url, "success": True, "title": "T", "content": "正文",
                "char_count": 2, "truncated": False, "billed": True}

    monkeypatch.setattr(fetch_mod, "fetch_web_page", _fake_fetch)
    payload = json.loads(_make_skill().web_fetch("https://example.com/a", 1200))

    assert payload["success"] is True and payload["content"] == "正文"
    assert calls == {"url": "https://example.com/a", "max_chars": 1200}


def test_web_fetch_via_langchain_entry(monkeypatch):
    import tools.search.deepseek_fetch as fetch_mod

    monkeypatch.setattr(fetch_mod, "fetch_web_page",
                        lambda url, max_chars=None, api_key=None:
                        {"url": url, "success": True, "content": "x", "billed": False})
    payload = json.loads(_tool("web_fetch").invoke({"url": "https://example.com/b"}))
    assert payload["url"] == "https://example.com/b"


# ── ③ 注册与启用 ─────────────────────────────────────────────

def test_registered_and_enabled_in_index_json():
    idx = json.loads((_REPO_ROOT / "agents/report_templates/index.json").read_text(encoding="utf-8"))
    assert "web_search" in idx["skills"]
    assert idx["skills"]["web_search"]["enabled"] is True


def test_skill_md_frontmatter_parsable():
    """SKILL.md frontmatter 必须能被技能注册器解析出 name/description。"""
    text = (_REPO_ROOT / "tools/skills/web_search/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    front = text.split("---", 2)[1]
    assert "name: web_search" in front
    assert "description:" in front
