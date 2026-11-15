"""策略研发技能「槽位闸门」（兜底层）回归测试。

背景（设计文档 §5.4 D1）：`create_strategy_research(idea, universe="600519", days=1095)`
的默认标的会让 LLM 不填 universe 时直接拿茅台开跑 8 轮迭代。本任务把默认值改空、
在工具入口加槽位闸门，作为「意图判定（L1/L2/L3）→ 图前 intercept_strategy_turn →
工具入口」三层里的最后一道兜底：宁可多问一句，不许用默认标的代替。

工具落点：`tools/skills` 非标准包（SkillRegister 按文件动态加载，且 `tools/skills.py`
模块会遮蔽同名目录），测试用 importlib spec_from_file_location 同款方式加载 main.py。

跑法：
    venv/Scripts/python.exe -m pytest test/unit/tools/test_strategy_research_slot_gate.py -v
"""
import importlib.util
import inspect
import json
from datetime import datetime
from pathlib import Path

import pytest

_MAIN_PY = Path(__file__).resolve().parents[3] / "tools" / "skills" / "strategy_research" / "main.py"

pytestmark = pytest.mark.unit


def _load_skill_module():
    spec = importlib.util.spec_from_file_location("strategy_research_main_under_test", _MAIN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def skill_mod():
    return _load_skill_module()


@pytest.fixture()
def calls(monkeypatch):
    """替换 start_strategy_run 并记录调用参数。

    ⚠️ 技能方法内部是函数体局部 import（`from server.routes.strategy_research import
    start_strategy_run`），所以补丁必须打在**源模块属性**上：技能模块上根本没有
    `start_strategy_run` 这个属性。
    """
    import server.routes.strategy_research as sr_routes

    recorded = []

    def _fake(idea, universe, period_start=None, period_end=None,
              source='panel', dialog_uuid=None, entry_timing=None,
              exit_timing=None):
        recorded.append({
            'idea': idea, 'universe': universe,
            'period_start': period_start, 'period_end': period_end,
            'source': source, 'dialog_uuid': dialog_uuid,
            'entry_timing': entry_timing, 'exit_timing': exit_timing,
        })
        return {'run_uuid': 'SR-TEST', 'task_id': 'SR-TEST',
                'period': period_start + '~' + period_end, 'events_url': '/x'}

    monkeypatch.setattr(sr_routes, 'start_strategy_run', _fake)
    return recorded


def _run(skill_mod, **kwargs) -> dict:
    """调技能并把 @skill_tool 包装后的 JSON 字符串解回 dict。"""
    skill = skill_mod.StrategyResearchSkill(logger=None, memory_mgr=None)
    return json.loads(skill.create_strategy_research(**kwargs))


def _days_between(start: str, end: str) -> int:
    return (datetime.strptime(end, '%Y-%m-%d') - datetime.strptime(start, '%Y-%m-%d')).days


class TestUniverseGate:
    def test_default_universe_is_empty(self, skill_mod):
        """危险默认值必须消失：签名默认值不再是 600519。"""
        sig = inspect.signature(skill_mod.StrategyResearchSkill.create_strategy_research)
        assert sig.parameters['universe'].default == ''

    def test_missing_universe_asks_instead_of_defaulting(self, skill_mod, calls):
        """例 1：universe 为空 → need_clarify，且不调用 start_strategy_run。"""
        res = _run(skill_mod, idea='尾盘买入第二天卖出这个策略如何', universe='')
        assert res['code'] == 1
        assert res['need_clarify'] is True
        assert 'universe' in res['missing']
        assert calls == []

    def test_concrete_code_runs_once_with_that_code(self, skill_mod, calls):
        """例 2：universe='600519' → 正常调用一次，且透传的就是这只票。"""
        res = _run(skill_mod, idea='收盘价上穿 5 日均线买入，次日开盘卖出', universe='600519')
        assert len(calls) == 1
        assert calls[0]['universe'] == '600519'
        assert calls[0]['source'] == 'skill'
        assert calls[0]['idea'] == '收盘价上穿 5 日均线买入，次日开盘卖出'
        assert calls[0]['entry_timing'] == 'next_open', '缺省口径要透传，否则研发链路跑的不是用户要的口径'
        assert res['run_uuid'] == 'SR-TEST'

    def test_us_stock_code_needs_clarify(self, skill_mod, calls):
        """例 3：universe='AAPL'（美股本期不支持）→ need_clarify，不调用。"""
        res = _run(skill_mod, idea='MACD 金叉买入，持有 5 天卖出', universe='AAPL')
        assert res['need_clarify'] is True
        assert res['missing'] == ['universe']
        assert calls == []

    def test_prefixed_code_normalized_to_six_digits(self, skill_mod, calls):
        """带 sh/sz/bj 前缀的代码要归一：下游 start_strategy_run 只接受 ^\\d{6}$。"""
        _run(skill_mod, idea='RSI 跌破 30 买入，次日收盘卖出', universe='SH600519')
        assert len(calls) == 1
        assert calls[0]['universe'] == '600519'


class TestIdeaAndDaysGate:
    def test_empty_idea_needs_clarify(self, skill_mod, calls):
        """例 4：idea 为空 → need_clarify，missing 含 'idea'。"""
        res = _run(skill_mod, idea='   ', universe='600519')
        assert res['need_clarify'] is True
        assert 'idea' in res['missing']
        assert calls == []

    def test_entry_timing_passed_through(self, skill_mod, calls):
        """成交口径要透传到研究任务：same_close = 「尾盘买入」口径。"""
        _run(skill_mod, idea='尾盘买入第二天卖出', universe='600519',
             entry_timing='same_close')
        assert len(calls) == 1
        assert calls[0]['entry_timing'] == 'same_close'
        # 卖出侧默认空串 = 跟随买入（不能悄悄变成 next_open）
        assert calls[0]['exit_timing'] == ''

    def test_exit_timing_passed_through(self, skill_mod, calls):
        """买卖不同口径（尾盘买、次日开盘卖）必须能经技能入口表达出来。"""
        _run(skill_mod, idea='尾盘买入第二天开盘卖出', universe='600519',
             entry_timing='same_close', exit_timing='next_open')
        assert len(calls) == 1
        assert calls[0]['entry_timing'] == 'same_close'
        assert calls[0]['exit_timing'] == 'next_open'

    def test_exit_timing_defaults_to_follow(self, skill_mod):
        """签名默认值是空串（跟随买入），不是 next_open。"""
        sig = inspect.signature(skill_mod.StrategyResearchSkill.create_strategy_research)
        assert sig.parameters['exit_timing'].default == ''

    def test_days_clamped_to_3650(self, skill_mod, calls):
        """例 5：days=99999 → 钳到 3650（约 10 年），不是 99999 天。"""
        _run(skill_mod, idea='均线多头排列买入，跌破 10 日线卖出',
             universe='600519', days=99999)
        assert len(calls) == 1
        delta = _days_between(calls[0]['period_start'], calls[0]['period_end'])
        assert 3649 <= delta <= 3650, f'period 跨度应为约 3650 天，实际 {delta}'
