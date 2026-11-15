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
import statistics
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backtest.strategy_research import (
    DISCLAIMER,
    SENSITIVE_WORDS,
    IterRecord,
    StrategyGenerator,
    StrategyResearchLoop,
    IsoOsRunner,
    BacktestRunStore,
    scan_sensitive_words,
)
from backtest.engine import ENGINE_VERSION
from backtest.strategy_base import normalize_timing
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


@contextmanager
def _research_scope(run_uuid: str, bind: Dict[str, Any]):
    """研发线程的 trace 作用域；trace 不可用时退化为空上下文（研发照常跑）。

    ⛔ 埋点失败绝不能让研发任务失败 —— trace 只是可观测性，不是业务依赖。
    """
    from contextlib import nullcontext
    try:
        from agents.shared.strategy_trace import strategy_research_scope
        cm = strategy_research_scope(run_uuid, bind=bind)
    except Exception as e:
        logger.warning('[策略研发] trace 作用域不可用，本任务无 trace 记录: %s', e)
        cm = nullcontext(None)
    try:
        with cm as scope:
            yield scope
    except Exception:
        # scope 退出异常不能带走研发结果（result 已在 with 内部拿到）
        raise

DEFAULT_PERIOD_YEARS = 3       # 默认回测区间：近 3 年
SSE_POLL_SECONDS = 0.5         # SSE 轮询事件队列间隔
SSE_MAX_TICKS = 7200           # SSE 最长挂 1 小时（0.5s * 7200）
DB_FALLBACK_TICKS = 8          # 每 ~4s 查一次 DB 终态兜底


# ============================================================
# SSE 事件队列（线程安全：后台线程推，事件循环拉）
# ============================================================

_sr_events: Dict[str, deque] = {}
_sr_events_lock = threading.Lock()
# 池子优先异步任务：run_id → {status, result, error, ...}
_pool_runs: Dict[str, Dict[str, Any]] = {}
_pool_runs_lock = threading.Lock()


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
    # 买卖**各自**的成交口径。历史事故：本端点曾只收不发（也没落库），
    # 面板/接口发起的任务永远是 next_open，用户勾的「尾盘买入」被静默丢掉。
    # 现在入口归一化后既传给后台线程，也写进 strategy_run 行。
    entry_timing: str = 'next_open'   # next_open 次日开盘 / same_close 当日收盘（尾盘）/ next_close 次日收盘
    exit_timing: str = ''             # 留空 = 跟随 entry_timing


class PoolRunRequest(BaseModel):
    """池子优先入口：给想法或策略 JSON + 池子，直接返回横截面 + 组合结果。"""
    idea: str = ''
    strategy_json: str = ''
    pool: str = 'POOL:default'       # POOL:default / POOL:600519,510300
    start: str = '2024-01-01'
    end: str = ''
    entry_timing: str = 'same_close'
    exit_timing: str = 'next_open'
    max_positions: int = 10
    workers: int = 4
    mode: str = 'both'               # validate / portfolio / both
    # 同一策略/区间/池子，卖出口径各跑一遍（次日开盘 vs 次日收盘）再对比
    compare_exit_timings: bool = False
    # 持有期矩阵：如 [3,4,5]，与卖出口径组合逐组回测
    hold_periods: List[int] = []


class AdoptRequest(BaseModel):
    action: str                    # save / monitor / track

class AdoptResponse(BaseModel):
    code: int = 0
    data: Dict[str, Any] = {}
    msg: str = 'ok'


# ============================================================
# 标的语义校验
# ============================================================

def _is_index_code(code: str) -> bool:
    """``code`` 是否是指数（查 ``index_info``，不靠前缀猜）。

    ⚠️ 不能按「000 开头 = 指数」硬编码：``000001`` 既是上证指数也是平安银行。
    以元数据表为准；读不到表时 fail-open（放行）——数据问题由研发前的
    ``preflight_data`` 兜底，不能因为元数据库异常就把所有任务拦死。

    背景（真机事故 2026-10-18）：确认卡的「沪深 300 成分股」被映射成裸值 ``000300``，
    后端把它当**一只股票**回测，从 stock_daily 拿到 24 根月线，8 轮全部
    insufficient_bars。指数只可用于基准对比，不能当标的交易。
    """
    code = (code or '').strip()
    if not code:
        return False
    try:
        from utils.cache.market_data_db import get_market_data_db
        with get_market_data_db() as conn:
            row = conn.execute('SELECT 1 FROM index_info WHERE code = ? LIMIT 1',
                               (code,)).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001 - 元数据读不到不阻断研发
        logger.warning('[策略研发] 指数判定失败（按非指数放行）: %s', code, exc_info=True)
        return False


# ============================================================
# 成交口径（买入/卖出各自独立）
# ============================================================


def _normalize_exit(exit_timing: str) -> str:
    """归一化卖出口径：**空串是合法值**（= 跟随买入口径），其余非法值回落 next_open。

    ⚠️ 不能直接用 ``normalize_timing(v)`` 一把过：它的默认值是 next_open，
    会把「跟随买入」这个最常见的默认悄悄改成「卖也是次日开盘」——买尾盘、
    卖次日开盘这种显式组合才该写成 next_open。
    """
    raw = str(exit_timing or '').strip()
    return normalize_timing(raw, 'next_open') if raw else ''


def _timings_of_run(run: Optional[Dict[str, Any]]) -> tuple:
    """任务行 → (entry, exit)；exit 已解析成**实际口径**（空串跟随 entry）。

    结果卡/详情页要展示「尾盘买入 → 次日开盘卖出」，所以这里给解析后的值，
    而不是把空串原样丢给前端（前端不该知道「空串=跟随」这条内部约定）。
    """
    run = run or {}
    entry = normalize_timing(run.get('entry_timing'), 'next_open')
    exit_ = _normalize_exit(run.get('exit_timing')) or entry
    return entry, exit_


# ============================================================
# 任务创建（面板与技能入口共用）
# ============================================================


