"""同花顺问财 Skills 单元测试
mock HTTP 请求，不依赖外部 API

注意：10jqka 目录名以数字开头，无法直接 import，
通过 importlib 加载 skills.py 模块。

覆盖重点（2026-09-20 工具收敛后）
--------------------------------
网关实测不校验 ``X-Claw-Skill-Id``（用不存在的 id 也返回 200），26 个官方技能包实际只有
两种能力（``/v1/query2data`` + ``/v1/comprehensive/search``）。因此工具从 4 个收敛为
2 个统一入口：``iwc_query`` / ``iwc_search``。本文件守住收敛后的三条不变量：

1. ``TOOL_REGISTRY`` 恒为 2 项，且不含已废弃的旧工具名；
2. 每个技能包 SKILL.md 声明的工具名必须落在 ``TOOL_REGISTRY`` 内（防错配 / 防漏跑
   ``scripts/ops/patch_iwencai_skill_contracts.py``）；
3. ``build_all_tools`` 按名去重后恒为 2 个工具。
"""
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = REPO_ROOT / "tools" / "other_skills" / "10jqka"

# 已废弃的一技能一工具命名（收敛为 iwc_query / iwc_search）
LEGACY_TOOL_NAMES = (
    "iwc_news_search",
    "iwc_announcement_search",
    "iwc_sector_selector",
    "iwc_market_query",
)


def _load_iwencai_skills_module():
    """通过 importlib 加载 10jqka/skills.py"""
    skills_path = SKILLS_ROOT / "skills.py"
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
    from config import Config
    monkeypatch.setattr(Config, "IWENCAI_API_KEY", "test-key-123")
    monkeypatch.setattr(Config, "IWENCAI_BASE_URL", "https://openapi.iwencai.com")


