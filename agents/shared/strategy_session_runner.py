"""策略会话执行器：模式无关的「会话轮」执行形状。

设计文档 §5.1：报告轮与策略会话轮是同一个 agent 的两种轮次。
本模块承载策略会话轮，**不导入任何 agent 模式**（react/plan/pdor/group）；
由三个公共执行封装在 `intercept_strategy_turn` 判定「这一轮属于会话」之后调用。
一轮的产物永远是一段对话文本（可能内含 strategy-card 卡片块）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from agents.shared.strategy_card import (build_progress_card, build_result_card, render_card_block,
                                        timing_text)
from agents.shared.strategy_gate import build_confirm_text, describe_timing_line

NEWLINE = chr(10)
MAX_TURNS_FALLBACK = 12
BUSY_STATUS = ('pending', 'queued', 'running')
OVERFIT_SHARPE_RATIO = 1.3
# OOS 短样本阈值：与 backtest/strategy_research.py:36 OOS_MIN_TRADES 同口径
# （strategy_iter 表没有 low_sample 列，它是 IterRecord 的内存派生量，故这里现算）
OOS_MIN_TRADES = 5
# ⛔ 这里**不再**定义 CONFIRM_WORDS / AMEND_WORDS / CLOSE_WORDS / RERUN_WORDS。
# 会话内「现在是哪个阶段、这句话想干什么」由 agents/shared/strategy_turn_router.py 的
# LLM 路由判定（正则只留固定口令闭集做 fast path）。
# 曾经用关键词表判阶段，用户换个说法就全错，且新词只能继续堆—— 方向错了。
# 「是不是策略研究」那一层仍是 regex + LLM 兜底（strategy_gate.py），两层别混淆。

_AMEND_NOTES_KEY = 'amend_notes'
# 修改意见的上限：120 字会把「区间 + 成交口径 + 要求」这类多条件句子腰斩
# （真机实测：用户 130 字的改条件被切掉「收益/胜率」这半句），放宽到 300。
_AMEND_NOTE_MAX = 300


def _txt(lines: List[str]) -> str:
    return NEWLINE.join(lines)


def _norm(text: str) -> str:
    return (text or '').strip().replace(' ', '')


# ⛔ 下面这组 is_*(text) 词表判定函数已全部删除。
# 阶段判定改由 strategy_turn_router.route_session_turn()（LLM）出意图，
# 调用方按intent 分派，不再对自由文本做关键词命中。

# 成交口径切换是**结构化字段**的变更，不是阶段判定：
# 用户说「换成尾盘口径 / 改成次日开盘卖」时，LLM 判为 amend，由调用方再看是否带口径语义。
# 口径词表在 strategy_card.timing_text /引擎 entry_timing 侧是封闭枚举，不参与阶段判定。

def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            got = json.loads(value)
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}
    return {}


def _emit(progress_callback: Optional[Callable], text: str) -> None:
    """进度事件：三种回调形状都容错。

    实测三个调用方的实际形状（计划步骤 5.1 第 3 问）：
    - Web  server/runtime/coordinator.py:160 收 ProgressEvent 且调 event.to_dict() → 必须是对象；
    - TUI  cli/tui/runner.py:37 _coerce 两种都认；
    - REPL cli/repl.py:177 _render_progress 只认 dict（非 dict 直接 return）。
    故先发真 ProgressEvent，再退回 dict 与 (type, text) 两种历史形状。
    """
    if not progress_callback:
        return
    try:
        from utils.progress import ProgressEvent, ProgressType
        progress_callback(ProgressEvent(type=ProgressType.MILESTONE, message=text,
                                        timestamp=time.time()))
        return
    except Exception:
        pass
    try:
        progress_callback({'event_name': 'progress', 'message': text})
        return
    except Exception:
        pass
    try:
        progress_callback('milestone', text)
    except Exception:
        pass


def _max_turns() -> int:
    try:
        from config import Config
        return int(getattr(Config, 'STRATEGY_SESSION_MAX_TURNS', MAX_TURNS_FALLBACK)
                   or MAX_TURNS_FALLBACK)
    except Exception:
        return MAX_TURNS_FALLBACK


def _session_key(session: Optional[Dict[str, Any]]) -> str:
    """取会话主键：DB 行里的字段名是 session_uuid，没有 session_id。

    写错主键不会报错——UPDATE ... WHERE session_uuid = NULL 只是更新 0 行，
    于是「确认后置 active / 记 run_uuid / 累加 turn_count」全部静默失效。
    """
    if not isinstance(session, dict):
        return ''
    for key in ('session_uuid', 'session_id', 'id'):
        val = session.get(key)
        if val not in (None, ''):
            return str(val)
    return ''


def _update_session(session: Optional[Dict[str, Any]], **fields: Any) -> None:
    """写回会话（失败不抛：一轮的产物永远是一段文本）。

    amend_notes 无对应列，合并进 plan_json 落库（见 _AMEND_NOTES_KEY）。
    """
    key = _session_key(session)
    note = fields.pop(_AMEND_NOTES_KEY, None)
    try:
        from utils.cache.backtest_db import update_strategy_session
        if key and note is not None:
            plan = _as_dict((session or {}).get('plan_json'))
            plan[_AMEND_NOTES_KEY] = list(note)
            fields['plan_json'] = json.dumps(plan, ensure_ascii=False)
        if key and fields:
            update_strategy_session(key, **fields)
    except Exception:
        pass


def _load_amend_notes(session: Optional[Dict[str, Any]]) -> List[str]:
    """读已记下的修改意见：内存优先，其次 plan_json 里的落库副本。"""
    if not isinstance(session, dict):
        return []
    notes = session.get(_AMEND_NOTES_KEY)
    if not isinstance(notes, list) or not notes:
        notes = _as_dict(session.get('plan_json')).get(_AMEND_NOTES_KEY)
    if not isinstance(notes, list):
        return []
    return [str(x) for x in notes]


def _patch_slots_from_route(session: Dict[str, Any],
                            route: Optional[Dict[str, Any]],
                            text: str = '') -> bool:
    """把轮内路由抽到的结构化修改**写回槽位与扁平列**（区间 / 标的 / 成交口径）。

    ⚠️ 这是「改条件」能真正生效的唯一入口：会话跑起来时用的是 period_start /
    period_end / universe 这些**列**，而 amend 曾经只把整句话追加进 idea ——
    用户说「区间改成 2024 年 9 月」时，卡上和实际回测都还是原来的推荐值。
    只覆盖用户明确给出的字段，路由没抽到（None）就不动；路由漏抽时用
    词表抽取（`strategy_intent._extract_slots`，只认封闭的相对区间词）兜底。
    """
    if not isinstance(session, dict):
        return False
    route = dict(route or {})
    if text and not (route.get('period_start') and route.get('period_end')
                     and route.get('universe')):
        try:
            from agents.shared.strategy_intent import detect_strategy_intent
            rule_slots = (detect_strategy_intent(text) or {}).get('slots') or {}
        except Exception:  # noqa: BLE001 - 词表兜底失败不影响主路径
            rule_slots = {}
        route['period_start'] = route.get('period_start') or rule_slots.get('period_start')
        route['period_end'] = route.get('period_end') or rule_slots.get('period_end')
        route['universe'] = route.get('universe') or rule_slots.get('universe')
    slots = _as_dict(session.get('slots_json'))
    changed = False
    period_start = str(route.get('period_start') or '').strip()
    period_end = str(route.get('period_end') or '').strip()
    if period_start or period_end:
        current = (slots.get('period') or {}).get('value') \
            if isinstance(slots.get('period'), dict) else None
        current = current if isinstance(current, list) and len(current) == 2 else ['', '']
        start = period_start or str(current[0] or '')
        end = period_end or str(current[1] or '')
        if start and not end:
            from datetime import date
            end = date.today().isoformat()
        if start and end and start <= end:
            slots['period'] = {'value': [start, end], 'source': 'user'}
            changed = True
    universe_raw = str(route.get('universe_spec') or route.get('universe') or '').strip()
    if not universe_raw and text:
        # 路由没抽到范围时，仍从**这句话本身**兜底解析（「范围换成个股ETF混合总数100」）
        try:
            from backtest.pool_scope import parse_scope_text
            universe_raw = parse_scope_text(text) or ''
        except Exception:  # noqa: BLE001
            universe_raw = ''
    universe = universe_raw
    if universe_raw:
        try:
            from agents.shared.strategy_gate import _normalize_universe_value
            universe = _normalize_universe_value(universe_raw)
        except Exception:  # noqa: BLE001
            universe = universe_raw
    if universe:
        slots['universe'] = {'value': universe, 'source': 'user'}
        if not universe.upper().startswith('POOL'):
            slots.pop('compare_exit_timings', None)   # 单票不支持同轮双口径
        changed = True
    timing = str(route.get('entry_timing') or '').strip()
    if timing:
        side = route.get('timing_side')
        side = side if side in ('entry', 'exit') else 'entry'
        slots[side + '_timing'] = {'value': timing, 'source': 'user'}
        changed = True
    if bool(route.get('compare_exit')):
        slots['compare_exit_timings'] = {'value': True, 'source': 'user'}
        changed = True
    if text and str((slots.get('universe') or {}).get('value') or '').upper().startswith('POOL'):
        try:
            from agents.shared.strategy_intent import parse_hold_periods
            holds = parse_hold_periods(text)
            if len(holds) >= 2:
                slots['hold_period_sweep'] = {'value': holds, 'source': 'user'}
                changed = True
        except Exception:  # noqa: BLE001
            pass
    if not changed:
        return False
    from agents.shared.strategy_gate import _slot_columns
    cols = _slot_columns(slots)
    payload = json.dumps(slots, ensure_ascii=False)
    _update_session(session, slots_json=payload, **cols)
    session['slots_json'] = payload
    session.update(cols)
    return True


def _effective_idea(session: Optional[Dict[str, Any]],
                    notes: Optional[List[str]] = None) -> str:
    """把「改条件」的意见并进 idea —— 研发入口只吃一段自由文本，补充条件必须写进去才生效。

    计划偏差说明 6：「再加个条件」本期的最小形态 = 改条件 → 重开会话确认 → 跑新 run；
    没有这一步，用户说的「换成 MACD 金叉」只留痕、跑的仍是原想法。
    """
    base = str((session or {}).get('idea') or '').strip()
    marker = '；补充条件：'
    if marker in base:                      # 多次改条件时不重复叠加
        base = base.split(marker, 1)[0]
    items = [str(x).strip() for x in (notes if notes is not None else _load_amend_notes(session))]
    items = [x for x in items if x]
    if not items:
        return base
    return (base + marker + '；'.join(items))[:500]


def _latest_eval(backtest_run_id: Any) -> Optional[Dict[str, Any]]:
    """取该回测 run 的样本内外评判行（设计文档 §7）。取不到就当没评判过。"""
    rid = str(backtest_run_id or '').strip()
    if not rid:
        return None
    try:
        from utils.cache.backtest_db import get_backtest_eval
        return get_backtest_eval(rid)
    except Exception:
        return None


def _get_run(run_uuid: str) -> Optional[Dict[str, Any]]:
    try:
        from utils.cache.backtest_db import get_strategy_run
        return get_strategy_run(run_uuid)
    except Exception:
        return None


def _get_iters(run_uuid: str) -> List[Dict[str, Any]]:
    try:
        from utils.cache.backtest_db import get_strategy_iters
        return get_strategy_iters(run_uuid) or []
    except Exception:
        return []


def _latest_backtest(run_uuid: str) -> Optional[Dict[str, Any]]:
    try:
        from utils.cache.backtest_db import get_latest_research_backtest
        return get_latest_research_backtest(run_uuid)
    except Exception:
        return None


def _pick_best_iter(iters: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """选最优迭代：口径对齐 backtest/strategy_research.py:select_best。

    引擎的红线四层（AGENTS.md 既有业务规则 + Spike 修订 3 + 事故 2026-10-18）：
    compile_ok 为真、overfit_flag=0、OOS trades ≥ OOS_MIN_TRADES、非 degenerate
    （零成交/数据不可用），且 IS 也不得是小样本。
    strategy_iter 表没有 low_sample 列，故按 metrics JSON 里的标记 / OOS trades 现算。
    没有一轮过红线时退回最后一轮（卡上仍会由 overfit_notes/sample_notes 标注不足）。
    """
    best = None
    best_key: Optional[float] = None
    for it in iters or []:
        if not it.get('compile_ok'):
            continue
        if it.get('overfit_flag'):
            continue
        if str(it.get('verdict') or '') == 'degenerate':
            continue
        oos = _as_dict(it.get('oos_metrics'))
        is_metrics = _as_dict(it.get('is_metrics'))
        if is_metrics.get('low_sample'):
            # IS 小样本同样不得当选（引擎 :select_best 同判据）
            continue
        oos_sharpe = _num(oos.get('sharpe'))
        is_sharpe = _num(is_metrics.get('sharpe'))
        trades = _num(oos.get('trades'))
        if oos_sharpe is None or trades is None or trades < OOS_MIN_TRADES:
            continue
        if is_sharpe and oos_sharpe and (is_sharpe / oos_sharpe) > OVERFIT_SHARPE_RATIO:
            continue
        if best_key is None or oos_sharpe > best_key:
            best_key = oos_sharpe
            best = it
    if best is None and iters:
        best = iters[-1]
    return best


def _extract_run_uuid(res: Any) -> str:
    if isinstance(res, dict):
        if res.get('run_uuid'):
            return str(res['run_uuid'])
        data = res.get('data')
        if isinstance(data, dict) and data.get('run_uuid'):
            return str(data['run_uuid'])
    return ''


def _start_run(session: Dict[str, Any], dialog_uuid: str) -> Any:
    """调用研发入口起任务。延迟导入，避免 agents 层与 server 层循环依赖。

    入口形参（server/routes/strategy_research.py:111）：
    start_strategy_run(idea, universe, period_start=None, period_end=None,
                       source='panel', dialog_uuid=None, entry_timing='next_open',
                       exit_timing='')。
    仍按「存在才传」筛可选参数：老入口/替身没有这些形参时不能硬传（会 TypeError）。
    """
    from server.routes.strategy_research import start_strategy_run
    kwargs: Dict[str, Any] = {
        'idea': str(session.get('idea') or ''),
        'universe': str(session.get('universe') or ''),
        'period_start': session.get('period_start'),
        'period_end': session.get('period_end'),
    }
    entry_timing = str(session.get('entry_timing') or 'next_open')
    # 卖出口径留空 = 让引擎跟随买入口径（保持「买卖同口径」这个最常见默认）
    exit_timing = str(session.get('exit_timing') or '')
    # C 批：把会话轮自建的 trace run_id 交给研发线程沿用 ——
    # 「用户点了开始」与「8 轮迭代」在执行过程里应是连续的一段，而不是两条独立记录。
    trace_bind = {'trace_run_id': str(session.get('trace_run_id') or '')}
    optional = {'source': 'panel', 'dialog_uuid': dialog_uuid,
                'entry_timing': entry_timing or None,
                'exit_timing': exit_timing or None,
                'trace_bind': trace_bind}
    try:
        params = set(inspect.signature(start_strategy_run).parameters)
    except (TypeError, ValueError):
        params = set()
    for key, value in optional.items():
        if key in params and value is not None:
            kwargs[key] = value
    return start_strategy_run(**kwargs)


async def run_session_turn(*, session: Dict[str, Any], user_input: str, dialog_uuid: str = '',
                           progress_callback: Optional[Callable] = None,
                           logger: Any = None,
                           cancel_check: Optional[Callable] = None,
                           channel: str = 'card') -> str:
    """跑一轮策略会话。返回值就是要发给用户的对话文本。

    ⚠️ 整轮（含阶段路由的 LLM 调用）都必须落在 trace 作用域**内**：路由是本轮
    唯一的 LLM 调用，之前排在 _run_in_trace 之前 —— 于是每一轮在月历里都是
    「有 run、token 恒 0」。分派逻辑见 _dispatch_turn。
    """
    if not session:
        return '会话上下文丢了：重新说一遍你的想法，我再开一个会话。'

    limit = _max_turns()
    # ⚠️ 轮次上限检查必须排在状态分派**之后**，不能排在前面：
    # 排在前面会让达到上限的会话连「落地」「重跑」「结束会话」都进不去，
    # 只会反复回同一句提示，会话永远停在 active（真会话实测：12 轮后彻底卡死）。
    status = str(session.get('status') or '')
    turns = int(session.get('turn_count') or 0) + 1
    _update_session(session, turn_count=turns)
    session['turn_count'] = turns

    if status == 'archived':
        return '这个会话已归档。重新说一遍你的想法，我开个新会话。'

    return await _run_in_trace(session, user_input, lambda: _dispatch_turn(
        session=session, user_input=user_input, dialog_uuid=dialog_uuid,
        progress_callback=progress_callback, logger=logger,
        status=status, turns=turns, limit=limit))


async def _dispatch_turn(*, session: Dict[str, Any], user_input: str, dialog_uuid: str,
                         progress_callback: Optional[Callable], logger: Any,
                         status: str, turns: int, limit: int) -> str:
    """阶段路由 + 轮次上限 + 状态分派（整段都跑在 trace 作用域内）。"""
    # ⚠️ 阶段判定交给 LLM（strategy_turn_router），本模块不再对自由文本做关键词命中。
    # 「是不是策略研究」那一层才是 regex + LLM 兜底（strategy_gate），两层职责别混。
    from agents.shared.strategy_turn_router import route_session_turn
    route = await route_session_turn(session, user_input, logger=logger)
    intent = str((route or {}).get('intent') or '')

    # 轮次上限：只拦截「既不是收尾也不是改条件」的输入。
    # 放在路由之后 —— 收尾/重跑/改条件永远要放行，否则会话到了上限就彻底关不掉。
    if turns >= limit and intent not in ('close', 'amend', 'rerun', 'confirm'):
        lines = ['这个会话已经聊了 ' + str(limit) + ' 轮，先把结论收个尾吧。']
        if str(session.get('run_uuid') or ''):
            lines.append('要落地就说「落地」，要重跑就说「重跑」，想换想法就重开一个会话。')
        else:
            lines.append('想换个想法重开一个会话就行；不想留了就说「结束会话」。')
        return _txt(lines)

    if status == 'pending_confirm':
        return await _handle_pending(session, user_input, dialog_uuid,
                                     progress_callback, route)
    return await _handle_active(session, user_input, dialog_uuid,
                                progress_callback, route)


def build_result_payload(run_uuid: str,
                         session: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """按 run_uuid 组装结果卡 payload。

    ⚠️ 与 ``run_session_turn`` 里 ``st == 'done'`` 分支**必须同源**：
    现在有两条路径会产出结果卡——(1) 用户再问一句（文本回复里带卡），
    (2) 前端收到 ``final`` 后调 ``GET /runs/{uuid}/card`` 自动升级。
    两条路径给出不同数字是最坏的一致性事故，所以都走这个函数。
    """
    run = _get_run(run_uuid)
    if not run:
        return None
    iters = _get_iters(run_uuid)
    ledger = _latest_backtest(run_uuid) or {}
    sess = session or {}
    return build_result_card(
        run=run, best_iter=_pick_best_iter(iters),
        health=str(ledger.get('health') or ''),
        health_reason=str(ledger.get('health_detail') or ''),
        # 口径优先任务行（新 run 已落库）；老 run 回退会话。exit 空串 = 跟随 entry。
        entry_timing=str(run.get('entry_timing') or sess.get('entry_timing') or 'next_open'),
        exit_timing=str(run.get('exit_timing') or sess.get('exit_timing') or ''),
        evaluation=_latest_eval(ledger.get('run_id')),
        # 能力缺口（模型想要但引擎没有）：从 sidecar 汇总后**直接告诉用户**，
        # 不能让「条件被近似替代」这件事只留在日志里（用户 2026-10-14 明确要求）。
        capability_gaps=_collect_capability_gaps(run_uuid),
    )


def _collect_capability_gaps(run_uuid: str) -> List[Dict[str, Any]]:
    """汇总某次研发的「能力缺口」（模型想要、引擎没有），去重保序。

    数据来自 utils/cache/backtest_db.py 的 sidecar（每轮写入 intent_check.gaps）。
    用途：结果卡里如实告诉用户「你的想法有 N 项引擎现在表达不了」。
    """
    out: List[Dict[str, Any]] = []
    try:
        from utils.cache.backtest_db import load_intent_checks
        for _round, payload in (load_intent_checks(run_uuid) or {}).items():
            for gap in ((payload or {}).get('gaps') or []):
                if isinstance(gap, dict) and gap.get('name') and gap not in out:
                    out.append(gap)
    except Exception:
        pass
    return out


async def _run_in_trace(session: Dict[str, Any], user_input: str, handler) -> str:
    """在一轮策略对话外包一层独立 trace run（C 批：策略自建 trace，不进 agent 图）。

    之前策略轮没有 trace run：run 由 LangGraph 建，而策略轮压根不进图 →
    研发期 8 轮 LLM 调用在月历/token 统计里凭空消失，且 agent_runner 只能兜底去
    「猜最近一条 run」（分库跨月回退时会命中文档性陈旧行 → 过程流显示上周的周报）。

    ⚠️ 关键约束：**handler 绝不允许跑第二次**（会把一轮对话变成两轮、起两次 run）。
    所以 trace 的建/收全部收敛在 ``_TraceGuard`` 里（建失败= 无 trace 的哨兵，
    它不会抛），本函数只有一条 handler 调用路径；异常原样上抛。
    """
    guard = _TraceGuard(session, user_input)
    try:
        scope = guard.scope
        if scope is None:
            return await handler()
        # 对话正文也要可回放：把用户原话与助手回复各记一个 step。
        # 需要截断，避免一次粘贴超长文本把 trace 载荷撑爆。
        user_text = (user_input or '')[:4000]
        with scope.step('用户输入', {'input': user_text}) as st:
            st.set_output({'input': user_text})
        result = await handler()
        reply = result if isinstance(result, str) else str(result)
        with scope.step('助手回复') as st:
            st.set_output({'reply': reply[:8000]})
        return result
    finally:
        guard.close()


class _TraceGuard:
    """会话轮的 trace 守卫：构造即建run（失败则降级为无 trace），close 负责收尾。"""

    def __init__(self, session: Dict[str, Any], user_input: str):
        self._cm = None
        self._scope = None
        try:
            from agents.shared.strategy_trace import strategy_turn_scope
            self._cm = strategy_turn_scope(session, user_input)
            self._scope = self._cm.__enter__()
            if self._scope is not None:
                session['trace_run_id'] = (self._scope.run_id
                                           or session.get('trace_run_id', ''))
        except Exception as e:
            # 建不起来就当没有 trace：业务照跑，不能因埋点失败打断用户这一轮
            logging.getLogger(__name__).warning(
                '[策略会话] trace 上下文建不起来，本轮无 trace 记录: %s', e)
            self._cm = None
            self._scope = None

    @property
    def scope(self):
        return self._scope

    def close(self) -> None:
        # 必须在 __exit__ 之前发布：__exit__ 会reset strategy_run_scope，之后读不到
        if self._scope is not None and getattr(self._scope, 'run_id', ''):
            try:
                from agents.shared.strategy_trace import publish_turn_run
                publish_turn_run(str(self._scope.run_id))
            except Exception:
                pass
        if self._cm is None:
            return
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass
        finally:
            self._cm = None


def _fmt_num(value: Any) -> str:
    n = _num(value)
    return '—' if n is None else format(n, '.2f')


def _flip_timing(timing: Any) -> str:
    '''两个口径互相切（下一轮默认口径是次日开盘）'''
    return 'next_open' if str(timing or '').strip().lower() == 'same_close' else 'same_close'


def _clear_compare_flag(session: Dict[str, Any]) -> None:
    """明确选定某一种卖出口径后，取消「开盘/收盘对比」标记（否则会继续跑两遍）。"""
    slots = _as_dict(session.get('slots_json'))
    if not isinstance(slots.get('compare_exit_timings'), dict):
        return
    slots.pop('compare_exit_timings', None)
    payload = json.dumps(slots, ensure_ascii=False)
    _update_session(session, slots_json=payload)
    session['slots_json'] = payload


def _handle_timing_switch(session: Dict[str, Any], new_timing: str,
                          side: str = 'entry') -> str:
    '''换成交口径：写回会话 → 回确认卡；确认后才重跑（起新记录，不覆盖刚才那条）

    ``side`` 区分买/卖两侧：引擎已支持买卖分离，「尾盘买入、次日卖出」是两个字段。
    只改一侧时另一侧保持原值，不连带改掉。
    '''
    side = side if side in ('entry', 'exit') else 'entry'
    key = side + '_timing'
    if side == 'exit':
        _clear_compare_flag(session)      # 明确选了卖法，就不再并列跑两种
    _update_session(session, **{key: new_timing}, status='pending_confirm')
    session[key] = new_timing
    session['status'] = 'pending_confirm'
    return _txt(['记下了：' + ('买入口径' if side == 'entry' else '卖出口径')
                 + '换成「' + timing_text(new_timing) + '」。',
                 '会按同一个想法重跑一次，起一条新记录（不覆盖刚才那条，方便你两条对比）。',
                 '', build_confirm_text(session)])


def _resolve_timing(session: Dict[str, Any], route: Optional[Dict[str, Any]]) -> Optional[str]:
    """从 LLM 路由结果里取新成交口径；LLM 没给出就翻转为另一个。

    不做关键词匹配：「尾盘」「次日开盘」这类词交给 LLM 抽（它同时看得见上下文，
    能分辨「改口径」与「加个指标条件」）。翻转会保留原有能力：
    只说「换个口径」而没指定方向时，仍然换到另一个。

    同时抽出改的是买侧还是卖侧（``timing_side``）；LLM 没给就默认买侧。
    """
    valid = ('same_close', 'next_open', 'next_close')
    got = str((route or {}).get('entry_timing') or '').strip().lower()
    side = str((route or {}).get('timing_side') or '').strip().lower()
    if side not in ('entry', 'exit'):
        side = 'entry'
    if got in valid:
        return got
    return _flip_timing(session.get(side + '_timing'))


def _process_summary(session: Dict[str, Any]) -> str:
    '''研发过程摘要：每轮的假设 / OOS 夏普 / 结论（策略详情页接管前的最小形态）'''
    run_uuid = str(session.get('run_uuid') or '')
    iters = _get_iters(run_uuid) if run_uuid else []
    if not iters:
        return '这次研发还没产生迭代记录（可能刚起跑）。过一会儿再问我一句「跑到哪了」就行。'
    lines = ['这次研发一共 ' + str(len(iters)) + ' 轮，每轮的假设与结论：', '']
    for it in iters:
        oos = _as_dict(it.get('oos_metrics'))
        flag = '（疑似过拟合，未当选）' if it.get('overfit_flag') else ''
        lines.append('- 第 ' + str(it.get('round')) + ' 轮：'
                     + str(it.get('hypothesis') or '')[:80]
                     + '｜样本外夏普 ' + _fmt_num(oos.get('sharpe'))
                     + '｜' + (str(it.get('verdict') or '') or '未评判') + flag)
    lines += ['', '完整的四块（回测记录 / 评判 / 前瞻跟踪 / 落地）在策略详情页「一个策略的一生」里。']
    return _txt(lines)


async def _handle_pending(session: Dict[str, Any], user_input: str, dialog_uuid: str,
                          progress_callback: Optional[Callable],
                          route: Optional[Dict[str, Any]] = None) -> str:
    """pending_confirm 阶段：按 LLM 判出的 intent 分派，不做关键词命中。"""
    text = (user_input or '').strip()
    intent = str((route or {}).get('intent') or '')

    if intent == 'close':
        return _handle_close(session, str(session.get('run_uuid') or ''))
    if intent == 'amend':
        # 只改成交口径时不混进条件备注 —— 口径是独立结构化字段（entry_timing）
        if (route or {}).get('timing_only'):
            _new_timing = _resolve_timing(session, route)
            return _handle_timing_switch(session, _new_timing,
                                         str((route or {}).get('timing_side') or 'entry'))
        # 区间/标的/口径这类**结构化**条件必须写回列，否则卡和实际回测都用旧值
        _patch_slots_from_route(session, route, text)
        notes = _load_amend_notes(session)
        note = text[:_AMEND_NOTE_MAX]
        notes.append(note)
        _update_session(session, amend_notes=notes, status='pending_confirm')
        session['amend_notes'] = notes
        return _txt(['记下了：' + note, '', build_confirm_text(session)])
    if intent == 'unrelated':
        return _txt(['你这句话像是想聊别的，我先不把它当成这个策略会话的条件。',
                     '要继续这个策略，直接说条件就行；',
                     '要回到普通分析，回一句「**结束会话**」我就把它收尾，然后你重新问一次。'])
    if intent != 'confirm':
        # LLM 没判成confirm（含 unknown / 判不出来）：不猜，回该阶段的默认动作。
        # 猜测的代价是「用户本来想改条件，却被当成确认直接把错策略跑起来」。
        hint = str((route or {}).get('hint') or '') or '要开跑回一句「开始」；要改条件就直接说。'
        return _txt([hint, '', build_confirm_text(session)])

    universe = str(session.get('universe') or '').strip()
    if not (re.fullmatch(r'\d{6}', universe) or universe.upper().startswith('POOL')):
        # 兜底：老会话/异常答案把一段人话写进了 universe，单票入口会报
        # 「标的需为 6 位代码」并让会话卡在 active 反复报错（真机 2026-10-25）。
        return _txt(['标的范围「' + universe[:60] + '」还不能回测。',
                     '直接说范围：6 位代码 / 沪深300 / 我的自选 / 我的持仓 / 医药板块。',
                     '', build_confirm_text(session)])
    idea = _effective_idea(session)
    session['idea'] = idea
    _update_session(session, status='active', idea=idea)
    session['status'] = 'active'
    _emit(progress_callback, '策略会话已确认，开始研发：' + str(session.get('universe') or ''))
    return await _start_and_report(session, dialog_uuid, progress_callback)


# stop_reason 的枚举字面量（=无信息量）；不在表里的都是「带真实原因的文本」，要展示
_STOP_ENUM = {'failed', 'converged', 'max_rounds', 'budget', 'cancelled', 'done',
              '', 'unknown', 'none', 'null'}


def _first_compile_error(iters: List[Dict[str, Any]], stop_reason: str = '') -> str:
    """取第一处有信息量的错误文本。

    两个来源都要读：
    - strategy_iter.compile_error —— 引擎层的真实报错（array index out of range 等）
    - strategy_run.stop_reason—— 非枚举取值时本身就是原因文案（如「编译失败：条件无法解析」）
    之前只读 stop_reason 且直接原样拼，导致卡片上永远是「原因：failed」。
    """
    for it in iters or []:
        err = str(it.get('compile_error') or '').strip()
        if err and err.lower() not in _STOP_ENUM:
            return err
    reason = (stop_reason or '').strip()
    if reason and reason.lower() not in _STOP_ENUM:
        return reason
    return ''


def _diagnose_failure(stop_reason: str, iters: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把失败归成三类：能自己改的、要换条件的、纯系统抖动的。

    每类给出不同的下一步指引 —— 失败态 UX 的核心不是"显示错误"，
    而是"用户看到失败后知道该按哪个按钮"。原始错误仍原样带出（诚实性硬规则）。
    """
    err = _first_compile_error(iters, stop_reason)
    low = err.lower()
    reason = (stop_reason or '').strip()
    if reason == 'cancelled':
        return {'kind': 'cancelled', 'error': err, 'actions': ['重跑']}
    # 指标预热不足：K 线根数不够，策略本身可能没问题
    if 'index out of range' in low or '预热' in err:
        return {'kind': 'insufficient_bars', 'error': err,
                'actions': ['拉长区间到近 5 年重跑', '换个标的重试']}
    # 数据缺口：行情缺失，不是策略问题
    # 「数据不足：…只有 N 根 K 线/非日线」是研发前闸门的固定前缀（strategy_research.preflight_data），
    # 归到这一类才能给出「去补数据」的下一步，而不是「这次研发没跑成」。
    if ('data' in low or 'gap' in low or 'no data' in low or 'empty' in low
            or '数据不足' in err or '非日线' in err):
        return {'kind': 'data_gap', 'error': err,
                'actions': ['去数据中心补这段数据', '换个区间重试']}
    # 条件引擎表达不了：这是能力缺口，必须直说而不是让用户反复重试
    if '不支持' in err or 'unsupported' in low or '编译' in err:
        return {'kind': 'bad_strategy', 'error': err,
                'actions': ['把条件改简单一点（例如只用均线 + 金叉）', '换个标的重试']}
    if err:
        return {'kind': 'error', 'error': err, 'actions': ['重跑', '换个标的重试']}
    return {'kind': 'error', 'error': '', 'actions': ['重跑']}


