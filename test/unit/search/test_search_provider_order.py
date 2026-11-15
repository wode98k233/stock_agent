# -*- coding: utf-8 -*-
"""搜索源顺序：DeepSeek 排首位 + `SEARCH_PROVIDER_ORDER` 白名单（2026-10-18 新增）。

两条硬约束：
1. **DeepSeek 必须在链首** —— 聚合器是 first-good-wins，MX 免 Key 且中文能出结果，
   只要它在前面，DeepSeek 几乎永远不会被调用（等于白集成）。
2. **顺序旋钮是白名单语义**：写了的才用、按写的顺序用；拼错的名字要告警（不能静默少源）。
"""
import pytest

from tools.search.base import SearchResponse
from tools.search.factory import SearchProviderFactory


class _P:
    """最小 provider 替身（只用于顺序逻辑，不起真实 provider）。"""
    def __init__(self, name):
        self.name = name

    @property
    def is_available(self):
        return True


@pytest.fixture(autouse=True)
def _clean_order(monkeypatch):
    monkeypatch.delenv("SEARCH_PROVIDER_ORDER", raising=False)
    yield


def _names(monkeypatch, keys="k1", enabled="true"):
    monkeypatch.setenv("DEEPSEEK_API_KEYS", keys)
    monkeypatch.setenv("DEEPSEEK_SEARCH_ENABLED", enabled)
    return [p.name for p in SearchProviderFactory.create_providers()]


# ── ① 接入与位置 ────────────────────────────────────────────

def test_deepseek_is_first_when_configured(monkeypatch):
    names = _names(monkeypatch)
    assert "DeepSeek" in names
    assert names[0] == "DeepSeek", f"必须排首位，实际 {names}"


def test_deepseek_absent_without_keys(monkeypatch):
    names = _names(monkeypatch, keys="")
    assert "DeepSeek" not in names


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no"])
def test_kill_switch_disables_source(monkeypatch, value):
    """11-01 之后想省钱 / 额度耗尽时的一键关闭"""
    names = _names(monkeypatch, enabled=value)
    assert "DeepSeek" not in names


def test_multi_keys_parsed(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEYS", "k1, k2 ,k3")
    assert SearchProviderFactory._parse_keys("DEEPSEEK_API_KEYS") == ["k1", "k2", "k3"]


# ── ② 顺序旋钮（纯函数）────────────────────────────────────

def test_no_order_keeps_default_sequence():
    providers = [_P("DeepSeek"), _P("MX"), _P("Tavily")]
    assert SearchProviderFactory._apply_order(providers, "") == providers
    assert SearchProviderFactory._apply_order(providers, None) == providers


def test_order_reorders_and_whitelists():
    providers = [_P("DeepSeek"), _P("MX"), _P("Tavily")]
    out = SearchProviderFactory._apply_order(providers, "MX,Tavily")
    assert [p.name for p in out] == ["MX", "Tavily"]      # DeepSeek 未列入 → 本次不用


def test_order_is_case_insensitive():
    providers = [_P("DeepSeek"), _P("MX")]
    out = SearchProviderFactory._apply_order(providers, "mx, DEEPSEEK")
    assert [p.name for p in out] == ["MX", "DeepSeek"]


def test_unknown_names_are_ignored_with_warning(caplog):
    providers = [_P("MX")]
    out = SearchProviderFactory._apply_order(providers, "Tavliy,MX")
    assert [p.name for p in out] == ["MX"]                # 拼错的被忽略，不能连累正确的
    assert "Tavliy" in caplog.text


def test_duplicate_names_collapse():
    providers = [_P("MX"), _P("DeepSeek")]
    out = SearchProviderFactory._apply_order(providers, "MX,MX,DeepSeek")
    assert [p.name for p in out] == ["MX", "DeepSeek"]


def test_all_unknown_yields_empty():
    assert SearchProviderFactory._apply_order([_P("MX")], "nope") == []


def test_env_drives_order(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER_ORDER", "DeepSeek")
    names = _names(monkeypatch)
    assert names == ["DeepSeek"]


def test_env_order_with_unknown_only_falls_to_empty(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER_ORDER", "Typo")
    assert _names(monkeypatch) == []