@pytest.fixture(autouse=True)
def _no_throttle(skills_mod, monkeypatch):
    """消掉节流与退避等待（真实间隔另有专门测试），并清空运行级技能上下文。"""
    monkeypatch.setattr(skills_mod, "_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(skills_mod, "_RETRY_SLEEPS", (0.0, 0.0))
    from utils import skill_ctx
    skill_ctx.reset_active_skill()
    yield
    skill_ctx.reset_active_skill()


@pytest.fixture()
def mock_response():
    """构造 mock 的 requests.post 返回值"""
    def _make(data=None, status_code=200):
        payload = data if data is not None else {"data": [{"title": "测试新闻", "summary": "摘要"}]}
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = payload
        resp.text = json.dumps(payload, ensure_ascii=False)
        return resp
    return _make


# ── 工具收敛不变量 ────────────────────────────────────────────────────────
class TestToolConvergence:
    def test_registry_is_exactly_two_tools(self, skills_mod):
        """TOOL_REGISTRY 必须收敛为 iwc_query + iwc_search"""
        assert set(skills_mod.TOOL_REGISTRY) == {"iwc_query", "iwc_search"}

    def test_legacy_tool_names_removed(self, skills_mod):
        """旧的一技能一工具命名不得残留"""
        for legacy in LEGACY_TOOL_NAMES:
            assert legacy not in skills_mod.TOOL_REGISTRY, f"旧工具名 {legacy} 未清理"

    def test_registry_values_are_callable_and_model(self, skills_mod):
        """每个 registry 条目应为 (可调用, pydantic 模型)"""
        from pydantic import BaseModel
        for name, (func, model) in skills_mod.TOOL_REGISTRY.items():
            assert callable(func), f"{name} 的实现不可调用"
            assert isinstance(model, type) and issubclass(model, BaseModel), f"{name} 参数模型非法"

    def test_param_models_require_query_only(self, skills_mod):
        """两个工具都只强制要求 query"""
        assert set(skills_mod.IWCQueryParams.model_fields) == {"query", "page", "limit"}
        assert set(skills_mod.IWCSearchParams.model_fields) == {"query", "channel", "size"}
        assert skills_mod.IWCQueryParams(query="x").page == "1"
        assert skills_mod.IWCSearchParams(query="x").channel == "news"


class TestSkillContractConsistency:
    """SKILL.md 声明的工具名必须与 TOOL_REGISTRY 对齐。"""

    def _skill_dirs(self):
        return sorted(
            d for d in os.listdir(SKILLS_ROOT)
            if (SKILLS_ROOT / d).is_dir() and not d.startswith(".") and d != "__pycache__"
            and (SKILLS_ROOT / d / "SKILL.md").exists()
        )

    def test_every_pack_declares_registry_tool(self, skills_mod):
        """每个技能包都必须声明一个已登记的统一工具（漏跑 patch 脚本会在此失败）"""
        assert self._skill_dirs(), "未发现任何问财技能包"
        for d in self._skill_dirs():
            content = (SKILLS_ROOT / d / "SKILL.md").read_text(encoding="utf-8-sig")
            tool_name, _ = skills_mod._parse_declared_tool(content)
            assert tool_name in skills_mod.TOOL_REGISTRY, (
                f"{d}/SKILL.md 未声明有效工具（需 '## 工具列表' 下 '### 工具N：iwc_query'），"
                f"请重跑 scripts/ops/patch_iwencai_skill_contracts.py"
            )

    def test_every_pack_registered_as_skill(self, skills_mod):
        """每个技能包都应有对应 loader"""
        loaders = skills_mod.get_skill_loaders()
        expected = {d.replace("-", "_") for d in self._skill_dirs()}
        assert expected <= set(loaders), f"缺 loader: {expected - set(loaders)}"

    def test_skill_register_parsers_agree(self, skills_mod):
        """注册器实际解析出的工具/目录/指南，必须与 skills.py 的判定一致。

        守住「skills.py 认得出工具、注册器却解析出 0 个工具」的静默错配：
        ``_parse_tool_list`` 要求 ``## 工具列表`` 段头存在，仅靠 ``### 工具N：`` 不算数。
        """
        from tools.skill_register import (
            _parse_tool_list, _parse_catalog_info, _parse_usage_guide,
        )
        for d in self._skill_dirs():
            content = (SKILLS_ROOT / d / "SKILL.md").read_text(encoding="utf-8-sig")
            declared, _ = skills_mod._parse_declared_tool(content)
            parsed = _parse_tool_list(content)
            assert len(parsed) == 1, f"{d}: 注册器解析出 {len(parsed)} 个工具，期望 1"
            assert parsed[0]["tool_name"] == declared, f"{d}: 两侧工具名不一致"
            assert parsed[0]["description"], f"{d}: 缺 '- 功能：' 描述"
            params = {p["name"] for p in parsed[0]["params"]}
            expected_params = ({"query", "page", "limit"} if declared == "iwc_query"
                               else {"query", "channel", "size"})
            assert params == expected_params, f"{d}: 参数声明不一致 {params}"
            assert _parse_catalog_info(content)["key_params"], f"{d}: 缺 '## 目录层信息' 关键参数"
            assert _parse_usage_guide(content), f"{d}: 缺 '## 使用指南'"

    def test_loaders_bind_valid_tool(self, skills_mod):
        """loader 产出的 LangChain 工具名必须落在 TOOL_REGISTRY 内且只有 1 个"""
        logger = MagicMock()
        for name, loader in skills_mod.get_skill_loaders().items():
            tools = loader(logger, None)
            assert len(tools) == 1, f"{name} 应产出 1 个工具，实际 {len(tools)}"
            assert tools[0].name in skills_mod.TOOL_REGISTRY, f"{name} 绑定了未知工具 {tools[0].name}"


# ── 测试模块加载 ──────────────────────────────────────────────────────────
class TestModuleLoad:
    def test_skill_catalog_not_empty(self, skills_mod):
        catalog = skills_mod.get_skill_catalog()
        assert "同花顺问财" in catalog

    def test_skill_loaders_count(self, skills_mod):
        loaders = skills_mod.get_skill_loaders()
        assert len(loaders) >= 12
        assert "news_search" in loaders
        assert "announcement_search" in loaders

    def test_skill_meta_contains_all(self, skills_mod):
        meta_keys = set(skills_mod._iwc_skill_meta.keys())
        assert "news_search" in meta_keys
        assert "announcement_search" in meta_keys


# ── 测试统一查询入口 ──────────────────────────────────────────────────────
class TestIwcQueryCore:
    def test_payload_and_endpoint(self, skills_mod, mock_response):
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwc_query_core("市盈率小于20的股票", page="2", limit="20")

        args, kwargs = mock_post.call_args
        assert args[0].endswith("/v1/query2data")
        assert kwargs["json"] == {
            "query": "市盈率小于20的股票", "page": "2", "limit": "20",
            "is_cache": "1", "expand_index": "true",
        }

    def test_default_pagination(self, skills_mod, mock_response):
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwc_query_core("银行股")

        payload = mock_post.call_args[1]["json"]
        assert payload["page"] == "1" and payload["limit"] == "10"

    def test_int_params_coerced_to_str(self, skills_mod, mock_response):
        """LLM 可能传 int，需转成网关要求的字符串"""
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwc_query_core("银行股", page=3, limit=50)

        payload = mock_post.call_args[1]["json"]
        assert payload["page"] == "3" and payload["limit"] == "50"


# ── 测试统一检索入口 ──────────────────────────────────────────────────────
class TestIwcSearchCore:
    def test_payload_and_endpoint(self, skills_mod, mock_response):
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            result = json.loads(skills_mod._iwc_search_core("贵州茅台研报", channel="report", size=5))

        args, kwargs = mock_post.call_args
        assert args[0].endswith("/v1/comprehensive/search")
        assert kwargs["json"] == {
            "query": "贵州茅台研报", "channels": ["report"], "app_id": "AIME_SKILL", "size": 5,
        }
        assert result["channels"] == ["report"]

    @pytest.mark.parametrize("channel", ["news", "announcement", "report"])
    def test_all_channels_allowed(self, skills_mod, mock_response, channel):
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwc_search_core("测试", channel=channel)
        assert mock_post.call_args[1]["json"]["channels"] == [channel]

    def test_invalid_channel_rejected_without_request(self, skills_mod):
        """非法 channel 必须本地拒绝，不发请求、不浪费配额"""
        with patch.object(skills_mod.requests, "post") as mock_post:
            result = json.loads(skills_mod._iwc_search_core("测试", channel="video"))
        mock_post.assert_not_called()
        assert result["status"] == "failed"
        assert "channel" in result["error"]


# ── 测试 headers / 错误处理 ───────────────────────────────────────────────
class TestRequestHeadersAndErrors:
    def test_headers_complete(self, skills_mod, mock_response):
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwc_query_core("test")

        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-key-123"
        assert headers["X-Claw-Skill-Id"] == "iwc_query"
        assert headers["X-Claw-Skill-Version"] == "1.0.0"
        assert headers["X-Claw-Call-Type"] == "normal"
        assert headers["X-Claw-Plugin-Id"] == "none"
        assert len(headers["X-Claw-Trace-Id"]) == 64

    def test_trace_id_is_fresh_per_request(self, skills_mod, mock_response):
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwc_query_core("a")
            skills_mod._iwc_query_core("b")
        ids = [c[1]["headers"]["X-Claw-Trace-Id"] for c in mock_post.call_args_list]
        assert ids[0] != ids[1]

    def test_skill_id_override(self, skills_mod, mock_response):
        """skill_id 可覆盖（网关不校验，仅统计用）"""
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mock_post:
            skills_mod._iwc_query_core("test", skill_id="hithink-finance-query")
        assert mock_post.call_args[1]["headers"]["X-Claw-Skill-Id"] == "hithink-finance-query"

    def test_no_api_key(self, skills_mod, monkeypatch):
        monkeypatch.delenv("IWENCAI_API_KEY", raising=False)
        from config import Config
        monkeypatch.setattr(Config, "IWENCAI_API_KEY", "")
        result = json.loads(skills_mod._iwc_query_core("test"))
        assert "error" in result
        assert "IWENCAI_API_KEY" in result["error"]

    def test_timeout_error(self, skills_mod):
        import requests as req
        with patch.object(skills_mod.requests, "post", side_effect=req.exceptions.Timeout("timeout")):
            result = json.loads(skills_mod._iwc_query_core("test"))
        assert result["status"] == "failed"
        assert "超时" in result["error"]

    def test_connection_error(self, skills_mod):
        import requests as req
        with patch.object(skills_mod.requests, "post", side_effect=req.exceptions.ConnectionError("conn")):
            result = json.loads(skills_mod._iwc_query_core("test"))
        assert result["status"] == "failed"
        assert "连接" in result["error"]

    def test_invalid_json_response(self, skills_mod):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        resp.text = "plain text"
        with patch.object(skills_mod.requests, "post", return_value=resp):
            result = json.loads(skills_mod._iwc_query_core("test"))
        assert result["error"] == "invalid_json_response"


# ── 测试 X-Claw-Skill-Id 归属上报 ─────────────────────────────────────────
class TestSkillIdReporting:
    """问财配额按 skill 分桶（官方口径 100 次/skill/天），记账媒介只能是 Skill-Id。

    工具收敛为 2 个统一入口后工具名 ≠ 技能名，故必须靠运行级上下文
    (`utils/skill_ctx.py`) 把域信息传到请求层。
    """

    @staticmethod
    def _sent_skill_id(mock_post):
        return mock_post.call_args[1]["headers"]["X-Claw-Skill-Id"]

    def test_uses_active_skill_from_context(self, skills_mod, mock_response):
        from utils import skill_ctx
        skill_ctx.note_active_skill("hithink_finance_query")
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mp:
            skills_mod._iwc_query_core("贵州茅台ROE")
        assert self._sent_skill_id(mp) == "hithink-finance-query"

    def test_search_uses_active_skill(self, skills_mod, mock_response):
        from utils import skill_ctx
        skill_ctx.note_active_skill("report_search")
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mp:
            skills_mod._iwc_search_core("贵州茅台研报", channel="report")
        assert self._sent_skill_id(mp) == "report-search"

    def test_non_iwencai_skill_falls_back(self, skills_mod, mock_response):
        """mx_data 之类的名字不得被当成问财 skill id 发出去"""
        from utils import skill_ctx
        skill_ctx.note_active_skill("mx_data")
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mp:
            skills_mod._iwc_query_core("x")
        assert self._sent_skill_id(mp) == "iwc_query"

    def test_explicit_arg_beats_context(self, skills_mod, mock_response):
        from utils import skill_ctx
        skill_ctx.note_active_skill("hithink_finance_query")
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mp:
            skills_mod._iwc_query_core("x", skill_id="hithink-macro-query")
        assert self._sent_skill_id(mp) == "hithink-macro-query"

    def test_no_context_falls_back_to_tool_name(self, skills_mod, mock_response):
        with patch.object(skills_mod.requests, "post", return_value=mock_response()) as mp:
            skills_mod._iwc_query_core("x")
        assert self._sent_skill_id(mp) == "iwc_query"

    def test_every_pack_maps_to_a_real_skill_name(self, skills_mod):
        """上下文能产出的 id 必须都是真实技能名（下划线→dash），不是自造值"""
        from utils import skill_ctx
        for name in skills_mod.get_skill_loaders():
            skill_ctx.note_active_skill(name)
            rid = skills_mod._resolve_skill_id()
            assert rid == name.replace("_", "-"), name
            assert "-" in rid and "_" not in rid
        skill_ctx.reset_active_skill()


# ── 测试节流 / 额度防护 ──────────────────────────────────────────────────
class TestQuotaGuard:
    def test_quota_exhausted_not_retried(self, skills_mod):
        """额度耗尽只打 1 次请求（重试无意义，省请求 + 省退避空等）"""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status_code": 0, "data": None,
                                  "status_msg": "今日调用次数已达到上限"}
        resp.text = "{}"
        with patch.object(skills_mod.requests, "post", return_value=resp) as mp:
            out = json.loads(skills_mod._iwc_query_core("x"))
        assert mp.call_count == 1
        assert out["status"] == "failed"
        assert "额度" in out["error"]

    def test_rate_limited_retried_with_retry_call_type(self, skills_mod, mock_response):
        limited = MagicMock()
        limited.status_code = 429
        limited.json.return_value = {"status_code": 429, "status_msg": "操作过于频繁"}
        limited.text = "{}"
        with patch.object(skills_mod.requests, "post",
                          side_effect=[limited, limited, mock_response()]) as mp:
            out = json.loads(skills_mod._iwc_query_core("x"))
        assert mp.call_count == 3
        call_types = [c[1]["headers"]["X-Claw-Call-Type"] for c in mp.call_args_list]
        assert call_types == ["normal", "retry", "retry"]
        assert out["data"]["data"][0]["title"] == "测试新闻"

    def test_rate_limited_exhausts_retries(self, skills_mod):
        limited = MagicMock()
        limited.status_code = 429
        limited.json.return_value = {"status_code": 429}
        limited.text = "{}"
        with patch.object(skills_mod.requests, "post", return_value=limited) as mp:
            out = json.loads(skills_mod._iwc_query_core("x"))
        assert mp.call_count == skills_mod._MAX_RETRIES + 1
        assert out["status"] == "failed"

    def test_trace_id_unique_across_retries(self, skills_mod):
        limited = MagicMock(); limited.status_code = 429
        limited.json.return_value = {"status_code": 429}; limited.text = "{}"
        ok = MagicMock(); ok.status_code = 200
        ok.json.return_value = {"data": []}; ok.text = "{}"
        with patch.object(skills_mod.requests, "post", side_effect=[limited, ok]) as mp:
            skills_mod._iwc_query_core("x")
        ids = [c[1]["headers"]["X-Claw-Trace-Id"] for c in mp.call_args_list]
        assert len(set(ids)) == len(ids)

    def test_marker_classification(self, skills_mod):
        assert skills_mod.is_quota_exhausted({"status_msg": "额度将于次周恢复"})
        assert skills_mod.is_quota_exhausted({"data": {"status_msg": "次数已达到上限"}})
        assert not skills_mod.is_quota_exhausted({"status_msg": "操作过于频繁"})
        assert skills_mod.is_rate_limited({"status_msg": "操作过于频繁"})
        assert skills_mod.is_rate_limited({}, status_code=429)
        assert not skills_mod.is_rate_limited({"status_msg": "正常"})

    def test_throttle_enforces_min_interval(self, skills_mod, monkeypatch):
        """真实间隔：连续两次请求之间至少间隔 _MIN_INTERVAL_S"""
        import time as _t
        monkeypatch.setattr(skills_mod, "_MIN_INTERVAL_S", 0.3)
        monkeypatch.setattr(skills_mod, "_last_request_at", 0.0)
        stamps = []
        resp = MagicMock(); resp.status_code = 200
        resp.json.return_value = {"data": []}; resp.text = "{}"

        def _fake_post(*a, **k):
            stamps.append(_t.monotonic())
            return resp

        with patch.object(skills_mod.requests, "post", side_effect=_fake_post):
            skills_mod._iwc_query_core("a")
            skills_mod._iwc_query_core("b")
        assert stamps[1] - stamps[0] >= 0.25


