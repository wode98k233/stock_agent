"""AI 信号评测 API：概览/矩阵/样本/TOP5 + 异步重扫。

非阻塞：agent 主流程不依赖本模块；rescan 走后台线程（对齐回测异步模式），
接口立即返回 task_id，前端轮询进度。统计接口同步计算放线程池（run_in_executor），
K 线本地读取（不 HTTP 自调用）——避免阻塞事件循环。
"""
import asyncio
import os
import re
import sqlite3
import threading
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from utils.app_paths import get_db_path
from signal_eval.db import get_conn
from signal_eval.extract import scan_messages
from signal_eval.stats import (
    CYCLES, win_rate, wilson_ci, pair_signals, paired_return, portfolio_equity,
)
from server.signal_eval_service import (
    _bench_code_for, _bench_returns, _kline_cached, _kline_for,
    _load_signals, _pair_returns, _returns_cache, _signal_returns, _symbol_code,
    build_calibration as _calibration_sync,
    build_overview as _overview_sync,
)

router = APIRouter(prefix="/api/signal-eval", tags=["signal-eval"])

MSG_DB_PATH = get_db_path()

_rescan_progress: dict[str, dict] = {}
# 沪深300 基准指数（alpha 对比用）
_BENCH_CODE = "000300"


_SYMBOL_KEY_RE = re.compile(r"^(sh|sz)\d{6}$")


def _validate_symbol_key(symbol_key: str | None) -> bool:
    """symbol_key 格式校验：sh/sz 前缀 + 6 位数字。"""
    return bool(symbol_key) and bool(_SYMBOL_KEY_RE.match(symbol_key))


def _dialog_context_msg(dialog_uuid: str, before_msg_id: int | None = None) -> str:
    """标的辅助上下文：优先取先于该报告的最后一条 user 消息，无则回退首条 user。"""
    try:
        conn = sqlite3.connect(MSG_DB_PATH)
        try:
            if before_msg_id:
                row = conn.execute(
                    "SELECT content FROM web_messages WHERE dialog_uuid=? AND role='user' "
                    "AND id<? ORDER BY id DESC LIMIT 1",
                    (dialog_uuid, before_msg_id),
                ).fetchone()
                if row and row[0]:
                    return row[0]
            row = conn.execute(
                "SELECT content FROM web_messages WHERE dialog_uuid=? AND role='user' "
                "ORDER BY id LIMIT 1",
                (dialog_uuid,),
            ).fetchone()
            return (row[0] if row else "") or ""
        finally:
            conn.close()
    except Exception:
        return ""


def _report_msg_id(message_uuid: str | None) -> int | None:
    """signal.message_uuid 是 web_messages.id 的字符串（strategy_research 合成的 'sr_' 前缀除外）。"""
    try:
        return int(str(message_uuid))
    except (TypeError, ValueError):
        return None


def _unrecognized_sync(page: int = 1, size: int = 50) -> dict:
    """未识别信号列表（symbol_key 为空且未标记大盘类），含对话上下文片段。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM signal WHERE symbol_key IS NULL AND is_market_wide=0 ORDER BY signal_time DESC"
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["context"] = _dialog_context_msg(d["dialog_uuid"], _report_msg_id(d.get("message_uuid")))[:60]
        items.append(d)
    total = len(items)
    start = (page - 1) * size
    return {"total": total, "items": items[start:start + size]}


def _mark_symbol_sync(sig_id: int, symbol_key: str, symbol_name: str | None) -> None:
    """补标：写入 symbol_key/name（is_market_wide 归零），该信号即参与统计。"""
    if not _validate_symbol_key(symbol_key):
        raise ValueError(f"非法 symbol_key: {symbol_key}")
    conn = get_conn()
    cur = conn.execute(
        "UPDATE signal SET symbol_key=?, symbol_name=?, is_market_wide=0 WHERE id=?",
        (symbol_key, symbol_name, sig_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"信号不存在: {sig_id}")
    # 补标后统计口径变化，清该模块级缓存
    _returns_cache.pop(sig_id, None)


def _mark_market_wide_sync(sig_id: int, is_market_wide: bool = True) -> None:
    """标记/取消大盘类：排除出（或放回）未识别列表与胜率统计。"""
    conn = get_conn()
    cur = conn.execute("UPDATE signal SET is_market_wide=? WHERE id=?", (1 if is_market_wide else 0, sig_id))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"信号不存在: {sig_id}")


def start_rescan() -> str:
    """异步启动扫描，返回 task_id（后台线程，不阻塞请求）。"""
    task_id = uuid.uuid4().hex[:12]
    _rescan_progress[task_id] = {"pct": 0, "status": "running", "added": 0}

    def _run():
        try:
            added = scan_messages()
            _rescan_progress[task_id] = {"pct": 1.0, "status": "completed", "added": added}
        except Exception as e:  # noqa: BLE001
            _rescan_progress[task_id] = {"pct": 1.0, "status": "failed", "error": str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return task_id


@router.get("/overview")
async def overview(cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """概览：样本数 + buy/sell 胜率(CI) + 期望值 + 盈亏比。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _overview_sync, cost)
    return {"code": 0, "data": data, "msg": "ok"}