_KIND_TITLE = {
    'cancelled': '这次研发被取消了',
    'insufficient_bars': '回测没跑起来：区间内 K 线不够指标预热',
    'data_gap': '回测没跑起来：这段区间的行情数据有缺口',
    'bad_strategy': '回测没跑起来：策略条件超出了当前引擎的表达能力',
    'error': '这次研发没跑成',
}

_KIND_NEXT = {
    'cancelled': '要重跑直接说「重跑」。',
    'insufficient_bars': 'MACD/RSI/布林带这类指标要几十根 K 线才成形。'
                        '把区间从近 1 年拉到近 3~5 年，或换一个历史更长的标的，再跑一次。',
    'data_gap': '这不是策略的问题，是数据没齐。可以去数据中心的「回测数据就绪度」看缺口，'
                '补完再重跑；或者先换一个区间试试。',
    'bad_strategy': '这是当前策略引擎还表达不出来的条件，不是你的想法有问题。'
                    '把条件收窄一点（例如「均线金叉买入 / 死叉卖出」这类基础条件）就能跑。',
    'error': '这是回测执行过程中出的问题，不是你的策略一定不行。'
             '可以直接重跑，也可以换个标的再试一次。',
}


def _failure_text(stop_reason: str, iters: List[Dict[str, Any]]) -> str:
    """失败文案：标题 + 真实错误 + 下一步（诚实性：真实错误必须原样带出）。"""
    diag = _diagnose_failure(stop_reason, iters)
    kind = diag['kind']
    lines = ['**' + _KIND_TITLE.get(kind, '这次研发没跑成') + '**', '']
    if diag['error']:
        lines += ['技术原因：' + diag['error'][:300], '']
    lines.append(_KIND_NEXT.get(kind, ''))
    lines.append('')
    lines.append('你可以这样继续：')
    lines += ['- ' + a for a in diag['actions']]
    lines.append('- 或直接说你的新想法，我重开一个会话重跑。')
    return _txt(lines)


