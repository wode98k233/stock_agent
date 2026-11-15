"""策略会话卡片：把一轮策略研发的结果渲染成对话里的结构化卡片。

承载方式与 chat-dashboard.js 同一约定：markdown fenced code block + language 标记，
前端按语言标记把整块替换成 DOM。

诚实性硬规则（设计文档 §5.7）：
1. 成交笔数 < MIN_SAMPLE 或胜率缺失 → 必须写出「样本不足，无法区分策略有效与抛硬币」；
2. 体检 health 不是 ok → 必须显式说明是哪一类问题，不许只报收益；
3. IS/OOS 夏普比 > OVERFIT_SHARPE_RATIO → 必须写出「该轮不得当选最优」（AGENTS.md 既有业务规则）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

CARD_MARKER = 'strategy-card'
FENCE = chr(96) * 3
NEWLINE = chr(10)

MIN_SAMPLE = 10
OVERFIT_SHARPE_RATIO = 1.3

_TIMING_TEXT = {
    'next_open': '次日开盘价成交（T+1 默认口径）',
    'same_close': '当日收盘价成交（尾盘买入，按当天收盘价算）',
    'next_close': '次日收盘价成交',
}

#: 结果卡/口径行的短标签（买卖组合时用，长句会读成「次日开盘价成交买入」）。
_TIMING_SHORT = {
    'next_open': '次日开盘',
    'same_close': '当日收盘（尾盘）',
    'next_close': '次日收盘',
}


def timing_text(timing: str) -> str:
    '''成交口径的中文说法（未知值回落默认口径）。会话执行器换口径时用同一套措辞。'''
    return _TIMING_TEXT.get((timing or '').strip().lower(), _TIMING_TEXT['next_open'])


def as_dict(value: Any) -> Dict[str, Any]:
    """把 JSON 字段（可能是 str 也可能是 dict）统一成 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            got = json.loads(value)
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}
    return {}


def num(value: Any) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cagr_of(metrics: Any) -> Optional[float]:
    """只展示新口径（自然日）的年化。

    老 run 的 annual_return 来自 backtrader 的 ``rnorm100``（固定 252 期/年，按 bar 数
    放大）。月线/周线数据下会得到 383% 这种假数字，且与新区间不可比 —— 宁可显示
    「暂无」，也不展示一个会误导人的年化。
    """
    m = as_dict(metrics)
    if m.get('annual_method') != 'calendar_days':
        return None
    return num(m.get('cagr'))


def annual_method_note(is_metrics: Any, oos_metrics: Any) -> List[str]:
    """年化被隐藏时说明原因（否则用户以为「算不出来」）。"""
    is_m = as_dict(is_metrics)
    oos_m = as_dict(oos_metrics)
    if is_m.get('annual_method') == 'calendar_days' and oos_m.get('annual_method') == 'calendar_days':
        return []
    if not is_m and not oos_m:
        return []
    return ['年化口径：这轮结果来自旧版（按 K 线根数放大），数字不可与新结果比较，'
            '已隐藏年化；请看「区间总收益」。']


def fmt_pct(value: Any) -> str:
    v = num(value)
    return '暂无' if v is None else str(round(v * 100, 1)) + '%'


def fmt_num(value: Any, digits: int = 2) -> str:
    v = num(value)
    return '暂无' if v is None else str(round(v, digits))


def wilson(wins: Any, trades: Any) -> Optional[Dict[str, Any]]:
    """复用 signal_eval.stats.wilson_ci（不复制公式）。"""
    w = num(wins)
    n = num(trades)
    if w is None or n is None or n <= 0:
        return None
    try:
        from signal_eval.stats import wilson_ci
        lo, hi = wilson_ci(int(w), int(n))
        return {'low': round(float(lo), 4), 'high': round(float(hi), 4), 'n': int(n)}
    except Exception:
        return None


def sample_notes(trades: Any, win_rate: Any) -> List[str]:
    notes: List[str] = []
    n = int(num(trades) or 0)
    wr = num(win_rate)
    if wr is None:
        notes.append('这批回测没有可用胜率（零交易或统计缺失），不能据此判断策略好坏。')
    elif n < MIN_SAMPLE:
        notes.append('样本不足：仅 ' + str(n) + ' 笔成交，胜率 ' + fmt_pct(wr)
                     + '，无法区分「策略有效」与「抛硬币」。')
    return notes


