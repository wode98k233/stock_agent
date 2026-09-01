"""报告模板服务的导入边界与写入行为测试。"""
import json

import pytest
from fastapi import HTTPException


@pytest.fixture
def template_root(tmp_path, monkeypatch):
    root = tmp_path / "report_templates"
    root.mkdir()
    (root / "blocks").mkdir()
    (root / "index.json").write_text(
        json.dumps({"default": "standard", "templates": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "server.report_template_service.get_report_templates_root",
        lambda: root,
    )
    return root


@pytest.fixture
def runtime_template_store(template_root, monkeypatch):
    from agents.analysis import template_store

    monkeypatch.setattr(template_store, "get_template_dir", lambda: str(template_root))
    template_store._rule_cache.clear()
    template_store._template_cache.clear()
    template_store._block_cache.clear()
    template_store._index_cache = None
    template_store._route_rules_cache = None
    yield template_store
    template_store._rule_cache.clear()
    template_store._template_cache.clear()
    template_store._block_cache.clear()
    template_store._index_cache = None
    template_store._route_rules_cache = None


def _write_runtime_template(root, name="旧模板"):
    template = {
        "id": "standard",
        "name": name,
        "version": "1.0",
        "role": "测试角色",
        "sections": [],
    }
    template_dir = root / "standard"
    template_dir.mkdir(exist_ok=True)
    (template_dir / "template.json").write_text(
        json.dumps(template, ensure_ascii=False), encoding="utf-8"
    )
    (root / "index.json").write_text(
        json.dumps(
            {
                "default": "standard",
                "templates": {
                    "standard": {
                        "name": name,
                        "path": "standard/template.json",
                        "enabled": True,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return template


@pytest.mark.parametrize(
    "template_id",
    ["../escape", r"..\escape", "a/b", r"a\b", "bad id", ".hidden", "a" * 65],
)
def test_import_template_rejects_invalid_id_without_writing(template_root, template_id):
    from server.report_template_service import import_template_file

    original_index = (template_root / "index.json").read_text(encoding="utf-8")
    payload = json.dumps({"id": template_id, "name": "非法模板"}).encode("utf-8")

    with pytest.raises(HTTPException) as exc_info:
        import_template_file(payload, "ignored.json")

    assert exc_info.value.status_code == 400
    assert (template_root / "index.json").read_text(encoding="utf-8") == original_index


def test_import_template_rejects_absolute_id_without_writing(template_root, tmp_path):
    from server.report_template_service import import_template_file

    outside = tmp_path / "outside_template"
    payload = json.dumps({"id": str(outside), "name": "非法模板"}).encode("utf-8")

    with pytest.raises(HTTPException) as exc_info:
        import_template_file(payload, "ignored.json")

    assert exc_info.value.status_code == 400
    assert not (outside / "template.json").exists()


def test_import_template_validates_payload_before_writing(template_root):
    from server.report_template_service import import_template_file

    payload = json.dumps(
        {"id": "bad_blocks", "name": "错误模板", "output_blocks": "not-a-list"}
    ).encode("utf-8")

    with pytest.raises(HTTPException) as exc_info:
        import_template_file(payload, "ignored.json")

    assert exc_info.value.status_code == 400
    assert not (template_root / "bad_blocks").exists()


def test_import_template_writes_valid_id_and_updates_index(template_root):
    from server.report_template_service import import_template_file

    payload = json.dumps(
        {"id": "market-daily", "name": "市场日报", "output_blocks": []},
        ensure_ascii=False,
    ).encode("utf-8")

    result = import_template_file(payload, "ignored.json")

    assert result["id"] == "market-daily"
    assert (template_root / "market-daily" / "template.json").is_file()
    index = json.loads((template_root / "index.json").read_text(encoding="utf-8"))
    assert index["templates"]["market-daily"]["path"] == "market-daily/template.json"


def test_save_template_is_visible_to_runtime_cache_immediately(
    template_root, runtime_template_store
):
    from server.report_template_service import save_template_file

    old_template = _write_runtime_template(template_root)
    assert runtime_template_store.load_template("standard")["name"] == "旧模板"

    new_template = dict(old_template, name="新模板")
    save_template_file("standard", new_template)

    assert runtime_template_store.load_template("standard")["name"] == "新模板"


def test_toggle_template_is_visible_to_runtime_index_immediately(
    template_root, runtime_template_store
):
    from server.report_template_service import toggle_template_enabled

    _write_runtime_template(template_root)
    assert "standard" in runtime_template_store._enabled_template_ids()

    toggle_template_enabled("standard", False)

    assert "standard" not in runtime_template_store._enabled_template_ids()


def test_import_template_is_visible_to_runtime_index_immediately(
    template_root, runtime_template_store
):
    from server.report_template_service import import_template_file

    _write_runtime_template(template_root)
    assert "imported" not in runtime_template_store._enabled_template_ids()
    payload = json.dumps(
        {
            "id": "imported",
            "name": "导入模板",
            "version": "1.0",
            "role": "测试角色",
            "sections": [],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    import_template_file(payload, "ignored.json")

    assert "imported" in runtime_template_store._enabled_template_ids()