def _handle_close(session: Dict[str, Any], run_uuid: str) -> str:
    """「落地 / 结束会话」：跑动中先中止，跑完了给结论摘要，然后归档。

    走update_strategy_run 直写 DB，不调 route 层（那是个 async 协程，此处同步上下文）。
    """
    run = _get_run(run_uuid) if run_uuid else None
    st = str((run or {}).get('status') or '')
    if run_uuid and st in BUSY_STATUS:
        try:
            from utils.cache.backtest_db import update_strategy_run
            update_strategy_run(run_uuid, status='cancelled')
        except Exception:
            pass
        _update_session(session, status='archived')
        session['status'] = 'archived'
        return _txt(['已经中止这次研发，任务号 ' + run_uuid + '。历史结果仍留在策略库页可以回看。',
                     '现在可以正常问你的问题了。'])

    tail = []
    if st == 'done':
        tail.append('这次研发的结果卡还在上面，策略已存进策略库（策略库 → 详情页可以看完整的一生）。')
    elif st == 'failed':
        tail.append('这次研发失败的原因已经写在上面了，修完条件可以重开一次。')
    _update_session(session, status='archived')
    session['status'] = 'archived'
    return _txt(['策略会话已收尾。'] + tail + ['现在可以正常问你的问题了。'])


