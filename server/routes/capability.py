"""引擎能力清单 + 能力扩展提议 API（AI 出方案 → 人审）。

⛔ 边界（用户已确认）：本接口只产出**方案文本**，不执行 AI 代码、不自动注册进引擎；
批准只是把定义写进 approved_registry.json（effective=false）。
"""
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from backtest import capability_proposals as cp
from backtest.json_compiler import export_capabilities
from utils.cache.backtest_db import load_intent_checks

router = APIRouter(prefix='/api/capability', tags=['capability'])


class ProposeRequest(BaseModel):
    kind: str = 'indicator'
    name: str = ''
    detail: str = ''
    idea: str = ''


class ProposeFromRunRequest(BaseModel):
    run_uuid: str = ''


class ReviewRequest(BaseModel):
    path: str = ''
    decision: str = ''
    note: str = ''


@router.get('/catalog')
async def catalog():
    """引擎当前能表达什么（数据字段 / 指标 / 算子 / 时序 / 成交口径 / 特殊形态）"""
    return await run_in_threadpool(export_capabilities)


@router.get('/proposals')
async def list_proposals(status: str = ''):
    return {'items': await run_in_threadpool(cp.list_proposals, status or None)}


@router.post('/proposals')
async def propose(req: ProposeRequest):
    res = await run_in_threadpool(cp.propose_extension,
                                  {'kind': req.kind, 'name': req.name, 'detail': req.detail},
                                  idea=req.idea)
    if not res.get('ok'):
        raise HTTPException(status_code=400, detail=res.get('reason') or 'propose_failed')
    path = await run_in_threadpool(cp.save_proposal, res['proposal'])
    return {'success': True, 'path': path, 'proposal': res['proposal']}


@router.post('/proposals/from-run')
async def propose_from_run(req: ProposeFromRunRequest):
    """按某次研发的能力缺口批量出方案（一次最多 3 条，避免 LLM 调用过多）"""
    checks = await run_in_threadpool(load_intent_checks, req.run_uuid)
    gaps, seen = [], set()
    for _round, payload in (checks or {}).items():
        for gap in ((payload or {}).get('gaps') or []):
            key = (gap.get('kind'), gap.get('name'))
            if gap.get('name') and key not in seen:
                seen.add(key)
                gaps.append(gap)
    items = []
    for gap in gaps[:3]:
        res = await run_in_threadpool(cp.propose_extension, gap)
        if res.get('ok'):
            items.append({'path': await run_in_threadpool(cp.save_proposal, res['proposal']),
                          'proposal': res['proposal']})
    return {'success': True, 'gaps': gaps, 'generated': len(items), 'items': items}


@router.post('/proposals/review')
async def review(req: ReviewRequest):
    res = await run_in_threadpool(cp.review_proposal, req.path, req.decision, req.note)
    if not res.get('ok'):
        raise HTTPException(status_code=400, detail=res.get('reason') or 'review_failed')
    return res
