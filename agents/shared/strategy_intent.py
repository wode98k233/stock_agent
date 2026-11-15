"""策略意图判定（设计文档 §5.2）。

误判代价不对称：提问误判成策略只需用户点一次「只是问问」；
策略误判成提问则整个能力失效 ⇒ 宁可多问一句。

纯函数、无 IO（词表首读后缓存），任何入口都能调；返回结构可落库（体检/复盘用）。

词表口径（`strategy_intent_rules.json`，可运营调参不必改代码）：

- `imperatives` 是 **L1 显式祈使**：命中即认定策略意图（confidence=0.9），不需要条件词配合。
- `intent_words` × `condition_words` 必须**成对命中**才是 L2 判定（confidence=0.7）。只命中一侧 → 走**分流题**（一句便宜的反问），不是直接进也不是直接否。
- `timing_words` 是**口径词**：`尾盘/收盘价/按收盘 → same_close`（当日收盘价成交）、`第二天/次日/隔天/开盘 → next_open`（次日开盘成交，引擎既有默认）。**JSON 键序即优先级**，同时命中取先出现者。
- `recommended_days` 是**推荐值**（近 3 年，覆盖 2024 年初大跌），只在用户没给区间时用于补全，且必须标注 `source: "recommended"`。
- `question_markers` 是提问句式（是什么/为什么/怎么算…）：命中且无意图词 → 判提问而非策略；主流量（「茅台最近怎么样」）既不命中条件词也不命中动作词 ⇒ 不调 LLM 兜底。
- `entry_words` / `exit_words` 是买卖动作词，供 R2「可疑带」判据使用（零命中但含动作词 → 交 LLM 兜底），本身不参与 `is_strategy` 判定。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

_RULES_PATH = Path(__file__).with_name('strategy_intent_rules.json')
_RULES: Dict[str, Any] = {}
_LOADED = False          # 用哨兵而不是「dict 非空」判断，否则降级成 {} 后每轮都会重读盘

logger = logging.getLogger(__name__)

# 6 位股票/指数代码；两侧不许再连数字，避免命中金额/年份
_CODE_RE = re.compile(r'(?<![0-9])([0-9]{6})(?![0-9])')
_CLAUSE_SPLIT = re.compile(r'[，。；！？,;!?]')

# 「操作/择时/选股」问题 ≠ 「策略验证」。用户问「现在能不能买 / 明天买还是等回踩 /
# 什么区间 / 仓位止损」时，哪怕出现「买入/卖出/条件」也不能进策略会话——否则
# 一句投资建议会被拿去生成策略并池子回测（2026-11-02 真机）。
_ADVICE_RE = re.compile(
    r'是不是(?:好)?(?:时机|时候|买点)|现在(?:能|可以|该|要不要|是不是)(?:买|加仓|抄底)|'
    r'能不能买|可以买吗|该不该买|要不要买|明天(?:就)?买|等回踩|回踩(?:到|再)?(?:买|加)?|'
    r'什么区间|买入区间|仓位|止损|止盈|操作建议|选股|哪只(?:好|更)|目标价|支撑位|压力位|'
    r'反弹|抄底|加仓|减仓|'
    # 「怎么操作 / 怎么买 / 帮我选几只」同样是**问操作**，不是**要验证策略**：
    # 2026-11-02 真机「尾盘买入，第二天开盘卖出…怎么操作？」被判 single_sided 弹策略分流题。
    r'怎么操作|如何操作|怎么买|怎么卖|怎么选|帮我(?:选|挑)|选几[只支]|挑几[只支]'
)
# 明确要求「验证/回测」时，动作词再多也按策略处理（advice 守卫让路）。
_STRATEGY_RE = re.compile(
    r'回测|策略|验证|胜率|历史(?:收益|表现)|参数|买卖规则|跑一下|跑个|测试一下|模拟'
)


def _rules() -> Dict[str, Any]:
    """读词表（进程内缓存；改词表后调 reload_rules()）。

    文件缺失 → `FileNotFoundError`（显式失败，不静默）；
    文件损坏 / 结构非法 → 降级为无意图并记日志（先例 `agents/analysis/template_store.py:304-322`
    的 `_load_intent_rules`：损坏降级、校验在消费方；本模块自己就是消费方，所以就地消毒）。
    """
    global _LOADED
    if not _LOADED:
        with open(_RULES_PATH, 'r', encoding='utf-8') as f:
            try:
                raw = json.load(f)
            except ValueError as e:
                logger.warning('策略意图词表 JSON 损坏，已降级为无意图：%s', e)
                raw = {}
        errs = validate_rules(raw)
        if errs:
            logger.warning('策略意图词表非法，已降级为无意图：%s', '；'.join(errs))
            raw = {}
        _RULES.update(raw)
        _LOADED = True
    return _RULES


_REQUIRED_LISTS = ('imperatives', 'intent_words', 'condition_words',
                   'entry_words', 'exit_words', 'question_markers')
_REQUIRED_DICTS = ('timing_words', 'period_words')


def validate_rules(rules: Any) -> List[str]:
    """校验词表结构，返回中文错误清单（空 = 通过）。

    R3 的配置页保存前调它（非法 → 拒绝保存 + 返回错误清单，不写盘）；
    `_rules()` 也调它（非法 → 降级为无意图，不抛）。纯函数，便于单测。
    """
    if not isinstance(rules, dict):
        return ['词表根节点必须是 JSON 对象']
    errs: List[str] = []
    for key in _REQUIRED_LISTS:
        val = rules.get(key)
        if not isinstance(val, list) or not val or not all(isinstance(w, str) and w.strip() for w in val):
            errs.append(f'{key} 必须是非空的字符串数组')
    for key in _REQUIRED_DICTS:
        val = rules.get(key)
        if not isinstance(val, dict) or not val or not all(isinstance(k, str) and k.strip() for k in val):
            errs.append(f'{key} 必须是「非空字符串 → 值」的对象')
    timing = rules.get('timing_words')
    if isinstance(timing, dict):
        bad = [k for k, v in timing.items() if v not in ('same_close', 'next_open', 'next_close')]
        if bad:
            errs.append('timing_words 的值只能是 same_close / next_open / next_close，问题键：'
                        + '、'.join(bad))
    days = rules.get('recommended_days')
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        errs.append('recommended_days 必须是正整数')
    return errs


def reload_rules() -> Dict[str, Any]:
    """清缓存重读（测试与热调参用；R3 保存词表后调它，三处同时生效）"""
    global _LOADED
    _RULES.clear()
    _LOADED = False
    return _rules()


def _hits(low_text: str, words: List[str]) -> List[str]:
    return [w for w in (words or []) if w.lower() in low_text]


def _clause_with(text: str, words: List[str], limit: int = 40) -> str:
    """返回第一个含关键词的短句（截断），用于把「怎么买/怎么卖」原话留档"""
    for clause in _CLAUSE_SPLIT.split(text or ''):
        c = clause.strip()
        if c and any(w in c for w in (words or [])):
            return c[:limit]
    return ''


def _longest_matches(text: str, mapping: Dict[str, str]) -> List[Tuple[int, int, str]]:
    """扫描「词 → 值」，返回 [(起点, 长度, 值)]，同一位置只保留最长命中。

    词表里既有「买入」也有「买」，同一位置要取最长的那个，否则会被切成碎片
    （「尾盘买入」会被切成「尾盘」+「买」+「买入」，归属全乱）。
    """
    best: Dict[int, Tuple[int, str]] = {}
    for kw, val in (mapping or {}).items():
        if not kw:
            continue
        start = 0
        while True:
            i = text.find(kw, start)
            if i < 0:
                break
            start = i + 1
            cur = best.get(i)
            if cur is None or len(kw) > cur[0]:
                best[i] = (len(kw), val)
    return sorted((i, ln, v) for i, (ln, v) in best.items())


def _extract_timings(text: str, r: Dict[str, Any]) -> Dict[str, str]:
    """抽取买/卖**各自**的成交口径。

    ⛔ 旧实现只有一个 ``entry_timing``，且遍历 timing_words 命中第一个就 break：
    用户原话「尾盘买入，次日卖出」只能取到 ``same_close``，**卖点时点被整个丢掉**
    （引擎侧也只有一个全局口径，于是买卖被迫同口径）。引擎现已支持买卖分离，
    这里必须按「口径词在原话里的位置」归属到买侧或卖侧。

    归属规则（宁可留空交给澄清题，不猜）：
    - 先标出所有买卖动作词的位置（买/卖两侧），同位置取最长词；
    - 每个口径词归属于**它后面最近的**动作词 —— 中文里口径写在动作词前面是常态
      （「尾盘买入」「次日卖出」「次日收盘卖」）。若用「前面最近」，「次日卖出」的
      次日会被归到上文的「买入」上，卖点时点整个丢失（这正是要修的老 bug）；
    - 后面没有动作词时才回退到前面最近的（「买入尾盘」这种倒装）；
    - 两侧都没有 → 只当默认买点处理。
    """
    out = {'entry_timing': '', 'exit_timing': ''}
    text = text or ''
    entry_marks = _longest_matches(text, {w: 'entry' for w in (r.get('entry_words') or [])})
    exit_marks = _longest_matches(text, {w: 'exit' for w in (r.get('exit_words') or [])})
    actions: List[Tuple[int, str]] = sorted(
        [(i, s) for i, _ln, s in entry_marks + exit_marks], key=lambda x: x[0])
    timing_marks = _longest_matches(text, r.get('timing_words') or {})

    for pos, _ln, timing in timing_marks:
        after = [a for a in actions if a[0] >= pos]
        before = [a for a in actions if a[0] < pos]
        if after:
            side = after[0][1]
        elif before:
            side = before[-1][1]
        else:
            side = 'entry'
        key = side + '_timing'
        if not out[key]:
            out[key] = timing
    return out


_COMPARE_TIMING_RE = re.compile(
    # ``(?![^，。；,;]{0,3}买)``：第一个时点若紧跟着「买」，它就是买入口径，
    # 不是两种卖法。反例「尾盘买入、次日开盘卖出」曾被判成要对比（真机 2026-10-25
    # 因此白跑两遍）——它只是「收盘买、次日开盘卖」的一个口径。
    r'开盘(?:价)?(?![^，。；,;]{0,3}买)[^，。；,;]{0,8}(?:或者|或|和|与|/|、|跟)[^，。；,;]{0,8}(?:收盘(?:价)?|尾盘)'
    r'|(?:收盘(?:价)?|尾盘)(?![^，。；,;]{0,3}买)[^，。；,;]{0,8}(?:或者|或|和|与|/|、|跟)[^，。；,;]{0,8}开盘(?:价)?'
    r'|两种(?:卖法|卖出口径|卖点口径)'
    r'|开盘.{0,6}收盘.{0,6}(?:都|对比|比较)'
)


def parse_compare_timing(text: str) -> bool:
    """用户是否要求**对比开盘卖与收盘卖**（同一策略跑两遍）。

    只认封闭说法（「开盘或收盘」「两种卖法」「开盘收盘都对比」）。
    这是「范围/口径」的抽取，不参与阶段判定；判不了就 False，绝不猜。
    """
    return bool(_COMPARE_TIMING_RE.search(str(text or '')))


_HOLD_RANGE_RE = re.compile(r'持有\s*(\d{1,2})\s*[-~到至]\s*(\d{1,2})\s*(?:个)?(?:交易日|天|根)')
_HOLD_LIST_RE = re.compile(
    r'持有\s*((?:\d{1,2}\s*[、,和]\s*)+\d{1,2})\s*(?:个)?(?:交易日|天|根)')

# 绝对月份区间：2024年9月 / 2024-09 / 2024.09
_ABS_MONTH_RE = re.compile(r'(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*月?(?:份)?')
# 只有出现这些词才把绝对月份当「区间起点」，避免把「2024年9月有一次深V」当区间
_ABS_MONTH_HINTS = ('区间', '开始', '以来', '至今', '到现在', '改成', '改为')


def parse_hold_periods(text: str) -> List[int]:
    """用户要求对比多个持有期时返回 [3,4,5] 这类列表；只给一个/没说返回 []。

    只认「持有 3-5 个交易日」「持有 3、4、5 天」这类封闭写法；不猜。
    """
    raw = str(text or '')
    m = _HOLD_RANGE_RE.search(raw)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a < b <= 10:
            return list(range(a, b + 1))
    m = _HOLD_LIST_RE.search(raw)
    if m:
        nums = sorted({int(x) for x in re.findall(r'\d{1,2}', m.group(1))
                       if 1 <= int(x) <= 10})
        if len(nums) >= 2:
            return nums
    return []


def _extract_slots(text: str, low: str, r: Dict[str, Any]) -> Dict[str, Any]:
    slots: Dict[str, Any] = {
        'universe': '', 'period_start': '', 'period_end': '',
        'entry': '', 'exit': '', 'entry_timing': '', 'exit_timing': '',
    }
    m = _CODE_RE.search(text) if text else None
    if m:
        slots['universe'] = m.group(1)
    else:
        for kw, code in (r.get('universe_defaults') or {}).items():
            if kw in text:
                slots['universe'] = code
                slots['universe_source'] = 'keyword'
                break
    slots.update(_extract_timings(text, r))
    for kw, days in (r.get('period_words') or {}).items():
        if kw in low or kw in text:
            end = date.today()
            slots['period_start'] = (end - timedelta(days=int(days))).isoformat()
            slots['period_end'] = end.isoformat()
            break
    # 绝对月份区间（「区间改成 2024 年 9 月份开始，结束时间到现在」）。
    # 旧实现只认相对词（近N年），用户写具体月份时静默落成近 3 年 —— 真机 2026-10-25。
    if not slots['period_start'] and any(h in text for h in _ABS_MONTH_HINTS):
        m_abs = _ABS_MONTH_RE.search(text)
        if m_abs:
            year, month = int(m_abs.group(1)), int(m_abs.group(2))
            if 1 <= month <= 12:
                slots['period_start'] = f'{year}-{month:02d}-01'
                slots['period_end'] = date.today().isoformat()
    slots['entry'] = _clause_with(text, r.get('entry_words'))
    slots['exit'] = _clause_with(text, r.get('exit_words'))
    return slots


def detect_strategy_intent(user_input: str, rules: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """判定一句话是不是「要验证一个策略」。

    返回：
        {
          "is_strategy": bool,          # 是否进策略会话
          "disambiguate": bool,         # 只命中一侧信号 → 先问一句分流题
          "confidence": float,          # 0~1
          "reason": str,                # explicit_imperative / condition_intent_pair /
                                        # question_form / single_sided / empty
          "signals": {...},             # 可落库、可解释、可运营复盘
          "slots": {...},               # 只填能从原话解析出的槽位，不猜
        }
    """
    r = rules or _rules()
    text = (user_input or '').strip()
    if not text:
        return {'is_strategy': False, 'disambiguate': False, 'confidence': 0.0,
                'reason': 'empty', 'signals': {}, 'slots': {}}
    low = text.lower()
    signals = {
        'imperatives': _hits(low, r.get('imperatives')),
        'intent_words': _hits(low, r.get('intent_words')),
        'condition_words': _hits(low, r.get('condition_words')),
        'question_marker': bool(_hits(low, r.get('question_markers'))),
        # 买卖动作词（R2 的「可疑带」判据：零命中但含动作词 → 交 LLM 兜底）
        'entry_words': _hits(low, r.get('entry_words')),
        'exit_words': _hits(low, r.get('exit_words')),
    }
    slots = _extract_slots(text, low, r)
    signals['has_universe'] = bool(slots.get('universe'))
    signals['has_period'] = bool(slots.get('period_start'))
    signals['has_entry'] = bool(slots.get('entry'))
    signals['has_exit'] = bool(slots.get('exit'))
    signals['advice_words'] = [m.group(0) for m in _ADVICE_RE.finditer(text)]
    signals['strategy_explicit'] = bool(_STRATEGY_RE.search(text))

    # 先判「操作/择时/选股」：没有明确要求回测/策略/验证时，动作词再多也让路。
    # 这一步必须排在 imperatives / condition_intent_pair 之前，否则「明天买还是
    # 等回踩」会因为「买」被送进策略会话。
    if signals['advice_words'] and not signals['strategy_explicit']:
        return {
            'is_strategy': False,
            'disambiguate': False,
            'confidence': 0.8,
            'reason': 'advice_question',
            'signals': signals,
            'slots': slots,
        }

    has_intent = bool(signals['intent_words'])
    has_cond = bool(signals['condition_words'])

    if signals['imperatives']:
        is_strategy, confidence, reason = True, 0.9, 'explicit_imperative'
    elif has_cond and has_intent:
        is_strategy, confidence, reason = True, 0.7, 'condition_intent_pair'
    elif signals['question_marker'] and not has_intent:
        is_strategy, confidence, reason = False, 0.15, 'question_form'
    else:
        is_strategy, confidence, reason = False, 0.3, 'single_sided'

    # 只命中一侧（有指标没意图 / 有意途没指标）→ 先问一句分流题，别猜
    disambiguate = (not is_strategy) and (has_cond != has_intent)
    return {
        'is_strategy': is_strategy,
        'disambiguate': disambiguate,
        'confidence': confidence,
        'reason': reason,
        'signals': signals,
        'slots': slots,
    }
