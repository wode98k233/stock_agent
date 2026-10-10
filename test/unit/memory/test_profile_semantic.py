"""P2（2026-09-23）：用户画像语义抽取单测（用确定性假 embedding，离线）。"""
from types import SimpleNamespace

import memory.sdk as sdk_mod
from memory.profile_semantic import extract_preferences
from memory.user_profile import UserProfileManager

# 假 embedding：按线索词命中维度（与 _PROTOTYPES 的种子语一致）
_DIMS = [
    ("保守", "稳健", "回撤", "怕亏", "安全边际"),      # 0 conservative
    ("激进", "高波动", "敢赌", "弹性"),                # 1 aggressive
    ("适中", "平衡"),                                  # 2 moderate
    ("短线", "快进快出", "打板", "几天", "做T", "做t"),  # 3 short
    ("中线", "波段", "几个月"),                        # 4 medium
    ("长线", "长期持有", "定投", "半年"),              # 5 long
    ("技术", "K线", "均线", "MACD", "macd"),           # 6 technical
    ("基本面", "财报", "估值", "现金流"),              # 7 fundamental
]


def _fake_embed(text):
    return [1.0 if any(w in text for w in dim) else 0.0 for dim in _DIMS] + [0.01]


def test_extract_conservative():
    assert extract_preferences("我比较保守，怕回撤", _fake_embed)["risk_tolerance"] == "conservative"


def test_extract_aggressive():
    assert extract_preferences("我比较激进，敢赌一把", _fake_embed)["risk_tolerance"] == "aggressive"


def test_extract_short_horizon():
    assert extract_preferences("我是短线，快进快出打板", _fake_embed)["investment_horizon"] == "short"


def test_extract_long_horizon():
    assert extract_preferences("我做长线，长期持有定投", _fake_embed)["investment_horizon"] == "long"


def test_extract_technical_style():
    assert extract_preferences("我主要看技术指标，K线和均线", _fake_embed)["analysis_style"] == "technical_heavy"


def test_extract_fundamental_style():
    assert extract_preferences("我主要看基本面，财报和估值", _fake_embed)["analysis_style"] == "fundamental_heavy"


def test_extract_balanced_style():
    assert extract_preferences("我技术面和基本面均衡，两者都要看", _fake_embed)["analysis_style"] == "balanced"


def test_cue_gate_skips_unrelated():
    assert extract_preferences("今天天气不错，出去走走", _fake_embed) == {}


def test_no_embedding_returns_empty():
    assert extract_preferences("我比较保守", None) == {}


def test_embedding_error_returns_empty():
    def boom(_):
        raise RuntimeError("rate limited")
    assert extract_preferences("我比较保守", boom) == {}


# ── 集成：仅补齐空字段、不覆盖已确定值 ──────────────────────

def _patch_sdk(monkeypatch):
    fake_sdk = SimpleNamespace(
        backend=SimpleNamespace(_embedding=SimpleNamespace(_embedding_fn=_fake_embed)))
    monkeypatch.setattr(sdk_mod, "get_sdk", lambda: fake_sdk)


def test_semantic_supplement_fills_empty(monkeypatch, tmp_path):
    _patch_sdk(monkeypatch)
    mgr = UserProfileManager(user_id="t", profile_dir=str(tmp_path))
    assert mgr.profile.risk_tolerance == ""
    mgr._semantic_supplement("我比较激进，敢赌一把")
    assert mgr.profile.risk_tolerance == "aggressive"


def test_semantic_supplement_does_not_override(monkeypatch, tmp_path):
    _patch_sdk(monkeypatch)
    mgr = UserProfileManager(user_id="t", profile_dir=str(tmp_path))
    mgr.profile.risk_tolerance = "conservative"
    mgr._semantic_supplement("我比较激进，敢赌一把")
    assert mgr.profile.risk_tolerance == "conservative"
