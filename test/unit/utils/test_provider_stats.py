"""按供应商统计（用户 2026-10-14 要求）：每次跑完 / 月历 / 大屏三处同口径。

同一模型可能由不同供应商提供（官方 / 中转 / 内网免费源），价格与缓存行为都不同 ——
只按「模型」统计会把它们混成一行，成本与缓存命中率都会被平均掉。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.logger import RequestContext, RadarLogger, _format_by_provider


def test_format_by_provider_shows_all_five_dims():
    line = _format_by_provider({
        'opencode-zen': {'calls': 10, 'in': 106300, 'out': 13800, 'cached': 63232,
                         'reasoning': 9554, 'cost': 1.2345},
        'deepseek': {'calls': 2, 'in': 1000, 'out': 200, 'cached': 0, 'reasoning': 0, 'cost': 0.0},
    })
    assert 'opencode-zen' in line and 'deepseek' in line
    assert '106.3k+13.8k' in line          # 输入+输出
    assert '缓存63.2k(59%)' in line        # 缓存命中 + 命中率
    assert '思考9.6k' in line
    assert '¥1.2345' in line               # 成本
    assert 'deepseek 2次 1k+200' in line   # _fmt_token_count 会把 1.0k 压成 1k


def test_format_by_provider_empty():
    assert _format_by_provider({}) == ''


def test_record_llm_aggregates_by_provider():
    """record_llm 收到 provider 时要单独成桶（token 五件套 + 成本）"""
    ctx = RequestContext('t', RadarLogger(__import__('logging').getLogger('t')))
    ctx.record_llm(1000, 200, 300, 50, 'agent', model='deepseek-v4.1-flash',
                   provider='opencode-zen',
                   cost={'total': 0.5, 'billing': 'paid', 'provider': 'opencode-zen',
                         'model': 'deepseek-v4.1-flash', 'cached_saved': 0.01})
    ctx.record_llm(2000, 100, 0, 0, 'agent', model='deepseek-v4.1-flash',
                   provider='deepseek',
                   cost={'total': 1.5, 'billing': 'paid', 'provider': 'deepseek',
                         'model': 'deepseek-v4.1-flash', 'cached_saved': 0.0})
    by = ctx.metrics['llm_by_provider']
    assert by['opencode-zen']['calls'] == 1 and by['opencode-zen']['cached'] == 300
    assert by['opencode-zen']['reasoning'] == 50 and by['opencode-zen']['cost'] == 0.5
    assert by['deepseek']['in'] == 2000 and by['deepseek']['cost'] == 1.5
