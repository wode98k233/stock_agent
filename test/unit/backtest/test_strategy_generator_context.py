"""append-only 多轮上下文（prompt cache 命中的前提）。

2026-10-19 实测：该 provider 只认「同一前缀 + 尾部追加」——改 user 中段、改 system
一个字节、完全重发都是 0 命中。旧实现每轮从零重建 [system, user]，缓存命中率 31.2%
且全来自重试；同时历史里**从不带上一轮策略 JSON**，导致 8 轮里 4 轮在同一个编译错误
上反复（`rules[N].right.args[1] 必须是数值`）。

这里钉死三件事：
1. 第 2 轮消息列表的前 N 项与第 1 轮逐项相同（前缀不可变）；
2. system 只有一条且永远在 index 0；
3. 上一轮的结果与 universe 真的进了下一轮 prompt（不是空串、不是全量历史散文）。
"""
import json

import pytest
from langchain_core.messages import AIMessage

from backtest.strategy_research import IterRecord, StrategyGenerator


def _strategy(name='策略A'):
    return {
        'meta': {'name': name, 'description': 'MA 金叉', 'tags': []},
        'params': {'fast': {'value': 5, 'min': 2, 'max': 60, 'step': 1, 'label': '快', 'type': 'int'},
                   'slow': {'value': 20, 'min': 5, 'max': 200, 'step': 1, 'label': '慢', 'type': 'int'}},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_above', 'right': {'func': 'ma', 'args': ['close', '{slow}']}}]},
            'sell': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_below', 'right': {'func': 'ma', 'args': ['close', '{slow}']}}]},
        },
    }


def _reply(name='策略A'):
    return json.dumps({'hypothesis': f'假设-{name}', 'strategy': _strategy(name)},
                      ensure_ascii=False)


