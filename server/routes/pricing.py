"""定价目录配置 API（设计文档 §9.3）。

价目**不含任何 API Key**（凭证仍在 .env），所以可以页面编辑、接口可写；
但保存走文件写，不进 ConfigManager.write_env_changes（.env 红线只管 .env 键）。
"""
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from server import pricing_service as svc

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


class CatalogSaveRequest(BaseModel):
    catalog: Dict[str, Any]


class CatalogValidateRequest(BaseModel):
    catalog: Dict[str, Any]


class TrialRequest(BaseModel):
    model: str = ""
    provider: str = ""
    base_url: str = ""
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    ts: float | None = None


@router.get("/catalog")
async def get_catalog():
    return await run_in_threadpool(svc.read_catalog)


@router.post("/validate")
async def validate_catalog(payload: CatalogValidateRequest):
    return await run_in_threadpool(svc.validate_catalog, payload.catalog)


@router.put("/catalog")
async def put_catalog(payload: CatalogSaveRequest):
    try:
        return await run_in_threadpool(svc.save_catalog, payload.catalog)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/trial")
async def trial(payload: TrialRequest):
    return await run_in_threadpool(svc.trial, payload.model_dump())


@router.post("/reset")
async def reset():
    try:
        return await run_in_threadpool(svc.reset_catalog)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
