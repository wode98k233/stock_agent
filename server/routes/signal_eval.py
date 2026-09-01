"""AI 信号评测 API：概览/矩阵/样本/TOP5 + 异步重扫。

非阻塞：agent 主流程不依赖本模块；rescan 走后台线程（对齐回测异步模式），
接口立即返回 task_id，前端轮询进度。统计接口同步计算放线程池（run_in_executor），
K 线本地读取（不 HTTP 自调用）——避免阻塞事件循环。
"""
import asyncio
import threading
import uuid

from fastapi import APIRouter, HTTPException, Query

from signal_eval.db import get_conn
from signal_eval.extract import scan_messages
from signal_eval.stats import (
    CYCLES, fetch_kline, calc_returns, win_rate, wilson_ci,
    expectancy_payoff, pair_signals, paired_return,
)

router = APIRouter(prefix="/api/signal-eval", tags=["signal-eval"])

_rescan_progress: dict[str, dict] = {}
_returns_cache: dict[int, dict] = {}
_kline_cache: dict[str, list] = {}
# 沪深300 基准指数（alpha 对比用）
_BENCH_CODE = "000300"


def _load_signals() -> list[dict]:
    """读取已识别标的的信号（symbol_key 非空）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM signal WHERE symbol_key IS NOT NULL ORDER BY signal_time"
    ).fetchall()
    return [dict(r) for r in rows]


def _symbol_code(symbol_key: str) -> str:
    """'sh600519' → '600519'（K 线接口只认裸代码）。"""
    return symbol_key[2:]


def _signal_returns(sig: dict) -> dict:
    """带缓存的收益计算。"""
    sig_id = sig["id"]
    if sig_id in _returns_cache:
        return _returns_cache[sig_id]
    code = _symbol_code(sig["symbol_key"])
    if code not in _kline_cache:
        try:
            _kline_cache[code] = fetch_kline(code)
        except Exception:
            _kline_cache[code] = []
    rets = calc_returns(_kline_cache[code], sig["signal_time"])
    _returns_cache[sig_id] = rets
    return rets


def _bench_returns(sig: dict) -> float | None:
    """信号日 → 最新的同期沪深300 收益（alpha 基准，指数走 market=INDEX）。"""
    if _BENCH_CODE not in _kline_cache:
        try:
            _kline_cache[_BENCH_CODE] = fetch_kline(_BENCH_CODE, market="INDEX")
        except Exception:
            _kline_cache[_BENCH_CODE] = []
    r = calc_returns(_kline_cache[_BENCH_CODE], sig["signal_time"])
    return r["ret"].get("now")


def _pair_returns(pairs: list[tuple[dict, dict]]) -> list[float]:
    """通道① 配对已实现收益（卖出价 − 买入价，用真实 K 线）。"""
    out = []
    for b, s in pairs:
        kline = _kline_for(b["symbol_key"])
        r = paired_return(kline, b["signal_time"], s["signal_time"])
        if r is not None:
            out.append(r)
    return out


def _kline_for(symbol_key: str) -> list:
    """取标的 K 线（带缓存，复用 _signal_returns 的缓存）。"""
    code = _symbol_code(symbol_key)
    if code not in _kline_cache:
        try:
            _kline_cache[code] = fetch_kline(code)
        except Exception:
            _kline_cache[code] = []
    return _kline_cache[code]


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
async def overview():
    """概览：样本数 + buy/sell 胜率(CI) + 期望值 + 盈亏比。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _overview_sync)
    return {"code": 0, "data": data, "msg": "ok"}


