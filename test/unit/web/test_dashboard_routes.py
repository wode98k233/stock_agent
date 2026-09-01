"""
仪表盘 API 路由单元测试

验证 /api/dashboards 路由的行为，使用 tmp_path 隔离文件系统。
运行: pytest test/unit/web/test_dashboard_routes.py -v
"""
import json
import pytest
from pathlib import Path


@pytest.fixture
def dashboards_dir(tmp_path):
    """创建临时仪表盘目录"""
    d = tmp_path / "dashboards"
    d.mkdir()

    index = {"version": 1, "dashboards": ["stock"], "default": "stock"}
    (d / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    stock = {
        "id": "stock",
        "display_name": "个股仪表盘",
        "description": "测试用",
        "sections": [{"id": "s1", "renderer": "test", "fields": ["f1"]}],
        "llm_fields": [{"name": "f1", "type": "str", "description": "字段1", "example": "v"}],
        "checklist_dimensions": ["技术面"],
    }
    (d / "stock.json").write_text(json.dumps(stock, ensure_ascii=False, indent=2), encoding="utf-8")

    assoc = {"version": 1, "mappings": [], "standalone": []}
    (d / "associations.json").write_text(json.dumps(assoc, ensure_ascii=False, indent=2), encoding="utf-8")

    return d


@pytest.fixture
def client(tmp_path, dashboards_dir, monkeypatch):
    """创建 FastAPI TestClient，mock 掉 dashboards_root"""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.storage import WebStorage
    from server.runtime import TaskRuntime
    from types import SimpleNamespace

    monkeypatch.setattr("server.dashboard_service.get_dashboards_root", lambda: dashboards_dir)
    # 也需要 mock dashboard_config 的缓存，让它读临时目录
    monkeypatch.setattr("agents.analysis.dashboard_config._DASHBOARDS_DIR", dashboards_dir)

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(None),
        agent_context=SimpleNamespace(),
    )
    app = create_app(state)
    # 必须用 with 进入上下文：路由注册在 lifespan 阶段，不进入则 404
    with TestClient(app) as c:
        yield c


# ══════════════════════════════════════════
# GET
# ══════════════════════════════════════════

def test_list_dashboards(client):
    res = client.get("/api/dashboards")
    assert res.status_code == 200
    data = res.json()
    assert "dashboards" in data
    assert "associations" in data
    assert len(data["dashboards"]) == 1
    assert data["dashboards"][0]["id"] == "stock"


def test_get_dashboard(client):
    res = client.get("/api/dashboards/stock")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "stock"
    assert data["display_name"] == "个股仪表盘"


def test_get_dashboard_not_found(client):
    res = client.get("/api/dashboards/nonexistent")
    assert res.status_code == 404


def test_get_associations(client):
    res = client.get("/api/dashboards/associations")
    assert res.status_code == 200
    data = res.json()
    assert "mappings" in data
    assert "standalone" in data


# ══════════════════════════════════════════
# PUT 保存
# ══════════════════════════════════════════

def test_save_dashboard(client, dashboards_dir):
    new_def = {
        "id": "stock",
        "display_name": "更新后的仪表盘",
        "sections": [],
        "llm_fields": [],
    }
    res = client.put("/api/dashboards/stock", json={"definition": new_def})
    assert res.status_code == 200
    assert res.json()["saved"] is True

    # 验证文件已更新
    saved = json.loads((dashboards_dir / "stock.json").read_text(encoding="utf-8"))
    assert saved["display_name"] == "更新后的仪表盘"


def test_save_dashboard_id_mismatch(client):
    res = client.put("/api/dashboards/stock", json={"definition": {"id": "other", "sections": []}})
    assert res.status_code == 400


def test_save_dashboard_invalid_payload(client):
    res = client.put("/api/dashboards/stock", json={"definition": {"sections": "bad"}})
    assert res.status_code == 400


# ══════════════════════════════════════════
# POST 导入
# ══════════════════════════════════════════

def test_import_dashboard(client, dashboards_dir):
    res = client.post("/api/dashboards/import", json={
        "definition": {"id": "imported", "display_name": "导入的", "sections": []}
    })
    assert res.status_code == 200
    assert res.json()["id"] == "imported"
    assert res.json()["action"] == "create"

    # 清理
    (dashboards_dir / "imported.json").unlink(missing_ok=True)


def test_import_dashboard_missing_id(client):
    res = client.post("/api/dashboards/import", json={
        "definition": {"display_name": "没有id"}
    })
    assert res.status_code == 400


# ══════════════════════════════════════════
# PUT 关联
# ══════════════════════════════════════════

def test_save_associations(client, dashboards_dir):
    new_assoc = {
        "version": 1,
        "mappings": [{"template_id": "t1", "dashboard_id": "stock"}],
        "standalone": [{"dashboard_id": "stock", "label": "测试"}],
    }
    res = client.put("/api/dashboards/associations", json={"data": new_assoc})
    assert res.status_code == 200

    saved = json.loads((dashboards_dir / "associations.json").read_text(encoding="utf-8"))
    assert len(saved["mappings"]) == 1
    assert saved["mappings"][0]["template_id"] == "t1"