def _progress_reply(session: Dict[str, Any], run_uuid: str, run: Dict[str, Any]) -> str:
    iters = _get_iters(run_uuid)
    card = build_progress_card(run=run, iters=iters)
    return _txt(['还在跑（已完成 ' + str(len(iters)) + ' 轮）：', '', render_card_block(card), '',
                 '页面上的进度条会自己走完，不用再问。'
                 '要中止就说「结束会话」，要改条件就先停下来说。'])


async def _handle_active(session: Dict[str, Any], user_input: str, dialog_uuid: str,
                         progress_callback: Optional[Callable],
                         route: Optional[Dict[str, Any]] = None) -> str:
    """active 阶段：按 LLM 判出的 intent 分派（不再有词表命中分支）。"""
    if _is_pool_universe(session):
        return await _handle_pool_active(session, user_input, dialog_uuid,
                                         progress_callback, route)
    run_uuid = str(session.get('run_uuid') or '')
    text = (user_input or '').strip()
    intent = str((route or {}).get('intent') or '')

    run = _get_run(run_uuid) if run_uuid else None
    st = str((run or {}).get('status') or '')

    # close 优先于一切：跑动中也必须能中止，否则会话永远关不掉
    if intent == 'close':
        return _handle_close(session, run_uuid)

    # 跑动中：一律只回进度卡，**任何**意图都不打断研发。
    # 曾经给 amend 开了口子（想「跑动中也能改条件」），实测反而把 active 打成
    # pending_confirm、口径被改掉，研发还在后台跑 —— 状态与实际脱节，很难排查。
    # 改条件必须等跑完/失败后再提，这是刻意的不对称。
    if run_uuid and st in BUSY_STATUS:
        return _progress_reply(session, run_uuid, run or {})

    # 与策略会话无关：明确让路，并给一条「结束会话」的退出口。
    if intent == 'unrelated':
        return _txt([
            '当前还有一个没结束的策略会话（' + (run_uuid or '尚未起跑') + '）。',
            '',
            '你这句话听起来是**普通分析/提问**，我先不把它当策略会话的话处理。',
            '如果确实想在这个会话里继续，直接说就行；',
            '要回到普通分析，回一句「**结束会话**」我就把它收尾，然后你重新问一次。',
        ])

    # summary：研发过程摘要
    if intent == 'summary':
        return _process_summary(session)

    if intent == 'amend':
        if (route or {}).get('timing_only'):
            _new_timing = _resolve_timing(session, route)
            return _handle_timing_switch(session, _new_timing,
                                         str((route or {}).get('timing_side') or 'entry'))
        _patch_slots_from_route(session, route, text)
        notes = _load_amend_notes(session)
        notes.append(text[:_AMEND_NOTE_MAX])
        idea = _effective_idea(session, notes)
        _update_session(session, amend_notes=notes, idea=idea, status='pending_confirm')
        session['amend_notes'] = notes
        session['idea'] = idea
        session['status'] = 'pending_confirm'
        return _txt(['记下了：' + notes[-1] + '。',
                     '这条会作为补充条件重跑一次（不是改这次已经跑完的结果）。',
                     '', build_confirm_text(session)])

    # rerun / confirm（跑完了再confirm 等价于重跑）：重开确认 + 起新 run
    # active 但还没起跑（run_uuid 为空）：confirm / rerun 都应该直接开跑，
    # 而不是重开一张确认卡 —— 会话已经确认过一次了，再问一遍是白问。
    if not run_uuid:
        if intent in ('confirm', 'rerun', 'progress', 'result', ''):
            return await _start_and_report(session, dialog_uuid, progress_callback)

    if intent in ('rerun', 'confirm'):
        if st == 'running':
            return _progress_reply(session, run_uuid, run or {})
        _update_session(session, status='pending_confirm', run_uuid='')
        session['status'] = 'pending_confirm'
        session['run_uuid'] = ''
        return _txt(['好，按原来的想法重跑一次。', '', build_confirm_text(session)])

    if not run:
        return ('没找到这次研发任务（' + run_uuid + '），可能被清理了。回一句「重跑」我就再起一次。')

    if st == 'done':
        # 结果卡与前端「跑完自动升级」共用同一个组装函数（见 build_result_payload 注释）
        card = build_result_payload(run_uuid, session) or build_result_card(run=run)
        return _txt(['跑完了。先看这张卡——口径、结果、以及哪些地方还不足：', '',
                     render_card_block(card), '',
                     '想继续就说「加个条件：成交额大于 20 日均量」；要落地就说「落地」。'])

    if st == 'cancelled':
        return _txt(['这次研发已经中止了。', '', build_confirm_text(session)])

    # intent 为 unknown/非法（LLM 判不出来）：给进度或失败说明，不猜
    if intent in ('progress', 'result', '') or intent not in (
            'confirm', 'amend', 'rerun', 'close', 'progress', 'result', 'summary', 'unrelated'):
        if st in ('done', 'failed'):
            if st == 'failed':
                return _failure_text(str(run.get('stop_reason') or ''), _get_iters(run_uuid))
        return _txt([str((route or {}).get('hint') or '这条我没太理解你想做什么。'),
                     '要结果就说「跑到哪了」，要改条件直接说条件，要收尾就说「结束会话」。'])

    return _failure_text(str(run.get('stop_reason') or ''), _get_iters(run_uuid))