# ── 测试 build_all_tools ──────────────────────────────────────────────────
class TestBuildAllTools:
    def test_dedupes_to_two_tools(self, skills_mod, mock_response):
        """16 个技能共享 2 个工具，去重后必须只剩 2 个"""
        logger = MagicMock()
        tools = skills_mod.build_all_tools(logger, None)
        names = [t.name for t in tools]
        assert sorted(names) == ["iwc_query", "iwc_search"], names
        assert len(names) == len(set(names))

    def test_descriptions_are_domain_agnostic(self, skills_mod):
        """统一工具描述不得内嵌某个技能独有的渠道/域（否则会误导 LLM）"""
        assert "本技能用" not in skills_mod._TOOL_DESCRIPTIONS["iwc_search"]
        assert "announcement" in skills_mod._TOOL_DESCRIPTIONS["iwc_search"]
        assert "news" in skills_mod._TOOL_DESCRIPTIONS["iwc_search"]
        assert "report" in skills_mod._TOOL_DESCRIPTIONS["iwc_search"]

    def test_build_all_no_api_key(self, skills_mod, monkeypatch):
        monkeypatch.delenv("IWENCAI_API_KEY", raising=False)
        from config import Config
        monkeypatch.setattr(Config, "IWENCAI_API_KEY", "")
        logger = MagicMock()
        assert skills_mod.build_all_tools(logger, None) == []
        logger.warning.assert_called()


