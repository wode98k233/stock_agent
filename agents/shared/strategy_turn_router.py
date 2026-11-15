"""策略会话**轮内路由**：判断「用户这句话在策略会话的哪个阶段想干什么」。

为什么单独一个文件（设计文档 §5.1.3）：
`strategy_intent.py` 解决的是「这句话**是不是**策略研究」（正则 + LLM 兜底，见
`_maybe_llm_adjudicate`）；本模块解决的是「**已经进了策略会话**，这一句想干什么」。
两个问题层级不同、判据不同，共享词表会让两边的误判互相污染，故独立成文件。

⛔ 本模块**不以正则为主**。曾经的实现把阶段判定写成关键词命中
（CONFIRM_WORDS / AMEND_WORDS / CLOSE_WORDS / RERUN_WORDS + 问号句式），
结果是：用户换个说法（"那就试试呗" / "算了不弄了" / "先缓一缓"）全部判错，
且新词只能靠继续堆，误判面只会越来越大。用户明确指出这一点：
*「正则 + LLM 兜底」只适用于「是不是策略研究」这一层；
进入策略会话后，「现在处于什么阶段」应该让 LLM 自己判断。*

所以分工是：
- **正则只保留一个极小的固定口令闭集**（fast path）—— 只有「开始 / 开跑 / 结束会话」
  这类用户会逐字照抄卡片按钮的短语才走这里，省掉一次 LLM 调用。
  闭集之外一律 LLM，不做「看起来像」的模糊匹配。
- **LLM 出阶段意图**，取值域封闭（TURN_INTENTS）。
- **LLM 失败/超时 → 保守兜底**：不猜用户意图，回到该阶段的**默认动作**
  并明确告知「没听懂，回一句…」。宁可多问一次，也不猜错后跑错策略。

⚠️ 失败兜底不许退化成「返回 None 当普通提问处理」：会话已开着，悄悄放行会让用户
以为切回了分析模式，实际下一句又被会话接走 —— 这正是「截胡」问题的另一种形态。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, Optional

# 轮内意图取值域（封闭；LLM 只能从中选，非法值按 unknown 处理）
TURN_CONFIRM = 'confirm'        # 确认按已给的槽位开跑
TURN_AMEND = 'amend'            # 改条件（买点/卖点/区间/口径）
TURN_RERUN = 'rerun'            # 用同样条件重跑一次
TURN_CLOSE = 'close'            # 落地/结束会话/中止
TURN_PROGRESS = 'progress'      # 问跑到哪了
TURN_RESULT = 'result'          # 要结果
TURN_SUMMARY = 'summary'        # 要研发过程/迭代史
TURN_UNRELATED = 'unrelated'    # 与策略会话无关（要回到普通分析）

TURN_INTENTS = (
    TURN_CONFIRM, TURN_AMEND, TURN_RERUN, TURN_CLOSE,
    TURN_PROGRESS, TURN_RESULT, TURN_SUMMARY, TURN_UNRELATED,
)

# 固定口令闭集（唯一允许走正则的地方）。
# 收录标准：必须是卡片按钮/提示语里**逐字出现**的短语，用户照抄才成立。
# 刻意不含「跑」「行」「可以」等短词 ——它们在正常对话里太常见，误判代价高。
FAST_PATH = {
    '开始': TURN_CONFIRM,
    '开跑': TURN_CONFIRM,
    '确认': TURN_CONFIRM,
    '开始吧': TURN_CONFIRM,
    '重跑': TURN_RERUN,
    '重跑一次': TURN_RERUN,
    '结束会话': TURN_CLOSE,
    '落地': TURN_CLOSE,
    '跑到哪了': TURN_PROGRESS,
}

# 成交口径封闭枚举。刻意内联而不 import ``backtest.strategy_base``：
# 路由层是「会话还在不在线」的判定入口，不该把回测引擎拖进来（import 链一长，
# 首屏就会被拖慢；且 enum 变了这里必须一起看，复制一份更显眼）。
# 口径猜错 = 回测结论整体失真，所以非法值一律 None，交给调用方按买侧处理。
TIMING_CHOICES = ('same_close', 'next_open', 'next_close')

# 阶段默认动作：LLM 判不出来时回到这里（按会话状态，不按词）
_DEFAULT_BY_STATUS = {
    'pending_confirm': (TURN_CONFIRM, '要开跑回一句「开始」；要改条件就直接说。'),
    'active': (TURN_PROGRESS, '要开跑/重跑就说「开始」或「重跑」；要收尾就说「结束会话」。'),
    'archived': (TURN_UNRELATED, '这个会话已经收尾了，重新说一遍你的想法我重开一个。'),
}

_ROUTE_PROMPT = """你在判断一个「策略回测」对话里用户这句话的意图。

