"""策略意图词表配置 API（设计文档 §5.2.3 / 需求单 R3）"""
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from server import strategy_intent_service as svc

router = APIRouter(prefix="/api/strategy-intent", tags=["strategy-intent"])


class RulesSaveRequest(BaseModel):
    rules: dict


class IntentTestRequest(BaseModel):
    text: str = ""


@router.get("/rules")
async def get_rules():
    rules = await run_in_threadpool(svc.read_rules)
    return {"rules": rules, "path": str(svc._RULES_PATH),
            "mtime": svc._RULES_PATH.stat().st_mtime}


@router.put("/rules")
async def put_rules(payload: RulesSaveRequest):
    try:
        return await run_in_threadpool(svc.save_rules, payload.rules)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/test")
async def test_intent(payload: IntentTestRequest):
    return await run_in_threadpool(svc.test_input, payload.text)
