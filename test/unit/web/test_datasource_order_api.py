"""数据源优先级 API 端点测试（PUT/DELETE /api/datasources/order）。

两处隔离，缺一不可：
1. **持久化隔离**：端点会写 .env，测试里把 `ConfigManager.write_env_changes` 打成 no-op；
2. **源集合隔离**：`DataSourceManager._sources` 是类级全局，其他测试会替换它且顺序不定
   （全量跑时曾撞到残留的 `test_source`）——这里显式用可控替身，保证确定性。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_SOURCES = [("mx_data", 110), ("sina_direct", 100), ("hithink", 95), ("akshare", 90)]


class _FakeSource:
    """满足 DataSourceManager.status() 读取所需的最小属性集。"""

    def __init__(self, name, priority):
        self.name = name
        self.priority = priority
        self.label = name
        self.description = f"{name} 描述"
        self.enabled = True

    @classmethod
    def is_available(cls):
        return True


@pytest.fixture
def client(monkeypatch):
    from server.routes import meta
    from tools.fetcher.base import DataSourceManager

    # 1) 可控源集合
    monkeypatch.setattr(DataSourceManager, "_sources",
                        [_FakeSource(n, p) for n, p in _SOURCES])
    monkeypatch.setattr(DataSourceManager, "_user_order", [])
    # _build_datasources_payload 内部会调 _ensure_initialized() 注册**真实**源，
    # 若它尚未初始化就会把真源追加到替身列表里 → 打桩为 no-op 保证确定性
    import tools.fetcher as _fetcher
    monkeypatch.setattr(_fetcher, "_ensure_initialized", lambda: None)

    # 2) 隔离持久化：不写真实 .env
    calls = []

    def _noop_write(self, changes, env_path=None):
        calls.append((dict(changes), env_path))

    monkeypatch.setattr("utils.config_manager.ConfigManager.write_env_changes", _noop_write)

    app = FastAPI()
    app.include_router(meta.router)
    c = TestClient(app)
    c._write_calls = calls
    yield c


def test_datasources_payload_has_order_fields(client):
    r = client.get("/api/datasources?refresh=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["name"] for s in body["sources"]] == [n for n, _ in _SOURCES]
    assert body["customized"] is False
    for key in ("name", "label", "description", "available", "priority", "order_index"):
        assert key in body["sources"][0], f"缺少字段 {key}"


def test_put_order_applies_and_persists(client):
    names = ["hithink", "akshare", "mx_data"]
    r = client.put("/api/datasources/order", json={"names": names})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["customized"] is True
    # 未列出的源补在其后
    assert body["order"] == ["hithink", "akshare", "mx_data", "sina_direct"]

    assert client._write_calls, "应调用 write_env_changes 持久化"
    changes, _env = client._write_calls[-1]
    assert changes["DATASOURCE_PRIORITY_ORDER"] == "hithink,akshare,mx_data,sina_direct"

    r2 = client.get("/api/datasources?refresh=1")
    assert r2.json()["customized"] is True
    assert [s["name"] for s in r2.json()["sources"]] == ["hithink", "akshare", "mx_data", "sina_direct"]


def test_put_order_rejects_unknown_source(client):
    r = client.put("/api/datasources/order", json={"names": ["hithink", "nope"]})
    assert r.status_code == 400
    assert "未知数据源" in r.json()["detail"]


def test_delete_order_restores_builtin(client):
    client.put("/api/datasources/order", json={"names": ["hithink", "akshare"]})
    r = client.delete("/api/datasources/order")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customized"] is False
    # 恢复内置 priority 降序
    assert body["order"] == [n for n, _ in sorted(_SOURCES, key=lambda x: -x[1])]
