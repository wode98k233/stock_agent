"""策略会话闸门：模式无关的「这一轮属于哪个轮次」判定（设计文档 §5.1）。

被三个公共执行封装在各自 agent.run(...) 之前调用：
- Web  server/agent_runner.py（第 211 行之前）
- TUI  cli/tui/runner.py（第 112 行之前）
- REPL cli/repl.py（第 111 行之前）

返回值语义：
- None  → 不是策略轮，调用方照旧进用户选择的模式（零影响）
- str   → 本轮由会话接管，调用方**不要**再进任何模式的图，直接把字符串当响应返回

为什么放在图前而不是图内：策略轮与报告轮是两种轮次，不是两个 agent；
放在图前才能拿到「有活会话 → 续轮」的闭环（设计文档 §5.1.2）。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

_DISAMBIGUATE_TEXT = (
    '这句我先确认一下：你是想**了解这个指标**（我给你讲清楚它怎么算、怎么用），'
    '还是想**回测一个用它的策略**（我把它变成可执行条件，跑历史数据看胜率）？\n\n'
    '回一句「只是问问」或「跑个回测」就行。'
)


def _log(logger, msg: str, level: str = 'info') -> None:
    if logger is None:
        return
    try:
        getattr(logger, level)(msg)
    except Exception:
        pass


def describe_timing_line(slots: Dict[str, Any]) -> str:
    """把买卖口径拼成一句人话（确认卡/起跑卡共用）。

    「尾盘价买入 → 次日开盘卖出」这种一眼能看懂的形式，是让用户发现口径被改坏的
    最直接手段 —— 口径错了回测结论整体失真，但界面上一旦只显示枚举值，没人会察觉。
    """
    from backtest.strategy_base import TIMING_LABELS, normalize_timing

    def _v(key):
        item = slots.get(key)
        return item.get('value') if isinstance(item, dict) else item

    entry = normalize_timing(_v('entry_timing'), 'next_open')
    exit_ = normalize_timing(_v('exit_timing') or entry, 'next_open')
    text = TIMING_LABELS.get(entry, entry) + '买入 → ' + TIMING_LABELS.get(exit_, exit_) + '卖出'
    if entry == 'same_close' and exit_ in ('next_open', 'next_close'):
        # 引擎已改成按订单设置 coc + T 日预挂卖单：卖单在下一交易日开盘/收盘成交，
        # 不再「晚一根」。这里只把口径讲清楚，不要再吓用户。
        text += '（买入按 T 日收盘；卖出按下一交易日' + ('开盘' if exit_ == 'next_open' else '收盘') + '）'
    return text


def _resolve_slots(verdict: Dict[str, Any], today=None) -> Dict[str, Any]:
    """把判定结果补成可执行槽位；每个槽位记住来源（用户给的 / 系统补的）"""
    from datetime import date, timedelta
    from agents.shared.strategy_intent import _rules
    r = _rules()
    raw = dict(verdict.get('slots') or {})
    slots: Dict[str, Any] = {}
    raw_uni = str(raw.get('universe') or '').strip()
    # 「范围」优先从原话解析（沪深300 / 自选 / 持仓 / 板块 / **中文股名/ETF 简称**）：
    # 只有单票才走 6 位代码；多标的必须落成 POOL: 范围，否则会被单票入口拒绝。
    scope_detail: Dict[str, Any] = {}
    scope = None
    try:
        from backtest.pool_scope import parse_scope_details
        scope_detail = parse_scope_details(str(verdict.get('text') or ''))
        scope = scope_detail.get('spec')
    except Exception:  # noqa: BLE001 - 范围解析失败不影响开会话，回退默认池
        scope_detail = {}
        scope = None
    unresolved = [str(x) for x in (scope_detail.get('unresolved') or []) if str(x).strip()]
    if scope and (not raw_uni or scope.startswith('POOL:codes:')):
        slots['universe'] = {'value': scope, 'source': 'user'}
    elif raw_uni:
        slots['universe'] = {'value': raw_uni, 'source': 'user'}
    else:
        # 池子优先：用户没明确说单只标的时，默认用代表性池验证；
        # 单票只在用户明确报代码时使用。池子规格由 pool_first.resolve_pool 解析。
        slots['universe'] = {'value': 'POOL:default', 'source': 'default_pool'}
    if unresolved:
        # 没认出来的名字必须显式带在卡上：旧实现认不出就静默落成默认/混合池，
        # 用户完全不知道自己点的那几只没进池子（真机 2026-10-25）。
        slots['universe']['unresolved'] = unresolved
    if raw.get('period_start') and raw.get('period_end'):
        slots['period'] = {'value': [raw['period_start'], raw['period_end']], 'source': 'user'}
    else:
        end = today or date.today()
        start = end - timedelta(days=int(r.get('recommended_days', 1095)))
        slots['period'] = {'value': [start.isoformat(), end.isoformat()], 'source': 'recommended'}
    slots['entry'] = {'value': raw.get('entry', ''),
                       'source': 'user' if raw.get('entry') else 'missing'}
    slots['exit'] = {'value': raw.get('exit', ''),
                      'source': 'user' if raw.get('exit') else 'missing'}
    #买/卖**各自**的成交口径（引擎支持买卖分离，不再只有一个全局口径）
    slots['entry_timing'] = {'value': raw.get('entry_timing') or 'next_open',
                             'source': 'user' if raw.get('entry_timing') else 'default'}
    # 卖出口径：没给就跟随买入口径（保持「同口径成交」这个最常见的默认），
    # 但**允许独立设置**——「尾盘买入、次日卖出」正是买卖不同口径。
    _exit_timing = raw.get('exit_timing') or raw.get('entry_timing') or 'next_open'
    slots['exit_timing'] = {'value': _exit_timing,
                            'source': ('user' if raw.get('exit_timing')
                                       else ('user' if raw.get('entry_timing') else 'default'))}
    # 「开盘卖 or 收盘卖」= 同一策略跑两遍对比，不选一个口径硬跑。
    # 只有池子验证支持同轮对比；单票 8 轮研发仍是一条口径。
    try:
        from agents.shared.strategy_intent import parse_compare_timing
        _uni = str((slots.get('universe') or {}).get('value') or '').upper()
        if _uni.startswith('POOL') and parse_compare_timing(str(verdict.get('text') or '')):
            slots['compare_exit_timings'] = {'value': True, 'source': 'user'}
    except Exception:  # noqa: BLE001
        pass
    # 「持有 3-5 个交易日」= 持有期也要对比（与卖出口径组成矩阵）
    try:
        from agents.shared.strategy_intent import parse_hold_periods
        _uni = str((slots.get('universe') or {}).get('value') or '').upper()
        _holds = parse_hold_periods(str(verdict.get('text') or ''))
        if _uni.startswith('POOL') and len(_holds) >= 2:
            slots['hold_period_sweep'] = {'value': _holds, 'source': 'user'}
    except Exception:  # noqa: BLE001
        pass
    return slots


def _slot_questions_template(verdict: Dict[str, Any], slots: Dict[str, Any]) -> List[Dict[str, Any]]:
    """**兜底**用的 4 槽位确认卡（LLM 不可用/输出不合法时才走这里）。

    正常路径是 _slot_questions()（LLM 就着用户这句话现写题面与选项）。
    复用现有 clarify 弹窗结构：id/question/options/recommended/allow_custom

    ⚠️ 题面文案与 `id` 必须成对维护：前端回传的 answers 以 id 为键
    （`agents/shared/clarify.py:125` 与 `:151` 就是按 `q["id"]` 取答案）。
    """
    idea = (verdict.get('text') or '')[:60]
    uni = str((slots.get('universe') or {}).get('value') or '')
    uni_code = uni if uni.isdigit() and len(uni) == 6 else '600519'

    def _slot_text(key):
        item = slots.get(key)
        return str((item or {}).get('value') or '').strip() if isinstance(item, dict) else str(item or '').strip()

    # ⚠️ 题面与**默认选项**都要贴着用户原话：
    # ① 用户说的是「MACD 金叉买入」，卡片却问「「第二天卖出」具体怎么卖？」—— 牛头不对马嘴；
    # ② 更严重的是默认选项写死成「收盘价上穿 5 日均线」：卡片超时（或用户直接点推荐）
    #    会把用户自己说的买点**覆盖**成这个默认值（_apply_answers 按答案写回槽位）。
    #    所以把「你原话里的条件」放成第一项 + recommended，默认即保持原意。
    entry_text = _slot_text('entry')
    exit_text = _slot_text('exit')
    _period_slot = slots.get('period') if isinstance(slots.get('period'), dict) else {}
    _period_raw = _period_slot.get('value')
    period_val = _period_raw if isinstance(_period_raw, list) and len(_period_raw) == 2 else None
    period_src = str(_period_slot.get('source') or '')
    uni = uni or 'POOL:default'          # 池子优先：没解析到范围时默认池仍是第一推荐
    is_pool = uni.upper().startswith('POOL')
    if is_pool:
        try:
            from backtest.pool_scope import describe_scope, scope_short_label
            uni_label = describe_scope(uni)
            uni_short = scope_short_label(uni)
        except Exception:  # noqa: BLE001
            uni_label, uni_short = '默认池（25 股票 + 15 ETF）', '默认池'
        uni_question = '标的范围按「' + uni_label + '」跑吗？想换就在下面选，或直接说范围'
    else:
        uni_question = '「' + (idea or '这个想法') + '」的标的是什么？'
    missing = (slots.get('universe') or {}).get('unresolved') \
        if isinstance(slots.get('universe'), dict) else None
    if missing:
        # 认不出的名字要当面说：不能让用户以为它们已经在池子里
        uni_question += ('；另外 ' + '、'.join(str(x) for x in missing[:5])
                         + ' 我没认出，回答里补 6 位代码（或直接说「不要它们」）')
    return [
        {
            'id': 'universe',
            'question': uni_question,
            # 池子优先：默认推荐已解析到的范围；只有用户明确报代码才退回单票。
            # 指数本身不可作为交易标的（000300 等只用于基准），范围一律落成 POOL: spec。
            'options': ([{'label': '就按「' + uni_short + '」', 'value': uni}] if is_pool else []) + [
                {'label': '我的自选', 'value': 'POOL:watchlist'},
                {'label': '我的持仓', 'value': 'POOL:portfolio'},
                {'label': '沪深300 成分股', 'value': 'POOL:index:000300'},
                {'label': '中证500 成分股', 'value': 'POOL:index:000905'},
                {'label': '个股 + ETF 混合（沪深300 + ETF）', 'value': 'POOL:mixed'},
                {'label': '默认池（25 股票 + 15 ETF）', 'value': 'POOL:default'},
                {'label': '只看 ' + uni_code + ' 单只', 'value': uni_code},
            ],
            'recommended': uni if is_pool else uni_code,
            'allow_custom': True,
        },
        {
            'id': 'entry',
            'question': ('买点确认：你原话里的买入条件是「' + entry_text + '」，按它跑吗？（想换就选下面的或自己写）'
                         if entry_text else '什么条件算「买」？（不写就是每天都买）'),
            'options': ([{'label': '就按「' + entry_text[:18] + '」', 'value': entry_text}] if entry_text else []) + [
                {'label': '收盘价上穿 5 日均线', 'value': 'close > ma5'},
                {'label': 'MACD 金叉', 'value': 'macd golden cross'},
                {'label': 'RSI 跌破 30 后回升', 'value': 'rsi < 30 then up'},
                {'label': 'KDJ 金叉', 'value': 'kdj golden cross'},
                {'label': '尾盘放量（成交量 > 20 日均量 1.5 倍）', 'value': 'volume > ma20 * 1.5'},
                {'label': '每天尾盘买（不看条件）', 'value': 'same_close'},
            ],
            'recommended': entry_text or 'close > ma5',
            'allow_custom': True,
        },
        {
            'id': 'exit',
            'question': ('卖点确认：你原话里的卖出条件是「' + exit_text + '」，按它跑吗？'
                         '（若是隔夜策略，下面可以直接选「次日怎么卖」）'
                         if exit_text else '「第二天卖出」具体怎么卖？（T+1 下本身有歧义）'),
            'options': ([{'label': '就按「' + exit_text[:18] + '」', 'value': exit_text}] if exit_text else []) + [
                {'label': '次日开盘卖出', 'value': 'next_open'},
                {'label': '次日收盘卖出', 'value': 'next_close'},
                {'label': '持有到 MACD 死叉', 'value': 'hold until macd dead cross'},
                {'label': '止盈 5% / 止损 3%', 'value': 'take_profit 5 / stop_loss 3'},
            ],
            'recommended': exit_text or 'next_open',
            'allow_custom': True,
        },
        {
            'id': 'period',
            'question': '拿哪段时间验证？',
            'options': ([{'label': '就按你说的 ' + str(period_val[0]) + ' → ' + str(period_val[1]),
                          'value': str(period_val[0]) + '~' + str(period_val[1])}]
                        if period_val and period_src == 'user' else []) + [
                {'label': '近 1 年', 'value': '365'},
                {'label': '近 3 年（含 2024 年初大跌）', 'value': '1095'},
                {'label': '近 5 年', 'value': '1825'},
            ],
            # 用户原话给了绝对区间时，推荐值必须是它 —— 否则「区间改成 2024 年 9 月」
            # 会被推荐值「近 3 年」静默覆盖（真机 2026-10-25）。
            'recommended': (str(period_val[0]) + '~' + str(period_val[1])
                            if period_val and period_src == 'user' else '1095'),
            'allow_custom': True,
        },
    ]


_SLOT_IDS = ('universe', 'entry', 'exit', 'period')

_SLOT_CARD_PROMPT = """你在把用户的一句「策略想法」落成可回测的参数，请**只针对这句话**写确认卡。

