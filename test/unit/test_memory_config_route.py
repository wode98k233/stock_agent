"""记忆系统配置接口 (GET/POST /api/memory/config) 集成测试。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import memory.sdk as sdk_mod
from server.routes import memory as memory_routes


class _FakeEmb:
    def __init__(self):
        self.v = None

    def set_min_similarity(self, x):
        self.v = x


class _FakeBackend:
    def __init__(self):
        self._embedding = _FakeEmb()

    def name(self):
        return "hybrid"

    def is_available(self):
        return True

    def count(self):
        return 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(sdk_mod, "get_memory_config_path", lambda: str(tmp_path / "memory_config.json"))

    sdk = sdk_mod.MemorySDK(sdk_mod.MemoryConfig())
    sdk._backend = _FakeBackend()
    sdk._initialized = True

    class _FakeState:
        memory_sdk = sdk

    app = FastAPI()
    app.include_router(memory_routes.router)
    app.state.web = _FakeState()
    return TestClient(app)


def test_get_config_default(client):
    r = client.get("/api/memory/config")
    assert r.status_code == 200
    assert r.json()["min_similarity"] == 0.72


def test_post_config_updates_and_persists(client, tmp_path):
    r = client.post("/api/memory/config", json={"min_similarity": 0.85})
    assert r.status_code == 200
    assert r.json()["min_similarity"] == 0.85
    # 持久化生效
    import json
    data = json.loads((tmp_path / "memory_config.json").read_text(encoding="utf-8"))
    assert data["min_similarity"] == 0.85
    # 再次 GET 反映新值
    assert client.get("/api/memory/config").json()["min_similarity"] == 0.85


def test_post_config_invalid_rejected(client):
    r = client.post("/api/memory/config", json={"min_similarity": 1.5})
    assert r.status_code == 400
