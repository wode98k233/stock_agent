"""仪表盘配置读写服务。"""
import json
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException


VALID_DASHBOARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def get_dashboards_root() -> Path:
    from utils.app_paths import get_resource_root
    return Path(get_resource_root()) / "agents" / "report_templates" / "dashboards"


def read_dashboards_index(http_exception_cls=HTTPException) -> dict[str, Any]:
    root = get_dashboards_root()
    index_path = root / "index.json"
    if not index_path.exists():
        raise http_exception_cls(status_code=404, detail="仪表盘索引不存在")
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise http_exception_cls(status_code=500, detail=f"仪表盘索引 JSON 无效: {exc.msg}")


def safe_dashboard_path(dashboard_id: str, http_exception_cls=HTTPException) -> Path:
    if not VALID_DASHBOARD_ID_RE.fullmatch(dashboard_id):
        raise http_exception_cls(status_code=400, detail="仪表盘 ID 非法")
    root = get_dashboards_root().resolve()
    target = (root / f"{dashboard_id}.json").resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise http_exception_cls(status_code=400, detail="仪表盘路径非法")
    return target


def load_dashboard(dashboard_id: str, http_exception_cls=HTTPException) -> tuple[dict[str, Any], Path]:
    path = safe_dashboard_path(dashboard_id, http_exception_cls)
    if not path.exists():
        raise http_exception_cls(status_code=404, detail=f"仪表盘 {dashboard_id} 不存在")
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise http_exception_cls(status_code=500, detail=f"仪表盘 JSON 无效: {exc.msg}")
    return definition, path


def validate_dashboard_payload(definition: dict[str, Any], http_exception_cls=HTTPException) -> None:
    if not isinstance(definition, dict):
        raise http_exception_cls(status_code=400, detail="definition 必须是 JSON 对象")
    if "sections" in definition and not isinstance(definition["sections"], list):
        raise http_exception_cls(status_code=400, detail="sections 必须是数组")
    if "llm_fields" in definition and not isinstance(definition["llm_fields"], list):
        raise http_exception_cls(status_code=400, detail="llm_fields 必须是数组")
    if "checklist_dimensions" in definition and not isinstance(definition["checklist_dimensions"], list):
        raise http_exception_cls(status_code=400, detail="checklist_dimensions 必须是数组")


def save_dashboard(dashboard_id: str, definition: dict[str, Any], http_exception_cls=HTTPException) -> dict[str, Any]:
    validate_dashboard_payload(definition, http_exception_cls)
    payload_id = definition.get("id")
    if payload_id and payload_id != dashboard_id:
        raise http_exception_cls(status_code=400, detail="仪表盘 id 不允许改名")

    root = get_dashboards_root()
    index = read_dashboards_index(http_exception_cls)
    dashboards = index.get("dashboards") or []

    # 新仪表盘自动注册
    if dashboard_id not in dashboards:
        dashboards.append(dashboard_id)
        index["dashboards"] = dashboards
        index_path = root / "index.json"
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    definition["id"] = dashboard_id
    path = safe_dashboard_path(dashboard_id, http_exception_cls)
    path.write_text(
        json.dumps(definition, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"id": dashboard_id, "saved": True}


def import_dashboard(file_content: bytes, http_exception_cls=HTTPException) -> dict[str, Any]:
    try:
        definition = json.loads(file_content)
    except json.JSONDecodeError as exc:
        raise http_exception_cls(status_code=400, detail=f"JSON 解析失败: {exc.msg}")

    validate_dashboard_payload(definition, http_exception_cls)
    dashboard_id = definition.get("id")
    if not dashboard_id or not isinstance(dashboard_id, str):
        raise http_exception_cls(status_code=400, detail="仪表盘缺少 id 字段")

    path = safe_dashboard_path(dashboard_id, http_exception_cls)

    root = get_dashboards_root()
    index = read_dashboards_index(http_exception_cls)
    dashboards = index.get("dashboards") or []

    action = "overwrite" if dashboard_id in dashboards else "create"

    # 写入文件
    path.write_text(
        json.dumps(definition, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 更新 index.json
    if dashboard_id not in dashboards:
        dashboards.append(dashboard_id)
        index["dashboards"] = dashboards
        index_path = root / "index.json"
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return {"id": dashboard_id, "action": action}


def load_associations(http_exception_cls=HTTPException) -> dict[str, Any]:
    root = get_dashboards_root()
    path = root / "associations.json"
    if not path.exists():
        return {"version": 1, "mappings": [], "standalone": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise http_exception_cls(status_code=500, detail=f"关联文件 JSON 无效: {exc.msg}")


def save_associations(data: dict[str, Any], http_exception_cls=HTTPException) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise http_exception_cls(status_code=400, detail="关联数据必须是 JSON 对象")
    root = get_dashboards_root()
    path = root / "associations.json"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"saved": True}
