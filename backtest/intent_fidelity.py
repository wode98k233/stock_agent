"""意图保真闸门：生成的策略**是不是用户说的那个策略**。

为什么必须有它（2026-10-14 用户质询）：想法是
「收盘价创 20 日新高且成交量大于 20 日均量的 1.5 倍时买入，持有 5 根后卖出」，
8 轮生成结果里**没有任何一轮**是「20 日新高」（用的是布林上轨 / 高点均线），
**也没有任何一轮**带 1.5 倍门槛。整个评判只回答「赚不赚钱」，没人回答「是不是这个策略」。

本模块做两件事，都不碰业务：
1. describe_conditions()：把编译出的规则树翻成人话条件清单（**确定性**，不用 LLM）；
2. check_intent()：把「用户原话 + 槽位」与「实际条件清单」交给 LLM 逐条核对，
   输出 {matches, deviations}。失败/关闭开关 → checked=False（**fail-open**：
   核对不了不能拦住研发，但绝不允许静默当作通过）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FIDELITY_PROMPT = """你在核对「生成的策略」是否忠实实现了「用户的想法」。

判「一致」：核心买卖条件与用户原话语义相同（换写法可以，例如「创 20 日新高」写成
highest(high,20) 算一致；写成布林上轨 **不算**）。
判「偏离」：条件被替换成别的指标、阈值被去掉或放宽、买卖方向/口径被改、加了用户没提的核心约束。

只输出 JSON，不要解释：
{"matches": true 或 false, "deviations": ["一句话说明偏离点", "..."], "summary": "一句话结论"}

用户的想法：
%s

用户确认的买点/卖点：
买点=%s；卖点=%s

实际生成的条件：
%s
"""


def _describe_operand(node: Any) -> str:
    """操作数 → 人话（data/func/value/param/mul/嵌套 value 都覆盖）"""
    if not isinstance(node, dict):
        return str(node)
    if str(node.get('func') or '') == 'mul':
        args = node.get('args') or []
        inner = _describe_operand(args[0]) if args else '?'
        factor = args[1] if len(args) > 1 else '?'
        return inner + ' × ' + str(factor)
    if 'data' in node:
        text = '数据:' + str(node['data'])
        if node.get('shift'):
            text += '[前 %s 根]' % node['shift']
        return text
    if 'func' in node:
        args = node.get('args') or []
        inner = ', '.join(_describe_operand(a) for a in args)
        text = str(node['func']) + '(' + inner + ')'
        if node.get('field'):
            text += '.' + str(node['field'])
        return text
    if 'value' in node:
        return '常量 ' + json.dumps(node['value'], ensure_ascii=False)
    if 'param' in node:
        return '参数 ' + str(node['param'])
    if 'pattern' in node:
        return '形态 ' + str(node['pattern'])
    return json.dumps(node, ensure_ascii=False)[:60]


def describe_conditions(strategy_json: Any) -> List[str]:
    """把策略 JSON 的条件翻成一行一条的人话清单（确定性，无 LLM，可单测）"""
    data = strategy_json
    if isinstance(data, str):
        try:
            data = json.loads(data or '{}')
        except json.JSONDecodeError:
            return []
    if not isinstance(data, dict):
        return []
    out: List[str] = []
    conditions = data.get('conditions') or {}
    for side, label in (('buy', '买点'), ('sell', '卖点')):
        block = conditions.get(side) or {}
        logic = str(block.get('logic') or 'AND')
        rules = block.get('rules') or []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if str(rule.get('type') or '') == 'group':
                inner = rule.get('rules') or []
                parts = [_describe_operand(r.get('left')) + ' ' + str(r.get('op')) + ' '
                         + _describe_operand(r.get('right')) for r in inner if isinstance(r, dict)]
                out.append('%s(%s 组): %s' % (label, rule.get('logic') or 'AND', '；'.join(parts)))
                continue
            out.append('%s(%s): %s %s %s' % (label, logic, _describe_operand(rule.get('left')),
                                             rule.get('op'), _describe_operand(rule.get('right'))))
        if block.get('hold_bars'):
            out.append('%s: 持有 %s 根后卖出' % (label, block['hold_bars']))
    return out


def check_intent(*, idea: str, entry_text: str = '', exit_text: str = '',
                 strategy_json: Any = '', logger: Any = None,
                 extra_context: str = '') -> Dict[str, Any]:
    """LLM 核对「生成条件 vs 用户原话」。

    ``extra_context``：引擎会在同一策略上**额外执行**的对比（持有期矩阵 / 卖出口径
    对比等）。这些由引擎参数实现，策略 JSON 里只有一个 hold_bars 是正常的 ——
    不告诉核对器，它会把「用户要求对比 3/4/5 天」误判成偏离（真机 2026-10-25）。

    返回 {checked, matches, deviations, summary}；
    checked=False 表示没核对（开关关闭 / LLM 失败 / 条件为空）——调用方必须把它当「未知」，
    不能当「一致」。
    """
    from config import Config

    if not bool(getattr(Config, 'STRATEGY_INTENT_FIDELITY', True)):
        return {'checked': False, 'reason': 'switch_off'}
    conditions = describe_conditions(strategy_json)
    if not conditions:
        return {'checked': False, 'reason': 'no_conditions'}
    idea = str(idea or '').strip()
    if not idea:
        return {'checked': False, 'reason': 'no_idea'}
    prompt = _FIDELITY_PROMPT % (idea, entry_text or '（未明确）', exit_text or '（未明确）',
                                 '\n'.join('- ' + c for c in conditions))
    extra = str(extra_context or '').strip()
    if extra:
        prompt += ('\n\n引擎额外执行的对比（由引擎参数实现，**不要算作偏离**）：\n'
                   + extra)
    try:
        from utils.llm_factory import get_llm, llm_json_with_retry

        llm = get_llm()
        data = llm_json_with_retry(llm, [{'role': 'user', 'content': prompt}],
                                   logger=logger, label='strategy-intent-fidelity',
                                   max_retries=2)
    except Exception as exc:                     # 网络/配置/超时都走这里
        if logger:
            try:
                logger.warning('意图核对失败（按未核对处理）：%s', exc)
            except Exception:
                pass
        return {'checked': False, 'reason': 'llm_error'}
    if not isinstance(data, dict) or not isinstance(data.get('matches'), bool):
        return {'checked': False, 'reason': 'bad_output'}
    deviations = [str(d) for d in (data.get('deviations') or []) if str(d).strip()]
    return {
        'checked': True,
        'matches': bool(data.get('matches')),
        'deviations': deviations[:5],
        'summary': str(data.get('summary') or ''),
        'conditions': conditions,
    }
