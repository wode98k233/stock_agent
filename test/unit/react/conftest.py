"""ReAct 工具面形态相关的单测共用夹具。

PRD-A9 §2 把 ``TOOL_SURFACE_MODE`` 默认值从 ``strict`` 改成了 ``full``，
而本目录多数用例断言的是 **strict 档的批装载语义**（``tools_ref`` 装本批工具、
切批即换绑、``bind_tools`` 恰好等于该批）。

这些用例应该锁住自己声称的行为，而不是跟着全局默认漂移——
否则一次改默认就会让它们集体变红，回归护栏反而失效。
因此这里提供 ``strict_surface`` 显式夹具，用它把 strict 档钉死；
full 档另有 ``test_tool_surface_full.py``（tools/skills/）与
本目录 ``test_tool_surface_full_behavior.py`` 覆盖。
"""
import pytest


@pytest.fixture
def strict_surface(monkeypatch):
    """把工具面钉在 strict 档（本目录 strict 语义用例共用）。"""
    import config as cfg
    monkeypatch.setattr(cfg.Config, "TOOL_SURFACE_MODE", "strict", raising=False)
    return "strict"


@pytest.fixture
def full_surface(monkeypatch):
    """把工具面钉在 full + model 档。"""
    import config as cfg
    monkeypatch.setattr(cfg.Config, "TOOL_SURFACE_MODE", "full", raising=False)
    monkeypatch.setattr(cfg.Config, "SKILL_ACTIVATE_TOOL_MODE", "model", raising=False)
    return "full"


@pytest.fixture
def full_surface_router(monkeypatch):
    """full + router 投递档（router 负责追加口径，模型不自己激活）。"""
    import config as cfg
    monkeypatch.setattr(cfg.Config, "TOOL_SURFACE_MODE", "full", raising=False)
    monkeypatch.setattr(cfg.Config, "SKILL_ACTIVATE_TOOL_MODE", "router", raising=False)
    return "router"