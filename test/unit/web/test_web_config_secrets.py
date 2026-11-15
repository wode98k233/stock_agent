"""配置面板「只写不读」密钥 + 连通性检测路由的单测。

背景（2026-11-14 用户要求）：
1. 敏感 Key **只能覆盖写，不能通过 GET 读回**（连掩码都不给，只给「是否已配置」）；
2. 用户没填某个 Key 的值时**不能改它**——只改了别的普通变量后点保存，
   绝不能把 Key 写成空串。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """最小 Web 应用 + 隔离的 .env；退出时还原 Config 属性与 os.environ。"""
    from fastapi.testclient import TestClient

    from config import Config
    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage
    from utils.config_manager import ConfigManager

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join([
            "OPENAI_API_KEY=sk-original-key",
            "MX_APIKEY=old-mx",
            "TUSHARE_TOKEN=old-token",
            "DASHBOARD_ENABLED=true",
            "",
        ]),
        encoding="utf-8",
    )

    watched = ["OPENAI_API_KEY", "MX_APIKEY", "TUSHARE_TOKEN", "DASHBOARD_ENABLED"]
    saved_attrs = {k: getattr(Config, k, None) for k in watched}
    saved_env = {k: os.environ.get(k) for k in watched}

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )

    monkeypatch.setattr("utils.app_paths.get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        "utils.app_paths.get_server_static_dir",
        lambda: str(Path(__file__).resolve().parents[3] / "server" / "static"),
    )
    monkeypatch.setattr("agents.factory.AgentFactory.list", classmethod(lambda cls: ["react_stock"]))

    app = create_app(state)
    with TestClient(app) as client:
        try:
            yield SimpleNamespace(client=client, env_path=env_path, config=Config)
        finally:
            for key, value in saved_attrs.items():
                setattr(Config, key, value)
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            ConfigManager._instance = None


# ── GET：只写不读 ────────────────────────────────────────────

def test_get_config_never_returns_secret_value(panel):
    res = panel.client.get("/api/config")
    assert res.status_code == 200

    items = {item["key"]: item for item in res.json()["items"]}
    for key in ("OPENAI_API_KEY", "MX_APIKEY", "TUSHARE_TOKEN"):
        assert items[key]["sensitive"] is True
        assert items[key]["value"] == ""
        assert items[key]["configured"] is True

    # 明文与掩码都不许出现在响应里
    assert "sk-original-key" not in res.text
    assert "old-mx" not in res.text
    assert "old-token" not in res.text


def test_get_config_marks_unconfigured_secret(panel):
    panel.env_path.write_text(
        "OPENAI_API_KEY=\nMX_APIKEY=old-mx\nDASHBOARD_ENABLED=true\n",
        encoding="utf-8",
    )
    panel.client.post("/api/config/reload")

    res = panel.client.get("/api/config")
    items = {item["key"]: item for item in res.json()["items"]}
    assert items["OPENAI_API_KEY"]["configured"] is False
    assert items["MX_APIKEY"]["configured"] is True


# ── apply：没填就不改 ───────────────────────────────────────

def test_apply_without_secrets_keeps_existing_keys(panel):
    res = panel.client.post(
        "/api/config/apply",
        json={"changes": {"DASHBOARD_ENABLED": False}},
    )
    assert res.status_code == 200
    assert res.json()["secrets_updated"] == []

    content = panel.env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-original-key" in content
    assert "MX_APIKEY=old-mx" in content
    assert "TUSHARE_TOKEN=old-token" in content
    assert "DASHBOARD_ENABLED=false" in content
    assert panel.config.OPENAI_API_KEY == "sk-original-key"


def test_apply_empty_secret_does_not_wipe_key(panel):
    """留空 / 纯空格 = 保持原值，绝不能把 Key 写成空。"""
    res = panel.client.post(
        "/api/config/apply",
        json={"changes": {}, "secrets": {"OPENAI_API_KEY": "", "MX_APIKEY": "   "}},
    )
    assert res.status_code == 200
    assert res.json()["secrets_updated"] == []

    content = panel.env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-original-key" in content
    assert "MX_APIKEY=old-mx" in content
    assert "OPENAI_API_KEY=\n" not in content
    assert panel.config.OPENAI_API_KEY == "sk-original-key"


def test_apply_secret_overwrites_only_target_key(panel):
    res = panel.client.post(
        "/api/config/apply",
        json={"secrets": {"MX_APIKEY": "new-mx"}},
    )
    assert res.status_code == 200
    assert res.json()["secrets_updated"] == ["MX_APIKEY"]

    content = panel.env_path.read_text(encoding="utf-8")
    assert "MX_APIKEY=new-mx" in content
    assert "OPENAI_API_KEY=sk-original-key" in content  # 其它 key 原样保留
    assert "TUSHARE_TOKEN=old-token" in content
    assert panel.config.MX_APIKEY == "new-mx"


def test_apply_blocks_sensitive_key_sent_through_changes(panel):
    """敏感项必须走 secrets；混在 changes 里一律拦截，不落盘。"""
    res = panel.client.post(
        "/api/config/apply",
        json={"changes": {"MX_APIKEY": "smuggled"}},
    )
    assert res.status_code == 200
    assert res.json()["blocked"] == ["MX_APIKEY"]

    content = panel.env_path.read_text(encoding="utf-8")
    assert "smuggled" not in content
    assert "MX_APIKEY=old-mx" in content


def test_apply_blocks_readonly_keys(panel):
    res = panel.client.post(
        "/api/config/apply",
        json={"changes": {"WEB_PORT": 9999, "WEB_HOST": "0.0.0.0"}},
    )
    assert res.status_code == 200
    assert sorted(res.json()["blocked"]) == ["WEB_HOST", "WEB_PORT"]

    content = panel.env_path.read_text(encoding="utf-8")
    assert "WEB_PORT=9999" not in content


# ── .env 写入的边界：dotenv 会静默改值的那些字符 ────────────────

def test_env_roundtrip_safe_flags_dotenv_hazards():
    from utils.config_manager import env_roundtrip_safe

    # 正常 key：原样往返
    assert env_roundtrip_safe("sk-abc123")
    assert env_roundtrip_safe("ab#cd")          # 无空格前缀的 # 不是注释
    assert env_roundtrip_safe('ab"cd')
    assert env_roundtrip_safe("ab'cd")
    assert env_roundtrip_safe("ab=cd")          # = 只在第一个分隔
    assert env_roundtrip_safe("ab$cd")          # 单个 $ 不触发变量替换

    # dotenv 会改写的：变量替换 / 注释截断 / 换行
    assert not env_roundtrip_safe("ab${HOME}cd")
    assert not env_roundtrip_safe("ab #cd")     # 空格 + # → 截断成 "ab"
    assert not env_roundtrip_safe("ab\ncd")
    assert not env_roundtrip_safe("ab\rcd")


def test_apply_rejects_secret_that_dotenv_would_rewrite(panel):
    """${...} 会被 dotenv 变量替换 → 必须 400 拒绝，且一个字节都不落盘。"""
    before = panel.env_path.read_text(encoding="utf-8")
    before_cfg = panel.config.MX_APIKEY

    res = panel.client.post(
        "/api/config/apply",
        json={"secrets": {"MX_APIKEY": "ab${HOME}cd"}},
    )
    assert res.status_code == 400
    assert "MX_APIKEY" in res.json()["detail"]
    assert panel.env_path.read_text(encoding="utf-8") == before
    assert panel.config.MX_APIKEY == before_cfg


def test_apply_rejects_secret_with_newline(panel):
    before = panel.env_path.read_text(encoding="utf-8")

    res = panel.client.post(
        "/api/config/apply",
        json={"secrets": {"OPENAI_API_KEY": "sk-abc\nINJECTED=1"}},
    )
    assert res.status_code == 400
    content = panel.env_path.read_text(encoding="utf-8")
    assert content == before
    assert "INJECTED" not in content


def test_apply_accepts_secret_with_hash_and_quotes(panel):
    """带 # / 引号的 key 能原样往返，必须照常写入。"""
    res = panel.client.post(
        "/api/config/apply",
        json={"secrets": {"MX_APIKEY": 'ab#c"d'}},
    )
    assert res.status_code == 200
    assert res.json()["secrets_updated"] == ["MX_APIKEY"]
    assert 'MX_APIKEY=ab#c"d' in panel.env_path.read_text(encoding="utf-8")
    assert panel.config.MX_APIKEY == 'ab#c"d'