def is_sample_notes(is_metrics: Any) -> List[str]:
    """样本内小样本标注（IS 过去没有门槛，16 根 bar 5 笔也能当有效结果展示）。"""
    m = as_dict(is_metrics)
    if not m.get('low_sample'):
        return []
    n = int(num(m.get('trades')) or 0)
    return ['样本内同样样本不足（' + str(n) + ' 笔成交）：样本内数字只作参考，'
            '该轮不得当选最优。']


def degenerate_note(best_iter: Any) -> List[str]:
    """这一轮是「空结果」（零成交或体检不过关）时必须说清楚，否则收益 0% 看着像不亏。

    体检的原因由 ``health_notes`` 统一给（同一条文案不重复两遍）。
    """
    it = as_dict(best_iter)
    if str(it.get('verdict') or '') != 'degenerate':
        return []
    return ['这轮没有可评估的结果：成交 0 笔或数据不足，收益/夏普不具信息量'
            '（不交易当然不亏，也不代表策略有效）。']


def overfit_notes(is_metrics: Any, oos_metrics: Any) -> List[str]:
    is_sharpe = num(as_dict(is_metrics).get('sharpe'))
    oos_sharpe = num(as_dict(oos_metrics).get('sharpe'))
    if not is_sharpe or not oos_sharpe:
        return []
    ratio = is_sharpe / oos_sharpe if oos_sharpe else 0.0
    if ratio > OVERFIT_SHARPE_RATIO:
        return ['稳健性存疑：IS 夏普 ' + fmt_num(is_sharpe) + ' / OOS 夏普 ' + fmt_num(oos_sharpe)
                + '，比值 ' + fmt_num(ratio) + ' 已超过 ' + fmt_num(OVERFIT_SHARPE_RATIO, 1)
                + '，该轮不得当选最优（口径见 AGENTS.md）。']
    return []


def health_notes(health: str, health_reason: str = '') -> List[str]:
    h = (health or '').strip().lower()
    if not h:
        return ['这次没取到回测体检结果（health 为空），收益数字请谨慎对待。']
    if h == 'ok':
        return []
    # 取值口径 = backtest/health.py 的 resolve_health/classify_failure 常量集
    # （ok / no_signal / insufficient_bars / data_gap / bad_strategy / invalid_params / error）
    label = {
        'no_signal': '策略在这段区间没有产生任何买卖信号',
        'low_sample': '成交笔数太少，统计不具区分度',
        'insufficient_bars': '窗口内 K 线根数不够指标预热，指标根本没成形',
        'data_gap': '这段区间的数据有缺口，回测结果不完整',
        'bad_strategy': '有条件引擎表达不了、未参与回测，指标不可信',
        'invalid_params': '参数本身不成立（例如初始资金不足一手），不是策略问题',
        'error': '回测执行出错',
    }.get(h, '体检状态异常：' + h)
    return ['体检未通过（' + h + '）：' + label + ('。' + health_reason if health_reason else '。')]