def start_strategy_run(idea: str, universe: str, period_start: Optional[str] = None,
                       period_end: Optional[str] = None, source: str = 'panel',
                       dialog_uuid: Optional[str] = None,
                       entry_timing: str = 'next_open',
                       exit_timing: str = '',
                       trace_bind: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """创建任务并启动后台状态机（面板/技能/会话轮共用入口）。

    Args:
        entry_timing / exit_timing: 买卖各自的成交口径。非法值回落 next_open；
            exit 空串 = 跟随 entry。两者都会**落库**（create_strategy_run），
            续跑和结果卡才有据可查。
        trace_bind: C 批。会话轮把自己刚建的 trace run_id 放进来，研发线程**沿用同一条
            run**，于是「用户点了开始」和「8 轮迭代」在执行过程里是连续的一段。
            面板直接发起时留空 → 研发线程自建 run（照样有 trace）。

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
    if _is_index_code(universe):
        raise ValueError(
            f'{universe} 是指数，不能作为单标的交易。请改为成分股（如 600519）或 ETF；'
            f'指数只用于基准对比。')

    # 口径先归一化再落库/起线程：落库值与线程实际用的值必须是同一个，
    # 否则续跑会按另一套口径重跑，结论不可复现。
    entry_timing = normalize_timing(entry_timing, 'next_open')
    exit_timing = _normalize_exit(exit_timing)

    now = datetime.now()
    end = period_end or now.strftime('%Y-%m-%d')
    start = period_start or (now - timedelta(days=365 * DEFAULT_PERIOD_YEARS)).strftime('%Y-%m-%d')
    period = f'{start}~{end}'

    run_uuid = f'SR-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'
    create_strategy_run(run_uuid, idea, universe, period,
                        source=source, dialog_uuid=dialog_uuid,
                        engine_version=ENGINE_VERSION,
                        entry_timing=entry_timing, exit_timing=exit_timing)

    thread = threading.Thread(
        target=_run_loop_async,
        args=(run_uuid, entry_timing, exit_timing, trace_bind),
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


def _run_loop_async(run_uuid: str, entry_timing: str = 'next_open',
                    exit_timing: str = '',
                    trace_bind: Optional[Dict[str, Any]] = None,
                    resume_iters: Optional[List[IterRecord]] = None) -> None:
    """后台线程：跑状态机，进度回调推 SSE。

    C 批：线程内自建 trace run（研发期8 轮 LLM 调用 + IS/OOS 双段回测逐步埋点），
    并把 run_id 通过 ``trace_bind`` 显式回传给起跑方落库。

    续跑（``resume_iters`` 非空）：沿用同一个 run_uuid，循环从第 N+1 轮开始，
    上下文由 ``StrategyGenerator.restore_context`` 从 ``strategy_iter`` 重建。
    """
    store = BacktestRunStore()
    _trace_bind = trace_bind if trace_bind is not None else {}

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
            generator=StrategyGenerator(entry_timing=entry_timing,
                                            exit_timing=exit_timing),
            runner=IsoOsRunner(run_uuid=run_uuid, entry_timing=entry_timing,
                               exit_timing=exit_timing),
            progress_cb=progress_cb,
            cancel_check=cancel_check,
        )
        with _research_scope(run_uuid, _trace_bind):
            result = loop.run(resume_iters=resume_iters)
        # 显式归属：把 trace run_id 写回任务行（取代「猜最近一条 run」）
        trace_run_id = str(_trace_bind.get('trace_run_id') or '')
        if trace_run_id:
            try:
                update_strategy_run(run_uuid, trace_run_id=trace_run_id)
            except Exception:
                logger.warning('[策略研发] trace_run_id 回写失败', exc_info=True)
        # 关流终态（event_stream 约定：final 关流；异常路径用 task_error）
        _push_event(run_uuid, 'final', {
            'run_uuid': run_uuid, 'type': 'final',
            'status': result.status, 'stop_reason': result.stop_reason,
            'total_iters': result.total_iters,
            'trace_run_id': trace_run_id,
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


@router.post('/pool-run')
async def create_pool_run(req: PoolRunRequest):
    """池子优先入口：一次性返回横截面 + 组合结果。

    - 给了 ``strategy_json`` 就不再调 LLM（可复现、快）；
    - 只给想法时用真实生成器按池子第一只标的生成一份（约 1~3 分钟）。
    这是同步端点，适合脚本/服务调用；Web 面板如果需要进度条，后续再包 SSE。
    """
    from backtest.pool_first import resolve_pool, run_pool_first

    try:
        codes = resolve_pool(req.pool)
    except ValueError as e:
        return {'code': 1, 'data': {}, 'msg': str(e)}
    if not codes:
        return {'code': 1, 'data': {}, 'msg': '池子为空'}

    strategy_json = (req.strategy_json or '').strip()
    if not strategy_json:
        idea = (req.idea or '').strip()
        if len(idea) < 4:
            return {'code': 1, 'data': {}, 'msg': '需要 strategy_json 或至少 4 个字的 idea'}
        hits = scan_sensitive_words(idea)
        if hits:
            return {'code': 1, 'data': {}, 'msg': f'想法包含不合规词汇（{",".join(hits)}）'}
        from backtest.pool_first import generate_pool_strategy
        from backtest.batch_engine import _auto_detect_asset_type
        n_etf = sum(1 for c in codes if _auto_detect_asset_type(c) == 'etf')
        desc = f'{len(codes) - n_etf} 股票 + {n_etf} ETF'
        res = await asyncio.to_thread(
            generate_pool_strategy, idea, desc,
            entry_timing=normalize_timing(req.entry_timing, 'next_open'),
            exit_timing=_normalize_exit(req.exit_timing))
        if not res.get('ok'):
            return {'code': 1, 'data': {}, 'msg': f'策略生成失败：{res.get("error")}'}
        strategy_json = res['strategy_json']
        intent_check = res.get('intent_check') or {}
    else:
        intent_check = {}

    try:
        result = await asyncio.to_thread(
            run_pool_first, strategy_json,
            pool_spec=req.pool, start=req.start,
            end=req.end or datetime.now().strftime('%Y-%m-%d'),
            entry_timing=normalize_timing(req.entry_timing, 'next_open'),
            exit_timing=_normalize_exit(req.exit_timing),
            workers=req.workers, max_positions=req.max_positions,
            mode=req.mode)
    except Exception as e:  # noqa: BLE001
        logger.exception('[策略研发] 池子优先运行失败')
        return {'code': 1, 'data': {}, 'msg': f'池子运行失败：{e}'}
    if intent_check:
        result['intent_check'] = intent_check
    return {'code': 0, 'data': result, 'msg': 'ok'}


def _pool_run_study_report(result: Dict[str, Any], *, pool_spec: str,
                           start: str, end: str,
                           elapsed_sec: Optional[float] = None) -> Dict[str, Any]:
    """把「对话池子验证」的结果翻译成「池子研究」页可读的报告结构。

    两条链路的结果形状不同（`run_pool_first` 给 cross_section/portfolio，
    `_run_pool_study` 给 items/by_industry）。这里只做**字段适配**，数值一律原样搬运，
    缺失就留空（不补 0）。

    为什么必须落成同一形状：此前对话池子验证只存在 `_pool_runs` 进程内字典里，
    **历史研究和池子研究两个页面都看不到，重启即丢**（用户 2026-10-24 复现）。
    """
    from backtest.pool_study import StockResult

    cs = result.get('cross_section') or {}
    pool = result.get('pool') or {}
    raw_items = cs.get('results') or []
    items: List[Dict[str, Any]] = []
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        sr = StockResult(
            code=str(row.get('code') or ''), name=str(row.get('name') or ''),
            health=str(row.get('health') or ''), trades=row.get('trades'),
            win_rate=row.get('win_rate'), total_return=row.get('total_return'),
            sharpe=row.get('sharpe'), bars=row.get('bars'),
            max_drawdown=row.get('max_drawdown'),
            profit_factor=row.get('profit_factor'),
            avg_hold_days=row.get('avg_hold_days'),
            neutral_rate=row.get('neutral_rate'),
            industry=str(row.get('industry') or ''), error=str(row.get('error') or ''),
        )
        items.append({'code': sr.code, 'name': sr.name, 'industry': sr.industry,
                      'bucket': sr.bucket, 'trades': sr.trades,
                      'win_rate': sr.win_rate, 'total_return': sr.total_return,
                      'sharpe': sr.sharpe, 'max_drawdown': sr.max_drawdown,
                      'profit_factor': sr.profit_factor,
                      'avg_hold_days': sr.avg_hold_days})
    items.sort(key=lambda x: -(x['total_return'] if x['total_return'] is not None else -9))
    try:
        days = (datetime.strptime(str(end), '%Y-%m-%d')
                - datetime.strptime(str(start), '%Y-%m-%d')).days
    except (TypeError, ValueError):
        days = None
    report: Dict[str, Any] = {
        'index_code': str(pool_spec or ''),
        'period': {'start': str(start), 'end': str(end), 'days': days},
        'universe': {'total': int(pool.get('n') or len(raw_items)),
                     'with_industry': 0, 'industries': 0,
                     'assets': pool.get('assets') or {}},
        'verdict': cs.get('verdict') or {},
        'overall': cs.get('overall') or {},
        'by_industry': [],          # 对话池子验证没有行业映射（不编造）
        'items': items[:12],
        'caveats': cs.get('caveats') or [],
        'portfolio': result.get('portfolio') or {},
        'timing': result.get('timing') or {},
        'intent_check': result.get('intent_check') or {},
        'compare': result.get('compare') or {},
        'sweep': result.get('sweep') or {},
    }
    if elapsed_sec is not None:
        report['elapsed_sec'] = round(float(elapsed_sec), 1)
    return report


def _persist_pool_run(run_id: str, *, req: 'PoolRunRequest',
                      result: Optional[Dict[str, Any]] = None,
                      error: str = '', elapsed_sec: Optional[float] = None) -> None:
    """池子验证跑完/失败后落盘到池子研究库（失败也要落，页面才能看到失败记录）。

    落盘失败绝不影响任务本身（`save_study` 内部已吞异常）。
    """
    from backtest import pool_study_store

    try:
        from backtest.pool_scope import scope_short_label
        scope_label = scope_short_label(req.pool) or str(req.pool or '')
    except Exception:  # noqa: BLE001 - 标签失败不影响落盘
        scope_label = str(req.pool or '')
    if not result:
        pool_study_store.save_study(
            run_id, status='failed', run_uuid='', index_code=scope_label,
            error=error or '未知错误', elapsed_sec=elapsed_sec)
        return
    start = str(req.start or '')
    end = str(req.end or datetime.now().strftime('%Y-%m-%d'))
    report = _pool_run_study_report(result, pool_spec=str(req.pool or ''),
                                    start=start, end=end, elapsed_sec=elapsed_sec)
    pool_study_store.save_study(
        run_id, status='done', run_uuid='', index_code=scope_label,
        report=report, elapsed_sec=elapsed_sec)


def _run_pool_async(run_id: str, req: PoolRunRequest) -> None:
    """池子后台线程：生成（可选）→ 逐只 + 组合，进度走 SSE 队列。"""
    from backtest.pool_first import generate_pool_strategy, resolve_pool, run_pool_first

    def _set_progress(payload: dict) -> None:
        with _pool_runs_lock:
            row = _pool_runs.setdefault(run_id, {'status': 'running'})
            row['status'] = 'running'
            row['progress'] = dict(payload)

    try:
        _t0 = time.time()
        codes = resolve_pool(req.pool)
        strategy_json = (req.strategy_json or '').strip()
        intent_check: Dict[str, Any] = {}
        if not strategy_json:
            _set_progress({'phase': 'generate', 'message': '正在生成策略…'})
            _push_event(run_id, 'pool_phase', {'run_id': run_id, 'phase': 'generate',
                                               'message': '正在生成策略…'})
            from backtest.batch_engine import _auto_detect_asset_type
            n_etf = sum(1 for c in codes if _auto_detect_asset_type(c) == 'etf')
            desc = f'{len(codes) - n_etf} 股票 + {n_etf} ETF'
            gen = generate_pool_strategy(
                (req.idea or '').strip(), desc,
                entry_timing=normalize_timing(req.entry_timing, 'next_open'),
                exit_timing=_normalize_exit(req.exit_timing),
                # 矩阵/口径对比是引擎参数：生成阶段就把这件事告诉意图核对器，
                # 否则它会误报「策略只固定持有一只 hold_bars，未按用户要求对比」（2026-10-25）
                sweep_holds=req.hold_periods,
                compare_exit=req.compare_exit_timings)
            if not gen.get('ok'):
                raise RuntimeError(f'策略生成失败：{gen.get("error")}')
            strategy_json = gen['strategy_json']
            intent_check = gen.get('intent_check') or {}
            _set_progress({'phase': 'evaluate', 'message': '策略已生成，开始逐只回测…'})
            _push_event(run_id, 'pool_phase', {'run_id': run_id, 'phase': 'evaluate',
                                               'message': '策略已生成，开始逐只回测…'})

        def _progress(event: str, payload: dict) -> None:
            data = dict(payload or {})
            data['run_id'] = run_id
            if event == 'pool_progress':
                _set_progress({'phase': 'evaluate', **data})
            elif event == 'pool_phase':
                _set_progress(data)
            _push_event(run_id, event, data)

        entry_timing = normalize_timing(req.entry_timing, 'next_open')
        actual_exit = _normalize_exit(req.exit_timing) or entry_timing
        end = req.end or datetime.now().strftime('%Y-%m-%d')

        def _run_once(spec_json: str, exit_mode: str,
                      idx: int = 1, rounds: int = 1) -> Dict[str, Any]:
            def _progress_scoped(event: str, payload: dict) -> None:
                data = dict(payload or {})
                # 矩阵/对比是多组串行：带上「当前第几组」，否则前端进度条每组都从 0 重来
                if rounds > 1:
                    data['round'] = idx
                    data['rounds'] = rounds
                _progress(event, data)
            return run_pool_first(
                spec_json, pool_spec=req.pool, start=req.start, end=end,
                entry_timing=entry_timing, exit_timing=exit_mode,
                workers=req.workers, max_positions=req.max_positions,
                mode=req.mode, progress_cb=_progress_scoped)

        holds = sorted({int(h) for h in (req.hold_periods or [])
                        if str(h).isdigit() and 1 <= int(h) <= 60})
        if len(holds) >= 2:
            # 持有期矩阵：同一策略只改 conditions.sell.hold_bars，逐组回测
            import copy
            from backtest.strategy_base import TIMING_LABELS
            try:
                base = json.loads(strategy_json or '{}')
            except (TypeError, ValueError):
                base = {}
            if not isinstance(base, dict):
                base = {}
            modes = ['next_open', 'next_close'] if req.compare_exit_timings else [actual_exit]
            combos = [(h, m) for h in holds for m in modes]
            runs: Dict[Any, Dict[str, Any]] = {}
            for idx, (hold, mode) in enumerate(combos, 1):
                spec = copy.deepcopy(base)
                cond = spec.setdefault('conditions', {})
                if not isinstance(cond, dict):
                    cond = {}
                    spec['conditions'] = cond
                sell = cond.setdefault('sell', {})
                if not isinstance(sell, dict):
                    sell = {}
                    cond['sell'] = sell
                sell['hold_bars'] = hold
                _set_progress({'phase': 'evaluate',
                               'message': f'持有期矩阵 {idx}/{len(combos)}：持有 {hold} 根·'
                                          + str(TIMING_LABELS.get(mode, mode)) + '…'})
                runs[(hold, mode)] = _run_once(
                    json.dumps(spec, ensure_ascii=False), mode,
                    idx=idx, rounds=len(combos))
            base_sell = ((base.get('conditions') or {}).get('sell') or {})
            try:
                default_hold = int(base_sell.get('hold_bars') or holds[0])
            except (TypeError, ValueError):
                default_hold = holds[0]
            primary = (default_hold, modes[0]) if (default_hold, modes[0]) in runs else combos[0]
            result = runs[primary]
            result['sweep'] = {
                'holds': holds, 'modes': modes, 'primary': list(primary),
                # 只留矩阵需要的摘要，不把每组逐只明细都塞进结果
                'rows': {
                    str(hold) + '_' + mode: {
                        'hold': hold, 'timing': mode,
                        'label': f'持有 {hold} 根 · ' + str(TIMING_LABELS.get(mode, mode)),
                        'overall': (r.get('cross_section') or {}).get('overall') or {},
                        'verdict': (r.get('cross_section') or {}).get('verdict') or {},
                        'portfolio': r.get('portfolio') or {},
                    }
                    for (hold, mode), r in runs.items()
                },
            }
        elif req.compare_exit_timings:
            from backtest.strategy_base import TIMING_LABELS
            modes = ['next_open', 'next_close']
            primary = actual_exit if actual_exit in modes else 'next_open'
            order = [primary] + [m for m in modes if m != primary]
            runs: Dict[str, Any] = {}
            for idx, mode in enumerate(order, 1):
                _set_progress({'phase': 'evaluate',
                               'message': f'对比卖出口径 {idx}/{len(order)}：'
                                          + str(TIMING_LABELS.get(mode, mode)) + '…'})
                runs[mode] = _run_once(strategy_json, mode, idx=idx, rounds=len(order))
            result = runs[primary]
            result['compare'] = {
                'primary': primary,
                'modes': modes,
                # 只留对比需要的摘要，不把两遍逐只明细都塞进结果
                'rows': {
                    m: {'timing': m, 'label': TIMING_LABELS.get(m, m),
                        'overall': (runs[m].get('cross_section') or {}).get('overall') or {},
                        'verdict': (runs[m].get('cross_section') or {}).get('verdict') or {},
                        'portfolio': runs[m].get('portfolio') or {}}
                    for m in modes
                },
            }
        else:
            result = _run_once(strategy_json, actual_exit)
        if intent_check:
            result['intent_check'] = intent_check
        with _pool_runs_lock:
            _pool_runs[run_id] = {'status': 'done', 'result': result}
        # 落盘到池子研究库：否则结果只在进程内存里，历史/池子研究两个页面都看不到
        _persist_pool_run(run_id, req=req, result=result,
                          elapsed_sec=round(time.time() - _t0, 1))
        _push_event(run_id, 'final', {'run_id': run_id, 'type': 'final',
                                      'status': 'done', 'result': result})
    except Exception as e:  # noqa: BLE001
        logger.exception('[策略研发] 池子异步任务 %s 失败', run_id)
        with _pool_runs_lock:
            _pool_runs[run_id] = {'status': 'failed', 'error': str(e)[:400]}
        _persist_pool_run(run_id, req=req, error=str(e)[:400],
                          elapsed_sec=round(time.time() - _t0, 1))
        _push_event(run_id, 'task_error', {'run_id': run_id, 'type': 'error',
                                           'message': f'池子运行失败：{e}'})


@router.post('/pool-runs')
async def start_pool_run(req: PoolRunRequest):
    """启动异步池子任务，返回 run_id；进度/结果走 SSE。"""
    from backtest.pool_first import resolve_pool

    try:
        resolve_pool(req.pool)
    except ValueError as e:
        return {'code': 1, 'data': {}, 'msg': str(e)}
    if not (req.strategy_json or '').strip():
        idea = (req.idea or '').strip()
        if len(idea) < 4:
            return {'code': 1, 'data': {}, 'msg': '需要 strategy_json 或至少 4 个字的 idea'}
        hits = scan_sensitive_words(idea)
        if hits:
            return {'code': 1, 'data': {}, 'msg': f'想法包含不合规词汇（{",".join(hits)}）'}
    run_id = 'PR-' + datetime.now().strftime('%Y%m%d%H%M%S') + '-' + uuid.uuid4().hex[:4]
    with _pool_runs_lock:
        _pool_runs[run_id] = {'status': 'running',
                              'progress': {'phase': 'queued', 'message': '排队中…'}}
    thread = threading.Thread(target=_run_pool_async, args=(run_id, req),
                              daemon=True, name=f'pool-run-{run_id}')
    thread.start()
    return {'code': 0, 'data': {'run_id': run_id,
                                'events_url': f'/api/strategy-research/pool-runs/{run_id}/events'},
            'msg': 'ok'}


@router.get('/pool-runs/{run_id}')
async def get_pool_run(run_id: str):
    with _pool_runs_lock:
        row = dict(_pool_runs.get(run_id) or {})
    if not row:
        raise HTTPException(status_code=404, detail=f'池子任务不存在: {run_id}')
    return {'code': 0, 'data': row, 'msg': 'ok'}


@router.get('/pool-runs/{run_id}/events')
async def stream_pool_events(run_id: str):
    with _pool_runs_lock:
        row = dict(_pool_runs.get(run_id) or {})
    if not row:
        raise HTTPException(status_code=404, detail=f'池子任务不存在: {run_id}')

    async def event_stream():
        # 重连/已完成：直接补 final
        if row.get('status') == 'done' and row.get('result'):
            yield f"event: final\ndata: {json.dumps({'run_id': run_id, 'type': 'final', 'status': 'done', 'result': row['result']}, ensure_ascii=False, default=str)}\n\n"
            return
        if row.get('status') == 'failed':
            yield f"event: task_error\ndata: {json.dumps({'run_id': run_id, 'type': 'error', 'message': row.get('error') or '池子失败'}, ensure_ascii=False)}\n\n"
            return
        tick = 0
        while tick < SSE_MAX_TICKS:
            for item in _drain_events(run_id):
                body = json.dumps(item['data'], ensure_ascii=False, default=str)
                yield f"event: {item['event']}\ndata: {body}\n\n"
                if item['event'] in ('final', 'task_error'):
                    return
            tick += 1
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@router.post('/run')
async def create_run(req: StartRunRequest):
    """创建策略研究任务（立即返回 run_uuid，SSE 走 /runs/{uuid}/events）"""
    try:
        info = start_strategy_run(
            req.idea, req.universe, period_start=req.period_start,
            period_end=req.period_end, source=req.source,
            dialog_uuid=req.dialog_uuid,
            entry_timing=req.entry_timing, exit_timing=req.exit_timing)
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
                _hide_legacy_annual(r.get(f))
    _attach_iter_counts(runs)
    return {'code': 0, 'data': {'runs': runs, 'total': len(runs)}, 'msg': 'ok'}


def _attach_iter_counts(runs: List[Dict[str, Any]]) -> None:
    """给中断任务补「已完成轮数」：历史项要显示「从第 N 轮续跑」。

    ``total_iters`` 只在循环结束时写，中断任务永远是 0；而续跑按钮必须知道 N。
    只对 interrupted 行发一条 grouped 查询（一次），不影响其它列表。
    """
    ids = [r.get('id') for r in runs if r.get('status') == 'interrupted' and r.get('id')]
    if not ids:
        return
    from utils.cache.backtest_db import get_backtest_db
    counts: Dict[int, int] = {}
    try:
        with get_backtest_db() as conn:
            rows = conn.execute(
                'SELECT run_id, COUNT(*) AS n FROM strategy_iter '
                'WHERE run_id IN (%s) GROUP BY run_id' % ','.join('?' * len(ids)),
                ids).fetchall()
        counts = {int(r['run_id']): int(r['n']) for r in rows}
    except Exception:  # noqa: BLE001 - 轮数补不到就退化成「续跑」，不拖垮列表
        logger.warning('[策略研究] 中断任务轮数统计失败', exc_info=True)
    for r in runs:
        if r.get('status') == 'interrupted':
            r['iter_count'] = counts.get(int(r.get('id') or 0), 0)


@router.get('/runs/{run_uuid}')
async def get_run_detail(run_uuid: str):
    """任务详情 + 最优策略卡（含过拟合检验结论与免责声明）"""
    if not get_strategy_run(run_uuid):
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    return {'code': 0, 'data': _run_detail_payload(run_uuid), 'msg': 'ok'}


def _hide_legacy_annual(metrics: Any) -> None:
    """旧口径（无 ``annual_method``）的年化从 API 出口抹掉。

    旧 run 的 ``annual_return`` 来自 backtrader 的 ``rnorm100``（固定 252 期/年、按
    bar 数放大），与新的自然日 CAGR 不可比，月线数据下还会出现 383% 这种假数字。
    结果卡已经在 ``cagr_of`` 里处理；轮次表/详情页直接读 metrics JSON，所以在这里
    统一置 None —— 前端所有视图读同一个字段，不会漏。
    """
    if isinstance(metrics, dict) and metrics.get('annual_method') != 'calendar_days':
        metrics['cagr'] = None


def _normalize_iters_annual(iters: List[Dict[str, Any]]) -> None:
    for it in iters or []:
        _hide_legacy_annual(it.get('is_metrics'))
        _hide_legacy_annual(it.get('oos_metrics'))


def _run_detail_payload(run_uuid: str) -> Dict[str, Any]:
    """任务详情载荷（HTTP 端点与技能工具共用）。任务不存在返回错误 dict（不抛 HTTP 异常）。"""
    run = get_strategy_run(run_uuid)
    if not run:
        return {'error': f'策略研究任务不存在: {run_uuid}'}
    iters = get_strategy_iters(run_uuid)
    _normalize_iters_annual(iters)
    best = None
    if run.get('best_iter_id'):
        best = get_strategy_iter_by_id(run['best_iter_id'])
    card = _build_best_card(run, best)
    entry_timing, exit_timing = _timings_of_run(run)
    return {'run': run, 'iters': iters, 'best_card': card, 'disclaimer': DISCLAIMER,
            'factor_prior': _factor_prior_summary(),
            # 顶层再给一份解析后的口径：前端展示「尾盘买入 → 次日开盘卖出」直接用，
            # 不用去 run 行里自己处理「空串 = 跟随 entry」这条内部约定。
            'entry_timing': entry_timing, 'exit_timing': exit_timing}


@router.get('/runs/{run_uuid}/iters')
async def list_iters(run_uuid: str):
    """轮次表（前端刷新/重连补齐用）"""
    run = get_strategy_run(run_uuid)
    if not run:
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    iters = get_strategy_iters(run_uuid)
    _normalize_iters_annual(iters)
    _attach_backtest_run_ids(run_uuid, iters)
    return {'code': 0, 'data': {'run_uuid': run_uuid, 'iters': iters,
                                'status': run.get('status')}, 'msg': 'ok'}


def _attach_backtest_run_ids(run_uuid: str, iters: List[Dict[str, Any]]) -> None:
    """为每轮补账本 run_id（形如 ``SR-IS-sr_<strategy_hash>``），前端据此直接拉 K 线。

    为什么由后端给：前端自己拼 ``SR-IS-sr_`` + hash 一旦拼错只会得到一张空图、
    且没有任何报错（静默失效）。查不到时给 None（更早的研究 / 该轮没生成策略 /
    账本没落库），由前端显示「本轮无回测账本记录」。
    """
    if not iters:
        return
    from utils.cache.backtest_db import get_backtest_db
    for it in iters:
        it['backtest_run_id'] = None
        it['backtest_run_status'] = None
        strategy_hash = str(it.get('strategy_hash') or '')
        if not strategy_hash:
            continue
        try:
            with get_backtest_db() as conn:
                row = conn.execute(
                    "SELECT run_id, status FROM backtest_runs "
                    "WHERE ref_id = ? AND strategy_id = ? AND run_id LIKE 'SR-IS-%' "
                    "ORDER BY CASE WHEN status = 'completed' THEN 0 ELSE 1 END, created_at DESC "
                    "LIMIT 1",
                    (run_uuid, 'sr_' + strategy_hash),
                ).fetchone()
        except Exception:  # noqa: BLE001 — 账本查询失败不能拖垮轮次表
            logger.warning('[策略研究] 账本 run_id 查询失败: %s', run_uuid, exc_info=True)
            row = None
        if row:
            it['backtest_run_id'] = row['run_id']
            it['backtest_run_status'] = row['status']


@router.get('/runs/{run_uuid}/card')
async def get_run_card(run_uuid: str, dialog_uuid: str = ''):
    """对话内卡片 payload：进度卡 / 结果卡（只读，无副作用）。

    用途：前端进度卡订阅到 ``final`` 后，**不用用户再问一句**就能把进度卡
    原地升级成结果卡。组装逻辑与「用户问一句拿结果卡」共用
    ``agents.shared.strategy_session_runner.build_result_payload``——
    两条路径给出不同数字是最坏的一致性事故。

    Args:
        dialog_uuid: 可选。任务行已落成交口径（entry_timing / exit_timing），
            这里带它只是老库（任务行缺列）时回退到会话的兜底；不传也能出正确口径。
    """
    run = get_strategy_run(run_uuid)
    if not run:
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')

    from agents.shared.strategy_card import build_progress_card
    from agents.shared.strategy_session_runner import build_result_payload

    session = None
    try:
        from utils.cache.backtest_db import get_strategy_session_by_run
        session = get_strategy_session_by_run(run_uuid)
    except Exception:  # noqa: BLE001
        session = None
    if session is None and dialog_uuid:
        try:
            from utils.cache.backtest_db import get_open_strategy_session
            cand = get_open_strategy_session(dialog_uuid)
            if cand and str(cand.get('run_uuid') or '') == run_uuid:
                session = cand
        except Exception:  # noqa: BLE001
            session = None

    status = str(run.get('status') or '')
    if status in ('done', 'failed', 'cancelled'):
        # 口径以**任务行**为准（发起时已落库）：会话可能早已归档/不存在，
        # 之前只靠会话 → 面板发起的 run 在卡上永远显示默认 next_open。
        entry_timing, exit_timing = _timings_of_run(run)
        sess = dict(session or {})
        sess['entry_timing'] = entry_timing
        sess['exit_timing'] = exit_timing
        payload = build_result_payload(run_uuid, sess)
        if payload is None:
            return {'code': 1, 'data': None, 'msg': '结果卡组装失败'}
        # 卡片 payload 里的 entry/exit 是给前端画「尾盘买入 → 次日开盘卖出」用的；
        # build_result_card 只认识 entry，这里补齐卖出侧与解析后的值。
        payload['entry_timing'] = entry_timing
        payload['exit_timing'] = exit_timing
        return {'code': 0, 'data': {'kind': 'result', 'payload': payload,
                                    'status': status}, 'msg': 'ok'}

    iters = get_strategy_iters(run_uuid)
    return {'code': 0, 'data': {'kind': 'progress', 'status': status,
                                'payload': build_progress_card(run=run, iters=iters)},
            'msg': 'ok'}


@router.get('/runs/{run_uuid}/iters/{round_no}/curve')
async def get_iter_curve(run_uuid: str, round_no: int):
    """某一轮的净值 / 回撤曲线（样本内 + 样本外两段）。

    研发期每次 IS/OOS 回测都落在统一回测账本里（run_id = SR-IS|SR-OOS-sr_<策略指纹>），
    这里按 strategy_iter.strategy_hash 反查，供前端画「样本内 + 样本外」净值曲线。
    曲线不在账本里（历史数据、或该轮没生成策略）时返回 exists=False，由前端显示空态。
    """
    if not get_strategy_run(run_uuid):
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    return {'code': 0, 'data': _iter_curve_payload(run_uuid, round_no), 'msg': 'ok'}


def _curve_freq(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从曲线日期序列反推 bar 频率（中位相邻间隔 > 5 自然日即非日线）。

    为什么要反推：历史 run 的 `backtest_results` 没存频率标记，而月线回测画出来
    只有十几个点、X 轴却横跨两三年，前端若不标注，用户会读成「曲线没渲染全」。
    阈值与 `backtest/data_adapter.MAX_DAILY_GAP_DAYS` 保持同一口径。
    """
    import statistics
    from datetime import date as _date

    dates = []
    for p in points or []:
        try:
            dates.append(_date.fromisoformat(str(p.get('date'))[:10]))
        except (ValueError, TypeError):
            continue
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    bars = len(points or [])
    if not gaps:
        return {'bars': bars, 'gap_days': None, 'is_daily': True}
    gap = float(statistics.median(gaps))
    return {'bars': bars, 'gap_days': round(gap, 1), 'is_daily': gap <= 5.0}


def _iter_curve_payload(run_uuid: str, round_no: int) -> Dict[str, Any]:
    """按轮次取净值/回撤曲线：strategy_hash → 账本里 SR-IS/SR-OOS-sr_<hash> 两条回测结果。"""
    iters = get_strategy_iters(run_uuid)
    target = next((it for it in iters if int(it.get('round') or 0) == int(round_no)), None)
    if not target:
        return {'exists': False, 'reason': f'第 {round_no} 轮不存在'}
    if target.get('compile_ok') in (0, False):
        return {'exists': False, 'reason': '这一轮没有生成可回测的策略'}
    strategy_hash = str(target.get('strategy_hash') or '')
    if not strategy_hash:
        return {'exists': False, 'reason': '这一轮没有记录策略指纹，取不到回测曲线'}
    from utils.cache.backtest_db import get_backtest_result
    seg: Dict[str, Any] = {}
    for label, key in (('IS', 'is'), ('OOS', 'oos')):
        run_id = f'SR-{label}-sr_{strategy_hash}'
        res = get_backtest_result(run_id) or {}
        equity = _load_curve(res.get('equity_curve_json'))
        seg[key] = {
            'run_id': run_id,
            'equity_curve': equity,
            'drawdown_curve': _load_curve(res.get('drawdown_curve_json')),
            'health': res.get('health') or '',
            'health_detail': res.get('health_detail') or '',
            'total_return': res.get('total_return'),
            'sharpe': res.get('sharpe_ratio'),
            'mdd': res.get('max_drawdown'),
            'trades': res.get('trade_count'),
            'freq': _curve_freq(equity),
        }
    if not seg['is']['equity_curve'] and not seg['oos']['equity_curve']:
        return {'exists': False, 'reason': '这一轮的回测曲线不在账本里（多为更早的研究）'}

    # 非日线（月线/周线快照）：曲线点数、年化、夏普全不可信，必须在页面上说清，
    # 不能只让用户自己数「怎么才 16 个点」。历史脏 run 不做回溯修正，只做标注。
    degraded = [label for label, key in (('样本内', 'is'), ('样本外', 'oos'))
                if seg[key]['equity_curve'] and not seg[key]['freq']['is_daily']]
    reason = ''
    if degraded:
        gaps = [str(seg[k]['freq']['gap_days']) for k in ('is', 'oos')
                if seg[k]['equity_curve'] and not seg[k]['freq']['is_daily']]
        reason = ('本次回测的行情不是日线（相邻 bar 中位间隔 ' + '/'.join(gaps)
                  + ' 天），只有 ' + ' / '.join(
                      str(seg[k]['freq']['bars']) for k in ('is', 'oos'))
                  + ' 根 bar —— 曲线密度、年化与夏普均不可信。'
                  + '（指数标的请改用成分股；月线快照不能做日线回测）')
    return {'exists': True, 'round': int(round_no), 'strategy_hash': strategy_hash,
            'is': seg['is'], 'oos': seg['oos'],
            'degraded': bool(degraded), 'degraded_segments': degraded,
            'degraded_reason': reason}


def _load_curve(raw: Any) -> List[Dict[str, Any]]:
    """账本里存的是 JSON 字符串，这里统一解析成列表（解析失败当空，不抛）。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


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


@router.post('/runs/{run_uuid}/resume')
async def resume_run(run_uuid: str):
    """断点续跑：从 ``strategy_iter`` 重建上下文，接着跑同一个 run 的第 N+1 轮。

    不新建 run（用户已确认方案 A）：run_uuid / 前 N 轮结果 / events_url 全部不变，
    前端只是重新订阅同一个事件流。只有 ``interrupted`` 的任务可续跑 —— 其它状态
    要么还在跑、要么已经正常结束，重复起线程只会把同一 run 写乱。
    """
    run = get_strategy_run(run_uuid)
    if not run:
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    status = str(run.get('status') or '')
    if status != 'interrupted':
        raise HTTPException(
            status_code=409,
            detail=f'只有已中断的任务可以续跑（当前 status={status}）')
    iters = get_strategy_iters(run_uuid)
    if not iters:
        raise HTTPException(status_code=400, detail='没有可续跑的轮次')

    resume_iters = _iter_records_from_db(iters)
    entry_timing, exit_timing = _resume_timings(run_uuid)
    # 先落 running 再起线程：前端拿到响应后立刻 GET /iters 也能看到正确状态
    update_strategy_run(run_uuid, status='running', stop_reason='', completed_at=None)
    threading.Thread(
        target=_run_loop_async,
        args=(run_uuid, entry_timing, exit_timing, None, resume_iters),
        daemon=True,
        name=f'strategy-research-resume-{run_uuid}',
    ).start()

    return {
        'code': 0,
        'data': {
            'run_uuid': run_uuid,
            'task_id': run_uuid,
            'period': run.get('period') or '',
            'events_url': f'/api/strategy-research/runs/{run_uuid}/events',
            'resume_from_round': len(resume_iters) + 1,
        },
        'msg': 'ok',
    }


def _iter_records_from_db(iters: List[Dict[str, Any]]) -> List[IterRecord]:
    """``strategy_iter`` 行 → IterRecord（续跑重建上下文用）。

    JSON 字段已由 ``get_strategy_iters`` 反序列化；数值列在 SQLite 里可能为 None，
    统一兜成默认值，避免续跑时在半路因为一条老数据抛异常。
    """
    out: List[IterRecord] = []
    for it in iters or []:
        oos = it.get('oos_metrics') or {}
        is_m = it.get('is_metrics') or {}
        out.append(IterRecord(
            round=int(it.get('round') or 0),
            hypothesis=it.get('hypothesis') or '',
            strategy_json=it.get('strategy_json'),
            strategy_hash=it.get('strategy_hash'),
            strategy_diff=it.get('strategy_diff') or '',
            compile_ok=int(it.get('compile_ok') or 0),
            compile_error=it.get('compile_error'),
            is_metrics=is_m or None,
            oos_metrics=oos or None,
            overfit_flag=int(it.get('overfit_flag') or 0),
            # 低样本标记落库在 metrics JSON 里，IterRecord 是独立字段：续跑时补回来，
            # 否则 select_best 会把「3 笔成交」的轮次当成有效候选。
            low_sample=int(bool(oos.get('low_sample') or is_m.get('low_sample'))),
            verdict=it.get('verdict') or '',
            model=it.get('model') or '',
            id=it.get('id'),
            created_at=it.get('created_at') or '',
        ))
    return out


def _resume_timings(run_uuid: str) -> tuple:
    """续跑的成交口径：优先任务行（新库已落库）→ 原会话 → 默认 next_open。

    为什么必须这个顺序：任务行是「这次研究实际跑的口径」，最权威；老 run（B 批之前）
    行里没有记录，只能回退会话；两者都没有才用默认。exit 空串跟随 entry ——
    续跑线程要拿到**确定**值，否则不同层各有默认值，重跑口径会漂。
    """
    run = get_strategy_run(run_uuid) or {}
    entry = str(run.get('entry_timing') or '').strip()
    exit_ = str(run.get('exit_timing') or '').strip()
    # 只有「任务行完全没记买入口径」才回退会话（老 run）。若任务行记了买入口径、
    # 卖出口径为空，那空串是「跟随买入」的合法值，必须按 entry 解析——不能被会话里
    # 另一个 exit 覆盖，否则续跑口径与首次运行不一致。
    if not entry:
        try:
            from utils.cache.backtest_db import get_strategy_session_by_run
            session = get_strategy_session_by_run(run_uuid) or {}
        except Exception:  # noqa: BLE001 - 口径读不到不能阻断续跑
            session = {}
        entry = str(session.get('entry_timing') or '').strip()
        if not exit_:
            exit_ = str(session.get('exit_timing') or '').strip()
    entry = normalize_timing(entry, 'next_open')
    exit_ = normalize_timing(exit_, 'next_open') if exit_ else entry
    return entry, exit_


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
        # 修 B5：采纳时把研发期的策略指纹写进 strategies，使采纳后的策略可反查迭代史。
        # 必须在 upsert_strategy 之后（INSERT OR REPLACE 会把未列出的列重置为默认值）。
        try:
            from utils.cache.backtest_db import get_strategy_iter_by_id, update_strategy_hash
            iter_id = run.get('best_iter_id')
            iter_row = get_strategy_iter_by_id(int(iter_id)) if iter_id else None
            adopted_hash = (iter_row or {}).get('strategy_hash') or ''
            if adopted_hash:
                update_strategy_hash(strategy_id, adopted_hash)
        except Exception as e:
            logger.warning(f'[策略研发] 回写 strategy_hash 失败（不阻断采纳）: {e}')
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
    身份三列（设计 §8）：source='strategy' / strategy_id（与 _adopt_save 同口径）/
    ref_run_id=研发期那次 run_uuid——前瞻统计据此按策略聚合。
    """
    from signal_eval.db import get_conn

    message_uuid = f"sr_{run['run_uuid']}"
    symbol_key = _to_symbol_key(run['universe'])
    idea_digest = (run.get('idea') or '')[:80]
    core_verdict = f"策略研究:{idea_digest}"
    signal_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    strategy_id = f"sr_{run['run_uuid'].split('-')[-1].lower()}"

    conn = get_conn()
    try:
        conn.execute(
            '''INSERT INTO signal
               (dialog_uuid, message_uuid, symbol_key, symbol_name, decision,
                confidence, sentiment, core_verdict, signal_time, is_market_wide,
                source, strategy_id, ref_run_id)
               VALUES (?, ?, ?, NULL, 'buy', NULL, NULL, ?, ?, 0, 'strategy', ?, ?)''',
            (run.get('dialog_uuid') or '', message_uuid, symbol_key,
             core_verdict, signal_time, strategy_id, run['run_uuid']))
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


def _factor_prior_summary() -> Dict[str, Any]:
    """研究页顶部「因子先验已注入」banner 的数据。

    只报「注入了什么」与「是不是只做了单池」——先验是弱提示，不是结论。
    缺文件但存在可生成先验的原始数据时显式说「该跑生成脚本」，
    不让它静默变空（口径见 `backtest/factor_prior.has_prior_source`）。
    """
    try:
        from backtest import factor_prior as fp
        prior = fp.load_prior()
        if not prior or not prior.get('factors'):
            return {
                'exists': False,
                'missing_reason': ('已有因子评估数据、但缺少先验文件：运行 '
                                   'scripts/ops/build_factor_prior.py 生成'
                                   if fp.has_prior_source() else ''),
            }
        summary = prior.get('summary') or {}
        factors = prior.get('factors') or {}
        source = prior.get('source') or {}

        def names(ids, limit: int = 3):
            return [factors[f]['name'] for f in (ids or [])[:limit] if f in factors]

        return {
            'exists': True,
            'written_at': prior.get('written_at') or '',
            'cross_pool': bool(source.get('cross_pool')),
            'pools': source.get('pools') or [],
            'robust': names(summary.get('robust')),
            'fragile': names(summary.get('fragile')),
            'reverse': names(summary.get('reverse')),
            'n': summary.get('n'),
            'caveat': summary.get('caveat') or '',
        }
    except Exception as e:  # noqa: BLE001 - 先验只影响提示，不能拖垮页面
        logger.warning('[策略研究] 读因子先验失败: %s', e)
        return {'exists': False, 'missing_reason': ''}



class VerifyPeersRequest(BaseModel):
    """多标的验证请求：把本轮最优策略批量跑在同一指数成分股上。"""
    index_code: str = '000300'      # 成分股来源（本地 index_constituent 快照）
    limit: int = 10                  # 跑多少只（上限见 PEER_VERIFY_MAX）；单只约 3s
    window: str = 'oos'             # oos = 只用样本外窗口（默认，结论口径且快）；full = 整段


PEER_VERIFY_MAX = 30
PEER_MIN_TRADES = 5                  # 与 MIN_SAMPLE 同口径：少于这个笔数不算数
PEER_WIN_RATE_OK = 0.5


def _peer_codes(index_code: str, limit: int) -> List[Dict[str, str]]:
    """指数成分股（本地快照，按权重降序取前 N）。不联网。"""
    try:
        from utils.cache.market_data_db import get_market_data_db
        with get_market_data_db() as conn:
            rows = conn.execute(
                'SELECT code, name FROM index_constituent WHERE index_code = ? '
                'ORDER BY COALESCE(weight, 0) DESC, code LIMIT ?',
                (index_code, int(limit))).fetchall()
        return [{'code': r['code'], 'name': r['name'] or ''} for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning('[多标的验证] 取成分股失败: %s', e)
        return []


def _peer_verify_payload(run_uuid: str, req: VerifyPeersRequest) -> Dict[str, Any]:
    """把最优策略跑在一组成分股上，返回聚合结论（不是逐票列表堆砌）。

    为什么需要它：单票胜率没有统计意义（用户 2026-10-18 质询：「就对一个股研究，
    这胜率没啥说法啊」）。这里回答的是「换个票还成立吗」，三档口径与批量回测一致：
    有效（样本够且胜率 > 50%）/ 弱（有结果没达标）/ 无法评判（数据或样本不足，
    不等于策略不行）。
    """
    from backtest.json_compiler import compile_strategy
    from backtest.strategy_research import IsoOsRunner, strategy_hash
    from backtest.strategies import _STRATEGY_REGISTRY

    run = get_strategy_run(run_uuid) or {}
    best = get_strategy_iter_by_id(run['best_iter_id']) if run.get('best_iter_id') else None
    if not best:
        iters = get_strategy_iters(run_uuid)
        best = next((it for it in reversed(iters) if it.get('compile_ok')), None)
    if not best or not best.get('strategy_json'):
        return {'kind': 'batch_validation', 'error': '这次研究没有可复用的策略，先把策略跑出来再做多标的验证'}

    period = str(run.get('period') or '')
    if '~' not in period:
        return {'kind': 'batch_validation', 'error': '这次研究没有记录回测区间'}
    p_start, p_end = period.split('~', 1)

    limit = max(1, min(int(req.limit or 10), PEER_VERIFY_MAX))
    peers = _peer_codes(req.index_code, limit)
    if not peers:
        return {'kind': 'batch_validation',
                'error': f'本地没有 {req.index_code} 的成分股快照，'
                         '先跑 scripts/ops/backfill_index_universe.py 回补'}

    try:
        strategy_cls = compile_strategy(best['strategy_json'])
    except Exception as e:  # noqa: BLE001
        return {'kind': 'batch_validation', 'error': f'策略编译失败：{e}'}
    sid = 'sr_' + strategy_hash(best['strategy_json'])
    _STRATEGY_REGISTRY[sid] = strategy_cls

    runner = IsoOsRunner(run_uuid=run_uuid, origin='peer_verify', ref_id=run_uuid)
    (_, _), (oos_start, oos_end) = runner.split_period(p_start, p_end, str(run.get('universe') or ''))
    win = (p_start, p_end) if req.window == 'full' else (oos_start, oos_end)

    items: List[Dict[str, Any]] = []
    for p in peers:
        code = p['code']
        row: Dict[str, Any] = {'code': code, 'name': p['name']}
        try:
            m = runner._run_segment(sid, code, win[0], win[1], f'PEER-{code}')
            row.update({'health': m.get('health') or '',
                        'trade_count': m.get('trades'),
                        'win_rate': m.get('win_rate'),
                        'sharpe_ratio': m.get('sharpe'),
                        'total_return': m.get('total_return')})
        except Exception as e:  # noqa: BLE001
            row.update({'health': 'error', 'error': str(e)[:160]})
        items.append(row)

    scored = [it for it in items
              if it.get('health') == 'ok' and (it.get('trade_count') or 0) >= PEER_MIN_TRADES]
    unscored = [it for it in items if it not in scored]
    valid = [it for it in scored if (it.get('win_rate') or 0) > PEER_WIN_RATE_OK]
    weak = [it for it in scored if it not in valid]

    def _median(key: str):
        vals = sorted(float(it[key]) for it in scored
                      if it.get(key) is not None and not isinstance(it.get(key), bool))
        if not vals:
            return None
        mid = len(vals) // 2
        return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 4)

    n_valid, n_weak, n_unscored = len(valid), len(weak), len(unscored)
    hint = (f'同一策略在 {req.index_code} 前 {len(peers)} 只上：'
            f'{n_valid} 只有效（样本够且胜率 > {int(PEER_WIN_RATE_OK * 100)}%）、'
            f'{n_weak} 只弱、{n_unscored} 只无法评判。'
            f'单票结论不可外推——看「有效占比」而不是任何一只的胜率。')
    if n_valid == 0 and n_weak > 0:
        hint += ' 一只都没过线，说明这个想法在当前形态下不具备跨票泛化性。'
    return {
        'kind': 'batch_validation',
        'strategy_name': str((best.get('hypothesis') or '')[:40]) or '本轮最优策略',
        'index_code': req.index_code,
        'window': f'{win[0]}~{win[1]}',
        'code_count': len(peers),
        'completed_count': len(items),
        'valid_count': n_valid,
        'weak_count': n_weak,
        'unscored_count': n_unscored,
        'median_sharpe': _median('sharpe_ratio'),
        'median_win_rate': _median('win_rate'),
        'median_total_return': _median('total_return'),
        'hint': hint,
        'items': sorted(items, key=lambda x: -(x.get('win_rate') or -1))[:30],
    }


@router.post('/runs/{run_uuid}/verify-peers')
async def verify_peers(run_uuid: str, req: VerifyPeersRequest):
    """多标的验证：把本轮最优策略批量跑在同一指数成分股上（同步执行，最多 30 只）。"""
    if not get_strategy_run(run_uuid):
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    payload = await asyncio.to_thread(_peer_verify_payload, run_uuid, req)
    return {'code': 0, 'data': payload, 'msg': 'ok'}


# ============================================================
# 池子级研究（cross-sectional study，2026-10-18）
# ============================================================
# 用户要的是研究员式交付：「300 只 / 28 个行业 / 近一年 → 整体成立与否 → 哪些行业更好」。
# 单标的研发只能给出「这只票赚不赚钱」，把它当策略结论没有统计意义。
# 这里把「选优集（单票）→ 验证集（全池）」做成一个后台任务：全池 300 只约 3-4 分钟。

_POOL_STUDIES: Dict[str, Dict[str, Any]] = {}
_POOL_LOCK = threading.Lock()
from backtest import pool_study_store  # noqa: E402 - 只在池子研究段用，就近导入
POOL_MAX_CODES = 400          # 防误传超大池
POOL_DEFAULT_DAYS = 365       # 默认近一年（本地成分股日线大多只回补了 ~1 年）


class PoolStudyRequest(BaseModel):
    index_code: str = '000300'
    limit: int = 0            # 0 = 全池；>0 只跑权重前 N 只（快速验证）
    days: int = POOL_DEFAULT_DAYS


def _industry_map(codes: List[str]) -> Dict[str, str]:
    """code -> 行业名（一只票可能命中多个行业板块，取字典序第一个，保证可复现）。"""
    if not codes:
        return {}
    try:
        from utils.cache.market_data_db import get_market_data_db
        ph = ','.join(['?'] * len(codes))
        with get_market_data_db() as conn:
            rows = conn.execute(
                'SELECT m.code AS code, b.board_name AS name '
                'FROM stock_board_member m JOIN stock_board b ON b.board_id = m.board_id '
                'WHERE b.board_type = ? AND m.code IN (' + ph + ') ORDER BY m.code, b.board_name',
                ['industry'] + list(codes)).fetchall()
        out: Dict[str, str] = {}
        for r in rows:
            out.setdefault(r['code'], r['name'])
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning('[池子研究] 读行业映射失败: %s', e)
        return {}


def _run_pool_study(study_id: str, run_uuid: str, req: PoolStudyRequest) -> None:
    """后台线程：全池跑一遍 + 汇总 + 落内存。任何异常都写进 status，不让任务静默消失。"""
    st = _POOL_STUDIES.get(study_id)
    if st is None:
        return
    _t0 = time.time()
    try:
        from backtest import pool_study as ps
        from backtest.json_compiler import compile_strategy
        from backtest.strategy_research import IsoOsRunner, strategy_hash
        from backtest.strategies import _STRATEGY_REGISTRY

        run = get_strategy_run(run_uuid) or {}
        best = get_strategy_iter_by_id(run['best_iter_id']) if run.get('best_iter_id') else None
        if not best:
            raise ValueError('这次研究没有选出最优策略，先跑完 8 轮再来做池子研究')

        period = str(run.get('period') or '')
        if '~' not in period:
            raise ValueError('这次研究没有记录回测区间')
        p_start, p_end = period.split('~', 1)
        end_dt = datetime.strptime(p_end, '%Y-%m-%d')
        start_dt = end_dt - timedelta(days=max(30, int(req.days or POOL_DEFAULT_DAYS)))
        start_limit = datetime.strptime(p_start, '%Y-%m-%d')
        study_start = max(start_dt, start_limit).strftime('%Y-%m-%d')
        st['period'] = {'start': study_start, 'end': p_end,
                        'days': (end_dt - datetime.strptime(study_start, '%Y-%m-%d')).days}

        peers = _peer_codes(req.index_code, POOL_MAX_CODES if not req.limit else int(req.limit))
        if not peers:
            raise ValueError('本地没有 ' + req.index_code + ' 的成分股快照，先跑 '
                             'scripts/ops/backfill_index_universe.py 回补')
        peers = peers[:POOL_MAX_CODES]

        strategy_cls = compile_strategy(best['strategy_json'])
        sid = 'sr_' + strategy_hash(best['strategy_json'])
        _STRATEGY_REGISTRY[sid] = strategy_cls

        runner = IsoOsRunner(run_uuid=run_uuid, origin='pool_study', ref_id=run_uuid)
        industries = _industry_map([p['code'] for p in peers])
        st['status'] = 'running'
        st['progress'] = {'done': 0, 'total': len(peers), 'ok': 0, 'fail': 0}

        def _seg(code: str) -> Dict[str, Any]:
            return runner._run_segment(sid, code, study_start, p_end, 'POOL-' + code)

        def _progress(done: int, total: int, ok: int, fail: int) -> None:
            st['progress'] = {'done': done, 'total': total, 'ok': ok, 'fail': fail}

        results = ps.run_study(codes=peers, run_segment=_seg, industries=industries,
                               progress=_progress)

        overall = ps.summarize(results)
        industry_rows = ps.group_by_industry(results)
        bars = [r.bars for r in results if r.bars is not None]
        bar_median = int(statistics.median(bars)) if bars else None
        st['report'] = {
            'study_id': study_id,
            'run_uuid': run_uuid,
            'index_code': req.index_code,
            'period': st.get('period'),
            'universe': {'total': len(peers),
                         'with_industry': len(industries),
                         'industries': len([r for r in industry_rows
                                            if r['industry'] != '未分类'])},
            'coverage': {'bars_min': min(bars) if bars else None,
                         'bars_median': bar_median,
                         'bars_max': max(bars) if bars else None},
            'verdict': ps.verdict_of(overall),
            'overall': overall,
            'by_industry': industry_rows,
            'items': [{'code': r.code, 'name': r.name, 'industry': r.industry,
                       'bucket': r.bucket, 'trades': r.trades, 'win_rate': r.win_rate,
                       'total_return': r.total_return}
                      for r in sorted(results,
                                      key=lambda x: -(x.total_return if x.total_return is not None
                                                      else -9))[:12]],
            'caveats': ps.build_caveats(total=len(peers),
                                        scored=overall.get('scored') or 0,
                                        with_industry=len(industries),
                                        period_days=st['period']['days'],
                                        bar_median=bar_median,
                                        neutral_rate_median=overall.get('neutral_rate_median')),
        }
        st['status'] = 'done'
        st['elapsed_sec'] = round(time.time() - _t0, 1)
        # ⚠️ 必须落盘：此前只存进程内字典，重启即丢，也没有任何页面能看到
        # （用户 2026-10-18：「你这个池子在哪看，目前是不是没有页面可以看到？」）
        pool_study_store.save_study(study_id, status='done', run_uuid=run_uuid,
                                    index_code=req.index_code, report=st['report'],
                                    created_at=st.get('created_at') or '',
                                    elapsed_sec=st['elapsed_sec'])
    except Exception as e:  # noqa: BLE001 - 后台线程必须自己收尾
        logger.exception('[池子研究] %s 失败', study_id)
        st['status'] = 'failed'
        st['error'] = str(e)[:400]
        pool_study_store.save_study(study_id, status='failed', run_uuid=run_uuid,
                                    index_code=req.index_code, error=st['error'],
                                    created_at=st.get('created_at') or '',
                                    elapsed_sec=round(time.time() - _t0, 1))


@router.post('/runs/{run_uuid}/pool-study')
async def start_pool_study(run_uuid: str, req: PoolStudyRequest):
    """启动池子级研究（后台跑，返回 study_id，前端轮询进度）。"""
    if not get_strategy_run(run_uuid):
        raise HTTPException(status_code=404, detail=f'策略研究任务不存在: {run_uuid}')
    study_id = 'PS-' + uuid.uuid4().hex[:10].upper()
    with _POOL_LOCK:
        _POOL_STUDIES[study_id] = {'study_id': study_id, 'run_uuid': run_uuid,
                                   'status': 'pending', 'progress': None,
                                   'report': None, 'error': '',
                                   'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    threading.Thread(target=_run_pool_study, args=(study_id, run_uuid, req),
                     daemon=True, name='pool-study').start()
    return {'code': 0, 'data': {'study_id': study_id}, 'msg': 'ok'}


@router.get('/pool-studies')
async def list_pool_studies(limit: int = Query(50, ge=1, le=200)):
    """池子研究列表（新→旧，只含摘要）。含正在跑的（进度取自内存）。"""
    rows = pool_study_store.list_studies(limit)
    with _POOL_LOCK:
        running = [dict(v) for v in _POOL_STUDIES.values()
                   if v.get('status') in ('pending', 'running')]
    # 正在跑的排最前：用户刚点完就在等它
    for r in sorted(running, key=lambda x: x.get('created_at') or '', reverse=True):
        rows.insert(0, {k: r.get(k) for k in (
            'study_id', 'run_uuid', 'status', 'created_at', 'progress')})
    return {'code': 0, 'data': {'studies': rows, 'running': len(running)}, 'msg': 'ok'}


@router.delete('/pool-studies/{study_id}')
async def delete_pool_study(study_id: str):
    """删除一条池子研究（运行中的不允许删）。"""
    with _POOL_LOCK:
        st = _POOL_STUDIES.get(study_id)
        if st and st.get('status') in ('pending', 'running'):
            raise HTTPException(status_code=409, detail='研究正在运行，先等它跑完')
        _POOL_STUDIES.pop(study_id, None)
    if not pool_study_store.delete_study(study_id):
        raise HTTPException(status_code=404, detail=f'池子研究不存在: {study_id}')
    return {'code': 0, 'data': {'deleted': study_id}, 'msg': 'ok'}


@router.get('/pool-studies/{study_id}')
async def get_pool_study(study_id: str):
    """查池子研究进度/结果。内存没有则回落到落盘文件（重启后仍可查看）。"""
    st = _POOL_STUDIES.get(study_id)
    if not st:
        saved = pool_study_store.load_study(study_id)
        if not saved:
            raise HTTPException(status_code=404, detail=f'池子研究不存在: {study_id}')
        return {'code': 0, 'data': saved, 'msg': 'ok'}
    return {'code': 0, 'data': st, 'msg': 'ok'}


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