def test_apply_reports_unknown_secret_key_as_blocked(panel):
    """secrets 里塞非敏感 key：不写，但要明确回报（静默丢弃最难查）。"""
    res = panel.client.post(
        "/api/config/apply",
        json={"secrets": {"NOT_A_REAL_KEY": "x"}},
    )
    assert res.status_code == 200
    assert res.json()["blocked"] == ["NOT_A_REAL_KEY"]
    assert res.json()["secrets_updated"] == []
    assert "NOT_A_REAL_KEY" not in panel.env_path.read_text(encoding="utf-8")


def test_apply_noop_empty_payload_does_not_touch_env(panel):
    before = panel.env_path.read_text(encoding="utf-8")
    res = panel.client.post("/api/config/apply", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["blocked"] == []
    assert body["secrets_updated"] == []
    assert panel.env_path.read_text(encoding="utf-8") == before


# ── 连通性检测 ──────────────────────────────────────────────

def test_config_test_endpoint_returns_detail(panel, monkeypatch):
    from server.routes import config as config_routes

    seen = {}

    def fake_run(kind, payload):
        seen["kind"] = kind
        seen["target"] = payload.target
        return {"message": f"ok-{kind}", "extra": 1}

    monkeypatch.setattr(config_routes, "_run_config_test", fake_run)

    res = panel.client.post("/api/config/test", json={"kind": "llm", "target": "report"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["message"] == "ok-llm"
    assert body["detail"] == {"extra": 1}
    assert seen == {"kind": "llm", "target": "report"}


def test_config_test_endpoint_reports_failure_as_message(panel, monkeypatch):
    from server.routes import config as config_routes

    # 先把 tmp .env 载入 os.environ/Config，才能验证「报错回显的 key 被脱敏」
    panel.client.post("/api/config/reload")

    def boom(kind, payload):
        raise RuntimeError("[auth] invalid api key sk-original-key")

    monkeypatch.setattr(config_routes, "_run_config_test", boom)

    res = panel.client.post("/api/config/test", json={"kind": "llm"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is False
    assert "auth" in body["message"]
    assert "Key 无效" in body["message"]
    assert "sk-original-key" not in body["message"]


def test_config_test_endpoint_rejects_unknown_kind(panel):
    res = panel.client.post("/api/config/test", json={"kind": "bogus"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is False
    assert "未知的检测类型" in body["message"]


# ── schema 自洽 ─────────────────────────────────────────────

def test_config_schema_has_no_duplicate_keys():
    from server.config_schema import get_config_schema

    keys = [item["key"] for item in get_config_schema()]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"schema 里出现重复 key（页面会渲染两行且互相覆盖）：{dupes}"


def test_sensitive_and_readonly_flags_match_key_sets():
    from server.config_schema import (_READONLY_KEYS, _SENSITIVE_KEYS,
                                      get_config_schema)

    for item in get_config_schema():
        key = item["key"]
        if item.get("sensitive"):
            assert key in _SENSITIVE_KEYS, f"{key} 标了 sensitive 但不在 _SENSITIVE_KEYS"
        if item.get("readonly"):
            assert key in _READONLY_KEYS, f"{key} 标了 readonly 但不在 _READONLY_KEYS"
        if key in _SENSITIVE_KEYS:
            assert item.get("sensitive") is True, f"{key} 在 _SENSITIVE_KEYS 但没标 sensitive"
        if key in _READONLY_KEYS:
            assert item.get("readonly") is True, f"{key} 在 _READONLY_KEYS 但没标 readonly"
