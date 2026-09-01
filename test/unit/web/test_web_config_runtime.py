from types import SimpleNamespace


def test_web_config_exposes_env_example_runtime_settings(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )

    monkeypatch.setattr("agents.factory.AgentFactory.list", classmethod(lambda cls: ["react_stock"]))
    app = create_app(state)

    with TestClient(app) as client:
        res = client.get("/api/config")

    assert res.status_code == 200
    items = {item["key"]: item for item in res.json()["items"]}

    assert items["DASHBOARD_ENABLED"]["type"] == "bool"
    assert items["DASHBOARD_LLM_ENABLED"]["type"] == "bool"
    assert items["DASHBOARD_MAX_LLM_TOKENS"]["type"] == "int"
    assert items["REACT_ENABLE_CONTEXT_COMPACTION"]["type"] == "bool"
    assert items["REACT_CONTEXT_RECENT_ROUNDS"]["type"] == "int"
    assert items["REACT_DEDUP_MODE"]["choices"] == ["exact", "off"]

    cache_expire = items["CACHE_EXPIRE_HOURS"]
    assert cache_expire["type"] == "int"
    assert isinstance(cache_expire["value"], int)


def test_web_config_apply_writes_env_and_reloads_runtime_settings(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from fastapi.testclient import TestClient

    from config import Config
    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage
    from utils.config_manager import ConfigManager

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join([
            "OPENAI_API_KEY=sk-test",
            "DASHBOARD_ENABLED=true",
            "DASHBOARD_MAX_LLM_TOKENS=1000",
            "MX_APIKEY=old-mx",
            "",
        ]),
        encoding="utf-8",
    )

    saved = {
        "DASHBOARD_ENABLED": Config.DASHBOARD_ENABLED,
        "DASHBOARD_MAX_LLM_TOKENS": Config.DASHBOARD_MAX_LLM_TOKENS,
        "REACT_ENABLE_CONTEXT_COMPACTION": Config.REACT_ENABLE_CONTEXT_COMPACTION,
        "REACT_CONTEXT_RECENT_ROUNDS": Config.REACT_CONTEXT_RECENT_ROUNDS,
        "MX_APIKEY": Config.MX_APIKEY,
    }
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "OPENAI_API_KEY",
            "DASHBOARD_ENABLED",
            "DASHBOARD_MAX_LLM_TOKENS",
            "REACT_ENABLE_CONTEXT_COMPACTION",
            "REACT_CONTEXT_RECENT_ROUNDS",
            "MX_APIKEY",
        )
    }

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

    try:
        app = create_app(state)
        with TestClient(app) as client:
            res = client.post(
                "/api/config/apply",
                json={"changes": {
                    "DASHBOARD_ENABLED": False,
                    "DASHBOARD_MAX_LLM_TOKENS": 777,
                    "REACT_ENABLE_CONTEXT_COMPACTION": False,
                    "REACT_CONTEXT_RECENT_ROUNDS": 4,
                    "MX_APIKEY": "new-mx",
                }},
            )

        assert res.status_code == 200
        assert res.json()["success"] is True
        content = env_path.read_text(encoding="utf-8")
        assert "DASHBOARD_ENABLED=false" in content
        assert "DASHBOARD_MAX_LLM_TOKENS=777" in content
        assert "REACT_ENABLE_CONTEXT_COMPACTION=false" in content
        assert "REACT_CONTEXT_RECENT_ROUNDS=4" in content
        assert "MX_APIKEY=new-mx" in content
        assert Config.DASHBOARD_ENABLED is False
        assert Config.DASHBOARD_MAX_LLM_TOKENS == 777
        assert Config.REACT_ENABLE_CONTEXT_COMPACTION is False
        assert Config.REACT_CONTEXT_RECENT_ROUNDS == 4
        assert Config.MX_APIKEY == "new-mx"
    finally:
        for key, value in saved.items():
            setattr(Config, key, value)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        ConfigManager._instance = None