@router.get("/matrix")
async def matrix(cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """胜率矩阵：方向(buy/sell/配对/置信度分层) × 周期(5/10/20/30/60/至今)。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _matrix_sync, cost)
    return {"code": 0, "data": data, "msg": "ok"}


def _matrix_sync(cost_pct: float = 0.0) -> dict:
    """matrix 同步实现（线程池执行，不阻塞事件循环）。"""
    sigs = _load_signals()
    cols = [*CYCLES, "now"]
    rows = []

    def _cells(arr):
        cells = []
        for c in cols:
            vals = [r for s in arr if (r := _signal_returns(s, cost_pct)["ret"].get(c)) is not None]
            w = win_rate(vals)
            ci = wilson_ci(sum(1 for v in vals if v > 0), len(vals)) if len(vals) >= 5 else None
            cells.append({"winrate": w, "n": len(vals), "ci": ci})
        return cells

    for key, label, fn in [
        ("buy", "buy 买入", lambda s: s["decision"] == "buy"),
        ("sell", "sell 卖出（反向）", lambda s: s["decision"] == "sell"),
    ]:
        arr = [s for s in sigs if fn(s)]
        rows.append({"key": key, "label": label, "cells": _cells(arr), "n": len(arr)})

    pairs = pair_signals(sigs)
    pair_rets = _pair_returns(pairs, cost_pct)
    pw = win_rate(pair_rets)
    rows.append({"key": "paired", "label": "配对平仓",
                 "cells": [{"winrate": pw, "n": len(pair_rets), "ci": None}] * len(cols),
                 "n": len(pair_rets)})

    for label, lo, hi in [
        ("buy 高置信 (≥0.75)", 0.75, 1.01),
        ("buy 中置信 (0.60~0.74)", 0.60, 0.75),
        ("buy 低置信 (<0.60)", 0.0, 0.60),
    ]:
        arr = [s for s in sigs if s["decision"] == "buy"
               and (s["confidence"] or 0) >= lo and (s["confidence"] or 0) < hi]
        if arr:
            rows.append({"key": "conf", "label": label, "cells": _cells(arr), "n": len(arr)})

    return {"cols": cols, "rows": rows}


@router.get("/signals")
async def signals(decision: str | None = None,
                  page: int = Query(default=1, ge=1),
                  size: int = Query(default=50, ge=1, le=200),
                  cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """信号样本列表（筛选/分页），每行含各周期收益/峰值/回撤 + paired 标记。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        None, _signals_sync, decision, page, size, cost)
    return {"code": 0, "data": data, "msg": "ok"}


def _signals_sync(decision: str | None, page: int, size: int, cost_pct: float = 0.0) -> dict:
    """signals 同步实现（线程池执行）。"""
    sigs = _load_signals()
    if decision:
        sigs = [s for s in sigs if s["decision"] == decision]
    # paired 标记 + 配对收益 + 卖出日期
    paired_ids = set()
    paired_info: dict[int, tuple[str | None, float | None]] = {}  # buy_id -> (sell_date, paired_ret)
    for b, s in pair_signals(sigs):
        paired_ids.add(b["id"])
        kline = _kline_for(b["symbol_key"])
        ret = paired_return(kline, b["signal_time"], s["signal_time"], cost_pct=cost_pct)
        paired_info[b["id"]] = (s["signal_time"][:10], ret)
    items = []
    for s in sigs:
        r = _signal_returns(s, cost_pct)
        bench = _bench_returns(s)
        now_ret = r["ret"].get("now")
        pi = paired_info.get(s["id"])
        items.append({**s, "returns": r["ret"], "peak": r["peak"], "maxDD": r["maxDD"],
                      "entry_price": r.get("entry_price"),
                      "paired": s["id"] in paired_ids,
                      "paired_ret": pi[1] if pi else None,
                      "paired_sell_date": pi[0] if pi else None,
                      "bench": bench,
                      "bench_code": _bench_code_for(s),
                      "alpha": (round(now_ret - bench, 2)
                                if now_ret is not None and bench is not None else None)})
    total = len(items)
    start = (page - 1) * size
    return {"total": total, "items": items[start:start + size]}


