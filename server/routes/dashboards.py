"""仪表盘配置路由：/api/dashboards..."""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from agents.analysis.dashboard_config import (
    get_dashboard_def, list_dashboards, get_associations, get_dashboard_category,
)
from server.dashboard_service import (
    save_dashboard as svc_save_dashboard,
    import_dashboard as svc_import_dashboard,
    load_associations as svc_load_associations,
    save_associations as svc_save_associations,
)
from server.schemas import DashboardSaveRequest, DashboardImportRequest, DashboardAssociationsSaveRequest

router = APIRouter()


@router.get("/api/dashboards")
async def dashboards_list():
    """返回所有仪表盘定义和关联关系。"""
    ids = list_dashboards()
    defs = []
    for did in ids:
        d = get_dashboard_def(did)
        if d:
            defs.append(d)
        else:
            defs.append({"id": did, "display_name": did, "description": "fallback"})
    associations = get_associations()
    return {
        "dashboards": defs,
        "associations": associations,
    }


@router.get("/api/dashboards/associations")
async def get_dashboard_associations():
    """返回仪表盘关联关系。"""
    return svc_load_associations()


@router.put("/api/dashboards/associations")
async def save_dashboard_associations(req: DashboardAssociationsSaveRequest):
    """保存仪表盘关联关系。"""
    return svc_save_associations(req.data)


@router.get("/api/dashboards/{dashboard_id}")
async def get_dashboard(dashboard_id: str):
    """返回单个仪表盘定义。"""
    defn = get_dashboard_def(dashboard_id)
    if not defn:
        raise HTTPException(status_code=404, detail=f"dashboard {dashboard_id} not found")
    return defn


@router.put("/api/dashboards/{dashboard_id}")
async def save_dashboard(dashboard_id: str, req: DashboardSaveRequest):
    """保存仪表盘定义。"""
    return svc_save_dashboard(dashboard_id, req.definition)


@router.post("/api/dashboards/import")
async def import_dashboard(req: DashboardImportRequest):
    """导入仪表盘 JSON。"""
    content = json.dumps(req.definition, ensure_ascii=False).encode("utf-8")
    return svc_import_dashboard(content)


@router.get("/api/dashboards/resolve/{template_id}")
async def resolve_dashboard(template_id: str):
    """根据 template_id 解析对应的仪表盘定义。"""
    cat = get_dashboard_category(template_id)
    if not cat:
        raise HTTPException(status_code=404, detail=f"no dashboard for template {template_id}")
    defn = get_dashboard_def(cat.dashboard_id)
    return defn or {"id": cat.dashboard_id, "scenario_tag": cat.scenario_tag}