当前日期：{today}
当前会话状态：{status_desc}
已确认的条件：{slots_desc}
研发任务：{run_desc}

可选意图（只能选一个）：
- confirm：确认按上面已给的条件开跑
- amend：想改条件（买点、卖点、标的、区间、成交口径），或补充新条件
- rerun：用同样条件再跑一次
- close：落地、结束会话、中止这次研发
- progress：问现在跑到哪了、还在跑吗
- result：要最终结果/结论
- summary：要研发过程、迭代史、每一轮假设
- unrelated：这句话跟这个策略会话没关系，是想聊别的（行情、个股、宏观、其他分析）

判断要点：
- 用户表达的是新想法、新条件、新标的、新区间 → amend（哪怕用了「改成/加上/而且」以外的说法）
- 用户婉拒、说不试了、先缓缓、算了 → close
- 用户问的这句完全是在聊别的（大盘、某只票、新闻） → unrelated
- 不要因为句子短就判unrelated；短句常常就是「开始」「重跑」这类口令
- 顺带抽取成交口径（只在用户明确说了买卖时点时才填，拿不准就填 null，不要猜）：
  same_close = 当天尾盘/收盘价成交（「尾盘买」「按收盘价」「当天收盘」）
  next_open  = 次日/隔天开盘成交（「次日开盘卖」「隔天开盘」）
  next_close = 次日/隔天收盘成交（「次日收盘卖出」）
- timing_side：这次改的是哪一侧的口径 —— entry（买入口径）/ exit（卖出口径）。
  「尾盘买入、次日卖出」是两个独立字段；只改卖点时点必须填 exit，否则会把买入口径改掉。
  判断不了填 null（调用方按买侧处理）。
- timing_only：这一句**只是在改成交口径**，没有加入或改动任何买卖条件。
  例：「换个成交口径再跑一次」「改成尾盘买」→ true；「换成 MACD 金叉」→ false。
  这个标用于区分「只翻口径」与「改了条件要重开确认」，避免把口径改动混进条件备注里。
- compare_exit：用户要求**对比两种卖出口径**（开盘卖 vs 收盘卖、「两种卖法都试试」）→ true；
  只指定其中一种 → false。
- 顺带抽取这一句**明确给出的结构化条件**（没提到就填 null，绝不猜；不确定也填 null）：
  period_start / period_end：日期统一写成 YYYY-MM-DD。例：「2024年9月开始」→ period_start
    "2024-09-01"；「结束时间到现在 / 至今」→ period_end 填今天 {today}；只说了年份 → 该年 01-01；
    「近 N 年 / 近几年」→ 按今天往前推 N 年换算成起止日期（终点填今天）。
  universe：只有用户明确给了**一个** 6 位代码时才填；单票以外的范围一律填 null。
  universe_scope：用户明确说出的**标的范围**（没说就 null）：
    {{"source":"default"}}                                 默认池
    {{"source":"index","target":"000300|000905|沪深300|中证500"}}
    {{"source":"watchlist"}} / {{"source":"portfolio"}} / {{"source":"union"}}
    {{"source":"mixed"}}                                   个股 + ETF 混合（沪深300 + ETF）
    {{"source":"board","target":"医药"}}                   行业/概念板块名
    {{"source":"codes","target":"600519,510300"}}          点名 >=2 个代码
    可附带 "limit": 数字（只跑/抽 N 只）、"kind": "stock"|"etf"|"all"。

只输出 JSON，不要解释：
{{"intent": "<上述之一>", "reason": "一句话理由（中文，不超过 25 字）",
 "entry_timing": "same_close | next_open | next_close | null",
 "timing_side": "entry | exit | null", "timing_only": true | false,
 "period_start": "YYYY-MM-DD | null", "period_end": "YYYY-MM-DD | null",
 "universe": "6位代码 | POOL:default | null",
 "universe_scope": {{...}} | null,
 "compare_exit": true | false}}