class FakeLLM:
    """记录每次请求的 messages/config；返回带 usage + 前缀指纹的 AIMessage。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
        self.model = 'fake-model'
        self._last = _reply()

    def invoke(self, messages, config=None):
        self.calls.append({
            'messages': [(m.type, m.content) for m in messages],
            'config': config,
        })
        content = self.replies.pop(0) if self.replies else self._last
        self._last = content
        return AIMessage(
            content=content,
            usage_metadata={
                'input_tokens': 100 + 10 * len(self.calls),
                'output_tokens': 20,
                'total_tokens': 120 + 10 * len(self.calls),
                'input_token_details': {'cache_read': 64},
            },
            response_metadata={
                'model_name': 'fake-model',
                'prefix_sha256': 'deadbeefcafe0001',
                'prefix_bytes': 4096,
            },
        )


def _gen(llm, **kw):
    kw.setdefault('pacing_seconds', 0)
    return StrategyGenerator(llm=llm, **kw)


def _iter(round_no, sharpe=1.2, trades=12):
    return IterRecord(
        round=round_no, hypothesis=f'第{round_no}轮假设', verdict='keep',
        is_metrics={'cagr': 0.12, 'sharpe': sharpe, 'trades': trades,
                    'health': 'ok', 'data_bars': 700, 'expected_bars': 720,
                    'coverage_pct': 0.97},
        oos_metrics={'cagr': 0.09, 'sharpe': round(sharpe * 0.8, 3),
                     'mdd': 0.11, 'trades': trades, 'health': 'ok'},
    )


# ============================================================
# 1. 前缀不可变（缓存命中的硬前提）
# ============================================================


def test_messages_are_append_only():
    llm = FakeLLM()
    gen = _gen(llm)
    gen.generate_initial('上升趋势尾盘买入', '600519')
    gen.mutate('上升趋势尾盘买入', '600519', [_iter(1)])

    first = llm.calls[0]['messages']
    second = llm.calls[1]['messages']

    assert len(second) > len(first), '第 2 轮必须比第 1 轮长（历史要真的追加进去）'
    for i, (a, b) in enumerate(zip(first, second)):
        assert a == b, f'第 {i} 条消息变了 —— 前缀不再稳定，缓存必然 0 命中'
    # 第 1 轮之后紧跟的那条是模型原文（assistant）
    assert second[len(first)][0] == 'ai'
    assert second[len(first)][1].strip().startswith('{')


def test_system_prompt_is_single_and_first():
    llm = FakeLLM()
    gen = _gen(llm)
    gen.generate_initial('想法', '600519')
    for n in range(1, 4):
        gen.mutate('想法', '600519', [_iter(i) for i in range(1, n + 1)])

    for idx, call in enumerate(llm.calls):
        roles = [role for role, _ in call['messages']]
        assert roles[0] == 'system', f'第 {idx + 1} 次调用 system 不在 index 0'
        assert roles.count('system') == 1, f'第 {idx + 1} 次调用有 {roles.count("system")} 条 system'
        # system 内容逐字相同（改一个字节都会打断前缀缓存）
        assert call['messages'][0][1] == llm.calls[0]['messages'][0][1]


# ============================================================
# 2. 上一轮结果 + universe 真的进了 prompt
# ============================================================


def test_mutate_prompt_carries_last_strategy_and_universe():
    llm = FakeLLM()
    gen = _gen(llm)
    gen.generate_initial('想法', '600519')
    gen.mutate('想法', '600519', [_iter(1, sharpe=1.7, trades=9)])

    prompt = llm.calls[1]['messages'][-1][1]
    assert '600519' in prompt, 'universe 不能是空串（旧 bug）'
    assert '第1轮' in prompt and '上一轮结果' in prompt
    assert 'sharpe=1.7' in prompt
    assert 'trades=9' in prompt
    assert '数据：700/720 根(97.0%)' in prompt
    assert '体检：正常' in prompt
    # 不再罗列全部历史（只有上一轮 + 变异要求）
    assert '历史迭代轮次' not in prompt


def test_two_rounds_grow_input_tokens():
    """第 2 轮输入显著变长 → 证明历史（含上一轮策略原文）真的进了上下文。"""
    llm = FakeLLM()
    gen = _gen(llm)
    gen.generate_initial('想法', '600519')
    gen.mutate('想法', '600519', [_iter(1)])

    size1 = sum(len(c) for _, c in llm.calls[0]['messages'])
    size2 = sum(len(c) for _, c in llm.calls[1]['messages'])
    assert size2 > size1 + 500, (size1, size2)


# ============================================================
# 3. 压缩：system 留在 0，最近两轮保留原文
# ============================================================


def test_compression_keeps_system_and_last_two_rounds():
    llm = FakeLLM()
    gen = _gen(llm)
    gen.MAX_CONTEXT_CHARS = 10          # 强制触发
    gen.generate_initial('想法', '600519')

    history = []
    for n in range(1, 5):
        history.append(_iter(n))
        gen.mutate('想法', '600519', history)

    msgs = llm.calls[-1]['messages']
    roles = [r for r, _ in msgs]
    assert roles[0] == 'system'
    assert roles.count('system') == 1

    joined = '\n'.join(c for _, c in msgs)
    assert '【前 1 轮摘要】' in joined, '第 1 轮应被压成摘要'
    # 最近两轮（第 3 轮的 prompt 提到第 2 轮；第 4 轮的 prompt 提到第 3 轮）保留原文
    assert '第2轮 假设' in joined or 'sharpe=1.2' in joined
    assert any('第3轮' in c for _, c in msgs)
    assert not any('历史迭代轮次' in c for _, c in msgs)
    # 只压一次
    assert gen._compressed is True


def test_compression_does_not_touch_system_bytes():
    llm = FakeLLM()
    gen = _gen(llm)
    gen.MAX_CONTEXT_CHARS = 10
    gen.generate_initial('想法', '600519')
    before = llm.calls[0]['messages'][0]
    for n in range(1, 4):
        gen.mutate('想法', '600519', [_iter(i) for i in range(1, n + 1)])
    assert llm.calls[-1]['messages'][0] == before


# ============================================================
# 4. trace / usage
# ============================================================


def test_llm_call_passes_trace_label():
    llm = FakeLLM()
    gen = _gen(llm)
    gen.generate_initial('想法', '600519')
    gen.mutate('想法', '600519', [_iter(1)])

    cfg1 = llm.calls[0]['config']
    assert cfg1['metadata']['purpose'] == 'strategy-generate'
    assert '第1轮' in cfg1['metadata']['label']
    assert '第2轮' in llm.calls[1]['config']['metadata']['label']


def test_retry_label_has_suffix():
    llm = FakeLLM(replies=['这不是 JSON', _reply()])
    gen = _gen(llm)
    gen.generate_initial('想法', '600519')
    assert len(llm.calls) == 2
    assert '重试1' in llm.calls[1]['config']['metadata']['label']


def test_usage_and_prefix_fingerprint_captured():
    llm = FakeLLM()
    gen = _gen(llm)
    gen.generate_initial('想法', '600519')
    meta = gen._usage_log[-1]
    assert meta['cached_tokens'] == 64
    assert meta['prefix_sha256'] == 'deadbeefcafe0001'
    assert meta['prefix_bytes'] == 4096
    assert meta['input_tokens'] > 0
