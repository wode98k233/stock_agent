"""进度卡的「阶段条 + 轮次时间线」字段（2026-10-17 对话内进度体验）。

背景：原型顶部是 stepper，对话里只显示「x/8 轮」看不出进行到哪一段。
设计见 docs/superpowers/specs/2026-10-17-strategy-chat-progress-ux-design.md §2.1。

这些断言锁的是**字段契约**——前端 chat-strategy-card.js 直接按字段名渲染，
字段改名/缺失会导致卡片静默退化成「只有进度条」。
"""
import json

from agents.shared.strategy_card import PROGRESS_PHASES, build_progress_card


def _iters(n: int):
    return [
        {
            'round': i + 1,
            'hypothesis': '假设' * 40,       # 80 字，用于验证截断
            'verdict': 'keep',
            'compile_ok': True,
            'overfit_flag': False,
            'oos_metrics': {'sharpe': 0.5, 'trades': 20},
        }
        for i in range(n)
    ]


def test_phase_mapping():
    cases = [
        ('running', 'running', 1),
        ('queued', 'running', 1),
        ('done', 'done', 2),
        ('failed', 'failed', 2),
        ('cancelled', 'failed', 2),
        ('error', 'failed', 2),
        ('pending_confirm', 'confirming', 0),
        ('confirming', 'confirming', 0),
    ]
    for status, phase, idx in cases:
        card = build_progress_card(run={'run_uuid': 'sr-1', 'status': status})
        assert card['phase'] == phase, status
        assert card['phase_index'] == idx, status
        assert card['phase_steps'] == PROGRESS_PHASES


def test_unknown_status_defaults_to_running():
    """未知状态不能退化成「结果」或「确认」，否则用户看到假的完成态。"""
    card = build_progress_card(run={'run_uuid': 'sr-1', 'status': 'weird'})
    assert card['phase'] == 'running'
    assert card['phase_index'] == 1


def test_rounds_timeline_fields():
    card = build_progress_card(run={'run_uuid': 'sr-1', 'status': 'running'},
                               iters=_iters(3))
    assert card['round_done'] == 3
    assert len(card['rounds']) == 3
    r = card['rounds'][0]
    assert r['round'] == 1
    assert r['oos_sharpe'] == 0.5
    assert r['oos_trades'] == 20
    assert r['compile_ok'] is True


def test_hypothesis_is_truncated():
    """卡片只放摘要：80 字假设必须截到 60 字 + 省略号，否则一张卡撑满屏。"""
    card = build_progress_card(run={'run_uuid': 'sr-1', 'status': 'running'},
                               iters=_iters(1))
    hypo = card['rounds'][0]['hypothesis']
    assert hypo.endswith('…')
    assert len(hypo) <= 61


def test_empty_rounds_at_start():
    card = build_progress_card(run={'run_uuid': 'sr-1', 'status': 'running'})
    assert card['rounds'] == []
    assert card['latest']['round'] is None
    assert card['round_done'] == 0


def test_payload_json_serialisable():
    """卡片块最终要写成 JSON 文本嵌进 markdown 围栏，不能有非序列化对象。"""
    card = build_progress_card(run={'run_uuid': 'sr-1', 'status': 'done'},
                               iters=_iters(2))
    text = json.dumps(card, ensure_ascii=False)
    assert 'strategy-card' not in text            # 围栏标记由 render_card_block 加
    assert json.loads(text)['kind'] == 'progress'


def test_build_result_payload_returns_none_for_unknown_run(monkeypatch):
    """run 不存在要返回 None（路由据此回 code!=0），不能抛异常。"""
    from agents.shared import strategy_session_runner as runner
    monkeypatch.setattr(runner, '_get_run', lambda uuid: None)
    assert runner.build_result_payload('SR-NOT-EXIST') is None


def test_amend_action_is_discoverable():
    """「不满意，改一下」是可发现性要求：标签直白，fill 仍是半句话。"""
    from agents.shared.strategy_card import build_result_card
    card = build_result_card(run={'run_uuid': 'sr-1', 'idea': '想法', 'universe': '600519',
                                  'period': '2024-01-01~2025-01-01'})
    acts = {a['id']: a for a in card['actions']}
    assert acts['add_condition']['label'] == '不满意，改一下'
    assert acts['add_condition']['fill'].endswith('：')
    assert '再加' in acts['add_condition']['fill']
