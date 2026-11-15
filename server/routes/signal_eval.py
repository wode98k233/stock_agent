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
    _bench_code_for, _bench_returns, _bump_cache_epoch, _kline_cached, _kline_for,
    _load_signals, _pair_returns, _returns_cache, _signal_returns,
    _symbol_code, cached_result, result_cache_key,
    build_calibration as _calibration_sync,
    build_conditional as _conditional_sync,
    build_overview as _overview_sync,
    classify_returns_status,
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
        "SELECT * FROM signal WHERE symbol_key IS NULL AND is_market_wide=0 "
        "AND IFNULL(scope,'symbol')='symbol' ORDER BY signal_time DESC"
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
    # 补标后统计口径变化：清该信号收益缓存，并 bump epoch 让结果级缓存整体失效
    _returns_cache.pop(sig_id, None)
    _bump_cache_epoch()


def _mark_market_wide_sync(sig_id: int, is_market_wide: bool = True) -> None:
    """标记/取消大盘类：排除出（或放回）未识别列表与胜率统计。"""
    conn = get_conn()
    cur = conn.execute("UPDATE signal SET is_market_wide=? WHERE id=?", (1 if is_market_wide else 0, sig_id))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"信号不存在: {sig_id}")
    # 主样本变化 → 结果级缓存失效（返回的统计不再包含/重新包含该信号）
    _bump_cache_epoch()


def start_rescan() -> str:
    """异步启动扫描，返回 task_id（后台线程，不阻塞请求）。"""
    task_id = uuid.uuid4().hex[:12]
    _rescan_progress[task_id] = {"pct": 0, "status": "running", "added": 0}
    # 重扫会改主样本：开始与结束各失效一次（期间算出的结果也不能留到扫完后）
    _bump_cache_epoch()

    def _run():
        try:
            added = scan_messages()
            _rescan_progress[task_id] = {"pct": 1.0, "status": "completed", "added": added}
        except Exception as e:  # noqa: BLE001
            _rescan_progress[task_id] = {"pct": 1.0, "status": "failed", "error": str(e)}
        finally:
            _bump_cache_epoch()

    threading.Thread(target=_run, daemon=True).start()
    return task_id


@router.get("/overview")
async def overview(cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """概览：样本数 + buy/sell 胜率(CI) + 期望值 + 盈亏比。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _overview_sync, cost)
    return {"code": 0, "data": data, "msg": "ok"}


@router.get("/conditional")
async def conditional(cost: float = Query(default=0.0, ge=0, le=1,
                                          description="单边交易成本%")):
    """条件单桶：类型分布 + 触发率（不计入主胜率）。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _conditional_sync, cost)
    return {"code": 0, "data": data, "msg": "ok"}


@router.get("/matrix")
async def matrix(cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """胜率矩阵：方向(buy/sell/配对/置信度分层) × 周期(5/10/20/30/60/至今)。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _matrix_sync, cost)
    return {"code": 0, "data": data, "msg": "ok"}


def _matrix_sync(cost_pct: float = 0.0) -> dict:
    """matrix 入口：结果级缓存（键含 cost 与 epoch）。"""
    return cached_result(result_cache_key("matrix", cost_pct),
                         lambda: _compute_matrix(cost_pct))


def _compute_matrix(cost_pct: float = 0.0) -> dict:
    """matrix 同步实现（线程池执行，不阻塞事件循环）。"""
    sigs = _load_signals()
    cols = [*CYCLES, "now"]
    rows = []

    def _cells(arr, reverse=False):
        """方向 × 周期胜率格；sell 需先把收益取反（卖后跌 = 卖对），与 overview 同口径。"""
        cells = []
        for c in cols:
            vals = []
            for s in arr:
                r = _signal_returns(s, cost_pct)["ret"].get(c)
                if r is not None:
                    vals.append(-r if reverse else r)
            w = win_rate(vals)
            ci = wilson_ci(sum(1 for v in vals if v > 0), len(vals)) if len(vals) >= 5 else None
            cells.append({"winrate": w, "n": len(vals), "ci": ci})
        return cells

    for key, label, fn, reverse in [
        ("buy", "buy 买入", lambda s: s["decision"] == "buy", False),
        ("sell", "sell 卖出（反向）", lambda s: s["decision"] == "sell", True),
    ]:
        arr = [s for s in sigs if fn(s)]
        rows.append({"key": key, "label": label, "cells": _cells(arr, reverse=reverse),
                     "n": len(arr)})

    pairs = pair_signals(sigs)
    pair_rets = _pair_returns(pairs, cost_pct)
    pw = win_rate(pair_rets)
    rows.append({"key": "paired", "label": "配对平仓",
                 "cells": [{"winrate": pw, "n": len(pair_rets), "ci": None}] * len(cols),
                 "n": len(pair_rets)})

    # 置信度分层：高 / 中 / 低 / 未评级。未评级（confidence 为 NULL）原先被
    # `(confidence or 0)` 折成 0 并入「低置信 <0.60」，与 dimension_stats 单列的
    # 「未评级」行对不上（同一页两个数）。这里显式四档、条件互斥，四行 n 之和
    # 等于 buy 信号数。
    buy_sigs = [s for s in sigs if s["decision"] == "buy"]
    for label, match in [
        ("buy 高置信 (≥0.75)", lambda c: c is not None and c >= 0.75),
        ("buy 中置信 (0.60~0.74)", lambda c: c is not None and 0.60 <= c < 0.75),
        ("buy 低置信 (<0.60)", lambda c: c is not None and c < 0.60),
        ("buy 未评级", lambda c: c is None),
    ]:
        arr = [s for s in buy_sigs if match(s.get("confidence"))]
        if arr:
            rows.append({"key": "conf", "label": label, "cells": _cells(arr), "n": len(arr)})

    return {"cols": cols, "rows": rows}


@router.get("/signals")
async def signals(decision: str | None = None,
                  page: int = Query(default=1, ge=1),
                  size: int = Query(default=50, ge=1, le=200),
                  include_conditional: bool = False,
                  cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%")):
    """信号样本列表（筛选/分页），每行含各周期收益/峰值/回撤 + paired 标记。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        None, _signals_sync, decision, page, size, cost, include_conditional)
    return {"code": 0, "data": data, "msg": "ok"}