要求：
1. 输出 4 个问题，id 依次固定为 universe / entry / exit / period：
   - universe：标的范围。**已解析到的范围**（下方提示里的 universe）必须作为第一项 + recommended，
     原样使用它的 value（如 `POOL:default` / `POOL:index:000300` / `POOL:watchlist` /
     `POOL:portfolio` / `POOL:union` / `POOL:mixed` / `POOL:board:医药` / `POOL:codes:...`）。再给 2-3 个针对这句话的备选：
     我的自选 / 我的持仓 / 沪深300 成分股 / 中证500 成分股 / 个股+ETF 混合 / 默认池 / 一个 6 位单票代码。
     指数本身不能当交易标的（只用于基准），所以「沪深300」只能写成 `POOL:index:000300`。
     **若原话点名了具体标的（中文股名 / ETF 简称 / 代码），提示里的 universe 就是已解析清单，
     必须原样推荐，绝不能改成沪深300/混合池/默认池**；提示里的 `unresolved` 是没认出的名字，
     必须写进 universe 的 question 里让用户补 6 位代码。
   - entry：买点条件。**必须把用户原话里的买点原样作为第一个选项**（label 写「就按『…』」），
     再给 2-3 个针对这句话的替代选项（不要抄通用清单）。
   - exit：卖点条件。同样把用户原话里的卖点放第一；若这句话是隔夜/次日类想法，
     再给「次日开盘卖出」「次日收盘卖出」。
   - period：回测区间。值是天数（如 365 / 730 / 1095），label 写人话。
