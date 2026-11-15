"""因子评估异步运行管理（工作台 v3 · F7 SSE）。

为什么不用阻塞 GET：90 秒里零进度、不可取消、关页面后端照跑（用户 2026-10-17 明确要求
改 SSE）。本模块照抄 `strategy_research` 的范式：POST 起 run → 后台线程 → SSE 拉事件。

事件流（`GET /api/v1/factor/runs/{uuid}/events`）：
    phase  {phase, index, total, label}   —— 阶段与进度（evaluate 阶段按因子推进）
    final  {report_uuid, status}          —— done / cancelled，附落库后的 report_uuid
    error  {message}

取消：``cancel()`` 置位 → progress 回调抛 ``FactorRunCancelled`` → 评估在**下一个因子
边界**停下（单因子约 2 秒，不做更细粒度打断）。
"""
from __future__ import annotations

import threading
import traceback
from collections import deque
from typing import Any, Dict, Optional

from backtest.factor_eval import EvalConfig, FactorRunCancelled

_RUNS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_MAX_RUNS = 8          # 同时保留的运行态上限（超出淘汰最旧的已完成项）
_MAX_EVENTS = 400


def _emit(uuid: str, event: str, data: Dict[str, Any]) -> None:
    """事件追加。必须整段持锁：cancel()（HTTP 线程）与 worker 的 progress
    会并发 emit，seq 的读改写不锁会丢号，破坏 SSE 端按 seq 去重的假设。"""
    with _LOCK:
        st = _RUNS.get(uuid)
        if not st:
            return
        st["seq"] += 1
        st["events"].append({"seq": st["seq"], "event": event, "data": data})


def _prune() -> None:
    """超出上限时淘汰最旧的已结束项；运行中的绝不淘汰。"""
    if len(_RUNS) <= _MAX_RUNS:
        return
    finished = [(k, v) for k, v in _RUNS.items() if v["status"] in ("done", "error", "cancelled")]
    finished.sort(key=lambda kv: kv[1].get("created", 0))
    for k, _ in finished:
        if len(_RUNS) <= _MAX_RUNS:
            break
        _RUNS.pop(k, None)


def start_run(params: Dict[str, Any]) -> str:
    """起一次评估，返回 run_uuid。参数校验在这里做（失败抛 ValueError）。"""
    from uuid import uuid4

    cfg = EvalConfig(
        ret_period=int(params.get("ret_period", 5)),
        quantiles=int(params.get("quantiles", 5)),
        lag=int(params.get("lag", 1)),
        min_names=int(params.get("min_names", 30)),
        neutralize=str(params.get("neutralize", "none")),
    )
    ids = params.get("factor_ids") or None
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",") if x.strip()] or None

    run_uuid = "FRUN-" + datetime_now() + "-" + uuid4().hex[:6]
    with _LOCK:
        _RUNS[run_uuid] = {
            "run_uuid": run_uuid, "status": "queued", "seq": 0,
            "events": deque(maxlen=_MAX_EVENTS),
            "phase": "queued", "index": 0, "total": len(ids or []) or 36, "label": "排队中",
            "report_uuid": "", "error": "", "cancel": False, "created": time_now(),
            "cfg": cfg, "ids": ids, "params": dict(params),
        }
        _prune()
    threading.Thread(target=_worker, args=(run_uuid,), daemon=True,
                     name=f"factor-run-{run_uuid}").start()
    return run_uuid


def get_state(uuid: str) -> Optional[Dict[str, Any]]:
    return _RUNS.get(uuid)


def cancel(uuid: str) -> bool:
    st = _RUNS.get(uuid)
    if not st:
        return False
    st["cancel"] = True
    if st["status"] in ("queued",):
        st["status"] = "cancelled"
        _emit(uuid, "final", {"status": "cancelled", "report_uuid": ""})
    return True


def datetime_now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H%M%S")


def time_now() -> float:
    import time
    return time.time()


def _worker(uuid: str) -> None:
    st = _RUNS.get(uuid)
    if not st:
        return
    # 竞态窗口：start_run 刚起线程、HTTP 线程的 cancel() 已置位——
    # 不在这里判一次，worker 会把 cancelled 覆盖成 running，客户端收到两次 final。
    if st["cancel"]:
        st["status"] = "cancelled"
        st["phase"] = "cancelled"
        _emit(uuid, "final", {"status": "cancelled", "report_uuid": ""})
        return
    params = st["params"]
    cfg: EvalConfig = st["cfg"]
    ids = st["ids"]

    def progress(phase: str, index: int, total: int, label: str) -> None:
        if st["cancel"]:
            raise FactorRunCancelled("用户取消")
        st.update(phase=phase, index=index, total=total, label=label, status="running")
        _emit(uuid, "phase", {"phase": phase, "index": index, "total": total,
                              "label": label, "percent": round(index / max(total, 1) * 100, 1)})

    try:
        from backtest.factor_data import load_panel_inputs
        from backtest.factor_eval import build_panel, evaluate_factors
        from backtest.factor_report import build_report_html
        from backtest import factor_store

        st["status"] = "running"
        progress("data", 0, 3, "读取数据")

        frames, meta = load_panel_inputs(
            index_code=params.get("index_code", "000300"),
            start_date=params.get("start", "2024-10-01"),
            end_date=params.get("end") or None,
            warmup_bars=int(params.get("warmup", 90)))
        if not frames:
            raise RuntimeError(f"标的池无数据：{params.get('index_code', '000300')}，"
                               f"请先跑 scripts/ops/backfill_index_universe.py")
        progress("data", 1, 3, f"读取数据（{len(frames)}/{meta.get('universe_size')} 只）")

        panel = build_panel(frames, ids, min_history=int(params.get("warmup", 90)) // 2,
                            meta_map=meta.get("meta_map"))
        if panel.empty:
            raise RuntimeError("面板为空：标的数或历史行数不足")
        progress("panel", 2, 3, f"构建面板 {len(panel):,} 行")

        payload = evaluate_factors(panel, ids, cfg, meta=meta, progress=progress)
        progress("persist", 3, 3, "保存报告")

        report_params = {**cfg.to_params(), "index_code": params.get("index_code", "000300"),
                         "start": params.get("start", ""), "end": params.get("end") or "",
                         "top": int(params.get("top", 10))}
        html = build_report_html(payload, meta, detail_top=int(params.get("top", 10)))
        report_uuid = factor_store.save_report(payload=payload, params=report_params,
                                               meta=meta, html=html)

        st["report_uuid"] = report_uuid
        st["status"] = "done"
        st["phase"] = "done"
        _emit(uuid, "final", {"status": "done", "report_uuid": report_uuid,
                              "n_factors": len(payload.get("ranking") or [])})
    except FactorRunCancelled:
        st["status"] = "cancelled"
        st["phase"] = "cancelled"
        _emit(uuid, "final", {"status": "cancelled", "report_uuid": ""})
    except Exception as e:  # noqa: BLE001
        st["status"] = "error"
        st["error"] = f"{type(e).__name__}: {e}"
        _emit(uuid, "error", {"message": st["error"]})
        traceback.print_exc()
