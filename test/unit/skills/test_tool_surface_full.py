"""工具面全量常驻（PRD-A9 §2）单元测试

核心不变量：**切技能不得改变 tools 数组**。

tools 是 prompt 缓存前缀的位置 0，缓存只对「从头开始的连续前缀」生效。
现状（strict）里``activate_skills`` 对共享 tools_ref 做 ``clear() + extend()``，
每轮换批都让位置 0 变化 —— 同一次 run 内后续调用必然 100% miss。
实测 strict 形态下 ``skill-router`` 每轮指纹都变、命中率 0%。

本测试全部用假 registry，不构建真实技能、不调 LLM。
"""
import pathlib

import pytest

import config

from agents.shared.tool_utils import (
    activate_skills,
    activate_tool_mode,
    is_full_surface,
    tool_surface_mode,
)


# ── 假 registry ──────────────────────────────────────────

class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeRegistry:
    """最小 registry 替身。

    ``get_skill_tools`` 必须返回 **dict**（``{"tool_name": ...}``）——这与真实
    ``SkillRegister.get_skill_tools`` 一致。曾经在这里用带 ``.name`` 的对象，
    导致 ``activate_skill`` 取工具名时抛异常被吞、工具清单恒为空。
    """

    def __init__(self, skills=None):
        self.skills = skills or {
            "a": ["a1", "a2"],
            "b": ["b1"],
            "c": ["c1", "c2", "c3"],
        }

    def get_tools(self, name, selected_skills=None):
        return [_FakeTool(n) for n in self.skills.get(name, [])]

    def get_skill_usage_guide(self, name):
        return f"## 技能 {name} 的用法\n口径：主源 X，回落 Y。"

    def get_skill_catalog(self):
        return [{"skill_name": s} for s in sorted(self.skills)]

    def get_skill_tools(self, name):
        return [{"tool_name": n, "description": ""} for n in self.skills.get(name, [])]


def _fingerprint(tools):
    import hashlib
    import json
    payload = [{"name": t.name, "desc": getattr(t, "description", "")} for t in tools]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# ── strict：保持原有换绑行为（回归护栏）────────────────────

def test_strict_still_rebinds_tools():
    """默认形态必须维持原语义，否则等于单方面改变了所有用户的行为。"""
    reg = _FakeRegistry()
    ref = []
    activate_skills(reg, ref, ["a"], logger=None)
    assert _fingerprint(ref) == _fingerprint([_FakeTool("a1"), _FakeTool("a2")])

    activate_skills(reg, ref, ["c"], logger=None)
    assert [t.name for t in ref] == ["c1", "c2", "c3"], "strict 形态未按批换绑"


# ── full surface：不换绑 ──────────────────────────────────

def test_mutate_tools_false_never_touches_tools_ref():
    reg = _FakeRegistry()
    ref = [_FakeTool(t) for t in ("a1", "a2", "b1", "c1", "c2", "c3")]
    before = _fingerprint(ref)

    names = activate_skills(reg, ref, ["c"], logger=None, mutate_tools=False)

    assert _fingerprint(ref) == before, "mutate_tools=False 却改动了 tools_ref"
    assert len(ref) == 6, "tools 数量发生变化"
    # 返回值仍应反映该批覆盖的工具（供日志/trace 使用）
    assert names == ["c1", "c2", "c3"]


def test_tools_fingerprint_stable_across_many_switches():
    """连续切批 10 次，tools 指纹必须始终一致——这是整个改动的意义所在。"""
    reg = _FakeRegistry()
    ref = [_FakeTool(t) for t in ("a1", "a2", "b1", "c1", "c2", "c3")]
    baseline = _fingerprint(ref)

    for skill in ("a", "b", "c") * 3 + ("a",):
        activate_skills(reg, ref, [skill], logger=None, mutate_tools=False)
        assert _fingerprint(ref) == baseline, f"切到 {skill} 后 tools 变了"
    assert len(ref) == 6


def test_empty_batch_does_not_clear_tools():
    """空批次在 strict 会清空 tools；full 形态必须守住。"""
    reg = _FakeRegistry()
    ref = [_FakeTool("a1")]
    activate_skills(reg, ref, [], logger=None, mutate_tools=False)
    assert [t.name for t in ref] == ["a1"]


# ── 形态解析 ──────────────────────────────────────────────

def test_tool_surface_mode_falls_back_on_invalid(monkeypatch):
    import config as cfg

    monkeypatch.setattr(cfg.Config, "TOOL_SURFACE_MODE", "不存在的形态", raising=False)
    assert tool_surface_mode() == "full", "非法值必须回落 full（不得退回最差形态）"
    assert is_full_surface() is True


@pytest.mark.parametrize("mode,full", [
    ("full", True), ("strict", False),
])
def test_is_full_surface_matrix(monkeypatch, mode, full):
    import config as cfg
    monkeypatch.setattr(cfg.Config, "TOOL_SURFACE_MODE", mode, raising=False)
    assert is_full_surface() is full