用户原话：{text}"""


def _norm(text: str) -> str:
    return (text or '').strip().replace(' ', '')


_DATE_FMT = '%Y-%m-%d'
_NULLISH = ('null', 'none', '')


def _parse_date(value: Any) -> Optional[str]:
    """把 LLM 给的日期收敛成 YYYY-MM-DD；非法/空一律 None（不许猜）。"""
    text = str(value or '').strip()
    if text.lower() in _NULLISH:
        return None
    try:
        return datetime.strptime(text, _DATE_FMT).date().isoformat()
    except ValueError:
        return None


def _parse_universe(value: Any) -> Optional[str]:
    """标的：只认 6 位代码与 POOL 系列；其它一律 None（标的一改，结论整体换标的）。"""
    text = str(value or '').strip()
    if text.lower() in _NULLISH:
        return None
    upper = text.upper()
    if upper.startswith('POOL'):
        return upper if upper.startswith('POOL:') else 'POOL:default'
    return text if re.fullmatch(r'\d{6}', text) else None


def _parse_universe_scope(value: Any) -> Optional[str]:
    """把 LLM 抽到的范围对象收敛成规范 spec；任何字段不合法 → None（不猜）。"""
    if not isinstance(value, dict):
        return None
    source = str(value.get('source') or '').strip().lower()
    if source not in ('default', 'smoke', 'index', 'watchlist', 'portfolio',
                      'union', 'mixed', 'board', 'codes'):
        return None
    target = str(value.get('target') or '').strip()
    limit = value.get('limit')
    try:
        limit_n = int(limit) if limit not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        limit_n = None
    if limit_n is not None and not (1 <= limit_n <= 5000):
        limit_n = None
    kind = str(value.get('kind') or 'all').strip().lower()
    if kind not in ('all', 'stock', 'etf'):
        kind = 'all'
    if source == 'codes':
        codes = list(dict.fromkeys(re.findall(r'(?<!\d)(\d{6})(?!\d)', target)))
        if len(codes) < 2:          # 单票走 universe，不走范围
            return None
        target = ','.join(codes)
    if source in ('index', 'board') and not target:
        return None
    try:
        from backtest.pool_scope import build_spec
        return build_spec(source, target, limit=limit_n, kind=kind)
    except Exception:  # noqa: BLE001 - 非法范围一律丢弃
        return None


def _session_desc(session: Dict[str, Any]) -> Dict[str, Any]:
    """把会话行压成给 LLM 的最小上下文（只给判阶段必需的字段，别把整行塞进 prompt）。"""
    status = str(session.get('status') or '')
    run_uuid = str(session.get('run_uuid') or '')
    label = {'pending_confirm': '已开会话，**还没开跑**，正等用户确认或改条件',
             'active': '会话进行中',
             'archived': '已归档'}.get(status, status or '未知')
    if status == 'active' and run_uuid:
        label = '会话进行中，研发任务 ' + run_uuid + ' 的状态见下方任务描述'
    return {'status': status or '', 'status_desc': label, 'run_uuid': run_uuid,
            'slots': session.get('slots_json') or '{}',
            'turn_count': int(session.get('turn_count') or 0)}


def _slots_desc(slots_json: str) -> str:
    try:
        slots = json.loads(slots_json or '{}')
    except (ValueError, TypeError):
        return '（未记录）'
    if not isinstance(slots, dict):
        return '（未记录）'
    out = []
    for key in ('universe', 'period', 'entry', 'exit', 'entry_timing', 'exit_timing'):
        item = slots.get(key)
        val = item.get('value') if isinstance(item, dict) else item
        if isinstance(val, list):
            val = ' → '.join(str(x) for x in val)
        if val:
            out.append(key + '=' + str(val))
    return '；'.join(out) or '（未记录）'


def _run_desc(run: Optional[Dict[str, Any]], run_uuid: str = '') -> str:
    if not run:
        # 池子任务不在 strategy_run 表里，_load_run 必然查不到；若直接写「还没起跑」
        # 会与会话状态「进行中」自相矛盾，把「还在跑吗」这类句子带偏。
        if str(run_uuid or '').startswith('PR-'):
            return run_uuid + '：池子验证任务（后台线程跑，进度问「跑到哪了」）'
        return '（还没起跑）'
    st = str(run.get('status') or '')
    label = {'running': '正在跑（后台线程，页面有进度条）',
             'done': '已跑完',
             'failed': '已失败',
             'cancelled': '已中止'}.get(st, st or '状态未知')
    return run.get('run_uuid', '') + '：' + label


def fallback_route(session: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 判不出来时的保守兜底：回到该阶段的默认动作，并要求用户明确表态。

    不猜用户意图（猜错= 跑错策略 / 白跑一次），也不把会话悄悄放行成普通提问。
    """
    status = str(session.get('status') or '')
    intent, hint = _DEFAULT_BY_STATUS.get(status, (TURN_UNRELATED, '要继续这个策略会话吗？'))
    if status == 'active' and str(session.get('run_uuid') or ''):
        # 有任务在跑时，默认动作是给进度而不是催确认
        from agents.shared.strategy_session_runner import _get_run
        st = str((_get_run(str(session['run_uuid'])) or {}).get('status') or '')
        if st in ('pending', 'queued', 'running'):
            intent = TURN_PROGRESS
            hint = '还在跑，页面进度条会自己走完。'
    return {'intent': intent, 'source': 'fallback', 'confidence': 0.0,
            'llm_reason': '', 'hint': hint, 'entry_timing': None,
            'timing_side': None, 'timing_only': False,
            'period_start': None, 'period_end': None, 'universe': None,
            'universe_spec': None, 'compare_exit': False}