def _is_pool_universe(session: Dict[str, Any]) -> bool:
    return str((session or {}).get('universe') or '').strip().upper().startswith('POOL')


def _pool_result_text(result: Dict[str, Any]) -> str:
    """池子优先结果：Markdown 直出，避免再为 MVP 造一套卡片协议。"""
    pool = result.get('pool') or {}
    cs = result.get('cross_section') or {}
    overall = cs.get('overall') or {}
    verdict = cs.get('verdict') or {}
    pf = result.get('portfolio') or {}
    assets = pool.get('assets') or {}
    n = pool.get('n') or overall.get('total') or 0

    def _pct(v, digits=1):
        return '—' if v is None else f'{float(v) * 100:.{digits}f}%'

    def _nn(v, digits=2):
        return '—' if v is None else f'{float(v):.{digits}f}'

    scope_label = str(pool.get('label') or '')
    cmp_data = result.get('compare') or {}
    cmp_rows = cmp_data.get('rows') or {}
    cmp_modes = cmp_data.get('modes') or []
    primary_label = str((cmp_rows.get(cmp_data.get('primary')) or {}).get('label') or '')
    sweep = result.get('sweep') or {}
    sweep_rows = sweep.get('rows') or {}
    sweep_holds = sweep.get('holds') or []
    sweep_modes = sweep.get('modes') or []
    sweep_primary = sweep.get('primary') or []
    sweep_primary_row = (sweep_rows.get(str(sweep_primary[0]) + '_' + str(sweep_primary[1]))
                         if len(sweep_primary) == 2 else {}) or {}
    lines = [
        f'**池子验证完成：{scope_label + "；" if scope_label else ""}'
        f'{n} 只（{assets.get("stock", 0)} 股票 + {assets.get("etf", 0)} ETF）**',
        '',
        f'- 横截面（逐只独立、不共享资金）可评判：{overall.get("scored", 0)}；'
        f'有效（≥5 笔且胜率>50%）：{overall.get("valid", 0)}（{_pct(overall.get("valid_ratio"))}）',
        f'- 胜率中位：{_pct(overall.get("win_rate_median"))}；'
        f'收益中位：{_pct(overall.get("total_return_median"), 2)}；'
        f'夏普中位：{_nn(overall.get("sharpe_median"))}',
        f'- 盈亏比中位：{_nn(overall.get("profit_factor_median"))}；'
        f'平均持有：{_nn(overall.get("avg_hold_days_median"), 1)} 天；'
        f'回撤中位：{_pct(overall.get("max_drawdown_median"), 1)}',
        f'- 结论：**{verdict.get("label") or "—"}** — {verdict.get("headline") or ""}',
    ]
    if cmp_data and primary_label:
        lines.insert(1, '- 主口径：' + primary_label + '（下列横截面/组合明细按此口径）')
    elif sweep_rows and sweep_primary_row:
        lines.insert(1, '- 主组合：' + str(sweep_primary_row.get('label') or '')
                     + '（下列横截面/组合明细按此组合）')
    if pf:
        lines += [
            '',
            f'- 组合（**共享资金**，{pf.get("max_positions", "?")} 仓、等权）：'
            f'期末 {pf.get("final_equity", 0):,.0f} 元（{_pct(pf.get("total_return"), 2)}），'
            f'年化 {_pct(pf.get("cagr"), 2)}，最大回撤 {_pct(pf.get("max_drawdown"), 1)}；'
            f'{pf.get("buys", 0)} 笔，胜率 {_pct(pf.get("win_rate"))}',
            f'- 组合质量：夏普 {_nn(pf.get("sharpe"))}，盈亏比 {_nn(pf.get("profit_factor"))}，'
            f'平均持有 {_nn(pf.get("avg_hold_days"), 1)} 天，Calmar {_nn(pf.get("calmar"))}',
        ]
    if sweep_rows and sweep_holds and sweep_modes:
        from backtest.strategy_base import TIMING_LABELS

        def _num_or_none(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        lines += ['', '**持有期 × 卖出口径矩阵（同一策略 / 区间 / 池子）**', '',
                  '| 持有期 | 卖出口径 | 胜率中位 | 收益中位 | 夏普中位 | 组合收益 | 最大回撤 |',
                  '|---|---|---|---|---|---|---|']
        best_ret_key, best_ret_val = '', None
        best_win_key, best_win_val = '', None
        for hold in sweep_holds:
            for mode in sweep_modes:
                row = sweep_rows.get(str(hold) + '_' + mode) or {}
                o = row.get('overall') or {}
                p = row.get('portfolio') or {}
                ret = _num_or_none(p.get('total_return'))
                win = _num_or_none(o.get('win_rate_median'))
                if ret is not None and (best_ret_val is None or ret > best_ret_val):
                    best_ret_val, best_ret_key = ret, row.get('label')
                if win is not None and (best_win_val is None or win > best_win_val):
                    best_win_val, best_win_key = win, row.get('label')
                lines.append('| 持有 ' + str(hold) + ' 根 | ' + str(TIMING_LABELS.get(mode, mode))
                             + ' | ' + _pct(win) + ' | ' + _pct(o.get('total_return_median'), 2)
                             + ' | ' + _nn(o.get('sharpe_median'))
                             + ' | ' + _pct(ret, 2) + ' | ' + _pct(p.get('max_drawdown'), 1) + ' |')
        if best_ret_key or best_win_key:
            lines += ['', '组合收益最高：**' + str(best_ret_key or '—') + '**（' + _pct(best_ret_val, 2)
                      + '）；胜率中位最高：**' + str(best_win_key or '—') + '**（'
                      + _pct(best_win_val) + '）。']
        lines += ['', '每组只改 `hold_bars` 与卖出口径，其余条件/区间/池子完全相同。']
    if cmp_modes and cmp_rows:
        def _diff(a: Any, b: Any, digits: int = 2) -> str:
            if a is None or b is None:
                return '—'
            return f'{(float(b) - float(a)) * 100:.{digits}f}%'

        def _ndiff(a: Any, b: Any, digits: int = 2) -> str:
            if a is None or b is None:
                return '—'
            return f'{float(b) - float(a):.{digits}f}'

        lines += ['', '**卖出口径对比（同一策略 / 同一区间 / 同一池子）**', '',
                  '| 卖出口径 | 有效占比 | 胜率中位 | 收益中位 | 夏普中位 | 组合收益 | 最大回撤 |',
                  '|---|---|---|---|---|---|---|']
        for mode in cmp_modes:
            row = cmp_rows.get(mode) or {}
            o = row.get('overall') or {}
            p = row.get('portfolio') or {}
            lines.append('| ' + str(row.get('label') or mode) + ' | '
                         + _pct(o.get('valid_ratio')) + ' | '
                         + _pct(o.get('win_rate_median')) + ' | '
                         + _pct(o.get('total_return_median'), 2) + ' | '
                         + _nn(o.get('sharpe_median')) + ' | '
                         + _pct(p.get('total_return'), 2) + ' | '
                         + _pct(p.get('max_drawdown'), 1) + ' |')
        if len(cmp_modes) >= 2:
            a, b = (cmp_rows.get(cmp_modes[0]) or {}), (cmp_rows.get(cmp_modes[1]) or {})
            oa, ob = (a.get('overall') or {}), (b.get('overall') or {})
            pa, pb = (a.get('portfolio') or {}), (b.get('portfolio') or {})
            lines.append('| 差值（' + str(b.get('label') or cmp_modes[1]) + ' − '
                         + str(a.get('label') or cmp_modes[0]) + '） | '
                         + _diff(oa.get('valid_ratio'), ob.get('valid_ratio'), 1) + ' | '
                         + _diff(oa.get('win_rate_median'), ob.get('win_rate_median'), 1) + ' | '
                         + _diff(oa.get('total_return_median'), ob.get('total_return_median'), 2) + ' | '
                         + _ndiff(oa.get('sharpe_median'), ob.get('sharpe_median')) + ' | '
                         + _diff(pa.get('total_return'), pb.get('total_return'), 2) + ' | '
                         + _diff(pa.get('max_drawdown'), pb.get('max_drawdown'), 1) + ' |')
        lines += ['', '两次回测除卖出口径外完全相同；差值为后一口径减前一口径。']
    caveats = cs.get('caveats') or []
    if caveats:
        lines += ['', '**边界**：' + '；'.join(str(c) for c in caveats[:3])]
    ic = result.get('intent_check') or {}
    try:
        from backtest.pool_first import material_deviations
        devs = material_deviations(ic)
    except Exception:  # noqa: BLE001
        devs = [str(x) for x in (ic.get('deviations') or [])]
    if ic.get('checked') and ic.get('matches') is False and devs:
        lines += ['', '⚠️ **生成策略与你的原话有偏差**：' + '；'.join(devs[:3])]
    lines += ['', '这是**' + (scope_label or '当前标的池') + '**的整体结论，不是单票；'
                 '想看某一只，直接报 6 位代码。']
    return '\n'.join(lines)


async def _start_pool_async(session: Dict[str, Any]) -> str:
    """启动异步池子任务，返回 run_id；不阻塞会话轮。"""
    from server.routes.strategy_research import PoolRunRequest, start_pool_run
    pool_spec = str(session.get('universe') or 'POOL:default')
    _slots = _as_dict(session.get('slots_json'))
    _cmp = _slots.get('compare_exit_timings')
    compare = bool(_cmp.get('value')) if isinstance(_cmp, dict) else False
    _holds = _slots.get('hold_period_sweep')
    holds = [int(x) for x in _holds.get('value')] if (
        isinstance(_holds, dict) and isinstance(_holds.get('value'), list)) else []
    req = PoolRunRequest(
        idea=str(session.get('idea') or '').strip(), pool=pool_spec,
        start=str(session.get('period_start') or '2024-01-01'),
        end=str(session.get('period_end') or time.strftime('%Y-%m-%d')),
        entry_timing=str(session.get('entry_timing') or 'same_close'),
        exit_timing=str(session.get('exit_timing') or ''),
        compare_exit_timings=compare,
        hold_periods=holds,
        mode=str(session.get('pool_mode') or 'both'), max_positions=10)
    out = await start_pool_run(req)
    if not out or out.get('code') != 0:
        raise RuntimeError(str((out or {}).get('msg') or '池子任务启动失败'))
    run_id = str((out.get('data') or {}).get('run_id') or '')
    if not run_id:
        raise RuntimeError('池子任务没有返回 run_id')
    session['pool_run_id'] = run_id
    _update_session(session, run_uuid=run_id, status='active')
    return run_id


async def _handle_pool_active(session: Dict[str, Any], user_input: str, dialog_uuid: str,
                              progress_callback: Optional[Callable],
                              route: Optional[Dict[str, Any]] = None) -> str:
    """池子会话的 active 分支：轮询异步任务，不查 strategy_run。"""
    intent = str((route or {}).get('intent') or '')
    if intent == 'close':
        return _handle_close(session, str(session.get('run_uuid') or ''))
    if intent == 'amend':
        # 池子会话同样要把区间/标的写回列：起跑参数直接取 period_start/universe
        _patch_slots_from_route(session, route, (user_input or '').strip())
        notes = _load_amend_notes(session)
        notes.append((user_input or '').strip()[:_AMEND_NOTE_MAX])
        idea = _effective_idea(session, notes)
        for key in ('pool_result', 'pool_run_id', 'strategy_json'):
            session.pop(key, None)
        _update_session(session, amend_notes=notes, idea=idea, status='pending_confirm', run_uuid='')
        session['amend_notes'] = notes          # build_confirm_text 读内存字段（plan_json 未必已回写）
        session['status'] = 'pending_confirm'
        session['run_uuid'] = ''
        return _txt(['记下了：' + notes[-1] + '。', '', build_confirm_text(session)])
    if intent == 'rerun':
        for key in ('pool_result', 'pool_run_id', 'strategy_json'):
            session.pop(key, None)
        _update_session(session, status='pending_confirm', run_uuid='')
        session['status'] = 'pending_confirm'
        session['run_uuid'] = ''
        return _txt(['好，按原来的想法重跑一次。', '', build_confirm_text(session)])

    # ⚠️ pool_run_id 只存在于内存；会话每轮都会从 DB 重载，因此下一轮必然读不到它。
    # 起跑时 ``_start_pool_async`` 已把同一个 run id 写进 run_uuid 列，这里必须回退读列，
    # 否则「跑到哪了」会走到底部 _start_and_report **再起一条新 run**（真机实测：
    # 每次问进度都新开一条池子回测，旧任务还在后台跑，结论永远拿不到）。
    run_id = str(session.get('pool_run_id') or session.get('run_uuid') or '')
    if run_id:
        from server.routes.strategy_research import get_pool_run
        try:
            snap = await get_pool_run(run_id)
            row = (snap or {}).get('data') or {}
        except Exception:  # noqa: BLE001
            row = {}
        if not row:
            # 池子任务只活在进程内存里：服务重启后必然查不到。
            # 这里必须显式说「任务丢了」，**绝不能**落到下面重开一条 —— 那会让
            # 用户每问一次进度就白跑一次全池回测。
            return _txt(['找不到这次池子任务的进度（服务重启过，后台任务已经丢了）。',
                         '回一句「重跑」我重新起一次，或直接说新的条件。'])
        status = str(row.get('status') or '')
        if status == 'done' and row.get('result'):
            session['pool_result'] = row['result']
            # 结果给完仍保持 active：会话若置 done，下一句「改个条件 / 报个代码」
            # 就不再有活会话，amend/rerun 分支永远走不到（单票路径同口径）。
            _update_session(session, status='active')
            return _pool_result_text(row['result'])
        if status == 'failed':
            _update_session(session, status='active')
            return _txt(['这次池子失败了：' + str(row.get('error') or '未知错误')[:300],
                         '可以重试，或改回单票。'])
        prog = row.get('progress') or {}
        if prog:
            text = (f"池子还在跑：{prog.get('done', 0)}/{prog.get('total', 0)} 只"
                    f"（成功 {prog.get('ok', 0)}，失败 {prog.get('fail', 0)}）"
                    if prog.get('total') else str(prog.get('message') or '生成策略中…'))
        else:
            text = '池子还在跑（生成策略 / 逐只回测）…'
        return _txt([text, '', f'run_id = {run_id}。问「跑到哪了」拿最新进度；'
                                '跑完会落盘到「池子研究」页，再问一次我也把结论贴在这里。'])

    result = session.get('pool_result')
    if isinstance(result, dict):
        return _pool_result_text(result)
    return await _start_and_report(session, dialog_uuid, progress_callback)


async def _start_and_report(session: Dict[str, Any], dialog_uuid: str,
                            progress_callback: Optional[Callable]) -> str:
    universe = str(session.get('universe') or '').strip()
    if universe and not (re.fullmatch(r'\d{6}', universe) or universe.upper().startswith('POOL')):
        # 集中兜底：任何路径想起跑前都先确认标的**可回测**，否则单票入口会报
        # 「标的需为 6 位代码」并且会话反复撞同一个错（真机 2026-10-25）。
        return _txt(['标的范围「' + universe[:60] + '」还不能回测。',
                     '直接说范围：6 位代码 / 沪深300 / 我的自选 / 我的持仓 / 医药板块 / '
                     '个股ETF混合（如「个股ETF混合跑100只」）。',
                     '', build_confirm_text(session)])
    if _is_pool_universe(session):
        try:
            run_id = await _start_pool_async(session)
        except Exception as e:  # noqa: BLE001
            return _txt(['这次池子没起来：' + str(e)[:200],
                         '可以改回单票，或重试一次；数据/生成失败不会编结论。'])
        return _txt(['池子验证已开跑：' + run_id + '。',
                     '后台会先生成策略，再逐只回测（可选共享资金组合）。',
                     '问「跑到哪了」看进度；跑完会落盘到「池子研究」页，'
                     '再问一次我也把结论贴在这里。'])
    try:
        res = _start_run(session, dialog_uuid)
    except Exception as e:
        # 入口自己会校验（想法 ≥4 字、标的必须是 6 位码），别让异常跳出成 500
        return _txt(['这次没起来，研发入口返回：' + str(e)[:200],
                     '把标的、买点、卖点、区间再确认一下，我重试。'])
    run_uuid = _extract_run_uuid(res)
    if not run_uuid:
        detail = str(res)[:200]
        return _txt(['这次没起来，研发入口返回：' + detail,
                     '把标的、买点、卖点、区间再确认一下，我重试。'])
    _update_session(session, run_uuid=run_uuid, status='active')
    session['run_uuid'] = run_uuid
    _emit(progress_callback, '策略研发已开跑：' + run_uuid)
    # 起跑后立刻回一张进度卡（设计 §5.4「进度回对话」）：研发跑在后台线程里，
    # 后端不会自己往对话里推消息 —— 所以文案必须说清「问一句才拿得到结果卡」，
    # 不能让用户以为跑完会自动弹（真浏览器走查发现的措辞不实）。
    card = build_progress_card(
        run={'run_uuid': run_uuid, 'status': 'running',
             'idea': str(session.get('idea') or ''),
             'universe': str(session.get('universe') or '')},
        iters=[],
    )
    # ⚠️ 卡片块与正文之间必须留空行：render_card_block 返回的围栏块结尾没有换行，
    #直接拼接会让收尾的 ``` 不独占一行 → Markdown 解析时把后续正文吞进代码块 →
    # 前端 JSON.parse 拿到的不是纯 JSON，卡片不渲染、露出裸 JSON（真浏览器实测复现）。
    return _txt([render_card_block(card), '',
                 '开跑了：8 轮迭代，每轮都做 IS/OOS 双段回测。run_uuid = ' + run_uuid + '。',
                 '成交口径：' + describe_timing_line(session.get('slots') or
                                                    {'entry_timing': session.get('entry_timing'),
                                                     'exit_timing': session.get('exit_timing')}) + '。',
                 '研发跑在后台，页面会实时更新进度，跑完自动出结果卡，不用再问。'])