def build_result_card(*, run: Dict[str, Any], best_iter: Optional[Dict[str, Any]] = None,
                      health: str = '', health_reason: str = '',
                      entry_timing: str = 'next_open',
                      exit_timing: str = '',
                      evaluation: Optional[Dict[str, Any]] = None,
                      capability_gaps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """结果卡 payload。前端 chat-strategy-card.js 按字段名渲染。"""
    best_iter = best_iter or {}
    oos = as_dict(best_iter.get('oos_metrics'))
    is_metrics = as_dict(best_iter.get('is_metrics'))
    timing = (entry_timing or 'next_open').strip().lower()
    # 卖出口径空串 = 跟随买入（与引擎/任务行同一契约），不能回落成 next_open。
    exit_timing_resolved = (exit_timing or timing).strip().lower()
    notes: List[str] = []
    notes += sample_notes(oos.get('trades'), oos.get('win_rate'))
    notes += is_sample_notes(is_metrics)
    notes += overfit_notes(is_metrics, oos)
    notes += annual_method_note(is_metrics, oos)
    notes += health_notes(health, health_reason)
    notes += degenerate_note(best_iter)
    if not best_iter:
        # 没有最优轮不是「没跑」，而是「跑完了但没一个站得住」——必须说清楚，
        # 否则用户看到的是一张全是「暂无」的卡，分不清是失败还是没结果。
        # （2026-10-14 起 sharpe 的「算不出来」不再当 0，这种情况会比以前更常见。）
        notes.append('这次迭代没有选出可用策略：样本外夏普要么为负、要么样本不足算不出来。'
                     '结论是「这个想法在 ' + str(run.get('universe') or '该标的')
                     + ' 上没站住」，不是没跑。可以换标的/区间，或说「加个条件再跑」换个结构。')
    # 能力缺口：**必须直接告诉用户**（用户 2026-10-14 明确要求）。
    # 「你的想法里有 2 条引擎现在表达不了」比「跑完 8 轮给你一个改了条件的策略」诚实得多。
    gaps = [g for g in (capability_gaps or []) if isinstance(g, dict) and g.get('name')]
    if gaps:
        _names = '、'.join(str(g.get('name')) for g in gaps[:4])
        notes.append('⚠️ 你的想法里有 ' + str(len(gaps)) + ' 项当前引擎表达不了：' + _names
                     + '。本轮是用现有能力里的近似条件跑的（结果只能当近似参考）；'
                     '要按原话精确验证，得先给引擎补上这些能力（可以告诉我「把这个能力加上」）。')
    _entry_short = _TIMING_SHORT.get(timing, _TIMING_SHORT['next_open'])
    _exit_short = _TIMING_SHORT.get(exit_timing_resolved, _TIMING_SHORT['next_open'])
    notes.append('成交口径：' + _entry_short + '买入 → ' + _exit_short
                 + '卖出。想换成另一个口径就说「换个口径再跑一次」。')
    # 联动标识：策略指纹来自最优轮（strategy_iter.strategy_hash），账本 run_id 沿用
    # research 的命名约定 SR-IS-/SR-OOS- + sr_<指纹>（见 server/routes/strategy_research.py）。
    hash_ = str(best_iter.get('strategy_hash') or '').strip()
    strategy_id = ('sr_' + hash_) if hash_ else ''
    is_run_id = ('SR-IS-' + strategy_id) if strategy_id else ''
    oos_run_id = ('SR-OOS-' + strategy_id) if strategy_id else ''
    wins = None
    trades = num(oos.get('trades'))
    wr = num(oos.get('win_rate'))
    if trades is not None and wr is not None:
        wins = int(round(trades * wr))
    return {
        'kind': 'result',
        'run_uuid': str(run.get('run_uuid') or ''),
        'idea': str(run.get('idea') or ''),
        'universe': str(run.get('universe') or ''),
        'period': str(run.get('period') or ''),
        'entry_timing': timing,
        'entry_timing_text': _TIMING_TEXT.get(timing, _TIMING_TEXT['next_open']),
        'exit_timing': exit_timing_resolved,
        'exit_timing_text': _TIMING_SHORT.get(exit_timing_resolved,
                                              _TIMING_SHORT['next_open']),
        'health': (health or '').strip().lower(),
        'oos': {
            'sharpe': num(oos.get('sharpe')),
            'cagr': cagr_of(oos),                             # 年化：旧口径一律不展示
            'total_return': num(oos.get('total_return')),      # 区间总收益：这段回测整体赚多少
            'mdd': num(oos.get('mdd')),
            'win_rate': wr,
            'trades': trades,
        },
        'is': {
            'sharpe': num(is_metrics.get('sharpe')),
            'cagr': cagr_of(is_metrics),
            'total_return': num(is_metrics.get('total_return')),
            'mdd': num(is_metrics.get('mdd')),
            'win_rate': num(is_metrics.get('win_rate')),
            'trades': num(is_metrics.get('trades')),
            'low_sample': int(is_metrics.get('low_sample') or 0),
        },
        'wilson': wilson(wins, trades),
        'notes': notes,
        # 联动标识（前端据此直接跳模块，而不是只往输入框里填一句话）：
        #   strategy_id = sr_<指纹>，账本/策略一生用它；IS/OOS 的账本 run_id 由它拼出来。
        'strategy_id': strategy_id,
        'is_run_id': is_run_id,
        'oos_run_id': oos_run_id,
        'capability_gaps': gaps,
        # fill = 点按钮后填进输入框的文本（前端只填入、不自动发送，用户可接着补字）
        # nav = 有对应模块时直接跳过去（'kline' 回测模块的 K 线视图 / 'lifecycle' 策略一生 /
        #       'research' 策略研究面板）；模块不可用时前端退回 fill 行为
        'actions': [
            {'id': 'rerun_timing', 'label': '换个口径再跑一次',
             'fill': '换个成交口径再跑一次'},
            {'id': 'add_condition', 'label': '不满意，改一下',
             # 用户口径（2026-10-17）：「对策略不满意，能直接调整」。
             # 标签直白，fill 仍是半句话（只填入不发送，用户接着补条件）。
             'fill': '在上一次的基础上，再加一个条件：'},
            {'id': 'kline', 'label': '在 K 线上看买卖点', 'nav': 'kline',
             'fill': '看看这个策略在 K 线上的买卖点'},
            {'id': 'ledger', 'label': '看这个策略的一生', 'nav': 'lifecycle',
             'fill': '看看这个策略的研发过程'},
            {'id': 'research', 'label': '打开策略研究面板', 'nav': 'research',
             'fill': '打开策略研究面板'},
        ],
        # 样本内外对照（设计文档 §7）：没评判过就是 None，前端显示「尚未评判」。
        # 判定只有一个来源 backtest/eval_runner.py，卡片不自己算 IS/OOS。
        'eval': {
            'verdict': str((evaluation or {}).get('verdict') or 'pending'),
            'is_sharpe': num((evaluation or {}).get('is_sharpe')),
            'oos_sharpe': num((evaluation or {}).get('oos_sharpe')),
            'sharpe_ratio_io': num((evaluation or {}).get('sharpe_ratio_io')),
            'split_date': str((evaluation or {}).get('split_date') or ''),
            'overfit_flag': int((evaluation or {}).get('overfit_flag') or 0),
        },
    }


PROGRESS_PHASES = ['确认口径', '迭代研发', '结果']

_PHASE_BY_STATUS = {
    'pending_confirm': 'confirming',
    'confirming': 'confirming',
    'running': 'running',
    'queued': 'running',
    'done': 'done',
    'failed': 'failed',
    'cancelled': 'failed',
    'error': 'failed',
}


def _phase_of(status: str) -> str:
    return _PHASE_BY_STATUS.get(str(status or '').strip().lower(), 'running')


def _round_row(it: Dict[str, Any]) -> Dict[str, Any]:
    """单轮摘要（供对话内时间线）。假设截断到 60 字，长文本留给面板。"""
    oos = as_dict(it.get('oos_metrics'))
    hypo = str(it.get('hypothesis') or '')
    if len(hypo) > 60:
        hypo = hypo[:60] + '…'
    return {
        'round': it.get('round'),
        'hypothesis': hypo,
        'verdict': str(it.get('verdict') or ''),
        'compile_ok': bool(it.get('compile_ok')),
        'overfit_flag': bool(it.get('overfit_flag')),
        'oos_sharpe': num(oos.get('sharpe')),
        'oos_trades': num(oos.get('trades')),
    }


def build_progress_card(*, run: Dict[str, Any], iters: Optional[List[Dict[str, Any]]] = None,
                        total_rounds: int = 8) -> Dict[str, Any]:
    iters = iters or []
    last = iters[-1] if iters else {}
    phase = _phase_of(str(run.get('status') or ''))
    return {
        'kind': 'progress',
        'run_uuid': str(run.get('run_uuid') or ''),
        'status': str(run.get('status') or ''),
        'idea': str(run.get('idea') or ''),
        'universe': str(run.get('universe') or ''),
        'round_done': len(iters),
        'total_rounds': int(total_rounds),
        # 阶段条（原型 stepper 的对话内版本）：只表达「进行到哪一段」，
        # 具体每一轮做了什么看 rounds 时间线 / 研发面板。
        'phase': phase,
        'phase_steps': PROGRESS_PHASES,
        'phase_index': {'confirming': 0, 'running': 1, 'done': 2, 'failed': 2}[phase],
        # 已跑完的轮次（从头到尾；前端只展示最近 N 条 + 「还有 N 轮」）
        'rounds': [_round_row(it) for it in iters],
        'latest': {
            'round': last.get('round'),
            'hypothesis': str(last.get('hypothesis') or ''),
            'verdict': str(last.get('verdict') or ''),
            'overfit_flag': bool(last.get('overfit_flag') or False),
        },
    }


def render_card_block(payload: Dict[str, Any]) -> str:
    """把 payload 包成 fenced code block（前端按 language 标记替换成 DOM）。"""
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return NEWLINE.join([FENCE + CARD_MARKER, body, FENCE])
