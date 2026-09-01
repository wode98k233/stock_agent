"""报告模板管理路由：/api/report-templates..."""
import json

from fastapi import APIRouter, HTTPException

from server.report_template_service import (
    read_report_templates_index, load_template, load_template_blocks,
    save_template_file, toggle_template_enabled, import_template_file,
)
from server.schemas import (
    ReportTemplateSaveRequest, ReportTemplateToggleRequest,
    ReportTemplateImportRequest, RouteRulesSaveRequest, RouteTestRequest,
)

router = APIRouter()

# 场景元数据字段列表
_METADATA_FIELDS = [
    "report_family", "asset_type", "intent_type",
    "dashboard_type", "verbosity", "max_report_words",
]


@router.get("/api/report-templates")
async def report_templates():
    index = read_report_templates_index()
    items = []
    for template_id, entry in (index.get("templates") or {}).items():
        item = dict(entry)
        item["id"] = template_id
        try:
            _, template, _ = load_template(template_id, index)
            item["version"] = template.get("version", "")
            item["block_count"] = len(template.get("output_blocks") or [])
            item["required_skill_count"] = len(template.get("required_skills") or [])
            item["data_contract_count"] = len(template.get("data_contract") or [])
            # 场景元数据
            for field in _METADATA_FIELDS:
                if field in template:
                    item[field] = template[field]
        except Exception as exc:
            item["error"] = str(exc)
            item["block_count"] = 0
            item["required_skill_count"] = 0
            item["data_contract_count"] = 0
        items.append(item)
    blocks = load_template_blocks()
    return {
        "default": index.get("default"),
        "templates": items,
        "blocks": blocks,
        "skills": index.get("skills") or {},
        "capability_mapping": index.get("capability_mapping") or {},
    }


@router.get("/api/report-templates/route-rules")
async def get_route_rules():
    from server.report_template_service import load_route_rules
    return {"rules": load_route_rules()}


@router.put("/api/report-templates/{template_id}/route-rules")
async def save_template_route_rules(template_id: str, payload: RouteRulesSaveRequest):
    from server.report_template_service import save_route_rules
    save_route_rules(template_id, payload.keywords)
    return {"success": True, "template_id": template_id, "keywords": payload.keywords}


@router.post("/api/report-templates/route-test")
async def route_test(payload: RouteTestRequest):
    from server.report_template_service import load_route_rules, read_report_templates_index
    from agents.analysis.template_store import _score_route

    rules = load_route_rules()
    index = read_report_templates_index()

    results = []
    for tid, keywords in rules.items():
        entry = index.get("templates", {}).get(tid, {})
        if not entry.get("enabled", True):
            continue
        score, matched = _score_route(payload.text, keywords)
        results.append({
            "template_id": tid,
            "name": entry.get("name", tid),
            "score": score,
            "matched_keywords": matched,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    winner = results[0]["template_id"] if results and results[0]["score"] > 0 else None

    return {
        "text": payload.text,
        "results": results,
        "winner": winner,
        "default": index.get("default", "standard"),
    }


@router.get("/api/report-templates/{template_id}")
async def get_report_template(template_id: str):
    from server.report_template_service import get_report_templates_root
    entry, template, path = load_template(template_id)
    root = get_report_templates_root().resolve()
    return {
        "id": template_id,
        "entry": dict(entry),
        "template": template,
        "path": str(path.relative_to(root)).replace("\\", "/"),
    }


@router.put("/api/report-templates/{template_id}")
async def save_report_template(template_id: str, payload: ReportTemplateSaveRequest):
    result = save_template_file(template_id, payload.template)
    return {"success": True, **result}


@router.patch("/api/report-templates/{template_id}/toggle")
async def toggle_report_template(template_id: str, payload: ReportTemplateToggleRequest):
    """切换模板启用/禁用状态。"""
    result = toggle_template_enabled(template_id, payload.enabled)
    return {"success": True, **result}


@router.post("/api/report-templates/import")
async def import_report_template(payload: ReportTemplateImportRequest):
    """导入模板（通过 JSON body 传递模板内容）。"""
    content = json.dumps(payload.template, ensure_ascii=False).encode("utf-8")
    filename = f"{payload.template.get('id', 'unknown')}/template.json"
    result = import_template_file(content, filename)
    return {"success": True, **result}
