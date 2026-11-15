"""因子评估 API（为面板铺路）。

端点
----
- ``GET  /api/v1/factor/list``              因子清单（元数据，轻量）
- ``POST /api/v1/factor/run``               异步评估（SSE 配套，F7）
- ``GET  /api/v1/factor/runs/{id}/events``  SSE 进度事件
- ``POST /api/v1/factor/runs/{id}/cancel``  取消
- ``GET  /api/v1/factor/reports``           历史报告列表（持久化，F1）
- ``GET  /api/v1/factor/reports/{uuid}``    报告 payload
- ``GET  /api/v1/factor/reports/{uuid}/page`` 完整 HTML（inline）
- ``DELETE /api/v1/factor/reports/{uuid}``  删除
- ``GET  /api/v1/factor/report``            同步评估（兼容旧面板调用，也落库）

设计约束（调研文档 §5.4 锁定）
------------------------------
- 响应统一 ``{code, data, message}``
- 所有时序统一 ``{dates, series:[{name, data}]}``，**缺失一律 null、长度对齐**
- Q1 = 因子值最小 / 最高分位 = 最大；多空 = 最高分位 − Q1
- 计算走同步函数 → FastAPI 自动放进线程池，不阻塞事件循环
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backtest.factor_lib import FACTOR_IDS, factor_meta
from backtest.factor_eval import EvalConfig
from backtest.factor_data import load_panel_inputs

router = APIRouter(prefix="/api/v1/factor", tags=["factor"])

logger = logging.getLogger(__name__)

# 进程内缓存：评估一次约数十秒，面板会反复拉取
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 1800.0


def _cache_key(**kw) -> tuple:
    return tuple(sorted(kw.items()))


def _get_cached(key: tuple) -> Optional[dict]:
    with _CACHE_LOCK:
        item = _CACHE.get(key)
    if not item:
        return None
    ts, payload = item
    if time.time() - ts > _CACHE_TTL:
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
        return None
    return payload


def _set_cached(key: tuple, payload: dict) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), payload)
        # 简单上限，避免无限增长（单人本地工具，够用）
        if len(_CACHE) > 8:
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:-8]
            for k, _ in oldest:
                _CACHE.pop(k, None)


def resolve_factor_ids(factor_id: Optional[str],
                       factor_ids: Optional[str]) -> Optional[list]:
    """解析并校验因子选择。None = 全部；未知因子抛 400。"""
    ids = None
    if factor_id:
        ids = [factor_id]
    elif factor_ids:
        ids = [x.strip() for x in factor_ids.split(",") if x.strip()]
    if ids:
        unknown = [x for x in ids if x not in FACTOR_IDS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知因子: {unknown}")
    return ids or None


@router.get("/list")
def list_factors():
    """因子清单（名称/类别/说明/所需行数）。"""
    items = factor_meta()
    return {
        "code": 0,
        "message": "ok",
        "data": {"factors": items, "count": len(items)},
    }


@router.get("/report")
def factor_report(
    index_code: str = Query("000300", description="指数代码（标的池来源）"),
    start: str = Query("2024-10-01", description="评估起始日 YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="评估结束日，默认今天"),
    ret_period: int = Query(5, ge=1, le=60, description="前向收益周期（交易日）"),
    quantiles: int = Query(5, ge=3, le=10, description="分层数"),
    lag: int = Query(1, ge=0, le=5, description="因子滞后（1=次日建仓，防前视）"),
    min_names: int = Query(30, ge=5, le=500, description="单日截面最少标的数"),
    neutralize: str = Query("none", pattern="^(none|industry|size|both)$",
                            description="中性化模式：none/industry/size/both"),
    factor_id: Optional[str] = Query(None, description="只评某个因子；空=全部"),
    factor_ids: Optional[str] = Query(None, description="逗号分隔的因子子集（前端多选用）"),
    limit: int = Query(0, ge=0, le=500, description="只取前 N 只标的（0=全部，调试用）"),
):
    """批量因子评估结果。

    首次调用会实时计算（数十秒），之后命中进程内缓存（TTL 30 分钟）。
    """
    from backtest.factor_eval import build_panel, evaluate_factors

    cfg = EvalConfig(ret_period=ret_period, quantiles=quantiles, lag=lag,
                     min_names=min_names, neutralize=neutralize)

    ids = resolve_factor_ids(factor_id, factor_ids)

    key = _cache_key(index_code=index_code, start=start, end=end or "",
                     ret_period=ret_period, quantiles=quantiles, lag=lag,
                     min_names=min_names, ids=",".join(ids or []),
                     neutralize=neutralize, limit=limit)
    cached = _get_cached(key)
    if cached is not None:
        return {"code": 0, "message": "ok (cached)", "data": cached}

    frames, meta = load_panel_inputs(index_code=index_code, start_date=start,
                                     end_date=end)
    if not frames:
        raise HTTPException(
            status_code=404,
            detail=f"标的池无数据：{index_code}。请先回补日线"
                   f"（scripts/ops/backfill_index_universe.py）")

    if limit:
        keep = sorted(frames.keys())[:limit]
        frames = {k: frames[k] for k in keep}
        meta["loaded"] = len(frames)

    panel = build_panel(frames, ids, min_history=30, meta_map=meta.get("meta_map"))
    if panel.empty:
        raise HTTPException(status_code=404, detail="面板为空：标的数或历史行数不足")

    payload = evaluate_factors(panel, ids, cfg, meta=meta)
    _set_cached(key, payload)

    # 同步路径也落库（F1）：不持久化的结果在用户切走后无法回看
    try:
        from backtest.factor_report import build_report_html
        from backtest import factor_store
        html = build_report_html(payload, meta, detail_top=10)
        report_uuid = factor_store.save_report(
            payload=payload, params=cfg.to_params(), meta=meta, html=html)
        payload["report_uuid"] = report_uuid
    except Exception as e:  # noqa: BLE001  持久化失败不阻断返回，但必须留痕
        logger.warning("同步评估结果落库失败（用户会以为已保存）: %s", e)
    return {"code": 0, "message": "ok", "data": payload}


# ══════════════ 异步评估 + SSE（F7）══════════════

class RunParams(BaseModel):
    index_code: str = "000300"
    start: str = "2024-10-01"
    end: Optional[str] = None
    ret_period: int = 5
    quantiles: int = 5
    lag: int = 1
    min_names: int = 30
    neutralize: str = "none"
    factor_ids: Optional[str] = None
    top: int = 10
    warmup: int = 90


@router.post("/run")
def factor_run(params: RunParams):
    """起一次异步评估，返回 run_uuid；进度走 GET /runs/{uuid}/events。"""
    from server import factor_service
    try:
        run_uuid = factor_service.start_run(params.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"code": 0, "message": "ok", "data": {"run_uuid": run_uuid}}


@router.get("/runs/{run_uuid}/events")
async def factor_run_events(run_uuid: str):
    """SSE：phase（进度）→ final（done/cancelled，带 report_uuid）或 error。"""
    from fastapi.responses import StreamingResponse
    from server import factor_service

    async def gen():
        last = 0
        deadline = time.time() + 900     # 15 分钟上限，防悬挂连接占满浏览器 6 条
        while True:
            st = factor_service.get_state(run_uuid)
            if not st:
                yield f"event: error\ndata: {json.dumps({'message': 'run 不存在'})}\n\n"
                return
            for ev in list(st["events"]):
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
                yield (f"event: {ev['event']}\n"
                       f"data: {json.dumps(ev['data'], ensure_ascii=False)}\n\n")
            if st["status"] in ("done", "error", "cancelled"):
                return
            if time.time() > deadline:
                yield f"event: error\ndata: {json.dumps({'message': 'SSE 超时'})}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/runs/{run_uuid}/cancel")
def factor_run_cancel(run_uuid: str):
    from server import factor_service
    ok = factor_service.cancel(run_uuid)
    return {"code": 0 if ok else 1, "message": "ok" if ok else "run 不存在",
            "data": {"cancelled": ok}}


# ══════════════ 历史报告（F1 持久化）══════════════

@router.get("/reports")
def factor_reports(limit: int = Query(50, ge=1, le=200)):
    """历史报告列表（新→旧，只含摘要）。"""
    from backtest import factor_store
    return {"code": 0, "message": "ok",
            "data": {"reports": factor_store.list_reports(limit)}}


@router.get("/reports/{report_uuid}")
def factor_report_detail(report_uuid: str):
    """报告完整 payload；文件被清理时明确告知，不返回空壳。"""
    from backtest import factor_store
    row = factor_store.get_report(report_uuid)
    if not row:
        raise HTTPException(status_code=404, detail=f"报告不存在: {report_uuid}")
    payload = factor_store.load_payload(report_uuid)
    if payload is None:
        return {"code": 1, "message": "payload 文件已清理",
                "data": {"report_uuid": report_uuid, "kpi": row["kpi"],
                         "params": row["params"], "payload": None}}
    return {"code": 0, "message": "ok", "data": {"payload": payload, "report": row}}


@router.get("/reports/{report_uuid}/page")
def factor_report_page(report_uuid: str):
    """完整 HTML 报告（inline 返回，浏览器直接渲染/另存）。"""
    from backtest import factor_store
    html = factor_store.load_html(report_uuid)
    if html:
        return HTMLResponse(content=html)
    # HTML 文件被清了但 payload 还在 → 现场重生成，不让用户看到 404
    payload = factor_store.load_payload(report_uuid)
    if payload is None:
        raise HTTPException(status_code=404,
                            detail=f"报告数据不存在或已清理: {report_uuid}")
    from backtest.factor_report import build_report_html
    row = factor_store.get_report(report_uuid)
    meta = {"index_code": row.get("index_code"), "start": row.get("start_date"),
            "end": row.get("end_date"), "data_range": f"{row.get('start_date')} ~ {row.get('end_date')}",
            "loaded": row.get("n_factors"), "generated_at": row.get("created_at")}
    return HTMLResponse(content=build_report_html(payload, meta, detail_top=10))


@router.delete("/reports/{report_uuid}")
def factor_report_delete(report_uuid: str):
    from backtest import factor_store
    ok = factor_store.delete_report(report_uuid)
    if not ok:
        raise HTTPException(status_code=404, detail=f"报告不存在: {report_uuid}")
    return {"code": 0, "message": "ok", "data": {"deleted": report_uuid}}


_HEALTH_CACHE: dict = {}
_HEALTH_TTL = 60.0


@router.get("/health")
def factor_health():
    """数据面体检：标的池覆盖情况（面板拉不起来时先看这里）。

    ⚠️ 只做 SQL 计数，**不加载行情体**（2026-10-18 性能修复）：
    旧实现用 load_frames 把 300 只票两年日线读成 pandas，冷启动实测 **34.5 秒**，
    而因子面板每次打开都会拉这个接口 —— 用户侧表现就是「打开因子页很慢」。
    体检只需要「每只有多少根 K 线」，一条 GROUP BY 就够，实测降到毫秒级。
    另加 60 秒进程内缓存：数据面不会秒级变化，重复打开面板无需再查一次。
    """
    now = time.time()
    cached = _HEALTH_CACHE.get("000300")
    if cached and now - cached[0] < _HEALTH_TTL:
        return cached[1]

    from backtest.factor_data import load_universe
    from utils.cache.market_data_db import get_market_data_db

    universe = load_universe("000300")
    codes = [r["code"] for r in universe]
    if not codes:
        return {"code": 0, "message": "ok",
                "data": {"universe": 0, "note": "index_constituent 为空，请先跑回补脚本"}}
    ph = ",".join(["?"] * len(codes))
    with get_market_data_db() as conn:
        rows = conn.execute(
            "SELECT code, COUNT(*) AS n FROM stock_daily "
            "WHERE code IN (" + ph + ") AND trade_date BETWEEN ? AND ? GROUP BY code",
            [*codes, "2025-01-01", "2026-12-31"]).fetchall()
    counts = {r["code"]: r["n"] for r in rows}
    ok = sum(1 for n in counts.values() if n >= 240)
    payload = {
        "code": 0,
        "message": "ok",
        "data": {
            "index_code": "000300",
            "as_of_date": universe[0]["as_of_date"] if universe else None,
            "universe": len(codes),
            "with_data": len(counts),
            "with_240_rows": ok,
            "coverage_pct": round(ok / len(codes), 4) if codes else 0.0,
        },
    }
    _HEALTH_CACHE["000300"] = (now, payload)
    return payload
