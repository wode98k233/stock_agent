"""Skill 管理路由：/api/skills..."""
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Any, Dict, Optional

from server.deps import get_web_state
from server.schemas import SkillEnabledRequest, SkillImportRequest

router = APIRouter()


def _get_reg(request: Request):
    """获取 bootstrap 阶段创建的 SkillRegister 单例"""
    return get_web_state(request).agent_context.skill_register


def _missing_imported_skills(reg, preview) -> list[str]:
    expected = preview.tools if preview.detected_type == "external_package" else [preview.skill_name]
    if not expected:
        return [preview.skill_name or "<unknown>"]
    missing = []
    for skill_name in expected:
        meta = reg.get_skill_unchecked(skill_name)
        has_bound_tool = meta and any(tool.tool_func is not None for tool in meta.tools)
        if not meta or not (meta.build_tools_func or meta.tool_registry or has_bound_tool):
            missing.append(skill_name)
    return missing


@router.get("/api/skills")
async def list_skills(request: Request):
    """返回当前注册表中的所有技能列表（含禁用）"""
    reg = _get_reg(request)
    items = []
    for name, meta in reg.get_all_skills_unchecked().items():
        items.append({
            "name": meta.skill_name,
            "version": meta.skill_version,
            "category": meta.category,
            "description": meta.skill_desc,
            "source": meta.source,
            "enabled": meta.enabled,
            "tools_count": len(meta.tools),
            "has_build_tools": meta.build_tools_func is not None,
        })
    return {"items": items}


@router.get("/api/skills/{skill_name}")
async def get_skill_detail(skill_name: str, request: Request):
    """获取单个技能详情"""
    reg = _get_reg(request)
    detail = reg.get_skill_detail(skill_name)
    if not detail:
        raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 不存在")
    return detail


@router.patch("/api/skills/{skill_name}/enabled")
async def toggle_skill_enabled(skill_name: str, payload: SkillEnabledRequest, request: Request):
    """启用/禁用技能"""
    reg = _get_reg(request)
    if not reg.set_skill_enabled(skill_name, payload.enabled):
        raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 不存在")
    return {"ok": True, "name": skill_name, "enabled": payload.enabled}


@router.post("/api/skills/rescan")
async def rescan_skills(request: Request):
    """重新扫描所有技能目录"""
    reg = _get_reg(request)
    reg.rescan()
    items = []
    for name, meta in reg.get_all_skills_unchecked().items():
        items.append({
            "name": meta.skill_name,
            "source": meta.source,
            "enabled": meta.enabled,
            "tools_count": len(meta.tools),
        })
    return {"ok": True, "items": items}


@router.delete("/api/skills/{skill_name}")
async def delete_skill(skill_name: str, request: Request):
    """删除用户导入的技能（内置/外部技能不允许删除）"""
    reg = _get_reg(request)
    if not reg.delete_user_skill(skill_name):
        raise HTTPException(status_code=400, detail=f"技能 '{skill_name}' 不存在或不允许删除（仅用户导入可删）")
    return {"ok": True, "name": skill_name}


@router.post("/api/skills/validate-import")
async def validate_skill_import(payload: SkillImportRequest, request: Request):
    """预检导入：校验 zip 结构、工具构建，不安装"""
    from server.skill_importer import preview_import
    reg = _get_reg(request)
    existing = list(reg.get_all_skills_unchecked().keys())
    result = preview_import(payload.zip_path, existing_skills=existing)
    return result.to_dict()


@router.post("/api/skills/import")
async def import_skill(payload: SkillImportRequest, request: Request):
    """确认导入：校验 + 安装到 user_skills/ + 重新扫描"""
    from server.skill_importer import preview_import, install_skill
    reg = _get_reg(request)
    existing = list(reg.get_all_skills_unchecked().keys())
    preview = preview_import(payload.zip_path, existing_skills=existing)
    if preview.status == "error":
        raise HTTPException(status_code=400, detail={"message": "导入预检失败", "preview": preview.to_dict()})
    install_path = install_skill(payload.zip_path, preview)
    reg.rescan()
    missing = _missing_imported_skills(reg, preview)
    if missing:
        raise HTTPException(
            status_code=500,
            detail={"message": "技能已复制但重新扫描后不可用", "skills": missing},
        )
    return {
        "ok": True,
        "name": preview.skill_name,
        "install_path": install_path,
        "preview": preview.to_dict(),
    }


class ToolExecuteRequest(BaseModel):
    params: Dict[str, Any] = {}


@router.post("/api/skills/{skill_name}/tools/{tool_name}/execute")
async def execute_skill_tool(skill_name: str, tool_name: str, payload: ToolExecuteRequest, request: Request):
    """执行指定 skill 的指定 tool，返回结果或错误"""
    reg = _get_reg(request)

    start = time.time()
    try:
        result = reg.execute_tool(skill_name, tool_name, payload.params)
        duration_ms = round((time.time() - start) * 1000)
        return {
            "ok": True,
            "result": result if isinstance(result, (dict, list)) else str(result),
            "duration_ms": duration_ms,
        }
    except ValueError as e:
        duration_ms = round((time.time() - start) * 1000)
        return {"ok": False, "error": str(e), "duration_ms": duration_ms}
    except Exception as e:
        duration_ms = round((time.time() - start) * 1000)
        return {"ok": False, "error": f"执行异常: {type(e).__name__}: {e}", "duration_ms": duration_ms}