def test_defaults_are_new_behavior():
    """默认应为 full + router（PRD-A9 §2 + §13.10 实测修正）。

    router 而非 model 的依据：2026-10-14 的 4 次真实 run 里，
    ``activate_skill`` 被模型调用 **0 次** —— model 态的设计前提（模型会主动
    激活技能）不成立；而 router 态由 SubGoalRouterNode 自动投递，确定性不漏。

    刻意**不做** importlib.reload 式断言：本地 `.env` 若显式写了该项，
    真实生效值就应当被用户的选择覆盖，reload 断言会变成"测试环境决定产品行为"。
    这里只锁「出厂默认」——即 _ENV_MAP 登记的缺省值与代码里的兜底。
    """
    # 2026-10-17 删档：两项已固化为常量（config/agent.py），不再进 _ENV_MAP
    from config import Config

    assert Config.TOOL_SURFACE_MODE == "full"
    assert Config.SKILL_ACTIVATE_TOOL_MODE == "router"
    assert not hasattr(Config, "_ENV_MAP") or "TOOL_SURFACE_MODE" not in Config._ENV_MAP, (
        "TOOL_SURFACE_MODE 已删档，不应再出现在 _ENV_MAP")

    # 2026-10-17 删档后为常量（config/agent.py），锁源码防手滑改值
    import config.agent as agent_mod
    src = pathlib.Path(agent_mod.__file__).read_text(encoding="utf-8")
    assert 'TOOL_SURFACE_MODE = "full"' in src, "固化常量应为 full"
    assert 'SKILL_ACTIVATE_TOOL_MODE = "router"' in src, "固化常量应为 router"


def test_activate_tool_mode_matrix(monkeypatch):
    import config as cfg
    for mode in ("model", "router", "off"):
        monkeypatch.setattr(cfg.Config, "SKILL_ACTIVATE_TOOL_MODE", mode, raising=False)
        assert activate_tool_mode() == mode
    monkeypatch.setattr(cfg.Config, "SKILL_ACTIVATE_TOOL_MODE", "乱填", raising=False)
    assert activate_tool_mode() == "model"


def test_tool_surface_and_activate_mode_are_orthogonal(monkeypatch):
    """两个配置互不覆盖：full+router 与 strict+model 都要合法可达。"""
    import config as cfg
    for surface, activate in (("full", "router"), ("full", "model"),
                              ("strict", "model"), ("strict", "off")):
        monkeypatch.setattr(cfg.Config, "TOOL_SURFACE_MODE", surface, raising=False)
        monkeypatch.setattr(cfg.Config, "SKILL_ACTIVATE_TOOL_MODE", activate, raising=False)
        assert tool_surface_mode() == surface
        assert activate_tool_mode() == activate


def test_off_with_full_is_rejected_at_load():
    """【2026-10-17 删档】off / strict 档已固化为常量，off 不可达。

    原「off 必须配 strict，否则配置期报错」的校验随之删除——
    约束在常量层面恒满足，validate 不再校验这两项。
    """
    import config as cfg
    assert cfg.Config.TOOL_SURFACE_MODE == "full"
    assert cfg.Config.SKILL_ACTIVATE_TOOL_MODE == "router"
    cfg.Config.validate()  # 不抛异常


# ── activate_skill 元操作工具 ─────────────────────────────

def test_activate_skill_returns_guide_and_tool_names():
    from agents.react.activate_skill_tool import build_activate_skill_tool

    tool = build_activate_skill_tool(_FakeRegistry())
    out = tool.invoke({"skill_name": "b"})
    assert "已激活技能「b」" in out
    assert "b1" in out, "应列出该技能下可用的工具名"
    assert "口径：主源 X，回落 Y。" in out


def test_activate_skill_rejects_unknown_skill_with_message_not_exception():
    """未知技能名必须返回明确文案；抛异常会让模型反复重试同一个错名字。"""
    from agents.react.activate_skill_tool import build_activate_skill_tool

    tool = build_activate_skill_tool(_FakeRegistry())
    out = tool.invoke({"skill_name": "不存在"})
    assert "不存在" in out
    assert "可用技能目录" in out


def test_activate_skill_handles_empty_name():
    from agents.react.activate_skill_tool import build_activate_skill_tool

    tool = build_activate_skill_tool(_FakeRegistry())
    assert "技能名为空" in tool.invoke({"skill_name": "  "})


def test_activate_skill_is_hk_us_allowed():
    """元操作与市场无关；不放行会导致港美股任务无法激活任何技能。"""
    from tools.tool_capabilities import tool_supports_market

    assert tool_supports_market("activate_skill", "HK")
    assert tool_supports_market("activate_skill", "US")


def test_activate_skill_exempt_from_output_compression():
    """再压一遍会截断口径，模型就会用错工具。"""
    from agents.react.utils import _COMPRESS_EXEMPT_TOOLS

    assert "activate_skill" in _COMPRESS_EXEMPT_TOOLS


def test_web_fetch_exempt_from_output_compression():
    """web_fetch 的价值就是原文（摘要已有 web_search.summary），压缩 = 白花那 ¥0.005。"""
    from agents.react.utils import _COMPRESS_EXEMPT_TOOLS

    assert "web_fetch" in _COMPRESS_EXEMPT_TOOLS