def _signals_sync(decision: str | None, page: int, size: int, cost_pct: float = 0.0,
                  include_conditional: bool = False) -> dict:
    """signals 入口：结果级缓存（键含筛选/分页/cost/epoch）。"""
    return cached_result(
        result_cache_key("signals", decision, page, size, cost_pct, include_conditional),
        lambda: _compute_signals(decision, page, size, cost_pct, include_conditional))


def _compute_signals(decision: str | None, page: int, size: int, cost_pct: float = 0.0,
                     include_conditional: bool = False) -> dict:
    """signals 同步实现（线程池执行）。"""
    sigs = _load_signals(include_conditional=include_conditional)
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
        now_ret = r["ret"].get("now")
        # 基准/alpha 只对 buy 有意义（页面只在 buy 行展示）：hold/sell 不再为
        # alpha 付基准K线/收益计算的钱（原先 144 条全算，只有 24 条 buy 需要）。
        # 前端 hold/sell 的 alpha 列显示「—」，属预期（见 docs/areas/observability-and-eval.md）。
        if s["decision"] == "buy":
            bench = _bench_returns(s)
            bench_code = _bench_code_for(s)
        else:
            bench = None
            bench_code = None
        pi = paired_info.get(s["id"])
        items.append({**s, "returns": r["ret"], "peak": r["peak"], "maxDD": r["maxDD"],
                      "entry_price": r.get("entry_price"),
                      "entry_date": r.get("entry_date"),
                      "entry_mode": r.get("entry_mode"),
                      "data_status": classify_returns_status(r),
                      "paired": s["id"] in paired_ids,
                      "paired_ret": pi[1] if pi else None,
                      "paired_sell_date": pi[0] if pi else None,
                      "bench": bench,
                      "bench_code": bench_code,
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
    """top 入口：结果级缓存（键含 cost 与 epoch）。"""
    return cached_result(result_cache_key("top", cost_pct),
                         lambda: _compute_top(cost_pct))


def _compute_top(cost_pct: float = 0.0) -> dict:
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
    """portfolio 入口：结果级缓存（键含 cost 与 epoch）。"""
    return cached_result(result_cache_key("portfolio", cost_pct),
                         lambda: _compute_portfolio(cost_pct))


def _compute_portfolio(cost_pct: float = 0.0) -> dict:
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


# ── 策略研发：按策略聚合的前瞻统计 ──────────────────────

def _by_strategy_sync(strategy_id: str, cost_pct: float, holding_days: int) -> dict:
    from server.signal_eval_service import build_by_strategy as _build
    return _build(strategy_id, cost_pct=cost_pct, holding_days=holding_days)


@router.get("/by-strategy/{strategy_id}")
async def by_strategy(strategy_id: str,
                      cost: float = Query(default=0.0, ge=0, le=1, description="单边交易成本%"),
                      holding_days: int = Query(default=5, ge=1, le=60)):
    """某策略的前瞻跟踪统计（回测口径 vs 前瞻口径的「前瞻」那一半）。

    返回裸 payload（不套 {code,data,msg} 信封）：消费方是策略研发页，直接读顶层键。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _by_strategy_sync, strategy_id, cost, holding_days)