# ── 测试 SkillRegister 集成 ──────────────────────────────────────────────
class TestSkillRegisterIntegration:
    @pytest.fixture(scope="class")
    def register(self):
        os.environ.setdefault("OPENAI_API_KEY", "test")
        from tools.skill_register import SkillRegister
        reg = SkillRegister()
        reg.auto_discover(fast=False)
        return reg

    def test_skills_discoverable(self, register):
        news = register._registry.get("news_search")
        ann = register._registry.get("announcement_search")
        assert news is not None, "news_search 未被 SkillRegister 发现"
        assert ann is not None, "announcement_search 未被 SkillRegister 发现"
        assert news.source == "external"
        assert news.build_tools_func is not None

    def test_registry_filtered_to_declared_tool(self, register):
        """每个技能过滤后的 tool_registry 只含它声明的那个统一工具"""
        for name in ("news_search", "announcement_search", "hithink_market_query",
                     "hithink_finance_query"):
            meta = register._registry.get(name)
            assert meta is not None, f"{name} 未注册"
            declared = {t.tool_name for t in meta.tools}
            assert declared == {"iwc_search"} if "search" in name else declared == {"iwc_query"}
            assert set(meta.tool_registry or {}) == declared

    def test_exposed_tools_deduped(self, register):
        """注册器最终暴露的问财工具恒为 2 个"""
        logger = MagicMock()
        names = [t.name for t in register.build_all_langchain_tools(logger, None)
                 if t.name.startswith("iwc_")]
        assert sorted(names) == ["iwc_query", "iwc_search"], names

    def test_execute_tool_accepts_default_pagination(self, register, mock_response):
        """execute_tool 路径（市场面板/资金流路由依赖）只传 query 也应正常走通"""
        with patch.object(skills_mod_requests(), "post", return_value=mock_response()):
            raw = register.execute_tool("hithink_market_query", "iwc_query", {"query": "上证指数"})
        assert isinstance(raw, str)
        assert json.loads(raw)["query"] == "上证指数"

    def test_execute_tool_rejects_unknown_tool(self, register):
        with pytest.raises(ValueError):
            register.execute_tool("hithink_market_query", "iwc_sector_selector", {"query": "x"})

    # ── 运行级技能上下文（配额分桶的传输通道）──

    def test_activate_skill_sets_skill_context(self, register):
        """activate_skill（渐进披露唯一入口）必须写入运行级技能上下文"""
        from unittest.mock import MagicMock as _MM
        from utils import skill_ctx
        from agents.shared.tool_utils import activate_skill
        from tools.skills import SkillRegistry

        reg = SkillRegistry(_MM(), None, register)
        skill_ctx.reset_active_skill()
        try:
            activate_skill(reg, [], "hithink_finance_query")
            assert skill_ctx.get_active_skill() == "hithink_finance_query"
        finally:
            skill_ctx.reset_active_skill()

    def test_execute_tool_restores_previous_context(self, register, mock_response):
        """execute_tool（直调路径）临时写入，调用结束后恢复原值"""
        from utils import skill_ctx
        skill_ctx.note_active_skill("some_other_skill")
        try:
            with patch("requests.post", return_value=mock_response()):
                register.execute_tool("hithink_market_query", "iwc_query", {"query": "上证指数"})
            assert skill_ctx.get_active_skill() == "some_other_skill"
        finally:
            skill_ctx.reset_active_skill()

    def test_execute_tool_reports_domain_skill_id(self, register, mock_response):
        """端到端：直调路径也要把域 id 报到 X-Claw-Skill-Id"""
        from utils import skill_ctx
        skill_ctx.reset_active_skill()
        try:
            with patch("requests.post", return_value=mock_response()) as mp:
                register.execute_tool("hithink_management_query", "iwc_query",
                                      {"query": "前十大股东"})
            assert mp.call_args[1]["headers"]["X-Claw-Skill-Id"] == "hithink-management-query"
        finally:
            skill_ctx.reset_active_skill()


def skills_mod_requests():
    """取 10jqka/skills.py 模块上的 requests 对象（供跨模块 patch）"""
    import requests
    return requests
