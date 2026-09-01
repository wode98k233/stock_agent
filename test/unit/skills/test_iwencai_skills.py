"""
同花顺问财 Skills 单元测试
mock HTTP 请求，不依赖外部 API

注意：10jqka 目录名以数字开头，无法直接 import，
通过 importlib 加载 skills.py 模块。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _load_iwencai_skills_module():
    """通过 importlib 加载 10jqka/skills.py"""
    skills_path = Path(__file__).resolve().parents[3] / "tools" / "other_skills" / "10jqka" / "skills.py"
    spec = importlib.util.spec_from_file_location("iwencai_skills", str(skills_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def skills_mod():
    """加载 skills 模块（module scope，只加载一次）"""
    return _load_iwencai_skills_module()


# ── Fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _set_iwencai_env(monkeypatch):
    """确保测试环境有 API Key（同步修改 os.environ 和 Config 类属性）"""
    monkeypatch.setenv("IWENCAI_API_KEY", "test-key-123")
    monkeypatch.setenv("IWENCAI_BASE_URL", "https://openapi.iwencai.com")
    # skills.py 现在从 Config 读取，需要同步 patch Config 类属性
    from config import Config
    monkeypatch.setattr(Config, "IWENCAI_API_KEY", "test-key-123")
    monkeypatch.setattr(Config, "IWENCAI_BASE_URL", "https://openapi.iwencai.com")


@pytest.fixture()
def mock_response():
    """构造 mock 的 requests.post 返回值"""
    def _make(data=None, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = data or {"data": [{"title": "测试新闻", "summary": "摘要"}]}
        resp.text = json.dumps(data or {"data": [{"title": "测试新闻", "summary": "摘要"}]}, ensure_ascii=False)
        return resp
    return _make


# ── 测试模块加载 ──────────────────────────────────────────────────────────
class TestModuleLoad:
    def test_skill_registry_populated(self, skills_mod):
        """skills.py 加载后 TOOL_REGISTRY 应包含核心工具"""
        assert len(skills_mod.TOOL_REGISTRY) >= 2
        assert "iwc_news_search" in skills_mod.TOOL_REGISTRY
        assert "iwc_announcement_search" in skills_mod.TOOL_REGISTRY

    def test_skill_catalog_not_empty(self, skills_mod):
        """get_skill_catalog 应返回包含问财关键字的字符串"""
        catalog = skills_mod.get_skill_catalog()
        assert "同花顺问财" in catalog
        assert "news_search" in catalog

    def test_skill_loaders_count(self, skills_mod):
        """get_skill_loaders 应包含核心 loader"""
        loaders = skills_mod.get_skill_loaders()
        assert len(loaders) >= 2
        assert "news_search" in loaders
        assert "announcement_search" in loaders

    def test_skill_meta_contains_all(self, skills_mod):
        """应包含核心 skill 的元数据（下划线风格）"""
        meta_keys = set(skills_mod._iwc_skill_meta.keys())
        assert "news_search" in meta_keys
        assert "announcement_search" in meta_keys


# ── 测试 API 调用 ─────────────────────────────────────────────────────────
class TestIwencaiSearch:
    def test_basic_search(self, skills_mod, mock_response):
        """基本搜索调用应返回包装后的 JSON"""
        with patch.object(skills_mod.requests, "post", return_value=mock_response()):
            result = json.loads(skills_mod._iwencai_search("人工智能", ["news"], "news-search", "1.0.0"))

        assert result["query"] == "人工智能"
        assert result["skill_id"] == "news-search"
        assert result["channels"] == ["news"]
        assert "data" in result

    def test_request_headers(self, skills_mod, mock_response):
        """请求头应包含所有 X-Claw-* 字段"""
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwencai_search("test", ["news"], "news-search", "1.0.0")

        _, kwargs = mock_post.call_args
        headers = kwargs.get("headers", {})
        assert headers["Authorization"] == "Bearer test-key-123"
        assert headers["X-Claw-Skill-Id"] == "news-search"
        assert headers["X-Claw-Skill-Version"] == "1.0.0"
        assert headers["X-Claw-Call-Type"] == "normal"
        assert len(headers["X-Claw-Trace-Id"]) == 64

    def test_request_payload(self, skills_mod, mock_response):
        """请求体应包含 channels、app_id、query"""
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwencai_search("贵州茅台", ["announcement"], "announcement-search", "1.0.0")

        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        assert payload["channels"] == ["announcement"]
        assert payload["app_id"] == "AIME_SKILL"
        assert payload["query"] == "贵州茅台"

    def test_no_api_key(self, skills_mod, monkeypatch):
        """未配置 API Key 时应返回错误"""
        monkeypatch.delenv("IWENCAI_API_KEY", raising=False)
        from config import Config
        monkeypatch.setattr(Config, "IWENCAI_API_KEY", "")
        result = json.loads(skills_mod._iwencai_search("test", ["news"], "news-search", "1.0.0"))
        assert "error" in result
        assert "IWENCAI_API_KEY" in result["error"]

    def test_timeout_error(self, skills_mod):
        """超时应返回错误 JSON"""
        import requests as req
        with patch.object(skills_mod.requests, "post", side_effect=req.exceptions.Timeout("timeout")):
            result = json.loads(skills_mod._iwencai_search("test", ["news"], "news-search", "1.0.0"))
        assert result["status"] == "failed"
        assert "超时" in result["error"]

    def test_connection_error(self, skills_mod):
        """连接失败应返回错误 JSON"""
        import requests as req
        with patch.object(skills_mod.requests, "post", side_effect=req.exceptions.ConnectionError("conn")):
            result = json.loads(skills_mod._iwencai_search("test", ["news"], "news-search", "1.0.0"))
        assert result["status"] == "failed"
        assert "连接" in result["error"]

    def test_invalid_json_response(self, skills_mod):
        """非 JSON 响应应返回错误"""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        resp.text = "plain text"
        with patch.object(skills_mod.requests, "post", return_value=resp):
            result = json.loads(skills_mod._iwencai_search("test", ["news"], "news-search", "1.0.0"))
        assert "error" in result
        assert result["error"] == "invalid_json_response"


# ── 测试各 skill core 函数 ────────────────────────────────────────────────
class TestSkillCoreFunctions:
    def test_news_search_channels(self, skills_mod):
        with patch.object(skills_mod, "_iwencai_search", return_value="{}") as mock:
            skills_mod._iwc_news_search_core("AI新闻")
            mock.assert_called_once_with("AI新闻", ["news"], "news-search", "1.0.0")

    def test_announcement_search_channels(self, skills_mod):
        with patch.object(skills_mod, "_iwencai_search", return_value="{}") as mock:
            skills_mod._iwc_announcement_search_core("茅台公告")
            mock.assert_called_once_with("茅台公告", ["announcement"], "announcement-search", "1.0.0")


# ── 测试 build_all_tools ──────────────────────────────────────────────────
class TestBuildAllTools:
    def test_build_all_returns_tools(self, skills_mod, mock_response):
        """build_all_tools 应返回至少 2 个 LangChain tool"""
        with patch.object(skills_mod.requests, "post", return_value=mock_response()):
            mock_logger = MagicMock()
            tools = skills_mod.build_all_tools(mock_logger, None)

        assert len(tools) >= 2
        tool_names = [t.name for t in tools]
        assert "iwc_news_search" in tool_names
        assert "iwc_announcement_search" in tool_names

    def test_build_all_no_api_key(self, skills_mod, monkeypatch):
        """无 API Key 时应返回空列表并 warning"""
        monkeypatch.delenv("IWENCAI_API_KEY", raising=False)
        from config import Config
        monkeypatch.setattr(Config, "IWENCAI_API_KEY", "")
        mock_logger = MagicMock()
        tools = skills_mod.build_all_tools(mock_logger, None)
        assert tools == []
        mock_logger.warning.assert_called()


# ── 测试 SkillRegister 集成 ──────────────────────────────────────────────
class TestSkillRegisterIntegration:
    def test_skills_discoverable(self):
        """SkillRegister 应能自动发现并注册 iwencai skills"""
        # 需要设置环境变量
        import os
        os.environ.setdefault("IWENCAI_API_KEY", "test-key")
        os.environ.setdefault("OPENAI_API_KEY", "test")

        from tools.skill_register import SkillRegister
        sr = SkillRegister()
        sr.auto_discover()

        # 应该发现至少 news_search 和 announcement_search
        news = sr._registry.get("news_search")
        ann = sr._registry.get("announcement_search")

        assert news is not None, "news_search 未被 SkillRegister 发现"
        assert ann is not None, "announcement_search 未被 SkillRegister 发现"
        assert news.source == "external"
        assert ann.source == "external"
        assert news.build_tools_func is not None
        assert ann.build_tools_func is not None
