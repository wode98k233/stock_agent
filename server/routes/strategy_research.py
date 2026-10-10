"""A3 策略研发闭环 API：任务创建/SSE 进度/轮次表/取消/落地三动作。

后台线程跑 StrategyResearchLoop（照 routes/backtest.py 异步模式，不阻塞事件循环），
SSE 轮次事件：strategy_iter_start / strategy_iter_done / strategy_run_done，
终态沿用 final / task_error 关流。

adopt 三动作：
- save：最优策略回写策略库（strategies 表）；
- monitor：对接 server/monitoring 规则创建（MonitorStore.add_rule，source=strategy_research）；
- track：合成 message_uuid='sr_'+run_uuid 写 signal_eval.signal 表（幂等，confidence=NULL）。

结果卡固定免责声明；敏感词（稳赚/必胜/躺赢等）在生成侧已过滤，此处再扫描兜底（AC-8）。
"""
import asyncio
import json
import logging
import re
import sqlite3
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backtest.strategy_research import (
    DISCLAIMER,
    SENSITIVE_WORDS,
    StrategyGenerator,
    StrategyResearchLoop,
    IsoOsRunner,
    BacktestRunStore,
    scan_sensitive_words,
)
from utils.cache.backtest_db import (
    create_strategy_run,
    get_strategy_run,
    get_strategy_iter_by_id,
    get_strategy_iters,
    list_strategy_runs,
    update_strategy_run,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/strategy-research', tags=['strategy-research'])

DEFAULT_PERIOD_YEARS = 3       # 默认回测区间：近 3 年
SSE_POLL_SECONDS = 0.5         # SSE 轮询事件队列间隔
SSE_MAX_TICKS = 7200           # SSE 最长挂 1 小时（0.5s * 7200）
DB_FALLBACK_TICKS = 8          # 每 ~4s 查一次 DB 终态兜底


# ============================================================
# SSE 事件队列（线程安全：后台线程推，事件循环拉）
# ============================================================

_sr_events: Dict[str, deque] = {}
_sr_events_lock = threading.Lock()


def _push_event(run_uuid: str, event: str, data: dict) -> None:
    """后台线程 → SSE 队列（线程安全）。"""
    with _sr_events_lock:
        q = _sr_events.setdefault(run_uuid, deque(maxlen=500))
        q.append({'event': event, 'data': data})


def _drain_events(run_uuid: str) -> List[dict]:
    with _sr_events_lock:
        q = _sr_events.get(run_uuid)
        if not q:
            return []
        items = list(q)
        q.clear()
        return items


# ============================================================
# 请求模型
# ============================================================


class StartRunRequest(BaseModel):
    idea: str
    universe: str = '600519'
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    source: str = 'panel'          # panel / skill
    dialog_uuid: Optional[str] = None


class AdoptRequest(BaseModel):
    action: str                    # save / monitor / track


class AdoptResponse(BaseModel):
    code: int = 0
    data: Dict[str, Any] = {}
    msg: str = 'ok'


# ============================================================
# 任务创建（面板与技能入口共用）
# ============================================================


def start_strategy_run(idea: str, universe: str, period_start: Optional[str] = None,
                       period_end: Optional[str] = None, source: str = 'panel',
                       dialog_uuid: Optional[str] = None) -> Dict[str, Any]:
    """创建任务并启动后台状态机（面板/技能共用入口）。

    Returns:
        {run_uuid, task_id, period, events_url}
    """
    idea = (idea or '').strip()
    universe = (universe or '').strip()
    if len(idea) < 4:
        raise ValueError('策略研究想法不能为空或过短')
    hits = scan_sensitive_words(idea)
    if hits:
        raise ValueError(f'想法包含不合规词汇（{",".join(hits)}），请调整表述后再试')
    if not re.fullmatch(r'\d{6}', universe):
        raise ValueError(f'标的需为 6 位 A 股代码: {universe}')

    now = datetime.now()
    end = period_end or now.strftime('%Y-%m-%d')
    start = period_start or (now - timedelta(days=365 * DEFAULT_PERIOD_YEARS)).strftime('%Y-%m-%d')
    period = f'{start}~{end}'

    run_uuid = f'SR-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'
    create_strategy_run(run_uuid, idea, universe, period,
                        source=source, dialog_uuid=dialog_uuid)

    thread = threading.Thread(
        target=_run_loop_async,
        args=(run_uuid, ),
        daemon=True,
        name=f'strategy-research-{run_uuid}',
    )
    thread.start()

    return {
        'run_uuid': run_uuid,
        'task_id': run_uuid,
        'period': period,
        'events_url': f'/api/strategy-research/runs/{run_uuid}/events',
    }


def _run_loop_async(run_uuid: str) -> None:
    """后台线程：跑状态机，进度回调推 SSE。"""
    store = BacktestRunStore()

    def progress_cb(event: str, payload: dict) -> None:
        data = dict(payload)
        data['run_uuid'] = run_uuid
        _push_event(run_uuid, event, data)

    def cancel_check() -> bool:
        run = store.get_run(run_uuid)
        return bool(run and run.get('status') == 'cancelled')

    try:
        loop = StrategyResearchLoop(
            run_uuid=run_uuid,
            db=store,
            generator=StrategyGenerator(),
            runner=IsoOsRunner(),
            progress_cb=progress_cb,
            cancel_check=cancel_check,
        )
        result = loop.run()
        # 关流终态（event_stream 约定：final 关流；异常路径用 task_error）
        _push_event(run_uuid, 'final', {
            'run_uuid': run_uuid, 'type': 'final',
            'status': result.status, 'stop_reason': result.stop_reason,
            'total_iters': result.total_iters,
        })
    except Exception as e:
        logger.error(f'[策略研发] 任务 {run_uuid} 异常: {e}', exc_info=True)
        try:
            update_strategy_run(
                run_uuid, status='failed', stop_reason='failed',
                completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        except Exception:
            pass
        _push_event(run_uuid, 'task_error', {
            'run_uuid': run_uuid, 'type': 'error',
            'message': f'策略研究任务失败：{e}',
        })


# ============================================================
# API 端点
# ============================================================


@router.post('/run')
async def create_run(req: StartRunRequest):
    """创建策略研究任务（立即返回 run_uuid，SSE 走 /runs/{uuid}/events）"""
    try:
        info = start_strategy_run(
            req.idea, req.universe, period_start=req.period_start,
            period_end=req.period_end, source=req.source,
            dialog_uuid=req.dialog_uuid)
    except ValueError as e:
        return {'code': 1, 'data': {}, 'msg': str(e)}
    return {'code': 0, 'data': info, 'msg': 'ok'}


@router.get('/runs')
async def list_runs(limit: int = Query(50, ge=1, le=200)):
    """任务列表（策略库视图，含最优轮次 OOS 摘要）"""
    runs = list_strategy_runs(limit=limit)
    for r in runs:
        for f in ('best_oos_metrics', 'best_is_metrics'):
            if r.get(f):
                try:
                    r[f] = json.loads(r[f])
                except (json.JSONDecodeError, TypeError):
                    pass
    return {'code': 0, 'data': {'runs': runs, 'total': len(runs)}, 'msg': 'ok'}


@router.get('/runs/{run_uuid}')
async def get_run_detail(run_uuid: str):
    """任务详情 + 最优策略卡（含过拟合检验结论与免责声明）"""
    if not get_strategy_run(run_uuid):
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    return {'code': 0, 'data': _run_detail_payload(run_uuid), 'msg': 'ok'}


def _run_detail_payload(run_uuid: str) -> Dict[str, Any]:
    """任务详情载荷（HTTP 端点与技能工具共用）。任务不存在返回错误 dict（不抛 HTTP 异常）。"""
    run = get_strategy_run(run_uuid)
    if not run:
        return {'error': f'策略研究任务不存在: {run_uuid}'}
    iters = get_strategy_iters(run_uuid)
    best = None
    if run.get('best_iter_id'):
        best = get_strategy_iter_by_id(run['best_iter_id'])
    card = _build_best_card(run, best)
    return {'run': run, 'iters': iters, 'best_card': card, 'disclaimer': DISCLAIMER}


@router.get('/runs/{run_uuid}/iters')
async def list_iters(run_uuid: str):
    """轮次表（前端刷新/重连补齐用）"""
    run = get_strategy_run(run_uuid)
    if not run:
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    iters = get_strategy_iters(run_uuid)
    return {'code': 0, 'data': {'run_uuid': run_uuid, 'iters': iters,
                                'status': run.get('status')}, 'msg': 'ok'}


@router.get('/runs/{run_uuid}/events')
async def stream_events(run_uuid: str):
    """SSE：strategy_iter_start / strategy_iter_done / strategy_run_done + final 关流"""
    run = get_strategy_run(run_uuid)
    if not run:
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')

    async def event_stream():
        # 已是终态：直接补一条 final（客户端重连场景）
        if run.get('status') in ('done', 'failed', 'cancelled'):
            yield f"event: final\ndata: {json.dumps({'run_uuid': run_uuid, 'type': 'final', 'status': run['status'], 'stop_reason': run.get('stop_reason'), 'db_fallback': True}, ensure_ascii=False)}\n\n"
            return
        tick = 0
        while tick < SSE_MAX_TICKS:
            for item in _drain_events(run_uuid):
                body = json.dumps(item['data'], ensure_ascii=False, default=str)
                yield f"event: {item['event']}\ndata: {body}\n\n"
                if item['event'] in ('final', 'task_error'):
                    return
            tick += 1
            # 双保险：每 ~4s 查 DB 终态（后台线程异常退出时兜底）
            if tick % DB_FALLBACK_TICKS == 0:
                db_run = get_strategy_run(run_uuid)
                if db_run and db_run.get('status') in ('done', 'failed', 'cancelled'):
                    yield f"event: final\ndata: {json.dumps({'run_uuid': run_uuid, 'type': 'final', 'status': db_run['status'], 'stop_reason': db_run.get('stop_reason'), 'db_fallback': True}, ensure_ascii=False)}\n\n"
                    return
            await asyncio.sleep(SSE_POLL_SECONDS)
        yield f"event: task_error\ndata: {json.dumps({'run_uuid': run_uuid, 'type': 'error', 'message': 'SSE 连接超时'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@router.post('/runs/{run_uuid}/cancel')
async def cancel_run(run_uuid: str):
    """取消任务（置 status=cancelled，循环在下一哨点退出）"""
    run = get_strategy_run(run_uuid)
    if not run:
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    if run.get('status') != 'running':
        return {'code': 1, 'data': {'status': run['status']},
                'msg': f'任务已结束（{run["status"]}），无需取消'}
    update_strategy_run(run_uuid, status='cancelled')
    return {'code': 0, 'data': {'run_uuid': run_uuid, 'status': 'cancelled'}, 'msg': 'ok'}


@router.post('/runs/{run_uuid}/adopt')
async def adopt_run(run_uuid: str, req: AdoptRequest):
    """落地三动作：save（存策略库）/ monitor（加入盯盘）/ track（信号入 signal_eval）"""
    run = get_strategy_run(run_uuid)
    if not run:
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    if req.action not in ('save', 'monitor', 'track'):
        return {'code': 1, 'data': {}, 'msg': f'未知动作: {req.action}（可选 save/monitor/track）'}
    if run.get('status') != 'done' or not run.get('best_iter_id'):
        return {'code': 1, 'data': {}, 'msg': '任务未完成或没有最优策略，无法落地'}

    best = get_strategy_iter_by_id(run['best_iter_id'])
    if not best or not best.get('strategy_json'):
        return {'code': 1, 'data': {}, 'msg': '最优轮次缺少策略 JSON'}

    if req.action == 'save':
        return _adopt_save(run, best)
    if req.action == 'monitor':
        return _adopt_monitor(run, best)
    return _adopt_track(run, best)


# ============================================================
# adopt 动作实现
# ============================================================


def _adopt_save(run: dict, best: dict) -> dict:
    """save：最优策略回写策略库（复用 routes/backtest.py save_strategy 的存储函数）。"""
    try:
        from utils.cache.backtest_db import upsert_strategy
        from backtest.strategies import load_json_strategies
        from backtest.json_compiler import compile_strategy

        strategy_obj = json.loads(best['strategy_json'])
        compile_strategy(best['strategy_json'])  # 落库前再过一次编译门
        meta = strategy_obj.get('meta') or {}
        strategy_id = f"sr_{run['run_uuid'].split('-')[-1].lower()}"
        raw_params = strategy_obj.get('params', {})
        params_schema = []
        for key, info in raw_params.items():
            if isinstance(info, dict):
                params_schema.append({
                    'key': key,
                    'label': info.get('label', key),
                    'type': info.get('type', 'float'),
                    'default': info.get('value', info.get('default', 0)),
                    'min': info.get('min', 0),
                    'max': info.get('max', 999999),
                    'unit': info.get('unit', ''),
                })
        tags = json.dumps(['策略研发', run['universe']], ensure_ascii=False)
        upsert_strategy(
            strategy_id=strategy_id,
            name=meta.get('name') or f"策略研究_{run['run_uuid']}",
            description=(best.get('hypothesis') or meta.get('description') or '')[:200],
            category='custom',
            params_schema=json.dumps(params_schema, ensure_ascii=False),
            is_builtin=False,
            code=json.dumps(strategy_obj, ensure_ascii=False),
            tags=tags,
        )
        load_json_strategies(force=True)
        return {'code': 0, 'data': {'strategy_id': strategy_id}, 'msg': '已存入策略库'}
    except Exception as e:
        # 策略库写入失败：仅落 best 标记并提示（不 500）
        logger.warning(f'[策略研发] adopt=save 落库失败: {e}')
        return {'code': 1, 'data': {'best_iter_id': run.get('best_iter_id')},
                'msg': f'策略库写入失败，已保留最优标记: {e}'}


def _adopt_monitor(run: dict, best: dict) -> dict:
    """monitor：对接盯盘规则创建（MonitorStore.add_rule）。

    盯盘规则引擎为阈值穿越语义，无法直接表达指标条件；
    以最优策略的止损位（strategy risk.stop_loss）创建 price_below 规则兜底：
    跌破止损位即告警。无止损配置或取不到最新价 → code 4210 提示。
    """
    try:
        from server.monitoring.store import MonitorStore
    except ImportError:
        return {'code': 4210, 'data': {}, 'msg': '盯盘功能未上线'}

    universe = run['universe']
    try:
        strategy_obj = json.loads(best['strategy_json'])
    except json.JSONDecodeError:
        strategy_obj = {}
    stop_loss = ((strategy_obj.get('risk') or {}).get('stop_loss') or {})
    stop_pct = stop_loss.get('value') if isinstance(stop_loss, dict) else None

    close = _latest_close(universe)
    if close is None:
        return {'code': 4210, 'data': {}, 'msg': '盯盘规则创建失败：本地无该标的最新收盘价'}
    if not stop_pct or stop_pct <= 0:
        stop_pct = 0.08
    threshold = round(close * (1 - stop_pct), 2)

    try:
        store = MonitorStore()
        rule_id = store.add_rule(
            stock_code=universe,
            stock_name=None,
            rule_type='price_below',
            threshold=threshold,
            once_only=False,
            cooldown_s=1800,
            remark=f"策略研究止损盯盘 {run['run_uuid']}",
            source='strategy_research',
        )
        return {'code': 0, 'data': {'rule_id': rule_id, 'rule_type': 'price_below',
                                    'threshold': threshold},
                'msg': f'已创建止损盯盘规则（跌破 {threshold} 告警）'}
    except Exception as e:
        logger.warning(f'[策略研发] adopt=monitor 失败: {e}')
        return {'code': 4210, 'data': {}, 'msg': f'盯盘规则创建失败: {e}'}


def _adopt_track(run: dict, best: dict) -> dict:
    """track：写 signal_eval.signal 表（合成 message_uuid='sr_'+run_uuid，幂等）。

    confidence=NULL（待明确 #4：策略信号非 LLM 报告结论，不污染 A2 校准样本）。
    """
    from signal_eval.db import get_conn

    message_uuid = f"sr_{run['run_uuid']}"
    symbol_key = _to_symbol_key(run['universe'])
    idea_digest = (run.get('idea') or '')[:80]
    core_verdict = f"策略研究:{idea_digest}"
    signal_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_conn()
    try:
        conn.execute(
            '''INSERT INTO signal
               (dialog_uuid, message_uuid, symbol_key, symbol_name, decision,
                confidence, sentiment, core_verdict, signal_time, is_market_wide)
               VALUES (?, ?, ?, NULL, 'buy', NULL, NULL, ?, ?, 0)''',
            (run.get('dialog_uuid') or '', message_uuid, symbol_key,
             core_verdict, signal_time))
        conn.commit()
        return {'code': 0, 'data': {'message_uuid': message_uuid, 'symbol_key': symbol_key},
                'msg': '已标记信号入 signal_eval 跟踪'}
    except sqlite3.IntegrityError:
        # UNIQUE(message_uuid) → 重复 adopt 幂等返回
        return {'code': 0, 'data': {'message_uuid': message_uuid, 'already': True},
                'msg': '该信号已标记过（幂等返回）'}


# ============================================================
# 辅助
# ============================================================


def _latest_close(code: str) -> Optional[float]:
    """本地库取最新收盘价（不外呼）。"""
    try:
        from utils.cache.market_data_db import get_stock_daily
        df = get_stock_daily(code, limit=1)
        if df is not None and not df.empty:
            return float(df.iloc[-1]['close'])
    except Exception:
        pass
    return None


def _to_symbol_key(code: str) -> str:
    """6 位裸代码 → signal_eval 的 symbol_key（sh600519 / sz159928）。"""
    if code.startswith(('5', '6', '9')):
        return f'sh{code}'
    if code.startswith(('0', '1', '2', '3')):
        return f'sz{code}'
    return f'sh{code}'


def _build_best_card(run: dict, best: Optional[dict]) -> dict:
    """最优策略卡载荷：OOS 指标大字 + IS 小字 + 过拟合结论 + 免责声明。"""
    if not best:
        return {'exists': False}
    oos = best.get('oos_metrics') or {}
    is_m = best.get('is_metrics') or {}
    strategy_name = ''
    try:
        strategy_name = (json.loads(best.get('strategy_json') or '{}')
                         .get('meta', {}).get('name', ''))
    except json.JSONDecodeError:
        pass
    overfit_note = ('IS/OOS 夏普比超红线，存在过拟合迹象'
                    if best.get('overfit_flag') else '未触发过拟合红线（IS/OOS 夏普比 ≤ 1.3）')
    sensitive_hits = scan_sensitive_words(best.get('hypothesis') or '')
    return {
        'exists': True,
        'iter_id': best.get('id'),
        'round': best.get('round'),
        'strategy_name': strategy_name,
        'hypothesis': best.get('hypothesis'),
        'oos_metrics': oos,
        'is_metrics': is_m,
        'overfit_flag': best.get('overfit_flag'),
        'overfit_note': overfit_note,
        'low_sample': (oos.get('trades') or 0) < 5,
        'sensitive_hits': sensitive_hits,
        'disclaimer': DISCLAIMER,
    }