@router.get("/top")
async def top(cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """TOP5：buy 利润/亏损 + sell 卖飞/卖对（反向解读）。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _top_sync, cost)
    return {"code": 0, "data": data, "msg": "ok"}


def _top_sync(cost_pct: float = 0.0) -> dict:
    """top 同步实现（线程池执行）。"""
    sigs = [s for s in _load_signals() if s["decision"] in ("buy", "sell")]
    rows = []
    for s in sigs:
        r = _signal_returns(s, cost_pct)["ret"].get("now")
        if r is not None:
            rows.append({**s, "now_ret": r})
    buys = sorted([x for x in rows if x["decision"] == "buy"], key=lambda x: x["now_ret"], reverse=True)
    sells = sorted([x for x in rows if x["decision"] == "sell"], key=lambda x: x["now_ret"], reverse=True)
    return {
        "buy_profit_top": buys[:5],
        "buy_loss_top": list(reversed(buys))[:5],
        "sell_fly_top": sells[:5],                      # 卖出后涨最多 = 卖飞
        "sell_right_top": list(reversed(sells))[:5],    # 卖出后跌最多 = 卖对
    }


@router.post("/rescan")
async def rescan():
    """异步触发扫描，立即返回 task_id。"""
    task_id = start_rescan()
    return {"code": 0, "data": {"task_id": task_id}, "msg": "ok"}


@router.get("/rescan/{task_id}")
async def rescan_status(task_id: str):
    if task_id not in _rescan_progress:
        raise HTTPException(status_code=404, detail="task not found")
    return {"code": 0, "data": _rescan_progress[task_id], "msg": "ok"}


# ── 组合净值（P5） ──────────────────────────────────────

def _bench_equity(dates: list[str], bench_kline: list[dict]) -> list[float | None]:
    """基准净值：沪深300 收盘对组合首日归一化（缺日前向填充，无数据段为 None）。"""
    bmap = {k["date"][:10]: k["close"] for k in bench_kline if k.get("close")}
    out: list[float | None] = []
    last = None
    base = None
    for d in dates:
        if d in bmap:
            last = bmap[d]
        if last is None:
            out.append(None)
            continue
        if base is None:
            base = last
        out.append(round(last / base, 4))
    return out


def _portfolio_sync(cost_pct: float = 0.0) -> dict:
    """组合净值同步实现（线程池执行）。"""
    sigs = _load_signals()
    syms = sorted({s["symbol_key"] for s in sigs if s["decision"] in ("buy", "sell")})
    klines = {}
    for sym in syms:
        k = _kline_cached(_symbol_code(sym))
        if k:
            klines[sym] = k
    data = portfolio_equity(sigs, klines, cost_pct=cost_pct)
    if data["dates"]:
        data["bench"] = _bench_equity(data["dates"], _kline_cached(_BENCH_CODE, market="INDEX"))
    else:
        data["bench"] = []
    data["cost"] = cost_pct
    return data


@router.get("/portfolio")
async def portfolio(cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """组合级净值曲线（等权独立子仓位）+ 沪深300 归一化基准 + 最大回撤。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _portfolio_sync, cost)
    return {"code": 0, "data": data, "msg": "ok"}


# ── 未识别标注（P3） ──────────────────────────────────────

class MarkSymbolRequest(BaseModel):
    symbol_key: str
    symbol_name: str | None = None


class MarkMarketWideRequest(BaseModel):
    is_market_wide: bool = True


@router.get("/unrecognized")
async def unrecognized(page: int = Query(default=1, ge=1),
                        size: int = Query(default=50, ge=1, le=200)):
    """未识别信号列表（含对话上下文片段，辅助人工补标）。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _unrecognized_sync, page, size)
    return {"code": 0, "data": data, "msg": "ok"}


@router.patch("/signals/{sig_id}")
async def mark_signal(sig_id: int, payload: MarkSymbolRequest):
    """补标标的：写入 symbol_key/name，该信号立即参与统计（统计查询时算，无需重算）。"""
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _mark_symbol_sync, sig_id, payload.symbol_key, payload.symbol_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 0, "data": {"id": sig_id, "symbol_key": payload.symbol_key}, "msg": "ok"}


@router.post("/signals/{sig_id}/market-wide")
async def mark_market_wide(sig_id: int, payload: MarkMarketWideRequest):
    """标记/取消大盘类：大盘类决策不计入胜率统计。"""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _mark_market_wide_sync, sig_id, payload.is_market_wide)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 0, "data": {"id": sig_id, "is_market_wide": payload.is_market_wide}, "msg": "ok"}


# ── A2 置信度校准 ──────────────────────────────────────

@router.get("/calibration")
async def calibration(cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """置信度校准：分桶统计（预测置信度 vs 实现胜率）+ ECE + 排除计数。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _calibration_sync, cost)
    return {"code": 0, "data": data, "msg": "ok"}