def fast_path_intent(text: str) -> Optional[str]:
    """固定口令闭集命中才返回意图，否则 None（交给 LLM）。"""
    return FAST_PATH.get(_norm(text))


async def route_session_turn(session: Dict[str, Any], user_input: str,
                              logger: Any = None) -> Dict[str, Any]:
    """轮内路由主入口。返回 {intent, source, confidence, llm_reason, hint}。

    intent ∈ TURN_INTENTS ∪ {unknown}；unknown 时调用方必须按hint 追问，不要猜。
    """
    text = (user_input or '').strip()
    if not text:
        fb = fallback_route(session)
        fb['intent'] = 'unknown'
        return fb

    hit = fast_path_intent(text)
    if hit:
        return {'intent': hit, 'source': 'fast_path', 'confidence': 1.0,
                'llm_reason': '固定口令', 'hint': '', 'entry_timing': None, 'timing_side': None,
                'timing_only': False, 'period_start': None, 'period_end': None,
                'universe': None, 'universe_spec': None, 'compare_exit': False}

    # 开关关闭时也走兜底（保留「纯规则模式」用于离线测试与成本敏感场景）
    try:
        from config import Config
        if not bool(getattr(Config, 'STRATEGY_TURN_ROUTER_LLM', True)):
            fb = fallback_route(session)
            fb['source'] = 'fallback_rule_only'
            return fb
    except Exception:
        pass

    ctx = _session_desc(session or {})
    today = datetime.now().date().isoformat()
    try:
        from utils.llm_factory import get_llm, llm_json_with_retry
        llm = get_llm()
        prompt = _ROUTE_PROMPT.format(
            today=today,
            status_desc=ctx['status_desc'],
            slots_desc=_slots_desc(ctx['slots']),
            run_desc=_run_desc(_load_run(ctx['run_uuid']), ctx['run_uuid']),
            text=text[:500],
        )
        data = await asyncio.to_thread(
            llm_json_with_retry, llm,
            [{"role": "user", "content": prompt}],
            logger=logger, label="strategy-turn-route", max_retries=2,
        )
    except Exception as e:   # 网络/配置/超时全走这里
        if logger:
            logger.warning("策略轮内路由 LLM 失败，回退阶段默认动作：%s", e)
        return fallback_route(session)

    if not isinstance(data, dict):
        return fallback_route(session)
    intent = str(data.get('intent') or '').strip().lower()
    if intent not in TURN_INTENTS:
        if logger:
            logger.warning("策略轮内路由返回非法意图 %r，回退阶段默认动作", intent)
        return fallback_route(session)
    # 成交口径：封闭枚举，拿不准就是 None（不许猜 —— 口径猜错= 回测结论整体失真）
    timing = str(data.get('entry_timing') or '').strip().lower()
    if timing not in TIMING_CHOICES:
        timing = None
    # 改的是买侧还是卖侧：拿不准就None，调用方按买侧处理（保持历史行为）
    side = str(data.get('timing_side') or '').strip().lower()
    if side not in ('entry', 'exit'):
        side = None
    # 区间 / 标的：只收用户明确给出的，非法值直接丢弃（改错标的比不改更糟）
    period_start = _parse_date(data.get('period_start'))
    period_end = _parse_date(data.get('period_end'))
    if period_start and period_end and period_start > period_end:
        if logger:
            logger.warning("策略轮内路由的区间起止颠倒（%s > %s），已丢弃",
                           period_start, period_end)
        period_start = period_end = None
    universe = _parse_universe(data.get('universe'))
    universe_spec = _parse_universe_scope(data.get('universe_scope'))
    return {'intent': intent, 'source': 'llm', 'confidence': 0.8,
            'llm_reason': str(data.get('reason') or '')[:80], 'hint': '',
            'entry_timing': timing, 'timing_side': side,
            'timing_only': bool(data.get('timing_only')),
            'period_start': period_start, 'period_end': period_end,
            'universe': universe, 'universe_spec': universe_spec,
            'compare_exit': bool(data.get('compare_exit'))}


def _load_run(run_uuid: str) -> Optional[Dict[str, Any]]:
    if not run_uuid:
        return None
    try:
        from agents.shared.strategy_session_runner import _get_run
        return _get_run(run_uuid)
    except Exception:
        return None