2. question 用中文口语，**贴着用户原话**（例：「你说的是『MACD 金叉买入』，买点按这个跑吗？」），
   不要出现「第二天卖出」这类与用户原话无关的模板腔。
3. recommended 必须是该题某个选项的 value。
4. 只输出 JSON，不要解释：
{"questions":[{"id":"universe","question":"...","options":[{"label":"...","value":"..."}],
"recommended":"...","allow_custom":true}, ...]}

用户原话："""


def _validate_slot_questions(questions: Any) -> Optional[List[Dict[str, Any]]]:
    """校验 LLM 生成的确认卡：四个 id 齐全、每题有选项、recommended 落在选项里。

    任何一项不满足就整体回退模板 —— 宁可给一张普通卡，也不给半张错卡
    （前端按 id 回传答案，缺 id 会让那次作答静默丢失）。
    """
    if not isinstance(questions, list):
        return None
    picked: Dict[str, Dict[str, Any]] = {}
    for q in questions:
        if not isinstance(q, dict):
            continue
        qid = str(q.get('id') or '').strip()
        if qid not in _SLOT_IDS or qid in picked:
            continue
        options = []
        for opt in (q.get('options') or []):
            if not isinstance(opt, dict):
                continue
            label = str(opt.get('label') or '').strip()
            value = str(opt.get('value') or '').strip()
            if label and value:
                options.append({'label': label[:48], 'value': value[:120]})
        if qid == 'universe':
            # LLM 可能把范围写成一段人话（真机：会直接导致单票入口报「需 6 位代码」）；
            # 能收敛的走范围解析，收敛不了的选项直接丢弃。
            normalized, seen = [], set()
            for opt in options:
                spec = _normalize_universe_value(opt['value'])
                if spec and spec not in seen:
                    seen.add(spec)
                    normalized.append({'label': opt['label'], 'value': spec})
            options = normalized
        if not options:
            continue
        rec = str(q.get('recommended') or '').strip()
        if qid == 'universe':
            rec = _normalize_universe_value(rec)
        if rec not in {o['value'] for o in options}:
            rec = options[0]['value']
        picked[qid] = {
            'id': qid,
            'question': str(q.get('question') or '').strip()[:200],
            'options': options,
            'recommended': rec,
            'allow_custom': True,
        }
    if set(picked) != set(_SLOT_IDS):
        return None
    out = []
    for qid in _SLOT_IDS:
        item = picked[qid]
        if not item['question']:
            return None
        out.append(item)
    return out


def _force_universe_spec(questions: List[Dict[str, Any]],
                         slots: Dict[str, Any]) -> List[Dict[str, Any]]:
    """点名标的（POOL:codes）时，强制把已解析清单放成第一项 + recommended。

    LLM 写卡时仍可能把 universe 推荐成「沪深300/混合池」（真机 2026-10-25：
    用户点名的科技股与 ETF 被换成了 315 只混合池）。这里做最后一道栏杆：
    只要原话已解析出代码清单，卡上的推荐值就必须是那份清单。
    """
    uni_slot = slots.get('universe') if isinstance(slots.get('universe'), dict) else {}
    uni = str(uni_slot.get('value') or '')
    if not uni.upper().startswith('POOL:CODES:'):
        return questions
    body = uni.split(':', 2)[2].split('|')[0]
    count = len([c for c in body.split(',') if c.strip()])
    for q in questions:
        if q.get('id') != 'universe':
            continue
        opts = [{'label': '就按你点名的 ' + str(count) + ' 只', 'value': uni}]
        opts += [o for o in (q.get('options') or []) if o.get('value') != uni]
        q['options'] = opts
        q['recommended'] = uni
    return questions


def _force_period_range(questions: List[Dict[str, Any]],
                        slots: Dict[str, Any]) -> List[Dict[str, Any]]:
    """用户原话给了绝对区间时，强制把它放成 period 的第一项 + recommended。

    LLM 卡常把区间折成「约 784 天」这种天数，转换回来会差一两天；
    用户明确说了「2024 年 9 月至今」，卡上就该原样显示这段起止日期。
    """
    slot = slots.get('period') if isinstance(slots.get('period'), dict) else {}
    val = slot.get('value')
    if str(slot.get('source') or '') != 'user' or not (
            isinstance(val, list) and len(val) == 2 and val[0] and val[1]):
        return questions
    value = str(val[0]) + '~' + str(val[1])
    for q in questions:
        if q.get('id') != 'period':
            continue
        opts = [{'label': '就按你说的 ' + str(val[0]) + ' → ' + str(val[1]), 'value': value}]
        opts += [o for o in (q.get('options') or []) if o.get('value') != value]
        q['options'] = opts
        q['recommended'] = value
    return questions


async def _slot_questions(verdict: Dict[str, Any], slots: Dict[str, Any],
                          logger: Any = None) -> List[Dict[str, Any]]:
    """确认卡：**优先让 LLM 就着用户这句话现写**（题面 + 选项都贴原话），失败回退模板。

    为什么必须是 LLM 生成：确认卡就是「反问」，反问要针对这句话才有意义 ——
    写死的模板会把「MACD 金叉买入」问成「第二天卖出具体怎么卖」，选项清单也与想法无关。
    失败/关闭开关/输出不合法 → 回退 _slot_questions_template（策略轮绝不能因为
    生成卡片失败而跑不动）。
    """
    from config import Config

    if not bool(getattr(Config, 'STRATEGY_SLOT_CARD_LLM', True)):
        return _slot_questions_template(verdict, slots)
    idea = str(verdict.get('text') or '').strip()[:400]
    if not idea:
        return _slot_questions_template(verdict, slots)
    hint = {k: (slots.get(k) or {}).get('value') for k in ('universe', 'entry', 'exit', 'period')
            if isinstance(slots.get(k), dict) and (slots.get(k) or {}).get('value')}
    uni_slot = slots.get('universe') if isinstance(slots.get('universe'), dict) else {}
    if uni_slot.get('unresolved'):
        hint['unresolved'] = uni_slot.get('unresolved')
    try:
        from utils.llm_factory import get_llm, llm_json_with_retry

        llm = get_llm()
        data = await asyncio.to_thread(
            llm_json_with_retry, llm,
            [{'role': 'user', 'content': _SLOT_CARD_PROMPT + idea
              + ('\n\n已解析到的槽位（请据此把用户原话放进选项）: '
                 + json.dumps(hint, ensure_ascii=False) if hint else '')}],
            logger=logger, label='strategy-slot-card', max_retries=2,
        )
    except Exception as e:                       # 网络/配置/超时都走这里
        _log(logger, '[策略闸门] 确认卡 LLM 生成失败，回退模板：' + str(e), 'warning')
        return _slot_questions_template(verdict, slots)
    questions = _validate_slot_questions((data or {}).get('questions') if isinstance(data, dict) else None)
    if not questions:
        _log(logger, '[策略闸门] 确认卡 LLM 输出不合法，回退模板', 'warning')
        return _slot_questions_template(verdict, slots)
    return _force_period_range(_force_universe_spec(questions, slots), slots)


def _normalize_universe_value(value: Any) -> str:
    """把「标的答案」收敛成可回测的 spec：6 位单票，或 POOL: 范围。

    ⚠️ 确认卡的 LLM 有时会把范围写成**一段人话**（真机 2026-10-25：
    「扩大范围，优先用沪深300，个股ETF混合直到找到100只标的」）——直接写进
    universe 列后，池子入口不认识它，会按单票校验并报「标的需为 6 位代码」。
    这里统一收敛：人话先走范围解析，解析不出来返回空串（调用方必须重新追问）。
    """
    text = str(value or '').strip()
    if not text:
        return ''
    if re.fullmatch(r'\d{6}', text):
        return text
    up = text.upper()
    if up in ('POOL', 'POOL:DEFAULT', 'DEFAULT', 'DEFAULT_POOL'):
        return 'POOL:default'
    if up in ('POOL:SMOKE', 'SMOKE', 'SMOKE_POOL'):
        return 'POOL:smoke'
    if up.startswith('POOL:'):
        try:
            from backtest.pool_scope import is_scope_spec
            if is_scope_spec(text):
                return text
        except Exception:  # noqa: BLE001
            pass
        codes = [c.strip() for c in text.split(':', 1)[1].split(',') if c.strip()]
        if codes and all(re.fullmatch(r'\d{6}', c) for c in codes):
            return text
        return ''
    try:
        from backtest.pool_scope import parse_scope_text
        return parse_scope_text(text) or ''
    except Exception:  # noqa: BLE001
        return ''


def _apply_answers(slots: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
    """把弹窗答案并回槽位（按题目 id 取；答案可能是选项 value，也可能是自定义文本）"""
    from datetime import date, timedelta
    merged = {k: dict(v) if isinstance(v, dict) else v for k, v in slots.items()}
    if not isinstance(answers, dict):
        return merged
    for key in ('universe', 'entry', 'exit'):
        val = str(answers.get(key) or '').strip()
        if key == 'universe':
            val = _normalize_universe_value(val)
            if not val:
                continue          # 解析不出来就别覆盖原范围（调用方会重新追问）
        if key in ('entry', 'exit') and re.fullmatch(r'[a-z0-9_\-]{3,}', val):
            # LLM 卡有时把选项值写成内部代号（deep_v_next_open / hold_3_5_compare_open_close）：
            # 覆盖进 entry_text/exit_text 会让卡上出现一串代号。此时保留**用户原话里的条件**，
            # 口径类答案由下面两个 enum 分支单独处理。
            continue
        if val:
            merged[key] = {'value': val, 'source': 'user'}
    # 卖出题：选项里混了两种语义——成交口径（next_open / next_close / same_close）
    # 与持有条件（MACD 死叉、止盈止损…）。
    #⛔ 旧实现把口径答案写进了 ``entry_timing``（买入口径）：问的是「次日怎么卖」，
    # 却改了「怎么买」的口径 —— 语义完全错位，且用户的买入口径被悄悄覆盖。
    # 现在口径落到 ``exit_timing``，且同时把 exit 文案改成对应的人话。
    exit_val = str(answers.get('exit') or '').strip()
    if exit_val in ('next_open', 'next_close', 'same_close'):
        _label = {'next_open': '次日开盘卖出',
                  'next_close': '次日收盘卖出',
                  'same_close': '当日收盘价卖出（尾盘卖）'}[exit_val]
        merged['exit'] = {'value': _label, 'source': 'user'}
        merged['exit_timing'] = {'value': exit_val, 'source': 'user'}
    # 买入题也允许直接给口径（「按收盘价买」→ same_close）
    entry_val = str(answers.get('entry') or '').strip()
    if entry_val in ('next_open', 'same_close', 'next_close'):
        merged['entry'] = {'value': '按开盘价买入' if entry_val == 'next_open'
                           else ('按收盘价买入（尾盘买）' if entry_val == 'same_close'
                                 else '按次日收盘价买入'), 'source': 'user'}
        merged['entry_timing'] = {'value': entry_val, 'source': 'user'}
    # 区间题：既支持「近 N 年」的天数，也支持 2020-01-01~2023-01-01 这种自定义写法
    period_val = str(answers.get('period') or '').strip()
    if period_val:
        if period_val.isdigit():
            end = date.today()
            start = end - timedelta(days=int(period_val))
            merged['period'] = {'value': [start.isoformat(), end.isoformat()], 'source': 'user'}
        elif '~' in period_val:
            a, _, b = period_val.partition('~')
            if a.strip() and b.strip():
                merged['period'] = {'value': [a.strip(), b.strip()], 'source': 'user'}
    return merged


def _slot_columns(slots: Dict[str, Any]) -> Dict[str, Any]:
    """槽位 → strategy_session 的扁平列（period 拆成 period_start/period_end）"""
    def _v(key, default=''):
        item = slots.get(key)
        if isinstance(item, dict):
            return item.get('value', default)
        return item if item is not None else default
    period = _v('period', []) or []
    start, end = (period[0], period[1]) if isinstance(period, list) and len(period) == 2 else ('', '')
    entry_timing = str(_v('entry_timing', 'next_open') or 'next_open')
    # 卖出口径缺省跟随买入口径（保持同口径这个最常见默认），但允许独立设置
    exit_timing = str(_v('exit_timing', '') or entry_timing or 'next_open')
    return {
        'universe': str(_v('universe') or ''),
        'period_start': str(start or ''),
        'period_end': str(end or ''),
        'entry_text': str(_v('entry') or ''),
        'exit_text': str(_v('exit') or ''),
        'entry_timing': entry_timing,
        'exit_timing': exit_timing,
    }


def _ask_via_channel(questions: List[Dict[str, Any]], raw_question: str, logger=None):
    """经既有澄清通道弹卡。返回 dict（answers）/ 'timeout' / None（无通道 → 文本模式）"""
    from agents.shared.budget_ctx import budget_decision_ctx
    cb = budget_decision_ctx.get()
    if cb is None:
        return None                      # CLI/TUI/测试：文本模式（偏差说明第 2 条）
    from config import Config
    from agents.shared.clarify import CLARIFY_TIMEOUT
    timeout_s = max(30, int(getattr(Config, 'CLARIFY_TIMEOUT_SECONDS', 300)))
    payload = {
        'reason': 'strategy_slots',      # 前端据此换文案，事件名保持 clarify_required
        'event_name': 'clarify_required',
        'questions': questions,
        'original_question': raw_question,
        'timeout_s': timeout_s,
        'deadline_ms': int(time.time() * 1000) + timeout_s * 1000,
    }
    try:
        raw = cb(payload)
    except Exception as e:
        _log(logger, '[策略闸门] 弹卡失败，回退文本确认：' + str(e), 'warning')
        return None
    if raw in (None, '') or raw == 'cancel':
        from utils.cancellation import AgentCancelled
        raise AgentCancelled('用户已停止任务')
    if raw == CLARIFY_TIMEOUT:
        return 'timeout'
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:
        return 'timeout'
    if not isinstance(data, dict):
        return 'timeout'
    if data.get('rejected'):
        return 'rejected'
    return data.get('answers') or {}


def build_confirm_text(session: Dict[str, Any], auto_started: bool = False) -> str:
    """文本模式（无交互通道）下的确认卡：把槽位讲清楚 + 一句确认口令

    公共函数：会话执行器（agents/shared/strategy_session_runner.py）在用户改条件时要重发这张卡。
    ``auto_started``：弹窗超时后已按默认口径开跑，卡上要写明「不用再回开始」。
    """
    try:
        slots = json.loads(session.get('slots_json') or '{}')
    except Exception:
        slots = {}
    lines = [('**已按下面这套开始**（不用再回「开始」）：' if auto_started else
              '**开始验证之前，我需要跟你确认这几件事**（回复「开始」就按下面这套跑）：'), '']
    for key, label in (('universe', '标的'), ('period', '区间'), ('entry', '买入条件'),
                       ('exit', '卖出条件')):
        item = slots.get(key) or {}
        val = item.get('value') if isinstance(item, dict) else item
        if isinstance(val, list):
            val = ' → '.join(str(x) for x in val)
        val = val or '（你在原话里没给，先用推荐值/生成时补）'
        if key == 'universe' and str(val).upper().startswith('POOL'):
            if str(val).upper() in ('POOL', 'POOL:DEFAULT'):
                val = '默认池（25 股票 + 15 ETF，逐只验证 + 组合回测）'
            else:
                try:
                    from backtest.pool_scope import describe_scope
                    val = describe_scope(str(val))
                except Exception:  # noqa: BLE001 - 标签失败也要出卡
                    val = str(val)
        src = {'user': '你给的', 'recommended': '推荐值', 'default': '默认',
               'default_pool': '默认池', 'missing': '待生成'}.get(
                   (item or {}).get('source') if isinstance(item, dict) else '', '')
        lines.append('- ' + label + '：' + str(val) + ('（' + src + '）' if src else ''))
    # 没认出来的名字必须显式写出来：旧实现认不出就静默落成默认/混合池，
    # 用户看到卡上是「沪深300 + ETF 315 只」才发现标的被换了（真机 2026-10-25）。
    uni_slot = slots.get('universe') if isinstance(slots.get('universe'), dict) else {}
    missing_targets = [str(x) for x in (uni_slot.get('unresolved') or []) if str(x).strip()]
    if missing_targets:
        lines.append('- 这些标的我没认出：' + '、'.join(missing_targets[:6])
                     + '（请补 6 位代码；当前只按已认出的跑，不会偷偷换成别的池子）')
    # 改条件留下的补充意见也要进卡：否则用户刚说「区间改成 2024 年 9 月」，
    # 卡片却一字不变地重发一遍 —— 看起来像系统没听见（真机走查复现过）。
    notes = session.get('amend_notes')
    if not isinstance(notes, list) or not notes:
        try:
            notes = json.loads(session.get('plan_json') or '{}').get('amend_notes') or []
        except Exception:  # noqa: BLE001
            notes = []
    for note in [str(x).strip() for x in (notes or []) if str(x).strip()][:5]:
        lines.append('- 补充条件（会并入策略生成）：' + note[:300])
    lines.append('- 成交口径：' + describe_timing_line(slots))
    _cmp = slots.get('compare_exit_timings')
    _uni = str((slots.get('universe') or {}).get('value') or '')
    if isinstance(_cmp, dict) and _cmp.get('value') and _uni.upper().startswith('POOL'):
        lines.append('- 卖出口径对比：次日开盘卖 vs 次日收盘卖（同一策略/区间/池子各跑一遍）')
    _holds = slots.get('hold_period_sweep')
    if (isinstance(_holds, dict) and isinstance(_holds.get('value'), list)
            and _uni.upper().startswith('POOL')):
        lines.append('- 持有期对比：' + ' / '.join(str(x) for x in _holds['value'])
                     + ' 个交易日（与卖出口径组合成矩阵）')
    # 「深V/超跌反弹」是自然语言概念，引擎会用价格条件表达（阈值是系统推断）。
    # 开跑前就把口径说清楚，别等跑完才在底部来一句「与你的原话有偏差」。
    _idea_low = str(session.get('idea') or '').lower()
    if any(k in _idea_low for k in ('深v', '深 v', '超跌', '下探', '探底', '长下影')):
        lines.append('- 深V 判定（系统推断）：最低较前收跌 ≥3%、收盘收回（close > open）、'
                     '振幅 ≥3%；要别的阈值直接说（例如「最低跌 5%」）')
    # 因子先验提醒（S4 F5-A）：想法里点名了「跨池变号 / 方向为负」的因子时先说一声。
    # 只提示不阻断——用户坚持用照样跑。
    try:
        from backtest.factor_prior import prior_warnings
        warns = prior_warnings(str(session.get('idea') or ''))
    except Exception:  # noqa: BLE001
        warns = []
    for w in warns:
        lines.append('- ' + w)
    lines += ['', ('已按这套开始；要改就说「改成…」，我会停掉重跑。' if auto_started
                    else '回一句「开始」我就跑；要改条件就直接说（例如「换成 MACD 金叉」）。')]
    return '\n'.join(lines)


# 兼容别名：任务 4 起的调用点/外部引用仍可用旧私有名
_confirm_text = build_confirm_text


def _has_channel() -> bool:
    try:
        from agents.shared.budget_ctx import budget_decision_ctx
        return budget_decision_ctx.get() is not None
    except Exception:
        return False


async def intercept_strategy_turn(*, user_input: str, dialog_uuid: str = '', mode: str = '',
                                 progress_callback: Optional[Callable] = None,
                                 logger=None, memory=None,
                                 cancel_check: Optional[Callable] = None) -> Optional[str]:
    """闸门主入口。返回 None 表示「不是策略轮」，调用方照旧进模式。"""
    from config import Config
    mode_cfg = str(getattr(Config, 'STRATEGY_INTENT_MODE', 'confirm') or 'confirm').lower()
    if mode_cfg == 'off':
        return None
    text = (user_input or '').strip()
    if not text:
        return None

    from utils.cache.backtest_db import (get_open_strategy_session, create_strategy_session,
                                         update_strategy_session)
    dlg = dialog_uuid or 'local'          # CLI：单用户本地，用固定 dialog 收口会话

    # ① 有活着的会话 → 续轮（不再做意图判定）
    session = get_open_strategy_session(dlg)
    if session:
        from agents.shared.strategy_session_runner import run_session_turn
        return await run_session_turn(session=session, user_input=text, dialog_uuid=dlg,
                                      progress_callback=progress_callback, logger=logger,
                                      cancel_check=cancel_check,
                                      channel='card' if _has_channel() else 'text')

    # ② 意图判定
    from agents.shared.strategy_intent import detect_strategy_intent
    verdict = detect_strategy_intent(text)
    verdict['text'] = text
    # R2（设计文档 §5.2.2）：词表判不了、又可能是策略的两档交 LLM 兜底裁决。
    #   判策略 → is_strategy=True、disambiguate=False、confidence=max(confidence, 0.6)、reason='llm_adjudicated'
    #   判提问 → 直接放行走报告管线（LLM 已替用户答了，不再弹分流题）
    #   失败 / 超时 / 开关关闭 → 原样返回（下面 disambiguate 分支照常回退分流题）
    verdict = await _maybe_llm_adjudicate(verdict, text, logger)
    if verdict.get('disambiguate'):
        return _DISAMBIGUATE_TEXT
    if not verdict.get('is_strategy'):
        return None
    if verdict.get('confidence', 0) < 0.5:
        return None

    # ③ 开会话（槽位按推荐值补全，但**一行回测都不跑**）
    slots = _resolve_slots(verdict)
    # reason='llm_adjudicated' 由 R2（任务 11）写入；本步骤先支持这个取值，任务 11 无需回改此处
    opened_by = 'explicit' if verdict['reason'] == 'explicit_imperative' else (
        'llm' if verdict['reason'] == 'llm_adjudicated' else 'rule')
    cols = _slot_columns(slots)
    session = create_strategy_session(
        'ss-' + uuid4().hex[:12], dlg, text, idea=text, opened_by=opened_by,
        intent_confidence=verdict.get('confidence', 0), intent_signals=verdict.get('signals'),
        slots=cols,
    )
    update_strategy_session(session['session_uuid'],
                            slots_json=json.dumps(slots, ensure_ascii=False))
    session = get_open_strategy_session(dlg) or session
    _log(logger, '[策略闸门] 命中 ' + verdict['reason'] + ' → 会话 ' + session['session_uuid'])

    # 新会话这一轮本身也是一次「策略执行」：建会话 / 确认卡都发生在这里。
    # 之前只有「委派给 run_session_turn」那条路会建 run —— 于是**用户的第一条
    # 策略消息**（如「尾盘买入第二天卖出这个策略如何」）在月历与执行过程里完全
    # 不存在。这里先建 run 并写回 session：后面若委派执行器，runner 见到
    # trace_run_id 会沿用同一条，不会出现两条记录。
    from agents.shared.strategy_trace import strategy_turn_scope, publish_turn_run
    # ⚠️ 埋点是可观测性，不是业务依赖：trace 建不起来就退化成空壳 scope（step 静默
    # 降级），绝不能让「第一句策略」因为统计组件挂掉而跑不动。
    _scope = None
    _scope_cm = None
    try:
        _scope_cm = strategy_turn_scope(session, text)
        _scope = _scope_cm.__enter__()
    except Exception:
        _scope_cm = None
        _scope = None
        _log(logger, '[策略闸门] trace 作用域创建失败，本轮无埋点', 'warning')
    try:
        return await _finish_new_session(
            session=session, verdict=verdict, slots=slots, text=text, dlg=dlg,
            mode_cfg=mode_cfg, logger=logger, scope=_scope,
            progress_callback=progress_callback, cancel_check=cancel_check)
    finally:
        # 必须在作用域退出前发布：agent_runner 靠它把本轮消息绑定到这条 run
        if _scope is not None and getattr(_scope, 'run_id', ''):
            publish_turn_run(str(_scope.run_id))
        if _scope_cm is not None:
            try:
                _scope_cm.__exit__(*sys.exc_info())
            except Exception:
                pass


def _maybe_set_pool_sweeps(slots: Dict[str, Any], verdict: Dict[str, Any]) -> None:
    """范围确认成池子后，按原话补上「卖出口径对比」与「持有期矩阵」标记。"""
    try:
        from agents.shared.strategy_intent import parse_compare_timing, parse_hold_periods
        text = str((verdict or {}).get('text') or '')
        uni = str((slots.get('universe') or {}).get('value') or '').upper()
        if not uni.startswith('POOL'):
            return
        if parse_compare_timing(text):
            slots.setdefault('compare_exit_timings', {'value': True, 'source': 'user'})
        holds = parse_hold_periods(text)
        if len(holds) >= 2:
            slots.setdefault('hold_period_sweep', {'value': holds, 'source': 'user'})
    except Exception:  # noqa: BLE001
        pass


async def _finish_new_session(*, session: Dict[str, Any], verdict: Dict[str, Any],
                              slots: Dict[str, Any], text: str, dlg: str,
                              mode_cfg: str, logger: Any, scope: Any,
                              progress_callback: Optional[Callable],
                              cancel_check: Optional[Callable]) -> str:
    """新会话这一轮的收尾：补槽位 → 确认卡 → 委派执行器。

    整段跑在调用方建的 strategy_turn_scope 里（trace 建不起来时 scope=None，
    step 静默降级，业务照跑）。
    """
    from utils.cache.backtest_db import (get_open_strategy_session,
                                         update_strategy_session)
    if scope is not None:
        with scope.step('建会话', {'reason': verdict.get('reason'),
                                  'confidence': verdict.get('confidence')}) as _step:
            _step.set_output({'session_uuid': session.get('session_uuid'),
                              'slots': slots})

    # auto 模式：槽位齐全才放行直接跑；缺关键槽位一律先问
    missing = [k for k, v in slots.items() if isinstance(v, dict)
               and v.get('source') == 'missing' and k in ('universe',)]
    if mode_cfg == 'auto' and not missing:
        update_strategy_session(session['session_uuid'], status='active')
        session = get_open_strategy_session(dlg) or session

    if session['status'] == 'pending_confirm':
        answers = _ask_via_channel(await _slot_questions(verdict, slots, logger=logger), text, logger)
        if answers == 'rejected':
            from utils.cancellation import AgentCancelled
            raise AgentCancelled('用户已取消策略会话')
        if answers is None:
            # 无交互通道（纯文本 CLI）：仍回卡，等用户回「开始」
            return build_confirm_text(session)
        timed_out = answers == 'timeout'
        if timed_out:
            # 弹窗超时：按已解析的默认口径**直接开跑**，不再要求第三次「开始」
            # （用户 2026-10-25 明确：提问 → 确认反问，两次就够）。
            update_strategy_session(session['session_uuid'], status='active')
        else:
            raw_uni = str((answers or {}).get('universe') or '').strip() \
                if isinstance(answers, dict) else ''
            if raw_uni and not _normalize_universe_value(raw_uni):
                # 范围解析不出来就**别开跑**：写进 universe 列会让单票入口报
                # 「标的需为 6 位代码」（真机 2026-10-25），而且会话会卡在 active 反复报错。
                return '\n'.join([
                    '标的范围「' + raw_uni[:60] + '」我没解析出来，先不开跑。',
                    '直接说范围就行：沪深300 / 中证500 / 我的自选 / 我的持仓 / 医药板块，'
                    '或一个 6 位代码。',
                    '',
                    build_confirm_text(session),
                ])
            merged = _apply_answers(slots, answers)
            # 范围落成池子后，补上「开盘/收盘对比」与「持有期矩阵」标记
            _maybe_set_pool_sweeps(merged, verdict)
            cols = _slot_columns(merged)
            update_strategy_session(session['session_uuid'], status='active',
                                    slots_json=json.dumps(merged, ensure_ascii=False),
                                    **cols)
        session = get_open_strategy_session(dlg) or session
        from agents.shared.strategy_session_runner import run_session_turn
        # ⚠️ 起跑口令必须是**固定口令「开始」**，不能把用户原话再交给轮内路由：
        # 槽位已经由这张卡确认过了，而用户原话（一段长策略描述）会被 LLM 路由判成
        # amend → 又追加成「补充条件」并重发确认卡，逼用户回第三次「开始」
        # （真机 2026-10-25 复现：答完弹窗仍收到「回一句开始我就跑」）。
        # 「开始」在 strategy_turn_router.FAST_PATH 里，零 LLM 调用。
        started = await run_session_turn(
            session=session, user_input='开始', dialog_uuid=dlg,
            progress_callback=progress_callback, logger=logger,
            cancel_check=cancel_check, channel='card')
        if not timed_out:
            return started
        return '\n'.join([
            '确认卡没等到你点选，我已按下面这套开始（不用再回「开始」）。',
            '', build_confirm_text(session, auto_started=True), '', started])

    # auto 模式（上面已置 active）：同样用固定口令开跑，不把原话再路由一遍
    from agents.shared.strategy_session_runner import run_session_turn
    return await run_session_turn(session=session, user_input='开始', dialog_uuid=dlg,
                                  progress_callback=progress_callback, logger=logger,
                                  cancel_check=cancel_check, channel='card')


_LLM_ADJUDICATE_PROMPT = """你在判断用户这句话是不是「想验证一个交易策略」，而不是「想了解某个指标或概念」。

