"""
仪表盘配置服务单元测试

验证 dashboard_service 的读写逻辑，使用 tmp_path 隔离文件系统。
运行: pytest test/unit/web/test_dashboard_service.py -v
"""
import json
import pytest
from pathlib import Path


@pytest.fixture
def dashboards_dir(tmp_path):
    """创建临时仪表盘目录，包含 index.json、stock.json、associations.json"""
    d = tmp_path / "dashboards"
    d.mkdir()

    index = {"version": 1, "dashboards": ["stock", "market"], "default": "stock"}
    (d / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    stock = {
        "id": "stock",
        "display_name": "个股决策仪表盘",
        "description": "个股买卖点",
        "scenario_tag": "个股决策",
        "checklist_dimensions": ["技术面", "基本面"],
        "sections": [
            {"id": "signal_row", "renderer": "signal_row", "required": True, "fields": ["price_levels"]},
            {"id": "verdict", "renderer": "verdict_text", "required": True, "fields": ["position_guidance"]},
        ],
        "llm_fields": [
            {"name": "price_levels", "type": "dict", "description": "支撑/压力位", "example": "{'support': 1800}"},
        ],
    }
    (d / "stock.json").write_text(json.dumps(stock, ensure_ascii=False, indent=2), encoding="utf-8")

    market = {
        "id": "market",
        "display_name": "市场全景仪表盘",
        "description": "大盘温度",
        "sections": [],
        "llm_fields": [],
    }
    (d / "market.json").write_text(json.dumps(market, ensure_ascii=False, indent=2), encoding="utf-8")

    assoc = {
        "version": 1,
        "mappings": [{"template_id": "market_daily", "dashboard_id": "market"}],
        "standalone": [{"dashboard_id": "market", "label": "市场全景"}],
    }
    (d / "associations.json").write_text(json.dumps(assoc, ensure_ascii=False, indent=2), encoding="utf-8")

    return d


@pytest.fixture
def _patch_dashboards_root(monkeypatch, dashboards_dir):
    """将 dashboard_service 的根目录指向临时目录"""
    monkeypatch.setattr("server.dashboard_service.get_dashboards_root", lambda: dashboards_dir)


# ══════════════════════════════════════════
# 读取
# ══════════════════════════════════════════

def test_read_dashboards_index(_patch_dashboards_root):
    from server.dashboard_service import read_dashboards_index
    idx = read_dashboards_index()
    assert idx["dashboards"] == ["stock", "market"]
    assert idx["default"] == "stock"


def test_read_dashboards_index_missing(tmp_path, monkeypatch):
    from server.dashboard_service import read_dashboards_index
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr("server.dashboard_service.get_dashboards_root", lambda: empty)
    with pytest.raises(Exception):
        read_dashboards_index()


def test_load_dashboard(_patch_dashboards_root):
    from server.dashboard_service import load_dashboard
    defn, path = load_dashboard("stock")
    assert defn["id"] == "stock"
    assert defn["display_name"] == "个股决策仪表盘"
    assert len(defn["sections"]) == 2
    assert len(defn["llm_fields"]) == 1


def test_load_dashboard_not_found(_patch_dashboards_root):
    from server.dashboard_service import load_dashboard
    with pytest.raises(Exception):
        load_dashboard("nonexistent")


def test_load_associations(_patch_dashboards_root):
    from server.dashboard_service import load_associations
    assoc = load_associations()
    assert len(assoc["mappings"]) == 1
    assert assoc["mappings"][0]["template_id"] == "market_daily"
    assert len(assoc["standalone"]) == 1


def test_load_associations_missing(tmp_path, monkeypatch):
    """associations.json 不存在时返回空结构"""
    from server.dashboard_service import load_associations
    d = tmp_path / "dashboards"
    d.mkdir()
    (d / "index.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("server.dashboard_service.get_dashboards_root", lambda: d)
    assoc = load_associations()
    assert assoc["mappings"] == []
    assert assoc["standalone"] == []


# ══════════════════════════════════════════
# 保存
# ══════════════════════════════════════════

def test_save_dashboard_updates_file(_patch_dashboards_root, dashboards_dir):
    from server.dashboard_service import save_dashboard, load_dashboard
    new_def = {
        "id": "stock",
        "display_name": "个股仪表盘 v2",
        "description": "更新后的描述",
        "sections": [{"id": "s1", "renderer": "test", "fields": []}],
        "llm_fields": [],
    }
    result = save_dashboard("stock", new_def)
    assert result["saved"] is True

    # 验证文件内容
    defn, _ = load_dashboard("stock")
    assert defn["display_name"] == "个股仪表盘 v2"
    assert defn["description"] == "更新后的描述"
    assert len(defn["sections"]) == 1


def test_save_dashboard_new_registers_in_index(_patch_dashboards_root, dashboards_dir):
    from server.dashboard_service import save_dashboard, read_dashboards_index
    new_def = {
        "id": "new_dashboard",
        "display_name": "新仪表盘",
        "sections": [],
        "llm_fields": [],
    }
    save_dashboard("new_dashboard", new_def)

    idx = read_dashboards_index()
    assert "new_dashboard" in idx["dashboards"]

    # 清理：删除新建的文件
    (dashboards_dir / "new_dashboard.json").unlink(missing_ok=True)


def test_save_dashboard_rejects_id_mismatch(_patch_dashboards_root):
    from server.dashboard_service import save_dashboard
    with pytest.raises(Exception):
        save_dashboard("stock", {"id": "different_id", "sections": []})


def test_save_dashboard_validates_types(_patch_dashboards_root):
    from server.dashboard_service import save_dashboard
    with pytest.raises(Exception):
        save_dashboard("stock", {"sections": "not a list"})
    with pytest.raises(Exception):
        save_dashboard("stock", {"llm_fields": "not a list"})
    with pytest.raises(Exception):
        save_dashboard("stock", {"checklist_dimensions": "not a list"})


# ══════════════════════════════════════════
# 导入
# ══════════════════════════════════════════

def test_import_dashboard_overwrite(_patch_dashboards_root, dashboards_dir):
    from server.dashboard_service import import_dashboard, load_dashboard
    content = json.dumps({"id": "stock", "display_name": "导入覆盖", "sections": []}).encode()
    result = import_dashboard(content)
    assert result["id"] == "stock"
    assert result["action"] == "overwrite"

    defn, _ = load_dashboard("stock")
    assert defn["display_name"] == "导入覆盖"


def test_import_dashboard_create(_patch_dashboards_root, dashboards_dir):
    from server.dashboard_service import import_dashboard, read_dashboards_index
    content = json.dumps({"id": "brand_new", "display_name": "全新仪表盘", "sections": []}).encode()
    result = import_dashboard(content)
    assert result["id"] == "brand_new"
    assert result["action"] == "create"

    idx = read_dashboards_index()
    assert "brand_new" in idx["dashboards"]

    # 清理
    (dashboards_dir / "brand_new.json").unlink(missing_ok=True)


def test_import_dashboard_missing_id(_patch_dashboards_root):
    from server.dashboard_service import import_dashboard
    content = json.dumps({"display_name": "没有id"}).encode()
    with pytest.raises(Exception):
        import_dashboard(content)


def test_import_dashboard_invalid_json(_patch_dashboards_root):
    from server.dashboard_service import import_dashboard
    with pytest.raises(Exception):
        import_dashboard(b"not json")


def test_import_dashboard_rejects_non_object(_patch_dashboards_root):
    from server.dashboard_service import import_dashboard

    with pytest.raises(Exception) as exc_info:
        import_dashboard(b"[]")

    assert getattr(exc_info.value, "status_code", None) == 400


@pytest.mark.parametrize("dashboard_id", ["../escape", r"..\escape", "a/b", r"a\b"])
def test_import_dashboard_rejects_invalid_id_without_writing(
    _patch_dashboards_root, dashboards_dir, dashboard_id
):
    from server.dashboard_service import import_dashboard

    original_index = (dashboards_dir / "index.json").read_text(encoding="utf-8")
    content = json.dumps({"id": dashboard_id, "sections": []}).encode("utf-8")

    with pytest.raises(Exception) as exc_info:
        import_dashboard(content)

    assert getattr(exc_info.value, "status_code", None) == 400
    assert (dashboards_dir / "index.json").read_text(encoding="utf-8") == original_index


def test_import_dashboard_rejects_absolute_id_without_writing(
    _patch_dashboards_root, dashboards_dir, tmp_path
):
    from server.dashboard_service import import_dashboard

    outside = tmp_path / "outside_dashboard"
    content = json.dumps({"id": str(outside), "sections": []}).encode("utf-8")

    with pytest.raises(Exception) as exc_info:
        import_dashboard(content)

    assert getattr(exc_info.value, "status_code", None) == 400
    assert not outside.with_suffix(".json").exists()


# ══════════════════════════════════════════
# 关联
# ══════════════════════════════════════════

def test_save_associations(_patch_dashboards_root, dashboards_dir):
    from server.dashboard_service import save_associations, load_associations
    new_data = {
        "version": 1,
        "mappings": [
            {"template_id": "t1", "dashboard_id": "stock"},
            {"template_id": "t2", "dashboard_id": "market"},
        ],
        "standalone": [],
    }
    save_associations(new_data)

    loaded = load_associations()
    assert len(loaded["mappings"]) == 2
    assert loaded["standalone"] == []


def test_save_associations_validates_type(_patch_dashboards_root):
    from server.dashboard_service import save_associations
    with pytest.raises(Exception):
        save_associations("not a dict")