def _overview_sync() -> dict:
    """overview 同步实现（线程池执行，不阻塞事件循环）。"""
    sigs = _load_signals()
    buys = [s for s in sigs if s["decision"] == "buy"]
    sells = [s for s in sigs if s["decision"] == "sell"]
    hold_cnt = sum(1 for s in sigs if s["decision"] == "hold")

    buy_now = [r for s in buys if (r := _signal_returns(s)["ret"].get("now")) is not None]
    wr = win_rate(buy_now)
    ci = wilson_ci(sum(1 for r in buy_now if r > 0), len(buy_now)) if buy_now else None
    exp, payoff = expectancy_payoff(buy_now)

    sell_now = [r for s in sells if (r := _signal_returns(s)["ret"].get("now")) is not None]
    sell_wr = win_rate([-r for r in sell_now])  # 反向：卖出后跌=卖对

    pairs = pair_signals(sigs)
    pair_ret = _pair_returns(pairs)
    pair_wr = win_rate(pair_ret)

    # alpha：buy 信号收益 − 同期沪深300（P1）
    alphas = []
    for s in buys:
        r = _signal_returns(s)["ret"].get("now")
        bench = _bench_returns(s)
        if r is not None and bench is not None:
            alphas.append(r - bench)
    avg_alpha = (sum(alphas) / len(alphas)) if alphas else None
    beat_cnt = sum(1 for a in alphas if a > 0)

    return {
        "buy_count": len(buys), "sell_count": len(sells), "hold_count": hold_cnt,
        "paired_count": len(pairs),
        "buy_winrate": wr, "buy_ci": ci, "expectancy": exp, "payoff": payoff,
        "sell_winrate_reverse": sell_wr, "paired_winrate": pair_wr,
        "avg_alpha": avg_alpha, "beat_count": beat_cnt, "beat_total": len(alphas),
    }


@router.get("/matrix")
async def matrix():
    """胜率矩阵：方向(buy/sell/配对/置信度分层) × 周期(5/10/20/30/60/至今)。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _matrix_sync)
    return {"code": 0, "data": data, "msg": "ok"}


def _matrix_sync() -> dict:
    """matrix 同步实现（线程池执行，不阻塞事件循环）。"""
    sigs = _load_signals()
    cols = [*CYCLES, "now"]
    rows = []

    def _cells(arr):
        cells = []
        for c in cols:
            vals = [r for s in arr if (r := _signal_returns(s)["ret"].get(c)) is not None]
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
    pair_rets = _pair_returns(pairs)
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
                  size: int = Query(default=50, ge=1, le=200)):
    """信号样本列表（筛选/分页），每行含各周期收益/峰值/回撤 + paired 标记。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        None, _signals_sync, decision, page, size)
    return {"code": 0, "data": data, "msg": "ok"}


def _signals_sync(decision: str | None, page: int, size: int) -> dict:
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
        ret = paired_return(kline, b["signal_time"], s["signal_time"])
        paired_info[b["id"]] = (s["signal_time"][:10], ret)
    items = []
    for s in sigs:
        r = _signal_returns(s)
        bench = _bench_returns(s)
        now_ret = r["ret"].get("now")
        pi = paired_info.get(s["id"])
        items.append({**s, "returns": r["ret"], "peak": r["peak"], "maxDD": r["maxDD"],
                      "entry_price": r.get("entry_price"),
                      "paired": s["id"] in paired_ids,
                      "paired_ret": pi[1] if pi else None,
                      "paired_sell_date": pi[0] if pi else None,
                      "bench": bench,
                      "alpha": (round(now_ret - bench, 2)
                                if now_ret is not None and bench is not None else None)})
    total = len(items)
    start = (page - 1) * size
    return {"total": total, "items": items[start:start + size]}


@router.get("/top")
async def top():
    """TOP5：buy 利润/亏损 + sell 卖飞/卖对（反向解读）。"""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _top_sync)
    return {"code": 0, "data": data, "msg": "ok"}


def _top_sync() -> dict:
    """top 同步实现（线程池执行）。"""
    sigs = [s for s in _load_signals() if s["decision"] in ("buy", "sell")]
    rows = []
    for s in sigs:
        r = _signal_returns(s)["ret"].get("now")
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