判策略：提出了可回测的买卖规则（哪怕只说了一半），或明确要求验证 / 回测 / 看胜率。
判提问：只是问概念、问原理、问某指标是什么、问行情 / 个股 / 新闻。

只输出 JSON，不要解释：
{"is_strategy": true 或 false, "reason": "一句话理由（中文，不超过 30 字）"}

用户原话："""


def should_llm_adjudicate(verdict: dict) -> bool:
    """只有「词表判不了、又可能是策略」的两档才调 LLM（主流量零成本）。

    基准带 = disambiguate（只命中一侧信号）；可疑带 = 零命中但含买卖动作词。
    纯函数，便于单测（设计文档 §5.2.2）。
    """
    from config import Config

    if not bool(getattr(Config, "STRATEGY_INTENT_LLM_FALLBACK", True)):
        return False
    if verdict.get("is_strategy"):
        return False  # 已经确定，不浪费调用
    if verdict.get("disambiguate"):
        return True  # 基准带
    sig = verdict.get("signals") or {}
    return bool(sig.get("entry_words") or sig.get("exit_words"))  # 可疑带


async def _maybe_llm_adjudicate(verdict: dict, text: str, logger=None) -> dict:
    """LLM 兜底裁决。返回**新的 verdict**；判不了就原样返回（调用方回退分流题）。

    失败 / 超时 / 开关关闭 → 原样返回：**绝不**因为 LLM 挂掉就把策略轮放走。
    输出契约：只取 {is_strategy, reason}，**不抽槽位**（槽位归澄清卡，见设计文档 §5.2.2）。
    """
    if not should_llm_adjudicate(verdict):
        return verdict
    try:
        from utils.llm_factory import get_llm, llm_json_with_retry

        llm = get_llm()
        data = await asyncio.to_thread(
            llm_json_with_retry, llm,
            [{"role": "user", "content": _LLM_ADJUDICATE_PROMPT + (text or "")[:500]}],
            logger=logger, label="strategy-intent-route", max_retries=2,
        )
    except Exception as e:  # 网络 / 配置 / 超时都走这里
        if logger:
            logger.warning("策略意图 LLM 兜底失败，回退分流题：%s", e)
        return verdict
    if not isinstance(data, dict) or not isinstance(data.get("is_strategy"), bool):
        return verdict
    out = dict(verdict)
    out["llm_reason"] = str(data.get("reason") or "")[:80]
    if data["is_strategy"]:
        out.update({"is_strategy": True, "disambiguate": False,
                    "confidence": max(float(verdict.get("confidence") or 0.0), 0.6),
                    "reason": "llm_adjudicated"})
        if logger:
            logger.info("策略意图 LLM 兜底：判策略（%s）", out["llm_reason"])
    else:
        # 判提问：不进会话，也不再弹分流题（LLM 已替用户答了）
        out.update({"is_strategy": False, "disambiguate": False, "reason": "llm_rejected"})
    return out
